"""Position manager - the exit path.

These are the tests that matter most in the whole suite. Everything else costs
you an opportunity when it breaks; this costs you money that is already at risk.
"""

from datetime import datetime, timedelta

import pytest

from newsalpha.config import ExecutionConfig, RiskConfig
from newsalpha.execution.base import Broker
from newsalpha.execution.positions import PositionManager
from newsalpha.execution.risk import RiskEngine
from newsalpha.models import OrderAck, OrderIntent, Side
from newsalpha.sessions import TradingCalendar
from newsalpha.utils import IST

NOW = datetime(2026, 9, 1, 10, 30, tzinfo=IST)


class FakeBroker(Broker):
    """Fills at the intent price, optionally rejecting the first N attempts."""

    name = "fake"

    def __init__(self, reject_first: int = 0) -> None:
        self.reject_first = reject_first
        self.placed: list[OrderIntent] = []

    async def place(self, intent: OrderIntent) -> OrderAck:
        self.placed.append(intent)
        if self.reject_first > 0:
            self.reject_first -= 1
            return OrderAck(
                ok=False, order_id="", status="REJECTED", broker=self.name, error="exchange said no"
            )
        return OrderAck(
            ok=True,
            order_id=f"F{len(self.placed)}",
            status="FILLED",
            broker=self.name,
            avg_price=intent.price,
            filled_quantity=intent.quantity,
        )


class FakePrices:
    def __init__(self, price: float | None) -> None:
        self.price = price

    async def ltp(self, segment: str, security_id: str) -> float | None:
        return self.price


def build(price=100.0, reject_first=0, **risk_kwargs):
    risk_cfg = RiskConfig(stop_loss_pct=0.01, take_profit_pct=0.02, **risk_kwargs)
    exec_cfg = ExecutionConfig(exit_retry_backoff_s=0.0, fill_confirm_timeout_s=0.0)
    risk = RiskEngine(risk_cfg, min_confidence=0.65)
    broker = FakeBroker(reject_first=reject_first)
    manager = PositionManager(
        broker,
        risk,
        FakePrices(price),
        exec_cfg,
        risk_cfg,
        calendar=TradingCalendar("09:20", "15:10"),
    )
    return manager, broker, risk


def open_position(manager, risk, **kwargs):
    """Open a position the way the router does - through both records.

    Registering only with the manager would leave the risk engine's exposure
    accounting empty, which is a divergence the code now detects and shouts about.
    """
    intent, ack = entry(**kwargs)
    signal = _signal_for(intent)
    risk.on_fill(signal, ack.filled_quantity, ack.avg_price, now=ack.submitted_at)
    return manager.register(intent, ack)


def _signal_for(intent):
    from newsalpha.models import Direction, Horizon, Signal

    return Signal(
        uid=intent.uid,
        symbol=intent.symbol,
        direction=Direction.BULLISH if intent.side is Side.BUY else Direction.BEARISH,
        confidence=0.9,
        materiality=4,
        horizon=Horizon.INTRADAY,
        rationale="test",
        engine="test",
    )


def entry(side=Side.BUY, price=100.0, quantity=10, at=NOW):
    intent = OrderIntent(
        uid="bse:1",
        symbol="INFY",
        security_id="1594",
        exchange_segment="NSE_EQ",
        side=side,
        quantity=quantity,
        price=price,
    )
    ack = OrderAck(
        ok=True,
        order_id="E1",
        status="FILLED",
        broker="fake",
        submitted_at=at,
        avg_price=price,
        filled_quantity=quantity,
    )
    return intent, ack


# --- levels ----------------------------------------------------------------


def test_long_levels_bracket_the_entry():
    manager, _, risk = build()
    position = manager.register(*entry())
    assert position.stop_price == pytest.approx(99.0)
    assert position.target_price == pytest.approx(102.0)


def test_short_levels_are_inverted():
    """A short's stop is above the entry. Getting this backwards would set a
    'stop' that can only trigger in profit - and never cut the loss."""
    manager, _, risk = build()
    position = manager.register(*entry(side=Side.SELL))
    assert position.stop_price == pytest.approx(101.0)
    assert position.target_price == pytest.approx(98.0)


# --- exits -----------------------------------------------------------------


