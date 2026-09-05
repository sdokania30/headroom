"""Unit tests for the 4H 50-EMA pullback strategy. Stdlib only:

    python3 -m unittest discover -s strategies/pullback_50ema -t .
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from strategy import (
    Candle,
    Direction,
    SetupStatus,
    StrategyConfig,
    atr,
    average_daily_volume,
    build_trade_plan,
    chandelier_stops,
    ema,
    evaluate_rvol_gate,
    find_chandelier_exit,
    find_orb_entry,
    opening_range,
    scan_4h,
)

T0 = datetime(2026, 1, 5, 1, 0)
BAR = timedelta(hours=4)
CFG = StrategyConfig(tick_size=0.01, long_only=False)


def bar(i: int, open_: float, close: float, wick: float = 1.0, volume: float = 1000.0) -> Candle:
    return Candle(
        ts=T0 + i * BAR,
        open=open_,
        high=max(open_, close) + wick,
        low=min(open_, close) - wick,
        close=close,
        volume=volume,
    )


def uptrend(n: int = 80, start: float = 100.0, step: float = 1.0) -> list[Candle]:
    """Steadily rising green bars; closes stay far above a lagging 50 EMA."""
    return [bar(i, start + i * step, start + (i + 1) * step) for i in range(n)]


def downtrend(n: int = 80, start: float = 200.0, step: float = 1.0) -> list[Candle]:
    return [bar(i, start - i * step, start - (i + 1) * step) for i in range(n)]


def extend(candles: list[Candle], *specs: tuple[float, float]) -> list[Candle]:
    """Append (open, close) bars after the existing series."""
    out = list(candles)
    for open_, close in specs:
        out.append(bar(len(out), open_, close))
    return out


class IndicatorTests(unittest.TestCase):
    def test_ema_is_sma_seeded(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        out = ema(values, 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)                 # SMA(1,2,3)
        self.assertAlmostEqual(out[3], 4 * 0.5 + 2.0 * 0.5)  # 3.0
        self.assertAlmostEqual(out[4], 5 * 0.5 + 3.0 * 0.5)  # 4.0

    def test_ema_short_series_is_all_none(self):
        self.assertEqual(ema([1.0, 2.0], 5), [None, None])

    def test_atr_wilder_smoothing(self):
        candles = [bar(i, 10.0, 11.0, wick=0.0) for i in range(4)]  # TR = 1 for every bar
        out = atr(candles, 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 1.0)
        self.assertAlmostEqual(out[3], 1.0)


class LongSetupTests(unittest.TestCase):
    def test_two_red_bars_arm_a_long_trigger(self):
        candles = extend(uptrend(), (181.0, 178.0), (178.0, 176.0))
        setup = scan_4h(candles, CFG)[-1]
        self.assertIs(setup.direction, Direction.LONG)
        self.assertIs(setup.status, SetupStatus.ARMED)
        self.assertEqual(setup.pullback_length, 2)
        first_pullback = candles[80]
        self.assertAlmostEqual(setup.anchor_price, first_pullback.high)
        self.assertAlmostEqual(setup.entry_price, first_pullback.high + CFG.tick_size)
        self.assertAlmostEqual(setup.protective_stop, candles[81].low - CFG.tick_size)

    def test_single_red_bar_does_not_arm(self):
        candles = extend(uptrend(), (181.0, 178.0), (178.0, 180.0))
        self.assertEqual([s for s in scan_4h(candles, CFG) if s.entry_price is not None], [])

    def test_breakout_triggers_the_setup(self):
        candles = extend(uptrend(), (181.0, 178.0), (178.0, 176.0), (176.5, 184.0))
        setup = scan_4h(candles, CFG)[-1]
        self.assertIs(setup.status, SetupStatus.TRIGGERED)
        self.assertEqual(setup.resolved_at_index, 82)
        self.assertEqual(setup.resolved_at_ts, candles[82].ts)

    def test_body_through_the_ema_cancels(self):
        # Third pullback bar closes its whole body below the lagging 50 EMA (~156).
        candles = extend(uptrend(), (181.0, 178.0), (178.0, 176.0), (154.0, 150.0))
        setup = scan_4h(candles, CFG)[-1]
        self.assertIs(setup.status, SetupStatus.CANCELLED_EMA_SLICE)
        self.assertEqual(setup.resolved_at_index, 82)

    def test_wick_through_the_ema_is_allowed(self):
        candles = list(uptrend())
        ema_now = ema([c.close for c in candles], 50)[-1]
        # Body stays above the EMA, the lower wick pierces it.
        candles.append(
            Candle(ts=T0 + 80 * BAR, open=181.0, high=182.0, low=ema_now - 5, close=178.0)
        )
        candles.append(bar(81, 178.0, 176.0))
        setup = scan_4h(candles, CFG)[-1]
        self.assertIs(setup.status, SetupStatus.ARMED)
        self.assertTrue(setup.touched_ema_zone)

    def test_entry_anchor_extreme_uses_highest_wick(self):
        # Second pullback bar prints the higher wick.
        candles = list(uptrend())
        candles.append(bar(80, 181.0, 178.0, wick=1.0))    # high 182.0
        candles.append(bar(81, 179.0, 177.0, wick=6.0))    # high 185.0
        first = scan_4h(candles, StrategyConfig(tick_size=0.01, entry_anchor="first", long_only=False))[-1]
        extreme = scan_4h(candles, StrategyConfig(tick_size=0.01, entry_anchor="extreme", long_only=False))[-1]
        self.assertAlmostEqual(first.entry_price, 182.01)
        self.assertAlmostEqual(extreme.entry_price, 185.01)

    def test_pullback_longer_than_max_expires(self):
        cfg = StrategyConfig(tick_size=0.01, max_pullback_bars=3, long_only=False)
        candles = extend(
            uptrend(), (181.0, 180.0), (180.0, 179.0), (179.0, 178.0), (178.0, 177.0)
        )
        setup = scan_4h(candles, cfg)[-1]
        self.assertIs(setup.status, SetupStatus.EXPIRED_PULLBACK_TOO_LONG)

    def test_trigger_expires_without_a_breakout(self):
        cfg = StrategyConfig(tick_size=0.01, trigger_valid_bars=2, long_only=False)
        candles = extend(
            uptrend(),
            (181.0, 178.0),
            (178.0, 176.0),
            (176.0, 176.5),  # green, no breakout (high 177.5 < 182.01)
            (176.5, 177.0),
            (177.0, 177.5),
        )
        setup = scan_4h(candles, cfg)[-1]
        self.assertIs(setup.status, SetupStatus.EXPIRED_NO_BREAKOUT)

    def test_require_ema_touch_withholds_the_trigger(self):
        cfg = StrategyConfig(tick_size=0.01, require_ema_touch=True, long_only=False)
        candles = extend(uptrend(), (181.0, 178.0), (178.0, 176.0))
        setup = scan_4h(candles, cfg)[-1]
        self.assertFalse(setup.touched_ema_zone)
        self.assertIsNone(setup.entry_price)


class LongOnlyTests(unittest.TestCase):
    def test_long_only_is_the_default(self):
        self.assertTrue(StrategyConfig().long_only)

    def test_long_only_suppresses_short_setups(self):
        candles = extend(downtrend(), (119.0, 122.0), (122.0, 124.0))
        self.assertEqual(scan_4h(candles, StrategyConfig(tick_size=0.01)), [])
        self.assertEqual(len(scan_4h(candles, CFG)), 1)

    def test_long_only_still_finds_long_setups(self):
        candles = extend(uptrend(), (181.0, 178.0), (178.0, 176.0))
        setup = scan_4h(candles, StrategyConfig(tick_size=0.01))[-1]
        self.assertIs(setup.direction, Direction.LONG)


class ShortSetupTests(unittest.TestCase):
    def test_two_green_bars_arm_a_short_trigger(self):
        candles = extend(downtrend(), (119.0, 122.0), (122.0, 124.0))
        setup = scan_4h(candles, CFG)[-1]
        self.assertIs(setup.direction, Direction.SHORT)
        self.assertIs(setup.status, SetupStatus.ARMED)
        self.assertAlmostEqual(setup.entry_price, candles[80].low - CFG.tick_size)
        self.assertAlmostEqual(setup.protective_stop, candles[81].high + CFG.tick_size)

    def test_short_breakdown_triggers(self):
        candles = extend(downtrend(), (119.0, 122.0), (122.0, 124.0), (123.5, 115.0))
        setup = scan_4h(candles, CFG)[-1]
        self.assertIs(setup.status, SetupStatus.TRIGGERED)

    def test_short_cancelled_when_body_closes_above_the_ema(self):
        candles = extend(downtrend(), (119.0, 122.0), (122.0, 124.0), (146.0, 150.0))
        setup = scan_4h(candles, CFG)[-1]
        self.assertIs(setup.status, SetupStatus.CANCELLED_EMA_SLICE)


class RvolGateTests(unittest.TestCase):
    OPEN = datetime(2026, 1, 5, 9, 15)

    def minutes(self, volumes: list[float], start_offset: int = 0) -> list[Candle]:
        return [
            Candle(
                ts=self.OPEN + timedelta(minutes=start_offset + i),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=v,
            )
            for i, v in enumerate(volumes)
        ]

    def test_gate_passes_and_records_the_breach_minute(self):
        gate = evaluate_rvol_gate(self.minutes([1000.0] * 10), self.OPEN, adv=100_000)
        self.assertTrue(gate.passed)
        self.assertEqual(gate.bars_used, 10)
        self.assertAlmostEqual(gate.ratio, 0.10)
        self.assertEqual(gate.breach_ts, self.OPEN + timedelta(minutes=8))  # 9,000 = 9%

    def test_gate_fails_below_threshold(self):
        gate = evaluate_rvol_gate(self.minutes([800.0] * 10), self.OPEN, adv=100_000)
        self.assertFalse(gate.passed)
        self.assertIsNone(gate.breach_ts)
        self.assertAlmostEqual(gate.ratio, 0.08)

    def test_volume_after_the_window_is_ignored(self):
        bars = self.minutes([500.0] * 10) + self.minutes([50_000.0] * 5, start_offset=10)
        gate = evaluate_rvol_gate(bars, self.OPEN, adv=100_000)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.bars_used, 10)

    def test_pre_open_volume_is_ignored(self):
        bars = self.minutes([9_000.0], start_offset=-5) + self.minutes([100.0] * 10)
        gate = evaluate_rvol_gate(bars, self.OPEN, adv=100_000)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.bars_used, 10)

    def test_average_daily_volume_uses_the_lookback(self):
        daily = [bar(i, 100.0, 101.0, volume=float(i)) for i in range(30)]
        self.assertAlmostEqual(average_daily_volume(daily, lookback=10), 24.5)

    def test_zero_adv_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_rvol_gate([], self.OPEN, adv=0.0)


class ChandelierTests(unittest.TestCase):
    def test_long_exit_fires_on_a_close_below_the_line(self):
        candles = uptrend(60) + [bar(60, 161.0, 120.0, wick=0.5)]
        signal = find_chandelier_exit(candles, Direction.LONG, entry_index=55)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.index, 60)
        self.assertLess(signal.close, signal.stop)

    def test_no_exit_while_the_trend_holds(self):
        self.assertIsNone(find_chandelier_exit(uptrend(60), Direction.LONG, entry_index=25))

    def test_short_exit_fires_on_a_close_above_the_line(self):
        candles = downtrend(60) + [bar(60, 141.0, 185.0, wick=0.5)]
        signal = find_chandelier_exit(candles, Direction.SHORT, entry_index=55)
        self.assertIsNotNone(signal)
        self.assertGreater(signal.close, signal.stop)

    def test_ratcheted_long_stop_never_falls(self):
        candles = uptrend(60)
        stops = [s for s in chandelier_stops(candles, Direction.LONG, ratchet=True) if s]
        self.assertTrue(all(b >= a for a, b in zip(stops, stops[1:])))

    def test_exit_search_starts_at_the_entry_index(self):
        candles = uptrend(60) + [bar(60, 161.0, 120.0, wick=0.5), bar(61, 120.0, 118.0, wick=0.5)]
        self.assertEqual(find_chandelier_exit(candles, Direction.LONG, entry_index=55).index, 60)
        self.assertEqual(find_chandelier_exit(candles, Direction.LONG, entry_index=61).index, 61)


class OrbEntryTests(unittest.TestCase):
    """5-minute opening range, 15-minute entry window, 9% RVOL. Tick 0.05."""

    OPEN = datetime(2026, 1, 5, 9, 15)
    ADV = 100_000.0
    TICK = 0.05

    def m5(self, specs: list[tuple[float, float]]) -> list[Candle]:
        """(high, volume) per 5-minute bar, starting at the session open."""
        return [
            Candle(
                ts=self.OPEN + timedelta(minutes=5 * i),
                open=100.0,
                high=h,
                low=99.0,
                close=100.0,
                volume=v,
            )
            for i, (h, v) in enumerate(specs)
        ]

    def entry(self, specs, **kw):
        return find_orb_entry(self.m5(specs), self.OPEN, self.ADV, self.TICK, **kw)

    def test_opening_range_is_the_first_five_minutes_only(self):
        rng = opening_range(self.m5([(101.0, 0), (108.0, 0), (109.0, 0)]), self.OPEN, 5)
        self.assertEqual(rng.bars, 1)
        self.assertAlmostEqual(rng.high, 101.0)
        self.assertTrue(rng.complete)

    def test_opening_range_spans_the_bars_inside_its_window(self):
        rng = opening_range(self.m5([(101.0, 0), (108.0, 0), (109.0, 0)]), self.OPEN, 10)
        self.assertEqual(rng.bars, 2)
        self.assertAlmostEqual(rng.high, 108.0)

    def test_entry_level_is_one_tick_above_the_or_high(self):
        e = self.entry([(101.0, 9000.0), (100.5, 100.0)])
        self.assertAlmostEqual(e.level, 101.05)

    def test_fills_on_the_second_bar_when_rvol_latches_on_the_first(self):
        e = self.entry([(101.0, 9000.0), (105.0, 500.0), (106.0, 100.0)])
        self.assertTrue(e.filled)
        self.assertEqual(e.reason, "filled")
        self.assertEqual(e.fill_ts, self.OPEN + timedelta(minutes=5))

    def test_a_late_rvol_latch_still_fills_inside_the_fifteen_minute_window(self):
        # 4% on bar 1, 9% cumulative only at bar 2's close -> bar 3 is the fill.
        e = self.entry([(101.0, 4000.0), (105.0, 5000.0), (106.0, 100.0)])
        self.assertTrue(e.filled)
        self.assertEqual(e.fill_ts, self.OPEN + timedelta(minutes=10))

    def test_the_same_late_latch_misses_a_ten_minute_window(self):
        e = self.entry(
            [(101.0, 4000.0), (105.0, 5000.0), (106.0, 100.0)],
            entry_window_minutes=10,
            rvol_window_minutes=10,
        )
        self.assertFalse(e.filled)
        self.assertTrue(e.rvol.passed)
        self.assertEqual(e.reason, "no_breakout")

    def test_no_fill_when_the_rvol_gate_never_passes(self):
        e = self.entry([(101.0, 100.0), (105.0, 100.0), (106.0, 100.0)])
        self.assertFalse(e.filled)
        self.assertEqual(e.reason, "no_rvol")

    def test_no_fill_when_price_never_takes_out_the_or_high(self):
        e = self.entry([(101.0, 9000.0), (100.9, 100.0), (101.04, 100.0)])
        self.assertFalse(e.filled)
        self.assertEqual(e.reason, "no_breakout")

    def test_bars_after_the_window_cannot_fill(self):
        e = self.entry([(101.0, 9000.0), (100.5, 100.0), (100.5, 100.0), (200.0, 100.0)])
        self.assertFalse(e.filled)

    def test_an_order_cannot_fill_on_the_bar_that_placed_it(self):
        # RVOL latches only at bar 1's close, so bar 1 itself is not fillable.
        e = self.entry([(105.0, 9000.0), (100.5, 100.0)])
        self.assertFalse(e.filled)

    def test_window_not_longer_than_the_opening_range_is_rejected(self):
        e = self.entry([(101.0, 9000.0)], or_minutes=5, entry_window_minutes=5)
        self.assertEqual(e.reason, "window_too_short")

    def test_no_intraday_data_reports_an_incomplete_opening_range(self):
        e = find_orb_entry([], self.OPEN, self.ADV, self.TICK)
        self.assertEqual(e.reason, "or_incomplete")


class EndToEndTests(unittest.TestCase):
    OPEN = datetime(2026, 1, 5, 9, 15)

    def intraday(self, first_bar_volume: float, breakout: bool = True) -> list[Candle]:
        """Bar 1 carries the volume; bar 2 breaks the opening-range high."""
        bars = [
            Candle(self.OPEN, 100.0, 101.0, 99.0, 100.5, first_bar_volume),
            Candle(
                self.OPEN + timedelta(minutes=5),
                100.5,
                105.0 if breakout else 100.9,
                100.0,
                104.0 if breakout else 100.5,
                2000.0,
            ),
        ]
        price = 104.0 if breakout else 100.4
        for i in range(2, 40):
            if not breakout:
                price -= 0.05          # never comes back to the opening-range high
            else:
                price += 0.5 if i < 30 else -4.0
            bars.append(
                Candle(
                    self.OPEN + timedelta(minutes=5 * i),
                    price,
                    price + 0.4,
                    price - 0.4,
                    price,
                    500.0,
                )
            )
        return bars

    def h4(self) -> list[Candle]:
        return extend(uptrend(), (181.0, 178.0), (178.0, 176.0), (176.5, 184.0))

    def test_triggered_setup_with_rvol_breach_and_breakout_is_executable(self):
        plan = build_trade_plan(self.h4(), self.intraday(12_000.0), self.OPEN, 100_000.0, CFG)
        self.assertIsNotNone(plan)
        self.assertIs(plan.setup.status, SetupStatus.TRIGGERED)
        self.assertTrue(plan.entry.filled)
        self.assertTrue(plan.executable)
        self.assertTrue(plan.rvol.passed)

    def test_no_rvol_breach_means_no_entry(self):
        plan = build_trade_plan(self.h4(), self.intraday(100.0), self.OPEN, 100_000.0, CFG)
        self.assertFalse(plan.executable)
        self.assertEqual(plan.entry.reason, "no_rvol")
        self.assertIsNone(plan.exit_signal)

    def test_rvol_breach_without_a_breakout_means_no_entry(self):
        plan = build_trade_plan(
            self.h4(), self.intraday(12_000.0, breakout=False), self.OPEN, 100_000.0, CFG
        )
        self.assertFalse(plan.executable)
        self.assertEqual(plan.entry.reason, "no_breakout")

    def test_the_chandelier_exit_is_found_after_the_fill(self):
        plan = build_trade_plan(self.h4(), self.intraday(12_000.0), self.OPEN, 100_000.0, CFG)
        self.assertIsNotNone(plan.exit_signal)
        self.assertGreaterEqual(plan.exit_signal.index, plan.entry.fill_index)

    def test_no_setup_returns_none(self):
        plan = build_trade_plan(uptrend(), self.intraday(12_000.0), self.OPEN, 100_000.0, CFG)
        self.assertIsNone(plan)


if __name__ == "__main__":
    unittest.main()
