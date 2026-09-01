"""Router: the seam between a signal and real exposure.

These cover the two steps whose absence produces silent failure - instrument
resolution and fill confirmation - plus the hand-off to the position manager.
"""

from datetime import datetime

import pytest

from newsalpha.config import ExecutionConfig, RiskConfig
from newsalpha.execution.base import Broker
from newsalpha.execution.positions import PositionManager
from newsalpha.execution.risk import RiskEngine
from newsalpha.execution.router import OrderRouter
from newsalpha.ingest.instruments import InstrumentMaster
from newsalpha.models import Direction, Horizon, OrderAck, OrderIntent, Side, Signal
from newsalpha.sessions import TradingCalendar
from newsalpha.utils import IST

NOW = datetime(2026, 9, 1, 10, 30, tzinfo=IST)

SCRIP = """EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,UNDERLYING_SYMBOL,DISPLAY_NAME,LOT_SIZE,TICK_SIZE,ISIN,SERIES
NSE,E,1594,EQUITY,INFY,Infosys Limited,1,0.05,INE009A01021,EQ
NSE,E,777,EQUITY,BIGLOT,Big Lot Co,50,0.10,INE777A01011,EQ
NSE,E,999,EQUITY,TTTCO,Trade To Trade Co,1,0.05,INE999A01011,BE
"""


class RecordingBroker(Broker):
    name = "recording"

    def __init__(self, confirm_ok: bool = True) -> None:
        self.placed: list[OrderIntent] = []
        self.confirm_ok = confirm_ok

    async def place(self, intent: OrderIntent) -> OrderAck:
        self.placed.append(intent)
        # A real broker acks PENDING with no fill price - that is the point.
        return OrderAck(ok=True, order_id="O1", status="PENDING", broker=self.name)

    async def confirm(self, ack: OrderAck, timeout_s: float = 10.0) -> OrderAck:
        if not self.confirm_ok:
            return OrderAck(
                ok=False,
                order_id=ack.order_id,
                status="UNCONFIRMED",
                broker=self.name,
                error="never filled",
            )
        return OrderAck(
            ok=True,
            order_id=ack.order_id,
            status="TRADED",
            broker=self.name,
            avg_price=self.placed[-1].price,
            filled_quantity=self.placed[-1].quantity,
        )


class FakePrices:
    async def ltp(self, segment: str, security_id: str) -> float | None:
        return 100.0


def signal(symbol="INFY", direction=Direction.BULLISH, confidence=0.9):
    return Signal(
        uid="bse:1",
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        materiality=4,
        horizon=Horizon.INTRADAY,
        rationale="test",
        engine="test",
    )


def build(confirm_ok=True, with_instruments=True):
    risk_cfg = RiskConfig(stop_loss_pct=0.01, take_profit_pct=0.02)
    exec_cfg = ExecutionConfig(exit_retry_backoff_s=0.0, fill_confirm_timeout_s=1.0)
    risk = RiskEngine(risk_cfg, min_confidence=0.65, calendar=TradingCalendar("09:20", "15:10"))
    broker = RecordingBroker(confirm_ok=confirm_ok)
    positions = PositionManager(
        broker, risk, FakePrices(), exec_cfg, risk_cfg, calendar=risk.calendar
    )
    master = None
    if with_instruments:
        master = InstrumentMaster()
        master.load_from_text(SCRIP)
    router = OrderRouter(broker, risk, exec_cfg, risk_cfg, positions=positions, instruments=master)
    return router, broker, risk, positions


@pytest.fixture(autouse=True)
def _inside_session(monkeypatch):
    """Pin 'now' inside the trading window so routing is not time-dependent."""
    import newsalpha.execution.risk as risk_module

    monkeypatch.setattr(risk_module, "utcnow", lambda: NOW)


async def test_resolves_the_security_id_from_the_symbol():
    """NSE filings carry no securityId. Without resolution nothing is routable."""
    router, broker, _, _ = build()
    await router.handle(signal(), price=100.0, security_id="", segment="NSE_EQ")

    assert broker.placed, "order should have been placed"
    assert broker.placed[0].security_id == "1594"


async def test_unresolvable_symbol_is_not_routed():
    router, broker, _, _ = build()
    ack = await router.handle(signal("NOSUCHCO"), price=100.0, security_id="", segment="NSE_EQ")

    assert ack is None
    assert broker.placed == []


async def test_trade_to_trade_series_is_refused():
    router, broker, _, _ = build()
    ack = await router.handle(signal("TTTCO"), price=100.0, security_id="", segment="NSE_EQ")

    assert ack is None
    assert broker.placed == []


async def test_quantity_is_rounded_to_a_whole_lot():
    router, broker, _, _ = build()
    await router.handle(signal("BIGLOT"), price=100.0, security_id="", segment="NSE_EQ")

    assert broker.placed[0].quantity % 50 == 0


async def test_price_is_snapped_to_the_tick_grid():
    router, broker, _, _ = build()
    await router.handle(signal("BIGLOT"), price=101.237, security_id="", segment="NSE_EQ")

    assert broker.placed[0].price == pytest.approx(101.20)


async def test_a_confirmed_fill_starts_being_managed():
    router, _, risk, positions = build()
    ack = await router.handle(signal(), price=100.0, security_id="", segment="NSE_EQ")

    assert ack is not None and ack.ok
    assert "INFY" in positions.open_positions
    assert "INFY" in risk.positions


