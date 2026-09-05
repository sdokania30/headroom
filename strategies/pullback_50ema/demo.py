"""End-to-end walkthrough on synthetic data.

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


def intraday_series(open_minute_volumes: list[float]) -> list[Candle]:
    bars = [
        Candle(SESSION_OPEN + timedelta(minutes=i), 182.0, 183.0, 181.5, 182.5, v)
        for i, v in enumerate(open_minute_volumes)
    ]
    # Quiet drift, then a slide that closes through the Chandelier line.
    price = 182.5
    for i in range(len(bars), 120):
        price += 0.4 if i < 90 else -3.0
        bars.append(
            Candle(SESSION_OPEN + timedelta(minutes=i), price, price + 0.6, price - 0.6, price, 2e4)
        )
    return bars


def main() -> None:
    h4 = h4_series()
    adv = 1_000_000.0

    print("=== Stage 1-3: 4H structural scan ===")
    for s in scan_4h(h4, CONFIG):
        print(
            f"  {s.direction.name:<5} pullback@{s.pullback_start_index} "
            f"bars={s.pullback_length} status={s.status.value} "
            f"entry={s.entry_price} stop={s.protective_stop}"
        )

    for label, volumes in (
        ("RVOL breach", [12_000.0] * 10),
        ("RVOL miss", [500.0] * 10),
    ):
        print(f"\n=== {label} ===")
        plan = build_trade_plan(h4, intraday_series(volumes), SESSION_OPEN, adv, CONFIG)
        if plan is None:
            print("  no actionable 4H setup")
            continue
        print(f"  setup      : {plan.setup.direction.name} {plan.setup.status.value}")
        print(f"  entry      : {plan.setup.entry_price}  (stop {plan.setup.protective_stop})")
        print(f"  {plan.rvol}")
        print(f"  breach at  : {plan.rvol.breach_ts}")
        print(f"  executable : {plan.executable}")
        if plan.exit_signal:
            e = plan.exit_signal
            print(f"  exit       : {e.ts} close={e.close:.2f} < chandelier={e.stop:.2f}")
        else:
            print("  exit       : none in window (hold the swing)")


if __name__ == "__main__":
    main()
