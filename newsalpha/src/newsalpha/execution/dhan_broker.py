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

import asyncio
import logging
import time

import httpx

from ..models import OrderAck, OrderIntent, utcnow
from .base import Broker

log = logging.getLogger(__name__)

# Dhan order lifecycle. Anything not in either set is still in flight.
_FILLED_STATES = frozenset({"TRADED", "FILLED", "COMPLETE"})
_DEAD_STATES = frozenset({"REJECTED", "CANCELLED", "CANCELED", "EXPIRED"})


def _as_int(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


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

    async def confirm(self, ack: OrderAck, timeout_s: float = 10.0) -> OrderAck:
        """Poll until the order reaches a terminal state or the budget runs out.

        A PENDING order that never resolves is the dangerous case: the position
        may or may not exist. Returning ok=False on timeout is deliberate - the
        caller then treats it as un-filled and does not start managing a position
        it might not have, and the order id is preserved in the error so the
        stuck order is traceable in the broker's own book.
        """
        if not ack.ok or not ack.order_id or timeout_s <= 0:
            return ack

        deadline = time.monotonic() + timeout_s
        delay = 0.25
        last_status = ack.status
        while time.monotonic() < deadline:
            try:
                payload = await self.status(ack.order_id)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("dhan: status poll failed for %s: %s", ack.order_id, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)
                continue

            last_status = str(payload.get("orderStatus", last_status)).upper()
            if last_status in _FILLED_STATES:
                traded = _as_int(payload.get("filledQty") or payload.get("filled_qty"))
                price = _as_float(payload.get("averageTradedPrice") or payload.get("price"))
                log.info("dhan: order %s filled %d @ %.2f", ack.order_id, traded, price)
                return OrderAck(
                    ok=True,
                    order_id=ack.order_id,
                    status=last_status,
                    broker=self.name,
                    submitted_at=ack.submitted_at,
                    avg_price=price,
                    filled_quantity=traded,
                )
            if last_status in _DEAD_STATES:
                log.warning("dhan: order %s ended %s", ack.order_id, last_status)
                return OrderAck(
                    ok=False,
                    order_id=ack.order_id,
                    status=last_status,
                    broker=self.name,
                    error=str(payload.get("omsErrorDescription") or last_status),
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)

        log.error(
            "dhan: order %s still %s after %.1fs - treating as unconfirmed",
            ack.order_id,
            last_status,
            timeout_s,
        )
        return OrderAck(
            ok=False,
            order_id=ack.order_id,
            status="UNCONFIRMED",
            broker=self.name,
            error=f"order {ack.order_id} not terminal after {timeout_s:.0f}s (last: {last_status})",
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