async def test_stop_exit():
    manager, broker, risk = build(price=98.5)
    open_position(manager, risk)
    await manager.tick(NOW)

    assert manager.open_positions == {}
    assert broker.placed[-1].side is Side.SELL
    assert manager.closed[-1]["reason"] == "STOP"
    assert risk.realised_pnl < 0


async def test_target_exit():
    manager, broker, risk = build(price=102.5)
    open_position(manager, risk)
    await manager.tick(NOW)

    assert manager.closed[-1]["reason"] == "TARGET"
    assert risk.realised_pnl > 0


async def test_short_stop_fires_on_a_rising_price():
    manager, broker, risk = build(price=101.5)
    open_position(manager, risk, side=Side.SELL)
    await manager.tick(NOW)

    assert manager.closed[-1]["reason"] == "STOP"
    assert broker.placed[-1].side is Side.BUY
    assert risk.realised_pnl < 0


async def test_position_is_held_while_inside_the_bracket():
    manager, broker, risk = build(price=100.5)
    open_position(manager, risk)
    await manager.tick(NOW)
    assert "INFY" in manager.open_positions
    assert broker.placed == []


async def test_time_exit_after_the_hold_window():
    manager, _, risk = build(price=100.5, max_hold_minutes=30)
    open_position(manager, risk)
    await manager.tick(NOW + timedelta(minutes=31))
    assert manager.closed[-1]["reason"] == "TIME"


async def test_square_off_before_the_close():
    manager, _, risk = build(price=100.5, square_off_buffer_s=300)
    open_position(manager, risk)
    await manager.tick(NOW.replace(hour=15, minute=8))
    assert manager.closed[-1]["reason"] == "SQUARE_OFF"


async def test_risk_halt_flattens_everything():
    """A halt that leaves positions open has not halted anything that matters."""
    manager, _, risk = build(price=100.5)
    open_position(manager, risk)
    risk.halt("daily loss limit")
    await manager.tick(NOW)
    assert manager.open_positions == {}
    assert manager.closed[-1]["reason"] == "RISK_HALT"


async def test_flatten_all_closes_open_positions():
    manager, broker, risk = build(price=100.5)
    open_position(manager, risk)
    await manager.flatten_all("SHUTDOWN")
    assert manager.open_positions == {}
    assert manager.closed[-1]["reason"] == "SHUTDOWN"


# --- degraded conditions ---------------------------------------------------


async def test_missing_price_does_not_evaluate_levels_but_holds():
    manager, broker, risk = build(price=None)
    open_position(manager, risk)
    await manager.tick(NOW)
    assert "INFY" in manager.open_positions
    assert broker.placed == []


async def test_missing_price_still_honours_the_clock():
    """A quote outage must not turn a time-limited position into an open-ended one."""
    manager, _, risk = build(price=None, max_hold_minutes=30)
    open_position(manager, risk)
    await manager.tick(NOW + timedelta(minutes=31))
    assert manager.closed[-1]["reason"] == "TIME_NO_PRICE"


async def test_missing_price_still_squares_off():
    """Square-off must fire even when the feed is down - especially then."""
    manager, _, risk = build(price=None, square_off_buffer_s=300)
    open_position(manager, risk)
    await manager.tick(NOW.replace(hour=15, minute=8))
    assert manager.closed[-1]["reason"] == "SQUARE_OFF"


async def test_exit_retries_and_succeeds():
    manager, broker, risk = build(price=98.5, reject_first=2)
    open_position(manager, risk)
    await manager.tick(NOW)

    assert manager.open_positions == {}
    assert manager.closed[-1]["attempts"] == 3


async def test_unexitable_position_halts_and_stays_registered():
    """The worst case. It must be loud, must stop new entries, and must keep
    trying - never silently drop a position that still has exposure."""
    manager, broker, risk = build(price=98.5, reject_first=99)
    open_position(manager, risk)
    await manager.tick(NOW)

    assert "INFY" in manager.open_positions, "a stuck position must not be forgotten"
    assert risk.halted
    assert "could not exit" in risk.halt_reason
    assert len(broker.placed) == 3

    # And the next tick tries again rather than giving up for good.
    await manager.tick(NOW)
    assert len(broker.placed) == 6


async def test_pnl_is_booked_to_the_risk_engine():
    manager, _, risk = build(price=102.5)
    open_position(manager, risk, quantity=10)
    await manager.tick(NOW)
    # 10 shares, entry 100, exit 102.5 (target crossed, filled at the quote).
    assert risk.realised_pnl == pytest.approx(25.0)
    assert risk.positions == {}


