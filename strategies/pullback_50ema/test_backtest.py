"""Tests for the walk-forward backtester."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, time, timedelta

from backtest import (
    BacktestConfig,
    daily_bars,
    demo_data,
    format_blotter,
    format_summary,
    format_sweep,
    htf_bars,
    load_csv,
    run_backtest,
    run_sweep,
    session_bars,
    summarise,
    write_trades_csv,
)
from strategy import Candle, StrategyConfig

DAY0 = datetime(2025, 1, 6, 9, 15)
CFG = BacktestConfig(use_h4=False, tick_size=0.05, slippage_ticks=2, commission_pct=0.0003)
SCFG = StrategyConfig(tick_size=0.05)


def flat_session(day: datetime, price: float = 100.0, bar1_vol: float = 1000.0,
                 vol: float = 1000.0, bars: int = 75) -> list[Candle]:
    return [
        Candle(day + timedelta(minutes=5 * b), price, price + 0.5, price - 0.5, price,
               bar1_vol if b == 0 else vol)
        for b in range(bars)
    ]


def quiet_history(n: int = 25, start: datetime = DAY0) -> tuple[list[Candle], datetime]:
    bars: list[Candle] = []
    day = start
    for _ in range(n):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars += flat_session(day)
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return bars, day


def breakout_session(day: datetime, bar1_vol: float) -> list[Candle]:
    """Bar 1 sets the opening range and the volume; bar 2 breaks out; then a ramp."""
    out = [
        Candle(day, 100.0, 101.0, 99.5, 100.5, bar1_vol),
        Candle(day + timedelta(minutes=5), 100.5, 105.0, 100.4, 104.5, 1000.0),
    ]
    price = 104.5
    for b in range(2, 75):
        price += 0.22
        out.append(Candle(day + timedelta(minutes=5 * b), price, price + 0.3, price - 0.2,
                          price, 1000.0))
    return out


class PlumbingTests(unittest.TestCase):
    def test_session_bars_drops_out_of_hours_data(self):
        bars = flat_session(DAY0) + [Candle(DAY0.replace(hour=16), 100, 100, 100, 100, 5)]
        sessions = session_bars(bars, CFG)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0]), 75)

    def test_session_bars_groups_by_calendar_day(self):
        bars, _ = quiet_history(3)
        self.assertEqual(len(session_bars(bars, CFG)), 3)

    def test_daily_bar_aggregates_the_session(self):
        d = daily_bars(session_bars(flat_session(DAY0, price=100.0), CFG))[0]
        self.assertAlmostEqual(d.high, 100.5)
        self.assertAlmostEqual(d.low, 99.5)
        self.assertAlmostEqual(d.volume, 75 * 1000.0)

    def test_htf_bars_anchor_to_the_session_open(self):
        pairs = htf_bars(session_bars(flat_session(DAY0), CFG), 240)
        # 375-minute session -> a full 4H bar plus a 2h15m remainder.
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0][1], DAY0 + timedelta(minutes=240))

    def test_csv_round_trip(self):
        bars, day = quiet_history(25)
        _, nxt = quiet_history(26)
        result = run_backtest(
            bars + breakout_session(day, 8000.0) + flat_session(nxt, price=110.0), CFG, SCFG
        )
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trades.csv")
            write_trades_csv(result, path)
            with open(path) as fh:
                rows = fh.read().strip().splitlines()
        self.assertEqual(rows[0].split(",")[0], "trade")
        self.assertIn("BUY", rows[1])

    def test_load_csv_reads_iso_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bars.csv")
            with open(path, "w") as fh:
                fh.write("timestamp,open,high,low,close,volume\n")
                fh.write("2025-01-06 09:15:00,100,101,99,100.5,1234\n")
            bars = load_csv(path)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].ts, datetime(2025, 1, 6, 9, 15))
        self.assertAlmostEqual(bars[0].volume, 1234.0)


class EngineTests(unittest.TestCase):
    def run_case(self, bar1_vol: float, tail: list[Candle] | None = None, **kw):
        history, day = quiet_history(25)
        bars = history + breakout_session(day, bar1_vol) + (tail or [])
        cfg = BacktestConfig(use_h4=False, tick_size=0.05, **kw)
        return run_backtest(bars, cfg, SCFG)

    def next_session(self, price: float) -> list[Candle]:
        _, day = quiet_history(26)
        return flat_session(day, price=price)

    def test_a_qualifying_day_fills_one_long(self):
        r = self.run_case(8000.0)
        self.assertEqual(len(r.trades) + (1 if r.open_trade else 0), 1)

    def test_entry_is_one_tick_above_the_opening_range_plus_slippage(self):
        r = self.run_case(8000.0, tail=self.next_session(110.0))
        t = r.trades[0]
        # OR high 101.00 -> stop 101.05, plus 2 ticks of slippage.
        self.assertAlmostEqual(t.entry_price, 101.15, places=2)

    def test_chandelier_close_through_books_the_profit(self):
        r = self.run_case(8000.0, tail=self.next_session(110.0))
        t = r.trades[0]
        self.assertEqual(t.exit_reason, "Chandelier")
        self.assertGreater(t.pnl, 0)
        self.assertGreater(t.quantity, 0)
        self.assertAlmostEqual(t.pnl, t.gross_pnl - t.costs, places=6)

    def test_costs_are_charged_on_both_sides(self):
        r = self.run_case(8000.0, tail=self.next_session(110.0))
        t = r.trades[0]
        expected = (t.entry_price * t.quantity + t.exit_price * t.quantity) * 0.0003
        self.assertAlmostEqual(t.costs, expected, places=4)

    def test_a_collapse_hits_the_initial_stop_instead(self):
        r = self.run_case(8000.0, tail=self.next_session(80.0))
        self.assertEqual(r.trades[0].exit_reason, "Init stop")
        self.assertLess(r.trades[0].pnl, 0)

    def test_no_rvol_means_no_trade(self):
        r = self.run_case(1000.0, tail=self.next_session(110.0))
        self.assertEqual(r.trades, [])
        self.assertIsNone(r.open_trade)
        self.assertGreater(r.skipped_no_rvol, 0)

    def test_the_position_is_carried_across_sessions(self):
        r = self.run_case(8000.0, tail=self.next_session(110.0))
        self.assertGreater(r.trades[0].exit_ts.date(), r.trades[0].entry_ts.date())

    def test_eod_exit_closes_the_same_day(self):
        r = self.run_case(8000.0, tail=self.next_session(110.0), eod_exit=True)
        self.assertEqual(r.trades[0].exit_reason, "EOD")
        self.assertEqual(r.trades[0].exit_ts.date(), r.trades[0].entry_ts.date())

    def test_a_window_shorter_than_the_opening_range_never_trades(self):
        r = self.run_case(8000.0, entry_window_minutes=5, or_minutes=5)
        self.assertEqual(r.trades, [])

    def test_the_funnel_accounts_for_every_session(self):
        r = self.run_case(8000.0, tail=self.next_session(110.0))
        self.assertEqual(r.skipped_no_rvol + r.skipped_no_h4 + r.setups_seen, r.sessions)
        self.assertEqual(r.setups_seen - r.skipped_no_breakout, len(r.trades))

    def test_empty_input_is_handled(self):
        r = run_backtest([], CFG, SCFG)
        self.assertEqual(r.sessions, 0)
        self.assertEqual(r.trades, [])


class SummaryTests(unittest.TestCase):
    def test_summary_of_an_empty_run_is_all_zeros(self):
        s = summarise(run_backtest([], CFG, SCFG))
        self.assertEqual(s.trades, 0)
        self.assertEqual(s.net_profit, 0.0)
        self.assertIsNone(s.profit_factor)

    def test_summary_math_on_the_demo_run(self):
        r = run_backtest(demo_data(sessions=120, seed=3), BacktestConfig(), StrategyConfig())
        s = summarise(r)
        self.assertEqual(s.trades, s.wins + s.losses)
        self.assertAlmostEqual(s.net_profit, sum(t.pnl for t in r.trades), places=6)
        self.assertGreaterEqual(s.max_drawdown, 0.0)
        if s.trades:
            self.assertAlmostEqual(s.expectancy, s.net_profit / s.trades, places=6)

    def test_reports_render_without_trades(self):
        r = run_backtest([], CFG, SCFG)
        self.assertIn("No trades", format_blotter(r))
        self.assertIn("PERFORMANCE", format_summary(r))

    def test_blotter_lists_buy_and_sell_for_each_trade(self):
        history, day = quiet_history(25)
        _, nxt = quiet_history(26)
        r = run_backtest(history + breakout_session(day, 8000.0) + flat_session(nxt, price=110.0),
                         CFG, SCFG)
        text = format_blotter(r)
        self.assertIn("BUY", text)
        self.assertIn("SELL", text)
        self.assertIn("Chandelier", text)

    def test_demo_data_is_shaped_like_a_session(self):
        bars = demo_data(sessions=3)
        self.assertEqual(len(bars), 225)
        self.assertEqual(bars[0].ts.time(), time(9, 15))


if __name__ == "__main__":
    unittest.main()


class OrWidthFilterTests(unittest.TestCase):
    """Opening-range width filter: skip days whose first bar is a gap-driven monster."""

    def build(self, or_range: float, max_or_atr):
        history, day = quiet_history(25)
        _, nxt = quiet_history(26)
        wide = [
            Candle(day, 100.0, 100.0 + or_range, 99.5, 100.5, 8000.0),
            Candle(day + timedelta(minutes=5), 100.5, 100.0 + or_range + 6, 100.4,
                   100.0 + or_range + 5, 1000.0),
        ]
        price = 100.0 + or_range + 5
        for b in range(2, 75):
            price += 0.22
            wide.append(Candle(day + timedelta(minutes=5 * b), price, price + 0.3, price - 0.2,
                               price, 1000.0))
        cfg = BacktestConfig(use_h4=False, tick_size=0.05, max_or_atr=max_or_atr)
        return run_backtest(history + wide + flat_session(nxt, price=110.0), cfg, SCFG)

    def test_a_narrow_open_still_trades_with_the_filter_on(self):
        # Quiet history gives a daily ATR near 1.0; a 1.0-wide range passes at 2x.
        r = self.build(or_range=1.0, max_or_atr=2.0)
        self.assertEqual(len(r.trades), 1)
        self.assertEqual(r.skipped_wide_or, 0)

    def test_a_wide_open_is_skipped(self):
        r = self.build(or_range=12.0, max_or_atr=2.0)
        self.assertEqual(r.trades, [])
        self.assertEqual(r.skipped_wide_or, 1)

    def test_the_same_wide_open_trades_with_the_filter_off(self):
        r = self.build(or_range=12.0, max_or_atr=None)
        self.assertEqual(len(r.trades), 1)
        self.assertEqual(r.skipped_wide_or, 0)

    def test_the_filter_appears_in_the_summary(self):
        self.assertIn("wide open", format_summary(self.build(12.0, 2.0)))


class SweepTests(unittest.TestCase):
    CANDLES = demo_data(sessions=60, seed=11)

    def sweep(self, **kw):
        params = dict(or_list=[5], window_list=[15, 30], rvol_list=[9.0], h4_list=[True, False])
        params.update(kw)
        return run_sweep(self.CANDLES, BacktestConfig(), StrategyConfig(), **params)

    def test_the_grid_covers_every_valid_combination(self):
        self.assertEqual(len(self.sweep()), 4)

    def test_combinations_that_can_never_fill_are_skipped(self):
        rows = self.sweep(or_list=[5], window_list=[5, 15])
        self.assertTrue(all(r.entry_window > r.or_minutes for r in rows))
        self.assertEqual(len(rows), 2)

    def test_each_row_matches_a_direct_run(self):
        row = self.sweep(window_list=[30], h4_list=[False])[0]
        direct = summarise(run_backtest(
            self.CANDLES,
            BacktestConfig(entry_window_minutes=30, rvol_window_minutes=30, use_h4=False),
            StrategyConfig(),
        ))
        self.assertEqual(row.trades, direct.trades)
        self.assertAlmostEqual(row.net_profit, direct.net_profit, places=6)

    def test_a_looser_threshold_never_sees_fewer_rvol_days(self):
        rows = {r.rvol_pct: r for r in self.sweep(rvol_list=[5.0, 15.0], h4_list=[False],
                                                  window_list=[30])}
        self.assertLessEqual(rows[5.0].days_no_rvol, rows[15.0].days_no_rvol)

    def test_the_report_renders_and_marks_a_best_row(self):
        text = format_sweep(self.sweep())
        self.assertIn("net P&L", text)
        self.assertIn("best net P&L", text)
        self.assertIn("*", text)

    def test_an_empty_grid_renders_safely(self):
        self.assertIn("No valid parameter combinations", format_sweep([]))
