"""Core domain objects.

Everything that flows through the pipeline is one of these. They are frozen and
slotted: the hot path allocates one per announcement, and immutability means a
stage can never corrupt an object another stage still holds.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    """Wall-clock UTC. Use for anything that gets persisted or compared to feed data."""
    return datetime.now(timezone.utc)


def monotonic_ns() -> int:
    """Monotonic nanoseconds. Use for measuring durations - never for timestamps.

    Wall clock can jump (NTP slew); latency numbers derived from it are lies.
    """
    return time.perf_counter_ns()


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Horizon(str, Enum):
    INTRADAY = "INTRADAY"
    SWING = "SWING"


@dataclass(frozen=True, slots=True)
class Announcement:
    """One corporate filing / exchange announcement, normalised across sources.

    Three distinct timestamps matter and are routinely conflated:

    * ``filed_at``          - when the company submitted it to the exchange.
    * ``disseminated_at``   - when the exchange published it on the feed.
    * ``received_at``       - when *this process* saw it.

    ``disseminated_at - filed_at`` is the exchange's own processing lag.
    ``received_at - disseminated_at`` is your infrastructure lag - the only part
    you can actually optimise. The gap to a later newswire pickup is the
    "latency edge" the strategy is betting on, measured in ``timing.latency``.
    """

    uid: str
    source: str
    symbol: str
    headline: str
    body: str
    category: str = ""
    security_id: str = ""
    exchange_segment: str = "NSE_EQ"
    filed_at: datetime | None = None
    disseminated_at: datetime | None = None
    received_at: datetime = field(default_factory=utcnow)
    t_received_ns: int = field(default_factory=monotonic_ns)
    attachment_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Headline + body, the unit the sentiment engines score."""
        return f"{self.headline}\n\n{self.body}".strip()

    @property
    def exchange_lag_s(self) -> float | None:
        """Seconds the exchange took to publish after the company filed."""
        if self.filed_at is None or self.disseminated_at is None:
            return None
        return (self.disseminated_at - self.filed_at).total_seconds()

    @property
    def ingest_lag_s(self) -> float | None:
        """Seconds between exchange publication and this process seeing it."""
        if self.disseminated_at is None:
            return None
        return (self.received_at - self.disseminated_at).total_seconds()


def make_uid(source: str, native_id: str, headline: str = "") -> str:
    """Stable id for de-duplication.

    Prefers the source's own id. Falls back to a headline hash for feeds that
    recycle or omit ids - which several of them do, unfortunately.
    """
    if native_id:
        return f"{source}:{native_id}"
    digest = hashlib.sha1(headline.encode("utf-8", "ignore")).hexdigest()[:16]
    return f"{source}:h:{digest}"


@dataclass(frozen=True, slots=True)
class Signal:
    """Sentiment engine output for one announcement."""

    uid: str
    symbol: str
    direction: Direction
    confidence: float
    materiality: int
    horizon: Horizon
    rationale: str
    engine: str
    key_numbers: tuple[str, ...] = ()
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def is_actionable(self) -> bool:
        return self.direction is not Direction.NEUTRAL


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """What the strategy wants to do, before the risk engine has had its say."""

    uid: str
    symbol: str
    security_id: str
    exchange_segment: str
    side: Side
    quantity: int
    order_type: str = "MARKET"
    product_type: str = "INTRADAY"
    price: float = 0.0
    trigger_price: float = 0.0
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    tag: str = ""


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str
    quantity: int = 0
    notional: float = 0.0


@dataclass(frozen=True, slots=True)
class OrderAck:
    """Broker response. ``ok=False`` means the order never reached the exchange."""

    ok: bool
    order_id: str
    status: str
    broker: str
    submitted_at: datetime = field(default_factory=utcnow)
    avg_price: float = 0.0
    filled_quantity: int = 0
    error: str = ""


@dataclass(frozen=True, slots=True)
class Trade:
    """A completed round trip, produced by the backtester or the position tracker."""

    uid: str
    symbol: str
    side: Side
    quantity: int
    entry_at: datetime
    entry_price: float
    exit_at: datetime
    exit_price: float
    exit_reason: str
    direction: Direction
    confidence: float
    delay_s: float = 0.0

    @property
    def gross_pnl(self) -> float:
        sign = 1.0 if self.side is Side.BUY else -1.0
        return sign * (self.exit_price - self.entry_price) * self.quantity

    @property
    def return_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        sign = 1.0 if self.side is Side.BUY else -1.0
        return sign * (self.exit_price - self.entry_price) / self.entry_price