async def test_an_unconfirmed_order_creates_no_position():
    """An ack is not a fill. Managing a position that may not exist would later
    send an exit for shares that were never bought."""
    router, _, risk, positions = build(confirm_ok=False)
    ack = await router.handle(signal(), price=100.0, security_id="", segment="NSE_EQ")

    assert ack is not None and not ack.ok
    assert positions.open_positions == {}
    assert risk.positions == {}
    assert risk.consecutive_rejects == 1


async def test_rejected_by_risk_never_reaches_the_broker():
    router, broker, _, positions = build()
    ack = await router.handle(signal(confidence=0.1), price=100.0, security_id="", segment="NSE_EQ")

    assert ack is None
    assert broker.placed == []
    assert positions.open_positions == {}


async def test_bearish_signal_sells():
    router, broker, _, _ = build()
    await router.handle(
        signal(direction=Direction.BEARISH), price=100.0, security_id="", segment="NSE_EQ"
    )
    assert broker.placed[0].side is Side.SELL


async def test_bse_scrip_code_routes_without_an_instrument_master():
    """BSE rows already carry a usable id, so they must still route if the
    scrip master failed to load."""
    router, broker, _, _ = build(with_instruments=False)
    await router.handle(signal(), price=100.0, security_id="500209", segment="BSE_EQ")

    assert broker.placed[0].security_id == "500209"


async def test_entry_then_managed_exit_books_the_pnl():
    """Full round trip through both records: route in, manage out."""
    router, broker, risk, positions = build()
    await router.handle(signal(), price=100.0, security_id="", segment="NSE_EQ")
    assert "INFY" in positions.open_positions

    await positions.flatten_all("SHUTDOWN")

    assert positions.open_positions == {}
    assert risk.positions == {}
    assert broker.placed[-1].side is Side.SELL
    assert positions.closed[-1]["reason"] == "SHUTDOWN"


class SlowBroker(Broker):
    """Takes a measurable amount of time to fill, so concurrent callers overlap."""

    name = "slow"

    def __init__(self) -> None:
        self.placed: list[OrderIntent] = []

    async def place(self, intent: OrderIntent) -> OrderAck:
        import asyncio

        self.placed.append(intent)
        await asyncio.sleep(0.01)
        return OrderAck(
            ok=True,
            order_id=f"O{len(self.placed)}",
            status="FILLED",
            broker=self.name,
            avg_price=intent.price,
            filled_quantity=intent.quantity,
        )


def burst_setup(max_open_positions=5, symbols=30):
    rows = [
        "EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,UNDERLYING_SYMBOL,"
        "DISPLAY_NAME,LOT_SIZE,TICK_SIZE,ISIN,SERIES"
    ]
    for i in range(symbols):
        rows.append(f"NSE,E,{1000 + i},EQUITY,SYM{i},Co {i},1,0.05,INE{i:03d}A01011,EQ")
    master = InstrumentMaster()
    master.load_from_text("\n".join(rows))

    risk_cfg = RiskConfig(
        max_open_positions=max_open_positions,
        max_orders_per_minute=1000,
        stop_loss_pct=0.01,
        take_profit_pct=0.02,
    )
    exec_cfg = ExecutionConfig(fill_confirm_timeout_s=0.0, exit_retry_backoff_s=0.0)
    risk = RiskEngine(risk_cfg, min_confidence=0.65, calendar=TradingCalendar("09:20", "15:10"))
    broker = SlowBroker()
    router = OrderRouter(broker, risk, exec_cfg, risk_cfg, instruments=master)
    return router, broker, risk


async def test_position_cap_holds_under_a_simultaneous_burst():
    """Regression: filings are handled concurrently, so every risk check between
    an approval and its fill was a time-of-check/time-of-use race. A results-day
    burst used to open twice the cap - defeating the control at exactly the
    moment it exists for."""
    import asyncio

    router, broker, risk = burst_setup(max_open_positions=5, symbols=30)
    await asyncio.gather(
        *(router.handle(signal(f"SYM{i}"), 100.0, "", "NSE_EQ") for i in range(30))
    )

    assert len(broker.placed) == 5
    assert len(risk.positions) == 5


async def test_one_symbol_cannot_be_entered_twice_concurrently():
    import asyncio

    router, broker, risk = burst_setup()
    await asyncio.gather(*(router.handle(signal("SYM0"), 100.0, "", "NSE_EQ") for _ in range(4)))

    assert len(broker.placed) == 1
    assert len(risk.positions) == 1


async def test_a_rejected_order_gives_its_reservation_back():
    """A leaked reservation permanently consumes a position slot - the account
    slowly stops being able to trade, with nothing in the log to say why."""
    router, _, risk, positions = build(confirm_ok=False)
    await router.handle(signal(), price=100.0, security_id="", segment="NSE_EQ")

    assert risk.positions == {}
    assert risk.snapshot()["pending_orders"] == 0
    # The slot is free again, so a later signal can still trade.
    assert risk.evaluate(signal(), price=100.0).approved


async def test_reservation_is_released_if_the_task_is_cancelled():
    """Shutdown cancels in-flight handlers; their reservations must not outlive
    them."""
    import asyncio

    router, _, risk = burst_setup()
    task = asyncio.create_task(router.handle(signal("SYM0"), 100.0, "", "NSE_EQ"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert risk.snapshot()["pending_orders"] == 0


async def test_gross_notional_counts_in_flight_orders():
    """Otherwise a burst can commit far more capital than the cap allows."""
    router, _, risk = burst_setup()
    decision = risk.evaluate(signal("SYM0"), price=100.0)

    assert decision.approved
    assert risk.gross_notional == pytest.approx(decision.notional)
