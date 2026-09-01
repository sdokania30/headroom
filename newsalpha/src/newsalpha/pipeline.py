"""The live pipeline.

    feeds -> dedupe -> prescreen -> LLM -> risk -> broker
                          |                 |
                          +----- timing ----+----> journal

Announcements are handled concurrently, bounded by a semaphore. That matters:
filings cluster - a results day dumps thirty in a second - and a serial pipeline
would put the thirtieth filing several seconds behind the first, which is the
entire edge, spent on queueing.

The feed reader itself never awaits an LLM call. It hands work to a task and goes
straight back to reading, so a slow scoring call delays one trade rather than
every trade behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

import httpx

from .clock import Stopwatch
from .config import Settings
from .execution.positions import PositionManager
from .ingest import (
    AnnouncementFeed,
    BseAnnouncementFeed,
    DhanAnnouncementFeed,
    DhanMarketData,
    NseAnnouncementFeed,
    SeenSet,
    merge,
)
from .ingest.instruments import InstrumentMaster
from .models import Announcement, utcnow
from .sentiment import build_engine
from .sentiment.base import SentimentEngine
from .timing import EventTimingEngine
from .utils import Journal

log = logging.getLogger(__name__)


class PriceSource(Protocol):
    async def ltp(self, segment: str, security_id: str) -> float | None: ...


class NullPriceSource:
    """Used when no market-data credentials are configured.

    Returns None, which the risk engine treats as "cannot size" and rejects. That
    is intentional - a pipeline that cannot price is allowed to score and journal,
    but it is not allowed to guess its way into a position.
    """

    async def ltp(self, segment: str, security_id: str) -> float | None:
        return None


class TradingPipeline:
    def __init__(
        self,
        settings: Settings,
        feeds: list[AnnouncementFeed],
        engine: SentimentEngine,
        router: object | None,
        prices: PriceSource,
        timing: EventTimingEngine,
        journal: Journal,
        positions: PositionManager | None = None,
        instruments: InstrumentMaster | None = None,
    ) -> None:
        self._cfg = settings
        self._feeds = feeds
        self._engine = engine
        self._router = router
        self._prices = prices
        self._timing = timing
        self._journal = journal
        self._positions = positions
        self._instruments = instruments
        self._sem = asyncio.Semaphore(settings.sentiment.max_concurrency)
        self._inflight: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()
        self.processed = 0

    async def run(self) -> None:
        log.info(
            "pipeline up: %d feed(s), engine=%s, broker=%s",
            len(self._feeds),
            self._cfg.sentiment.engine,
            self._cfg.execution.broker,
        )
        # The exit loop runs alongside the feed, not after it. If it only ran
        # between announcements, a quiet feed would mean unmanaged stops.
        manager_task: asyncio.Task[None] | None = None
        if self._positions is not None:
            manager_task = asyncio.create_task(self._positions.run(), name="positions")

        stream = merge(self._feeds)
        try:
            async for announcement in stream:
                if self._stopping.is_set():
                    break
                task = asyncio.create_task(self._handle(announcement))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
        except asyncio.CancelledError:
            pass
        finally:
            await self._drain(manager_task)

    def stop(self) -> None:
        self._stopping.set()

    async def _drain(self, manager_task: asyncio.Task[None] | None = None) -> None:
        """Let in-flight work finish before tearing down.

        Order matters here. In-flight announcements finish first - an order that
        has been sent but not journalled is the worst possible moment to exit.
        Only then are positions flattened, so a position opened microseconds
        before shutdown is still managed rather than orphaned.
        """
        if self._inflight:
            log.info("draining %d in-flight announcement(s)", len(self._inflight))
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

        if self._positions is not None:
            open_now = len(self._positions.open_positions)
            if open_now and self._cfg.execution.flatten_on_shutdown:
                log.warning("flattening %d open position(s) before shutdown", open_now)
                with contextlib.suppress(Exception):
                    await self._positions.flatten_all("SHUTDOWN")
            elif open_now:
                log.error(
                    "shutting down with %d position(s) still open and "
                    "flatten_on_shutdown disabled - nothing is watching their stops",
                    open_now,
                )
            self._positions.stop()
            if manager_task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(manager_task, timeout=10.0)

        for feed in self._feeds:
            with contextlib.suppress(Exception):
                await feed.aclose()
        with contextlib.suppress(Exception):
            await self._engine.aclose()

    async def _handle(self, announcement: Announcement) -> None:
        watch = Stopwatch(announcement.uid)
        self.processed += 1
        try:
            self._journal.write(
                {
                    "type": "announcement",
                    "at": utcnow(),
                    "uid": announcement.uid,
                    "source": announcement.source,
                    "symbol": announcement.symbol,
                    "headline": announcement.headline[:300],
                    "category": announcement.category,
                    "filed_at": announcement.filed_at,
                    "disseminated_at": announcement.disseminated_at,
                    "ingest_lag_s": announcement.ingest_lag_s,
                }
            )
            watch.mark("prescreened")

            async with self._sem:
                signal = await self._engine.score(announcement)
            watch.mark("scored")

            if signal is None:
                self._timing.observe(announcement, watch)
                return

            log.info(
                "%s %s conf=%.2f mat=%d (%.0fms) - %s",
                announcement.symbol,
                signal.direction.value,
                signal.confidence,
                signal.materiality,
                signal.latency_ms,
                signal.rationale[:90],
            )

            if signal.materiality < self._cfg.sentiment.min_materiality:
                self._timing.observe(announcement, watch, signal)
                return

            if self._router is None:
                self._timing.observe(announcement, watch, signal)
                return

            price = await self._prices.ltp(announcement.exchange_segment, announcement.security_id)
            watch.mark("risk_checked")

            await self._router.handle(  # type: ignore[attr-defined]
                signal, price, announcement.security_id, announcement.exchange_segment
            )
            watch.mark("order_sent")
            self._timing.observe(announcement, watch, signal)

        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad announcement must not stop the pipeline
            log.exception("failed handling %s", announcement.uid)


def build_feeds(
    settings: Settings, client: httpx.AsyncClient, seen: SeenSet | None = None
) -> list[AnnouncementFeed]:
    """Construct the enabled feeds. A shared SeenSet de-duplicates *across*
    sources, so a filing carried by both BSE and NSE is traded once."""
    seen = seen or SeenSet()
    feeds: list[AnnouncementFeed] = []
    if settings.feeds.bse:
        feeds.append(BseAnnouncementFeed(client, settings.feeds, seen))
    if settings.feeds.nse:
        feeds.append(NseAnnouncementFeed(client, settings.feeds, seen))
    if settings.feeds.dhan:
        feeds.append(
            DhanAnnouncementFeed(
                client,
                settings.feeds,
                access_token=settings.dhan_access_token,
                client_id=settings.dhan_client_id,
                seen=seen,
            )
        )
    if not feeds:
        raise RuntimeError("no feeds enabled - set feeds.bse or feeds.nse in the config")
    return feeds


def build_prices(settings: Settings, client: httpx.AsyncClient) -> PriceSource:
    if settings.dhan_access_token and settings.dhan_client_id:
        return DhanMarketData(
            client,
            client_id=settings.dhan_client_id,
            access_token=settings.dhan_access_token,
            base_url=settings.execution.dhan_base_url,
            timeout_s=settings.execution.http_timeout_s,
        )
    log.warning(
        "no Dhan credentials: running without live prices. Signals will be scored "
        "and journalled, but every order will be rejected for lack of a price."
    )
    return NullPriceSource()


def build_engine_for(settings: Settings) -> SentimentEngine:
    return build_engine(settings.sentiment, api_key=settings.anthropic_api_key)


async def build_instruments(
    settings: Settings, client: httpx.AsyncClient
) -> InstrumentMaster | None:
    """Load the scrip master. Returns None if it could not be indexed.

    A None here is survivable for BSE-sourced filings, which carry a usable scrip
    code already, but every NSE-sourced signal will be unroutable - so it warns
    rather than failing quietly.
    """
    master = InstrumentMaster(cache_path=settings.execution.instruments_path)
    count = await master.load(client)
    if not count:
        log.warning(
            "instrument master empty: NSE symbols cannot be resolved to securityIds, "
            "so NSE-sourced signals will not be routed"
        )
        return None
    log.info("instrument master ready (%d instruments)", count)
    return master
