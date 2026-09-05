"""Walk-forward backtester for the RVOL ORB swing strategy (long only).

Mirrors ``pine/rvol_hod_swing_long.pine`` bar for bar, so the numbers here and
the numbers in the TradingView Strategy Tester should agree to within fill
conventions.

    python3 backtest.py --demo                 # synthetic data, no input needed
    python3 backtest.py --csv bars_5m.csv      # your own 5-minute OHLCV
    python3 backtest.py --csv bars_5m.csv --out trades.csv --no-h4 --window 30

CSV format: a header row, then ``timestamp,open,high,low,close,volume``.
Timestamps are ISO-8601 (``2026-01-05 09:15:00``) or epoch seconds, in the
exchange's local time. 5-minute bars.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional, Sequence

from strategy import Candle, StrategyConfig, atr, ema, h4_ready_series


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BacktestConfig:
    # Entry
    or_minutes: int = 5
    entry_window_minutes: int = 15
    rvol_window_minutes: int = 15
    rvol_threshold: float = 0.09
    adv_lookback: int = 20
    use_h4: bool = True
    h4_minutes: int = 240
    # Exit
    chandelier_period: int = 22
    chandelier_multiplier: float = 3.0
    ratchet: bool = True
    use_init_stop: bool = True
    init_atr_mult: float = 1.5
    eod_exit: bool = False
    # Costs and sizing
    capital: float = 1_000_000.0
    commission_pct: float = 0.0003      # 0.03% per side
    slippage_ticks: int = 2
    tick_size: float = 0.05
    # Session
    session_start: time = time(9, 15)
    session_end: time = time(15, 30)


@dataclass(frozen=True)
class Trade:
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str
    quantity: int
    gross_pnl: float
    costs: float
    pnl: float
    pnl_pct: float
    bars_held: int
    mae_pct: float          # worst adverse excursion while open
    mfe_pct: float          # best favourable excursion while open


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    sessions: int = 0
    setups_seen: int = 0
    skipped_no_rvol: int = 0
    skipped_no_breakout: int = 0
    skipped_no_h4: int = 0
    config: Optional[BacktestConfig] = None
    open_trade: Optional[tuple[datetime, float, int]] = None


# ---------------------------------------------------------------------------
# Bar plumbing
# ---------------------------------------------------------------------------

def load_csv(path: str) -> list[Candle]:
    out: list[Candle] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            keys = {k.strip().lower(): v for k, v in row.items() if k}
            raw = keys.get("timestamp") or keys.get("time") or keys.get("date") or keys.get("ts")
            if raw is None:
                raise ValueError("CSV needs a timestamp/time/date column")
            raw = raw.strip()
            try:
                ts = datetime.fromtimestamp(float(raw))
            except ValueError:
                ts = datetime.fromisoformat(raw.replace("Z", "").replace("T", " ").strip())
            out.append(
                Candle(
                    ts=ts,
                    open=float(keys["open"]),
                    high=float(keys["high"]),
                    low=float(keys["low"]),
                    close=float(keys["close"]),
                    volume=float(keys.get("volume") or 0.0),
                )
            )
    out.sort(key=lambda c: c.ts)
    return out


def session_bars(candles: Sequence[Candle], cfg: BacktestConfig) -> list[list[Candle]]:
    """Group bars into trading sessions, dropping anything outside session hours."""
    by_day: dict[date, list[Candle]] = {}
    for c in candles:
        if cfg.session_start <= c.ts.time() < cfg.session_end:
            by_day.setdefault(c.ts.date(), []).append(c)
    return [by_day[d] for d in sorted(by_day) if by_day[d]]


def _aggregate(bars: Sequence[Candle]) -> Candle:
    return Candle(
        ts=bars[0].ts,
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume=sum(b.volume for b in bars),
    )


def daily_bars(sessions: Sequence[Sequence[Candle]]) -> list[Candle]:
    return [_aggregate(s) for s in sessions]


def htf_bars(sessions: Sequence[Sequence[Candle]], minutes: int) -> list[tuple[Candle, datetime]]:
    """Higher-timeframe bars anchored to each session open, as (bar, close_time).

    Anchoring to the session open is what TradingView does for intraday HTF bars
    and is why a 6h15m session yields an odd final 4H bar.
    """
    out: list[tuple[Candle, datetime]] = []
    for s in sessions:
        open_ts = s[0].ts
        buckets: dict[int, list[Candle]] = {}
        for c in s:
            k = int((c.ts - open_ts).total_seconds() // (minutes * 60))
            buckets.setdefault(k, []).append(c)
        for k in sorted(buckets):
            group = buckets[k]
            close_time = group[-1].ts + timedelta(minutes=5)
            out.append((_aggregate(group), close_time))
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def run_backtest(
    candles: Sequence[Candle],
    cfg: Optional[BacktestConfig] = None,
    strategy_config: Optional[StrategyConfig] = None,
) -> BacktestResult:
    cfg = cfg or BacktestConfig()
    scfg = strategy_config or StrategyConfig(tick_size=cfg.tick_size)
    result = BacktestResult(config=cfg)

    sessions = session_bars(candles, cfg)
    if not sessions:
        return result
    result.sessions = len(sessions)

    dailies = daily_bars(sessions)
    daily_atr = atr(dailies, cfg.chandelier_period)

    # 4H readiness, keyed by the time the 4H bar closed.
    h4_ready: list[tuple[datetime, bool]] = []
    if cfg.use_h4:
        pairs = htf_bars(sessions, cfg.h4_minutes)
        flags = h4_ready_series([b for b, _ in pairs], scfg)
        h4_ready = [(close_ts, flag) for (_, close_ts), flag in zip(pairs, flags)]

    def h4_ok(ts: datetime) -> bool:
        if not cfg.use_h4:
            return True
        state = False
        for close_ts, flag in h4_ready:
            if close_ts > ts:
                break
            state = flag
        return state

    slip = cfg.slippage_ticks * cfg.tick_size
    equity = cfg.capital

    # Open-position state, carried across sessions.
    in_pos = False
    entry_price = 0.0
    entry_ts: Optional[datetime] = None
    entry_bar = 0
    qty = 0
    trail: Optional[float] = None
    init_stop: Optional[float] = None
    best = worst = 0.0
    bar_counter = 0

    def close_position(ts: datetime, raw_price: float, reason: str) -> None:
        nonlocal in_pos, equity, trail, init_stop, best, worst
        fill = raw_price - slip
        gross = (fill - entry_price) * qty
        costs = (entry_price * qty + fill * qty) * cfg.commission_pct
        pnl = gross - costs
        equity += pnl
        result.trades.append(
            Trade(
                entry_ts=entry_ts,
                entry_price=entry_price,
                exit_ts=ts,
                exit_price=fill,
                exit_reason=reason,
                quantity=qty,
                gross_pnl=gross,
                costs=costs,
                pnl=pnl,
                pnl_pct=(fill - entry_price) / entry_price * 100.0 if entry_price else 0.0,
                bars_held=bar_counter - entry_bar,
                mae_pct=worst,
                mfe_pct=best,
            )
        )
        in_pos = False
        trail = None
        init_stop = None

    for di, session in enumerate(sessions):
        open_ts = session[0].ts
        # ADV over completed prior sessions only.
        prior = dailies[max(0, di - cfg.adv_lookback) : di]
        adv = sum(b.volume for b in prior) / len(prior) if prior else 0.0
        # Chandelier inputs from completed prior sessions only.
        atr_d = daily_atr[di - 1] if di >= 1 else None
        hh_prior = max((b.high for b in dailies[max(0, di - cfg.chandelier_period) : di]), default=None)

        cum_vol = 0.0
        or_high: Optional[float] = None
        rvol_ok = False
        pending: Optional[float] = None
        traded_today = False
        day_high = -math.inf
        saw_setup = False
        latched_in_window = False

        for bi, bar in enumerate(session):
            bar_counter += 1
            day_high = max(day_high, bar.high)
            mins = (bar.ts - open_ts).total_seconds() / 60.0
            last_bar = bi == len(session) - 1

            # -- fill a resting order placed at the previous bar's close --------
            if not in_pos and pending is not None and bar.high >= pending:
                fill = max(pending, bar.open) + slip     # gap-through fills worse
                qty = int(equity // fill)
                if qty > 0:
                    in_pos = True
                    entry_price = fill
                    entry_ts = bar.ts
                    entry_bar = bar_counter
                    traded_today = True
                    best = worst = 0.0
                    trail = None
                    init_stop = None
                pending = None

            # -- manage an open position ---------------------------------------
            if in_pos:
                move = (bar.close - entry_price) / entry_price * 100.0
                best = max(best, (bar.high - entry_price) / entry_price * 100.0)
                worst = min(worst, (bar.low - entry_price) / entry_price * 100.0)

                raw_trail = None
                if atr_d is not None and hh_prior is not None:
                    raw_trail = max(hh_prior, day_high) - cfg.chandelier_multiplier * atr_d
                if raw_trail is not None:
                    trail = raw_trail if trail is None else (
                        max(trail, raw_trail) if cfg.ratchet else raw_trail
                    )
                if init_stop is None and cfg.use_init_stop and atr_d is not None:
                    init_stop = entry_price - cfg.init_atr_mult * atr_d

                if init_stop is not None and bar.low <= init_stop:
                    close_position(bar.ts, min(init_stop, bar.open), "Init stop")
                elif trail is not None and bar.close < trail:
                    close_position(bar.ts, bar.close, "Chandelier")
                elif cfg.eod_exit and last_bar:
                    close_position(bar.ts, bar.close, "EOD")
                if not in_pos:
                    continue

            # -- accumulate, latch RVOL, then place or cancel the order ---------
            cum_vol += bar.volume
            if mins < cfg.or_minutes:
                or_high = bar.high if or_high is None else max(or_high, bar.high)
            if mins < cfg.rvol_window_minutes and adv > 0 and cum_vol / adv >= cfg.rvol_threshold:
                if not rvol_ok:
                    latched_in_window = True
                rvol_ok = True

            nxt = mins + 5.0
            may_work = (
                cfg.entry_window_minutes > cfg.or_minutes
                and nxt >= cfg.or_minutes
                and nxt < cfg.entry_window_minutes
            )
            gate = rvol_ok and h4_ok(bar.ts)
            if gate and may_work:
                saw_setup = True
            pending = (
                or_high + cfg.tick_size
                if (may_work and gate and or_high is not None and not in_pos and not traded_today)
                else None
            )

        # -- per-session accounting -------------------------------------------
        if saw_setup:
            result.setups_seen += 1
            if not traded_today:
                result.skipped_no_breakout += 1
        elif not latched_in_window:
            result.skipped_no_rvol += 1
        else:
            result.skipped_no_h4 += 1

        mtm = equity + ((session[-1].close - entry_price) * qty if in_pos else 0.0)
        result.equity_curve.append((session[-1].ts, mtm))

    if in_pos:
        result.open_trade = (entry_ts, entry_price, qty)
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Summary:
    trades: int
    wins: int
    losses: int
    win_rate: float
    net_profit: float
    return_pct: float
    gross_profit: float
    gross_loss: float
    profit_factor: Optional[float]
    avg_win: float
    avg_loss: float
    expectancy: float
    best: float
    worst: float
    avg_bars_held: float
    max_drawdown: float
    max_drawdown_pct: float
    total_costs: float


def summarise(result: BacktestResult) -> Summary:
    ts = result.trades
    cap = result.config.capital if result.config else 0.0
    wins = [t for t in ts if t.pnl > 0]
    losses = [t for t in ts if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    net = sum(t.pnl for t in ts)

    peak = cap
    max_dd = 0.0
    max_dd_pct = 0.0
    for _, eq in result.equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100.0)

    return Summary(
        trades=len(ts),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(ts) * 100.0 if ts else 0.0,
        net_profit=net,
        return_pct=net / cap * 100.0 if cap else 0.0,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        avg_win=gross_profit / len(wins) if wins else 0.0,
        avg_loss=-gross_loss / len(losses) if losses else 0.0,
        expectancy=net / len(ts) if ts else 0.0,
        best=max((t.pnl for t in ts), default=0.0),
        worst=min((t.pnl for t in ts), default=0.0),
        avg_bars_held=sum(t.bars_held for t in ts) / len(ts) if ts else 0.0,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        total_costs=sum(t.costs for t in ts),
    )


def _money(x: float) -> str:
    return f"{x:>14,.2f}"


def format_blotter(result: BacktestResult, limit: Optional[int] = None) -> str:
    rows = result.trades if limit is None else result.trades[-limit:]
    if not rows:
        return "No trades.\n"
    head = (
        f"{'#':>3}  {'BUY':<17} {'price':>9}  {'SELL':<17} {'price':>9}  "
        f"{'qty':>7}  {'P&L':>13} {'%':>7}  {'bars':>5}  {'MAE%':>6}  why"
    )
    out = [head, "-" * len(head)]
    offset = len(result.trades) - len(rows)
    for i, t in enumerate(rows, start=offset + 1):
        out.append(
            f"{i:>3}  {t.entry_ts:%Y-%m-%d %H:%M}  {t.entry_price:>9.2f}  "
            f"{t.exit_ts:%Y-%m-%d %H:%M}  {t.exit_price:>9.2f}  {t.quantity:>7}  "
            f"{t.pnl:>13,.2f} {t.pnl_pct:>6.2f}%  {t.bars_held:>5}  {t.mae_pct:>5.1f}%  {t.exit_reason}"
        )
    return "\n".join(out) + "\n"


def format_summary(result: BacktestResult) -> str:
    s = summarise(result)
    cfg = result.config or BacktestConfig()
    pf = "n/a" if s.profit_factor is None else f"{s.profit_factor:.2f}"
    lines = [
        "",
        "PERFORMANCE",
        "-" * 46,
        f"  Starting capital        {_money(cfg.capital)}",
        f"  Net profit              {_money(s.net_profit)}   ({s.return_pct:+.2f}%)",
        f"  Gross profit / loss     {_money(s.gross_profit)} / {s.gross_loss:,.2f}",
        f"  Costs paid              {_money(s.total_costs)}",
        f"  Profit factor           {pf:>14}",
        f"  Closed trades           {s.trades:>14}",
        f"  Win rate                {s.win_rate:>13.1f}%   ({s.wins}W / {s.losses}L)",
        f"  Avg win / avg loss      {_money(s.avg_win)} / {s.avg_loss:,.2f}",
        f"  Expectancy per trade    {_money(s.expectancy)}",
        f"  Best / worst trade      {_money(s.best)} / {s.worst:,.2f}",
        f"  Avg bars held           {s.avg_bars_held:>14.1f}",
        f"  Max drawdown            {_money(s.max_drawdown)}   ({s.max_drawdown_pct:.2f}%)",
        "",
        "SIGNAL FUNNEL",
        "-" * 46,
        f"  Sessions tested         {result.sessions:>14}",
        f"  Days with no RVOL       {result.skipped_no_rvol:>14}",
        f"  Days blocked by 4H      {result.skipped_no_h4:>14}",
        f"  Days armed but no fill  {result.skipped_no_breakout:>14}",
        f"  Days filled             {s.trades + (1 if result.open_trade else 0):>14}",
    ]
    if result.open_trade:
        ts, px, q = result.open_trade
        lines += ["", f"  Still open: {q} @ {px:.2f} from {ts:%Y-%m-%d %H:%M}"]
    return "\n".join(lines) + "\n"


def write_trades_csv(result: BacktestResult, path: str) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "trade", "signal", "timestamp", "price", "quantity", "gross_pnl",
            "costs", "net_pnl", "pnl_pct", "bars_held", "mae_pct", "mfe_pct", "reason",
        ])
        for i, t in enumerate(result.trades, start=1):
            w.writerow([i, "BUY", t.entry_ts.isoformat(), f"{t.entry_price:.4f}",
                        t.quantity, "", "", "", "", "", "", "", "RVOL+ORB"])
            w.writerow([i, "SELL", t.exit_ts.isoformat(), f"{t.exit_price:.4f}",
                        t.quantity, f"{t.gross_pnl:.2f}", f"{t.costs:.2f}", f"{t.pnl:.2f}",
                        f"{t.pnl_pct:.4f}", t.bars_held, f"{t.mae_pct:.2f}",
                        f"{t.mfe_pct:.2f}", t.exit_reason])


# ---------------------------------------------------------------------------
# Synthetic data for --demo
# ---------------------------------------------------------------------------

def demo_data(sessions: int = 160, seed: int = 7) -> list[Candle]:
    """5-minute bars with trending regimes and occasional volume-heavy opens."""
    rnd = random.Random(seed)
    bars: list[Candle] = []
    day = datetime(2025, 1, 6, 9, 15)
    price = 500.0
    drift = 0.02
    for d in range(sessions):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        if d % 22 == 0:
            drift = rnd.choice([0.05, 0.03, -0.03, 0.01])
        spike = rnd.random() < 0.22          # heavy-volume open
        base_vol = rnd.uniform(1200, 1800)
        for b in range(75):                  # 09:15 -> 15:30
            ts = day + timedelta(minutes=5 * b)
            shock = rnd.gauss(drift, 0.9)
            if spike and b == 0:
                shock += rnd.uniform(1.5, 4.0)
            open_ = price
            close = max(1.0, price + shock)
            rng = abs(rnd.gauss(0, 0.6)) + 0.1
            vol = base_vol * rnd.uniform(0.5, 1.6)
            if b == 0:
                vol = base_vol * (rnd.uniform(9.0, 16.0) if spike else rnd.uniform(1.5, 3.0))
            bars.append(
                Candle(ts, open_, max(open_, close) + rng, min(open_, close) - rng, close, vol)
            )
            price = close
        day += timedelta(days=1)
    return bars


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest the RVOL ORB swing strategy (long only).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="5-minute OHLCV file")
    src.add_argument("--demo", action="store_true", help="run on generated synthetic data")
    ap.add_argument("--or", dest="or_minutes", type=int, default=5, help="opening range, minutes")
    ap.add_argument("--window", type=int, default=15, help="entry window, minutes")
    ap.add_argument("--rvol-window", type=int, default=15, help="RVOL latch window, minutes")
    ap.add_argument("--rvol", type=float, default=9.0, help="RVOL threshold, %% of ADV")
    ap.add_argument("--no-h4", action="store_true", help="drop the 4H pullback filter")
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--tick", type=float, default=0.05)
    ap.add_argument("--commission", type=float, default=0.03, help="%% per side")
    ap.add_argument("--slippage", type=int, default=2, help="ticks per side")
    ap.add_argument("--eod-exit", action="store_true", help="flat at the session close")
    ap.add_argument("--out", help="write the signal-by-signal blotter to this CSV")
    ap.add_argument("--list", type=int, default=25, help="trades to print (0 = all)")
    a = ap.parse_args(argv)

    candles = demo_data() if a.demo else load_csv(a.csv)
    if not candles:
        print("No bars loaded.", file=sys.stderr)
        return 1

    cfg = BacktestConfig(
        or_minutes=a.or_minutes,
        entry_window_minutes=a.window,
        rvol_window_minutes=a.rvol_window,
        rvol_threshold=a.rvol / 100.0,
        use_h4=not a.no_h4,
        capital=a.capital,
        tick_size=a.tick,
        commission_pct=a.commission / 100.0,
        slippage_ticks=a.slippage,
        eod_exit=a.eod_exit,
    )
    result = run_backtest(candles, cfg, StrategyConfig(tick_size=a.tick))

    print(f"\nBars {len(candles):,}   sessions {result.sessions}   "
          f"{candles[0].ts:%Y-%m-%d} -> {candles[-1].ts:%Y-%m-%d}")
    print(f"OR {cfg.or_minutes}m  window {cfg.entry_window_minutes}m  "
          f"RVOL {cfg.rvol_threshold:.1%}  4H filter {'on' if cfg.use_h4 else 'off'}\n")
    print(format_blotter(result, None if a.list == 0 else a.list))
    print(format_summary(result))
    if a.out:
        write_trades_csv(result, a.out)
        print(f"Signals written to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
