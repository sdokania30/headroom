"""Command line entry point.

    newsalpha scan            score filings, place nothing  (needs only an API key)
    newsalpha capture         record filings to JSONL for later backtesting
    newsalpha paper           full pipeline against the paper broker
    newsalpha live            full pipeline against DhanHQ  (two switches required)
    newsalpha backtest        replay captured filings, with a latency sweep
    newsalpha latency-report  summarise a journal's timing records

``scan`` and ``capture`` are the ones to start with. Run ``capture`` for a few
sessions before you backtest anything: without your own captured filings there is
nothing to replay, and a strategy validated on someone else's data is not
validated.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from pathlib import Path

from .backtest import Backtester, BarStore, SignalCache
from .config import Settings, load_settings
from .execution import DhanBroker, OrderRouter, PaperBroker, RiskEngine
from .ingest import build_client, load_announcements
from .pipeline import (
    TradingPipeline,
    build_engine_for,
    build_feeds,
    build_prices,
)
from .timing import EventTimingEngine
from .utils import Journal, read_jsonl

log = logging.getLogger("newsalpha")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO, which drowns the trading log.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _install_signal_handlers(pipeline: TradingPipeline) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, pipeline.stop)


async def _run_pipeline(settings: Settings, mode: str) -> int:
    journal = Journal(Path(settings.journal_dir) / f"{mode}.jsonl")
    timing = EventTimingEngine(journal=Journal(Path(settings.journal_dir) / "timing.jsonl"))
    client = build_client()

    try:
        feeds = build_feeds(settings, client)
        engine = build_engine_for(settings)
        prices = build_prices(settings, client)

        router = None
        if mode in ("paper", "live"):
            risk = RiskEngine(settings.risk, min_confidence=settings.sentiment.min_confidence)
            if mode == "live":
                settings.require_live_credentials()
                if not settings.execution.live_trading_armed:
                    log.error(
                        "live mode requires execution.live_trading_armed=true. "
                        "Refusing to start half-armed."
                    )
                    return 2
                broker = DhanBroker(
                    client,
                    client_id=settings.dhan_client_id,
                    access_token=settings.dhan_access_token,
                    base_url=settings.execution.dhan_base_url,
                    timeout_s=settings.execution.http_timeout_s,
                    armed=True,
                )
                log.warning("LIVE TRADING ARMED - real orders will be placed")
            else:
                broker = PaperBroker(slippage_bps=settings.execution.slippage_bps)  # type: ignore[assignment]
            router = OrderRouter(broker, risk, settings.execution, settings.risk, journal)

        pipeline = TradingPipeline(settings, feeds, engine, router, prices, timing, journal)
        _install_signal_handlers(pipeline)
        await pipeline.run()

        log.info("processed %d announcements", pipeline.processed)
        print(json.dumps(timing.report(), indent=2, default=str))
        return 0
    finally:
        await client.aclose()


async def _capture(settings: Settings, out: str) -> int:
    """Record everything the feeds emit. No scoring, no LLM cost."""
    journal = Journal(out)
    client = build_client()
    seen_count = 0
    try:
        feeds = build_feeds(settings, client)
        from .ingest import merge

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        log.info("capturing to %s - ctrl-c to stop", out)
        async for ann in merge(feeds):
            journal.write(
                {
                    "uid": ann.uid,
                    "source": ann.source,
                    "symbol": ann.symbol,
                    "security_id": ann.security_id,
                    "exchange_segment": ann.exchange_segment,
                    "headline": ann.headline,
                    "body": ann.body,
                    "category": ann.category,
                    "filed_at": ann.filed_at,
                    "disseminated_at": ann.disseminated_at,
                    "received_at": ann.received_at,
                    "attachment_url": ann.attachment_url,
                }
            )
            seen_count += 1
            if seen_count % 25 == 0:
                log.info("captured %d", seen_count)
            if stop.is_set():
                break
    finally:
        await client.aclose()
    log.info("captured %d announcements to %s", seen_count, out)
    return 0


async def _backtest(settings: Settings, path: str | None, no_sweep: bool) -> int:
    source = path or settings.backtest.announcements_path
    announcements = load_announcements(source)
    if not announcements:
        log.error("no announcements in %s - run `newsalpha capture` first", source)
        return 1

    log.info("replaying %d announcements from %s", len(announcements), source)
    bars = BarStore(settings.backtest.bars_path)
    cache = SignalCache(settings.backtest.cache_path)

    # Only build a sentiment engine if the cache does not already cover this run.
    # Re-running a sweep with different risk parameters should not need an API key,
    # and should not re-score - that would change two variables at once.
    uncached = [a for a in announcements if cache.get(a.uid) is None]
    engine = build_engine_for(settings) if uncached else None
    if engine is None:
        log.info("all signals cached; running without a sentiment engine")

    tester = Backtester(
        settings.backtest,
        settings.risk,
        settings.sentiment,
        engine,
        bars,
        cache,
    )
    try:
        result = await tester.run(announcements, sweep=not no_sweep)
    finally:
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.aclose()

    print(json.dumps(result.summary(), indent=2, default=str))

    if result.metrics.trades == 0:
        log.warning(
            "no trades simulated. Usually this means bar data is missing for the "
            "symbols in the capture - check %s",
            settings.backtest.bars_path,
        )
    return 0


def _latency_report(journal_path: str) -> int:
    """Summarise timing records already written to a journal."""
    rows = [r for r in read_jsonl(journal_path) if r.get("type") in ("timing", "press_edge")]
    if not rows:
        log.error("no timing records in %s", journal_path)
        return 1

    def stats(values: list[float], label: str) -> dict[str, float] | None:
        if not values:
            return None
        values.sort()
        return {
            "n": len(values),
            "p50": round(values[len(values) // 2], 3),
            "p90": round(values[min(len(values) - 1, int(len(values) * 0.9))], 3),
            "max": round(values[-1], 3),
            "label": label,  # type: ignore[dict-item]
        }

    report = {
        "records": len(rows),
        "exchange_lag_s": stats(
            [r["exchange_lag_s"] for r in rows if r.get("exchange_lag_s")], "filed -> disseminated"
        ),
        "ingest_lag_s": stats(
            [r["ingest_lag_s"] for r in rows if r.get("ingest_lag_s")], "disseminated -> received"
        ),
        "decision_ms": stats(
            [r["stages_ms"]["total"] for r in rows if r.get("stages_ms", {}).get("total")],
            "received -> order",
        ),
        "press_edge_s": stats(
            [r["press_edge_s"] for r in rows if r.get("press_edge_s")], "disseminated -> newswire"
        ),
    }
    print(json.dumps(report, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="newsalpha", description=__doc__)
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("--log-level", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="score filings without trading")
    sub.add_parser("paper", help="trade against the paper broker")
    sub.add_parser("live", help="trade for real via DhanHQ")

    capture = sub.add_parser("capture", help="record filings to JSONL")
    capture.add_argument("-o", "--out", default="data/announcements.jsonl")

    backtest = sub.add_parser("backtest", help="replay captured filings")
    backtest.add_argument("-i", "--input", help="announcements JSONL")
    backtest.add_argument("--no-sweep", action="store_true", help="skip the latency sweep")

    latency = sub.add_parser("latency-report", help="summarise a timing journal")
    latency.add_argument("-j", "--journal", default="journal/timing.jsonl")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    _setup_logging(args.log_level or settings.log_level)

    if args.command == "latency-report":
        return _latency_report(args.journal)

    try:
        if args.command == "capture":
            return asyncio.run(_capture(settings, args.out))
        if args.command == "backtest":
            return asyncio.run(_backtest(settings, args.input, args.no_sweep))
        return asyncio.run(_run_pipeline(settings, args.command))
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        # Misconfiguration, not a crash. A traceback here just buries the one
        # line that says what to fix.
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
