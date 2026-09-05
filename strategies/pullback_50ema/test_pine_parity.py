"""Parity check between the Pine state machine and the tested Python reference.

``pine/rvol_hod_swing_long.pine`` re-implements the 4H pullback state machine in
Pine Script, where it cannot be executed here. This module transliterates that
Pine block line-for-line into Python and asserts it fires the same breakouts, at
the same bars, at the same trigger prices, as ``scan_4h`` -- on randomised
walks as well as hand-built edge cases.

It validates the ALGORITHM, not the Pine syntax. The .pine file still has to be
compiled on TradingView.
"""

from __future__ import annotations

import random
import unittest

from strategy import (
    Candle,
    SetupStatus,
    StrategyConfig,
    atr,
    ema,
    scan_4h,
)
from test_strategy import T0, BAR, extend, uptrend


def pine_state_machine(
    candles, ema_len=50, atr_len=14, trend_bars=3, sep_atr=1.0,
    min_pb=2, max_pb=6, valid_bars=3, mintick=0.01,
):
    """Line-for-line transliteration of the `if newH4 ...` block in the .pine file.

    Returns the list of (bar index, trigger price) breakouts it fires.
    """
    ema_vals = ema([c.close for c in candles], ema_len)
    atr_vals = atr(candles, atr_len)

    run_side, run_len, run_sep = 0, 0, 0.0
    pb_count, pb_first_high = 0, None
    h4_trigger, since_arm, since_trig = None, 0, None
    fires = []

    for i, bar_ in enumerate(candles):
        e, a = ema_vals[i], atr_vals[i]
        if e is None or a is None or a <= 0:
            continue
        o, h, c = bar_.open, bar_.high, bar_.close

        side = 1 if c > e else (-1 if c < e else 0)
        prev_side = run_side
        if side != 0 and side == run_side:
            run_len += 1
        else:
            run_side = side
            run_len = 1 if side != 0 else 0
            run_sep = 0.0
        if side != 0:
            run_sep = max(run_sep, abs(c - e) / a)
        flipped_down = side == -1 and prev_side == 1

        fired = False
        kill = False

        if h4_trigger is not None and h >= h4_trigger:
            fired = True
            kill = True
        elif flipped_down:
            kill = True
        elif c < o:
            if max(o, c) < e:
                kill = True
            else:
                if pb_count == 0:
                    if run_side == 1 and run_len >= trend_bars and run_sep >= sep_atr:
                        pb_count = 1
                        pb_first_high = h
                else:
                    pb_count += 1
                if pb_count > max_pb:
                    kill = True
                elif pb_count >= min_pb and pb_first_high is not None:
                    h4_trigger = pb_first_high + mintick
                    since_arm = 0
        else:
            if pb_count > 0 and h4_trigger is None:
                pb_count, pb_first_high = 0, None
            elif h4_trigger is not None:
                since_arm += 1
                if since_arm > valid_bars:
                    kill = True

        if fired:
            fires.append((i, h4_trigger))
        if kill:
            pb_count, pb_first_high, h4_trigger, since_arm = 0, None, None, 0
        if fired:
            since_trig = 0
        elif since_trig is not None:
            since_trig += 1

    return fires


def reference_fires(candles, cfg):
    return [
        (s.resolved_at_index, s.entry_price)
        for s in scan_4h(candles, cfg)
        if s.status is SetupStatus.TRIGGERED
    ]


def random_walk(n: int, seed: int) -> list[Candle]:
    rnd = random.Random(seed)
    price = 100.0
    out = []
    for i in range(n):
        drift = rnd.gauss(0.05, 1.4)
        open_ = price
        close = max(1.0, price + drift)
        rng = abs(rnd.gauss(0.0, 0.8)) + 0.05
        out.append(
            Candle(
                ts=T0 + i * BAR,
                open=open_,
                high=max(open_, close) + rng,
                low=min(open_, close) - rng,
                close=close,
                volume=1000.0,
            )
        )
        price = close
    return out


