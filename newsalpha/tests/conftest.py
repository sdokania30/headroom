"""Shared fixtures.

The clock is frozen for every test. Time-dependent tests are a defect: this
suite passed for a day and then broke at midnight because the risk engine
recorded its trading day from the real clock at construction while the tests
passed a fixed date, and the two silently disagreed once the real date moved on.
Freezing removes the whole class.
"""

from datetime import datetime

import pytest

from newsalpha.utils import IST

# A Tuesday, 10:30 IST - inside the trading window, outside the square-off buffer.
FROZEN_NOW = datetime(2026, 9, 1, 10, 30, tzinfo=IST)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Pin utcnow() everywhere it influences trading-day or session decisions."""
    for module in (
        "newsalpha.execution.risk",
        "newsalpha.execution.positions",
        "newsalpha.execution.router",
    ):
        try:
            monkeypatch.setattr(f"{module}.utcnow", lambda: FROZEN_NOW)
        except AttributeError:
            # Module does not import utcnow; nothing to pin.
            pass
    return FROZEN_NOW
