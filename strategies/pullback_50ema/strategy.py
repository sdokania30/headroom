"""Deterministic implementation of the 4H 50-EMA pullback strategy.

Three independent gates, evaluated in order:

  1. ``scan_4h``              -- structural setup on the 4H chart (trend, pullback,
                                50-EMA invalidation, breakout trigger level).
  2. ``evaluate_rvol_gate``   -- intraday execution filter on the breakout day.
  3. ``find_chandelier_exit`` -- position exit on the intraday chart.

Each gate is pure and side-effect free, so they can be unit tested, backtested or
wired into a live feed independently. Stdlib only -- no pandas, no numpy.

See README.md for the rule-by-rule mapping and the list of source ambiguities
that had to be resolved with an explicit assumption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Optional, Sequence

# Minimum price increment used for the "1 pip beyond the wick" offset.
TICK_FX_5DP = 0.0001  # 1 pip on a 4-decimal FX pair (EURUSD, GBPUSD, ...)
TICK_FX_JPY = 0.01    # 1 pip on a JPY cross
TICK_NSE_EQUITY = 0.05  # 1 tick on most NSE cash-segment scrips


class Direction(Enum):
    LONG = 1
    SHORT = -1

    @property
    def sign(self) -> int:
        return self.value


class SetupStatus(Enum):
    ARMED = "armed"                          # trigger level live, waiting for breakout
    TRIGGERED = "triggered"                  # breakout level traded through
    CANCELLED_EMA_SLICE = "cancelled_ema_slice"  # pullback body closed through the 50 EMA
    CANCELLED_TREND_FLIP = "cancelled_trend_flip"  # price closed to the wrong side of the EMA
    EXPIRED_PULLBACK_TOO_LONG = "expired_pullback_too_long"
    EXPIRED_NO_BREAKOUT = "expired_no_breakout"


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    def is_counter_trend(self, direction: Direction) -> bool:
        """A red candle inside an uptrend, or a green candle inside a downtrend."""
        if direction is Direction.LONG:
            return self.close < self.open
        return self.close > self.open


@dataclass(frozen=True)
class StrategyConfig:
    # -- Stage 1: trend and initial separation -------------------------------
    ema_period: int = 50
    atr_period: int = 14
    trend_confirm_bars: int = 3
    min_separation_atr: float = 1.0
    # -- Stage 2: pullback ---------------------------------------------------
    min_pullback_bars: int = 2
    max_pullback_bars: int = 6
    require_ema_touch: bool = False
    ema_zone_atr: float = 0.5
    # -- Stage 3: entry trigger ---------------------------------------------
    entry_anchor: str = "first"   # "first" = first counter-trend bar; "extreme" = whole sequence
    trigger_valid_bars: int = 3
    tick_size: float = TICK_FX_5DP

    def __post_init__(self) -> None:
        if self.entry_anchor not in ("first", "extreme"):
            raise ValueError("entry_anchor must be 'first' or 'extreme'")
        if self.min_pullback_bars < 1:
            raise ValueError("min_pullback_bars must be >= 1")
        if self.max_pullback_bars < self.min_pullback_bars:
            raise ValueError("max_pullback_bars must be >= min_pullback_bars")


@dataclass
class Setup:
    direction: Direction
    pullback_start_index: int
    pullback_bar_indices: list[int] = field(default_factory=list)
    status: SetupStatus = SetupStatus.ARMED
    anchor_index: Optional[int] = None
    anchor_price: Optional[float] = None
    entry_price: Optional[float] = None
    protective_stop: Optional[float] = None
    ema_at_pullback_start: Optional[float] = None
    armed_at_index: Optional[int] = None
    resolved_at_index: Optional[int] = None
    resolved_at_ts: Optional[datetime] = None
    touched_ema_zone: bool = False

    @property
    def pullback_length(self) -> int:
        return len(self.pullback_bar_indices)

    @property
    def is_actionable(self) -> bool:
        return self.status in (SetupStatus.ARMED, SetupStatus.TRIGGERED)


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def ema(values: Sequence[float], period: int) -> list[Optional[float]]:
    """EMA seeded with the SMA of the first ``period`` values (TradingView default)."""
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def true_range(candles: Sequence[Candle]) -> list[float]:
    tr: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            tr.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return tr


def atr(candles: Sequence[Candle], period: int) -> list[Optional[float]]:
    """Wilder-smoothed ATR."""
    tr = true_range(candles)
    out: list[Optional[float]] = [None] * len(candles)
    if len(candles) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(candles)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# Stage 1-3: the 4H structural scan
# ---------------------------------------------------------------------------

def scan_4h(candles: Sequence[Candle], config: Optional[StrategyConfig] = None) -> list[Setup]:
    """Walk 4H candles once and return every pullback setup, resolved or live.

    A setup is opened when a confirmed, separated trend prints its first
    counter-trend candle, and is resolved by exactly one of: a breakout through
    the trigger level, a body-close through the 50 EMA, a trend flip, an
    over-long pullback, or trigger expiry.
    """
    cfg = config or StrategyConfig()
    closes = [c.close for c in candles]
    ema_vals = ema(closes, cfg.ema_period)
    atr_vals = atr(candles, cfg.atr_period)

    setups: list[Setup] = []
    active: Optional[Setup] = None
    run_side = 0          # +1 = closing above the EMA, -1 = below
    run_len = 0
    run_max_sep = 0.0     # peak |close - ema| / ATR reached during the current run
    bars_since_pullback_end = 0

    def resolve(setup: Setup, status: SetupStatus, index: int) -> None:
        setup.status = status
        setup.resolved_at_index = index
        setup.resolved_at_ts = candles[index].ts

    for i, c in enumerate(candles):
        e = ema_vals[i]
        a = atr_vals[i]
        if e is None or a is None or a <= 0:
            continue

        side = 1 if c.close > e else (-1 if c.close < e else 0)

        # -- trend run bookkeeping ------------------------------------------
        flipped = False
        if side != 0 and side == run_side:
            run_len += 1
        else:
            flipped = (
                active is not None and side != 0 and side != active.direction.sign
            )
            run_side = side
            run_len = 1 if side != 0 else 0
            run_max_sep = 0.0
        if side != 0:
            run_max_sep = max(run_max_sep, abs(c.close - e) / a)

        # -- (1) a resting stop order fills intrabar, before this bar closes --
        if active is not None and active.entry_price is not None:
            hit = (
                c.high >= active.entry_price
                if active.direction is Direction.LONG
                else c.low <= active.entry_price
            )
            if hit:
                resolve(active, SetupStatus.TRIGGERED, i)
                setups.append(active)
                active = None
                continue

        # -- (2) price closed on the wrong side of the 50 EMA ----------------
        if flipped and active is not None:
            status = (
                SetupStatus.CANCELLED_EMA_SLICE
                if c.is_counter_trend(active.direction)
                and _slices_ema(c, e, active.direction)
                else SetupStatus.CANCELLED_TREND_FLIP
            )
            resolve(active, status, i)
            setups.append(active)
            active = None
            continue

        # -- (3) classify this bar against the active / candidate pullback ---
        if active is not None:
            direction = active.direction
            if c.is_counter_trend(direction):
                if _slices_ema(c, e, direction):
                    resolve(active, SetupStatus.CANCELLED_EMA_SLICE, i)
                    setups.append(active)
                    active = None
                    continue
                active.pullback_bar_indices.append(i)
                bars_since_pullback_end = 0
                if _touches_ema_zone(c, e, a, direction, cfg):
                    active.touched_ema_zone = True
                if active.pullback_length > cfg.max_pullback_bars:
                    resolve(active, SetupStatus.EXPIRED_PULLBACK_TOO_LONG, i)
                    setups.append(active)
                    active = None
                    continue
                _arm(active, candles, cfg)
            else:
                # Trend-direction (or doji) bar: the pullback sequence is over.
                if _slices_ema(c, e, direction):
                    resolve(active, SetupStatus.CANCELLED_EMA_SLICE, i)
                    setups.append(active)
                    active = None
                    continue
                if active.entry_price is None:
                    # Fewer than min_pullback_bars counter-trend candles -- discard.
                    active = None
                else:
                    bars_since_pullback_end += 1
                    if bars_since_pullback_end > cfg.trigger_valid_bars:
                        resolve(active, SetupStatus.EXPIRED_NO_BREAKOUT, i)
                        setups.append(active)
                        active = None
            if active is not None:
                continue

        # -- (4) open a new pullback candidate -------------------------------
        trend_confirmed = (
            side != 0
            and run_len >= cfg.trend_confirm_bars
            and run_max_sep >= cfg.min_separation_atr
        )
        if not trend_confirmed:
            continue
        direction = Direction.LONG if side == 1 else Direction.SHORT
        if not c.is_counter_trend(direction) or _slices_ema(c, e, direction):
            continue
        active = Setup(
            direction=direction,
            pullback_start_index=i,
            pullback_bar_indices=[i],
            ema_at_pullback_start=e,
            touched_ema_zone=_touches_ema_zone(c, e, a, direction, cfg),
        )
        bars_since_pullback_end = 0
        _arm(active, candles, cfg)

    if active is not None:
        setups.append(active)  # still live at the right edge of the data
    return setups


def _slices_ema(c: Candle, ema_value: float, direction: Direction) -> bool:
    """True when the candle body sits entirely on the far side of the 50 EMA.

    Wicks may pierce the EMA -- that is the bounce the strategy wants. A body
    that closes completely through it is the "cancelled" case from the video.
    """
    if direction is Direction.LONG:
        return c.body_high < ema_value
    return c.body_low > ema_value


def _touches_ema_zone(
    c: Candle, ema_value: float, atr_value: float, direction: Direction, cfg: StrategyConfig
) -> bool:
    zone = cfg.ema_zone_atr * atr_value
    if direction is Direction.LONG:
        return c.low <= ema_value + zone
    return c.high >= ema_value - zone


def _arm(setup: Setup, candles: Sequence[Candle], cfg: StrategyConfig) -> None:
    """Publish the trigger level once the pullback has enough counter-trend bars."""
    if setup.pullback_length < cfg.min_pullback_bars:
        return
    if cfg.require_ema_touch and not setup.touched_ema_zone:
        return

    bars = [(i, candles[i]) for i in setup.pullback_bar_indices]
    if setup.direction is Direction.LONG:
        anchor_index, anchor = (
            bars[0] if cfg.entry_anchor == "first" else max(bars, key=lambda b: b[1].high)
        )
        setup.anchor_price = anchor.high
        setup.entry_price = anchor.high + cfg.tick_size
        setup.protective_stop = min(c.low for _, c in bars) - cfg.tick_size
    else:
        anchor_index, anchor = (
            bars[0] if cfg.entry_anchor == "first" else min(bars, key=lambda b: b[1].low)
        )
        setup.anchor_price = anchor.low
        setup.entry_price = anchor.low - cfg.tick_size
        setup.protective_stop = max(c.high for _, c in bars) + cfg.tick_size
    setup.anchor_index = anchor_index
    if setup.armed_at_index is None:
        setup.armed_at_index = setup.pullback_bar_indices[-1]


# ---------------------------------------------------------------------------
# Stage 4: intraday RVOL execution gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RvolGate:
    passed: bool
    cumulative_volume: float
    average_daily_volume: float
    ratio: float                       # cumulative_volume / average_daily_volume
    threshold: float
    window_minutes: int
    breach_ts: Optional[datetime]      # first bar close at which the threshold was met
    bars_used: int

    def __str__(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"RVOL {verdict}: {self.ratio:.2%} of ADV in the first "
            f"{self.window_minutes}m (threshold {self.threshold:.2%})"
        )


def average_daily_volume(daily_candles: Sequence[Candle], lookback: int = 20) -> float:
    """Mean volume of the last ``lookback`` completed daily bars."""
    if not daily_candles:
        raise ValueError("daily_candles is empty")
    window = list(daily_candles)[-lookback:]
    return sum(c.volume for c in window) / len(window)


def evaluate_rvol_gate(
    intraday_candles: Iterable[Candle],
    session_open: datetime,
    adv: float,
    threshold: float = 0.09,
    window_minutes: int = 10,
) -> RvolGate:
    """Cumulative volume in the first N minutes as a share of average daily volume.

    ``threshold`` is a fraction of ADV (0.09 == 9%), matching "Daily Cumulative
    RVOL must reach >= 9% within the first 10 minutes of market open". Bars are
    assigned to the window by their timestamp, which is treated as the bar's
    OPEN time; a bar opening inside the window counts in full.
    """
    if adv <= 0:
        raise ValueError("adv must be > 0")
    window_end = session_open + timedelta(minutes=window_minutes)
    cumulative = 0.0
    breach_ts: Optional[datetime] = None
    bars_used = 0
    for c in intraday_candles:
        if c.ts < session_open or c.ts >= window_end:
            continue
        cumulative += c.volume
        bars_used += 1
        if breach_ts is None and cumulative / adv >= threshold:
            breach_ts = c.ts
    return RvolGate(
        passed=breach_ts is not None,
        cumulative_volume=cumulative,
        average_daily_volume=adv,
        ratio=cumulative / adv,
        threshold=threshold,
        window_minutes=window_minutes,
        breach_ts=breach_ts,
        bars_used=bars_used,
    )


# ---------------------------------------------------------------------------
# Stage 5: Chandelier Exit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExitSignal:
    index: int
    ts: datetime
    close: float
    stop: float


def chandelier_stops(
    candles: Sequence[Candle],
    direction: Direction,
    period: int = 22,
    multiplier: float = 3.0,
    ratchet: bool = True,
) -> list[Optional[float]]:
    """Chandelier Exit line.

    LONG : highest high over ``period`` bars - multiplier * ATR(period)
    SHORT: lowest low over ``period`` bars  + multiplier * ATR(period)

    With ``ratchet`` the line only ever moves in the trade's favour while price
    stays on the right side of it (the TradingView behaviour).
    """
    atr_vals = atr(candles, period)
    out: list[Optional[float]] = [None] * len(candles)
    prev: Optional[float] = None
    for i in range(len(candles)):
        a = atr_vals[i]
        if a is None or i + 1 < period:
            continue
        window = candles[i - period + 1 : i + 1]
        if direction is Direction.LONG:
            raw = max(c.high for c in window) - multiplier * a
            if ratchet and prev is not None and candles[i - 1].close > prev:
                raw = max(raw, prev)
        else:
            raw = min(c.low for c in window) + multiplier * a
            if ratchet and prev is not None and candles[i - 1].close < prev:
                raw = min(raw, prev)
        out[i] = prev = raw
    return out


def find_chandelier_exit(
    candles: Sequence[Candle],
    direction: Direction,
    entry_index: int = 0,
    period: int = 22,
    multiplier: float = 3.0,
    ratchet: bool = True,
) -> Optional[ExitSignal]:
    """First bar at or after ``entry_index`` that CLOSES through the Chandelier line."""
    stops = chandelier_stops(candles, direction, period, multiplier, ratchet)
    for i in range(max(entry_index, 0), len(candles)):
        stop = stops[i]
        if stop is None:
            continue
        c = candles[i]
        breached = c.close < stop if direction is Direction.LONG else c.close > stop
        if breached:
            return ExitSignal(index=i, ts=c.ts, close=c.close, stop=stop)
    return None


# ---------------------------------------------------------------------------
# End-to-end helper
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TradePlan:
    setup: Setup
    rvol: RvolGate
    exit_signal: Optional[ExitSignal]

    @property
    def executable(self) -> bool:
        return self.setup.status is SetupStatus.TRIGGERED and self.rvol.passed


def build_trade_plan(
    h4_candles: Sequence[Candle],
    intraday_candles: Sequence[Candle],
    session_open: datetime,
    adv: float,
    config: Optional[StrategyConfig] = None,
    rvol_threshold: float = 0.09,
    rvol_window_minutes: int = 10,
    chandelier_period: int = 22,
    chandelier_multiplier: float = 3.0,
) -> Optional[TradePlan]:
    """Run all three gates for a single breakout day. Returns ``None`` if no
    4H setup is actionable in the supplied window."""
    setups = [s for s in scan_4h(h4_candles, config) if s.is_actionable]
    if not setups:
        return None
    setup = setups[-1]
    gate = evaluate_rvol_gate(
        intraday_candles, session_open, adv, rvol_threshold, rvol_window_minutes
    )
    exit_signal = None
    if setup.status is SetupStatus.TRIGGERED and gate.passed and gate.breach_ts is not None:
        entry_index = next(
            (i for i, c in enumerate(intraday_candles) if c.ts >= gate.breach_ts), 0
        )
        exit_signal = find_chandelier_exit(
            intraday_candles,
            setup.direction,
            entry_index,
            chandelier_period,
            chandelier_multiplier,
        )
    return TradePlan(setup=setup, rvol=gate, exit_signal=exit_signal)
