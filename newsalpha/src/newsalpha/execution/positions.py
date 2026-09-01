"""Position manager - the half that closes trades.

Opening a position is the easy half and the one that feels like the strategy.
Closing it is where the money actually is, and a system that only opens positions
is not an incomplete trading system, it is a dangerous one.

This runs as a background task alongside the feed and, on every tick, checks each
open position against four exits, in priority order:

1. **Risk halt** - the kill switch tripped. Flatten everything, immediately. A
   halt that leaves positions open has not halted anything that matters.
2. **Square-off** - the session is inside its closing buffer. An intraday
   position still open at the exchange's own square-off is closed by the broker
   at whatever price exists, which is not a price you chose.
3. **Stop / target** - the levels the trade was sized against.
4. **Time** - the thesis had its chance. A news reaction that has not happened
   within the hold window is not going to.

The one property that matters more than any of the above: **a failed exit is
retried and escalated, never dropped.** A rejected exit order leaves real
exposure, so it retries with backoff and, if it still cannot get out, logs at
ERROR, journals, and trips the risk halt so nothing new is opened while a
position is stuck. Silence here is the worst possible behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import ExecutionConfig, RiskConfig
from ..models import OrderAck, OrderIntent, Side, utcnow
from ..sessions import TradingCalendar
from ..utils import Journal
from .base import Broker
from .risk import RiskEngine

log = logging.getLogger(__name__)


@dataclass
class ManagedPosition:
    uid: str
    symbol: str
    security_id: str
    exchange_segment: str
    side: Side
    quantity: int
    entry_price: float
    entry_at: datetime
    stop_price: float
    target_price: float
    max_hold_minutes: int
    entry_order_id: str = ""
    exit_attempts: int = 0
    closing: bool = False
    last_price: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.side is Side.BUY

    @property
    def exit_side(self) -> Side:
        return Side.SELL if self.is_long else Side.BUY

    def unrealised(self, price: float) -> float:
        sign = 1.0 if self.is_long else -1.0
        return sign * (price - self.entry_price) * self.quantity

    def hit_stop(self, price: float) -> bool:
        return price <= self.stop_price if self.is_long else price >= self.stop_price

    def hit_target(self, price: float) -> bool:
        return price >= self.target_price if self.is_long else price <= self.target_price

    def expired(self, now: datetime) -> bool:
        return now - self.entry_at >= timedelta(minutes=self.max_hold_minutes)


class PositionManager:
    """Watches open positions and closes them. Runs as a background task."""

    def __init__(
        self,
        broker: Broker,
        risk: RiskEngine,
        prices: object,
        exec_cfg: ExecutionConfig,
        risk_cfg: RiskConfig,
        calendar: TradingCalendar | None = None,
        journal: Journal | None = None,
    ) -> None:
        self._broker = broker
        self._risk = risk
        self._prices = prices
        self._exec = exec_cfg
        self._risk_cfg = risk_cfg
        self._calendar = calendar or risk.calendar
        self._journal = journal
        self._positions: dict[str, ManagedPosition] = {}
        self._stopping = asyncio.Event()
        self.closed: list[dict[str, object]] = []

    # -- registration ---------------------------------------------------------

    def register(self, intent: OrderIntent, ack: OrderAck) -> ManagedPosition:
        """Take ownership of a filled entry."""
        entry_price = ack.avg_price or intent.price
        long = intent.side is Side.BUY
        stop_pct = intent.stop_loss_pct or self._risk_cfg.stop_loss_pct
        target_pct = intent.take_profit_pct or self._risk_cfg.take_profit_pct

        position = ManagedPosition(
            uid=intent.uid,
            symbol=intent.symbol.upper(),
            security_id=intent.security_id,
            exchange_segment=intent.exchange_segment,
            side=intent.side,
            quantity=ack.filled_quantity or intent.quantity,
            entry_price=entry_price,
            entry_at=ack.submitted_at or utcnow(),
            stop_price=entry_price * (1 - stop_pct) if long else entry_price * (1 + stop_pct),
            target_price=entry_price * (1 + target_pct) if long else entry_price * (1 - target_pct),
            max_hold_minutes=self._risk_cfg.max_hold_minutes,
            entry_order_id=ack.order_id,
            last_price=entry_price,
        )
        self._positions[position.symbol] = position
        log.info(
            "managing %s %s x%d entry %.2f stop %.2f target %.2f",
            position.side.value,
            position.symbol,
            position.quantity,
            position.entry_price,
            position.stop_price,
            position.target_price,
        )
        return position

    @property
    def open_positions(self) -> dict[str, ManagedPosition]:
        return dict(self._positions)

    # -- main loop ------------------------------------------------------------

    async def run(self) -> None:
        log.info("position manager up (poll %.1fs)", self._exec.position_poll_s)
        try:
            while not self._stopping.is_set():
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - never let the exit loop die
                    log.exception("position manager tick failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._exec.position_poll_s
                    )
        finally:
            log.info("position manager stopped with %d open", len(self._positions))

    def stop(self) -> None:
        self._stopping.set()

    async def flatten_all(self, reason: str) -> None:
        """Close everything now. Used on shutdown and on a risk halt."""
        for position in list(self._positions.values()):
            if position.closing:
                continue
            price = await self._price(position) or position.last_price
            await self._close(position, price, reason)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        if not self._positions:
            return

        halted = self._risk.halted
        square_off = self._calendar.closing_soon(now, self._risk_cfg.square_off_buffer_s)

        for position in list(self._positions.values()):
            if position.closing:
                continue

            price = await self._price(position)
            if price is not None:
                position.last_price = price

            # 1 and 2 do not need a price - they must fire even when the quote
            # feed is down, which is exactly when you most want to be flat.
            if halted:
                await self._close(position, price or position.last_price, "RISK_HALT")
                continue
            if square_off:
                await self._close(position, price or position.last_price, "SQUARE_OFF")
                continue

            if price is None:
                # A position we cannot price is a position we cannot manage. Not
                # fatal on its own - quotes drop out - but it must be visible.
                log.warning(
                    "no price for open position %s; stop/target not evaluated", position.symbol
                )
                if position.expired(now):
                    await self._close(position, position.last_price, "TIME_NO_PRICE")
                continue

            if position.hit_stop(price):
                await self._close(position, price, "STOP")
            elif position.hit_target(price):
                await self._close(position, price, "TARGET")
            elif position.expired(now):
                await self._close(position, price, "TIME")

    # -- internals ------------------------------------------------------------

    async def _price(self, position: ManagedPosition) -> float | None:
        try:
            return await self._prices.ltp(  # type: ignore[attr-defined]
                position.exchange_segment, position.security_id
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("price lookup failed for %s: %s", position.symbol, exc)
            return None

    async def _close(self, position: ManagedPosition, price: float, reason: str) -> None:
        """Send the exit, retrying before giving up - and never giving up quietly.

        The guard below is load-bearing. Two callers can reach a position at once -
        the manager's own tick and a shutdown flatten - and without it both would
        send an exit, turning a flat position into an equal and opposite one. The
        check and the set have no await between them, so under asyncio they are
        atomic and the second caller returns immediately.
        """
        if position.closing:
            log.debug("%s is already being closed; ignoring duplicate %s", position.symbol, reason)
            return
        if self._positions.get(position.symbol) is not position:
            log.debug("%s is no longer managed; ignoring %s", position.symbol, reason)
            return
        position.closing = True
        intent = OrderIntent(
            uid=f"{position.uid}:exit",
            symbol=position.symbol,
            security_id=position.security_id,
            exchange_segment=position.exchange_segment,
            side=position.exit_side,
            quantity=position.quantity,
            order_type=self._exec.order_type,
            product_type=self._exec.product_type,
            price=price,
            tag=f"exit:{reason}",
        )

        for attempt in range(1, self._exec.exit_retry_attempts + 1):
            position.exit_attempts = attempt
            ack = await self._broker.place(intent)
            if ack.ok:
                fill = ack.avg_price or price
                pnl = self._risk.on_close(position.symbol, fill)
                if pnl is None:
                    # The two exposure records have diverged. The exit itself is
                    # done and correct, so book the P&L from this manager's own
                    # entry price rather than losing it - but say so loudly,
                    # because the daily loss limit is computed from this number.
                    pnl = self._risk.book_pnl(position.unrealised(fill))
                    log.error(
                        "%s was not tracked by the risk engine - exposure records "
                        "diverged; booked %.2f from the position manager's entry",
                        position.symbol,
                        pnl,
                    )
                self._positions.pop(position.symbol, None)
                record = {
                    "type": "exit",
                    "at": utcnow(),
                    "uid": position.uid,
                    "symbol": position.symbol,
                    "reason": reason,
                    "side": position.exit_side.value,
                    "quantity": position.quantity,
                    "entry_price": round(position.entry_price, 2),
                    "exit_price": round(fill, 2),
                    "pnl": round(pnl, 2),
                    "attempts": attempt,
                    "order_id": ack.order_id,
                    "held_s": round((utcnow() - position.entry_at).total_seconds(), 1),
                }
                self.closed.append(record)
                if self._journal:
                    self._journal.write(record)
                log.info(
                    "closed %s (%s) @ %.2f pnl %.2f after %d attempt(s)",
                    position.symbol,
                    reason,
                    fill,
                    pnl,
                    attempt,
                )
                return

            log.warning(
                "exit attempt %d/%d for %s failed: %s",
                attempt,
                self._exec.exit_retry_attempts,
                position.symbol,
                ack.error or ack.status,
            )
            if attempt < self._exec.exit_retry_attempts:
                await asyncio.sleep(min(self._exec.exit_retry_backoff_s * attempt, 5.0))

        # Out of attempts with real exposure still on. Halt so nothing new opens,
        # shout, and leave the position registered so the next tick tries again.
        position.closing = False
        self._risk.halt(f"could not exit {position.symbol} after {position.exit_attempts} attempts")
        log.error(
            "STUCK POSITION: %s %s x%d - exit rejected %d times. Manual intervention needed.",
            position.side.value,
            position.symbol,
            position.quantity,
            position.exit_attempts,
        )
        if self._journal:
            self._journal.write(
                {
                    "type": "exit_failed",
                    "at": utcnow(),
                    "uid": position.uid,
                    "symbol": position.symbol,
                    "reason": reason,
                    "attempts": position.exit_attempts,
                    "quantity": position.quantity,
                }
            )