CFG = StrategyConfig(tick_size=0.01, long_only=True)
PINE_KW = dict(
    ema_len=CFG.ema_period, atr_len=CFG.atr_period, trend_bars=CFG.trend_confirm_bars,
    sep_atr=CFG.min_separation_atr, min_pb=CFG.min_pullback_bars, max_pb=CFG.max_pullback_bars,
    valid_bars=CFG.trigger_valid_bars, mintick=CFG.tick_size,
)


class PineParityTests(unittest.TestCase):
    def assert_parity(self, candles, msg=""):
        self.assertEqual(pine_state_machine(candles, **PINE_KW), reference_fires(candles, CFG), msg)

    def test_parity_on_random_walks(self):
        fired_total = 0
        for seed in range(60):
            candles = random_walk(400, seed)
            pine = pine_state_machine(candles, **PINE_KW)
            self.assertEqual(pine, reference_fires(candles, CFG), f"seed {seed}")
            fired_total += len(pine)
        # Guard against a vacuous pass where neither implementation ever fires.
        self.assertGreater(fired_total, 20, "random walks produced too few breakouts to be meaningful")

    def test_parity_on_the_textbook_long(self):
        self.assert_parity(extend(uptrend(), (181.0, 178.0), (178.0, 176.0), (176.5, 184.0)))

    def test_parity_on_an_ema_slice_cancel(self):
        self.assert_parity(extend(uptrend(), (181.0, 178.0), (178.0, 176.0), (154.0, 150.0)))

    def test_parity_when_only_one_pullback_bar_prints(self):
        self.assert_parity(extend(uptrend(), (181.0, 178.0), (178.0, 180.0), (180.0, 186.0)))

    def test_parity_on_an_overlong_pullback(self):
        self.assert_parity(
            extend(uptrend(), *[(181.0 - i, 180.0 - i) for i in range(8)], (174.0, 190.0))
        )

    def test_parity_on_a_trend_flip(self):
        self.assert_parity(extend(uptrend(), (181.0, 178.0), (178.0, 176.0), (176.0, 150.0), (151.0, 185.0)))

    def test_both_implementations_fire_on_the_textbook_case(self):
        candles = extend(uptrend(), (181.0, 178.0), (178.0, 176.0), (176.5, 184.0))
        fires = pine_state_machine(candles, **PINE_KW)
        self.assertEqual(len(fires), 1)
        self.assertEqual(fires[0][0], 82)
        self.assertAlmostEqual(fires[0][1], candles[80].high + 0.01)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Intraday ORB entry: Pine transliteration vs find_orb_entry
# ---------------------------------------------------------------------------

from datetime import datetime as _dt, timedelta as _td  # noqa: E402

from strategy import find_orb_entry  # noqa: E402

SESSION_OPEN = _dt(2026, 1, 5, 9, 15)


def pine_orb_entry(bars, session_open, adv, tick, or_min, entry_win, rvol_min, thr, tf_mins=5.0):
    """Transliteration of the entry block in the .pine file.

    Pine evaluates a bar at its close: accumulate volume, extend the opening
    range, latch RVOL, then place or cancel the resting order. The broker
    emulator fills that order during the FOLLOWING bar.
    """
    cum = 0.0
    or_high = None
    rvol_ok = False
    pending = None
    bad_config = entry_win <= or_min

    for i, c in enumerate(bars):
        m = (c.ts - session_open).total_seconds() / 60.0
        if m < 0:
            continue
        if pending is not None and c.high >= pending:
            return (True, i, pending)
        cum += c.volume
        if m < or_min:
            or_high = c.high if or_high is None else max(or_high, c.high)
        if m < rvol_min and adv > 0 and cum / adv >= thr:
            rvol_ok = True
        nxt = m + tf_mins
        may = (nxt >= or_min) and (nxt < entry_win) and not bad_config
        pending = or_high + tick if (may and rvol_ok and or_high is not None) else None
    return (False, None, None if or_high is None else or_high + tick)


