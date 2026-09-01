"""Trading calendar.

Answers three questions the rest of the system keeps asking: is the market open
right now, when does it close today, and how long have we got. The position
manager needs the second one - an intraday position that is still open at the
exchange's square-off is closed by the broker at whatever price is available,
which is not a price you chose.

Exchange holidays are a config list rather than a live lookup. That is deliberate:
a holiday calendar fetched at runtime is a network dependency in the one code path
that must never be uncertain, and the NSE list for a year fits on one screen.
Update it every January. The default list is empty, which means the calendar
treats every weekday as a trading day - so populate it before live use, or the
first Diwali session will surprise you.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time

from .utils import IST

log = logging.getLogger(__name__)


def _hhmm(value: str) -> time:
    hours, _, minutes = value.partition(":")
    return time(int(hours), int(minutes or 0))


@dataclass(frozen=True)
class TradingCalendar:
    """IST equity session window, weekends and holidays excluded."""

    start: str = "09:20"
    end: str = "15:10"
    holidays: frozenset[date] = field(default_factory=frozenset)

    @classmethod
    def from_config(
        cls, start: str, end: str, holidays: list[str] | None = None
    ) -> TradingCalendar:
        parsed: set[date] = set()
        for item in holidays or []:
            try:
                parsed.add(date.fromisoformat(item.strip()))
            except ValueError:
                # A typo'd holiday silently becomes a trading day, which is the
                # dangerous direction, so say something loud rather than skip.
                log.error("ignoring unparseable holiday %r (want YYYY-MM-DD)", item)
        return cls(start=start, end=end, holidays=frozenset(parsed))

    def is_trading_day(self, moment: datetime) -> bool:
        local = moment.astimezone(IST)
        if local.weekday() >= 5:
            return False
        return local.date() not in self.holidays

    def is_open(self, moment: datetime) -> bool:
        if not self.is_trading_day(moment):
            return False
        local = moment.astimezone(IST)
        return _hhmm(self.start) <= local.time() < _hhmm(self.end)

    def session_close(self, moment: datetime) -> datetime:
        """UTC instant at which today's session window ends.

        Returns a time on the same IST calendar day even when the market is shut,
        so callers can compare against it without special-casing weekends. Check
        :meth:`is_trading_day` first if that distinction matters to you.
        """
        local = moment.astimezone(IST)
        close_local = datetime.combine(local.date(), _hhmm(self.end), tzinfo=IST)
        return close_local.astimezone(moment.tzinfo or IST)

    def seconds_to_close(self, moment: datetime) -> float:
        """Negative once the window has passed."""
        return (self.session_close(moment) - moment).total_seconds()

    def closing_soon(self, moment: datetime, buffer_s: float) -> bool:
        """Inside the square-off buffer.

        Used to stop opening new positions before the close rather than after -
        an entry with ninety seconds left is not a trade, it is a donation to the
        spread.
        """
        return 0 <= self.seconds_to_close(moment) <= buffer_s


def in_session(
    moment: datetime, start: str, end: str, holidays: frozenset[date] | None = None
) -> bool:
    """Convenience wrapper for callers that hold no calendar."""
    return TradingCalendar(start, end, holidays or frozenset()).is_open(moment)
