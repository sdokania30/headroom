"""Rating scale and dashboard rendering."""

import json

import pytest

from newsalpha.dashboard import collect, render, row_from_record, summarise
from newsalpha.models import Direction, Rating, conviction_of, rate

# --- the rating scale -------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "confidence", "materiality", "expected"),
    [
        (Direction.BULLISH, 0.92, 5, Rating.STRONG_BUY),
        (Direction.BULLISH, 0.85, 3, Rating.BUY),
        (Direction.BEARISH, 0.93, 5, Rating.STRONG_SELL),
        (Direction.BEARISH, 0.70, 3, Rating.SELL),
        (Direction.NEUTRAL, 0.99, 5, Rating.HOLD),
    ],
)
def test_ratings(direction, confidence, materiality, expected):
    assert rate(direction, confidence, materiality) is expected


def test_a_certain_read_on_a_trivial_filing_is_not_a_trade():
    """The whole point of weighting confidence by materiality: being sure that a
    name-change notice is mildly positive is not a signal."""
    assert rate(Direction.BULLISH, 0.95, 1) is Rating.HOLD


def test_a_coin_flip_on_a_huge_filing_is_not_a_trade_either():
    assert rate(Direction.BULLISH, 0.30, 5) is Rating.HOLD


def test_conviction_is_bounded():
    assert conviction_of(2.0, 99) == 1.0
    assert conviction_of(-1.0, 5) == 0.0


def test_sides_and_actionability():
    assert Rating.STRONG_BUY.side == "LONG"
    assert Rating.SELL.side == "SHORT"
    assert Rating.HOLD.side == "FLAT"
    assert not Rating.HOLD.is_actionable
    assert Rating.BUY.is_actionable


# --- rows -------------------------------------------------------------------


def record(**kwargs):
    base = {
        "uid": "bse:1",
        "symbol": "infy",
        "direction": "BULLISH",
        "confidence": 0.9,
        "materiality": 5,
        "headline": "Order win",
        "rationale": "big order",
        "filed_at": "2026-09-02T09:42:00+05:30",
    }
    base.update(kwargs)
    return base


def test_row_carries_the_rating_and_normalises_the_symbol():
    row = row_from_record(record())
    assert row.rating == "STRONG BUY"
    assert row.side == "LONG"
    assert row.symbol == "INFY"


def test_row_renders_the_filing_time_in_ist():
    row = row_from_record(record())
    assert "09:42" in row.filed_at


def test_non_signal_records_are_skipped():
    """Journals hold announcements and exits too; only scored rows belong here."""
    assert row_from_record({"uid": "x", "type": "exit"}) is None


# --- collection -------------------------------------------------------------


def test_collect_merges_records_for_one_filing(tmp_path):
    """A filing appears twice - announcement then decision. One row, richest data."""
    path = tmp_path / "journal.jsonl"
    path.write_text(
        json.dumps({"uid": "bse:1", "symbol": "INFY", "headline": "Order win"})
        + "\n"
        + json.dumps(record(headline=""))
        + "\n"
    )
    rows = collect(path)
    assert len(rows) == 1
    assert rows[0].headline == "Order win"
    assert rows[0].rating == "STRONG BUY"


def test_collect_sorts_by_conviction(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                record(uid="a", symbol="LOW", confidence=0.7, materiality=2),
                record(uid="b", symbol="HIGH", confidence=0.95, materiality=5),
                record(uid="c", symbol="MID", confidence=0.8, materiality=3),
            )
        )
    )
    assert [r.symbol for r in collect(path)] == ["HIGH", "MID", "LOW"]


def test_collect_on_a_missing_file_is_empty_not_an_error(tmp_path):
    assert collect(tmp_path / "nope.jsonl") == []


def test_summarise_counts_every_bucket():
    rows = [
        row_from_record(record(uid="a")),
        row_from_record(record(uid="b", direction="NEUTRAL", materiality=0)),
    ]
    counts = summarise(rows)
    assert counts["STRONG BUY"] == 1
    assert counts["HOLD"] == 1
    assert counts["SELL"] == 0


# --- rendering --------------------------------------------------------------


def test_render_embeds_the_rows_as_data():
    rows = [row_from_record(record())]
    page = render(rows)
    assert "STRONG BUY" in page
    assert "INFY" in page
    assert "const ROWS = [" in page


def test_render_escapes_hostile_content():
    """Filing text is third-party input and lands in HTML."""
    rows = [row_from_record(record(headline="<script>alert(1)</script>"))]
    page = render(rows)
    assert "<script>alert(1)</script>" not in page.replace("\\u003c", "<")


def test_standalone_puts_markup_in_the_body():
    page = render([], standalone=True)
    assert page.startswith("<!doctype html>")
    assert '<div class="wrap">' in page.split("<body>")[1]


def test_artifact_fragment_has_no_document_shell():
    fragment = render([], standalone=False)
    assert "<!doctype" not in fragment.lower()
    assert fragment.lstrip().startswith("<title>")


def test_sample_banner_only_appears_for_sample_data():
    assert "Sample data" in render([], sample=True)
    assert "Sample data" not in render([], sample=False)


def test_every_theme_token_is_defined_on_bare_root():
    """A token defined only inside a media query renders one theme's text on the
    other theme's ground."""
    page = render([])
    base = page.split("@media (prefers-color-scheme: dark)")[0]
    for token in ("--ground", "--surface", "--ink", "--line", "--pos", "--neg", "--flat"):
        assert f"{token}:" in base, token
