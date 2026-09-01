"""Backtesting framework.

Two things this is built to avoid, because they are how event-driven backtests
usually lie:

**Look-ahead.** Entry is the first bar at or *after* the filing timestamp plus an
assumed execution delay. Never the bar containing it, never its open. Exits scan
bars strictly after the entry bar, so the entry candle's own high and low - which
may well have printed before the filing - can never trigger a stop or a target.

**Free latency.** The whole thesis is that being early pays. So the delay is not
a fixed assumption; :meth:`Backtester.sweep` re-runs the same signals across a
range of delays and reports P&L as a function of it. If the curve is flat, the
edge is not latency and the engineering effort belongs elsewhere. If it falls off
a cliff between 1s and 30s, you have found the actual shape of the opportunity
and can size the infrastructure spend against it.

One limit to be clear about: **the sweep can only resolve delays that cross a bar
boundary.** On minute bars, 1s / 5s / 30s all land in the same candle and return
identical numbers - which looks like "latency doesn't matter" but is really "this
data cannot see it". Sub-minute edges need tick or sub-minute bars. If your sweep
comes back flat across the sub-minute range, check the bar resolution before you
conclude anything about the strategy.

Scoring runs once and is cached to disk, so a sweep across five delays costs one
pass of LLM calls rather than five.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from ..config import BacktestConfig, RiskConfig, SentimentConfig
from ..models import Announcement, Direction, Horizon, Side, Signal, Trade
from ..sentiment.base import SentimentEngine
from .data import Bar, BarStore
from .metrics import Metrics, by_group, compute

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    trades: list[Trade]
    metrics: Metrics
    by_direction: dict[str, Metrics]
    by_exit_reason: dict[str, Metrics]
    scored: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    sweep: dict[float, Metrics] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "headline": self.metrics.as_dict(),
            "by_direction": {k: v.as_dict() for k, v in self.by_direction.items()},
            "by_exit_reason": {k: v.as_dict() for k, v in self.by_exit_reason.items()},
            "signals_scored": self.scored,
            "skipped": self.skipped,
            "delay_sweep": {
                f"{delay:g}s": {
                    "trades": m.trades,
                    "net_pnl": round(m.net_pnl, 2),
                    "hit_rate": round(m.hit_rate, 4),
                    "expectancy": round(m.expectancy, 2),
                }
                for delay, m in sorted(self.sweep.items())
            },
        }


class SignalCache:
    """Disk-backed signal cache keyed by announcement uid.

    Scoring is the expensive part of a backtest and the only non-deterministic
    one. Caching it means a re-run with different risk parameters is instant and,
    more importantly, compares like with like - you are changing one variable.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._items: dict[str, Signal] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    self._items[row["uid"]] = _signal_from_dict(row)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
            log.info("loaded %d cached signals from %s", len(self._items), self.path)

    def get(self, uid: str) -> Signal | None:
        return self._items.get(uid)

    def put(self, signal: Signal) -> None:
        self._items[signal.uid] = signal
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_signal_to_dict(signal)) + "\n")


