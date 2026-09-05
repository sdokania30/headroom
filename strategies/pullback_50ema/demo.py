"""End-to-end walkthrough on synthetic data.

5-minute opening range, 15-minute entry window, 9% RVOL, long only.

    python3 strategies/pullback_50ema/demo.py
"""

from __future__ import annotations

from datetime import datetime, timedelta

from strategy import (
    Candle,
    Direction,
    SetupStatus,
    StrategyConfig,
    build_trade_plan,
    scan_4h,
)

H4_START = datetime(2026, 1, 1, 1, 0)
SESSION_OPEN = datetime(2026, 1, 15, 9, 15)
CONFIG = StrategyConfig(tick_size=0.05)  # NSE cash-segment tick


def h4_series() -> list[Candle]:
    """80 rising 4H bars, then a 2-bar pullback, then the breakout bar."""
    candles = [
        Candle(H4_START + i * timedelta(hours=4), 100 + i, 101 + i + 1, 100 + i - 1, 101 + i, 5e5)
        for i in range(80)
    ]
    spec = [(181.0, 178.0), (178.0, 176.0), (176.5, 184.0)]
    for j, (o, cl) in enumerate(spec):
        candles.append(
            Candle(H4_START + (80 + j) * timedelta(hours=4), o, max(o, cl) + 1, min(o, cl) - 1, cl, 5e5)
        )
    return candles


def intraday_series(bar1_volume: float) -> list[Candle]:
    """5-minute bars. Bar 1 sets the opening range and carries the volume;
    bar 2 breaks out; then a drift up and a slide through the Chandelier."""
    bars = [
        Candle(SESSION_OPEN, 182.0, 183.0, 181.5, 182.5, bar1_volume),
        Candle(SESSION_OPEN + timedelta(minutes=5), 182.5, 186.0, 182.2, 185.5, 2e4),
    ]
    price = 185.5
    for i in range(2, 60):
        price += 0.4 if i < 45 else -3.0
        bars.append(
            Candle(SESSION_OPEN + timedelta(minutes=5 * i), price, price + 0.6, price - 0.6, price, 2e4)
        )
    return bars


def main() -> None:
    h4 = h4_series()
    adv = 1_000_000.0   # 20-day average daily volume

    print("=== Stage 1-3: 4H structural scan ===")
    for s in scan_4h(h4, CONFIG):
        print(
            f"  {s.direction.name:<5} pullback@{s.pullback_start_index} "
            f"bars={s.pullback_length} status={s.status.value} "
            f"entry={s.entry_price} stop={s.protective_stop}"
        )

    for label, bar1_volume in (("RVOL breach", 120_000.0), ("RVOL miss", 5_000.0)):
        print(f"\n=== {label} ===")
        plan = build_trade_plan(h4, intraday_series(bar1_volume), SESSION_OPEN, adv, CONFIG)
        if plan is None:
            print("  no actionable 4H setup")
            continue
        print(f"  setup      : {plan.setup.direction.name} {plan.setup.status.value}")
        print(f"  4H trigger : {plan.setup.entry_price}  (4H stop {plan.setup.protective_stop})")
        print(f"  OR (5m)    : high {plan.entry.opening_range.high} over {plan.entry.opening_range.bars} bar(s)")
        print(f"  {plan.rvol}")
        print(f"  breach at  : {plan.rvol.breach_ts}")
        print(f"  {plan.entry}")
        print(f"  executable : {plan.executable}")
        if plan.exit_signal:
            e = plan.exit_signal
            print(f"  exit       : {e.ts} close={e.close:.2f} < chandelier={e.stop:.2f}")
        else:
            print("  exit       : none in window (hold the swing)")


if __name__ == "__main__":
    main()
