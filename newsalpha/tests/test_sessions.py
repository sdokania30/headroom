from datetime import datetime

from newsalpha.sessions import TradingCalendar, in_session
from newsalpha.utils import IST

CAL = TradingCalendar.from_config("09:20", "15:10", ["2026-10-20", "2026-11-05"])


def at(day=1, hour=10, minute=0, month=9):
    return datetime(2026, month, day, hour, minute, tzinfo=IST)


def test_open_during_the_window_on_a_weekday():
    assert CAL.is_open(at(hour=10))


def test_closed_before_open_and_after_close():
    assert not CAL.is_open(at(hour=9, minute=0))
    assert not CAL.is_open(at(hour=15, minute=30))


def test_closed_at_the_boundary():
    """End is exclusive: an entry at the closing second is not a trade."""
    assert CAL.is_open(at(hour=9, minute=20))
    assert not CAL.is_open(at(hour=15, minute=10))


def test_weekends_are_closed():
    assert not CAL.is_open(at(day=5))  # Saturday
    assert not CAL.is_open(at(day=6))  # Sunday


def test_holidays_are_closed():
    """Without this the system would happily trade Diwali."""
    assert not CAL.is_trading_day(at(day=20, month=10))
    assert not CAL.is_open(at(day=20, month=10))


def test_unparseable_holiday_is_ignored_not_fatal():
    cal = TradingCalendar.from_config("09:20", "15:10", ["not-a-date", "2026-10-20"])
    assert not cal.is_trading_day(at(day=20, month=10))
    assert cal.is_open(at(hour=10))


def test_seconds_to_close_goes_negative_after_the_close():
    assert CAL.seconds_to_close(at(hour=15, minute=0)) == 600.0
    assert CAL.seconds_to_close(at(hour=15, minute=20)) < 0


def test_closing_soon_only_inside_the_buffer():
    assert not CAL.closing_soon(at(hour=14, minute=0), buffer_s=300)
    assert CAL.closing_soon(at(hour=15, minute=6), buffer_s=300)
    # Already past the close is not "closing soon" - it is closed.
    assert not CAL.closing_soon(at(hour=15, minute=30), buffer_s=300)


def test_no_holidays_configured_means_every_weekday_trades():
    bare = TradingCalendar.from_config("09:20", "15:10", [])
    assert bare.is_open(at(day=20, month=10))


def test_in_session_wrapper_matches_the_calendar():
    assert in_session(at(hour=10), "09:20", "15:10")
    assert not in_session(at(day=5), "09:20", "15:10")