async def test_diverged_records_still_book_the_pnl(caplog):
    """If the two exposure records disagree, the P&L must still land - a silent
    zero here would corrupt the daily loss limit, which is a kill switch."""
    manager, _, risk = build(price=102.5)
    manager.register(*entry(quantity=10))  # manager only - risk engine never told
    await manager.tick(NOW)

    assert risk.realised_pnl == pytest.approx(25.0)
    assert any("diverged" in r.message for r in caplog.records if r.levelname == "ERROR")


class GatedBroker(Broker):
    """Holds an exit order open so two closers can be made to interleave."""

    name = "gated"

    def __init__(self) -> None:
        self.placed: list[OrderIntent] = []
        self.gate = __import__("asyncio").Event()

    async def place(self, intent: OrderIntent) -> OrderAck:
        self.placed.append(intent)
        await self.gate.wait()
        return OrderAck(
            ok=True,
            order_id=f"O{len(self.placed)}",
            status="FILLED",
            broker=self.name,
            avg_price=intent.price,
            filled_quantity=intent.quantity,
        )


async def test_concurrent_closers_send_exactly_one_exit():
    """Regression: a tick and a shutdown flatten both reaching the same position
    used to send two exits, which does not leave you flat - it leaves you the
    same size the other way round."""
    import asyncio

    risk_cfg = RiskConfig(stop_loss_pct=0.01, take_profit_pct=0.02)
    exec_cfg = ExecutionConfig(exit_retry_backoff_s=0.0, fill_confirm_timeout_s=0.0)
    risk = RiskEngine(risk_cfg, min_confidence=0.65)
    broker = GatedBroker()
    manager = PositionManager(
        broker,
        risk,
        FakePrices(98.0),
        exec_cfg,
        risk_cfg,
        calendar=TradingCalendar("09:20", "15:10"),
    )
    open_position(manager, risk)

    ticking = asyncio.create_task(manager.tick(NOW))
    await asyncio.sleep(0)
    flattening = asyncio.create_task(manager.flatten_all("SHUTDOWN"))
    await asyncio.sleep(0)

    broker.gate.set()
    await asyncio.gather(ticking, flattening)

    assert len(broker.placed) == 1, f"sent {len(broker.placed)} exits for one position"
    assert manager.open_positions == {}
    assert risk.positions == {}


async def test_closing_an_unmanaged_position_is_a_no_op():
    """Defence in depth: a stale reference must not resurrect an exit."""
    manager, broker, risk = build(price=98.0)
    position = open_position(manager, risk)
    await manager.tick(NOW)
    assert len(broker.placed) == 1

    position.closing = False  # pretend a stale caller still holds it
    await manager._close(position, 98.0, "STALE")
    assert len(broker.placed) == 1


async def test_open_losses_count_towards_the_daily_limit():
    """A loss limit that only sees closed trades lets you keep opening positions
    while deeply underwater - the exact situation it exists to stop."""
    manager, _, risk = build(price=100.0, daily_loss_limit=500.0, max_hold_minutes=600)
    open_position(manager, risk, quantity=100)  # 100 shares at 100

    assert not risk.halted

    # Price falls 6%: 600 down on open positions, past the 500 limit. The stop
    # would also fire here - the point is that the account halts on the mark,
    # before the exit has been booked.
    manager._prices.price = 94.0
    await manager.tick(NOW)

    assert risk.halted
    assert "incl. open" in risk.halt_reason or "open" in risk.halt_reason


async def test_mark_to_market_is_reported_every_tick():
    manager, _, risk = build(price=101.0, max_hold_minutes=600)
    open_position(manager, risk, quantity=10)
    await manager.tick(NOW)

    # 10 shares, entry 100, marked at 101.
    assert risk.unrealised_pnl == pytest.approx(10.0)
    assert risk.total_pnl == pytest.approx(10.0)


async def test_the_mark_clears_when_the_position_closes():
    manager, _, risk = build(price=102.5, max_hold_minutes=600)
    open_position(manager, risk, quantity=10)
    await manager.tick(NOW)  # target hit, position closed

    assert risk.positions == {}
    await manager.tick(NOW)  # nothing open -> mark must go back to zero
    assert risk.unrealised_pnl == pytest.approx(0.0)
    assert risk.realised_pnl == pytest.approx(25.0)
