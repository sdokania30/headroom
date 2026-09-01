from newsalpha.models import Direction
from newsalpha.sentiment.rules import prescreen


def test_routine_compliance_is_suppressed():
    """Most of the feed is this. If it isn't filtered, the LLM bill is the feed."""
    for text in (
        "Closure of Trading Window for Q2 FY25",
        "Newspaper Publication of the audited financial results",
        "Certificate under Regulation 74(5) of SEBI DP Regulations",
        "Intimation of Analyst Meet scheduled for 12 November",
        "Shareholding Pattern for the quarter ended September",
    ):
        result = prescreen(text)
        assert result.is_noise, text
        assert not result.material


def test_order_win_is_bullish_and_material():
    result = prescreen("Company bags order worth INR 1,240 crore from NHAI")
    assert result.direction is Direction.BULLISH
    assert result.material
    assert result.weight > 0.5
    assert result.matched


def test_auditor_resignation_is_maximally_bearish():
    result = prescreen("Intimation regarding resignation of Statutory Auditor")
    assert result.direction is Direction.BEARISH
    assert result.weight >= 0.9


def test_default_on_debt_is_bearish():
    result = prescreen("Disclosure of default on payment of interest on NCDs")
    assert result.direction is Direction.BEARISH


def test_noise_wins_over_a_keyword_match():
    """'Newspaper publication of the buyback notice' is not a buyback."""
    result = prescreen("Newspaper publication of the public announcement for buy-back")
    assert result.is_noise
    assert not result.material


def test_contradictory_filing_scores_lower_than_a_clean_one():
    clean = prescreen("Company bags order worth INR 1,240 crore")
    mixed = prescreen(
        "Company bags order worth INR 1,240 crore; credit rating downgraded by CRISIL"
    )
    assert mixed.weight < clean.weight


def test_unremarkable_text_is_neutral_but_not_noise():
    result = prescreen("Intimation of change of registered office address")
    assert result.direction is Direction.NEUTRAL
    assert not result.material
    assert not result.is_noise
