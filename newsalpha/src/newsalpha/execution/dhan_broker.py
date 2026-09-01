"""DhanHQ order placement (v2).

Endpoint: ``POST {base}/v2/orders``, authenticated with the ``access-token``
header. Verify the field names against the current DhanHQ docs before your first
live session - this is a broker API and it does change.

Two deliberate choices:

* Errors are returned, never raised. An exception escaping into the hot path
  would take down the pipeline over one bad order.
* ``correlationId`` carries the announcement uid, so a fill in Dhan's own order
  book can be traced back to the filing that caused it without joining on time.
"""

from __future__ import annotations

import logging

import httpx

from ..models import OrderAck, OrderIntent, utcnow
from .base import Broker

log = logging.getLogger(__name__)


class DhanBroker(Broker):
    name = "dhan"

    def __init__(
        self,
        client: httpx.AsyncClient,
        client_id: str,
        access_token: str,
        base_url: str = "https://api.dhan.co",
        timeout_s: float = 3.0,
        armed: bool = False,
    ) -> None:
        if not client_id or not access_token:
            raise ValueError("DhanBroker requires DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN")
        self._client = client
        self._client_id = client_id
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        # Second switch. execution.broker="dhan" alone does not place real orders;
        # execution.live_trading_armed must also be true. One flag is too easy to
        # leave set in a config you copied from somewhere.
        self._armed = armed
        self._headers = {
            "access-token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def place(self, intent: OrderIntent) -> OrderAck:
        if not self._armed:
            log.warning(
                "dhan: DISARMED - would have sent %s %s x%d. "
                "Set execution.live_trading_armed=true to send real orders.",
                intent.side.value,
                intent.symbol,
                intent.quantity,
            )
            return OrderAck(
                ok=False, order_id="", status="DISARMED", broker=self.name, error="not armed"
            )

        body = {
            "dhanClientId": self._client_id,
            "correlationId": intent.uid[:25],
            "transactionType": intent.side.value,
            "exchangeSegment": intent.exchange_segment,
            "productType": intent.product_type,
            "orderType": intent.order_type,
            "validity": "DAY",
            "securityId": intent.security_id,
            "quantity": intent.quantity,
            "disclosedQuantity": 0,
            "price": round(intent.price, 2) if intent.order_type == "LIMIT" else 0,
            "triggerPrice": round(intent.trigger_price, 2) if intent.trigger_price else 0,
            "afterMarketOrder": False,
        }

        try:
            response = await self._client.post(
                f"{self._base}/v2/orders",
                json=body,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            log.error("dhan: order transport failure for %s: %s", intent.symbol, exc)
            return OrderAck(ok=False, order_id="", status="ERROR", broker=self.name, error=str(exc))

        if response.status_code >= 400:
            detail = response.text[:300]
            log.error("dhan: order rejected (%s): %s", response.status_code, detail)
            return OrderAck(
                ok=False,
                order_id="",
                status="REJECTED",
                broker=self.name,
                error=f"{response.status_code}: {detail}",
            )

        payload = response.json()
        order_id = str(payload.get("orderId", ""))
        status = str(payload.get("orderStatus", "UNKNOWN"))
        log.info(
            "dhan: %s %s x%d -> order %s (%s)",
            intent.side.value,
            intent.symbol,
            intent.quantity,
            order_id,
            status,
        )
        # PENDING is the normal response; the fill arrives asynchronously. Poll
        # `status` or subscribe to the order-update socket for the actual fill.
        return OrderAck(
            ok=bool(order_id),
            order_id=order_id,
            status=status,
            broker=self.name,
            submitted_at=utcnow(),
        )

    async def status(self, order_id: str) -> dict[str, object]:
        response = await self._client.get(
            f"{self._base}/v2/orders/{order_id}", headers=self._headers, timeout=self._timeout
        )
        response.raise_for_status()
        payload = response.json()
        return payload[0] if isinstance(payload, list) and payload else payload

    async def cancel(self, order_id: str) -> bool:
        try:
            response = await self._client.delete(
                f"{self._base}/v2/orders/{order_id}",
                headers=self._headers,
                timeout=self._timeout,
            )
            return response.status_code < 400
        except httpx.HTTPError as exc:
            log.error("dhan: cancel failed for %s: %s", order_id, exc)
            return False
