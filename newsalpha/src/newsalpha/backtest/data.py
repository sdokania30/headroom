"""Historical bar data for the backtester.

Expects one CSV per symbol at ``{bars_path}/{SYMBOL}.csv`` with a header:

    timestamp,open,high,low,close,volume

``timestamp`` may be ISO-8601 or any format ``utils.parse_dt`` handles; naive
values are read as IST, matching the exchange feeds. Minute bars are the coarsest
resolution worth using here - on hourly bars the entry and exit land in the same
candle and the result is noise.
"""

from __future__ import annotations

import bisect
import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..utils import parse_dt

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarStore:
    """Lazily-loaded per-symbol bar series with time lookups."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._cache: dict[str, list[Bar]] = {}

    def bars(self, symbol: str) -> list[Bar]:
        key = symbol.upper()
        if key in self._cache:
            return self._cache[key]

        path = self._root / f"{key}.csv"
        series: list[Bar] = []
        if not path.exists():
            log.debug("no bar file for %s (%s)", key, path)
        else:
            with path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    ts = parse_dt(row.get("timestamp") or row.get("date"))
                    if ts is None:
                        continue
                    try:
                        series.append(
                            Bar(
                                ts=ts,
                                open=float(row["open"]),
                                high=float(row["high"]),
                                low=float(row["low"]),
                                close=float(row["close"]),
                                volume=float(row.get("volume") or 0),
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
            series.sort(key=lambda b: b.ts)

        self._cache[key] = series
        return series

    def bar_at_or_after(self, symbol: str, moment: datetime) -> Bar | None:
        """First bar starting at or after ``moment``.

        At-or-after, never before: filling at a price that printed before your
        order existed is the classic backtest lie, and it is exactly the lie that
        would make a latency strategy look profitable when it isn't.
        """
        series = self.bars(symbol)
        if not series:
            return None
        stamps = [b.ts for b in series]
        idx = bisect.bisect_left(stamps, moment)
        return series[idx] if idx < len(series) else None

    def window(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        series = self.bars(symbol)
        if not series:
            return []
        stamps = [b.ts for b in series]
        lo = bisect.bisect_left(stamps, start)
        hi = bisect.bisect_right(stamps, end)
        return series[lo:hi]

    def has(self, symbol: str) -> bool:
        return bool(self.bars(symbol))
