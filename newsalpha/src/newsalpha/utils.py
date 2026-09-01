"""Small shared helpers: timezone handling, tolerant field access, JSONL journals."""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:  # pragma: no cover - depends on the host having tzdata installed
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # noqa: BLE001 - slim containers often ship without tzdata
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# Formats seen across BSE, NSE and broker payloads. Ordered by observed frequency.
_DT_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%d-%b-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%Y-%m-%d",
)


def parse_dt(value: Any, assume_tz: Any = IST) -> datetime | None:
    """Parse a timestamp from a feed, returning an aware UTC datetime.

    Exchange feeds emit naive local (IST) timestamps. Treating those as UTC would
    silently shift every filing by 5h30m and make the whole latency measurement
    meaningless, so naive values are localised to ``assume_tz`` before conversion.
    """
    if value in (None, "", "NA", "-"):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        dt = None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in _DT_FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            log.debug("unparseable timestamp: %r", value)
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt.astimezone(timezone.utc)


def first(row: dict[str, Any], *keys: str, default: str = "") -> str:
    """First non-empty value among ``keys``.

    Exchange JSON is not a stable contract - fields get renamed and casing drifts
    between endpoints. Matching case-insensitively across a list of candidates
    keeps a rename from silently zeroing a field the strategy depends on.
    """
    lowered = {k.lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return str(value).strip()
    return default


def utcnow_ist_str() -> str:
    """Current time in IST, for human-facing messages."""
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")


def in_session(moment: datetime, start: str, end: str) -> bool:
    """Is ``moment`` inside the IST intraday window, on a weekday?

    Exchange holidays are *not* handled here - wire a holiday calendar in before
    live use, or the first Diwali session will surprise you.
    """
    local = moment.astimezone(IST)
    if local.weekday() >= 5:
        return False
    return _hhmm(start) <= local.time() < _hhmm(end)


def _hhmm(value: str) -> time:
    hours, _, minutes = value.partition(":")
    return time(int(hours), int(minutes or 0))


class Journal:
    """Append-only JSONL writer.

    Every decision the system makes is written here before it is acted on. When a
    trade goes wrong the question is always "what did it know and when", and this
    is the only thing that answers it.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        except OSError:  # noqa: BLE001 - journalling must never break trading
            log.exception("journal write failed: %s", self.path)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL file, skipping malformed lines rather than aborting."""
    out: list[dict[str, Any]] = []
    p = Path(path)
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("%s:%d: skipping malformed JSON line", p, lineno)
    return out
