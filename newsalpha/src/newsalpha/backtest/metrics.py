"""Performance statistics for a set of trades.

Deliberately plain: hit rate, expectancy, profit factor, drawdown. There is no
annualised Sharpe here, because a news strategy trades an irregular number of
times a day and annualising a handful of event trades produces a number that
looks authoritative and means nothing.

Read ``expectancy`` and ``profit_factor`` first. A strategy with a 70% hit rate
and negative expectancy is losing money on the 30%, which is the normal way news
strategies fail.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from ..models import Trade


@dataclass(frozen=True, slots=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    hit_rate: float
    gross_pnl: float
    net_pnl: float
    avg_win: float
    avg_loss: float
    expectancy: float
    profit_factor: float
    max_drawdown: float
    best: float
    worst: float
    avg_return_pct: float
    median_hold_minutes: float

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "hit_rate": round(self.hit_rate, 4),
            "gross_pnl": round(self.gross_pnl, 2),
            "net_pnl": round(self.net_pnl, 2),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "expectancy": round(self.expectancy, 2),
            # None, not Infinity: `Infinity` is not valid JSON and breaks any
            # consumer that parses the report (jq included).
            "profit_factor": (
                None if self.profit_factor == float("inf") else round(self.profit_factor, 3)
            ),
            "max_drawdown": round(self.max_drawdown, 2),
            "best": round(self.best, 2),
            "worst": round(self.worst, 2),
            "avg_return_pct": round(self.avg_return_pct * 100, 3),
            "median_hold_minutes": round(self.median_hold_minutes, 1),
        }


EMPTY = Metrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def compute(trades: Sequence[Trade], cost_bps: float = 0.0) -> Metrics:
    """Summarise a trade list. ``cost_bps`` is charged on both legs."""
    if not trades:
        return EMPTY

    cost_rate = cost_bps / 10_000.0
    net_pnls: list[float] = []
    gross_total = 0.0
    for trade in trades:
        gross = trade.gross_pnl
        gross_total += gross
        turnover = (trade.entry_price + trade.exit_price) * trade.quantity
        net_pnls.append(gross - turnover * cost_rate)

    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in net_pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    holds = [
        (t.exit_at - t.entry_at).total_seconds() / 60.0 for t in trades if t.exit_at > t.entry_at
    ]

    return Metrics(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        hit_rate=len(wins) / len(trades),
        gross_pnl=gross_total,
        net_pnl=sum(net_pnls),
        avg_win=statistics.fmean(wins) if wins else 0.0,
        avg_loss=statistics.fmean(losses) if losses else 0.0,
        expectancy=statistics.fmean(net_pnls),
        # A strategy with no losing trades in the sample is not infinitely good,
        # it is under-sampled. Reporting inf makes that obvious rather than
        # hiding it behind a large finite number.
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        max_drawdown=max_dd,
        best=max(net_pnls),
        worst=min(net_pnls),
        avg_return_pct=statistics.fmean([t.return_pct for t in trades]),
        median_hold_minutes=statistics.median(holds) if holds else 0.0,
    )


def by_group(trades: Sequence[Trade], key: str, cost_bps: float = 0.0) -> dict[str, Metrics]:
    """Break metrics out by an attribute - ``direction``, ``symbol``, ``exit_reason``.

    The direction split is the one to look at first: a strategy that only works
    long is usually reading market drift, not filings.
    """
    buckets: dict[str, list[Trade]] = {}
    for trade in trades:
        value = getattr(trade, key)
        label = value.value if hasattr(value, "value") else str(value)
        buckets.setdefault(label, []).append(trade)
    return {k: compute(v, cost_bps) for k, v in sorted(buckets.items())}
