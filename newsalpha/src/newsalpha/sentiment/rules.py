"""Deterministic prescreen.

Roughly nine out of ten exchange announcements are routine compliance paperwork -
newspaper publications, trading-window closures, certificates under some
regulation. Sending those to an LLM costs money and, far more importantly, costs
milliseconds on the one filing per day that actually matters.

So this runs first: a compiled-regex pass that takes microseconds and answers two
questions - is this material at all, and which way does it lean. The LLM only
sees what survives. It also doubles as the degraded-mode fallback if the LLM call
times out.

The lexicon is the strategy's opinion, not a fact. Tune it on your own captured
data; the defaults below are a starting point for Indian large/mid-cap filings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import Announcement, Direction, Horizon, Signal

# (pattern, direction, weight). Weight is a materiality proxy in [0, 1].
_LEXICON: tuple[tuple[str, Direction, float], ...] = (
    # --- bullish -------------------------------------------------------------
    (
        r"\b(bags?|wins?|secure[sd]?|receiv\w+)\s+(a\s+)?(new\s+)?(order|contract|lo[ia])\b",
        Direction.BULLISH,
        0.9,
    ),
    (r"\border\s+(win|inflow|book)\b", Direction.BULLISH, 0.8),
    (r"\b(letter of (intent|award)|work order)\b", Direction.BULLISH, 0.8),
    (r"\bbuy[- ]?back\b", Direction.BULLISH, 0.9),
    (r"\bbonus\s+(issue|share)", Direction.BULLISH, 0.8),
    (r"\bstock\s+split|sub-?division of (equity )?shares\b", Direction.BULLISH, 0.6),
    (r"\b(interim|final|special)\s+dividend\b", Direction.BULLISH, 0.5),
    (
        r"\b(usfda|us fda|edqm|who-?gmp)\b.{0,40}\b(approval|clearance|eir|zero observations)\b",
        Direction.BULLISH,
        0.9,
    ),
    (r"\bpatent (granted|allowed)\b", Direction.BULLISH, 0.6),
    (r"\b(credit )?rating\b.{0,30}\bupgrad", Direction.BULLISH, 0.8),
    (r"\bcapacity (expansion|addition)|new (plant|facility) commission", Direction.BULLISH, 0.7),
    (r"\b(acquisition of|acquires?|stake purchase|merger)\b", Direction.BULLISH, 0.7),
    (r"\bfund rais\w+|qip|preferential (issue|allotment)\b", Direction.BULLISH, 0.5),
    # --- bearish -------------------------------------------------------------
    (
        r"\bresignation of\b.{0,40}\b(cfo|chief financial|managing director|md|ceo|auditor)\b",
        Direction.BEARISH,
        0.9,
    ),
    (r"\bauditor\b.{0,30}\bresign", Direction.BEARISH, 1.0),
    (
        r"\b(qualified opinion|adverse opinion|disclaimer of opinion|emphasis of matter)\b",
        Direction.BEARISH,
        0.9,
    ),
    (
        r"\b(sebi|rbi|nse|bse)\b.{0,40}\b(penalty|fine|show[- ]cause|adjudication|debarr)",
        Direction.BEARISH,
        0.9,
    ),
    (
        r"\b(gst|income tax|it department)\b.{0,40}\b(demand|notice|search|survey|raid|seizure)\b",
        Direction.BEARISH,
        0.8,
    ),
    (r"\b(credit )?rating\b.{0,30}\bdowngrad", Direction.BEARISH, 0.9),
    (r"\bdefault\b.{0,30}\b(payment|interest|principal|repayment)\b", Direction.BEARISH, 1.0),
    (r"\b(nclt|insolvency|cirp|liquidation|winding up)\b", Direction.BEARISH, 1.0),
    (
        r"\b(fire|explosion|accident|shutdown|lock[- ]?out|strike)\b.{0,30}\b(plant|facility|unit|factory)\b",
        Direction.BEARISH,
        0.8,
    ),
    (r"\bproduct recall|import alert|warning letter\b", Direction.BEARISH, 0.9),
    (r"\b(fraud|misappropriation|embezzle|forensic audit)\b", Direction.BEARISH, 1.0),
    (r"\b(invocation|creation) of pledge\b", Direction.BEARISH, 0.7),
    (r"\bimpairment\b|\bwrite[- ]?off\b", Direction.BEARISH, 0.6),
    (r"\bcontract\b.{0,30}\b(terminat|cancell)", Direction.BEARISH, 0.9),
)

# Routine filings. Matching any of these suppresses the announcement outright,
# regardless of what else it matched - "newspaper publication of the results" is
# not the results.
_NOISE = (
    r"\btrading window\b",
    r"\bnewspaper (publication|advertisement|clipping)\b",
    r"\bcompliance certificate\b",
    r"\bcertificate under regulation\b",
    r"\breg(ulation)?\.?\s*7\(3\)|\breg(ulation)?\.?\s*74\s*\(5\)",
    r"\bintimation of (the )?(board meeting|analyst|investor) (meet|call|presentation)\b",
    r"\bschedule of (analyst|investor)\b",
    r"\bcopy of (the )?(newspaper|advertisement)\b",
    r"\bshareholding pattern\b",
    r"\bdisclosure under regulation 30\b.{0,40}\bnewspaper\b",
    r"\bloss of share certificate|duplicate share certificate\b",
)

_COMPILED = tuple((re.compile(p, re.I), d, w) for p, d, w in _LEXICON)
_NOISE_RE = re.compile("|".join(_NOISE), re.I)


@dataclass(frozen=True, slots=True)
class PreScore:
    direction: Direction
    weight: float
    matched: tuple[str, ...]
    is_noise: bool

    @property
    def material(self) -> bool:
        return not self.is_noise and self.weight > 0.0


def prescreen(text: str) -> PreScore:
    """Classify an announcement with regexes. Microseconds, no network."""
    if _NOISE_RE.search(text):
        return PreScore(Direction.NEUTRAL, 0.0, (), is_noise=True)

    bull = bear = 0.0
    matched: list[str] = []
    for pattern, direction, weight in _COMPILED:
        found = pattern.search(text)
        if not found:
            continue
        matched.append(found.group(0).strip()[:60])
        if direction is Direction.BULLISH:
            bull = max(bull, weight)
        else:
            bear = max(bear, weight)

    if bull == bear == 0.0:
        return PreScore(Direction.NEUTRAL, 0.0, (), is_noise=False)
    # Contradictory hits (an order win *and* a rating downgrade in one filing)
    # net out. The residual is genuinely less tradable, so a smaller weight here
    # is the right answer rather than a bug.
    if bull >= bear:
        return PreScore(Direction.BULLISH, bull - bear * 0.5, tuple(matched), False)
    return PreScore(Direction.BEARISH, bear - bull * 0.5, tuple(matched), False)


class RulesEngine:
    """Prescreen used as a standalone engine, for degraded mode and testing."""

    name = "rules"

    async def score(self, announcement: Announcement) -> Signal | None:
        pre = prescreen(announcement.text)
        if not pre.material:
            return None
        return signal_from_prescreen(announcement, pre)

    async def aclose(self) -> None:
        return None


def signal_from_prescreen(announcement: Announcement, pre: PreScore) -> Signal:
    """Convert a prescreen into a Signal.

    Confidence is capped at 0.7: a keyword match is weaker evidence than a read
    of the actual filing, and sizing downstream is a function of confidence.
    """
    weight = min(pre.weight, 1.0)
    return Signal(
        uid=announcement.uid,
        symbol=announcement.symbol,
        direction=pre.direction,
        confidence=round(min(0.7, 0.35 + weight * 0.35), 3),
        materiality=max(1, min(5, round(weight * 5))),
        horizon=Horizon.INTRADAY,
        rationale="keyword match: " + ", ".join(pre.matched[:4]),
        engine="rules",
        key_numbers=(),
    )
