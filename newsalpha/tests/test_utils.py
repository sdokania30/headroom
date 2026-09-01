from datetime import datetime, timezone

from newsalpha.ingest.dedupe import SeenSet
from newsalpha.utils import first, in_session, parse_dt


def test_naive_timestamps_are_read_as_ist_not_utc():
    """Getting this wrong shifts every filing by 5h30m and silently voids the
    entire latency measurement."""
    parsed = parse_dt("2026-09-01 10:30:00")
    assert parsed == datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)


def test_aware_timestamps_are_respected():
    assert parse_dt("2026-09-01T05:00:00Z") == datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)


def test_alternate_feed_formats():
    assert parse_dt("01-Sep-2026 10:30:00") is not None
    assert parse_dt("01-09-2026 10:30:00") is not None


def test_unparseable_and_empty_values_return_none():
    for value in (None, "", "NA", "-", "not a date"):
        assert parse_dt(value) is None


def test_first_is_case_insensitive_and_skips_blanks():
    row = {"HEADLINE": "", "NewsSub": "Order win"}
    assert first(row, "headline", "newssub") == "Order win"
    assert first(row, "missing", default="fallback") == "fallback"


def test_session_window_excludes_weekends_and_after_hours():
    from newsalpha.utils import IST

    assert in_session(datetime(2026, 9, 1, 10, 0, tzinfo=IST), "09:20", "15:10")
    assert not in_session(datetime(2026, 9, 1, 9, 0, tzinfo=IST), "09:20", "15:10")
    assert not in_session(datetime(2026, 9, 1, 15, 30, tzinfo=IST), "09:20", "15:10")
    assert not in_session(datetime(2026, 9, 5, 10, 0, tzinfo=IST), "09:20", "15:10")


def test_seen_set_suppresses_repeats():
    """Polling feeds re-serve the same rows every second; without this the same
    filing would be traded repeatedly."""
    seen = SeenSet()
    assert seen.add_if_new("bse:1")
    assert not seen.add_if_new("bse:1")
    assert seen.add_if_new("bse:2")


def test_seen_set_is_bounded():
    seen = SeenSet(max_size=10)
    for i in range(50):
        seen.add_if_new(f"uid:{i}")
    assert len(seen) <= 10
