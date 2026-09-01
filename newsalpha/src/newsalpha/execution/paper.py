"""Paper broker.

Fills immediately at the touch price plus modelled slippage. That is optimistic
in exactly the way that matters for this strategy: in the seconds after a material
filing the book is thin and moving, and real slippage will exceed the constant
modelled here. Treat paper P&L as a ceiling, not an estimate - and set
``slippage_bps`` from your own measured fills as soon as you have any.
"""

from __future__ import annotations

import itertools
import logging

from ..models import OrderAck, OrderIntent, Side
from .base import Broker

log = logging.getLogger(__name__)


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, slippage_bps: float = 8.0) -> None:
        self._slippage = slippage_bps / 10_000.0
        self._ids = itertools.count(1)
        self.orders: list[OrderAck] = []

    async def place(self, intent: OrderIntent) -> OrderAck:
        if intent.price <= 0:
            return OrderAck(
                ok=False,
                order_id="",
                status="REJECTED",
                broker=self.name,
                error="paper broker needs a reference price",
            )

        # Slippage always works against you, whichever way you are going.
        direction = 1.0 if intent.side is Side.BUY else -1.0
        fill_price = intent.price * (1.0 + direction * self._slippage)

        ack = OrderAck(
            ok=True,
            order_id=f"PAPER-{next(self._ids):06d}",
            status="FILLED",
            broker=self.name,
            avg_price=round(fill_price, 2),
            filled_quantity=intent.quantity,
        )
        self.orders.append(ack)
        log.info(
            "PAPER %s %s x%d @ %.2f (ref %.2f)",
            intent.side.value,
            intent.symbol,
            intent.quantity,
            ack.avg_price,
            intent.price,
        )
        return ack
