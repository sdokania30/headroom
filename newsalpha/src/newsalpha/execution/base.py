"""Broker contract."""

from __future__ import annotations

import abc

from ..models import OrderAck, OrderIntent


class Broker(abc.ABC):
    name: str = "broker"

    @abc.abstractmethod
    async def place(self, intent: OrderIntent) -> OrderAck:
        """Submit an order. Must never raise - failures come back as ok=False."""
        raise NotImplementedError

    async def cancel(self, order_id: str) -> bool:
        return False

    async def confirm(self, ack: OrderAck, timeout_s: float = 10.0) -> OrderAck:
        """Resolve an accepted order to a terminal state.

        Default is a no-op: brokers that fill inline (the paper broker) have
        nothing to wait for. Brokers that acknowledge asynchronously override it,
        because an ack is not a fill and treating one as the other means the
        position tracker believes in shares that were never bought.
        """
        return ack

    async def aclose(self) -> None:
        return None