class Backtester:
    def __init__(
        self,
        cfg: BacktestConfig,
        risk_cfg: RiskConfig,
        sentiment_cfg: SentimentConfig,
        engine: SentimentEngine | None,
        bars: BarStore,
        cache: SignalCache | None = None,
    ) -> None:
        self._cfg = cfg
        self._risk = risk_cfg
        self._sent = sentiment_cfg
        self._engine = engine
        self._bars = bars
        self._cache = cache or SignalCache(cfg.cache_path)

    async def score_all(self, announcements: Sequence[Announcement]) -> dict[str, Signal]:
        """Score every announcement once, honouring the cache."""
        signals: dict[str, Signal] = {}
        pending: list[Announcement] = []
        for ann in announcements:
            cached = self._cache.get(ann.uid)
            if cached is not None:
                signals[ann.uid] = cached
            else:
                pending.append(ann)

        if pending and self._engine is None:
            raise RuntimeError(
                f"{len(pending)} announcements are not in the signal cache and no "
                "sentiment engine is available (set ANTHROPIC_API_KEY, or use "
                "sentiment.engine=rules)"
            )
        if not pending:
            log.info("all %d announcements served from cache", len(signals))
            return signals

        log.info("scoring %d announcements (%d cached)", len(pending), len(signals))
        semaphore = asyncio.Semaphore(self._sent.max_concurrency)

        engine = self._engine
        assert engine is not None  # guarded above

        async def one(ann: Announcement) -> None:
            async with semaphore:
                signal = await engine.score(ann)
            if signal is not None:
                signals[ann.uid] = signal
                self._cache.put(signal)

        await asyncio.gather(*(one(a) for a in pending))
        return signals

    def simulate(
        self,
        announcements: Sequence[Announcement],
        signals: dict[str, Signal],
        delay_s: float,
    ) -> tuple[list[Trade], dict[str, int]]:
        """Run the strategy at one assumed execution delay."""
        trades: list[Trade] = []
        skipped: dict[str, int] = {}

        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        for ann in announcements:
            signal = signals.get(ann.uid)
            if signal is None:
                skip("no signal")
                continue
            if not signal.is_actionable:
                skip("neutral")
                continue
            if signal.confidence < self._sent.min_confidence:
                skip("low confidence")
                continue
            if signal.materiality < self._sent.min_materiality:
                skip("low materiality")
                continue

            reference = ann.filed_at or ann.disseminated_at or ann.received_at
            symbol = ann.symbol.upper()
            if not self._bars.has(symbol):
                skip("no bar data")
                continue

            entry_bar = self._bars.bar_at_or_after(symbol, reference + timedelta(seconds=delay_s))
            if entry_bar is None:
                skip("no bar after filing")
                continue

            trade = self._walk(ann, signal, entry_bar, delay_s)
            if trade is None:
                skip("no exit data")
                continue
            trades.append(trade)

        return trades, skipped

    def _walk(
        self, ann: Announcement, signal: Signal, entry_bar: Bar, delay_s: float
    ) -> Trade | None:
        """Hold from ``entry_bar`` until a stop, a target, or the time limit."""
        symbol = ann.symbol.upper()
        side = Side.BUY if signal.direction is Direction.BULLISH else Side.SELL
        # The open of the first bar at-or-after our arrival is the earliest price
        # we could plausibly have transacted at.
        entry_price = entry_bar.open
        if entry_price <= 0:
            return None

        hold = self._cfg.hold_minutes
        if signal.horizon is Horizon.SWING:
            # A swing call held for an intraday window is a different strategy.
            # Doubling the window is a blunt approximation; if swing signals are a
            # meaningful share of your flow, model them separately.
            hold *= 2

        long = side is Side.BUY
        stop = (
            entry_price * (1 - self._risk.stop_loss_pct)
            if long
            else entry_price * (1 + self._risk.stop_loss_pct)
        )
        target = (
            entry_price * (1 + self._risk.take_profit_pct)
            if long
            else entry_price * (1 - self._risk.take_profit_pct)
        )

        # Strictly after the entry bar - see the module docstring on look-ahead.
        window = self._bars.window(
            symbol,
            entry_bar.ts + timedelta(seconds=1),
            entry_bar.ts + timedelta(minutes=hold),
        )
        if not window:
            return None

        quantity = max(1, int(self._risk.max_notional_per_trade // entry_price))

        for bar in window:
            hit_stop = bar.low <= stop if long else bar.high >= stop
            hit_target = bar.high >= target if long else bar.low <= target
            # Both inside one bar: assume the stop filled first. Minute bars do
            # not say which came first, and the pessimistic reading is the only
            # one that will not flatter the strategy.
            if hit_stop:
                return _trade(
                    ann, signal, side, quantity, entry_bar, entry_price, bar, stop, "STOP", delay_s
                )
            if hit_target:
                return _trade(
                    ann,
                    signal,
                    side,
                    quantity,
                    entry_bar,
                    entry_price,
                    bar,
                    target,
                    "TARGET",
                    delay_s,
                )

        last = window[-1]
        return _trade(
            ann, signal, side, quantity, entry_bar, entry_price, last, last.close, "TIME", delay_s
        )

    async def run(
        self, announcements: Sequence[Announcement], sweep: bool = True
    ) -> BacktestResult:
        signals = await self.score_all(announcements)
        trades, skipped = self.simulate(announcements, signals, self._cfg.execution_delay_s)

        result = BacktestResult(
            trades=trades,
            metrics=compute(trades, self._cfg.cost_bps),
            by_direction=by_group(trades, "direction", self._cfg.cost_bps),
            by_exit_reason=by_group(trades, "exit_reason", self._cfg.cost_bps),
            scored=len(signals),
            skipped=skipped,
        )

        if sweep:
            for delay in self._cfg.delay_sweep_s:
                swept, _ = self.simulate(announcements, signals, delay)
                result.sweep[delay] = compute(swept, self._cfg.cost_bps)

        return result


def _trade(
    ann: Announcement,
    signal: Signal,
    side: Side,
    quantity: int,
    entry_bar: Bar,
    entry_price: float,
    exit_bar: Bar,
    exit_price: float,
    reason: str,
    delay_s: float,
) -> Trade:
    return Trade(
        uid=ann.uid,
        symbol=ann.symbol.upper(),
        side=side,
        quantity=quantity,
        entry_at=entry_bar.ts,
        entry_price=entry_price,
        exit_at=exit_bar.ts,
        exit_price=exit_price,
        exit_reason=reason,
        direction=signal.direction,
        confidence=signal.confidence,
        delay_s=delay_s,
    )


def _signal_to_dict(signal: Signal) -> dict[str, object]:
    return {
        "uid": signal.uid,
        "symbol": signal.symbol,
        "direction": signal.direction.value,
        "confidence": signal.confidence,
        "materiality": signal.materiality,
        "horizon": signal.horizon.value,
        "rationale": signal.rationale,
        "engine": signal.engine,
        "key_numbers": list(signal.key_numbers),
        "latency_ms": signal.latency_ms,
        "input_tokens": signal.input_tokens,
        "output_tokens": signal.output_tokens,
    }


def _signal_from_dict(row: dict[str, object]) -> Signal:
    return Signal(
        uid=str(row["uid"]),
        symbol=str(row.get("symbol", "")),
        direction=Direction(str(row.get("direction", "NEUTRAL"))),
        confidence=float(row.get("confidence", 0.0)),  # type: ignore[arg-type]
        materiality=int(row.get("materiality", 0)),  # type: ignore[arg-type]
        horizon=Horizon(str(row.get("horizon", "INTRADAY"))),
        rationale=str(row.get("rationale", "")),
        engine=str(row.get("engine", "cache")),
        key_numbers=tuple(str(n) for n in (row.get("key_numbers") or [])),  # type: ignore[union-attr]
        latency_ms=float(row.get("latency_ms", 0.0)),  # type: ignore[arg-type]
    )
