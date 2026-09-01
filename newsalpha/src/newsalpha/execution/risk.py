"""Risk engine.

Every order passes through here, in paper mode and live mode alike. That is the
point: a paper run only tells you something if it exercises the same gates the
live run will.

The gates run cheapest-and-most-fatal first, so a halted account costs one
boolean rather than a sizing calculation. Each returns a specific reason string -
"rejected" with no reason is useless at 09:31 when you are trying to work out why
nothing traded.

What this does NOT do: decide direction. It takes a signal as given and answers
only "may we, and how much".
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import RiskConfig
from ..models import Direction, RiskDecision, Side, Signal, utcnow
from ..sessions import TradingCalendar

log = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    side: Side
    quantity: int
    entry_price: float
    opened_at: datetime

    @property
    def notional(self) -> float:
        return self.quantity * self.entry_price


class RiskEngine:
    """Stateful account-level guardrails."""

    def __init__(
        self,
        cfg: RiskConfig,
        min_confidence: float = 0.0,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self._cfg = cfg
        self._min_confidence = min_confidence
        self.calendar = calendar or TradingCalendar.from_config(
            cfg.session_start, cfg.session_end, cfg.holidays
        )
        self.positions: dict[str, Position] = {}
        # Capacity reserved by approved-but-not-yet-filled orders. Announcements
        # are handled concurrently, so without this every check below is a
        # time-of-check/time-of-use race: a burst of filings all read "no
        # position yet" before any of them fills, and the caps do nothing at
        # precisely the moment they are needed.
        self._pending: dict[str, float] = {}
        self.realised_pnl: float = 0.0
        self.consecutive_rejects: int = 0
        self.halted: bool = False
        self.halt_reason: str = ""
        self._order_times: deque[datetime] = deque(maxlen=cfg.max_orders_per_minute * 4)
        self._day: str = utcnow().date().isoformat()

    # -- main gate ------------------------------------------------------------

    def evaluate(
        self, signal: Signal, price: float | None, now: datetime | None = None
    ) -> RiskDecision:
        now = now or utcnow()
        self._roll_day(now)

        if self.halted:
            return RiskDecision(False, f"halted: {self.halt_reason}")

        if not signal.is_actionable:
            return RiskDecision(False, "neutral signal")

        if signal.confidence < self._min_confidence:
            return RiskDecision(
                False, f"confidence {signal.confidence:.2f} < {self._min_confidence:.2f}"
            )

        if not self.calendar.is_open(now):
            return RiskDecision(
                False, f"outside session {self._cfg.session_start}-{self._cfg.session_end} IST"
            )

        # Refuse entries inside the square-off buffer. The position manager is
        # about to start closing things; opening into that is self-defeating.
        if self.calendar.closing_soon(now, self._cfg.square_off_buffer_s):
            return RiskDecision(False, "inside square-off buffer")

        symbol = signal.symbol.upper()
        if symbol in {s.upper() for s in self._cfg.denylist}:
            return RiskDecision(False, f"{symbol} is denylisted")

        # No price means no sizing. Refusing is strictly better than guessing.
        if price is None or price <= 0:
            return RiskDecision(False, "no live price available")
        if not (self._cfg.min_price <= price <= self._cfg.max_price):
            return RiskDecision(False, f"price {price:.2f} outside tradable band")

        if self.realised_pnl <= -self._cfg.daily_loss_limit:
            self.halt(f"daily loss limit hit ({self.realised_pnl:.0f})")
            return RiskDecision(False, self.halt_reason)

        if self._orders_last_minute(now) >= self._cfg.max_orders_per_minute:
            return RiskDecision(False, "order rate limit")

        if symbol in self.positions:
            # One position per name. Averaging into a news trade turns a bounded
            # loss into an unbounded one, which is exactly the wrong shape here.
            return RiskDecision(False, f"already holding {symbol}")

        if symbol in self._pending:
            return RiskDecision(False, f"order already in flight for {symbol}")

        if len(self.positions) + len(self._pending) >= self._cfg.max_open_positions:
            return RiskDecision(False, f"max open positions ({self._cfg.max_open_positions})")

        quantity, notional, reason = self._size(signal, price)
        if quantity <= 0:
            return RiskDecision(False, reason)

        # Reserve here, with no await between the checks above and this line, so
        # a concurrent caller cannot slip through the same gates.
        self._pending[symbol] = notional
        return RiskDecision(True, "approved", quantity=quantity, notional=notional)

    # -- sizing ---------------------------------------------------------------

    def _size(self, signal: Signal, price: float) -> tuple[int, float, str]:
        """Risk-parity sizing: the stop distance, not the price, sets the size.

        Scaled by confidence so a 0.95 call gets a full clip and a marginal one
        does not. Then clamped by the per-trade cap and by whatever gross
        headroom is left, in that order.
        """
        span = max(1e-9, 1.0 - self._min_confidence)
        scale = 0.5 + 0.5 * min(1.0, max(0.0, (signal.confidence - self._min_confidence) / span))

        risk_amount = self._cfg.equity * self._cfg.risk_per_trade_pct * scale
        notional = risk_amount / self._cfg.stop_loss_pct
        notional = min(notional, self._cfg.max_notional_per_trade)

        headroom = self._cfg.max_gross_notional - self.gross_notional
        if headroom <= 0:
            return 0, 0.0, "gross notional limit reached"
        notional = min(notional, headroom)

        quantity = int(notional // price)
        if quantity <= 0:
            return 0, 0.0, f"sized to zero shares at {price:.2f}"
        return quantity, quantity * price, "ok"

    # -- state ----------------------------------------------------------------

    @property
    def gross_notional(self) -> float:
        """Filled exposure plus capacity reserved by in-flight orders."""
        return sum(p.notional for p in self.positions.values()) + sum(self._pending.values())

    def on_order_sent(self, now: datetime | None = None) -> None:
        self._order_times.append(now or utcnow())

    def on_fill(
        self, signal: Signal, quantity: int, price: float, now: datetime | None = None
    ) -> None:
        """Convert a reservation into a real position."""
        self._pending.pop(signal.symbol.upper(), None)
        side = Side.BUY if signal.direction is Direction.BULLISH else Side.SELL
        self.positions[signal.symbol.upper()] = Position(
            symbol=signal.symbol.upper(),
            side=side,
            quantity=quantity,
            entry_price=price,
            opened_at=now or utcnow(),
        )
        self.consecutive_rejects = 0

    def on_close(self, symbol: str, exit_price: float) -> float | None:
        """Close a position and book the P&L.

        Returns None - not 0.0 - when the symbol was not tracked here. That
        distinction matters: a zero would silently fold a bookkeeping divergence
        into the day's P&L, and the daily loss limit is computed from that number.
        The caller is expected to treat None as the alarm it is.
        """
        position = self.positions.pop(symbol.upper(), None)
        if position is None:
            return None
        sign = 1.0 if position.side is Side.BUY else -1.0
        return self.book_pnl(sign * (exit_price - position.entry_price) * position.quantity)

    def book_pnl(self, pnl: float) -> float:
        """Add to the day's realised P&L, tripping the kill switch if breached."""
        self.realised_pnl += pnl
        if self.realised_pnl <= -self._cfg.daily_loss_limit:
            self.halt(f"daily loss limit hit ({self.realised_pnl:.0f})")
        return pnl

    def release(self, symbol: str) -> None:
        """Give back capacity reserved by an order that never became a position.

        Every path out of an approved decision must call this or on_fill, or the
        reservation leaks and the account slowly stops being able to trade.
        """
        self._pending.pop(symbol.upper(), None)

    def on_reject(self, reason: str) -> None:
        """A broker rejection. Enough in a row means something is systemically
        wrong - bad token, wrong segment, stale instrument master - and firing
        more orders into it will not help."""
        self.consecutive_rejects += 1
        if self.consecutive_rejects >= self._cfg.max_consecutive_rejects:
            self.halt(f"{self.consecutive_rejects} consecutive broker rejects ({reason})")

    def halt(self, reason: str) -> None:
        if not self.halted:
            log.error("RISK HALT: %s", reason)
        self.halted = True
        self.halt_reason = reason

    def resume(self) -> None:
        """Manual only. Nothing in this system un-halts itself."""
        self.halted = False
        self.halt_reason = ""
        self.consecutive_rejects = 0

    def _orders_last_minute(self, now: datetime) -> int:
        cutoff = now - timedelta(minutes=1)
        return sum(1 for t in self._order_times if t > cutoff)

    def _roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if today != self._day:
            log.info("new session %s: resetting daily P&L and halt state", today)
            self._day = today
            self.realised_pnl = 0.0
            self.consecutive_rejects = 0
            self._pending.clear()
            self.halted = False
            self.halt_reason = ""

    def snapshot(self) -> dict[str, object]:
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "open_positions": len(self.positions),
            "pending_orders": len(self._pending),
            "gross_notional": round(self.gross_notional, 2),
            "realised_pnl": round(self.realised_pnl, 2),
            "consecutive_rejects": self.consecutive_rejects,
        }
