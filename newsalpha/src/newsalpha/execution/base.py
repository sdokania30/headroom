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

    async def aclose(self) -> None:
        return None
