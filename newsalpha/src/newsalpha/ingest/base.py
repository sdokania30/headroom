"""Feed abstractions.

A feed is an async iterator of :class:`Announcement`. Live sources poll or hold a
socket; the backtest source replays a file. The pipeline cannot tell them apart,
which is what makes the backtest worth anything.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Sequence

import httpx

from ..config import FeedConfig
from ..models import Announcement
from .dedupe import SeenSet

log = logging.getLogger(__name__)


class AnnouncementFeed(abc.ABC):
    name: str = "feed"

    @abc.abstractmethod
    def stream(self) -> AsyncIterator[Announcement]:
        """Yield announcements as they arrive. Must not raise on transient errors."""
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


class PollingFeed(AnnouncementFeed):
    """Base for HTTP feeds that must be polled.

    Subclasses implement :meth:`fetch`. This class owns the loop, the error
    backoff, de-duplication and the symbol/category filters, so every source
    behaves identically when the network misbehaves.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        cfg: FeedConfig,
        seen: SeenSet | None = None,
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._seen = seen or SeenSet()
        self._symbols = {s.upper() for s in cfg.symbols}
        self._categories = {c.lower() for c in cfg.categories}

    @abc.abstractmethod
    async def fetch(self) -> Sequence[Announcement]:
        """One poll. Returns whatever the source currently offers, unfiltered."""
        raise NotImplementedError

    async def stream(self) -> AsyncIterator[Announcement]:
        backoff = 0.0
        while True:
            try:
                batch = await self.fetch()
                backoff = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a feed must never kill the pipeline
                backoff = min(
                    self._cfg.error_backoff_max_s,
                    max(self._cfg.poll_interval_s, backoff * 2 or 1.0),
                )
                log.warning("%s: poll failed (%s); backing off %.1fs", self.name, exc, backoff)
                await asyncio.sleep(backoff)
                continue

            for ann in batch:
                if self._accept(ann):
                    yield ann

            # Jitter so several feeds don't synchronise into a thundering herd
            # against the same origin every interval.
            await asyncio.sleep(self._cfg.poll_interval_s * random.uniform(0.85, 1.15))

    def _accept(self, ann: Announcement) -> bool:
        if self._symbols and ann.symbol.upper() not in self._symbols:
            return False
        if self._categories and ann.category.lower() not in self._categories:
            return False
        return self._seen.add_if_new(ann.uid)


async def merge(
    feeds: Sequence[AnnouncementFeed], maxsize: int = 1000
) -> AsyncIterator[Announcement]:
    """Fan several feeds into one ordered-by-arrival stream.

    A bounded queue gives back-pressure: if scoring falls behind, the producers
    block rather than growing an unbounded backlog of announcements that are
    already too stale to trade by the time they are read.
    """
    queue: asyncio.Queue[Announcement] = asyncio.Queue(maxsize=maxsize)

    async def pump(feed: AnnouncementFeed) -> None:
        try:
            async for ann in feed.stream():
                await queue.put(ann)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("%s: stream died", feed.name)

    tasks = [asyncio.create_task(pump(f), name=f"feed:{f.name}") for f in feeds]
    try:
        while True:
            yield await queue.get()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
