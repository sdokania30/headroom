"""newsalpha - event-driven trading on Indian corporate filings.

Pipeline: exchange filings in, Claude-scored sentiment, risk-gated orders out,
with every stage's latency measured because latency is the premise.
"""

from .config import Settings, load_settings
from .models import (
    Announcement,
    Direction,
    Horizon,
    OrderAck,
    OrderIntent,
    RiskDecision,
    Side,
    Signal,
    Trade,
)

__version__ = "0.1.0"

__all__ = [
    "Announcement",
    "Direction",
    "Horizon",
    "OrderAck",
    "OrderIntent",
    "RiskDecision",
    "Settings",
    "Side",
    "Signal",
    "Trade",
    "__version__",
    "load_settings",
]
