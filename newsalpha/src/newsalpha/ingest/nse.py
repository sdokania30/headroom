"""NSE corporate announcements feed.

NSE's JSON API refuses requests that do not carry cookies issued by a prior page
load, so the feed primes a session before its first poll and re-primes whenever
the server starts returning 401/403. Same caveats as the BSE feed: undocumented,
unversioned, and not a substitute for a licensed feed if you go to production.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from ..models import Announcement, make_uid
from ..utils import first, parse_dt
from .base import PollingFeed

log = logging.getLogger(__name__)

NSE_HOME = "https://www.nseindia.com"
NSE_PRIME = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
NSE_API = "https://www.nseindia.com/api/corporate-announcements"

NSE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_PRIME,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
}


class NseAnnouncementFeed(PollingFeed):
    name = "nse"
    _primed = False

    async def _prime(self) -> None:
        """Fetch cookies. Cheap, and required before the API will answer."""
        for url in (NSE_HOME, NSE_PRIME):
            try:
                await self._client.get(url, headers=NSE_HEADERS, timeout=self._cfg.http_timeout_s)
            except httpx.HTTPError as exc:  # pragma: no cover - network dependent
                log.debug("nse: priming %s failed: %s", url, exc)
        self._primed = True

    async def fetch(self) -> Sequence[Announcement]:
        if not self._primed:
            await self._prime()

        response = await self._client.get(
            NSE_API,
            params={"index": "equities"},
            headers=NSE_HEADERS,
            timeout=self._cfg.http_timeout_s,
        )
        if response.status_code in (401, 403):
            # Session expired. Re-prime and let the next poll pick it up rather
            # than retrying inline and doubling this poll's latency budget.
            self._primed = False
            log.info("nse: session expired (%s); will re-prime", response.status_code)
            return []
        response.raise_for_status()

        payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("data") or []
        if not isinstance(rows, list):
            log.warning("nse: unexpected payload shape: %s", type(rows).__name__)
            return []
        return [self._normalise(row) for row in rows if isinstance(row, dict)]

    def _normalise(self, row: dict[str, Any]) -> Announcement:
        symbol = first(row, "symbol", "SYMBOL")
        headline = first(row, "desc", "subject", "attchmntText")
        body = first(row, "attchmntText", "smIndustry", "desc")
        return Announcement(
            uid=make_uid("nse", first(row, "seqId", "seq_id", "id"), f"{symbol}{headline}"),
            source="nse",
            symbol=symbol,
            security_id="",  # resolved later from the instrument master
            exchange_segment="NSE_EQ",
            headline=headline,
            body=body,
            category=first(row, "smIndustry", "category", "an_type"),
            # NSE exposes the broadcast time; the company's own submission time is
            # not published here, so exchange_lag_s stays None for NSE-sourced rows.
            filed_at=parse_dt(first(row, "sort_date", "an_dt", "exchdisstime")),
            disseminated_at=parse_dt(first(row, "exchdisstime", "an_dt", "sort_date")),
            attachment_url=first(row, "attchmntFile", "attchmnt"),
            raw=row,
        )