def random_intraday(n: int, seed: int):
    rnd = random.Random(seed)
    price = 100.0
    out = []
    for i in range(n):
        price += rnd.gauss(0.0, 0.9)
        rng = abs(rnd.gauss(0.0, 0.6)) + 0.05
        out.append(
            Candle(
                ts=SESSION_OPEN + _td(minutes=5 * i),
                open=price,
                high=price + rng,
                low=price - rng,
                close=price,
                volume=rnd.choice([200.0, 1500.0, 4000.0, 9500.0]),
            )
        )
    return out


class OrbEntryParityTests(unittest.TestCase):
    ADV = 100_000.0
    TICK = 0.05
    THR = 0.09

    WINDOWS = [(5, 15, 15), (5, 10, 10), (5, 15, 10), (10, 15, 15), (5, 30, 15), (5, 5, 15)]

    def test_parity_across_window_settings_and_random_sessions(self):
        fills = 0
        for or_min, entry_win, rvol_min in self.WINDOWS:
            for seed in range(120):
                bars = random_intraday(8, seed)
                pine = pine_orb_entry(
                    bars, SESSION_OPEN, self.ADV, self.TICK, or_min, entry_win, rvol_min, self.THR
                )
                ref = find_orb_entry(
                    bars, SESSION_OPEN, self.ADV, self.TICK,
                    or_minutes=or_min, entry_window_minutes=entry_win,
                    rvol_window_minutes=rvol_min, rvol_threshold=self.THR,
                )
                ctx = f"or={or_min} win={entry_win} rvol={rvol_min} seed={seed}"
                self.assertEqual(pine[0], ref.filled, ctx)
                if ref.filled:
                    self.assertEqual(pine[1], ref.fill_index, ctx)
                    self.assertAlmostEqual(pine[2], ref.level, msg=ctx)
                    fills += 1
        self.assertGreater(fills, 50, "too few fills for the parity check to be meaningful")

    def test_the_default_5_15_setup_fills_on_the_second_bar(self):
        bars = [
            Candle(SESSION_OPEN, 100.0, 101.0, 99.0, 100.0, 9000.0),
            Candle(SESSION_OPEN + _td(minutes=5), 100.0, 105.0, 99.0, 104.0, 500.0),
        ]
        self.assertEqual(
            pine_orb_entry(bars, SESSION_OPEN, self.ADV, self.TICK, 5, 15, 15, self.THR),
            (True, 1, 101.05),
        )

    def test_the_default_5_15_setup_also_fills_on_the_third_bar(self):
        bars = [
            Candle(SESSION_OPEN, 100.0, 101.0, 99.0, 100.0, 4000.0),
            Candle(SESSION_OPEN + _td(minutes=5), 100.0, 100.9, 99.0, 100.5, 5000.0),
            Candle(SESSION_OPEN + _td(minutes=10), 100.5, 106.0, 100.0, 105.0, 500.0),
        ]
        self.assertEqual(
            pine_orb_entry(bars, SESSION_OPEN, self.ADV, self.TICK, 5, 15, 15, self.THR),
            (True, 2, 101.05),
        )

    def test_the_fourth_bar_is_outside_the_window(self):
        bars = [
            Candle(SESSION_OPEN, 100.0, 101.0, 99.0, 100.0, 9000.0),
            Candle(SESSION_OPEN + _td(minutes=5), 100.0, 100.9, 99.0, 100.5, 500.0),
            Candle(SESSION_OPEN + _td(minutes=10), 100.5, 100.9, 100.0, 100.5, 500.0),
            Candle(SESSION_OPEN + _td(minutes=15), 100.5, 200.0, 100.0, 199.0, 500.0),
        ]
        self.assertEqual(
            pine_orb_entry(bars, SESSION_OPEN, self.ADV, self.TICK, 5, 15, 15, self.THR)[0], False
        )
