"""Replay feed - the backtest's data source.

Reads a JSONL file of captured announcements and yields them as if they were
arriving live. Because it satisfies the same :class:`AnnouncementFeed` contract,
the research path and the live path run identical code from ingest onward. Any
divergence between backtest and production is then a data problem, not a
"the backtest used a different code path" problem.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

from ..models import Announcement, make_uid
from ..utils import first, parse_dt, read_jsonl
from .base import AnnouncementFeed


def announcement_from_record(row: dict[str, Any]) -> Announcement:
    """Rehydrate an Announcement from a journal or capture record."""
    headline = first(row, "headline", "HEADLINE", "desc")
    source = first(row, "source", default="replay")
    return Announcement(
        uid=first(row, "uid") or make_uid(source, first(row, "id"), headline),
        source=source,
        symbol=first(row, "symbol", "SYMBOL", "scrip"),
        security_id=first(row, "security_id", "securityId", "SCRIP_CD"),
        exchange_segment=first(row, "exchange_segment", default="NSE_EQ"),
        headline=headline,
        body=first(row, "body", "MORE", "attchmntText"),
        category=first(row, "category", "CATEGORYNAME"),
        filed_at=parse_dt(first(row, "filed_at", "News_submission_dt", "sort_date")),
        disseminated_at=parse_dt(first(row, "disseminated_at", "DissemDT", "exchdisstime")),
        attachment_url=first(row, "attachment_url"),
        raw=row,
    )


def load_announcements(path: str | Path) -> list[Announcement]:
    """Load and sort by filing time. Sorting matters - capture order is not
    chronological when several pollers wrote to the same journal."""
    items = [announcement_from_record(r) for r in read_jsonl(path)]
    return sorted(items, key=lambda a: a.filed_at or a.received_at)


class ReplayFeed(AnnouncementFeed):
    name = "replay"

    def __init__(self, path: str | Path) -> None:
        self._items = load_announcements(path)

    def __iter__(self) -> Iterator[Announcement]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    async def stream(self) -> AsyncIterator[Announcement]:
        for item in self._items:
            yield item
