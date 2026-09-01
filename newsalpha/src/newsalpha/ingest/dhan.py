"""DhanHQ data access.

Two separate things live here, and it is worth being precise about which is which:

* :class:`DhanMarketData` - live quotes. Used to price an order and to size it.
  This is well-documented broker functionality and is the reliable half.
* :class:`DhanAnnouncementFeed` - a filings feed *if your Dhan plan exposes one*.
  DhanHQ's published v2 API covers orders, portfolio and market data; it does not
  document a corporate-announcements endpoint. The path is therefore configurable
  (``feeds.dhan_ann_path``) and the feed is disabled by default. The exchange
  feeds in ``bse.py`` / ``nse.py`` are the source of record for filings.

API surface used here (DhanHQ v2): base ``https://api.dhan.co``, auth via the
``access-token`` header. Verify request/response shapes against the current docs
before going live - broker APIs change without much ceremony.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from ..config import FeedConfig
from ..models import Announcement, make_uid
from ..utils import first, parse_dt
from .base import PollingFeed
from .dedupe import SeenSet

log = logging.getLogger(__name__)

DHAN_BASE = "https://api.dhan.co"


def dhan_headers(access_token: str) -> dict[str, str]:
    return {
        "access-token": access_token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


class DhanMarketData:
    """Last-traded prices, used for sizing and for paper fills."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        client_id: str,
        access_token: str,
        base_url: str = DHAN_BASE,
        timeout_s: float = 3.0,
    ) -> None:
        self._client = client
        self._client_id = client_id
        self._headers = dhan_headers(access_token)
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    async def ltp(self, segment: str, security_id: str) -> float | None:
        """Last traded price for one instrument, or None if unavailable.

        Returning None rather than a stale or zero price is deliberate: the risk
        engine refuses to size a position without a live price, and a silently
        wrong price is far worse than a skipped trade.
        """
        payload = {segment: [int(security_id)]} if security_id.isdigit() else {segment: []}
        try:
            response = await self._client.post(
                f"{self._base}/v2/marketfeed/ltp",
                json=payload,
                headers={**self._headers, "client-id": self._client_id},
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json().get("data", {})
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("dhan: ltp lookup failed for %s/%s: %s", segment, security_id, exc)
            return None

        bucket = data.get(segment) or {}
        quote = bucket.get(str(security_id)) or {}
        price = quote.get("last_price") or quote.get("ltp")
        try:
            value = float(price)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None


class DhanAnnouncementFeed(PollingFeed):
    """Optional broker-sourced filings feed. Disabled unless configured."""

    name = "dhan"

    def __init__(
        self,
        client: httpx.AsyncClient,
        cfg: FeedConfig,
        access_token: str,
        client_id: str,
        base_url: str = DHAN_BASE,
        seen: SeenSet | None = None,
    ) -> None:
        super().__init__(client, cfg, seen)
        self._headers = {**dhan_headers(access_token), "client-id": client_id}
        self._base = base_url.rstrip("/")

    async def fetch(self) -> Sequence[Announcement]:
        response = await self._client.get(
            f"{self._base}{self._cfg.dhan_ann_path}",
            headers=self._headers,
            timeout=self._cfg.http_timeout_s,
        )
        if response.status_code == 404:
            log.error(
                "dhan: %s returned 404 - this plan does not expose an announcements "
                "endpoint. Disable feeds.dhan and rely on the exchange feeds.",
                self._cfg.dhan_ann_path,
            )
            return []
        response.raise_for_status()
        payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("data") or []
        return [self._normalise(r) for r in rows if isinstance(r, dict)]

    def _normalise(self, row: dict[str, Any]) -> Announcement:
        headline = first(row, "headline", "subject", "title")
        return Announcement(
            uid=make_uid("dhan", first(row, "id", "announcementId"), headline),
            source="dhan",
            symbol=first(row, "symbol", "tradingSymbol"),
            security_id=first(row, "securityId", "security_id"),
            exchange_segment=first(row, "exchangeSegment", default="NSE_EQ"),
            headline=headline,
            body=first(row, "body", "description", "text"),
            category=first(row, "category", "type"),
            filed_at=parse_dt(first(row, "filedAt", "submittedAt", "timestamp")),
            disseminated_at=parse_dt(first(row, "publishedAt", "timestamp")),
            raw=row,
        )
