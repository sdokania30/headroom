"""BSE corporate announcements feed.

This is the primary filings source. BSE publishes announcements through the JSON
API that backs bseindia.com, and - crucially for this strategy - the payload
carries both the company's submission timestamp and the exchange's dissemination
timestamp, which is the raw material for the latency-edge measurement.

The endpoint is undocumented and unversioned. Field names are matched
case-insensitively across several candidates (see ``utils.first``) so a rename
degrades one field rather than breaking ingestion. Respect the site's terms and
keep the poll interval sane; this is not a licensed low-latency feed, and if you
need one, that is a commercial conversation with the exchange.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import httpx

from ..models import Announcement, make_uid, utcnow
from ..utils import first, parse_dt
from .base import PollingFeed

log = logging.getLogger(__name__)

BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

# The API rejects requests without a browser-shaped origin.
BSE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/corporates/ann.html",
    "Origin": "https://www.bseindia.com",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
}


class BseAnnouncementFeed(PollingFeed):
    name = "bse"

    async def fetch(self) -> Sequence[Announcement]:
        now = utcnow()
        window_start = now - timedelta(minutes=self._cfg.lookback_minutes)
        params = {
            "pageno": 1,
            "strCat": -1,
            "subcategory": -1,
            "strPrevDate": window_start.strftime("%Y%m%d"),
            "strToDate": now.strftime("%Y%m%d"),
            "strScrip": "",
            "strSearch": "P",
            "strType": "C",
        }
        response = await self._client.get(
            BSE_API,
            params=params,
            headers=BSE_HEADERS,
            timeout=self._cfg.http_timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("Table") or []
        if not isinstance(rows, list):
            log.warning("bse: unexpected payload shape: %s", type(rows).__name__)
            return []
        return [self._normalise(row) for row in rows if isinstance(row, dict)]

    def _normalise(self, row: dict[str, Any]) -> Announcement:
        headline = first(row, "HEADLINE", "NEWSSUB", "NEWS_SUB")
        body = first(row, "MORE", "NEWSBODY", "NEWSSUB")
        scrip = first(row, "SCRIP_CD", "SCRIPCD")
        attachment = first(row, "ATTACHMENTNAME", "ATTACHMENT")

        return Announcement(
            uid=make_uid("bse", first(row, "NEWSID", "NEWS_ID"), headline),
            source="bse",
            # SLONGNAME is the company name; NSURL is a link to the filing and is
            # emphatically not an identifier. The scrip code is what maps to a
            # tradable instrument, so it is carried in security_id and the
            # instrument master supplies the canonical trading symbol later.
            symbol=first(row, "SLONGNAME", "SC_NAME", default=scrip) or scrip,
            security_id=scrip,
            exchange_segment="BSE_EQ",
            headline=headline,
            body=body,
            category=first(row, "CATEGORYNAME", "CATEGORY", "SUBCATNAME"),
            filed_at=parse_dt(first(row, "News_submission_dt", "NEWS_SUBMISSION_DT", "NEWS_DT")),
            disseminated_at=parse_dt(first(row, "DissemDT", "DISSEM_DT", "NEWS_DT")),
            attachment_url=f"{BSE_ATTACHMENT_BASE}{attachment}" if attachment else "",
            raw=row,
        )


def build_client() -> httpx.AsyncClient:
    """Shared client with keep-alive.

    Connection reuse matters more than it looks: a fresh TLS handshake to the
    exchange costs more than the entire rest of the decision path.
    """
    return httpx.AsyncClient(
        http2=False,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        follow_redirects=True,
    )
