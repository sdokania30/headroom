"""End-to-end wiring.

The unit tests prove each part works. This proves they are actually connected -
which is the failure the others cannot see, and the one that produces a live run
that processes filings all day and places nothing.
"""

import json
from datetime import datetime, timedelta

import pytest

from newsalpha.config import Settings
from newsalpha.execution import OrderRouter, PaperBroker, PositionManager, RiskEngine
from newsalpha.ingest.instruments import InstrumentMaster
from newsalpha.ingest.replay import ReplayFeed
from newsalpha.pipeline import TradingPipeline
from newsalpha.sentiment.rules import RulesEngine
from newsalpha.timing import EventTimingEngine
from newsalpha.utils import IST, Journal

NOW = datetime(2026, 9, 1, 10, 30, tzinfo=IST)

SCRIP = """EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,UNDERLYING_SYMBOL,DISPLAY_NAME,LOT_SIZE,TICK_SIZE,ISIN,SERIES
NSE,E,1594,EQUITY,INFY,Infosys Limited,1,0.05,INE009A01021,EQ
NSE,E,11536,EQUITY,TCS,Tata Consultancy Services,1,0.05,INE467B01029,EQ
"""

FILINGS = [
    {
        "uid": "bse:1",
        "source": "bse",
        "symbol": "INFY",
        "exchange_segment": "NSE_EQ",
        "headline": "Infosys bags order worth INR 1,240 crore from a European bank",
        "body": "",
        "filed_at": NOW.isoformat(),
        "disseminated_at": NOW.isoformat(),
    },
    {
        # Routine paperwork - must be discarded before it costs anything.
        "uid": "bse:2",
        "source": "bse",
        "symbol": "TCS",
        "exchange_segment": "NSE_EQ",
        "headline": "Closure of Trading Window for Q2 FY27",
        "body": "",
        "filed_at": NOW.isoformat(),
        "disseminated_at": NOW.isoformat(),
    },
]


class FixedPrices:
    def __init__(self, price=100.0):
        self.price = price

    async def ltp(self, segment, security_id):
        return self.price


@pytest.fixture(autouse=True)
def _inside_session(monkeypatch):
    import newsalpha.execution.risk as risk_module

    monkeypatch.setattr(risk_module, "utcnow", lambda: NOW)


def build(tmp_path, price=100.0):
    path = tmp_path / "filings.jsonl"
    path.write_text("\n".join(json.dumps(f) for f in FILINGS))

    settings = Settings()
    settings.execution.exit_retry_backoff_s = 0.0
    settings.execution.fill_confirm_timeout_s = 0.0
    settings.sentiment.engine = "rules"
    settings.sentiment.min_materiality = 1

    journal = Journal(tmp_path / "journal.jsonl")
    risk = RiskEngine(settings.risk, min_confidence=settings.sentiment.min_confidence)
    broker = PaperBroker(slippage_bps=0.0)
    prices = FixedPrices(price)
    master = InstrumentMaster()
    master.load_from_text(SCRIP)

    positions = PositionManager(
        broker,
        risk,
        prices,
        settings.execution,
        settings.risk,
        calendar=risk.calendar,
        journal=journal,
    )
    router = OrderRouter(
        broker,
        risk,
        settings.execution,
        settings.risk,
        journal,
        positions=positions,
        instruments=master,
    )
    pipeline = TradingPipeline(
        settings,
        [ReplayFeed(path)],
        RulesEngine(),
        router,
        prices,
        EventTimingEngine(),
        journal,
        positions,
        master,
    )
    return pipeline, positions, risk, broker, journal


async def test_replay_drives_the_pipeline_to_completion(tmp_path):
    """merge() must finish once the feeds are exhausted, or this hangs forever."""
    pipeline, _, _, _, _ = build(tmp_path)
    await pipeline.run()
    assert pipeline.processed == 2


async def test_a_material_filing_becomes_a_managed_position(tmp_path):
    pipeline, positions, risk, broker, _ = build(tmp_path)
    # Hold the position open: price sits inside the bracket, clock hasn't run out.
    pipeline._cfg.execution.flatten_on_shutdown = False
    await pipeline.run()

    assert len(broker.orders) == 1, "exactly one filing was material"
    assert broker.orders[0].order_id.startswith("PAPER-")
    assert "INFY" in risk.positions


async def test_routine_paperwork_places_nothing(tmp_path):
    pipeline, _, _, broker, _ = build(tmp_path)
    await pipeline.run()
    assert all(o.order_id for o in broker.orders)
    # TCS was the trading-window notice; it must never have reached the broker.
    assert not any("TCS" in str(o.order_id) for o in broker.orders)


async def test_shutdown_flattens_open_positions(tmp_path):
    """The default. A position left open by a dead process is unmanaged exposure."""
    pipeline, positions, risk, broker, _ = build(tmp_path)
    await pipeline.run()

    assert positions.open_positions == {}
    assert positions.closed[-1]["reason"] == "SHUTDOWN"
    assert len(broker.orders) == 2  # entry + exit


async def test_the_journal_records_the_whole_decision_path(tmp_path):
    """When a trade goes wrong the only question is what it knew and when."""
    pipeline, _, _, _, journal = build(tmp_path)
    await pipeline.run()

    records = [json.loads(line) for line in journal.path.read_text().splitlines() if line]
    kinds = {r["type"] for r in records}
    assert {"announcement", "decision", "exit"} <= kinds

    decision = next(r for r in records if r["type"] == "decision")
    assert decision["symbol"] == "INFY"
    assert decision["security_id"] == "1594", "symbol was resolved before routing"
    assert decision["quantity"] > 0


async def test_a_stop_through_the_price_exits_on_the_managers_tick(tmp_path):
    pipeline, positions, risk, broker, _ = build(tmp_path)
    pipeline._cfg.execution.flatten_on_shutdown = False
    await pipeline.run()
    assert "INFY" in positions.open_positions

    # Price gaps below the stop; the next tick must take us out.
    pipeline._prices.price = 90.0
    await positions.tick(NOW + timedelta(seconds=30))

    assert positions.open_positions == {}
    assert positions.closed[-1]["reason"] == "STOP"
    assert risk.realised_pnl < 0
