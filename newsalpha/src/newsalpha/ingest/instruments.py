"""Instrument master: symbol -> Dhan securityId.

Exchange filings identify a company by trading symbol (NSE) or numeric scrip code
(BSE). Dhan orders need a ``securityId``, which for NSE is Dhan's own token and is
not derivable from the symbol. Without this lookup every NSE-sourced signal is
unroutable, which is the single thing most likely to make a live run place zero
trades while looking perfectly healthy.

Source is Dhan's published scrip master CSV, refreshed daily and cached on disk.
Column names are matched across candidates because Dhan has added and renamed
columns before; a rename should cost one field, not the whole file.

Two things it returns beyond the id, both of which orders actually need:

* ``lot_size`` - order quantity must be a multiple of it. For NSE equities this is
  1 and the point is easy to miss until the first F&O symbol arrives and every
  order is rejected.
* ``tick_size`` - limit prices must be a multiple of it, or the exchange rejects
  the order.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

# Candidate column names, in preference order. Dhan ships both a compact file
# (SEM_* names) and a detailed one (plain names); this reads either.
_COLUMNS = {
    "security_id": ("SECURITY_ID", "SEM_SMST_SECURITY_ID", "securityId"),
    "symbol": ("UNDERLYING_SYMBOL", "SYMBOL_NAME", "SEM_TRADING_SYMBOL", "TRADING_SYMBOL"),
    "display_name": ("DISPLAY_NAME", "SEM_CUSTOM_SYMBOL", "SM_SYMBOL_NAME"),
    "exchange": ("EXCH_ID", "SEM_EXM_EXCH_ID", "EXCHANGE"),
    "segment": ("SEGMENT", "SEM_SEGMENT"),
    "instrument": ("INSTRUMENT", "SEM_INSTRUMENT_NAME", "INSTRUMENT_TYPE"),
    "lot_size": ("LOT_SIZE", "SEM_LOT_UNITS"),
    "tick_size": ("TICK_SIZE", "SEM_TICK_SIZE"),
    "isin": ("ISIN", "SEM_ISIN"),
    "series": ("SERIES", "SEM_SERIES"),
}

# Series worth trading on the cash segment. BE/BZ are trade-to-trade settlement -
# no intraday netting - so an intraday strategy must not touch them.
_TRADABLE_SERIES = frozenset({"EQ", ""})


@dataclass(frozen=True, slots=True)
class Instrument:
    security_id: str
    symbol: str
    display_name: str
    exchange_segment: str
    lot_size: int = 1
    tick_size: float = 0.05
    isin: str = ""
    series: str = ""

    @property
    def tradable_intraday(self) -> bool:
        return self.series.upper() in _TRADABLE_SERIES

    def round_quantity(self, quantity: int) -> int:
        """Largest valid order quantity at or below ``quantity``."""
        if self.lot_size <= 1:
            return max(0, quantity)
        return (quantity // self.lot_size) * self.lot_size

    def round_price(self, price: float) -> float:
        """Snap to the tick grid. Exchanges reject prices that miss it."""
        if self.tick_size <= 0:
            return round(price, 2)
        return round(round(price / self.tick_size) * self.tick_size, 4)


def _pick(row: dict[str, str], field: str) -> str:
    lowered = {k.strip().lower(): v for k, v in row.items() if k}
    for candidate in _COLUMNS[field]:
        value = lowered.get(candidate.lower())
        if value not in (None, ""):
            return str(value).strip()
    return ""


# NSE series suffixes that feeds append to the trading symbol ("INFY-EQ").
_SERIES_SUFFIXES = frozenset({"EQ", "BE", "BZ", "SM", "ST", "GB", "GS", "IV", "MF"})


def _normalise(symbol: str) -> str:
    """Loose key for fallback matching.

    Feeds write the same instrument as "INFY", "INFY-EQ" and "INFY EQ". Strip a
    trailing series suffix, then reduce to alphanumerics. That covers the
    punctuation and suffix variants without pretending to do fuzzy name matching -
    fuzzy matching here would mis-route an order to the wrong company, which is
    considerably worse than failing to place one.
    """
    text = symbol.upper().strip()
    head, sep, tail = text.rpartition("-")
    if sep and tail in _SERIES_SUFFIXES:
        text = head
    return "".join(ch for ch in text if ch.isalnum())


class InstrumentMaster:
    """Cached symbol/id lookup over Dhan's scrip master."""

    def __init__(
        self,
        cache_path: str | Path = "data/scrip_master.csv",
        url: str = SCRIP_MASTER_URL,
        ttl_hours: float = 20.0,
    ) -> None:
        self._cache = Path(cache_path)
        self._url = url
        self._ttl_s = ttl_hours * 3600
        self._by_symbol: dict[tuple[str, str], Instrument] = {}
        self._by_loose: dict[tuple[str, str], Instrument] = {}
        self._by_id: dict[tuple[str, str], Instrument] = {}
        self._by_isin: dict[str, Instrument] = {}
        self.loaded = False

    # -- loading --------------------------------------------------------------

    def _cache_is_fresh(self) -> bool:
        if not self._cache.exists():
            return False
        return (time.time() - self._cache.stat().st_mtime) < self._ttl_s

    async def load(self, client: httpx.AsyncClient | None = None, force: bool = False) -> int:
        """Populate from cache, downloading only when stale.

        Returns the number of instruments indexed. A download failure with a
        stale-but-present cache falls back to the cache and warns: yesterday's
        ids are overwhelmingly still correct, and refusing to trade because a CSV
        was briefly unavailable is the wrong trade-off.
        """
        if self.loaded and not force:
            return len(self._by_symbol)

        if force or not self._cache_is_fresh():
            if client is None:
                log.warning("instrument master is stale and no HTTP client was given")
            else:
                try:
                    response = await client.get(self._url, timeout=30.0)
                    response.raise_for_status()
                    self._cache.parent.mkdir(parents=True, exist_ok=True)
                    self._cache.write_bytes(response.content)
                    log.info("downloaded scrip master (%d bytes)", len(response.content))
                except httpx.HTTPError as exc:
                    if self._cache.exists():
                        log.warning("scrip master download failed (%s); using stale cache", exc)
                    else:
                        log.error("scrip master download failed and no cache exists: %s", exc)
                        return 0

        if not self._cache.exists():
            return 0
        return self.load_from_text(self._cache.read_text(encoding="utf-8", errors="replace"))

    def load_from_text(self, text: str) -> int:
        """Index a scrip master CSV. Separate from IO so it is testable."""
        self._by_symbol.clear()
        self._by_loose.clear()
        self._by_id.clear()
        self._by_isin.clear()

        for row in csv.DictReader(io.StringIO(text)):
            instrument = self._row_to_instrument(row)
            if instrument is None:
                continue
            key = (instrument.exchange_segment, instrument.symbol.upper())
            # First entry wins: the file lists the cash line before derivatives,
            # and a later F&O row must not shadow the equity it is written on.
            self._by_symbol.setdefault(key, instrument)
            self._by_loose.setdefault(
                (instrument.exchange_segment, _normalise(instrument.symbol)), instrument
            )
            self._by_id.setdefault(
                (instrument.exchange_segment, instrument.security_id), instrument
            )
            if instrument.isin:
                self._by_isin.setdefault(instrument.isin.upper(), instrument)

        self.loaded = True
        log.info("indexed %d instruments", len(self._by_symbol))
        return len(self._by_symbol)

    def _row_to_instrument(self, row: dict[str, str]) -> Instrument | None:
        security_id = _pick(row, "security_id")
        symbol = _pick(row, "symbol")
        if not security_id or not symbol:
            return None

        exchange = _pick(row, "exchange").upper() or "NSE"
        segment = _pick(row, "segment").upper()
        instrument_type = _pick(row, "instrument").upper()

        # Cash equity only. Options and futures share a symbol with the underlying
        # and would otherwise shadow it in the index.
        if instrument_type and instrument_type not in ("EQUITY", "ES", "INDEX"):
            return None
        if segment and segment not in ("E", "EQUITY", "I", "INDEX"):
            return None

        try:
            lot_size = max(1, int(float(_pick(row, "lot_size") or 1)))
        except ValueError:
            lot_size = 1
        try:
            tick_size = float(_pick(row, "tick_size") or 0.05) or 0.05
        except ValueError:
            tick_size = 0.05

        return Instrument(
            security_id=security_id,
            symbol=symbol.upper(),
            display_name=_pick(row, "display_name") or symbol,
            exchange_segment=f"{exchange}_EQ",
            lot_size=lot_size,
            tick_size=tick_size,
            isin=_pick(row, "isin"),
            series=_pick(row, "series"),
        )

    # -- lookups --------------------------------------------------------------

    def resolve(self, symbol: str, segment: str = "NSE_EQ") -> Instrument | None:
        """Exact trading-symbol match, then a punctuation-insensitive retry."""
        if not symbol:
            return None
        exact = self._by_symbol.get((segment, symbol.upper()))
        if exact is not None:
            return exact
        return self._by_loose.get((segment, _normalise(symbol)))

    def by_security_id(self, security_id: str, segment: str = "NSE_EQ") -> Instrument | None:
        return self._by_id.get((segment, str(security_id)))

    def by_isin(self, isin: str) -> Instrument | None:
        """ISIN is the only identifier that is stable across both exchanges."""
        return self._by_isin.get(isin.upper()) if isin else None

    def enrich(self, symbol: str, security_id: str, segment: str) -> Instrument | None:
        """Best-effort resolution from whatever a feed happened to provide.

        BSE rows arrive with a numeric scrip code and no symbol; NSE rows arrive
        with a symbol and no id. Try both, in that order.
        """
        if security_id:
            found = self.by_security_id(security_id, segment)
            if found is not None:
                return found
        return self.resolve(symbol, segment)

    def __len__(self) -> int:
        return len(self._by_symbol)
