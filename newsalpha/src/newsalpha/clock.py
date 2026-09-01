"""Stage-by-stage latency instrumentation.

The whole strategy is a bet on latency, so latency is measured as a first-class
output rather than inferred from logs. One ``Stopwatch`` follows one announcement
through the pipeline and records a monotonic mark at each stage boundary.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .models import monotonic_ns

# Canonical stage names. Ordered, so a report can print them meaningfully.
STAGES = (
    "received",
    "prescreened",
    "scored",
    "risk_checked",
    "order_sent",
    "order_acked",
)


@dataclass
class Stopwatch:
    """Records monotonic marks for one announcement's trip through the pipeline."""

    uid: str
    marks: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if "received" not in self.marks:
            self.marks["received"] = monotonic_ns()

    def mark(self, stage: str) -> None:
        self.marks[stage] = monotonic_ns()

    def since(self, stage: str = "received") -> float:
        """Milliseconds elapsed since ``stage``."""
        start = self.marks.get(stage)
        if start is None:
            return 0.0
        return (monotonic_ns() - start) / 1e6

    def span_ms(self, start: str, end: str) -> float | None:
        a, b = self.marks.get(start), self.marks.get(end)
        if a is None or b is None:
            return None
        return (b - a) / 1e6

    def breakdown(self) -> dict[str, float]:
        """Per-stage durations in ms, in pipeline order.

        Only consecutive marks that both exist are reported, so a run that
        short-circuited at the prescreen stage yields a partial breakdown rather
        than zeros that would pollute the percentiles.
        """
        present = [s for s in STAGES if s in self.marks]
        out: dict[str, float] = {}
        # strict=False is correct: the second list is one shorter by construction.
        for start, end in zip(present, present[1:], strict=False):
            span = self.span_ms(start, end)
            if span is not None:
                out[f"{start}->{end}"] = round(span, 3)
        total = self.span_ms(present[0], present[-1]) if len(present) > 1 else None
        if total is not None:
            out["total"] = round(total, 3)
        return out


class LatencyTracker:
    """Rolling percentiles per stage transition.

    Bounded deques - this runs for a whole session and must not grow without
    limit. 4096 samples is plenty for a stable p99 on a feed that produces a few
    hundred actionable filings a day.
    """

    def __init__(self, window: int = 4096) -> None:
        self._window = window
        self._samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))

    def record(self, watch: Stopwatch) -> dict[str, float]:
        breakdown = watch.breakdown()
        for key, value in breakdown.items():
            self._samples[key].append(value)
        return breakdown

    def record_value(self, key: str, value_ms: float) -> None:
        self._samples[key].append(value_ms)

    def percentiles(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for key, values in self._samples.items():
            if not values:
                continue
            ordered = sorted(values)
            out[key] = {
                "n": float(len(ordered)),
                "p50": round(_quantile(ordered, 0.50), 3),
                "p95": round(_quantile(ordered, 0.95), 3),
                "p99": round(_quantile(ordered, 0.99), 3),
                "max": round(ordered[-1], 3),
                "mean": round(statistics.fmean(ordered), 3),
            }
        return out


def _quantile(ordered: list[float], q: float) -> float:
    """Nearest-rank quantile on a pre-sorted list."""
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]
