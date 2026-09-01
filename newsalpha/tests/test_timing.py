from datetime import datetime, timedelta, timezone

from newsalpha.clock import Stopwatch
from newsalpha.models import Announcement
from newsalpha.timing import EventTimingEngine, similarity

BASE = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)


def make_announcement(**kwargs) -> Announcement:
    defaults = {
        "uid": "bse:1",
        "source": "bse",
        "symbol": "INFY",
        "headline": "Infosys bags order worth INR 1200 crore from a European bank",
        "body": "",
        "filed_at": BASE,
        "disseminated_at": BASE + timedelta(seconds=45),
        "received_at": BASE + timedelta(seconds=46),
    }
    defaults.update(kwargs)
    return Announcement(**defaults)


def test_lag_components_are_separated():
    """Exchange lag and our own ingest lag are different problems."""
    ann = make_announcement()
    assert ann.exchange_lag_s == 45.0
    assert ann.ingest_lag_s == 1.0


def test_lags_are_none_when_the_feed_omits_timestamps():
    ann = make_announcement(filed_at=None, disseminated_at=None)
    assert ann.exchange_lag_s is None
    assert ann.ingest_lag_s is None


def test_stopwatch_reports_per_stage_and_total():
    watch = Stopwatch("x")
    for stage in ("prescreened", "scored", "order_sent"):
        watch.mark(stage)
    breakdown = watch.breakdown()
    assert "received->prescreened" in breakdown
    assert "scored->order_sent" in breakdown
    assert breakdown["total"] >= 0.0


def test_stopwatch_partial_run_does_not_invent_zero_stages():
    """A run that stopped at the prescreen must not report a fake order latency."""
    watch = Stopwatch("x")
    watch.mark("prescreened")
    breakdown = watch.breakdown()
    assert "prescreened->scored" not in breakdown


def test_similarity_matches_a_reworded_headline():
    assert (
        similarity(
            "Infosys bags order worth INR 1200 crore from a European bank",
            "Infosys wins INR 1200 crore order from European bank",
        )
        > 0.4
    )


def test_similarity_ignores_boilerplate_overlap():
    """Two unrelated filings share their compliance boilerplate, not their content."""
    score = similarity(
        "Disclosure under Regulation 30 of SEBI Listing Obligations - plant fire",
        "Disclosure under Regulation 30 of SEBI Listing Obligations - dividend record date",
    )
    assert score < 0.45


def test_press_pickup_is_matched_and_edge_measured():
    engine = EventTimingEngine()
    ann = make_announcement()
    engine.observe(ann, Stopwatch(ann.uid))

    press_at = ann.disseminated_at + timedelta(seconds=240)
    matched = engine.register_press(
        "INFY", "Infosys wins INR 1200 crore order from European bank", press_at
    )

    assert matched is not None
    assert matched.press_edge_s == 240.0


def test_press_pickup_for_a_different_symbol_does_not_match():
    engine = EventTimingEngine()
    ann = make_announcement()
    engine.observe(ann, Stopwatch(ann.uid))
    assert (
        engine.register_press("TCS", ann.headline, ann.disseminated_at + timedelta(seconds=60))
        is None
    )


def test_press_item_predating_the_filing_does_not_match():
    """A story that ran before the filing cannot have been caused by it."""
    engine = EventTimingEngine()
    ann = make_announcement()
    engine.observe(ann, Stopwatch(ann.uid))
    earlier = ann.disseminated_at - timedelta(minutes=5)
    assert engine.register_press("INFY", ann.headline, earlier) is None


def test_press_item_outside_the_window_does_not_match():
    engine = EventTimingEngine(correlation_window=timedelta(minutes=10))
    ann = make_announcement()
    engine.observe(ann, Stopwatch(ann.uid))
    late = ann.disseminated_at + timedelta(hours=3)
    assert engine.register_press("INFY", ann.headline, late) is None


def test_report_shape():
    engine = EventTimingEngine()
    ann = make_announcement()
    engine.observe(ann, Stopwatch(ann.uid))
    engine.register_press("INFY", ann.headline, ann.disseminated_at + timedelta(seconds=120))
    report = engine.report()
    assert report["observed"] == 1
    assert report["press_edge"]["n"] == 1.0
    assert "exchange_lag_ms" in report["stage_latency_ms"]
