"""Pre-flight checks.

Run before a live session. Everything the system needs in order to trade
correctly is verified here, once, loudly - rather than discovered at 09:21 when
the first material filing arrives and something silently does nothing.

Three outcomes. FAIL means live trading is refused. WARN means it will run but
something is set in a way you probably did not intend. PASS is PASS.

The subtle checks matter more than the obvious ones. Missing credentials
announce themselves; a risk budget that makes the per-trade cap bind at every
confidence level does not - it just quietly turns confidence-based sizing off
and puts every trade on at maximum size.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from .config import Settings
from .ingest.instruments import InstrumentMaster
from .sessions import TradingCalendar
from .utils import IST, utcnow_ist_str

log = logging.getLogger(__name__)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    detail: str

    @property
    def icon(self) -> str:
        return {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}[self.status]


def _config_checks(settings: Settings) -> list[Check]:
    out: list[Check] = []
    risk = settings.risk

    # Sizing is risk/stop_loss_pct. If that exceeds the per-trade cap, the cap
    # binds at every confidence level and confidence-based sizing is dead code -
    # every trade goes on at maximum size. Nothing about the config looks wrong.
    implied = risk.equity * risk.risk_per_trade_pct / risk.stop_loss_pct
    if implied > risk.max_notional_per_trade:
        out.append(
            Check(
                "position sizing",
                WARN,
                f"risk budget implies {implied:,.0f} per trade but the cap is "
                f"{risk.max_notional_per_trade:,.0f}, so the cap binds at every "
                f"confidence level and every trade is maximum size. Lower "
                f"risk_per_trade_pct to about "
                f"{risk.max_notional_per_trade * risk.stop_loss_pct / risk.equity:.4f}.",
            )
        )
    else:
        out.append(
            Check(
                "position sizing",
                PASS,
                f"{implied:,.0f} at full confidence, under the "
                f"{risk.max_notional_per_trade:,.0f} cap",
            )
        )

    worst_case = risk.max_open_positions * risk.max_notional_per_trade * risk.stop_loss_pct
    if worst_case > risk.daily_loss_limit:
        out.append(
            Check(
                "loss limit vs book",
                WARN,
                f"every position stopping out costs {worst_case:,.0f} but the daily "
                f"limit is {risk.daily_loss_limit:,.0f} - the limit will halt you "
                f"mid-book rather than bound the day",
            )
        )
    else:
        out.append(Check("loss limit vs book", PASS, f"worst case {worst_case:,.0f} within limit"))

    if not risk.holidays:
        out.append(
            Check(
                "holiday calendar",
                WARN,
                "risk.holidays is empty, so every weekday counts as a trading day. "
                "Populate it from the NSE calendar or the system will try to trade "
                "exchange holidays.",
            )
        )
    else:
        out.append(Check("holiday calendar", PASS, f"{len(risk.holidays)} holidays configured"))

    calendar = TradingCalendar.from_config(risk.session_start, risk.session_end, risk.holidays)
    now = datetime.now(IST)
    window = f"{risk.session_start}-{risk.session_end} IST"
    if calendar.is_open(now):
        out.append(
            Check(
                "session window",
                PASS,
                f"{window}; open now, {calendar.seconds_to_close(now) / 60:.0f} min to close",
            )
        )
    else:
        out.append(
            Check("session window", PASS, f"{window}; closed right now ({utcnow_ist_str()})")
        )

    if risk.square_off_buffer_s <= 0:
        out.append(
            Check(
                "square-off buffer",
                WARN,
                "no buffer: positions will be held to the session end and closed by "
                "the broker at whatever price exists",
            )
        )
    else:
        out.append(
            Check("square-off buffer", PASS, f"{risk.square_off_buffer_s:.0f}s before close")
        )

    return out


def _execution_checks(settings: Settings, live: bool) -> list[Check]:
    out: list[Check] = []
    execution = settings.execution

    if live:
        missing = [
            name
            for name, value in (
                ("DHAN_CLIENT_ID", settings.dhan_client_id),
                ("DHAN_ACCESS_TOKEN", settings.dhan_access_token),
            )
            if not value
        ]
        if missing:
            out.append(Check("dhan credentials", FAIL, f"missing {', '.join(missing)}"))
        else:
            out.append(Check("dhan credentials", PASS, "present"))

        if execution.broker != "dhan":
            out.append(
                Check("broker", FAIL, f"live mode needs broker=dhan, found {execution.broker}")
            )
        elif not execution.live_trading_armed:
            out.append(
                Check("broker", FAIL, "execution.live_trading_armed is false - orders are disarmed")
            )
        else:
            out.append(Check("broker", PASS, "dhan, ARMED - real orders will be placed"))
    else:
        out.append(Check("broker", PASS, f"{execution.broker} (no real orders)"))

    if not settings.anthropic_api_key and settings.sentiment.engine != "rules":
        out.append(
            Check(
                "anthropic key",
                FAIL,
                "ANTHROPIC_API_KEY is unset and sentiment.engine is not 'rules'",
            )
        )
    else:
        out.append(Check("anthropic key", PASS, f"engine={settings.sentiment.engine}"))

    if not execution.flatten_on_shutdown:
        out.append(
            Check(
                "flatten on shutdown",
                WARN,
                "disabled - a position open when this process stops has nothing watching its stop",
            )
        )
    else:
        out.append(Check("flatten on shutdown", PASS, "enabled"))

    if execution.fill_confirm_timeout_s <= 0 and execution.broker == "dhan":
        out.append(
            Check(
                "fill confirmation",
                WARN,
                "disabled against a real broker: an ack will be treated as a fill",
            )
        )
    else:
        out.append(
            Check("fill confirmation", PASS, f"{execution.fill_confirm_timeout_s:.0f}s budget")
        )

    journal = Path(settings.journal_dir)
    try:
        journal.mkdir(parents=True, exist_ok=True)
        probe = journal / ".preflight"
        probe.write_text("ok")
        probe.unlink()
        out.append(Check("journal writable", PASS, str(journal)))
    except OSError as exc:
        out.append(Check("journal writable", FAIL, f"{journal}: {exc}"))

    return out


async def _instrument_check(settings: Settings, client: httpx.AsyncClient | None) -> Check:
    master = InstrumentMaster(cache_path=settings.execution.instruments_path)
    try:
        count = await master.load(client)
    except Exception as exc:  # noqa: BLE001
        return Check("instrument master", FAIL, f"could not load: {exc}")

    if not count:
        return Check(
            "instrument master",
            FAIL,
            "no instruments indexed - NSE symbols cannot be resolved to securityIds, "
            "so no NSE-sourced signal will ever be routed",
        )
    if count < 1000:
        return Check(
            "instrument master",
            WARN,
            f"only {count} instruments indexed - the file looks truncated",
        )
    return Check("instrument master", PASS, f"{count:,} instruments")


async def run_preflight(
    settings: Settings, live: bool = False, client: httpx.AsyncClient | None = None
) -> list[Check]:
    checks = _config_checks(settings) + _execution_checks(settings, live)
    checks.append(await _instrument_check(settings, client))

    if settings.feeds.symbols:
        checks.append(
            Check("symbol allowlist", PASS, f"{len(settings.feeds.symbols)} symbols only")
        )
    else:
        checks.append(Check("symbol allowlist", WARN, "empty - every listed company is in scope"))

    if os.environ.get("TZ") and os.environ["TZ"] not in ("Asia/Kolkata", "UTC"):
        checks.append(
            Check("timezone", WARN, f"TZ={os.environ['TZ']}; feeds are read as IST regardless")
        )

    return checks


def render(checks: list[Check]) -> tuple[str, int]:
    """Format the report and return it with the exit code the CLI should use."""
    width = max(len(c.name) for c in checks)
    lines = ["", "  pre-flight", "  " + "-" * (width + 40)]
    for check in checks:
        lines.append(f"  [{check.icon}] {check.name.ljust(width)}  {check.detail}")

    failures = sum(1 for c in checks if c.status == FAIL)
    warnings = sum(1 for c in checks if c.status == WARN)
    lines.append("")
    if failures:
        lines.append(f"  {failures} failure(s), {warnings} warning(s) - not safe to trade live")
    elif warnings:
        lines.append(f"  0 failures, {warnings} warning(s) - review before trading live")
    else:
        lines.append("  all checks passed")
    lines.append("")
    return "\n".join(lines), (1 if failures else 0)
