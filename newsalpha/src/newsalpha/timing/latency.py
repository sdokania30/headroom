"""Event timing engine.

This module exists to answer one question honestly: **is there actually a latency
edge, and how big is it?**

Three intervals get measured, and they are routinely confused with each other:

``exchange_lag``   filed_at -> disseminated_at. The exchange's own processing
                   time. Not yours to optimise, but it bounds everything.
``ingest_lag``     disseminated_at -> received_at. Your infrastructure. This is
                   the part that repays engineering effort.
``decision_lag``   received_at -> order sent. Prescreen + LLM + risk. Measured
                   per stage by ``clock.Stopwatch``.
``press_edge``     disseminated_at -> the same story appearing on a newswire.
                   This is the edge being traded. If it is near zero for the
                   filings you care about, the strategy has no premise and the
                   right move is to find that out here rather than in the P&L.

The press-release correlation is deliberately conservative: it requires the same
symbol, a later timestamp, and strong headline overlap. A false match inflates
the apparent edge, which is the one error that would make you trade more.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..clock import LatencyTracker, Stopwatch
from ..models import Announcement, Signal, utcnow
from ..utils import Journal

log = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]+")
# Words that appear in most filing headlines and carry no matching signal.
_STOPWORDS = frozenset(
    """
    the a an of and or for to in on at by with from is are be as its it this that
    ltd limited company intimation disclosure regulation regulations sebi under
    announcement announcements pursuant listing obligations requirements
    """.split()
)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2)


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of significant words. 0.0-1.0."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass(frozen=True, slots=True)
class TimingRecord:
    uid: str
    symbol: str
    headline: str
    filed_at: datetime | None
    disseminated_at: datetime | None
    received_at: datetime
    exchange_lag_s: float | None
    ingest_lag_s: float | None
    stages_ms: dict[str, float]
    direction: str = ""
    press_seen_at: datetime | None = None
    press_edge_s: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "uid": self.uid,
            "symbol": self.symbol,
            "headline": self.headline[:200],
            "filed_at": self.filed_at,
            "disseminated_at": self.disseminated_at,
            "received_at": self.received_at,
            "exchange_lag_s": self.exchange_lag_s,
            "ingest_lag_s": self.ingest_lag_s,
            "stages_ms": self.stages_ms,
            "direction": self.direction,
            "press_seen_at": self.press_seen_at,
            "press_edge_s": self.press_edge_s,
        }


@dataclass
class EventTimingEngine:
    """Records timing for every announcement and correlates later press pickups."""

    journal: Journal | None = None
    correlation_window: timedelta = timedelta(minutes=90)
    similarity_threshold: float = 0.45
    tracker: LatencyTracker = field(default_factory=LatencyTracker)

    # Recent filings kept for press correlation. Bounded: only the last couple of
    # hours can plausibly match, so this never needs to be large.
    _recent: deque[TimingRecord] = field(default_factory=lambda: deque(maxlen=2000))
    _edges: list[float] = field(default_factory=list)

    def observe(
        self,
        announcement: Announcement,
        watch: Stopwatch,
        signal: Signal | None = None,
    ) -> TimingRecord:
        stages = self.tracker.record(watch)

        if announcement.exchange_lag_s is not None:
            self.tracker.record_value("exchange_lag_ms", announcement.exchange_lag_s * 1000)
        if announcement.ingest_lag_s is not None:
            self.tracker.record_value("ingest_lag_ms", announcement.ingest_lag_s * 1000)

        record = TimingRecord(
            uid=announcement.uid,
            symbol=announcement.symbol,
            headline=announcement.headline,
            filed_at=announcement.filed_at,
            disseminated_at=announcement.disseminated_at,
            received_at=announcement.received_at,
            exchange_lag_s=announcement.exchange_lag_s,
            ingest_lag_s=announcement.ingest_lag_s,
            stages_ms=stages,
            direction=signal.direction.value if signal else "",
        )
        self._recent.append(record)
        if self.journal:
            self.journal.write({"type": "timing", **record.as_dict()})
        return record

    def register_press(
        self, symbol: str, headline: str, seen_at: datetime | None = None
    ) -> TimingRecord | None:
        """Report a newswire/press pickup and match it to an earlier filing.

        Returns the matched record updated with the measured edge, or None when
        nothing matches - which is the common and correct outcome for stories
        that never had a filing behind them.
        """
        seen_at = seen_at or utcnow()
        symbol_key = symbol.upper()

        best: TimingRecord | None = None
        best_score = self.similarity_threshold
        for record in reversed(self._recent):
            reference = record.disseminated_at or record.received_at
            if reference > seen_at or seen_at - reference > self.correlation_window:
                continue
            if record.symbol.upper() != symbol_key:
                continue
            score = similarity(record.headline, headline)
            if score > best_score:
                best, best_score = record, score

        if best is None:
            return None

        reference = best.disseminated_at or best.received_at
        edge_s = (seen_at - reference).total_seconds()
        self._edges.append(edge_s)

        matched = TimingRecord(
            uid=best.uid,
            symbol=best.symbol,
            headline=best.headline,
            filed_at=best.filed_at,
            disseminated_at=best.disseminated_at,
            received_at=best.received_at,
            exchange_lag_s=best.exchange_lag_s,
            ingest_lag_s=best.ingest_lag_s,
            stages_ms=best.stages_ms,
            direction=best.direction,
            press_seen_at=seen_at,
            press_edge_s=edge_s,
        )
        if self.journal:
            self.journal.write(
                {"type": "press_edge", "similarity": round(best_score, 3), **matched.as_dict()}
            )
        log.info(
            "press edge: %s +%.1fs (similarity %.2f) - %s",
            matched.symbol,
            edge_s,
            best_score,
            matched.headline[:80],
        )
        return matched

    def report(self) -> dict[str, object]:
        """Everything measured so far, for the `latency-report` command."""
        edges = sorted(self._edges)
        edge_stats: dict[str, float] = {}
        if edges:
            edge_stats = {
                "n": float(len(edges)),
                "p50_s": round(edges[len(edges) // 2], 2),
                "p90_s": round(edges[min(len(edges) - 1, int(len(edges) * 0.9))], 2),
                "min_s": round(edges[0], 2),
                "max_s": round(edges[-1], 2),
                "mean_s": round(sum(edges) / len(edges), 2),
            }
        return {
            "observed": len(self._recent),
            "stage_latency_ms": self.tracker.percentiles(),
            "press_edge": edge_stats,
        }
