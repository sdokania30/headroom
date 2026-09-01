from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from newsalpha.config import RiskConfig
from newsalpha.execution.risk import RiskEngine
from newsalpha.models import Direction, Horizon, Signal
from newsalpha.utils import IST

# A Tuesday, 10:30 IST - comfortably inside the trading window.
IN_SESSION = datetime(2026, 9, 1, 10, 30, tzinfo=IST)


def make_signal(confidence: float = 0.9, direction: Direction = Direction.BULLISH) -> Signal:
    return Signal(
        uid="t:1",
        symbol="INFY",
        direction=direction,
        confidence=confidence,
        materiality=4,
        horizon=Horizon.INTRADAY,
        rationale="test",
        engine="test",
    )


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine(RiskConfig(), min_confidence=0.65)


def test_approves_a_clean_signal(engine):
    decision = engine.evaluate(make_signal(), price=1500.0, now=IN_SESSION)
    assert decision.approved
    assert decision.quantity > 0
    assert decision.notional <= RiskConfig().max_notional_per_trade


def test_rejects_outside_the_session(engine):
    after_close = IN_SESSION.replace(hour=16, minute=0)
    decision = engine.evaluate(make_signal(), price=1500.0, now=after_close)
    assert not decision.approved
    assert "session" in decision.reason


def test_rejects_on_weekend(engine):
    saturday = datetime(2026, 9, 5, 10, 30, tzinfo=IST)
    assert not engine.evaluate(make_signal(), price=1500.0, now=saturday).approved


def test_missing_price_is_a_rejection_not_a_guess(engine):
    """Sizing without a price is the failure mode that empties an account."""
    decision = engine.evaluate(make_signal(), price=None, now=IN_SESSION)
    assert not decision.approved
    assert "price" in decision.reason


def test_neutral_signal_never_trades(engine):
    decision = engine.evaluate(
        make_signal(direction=Direction.NEUTRAL), price=1500.0, now=IN_SESSION
    )
    assert not decision.approved


def test_low_confidence_rejected(engine):
    decision = engine.evaluate(make_signal(confidence=0.4), price=1500.0, now=IN_SESSION)
    assert not decision.approved
    assert "confidence" in decision.reason


def test_denylist():
    engine = RiskEngine(RiskConfig(denylist=["INFY"]), min_confidence=0.65)
    decision = engine.evaluate(make_signal(), price=1500.0, now=IN_SESSION)
    assert not decision.approved
    assert "denylist" in decision.reason


def test_size_scales_with_confidence(engine):
    low = engine.evaluate(make_signal(confidence=0.66), price=1000.0, now=IN_SESSION)
    other = RiskEngine(RiskConfig(), min_confidence=0.65)
    high = other.evaluate(make_signal(confidence=0.99), price=1000.0, now=IN_SESSION)
    assert high.quantity > low.quantity


def test_no_second_position_in_the_same_name(engine):
    signal = make_signal()
    first = engine.evaluate(signal, price=1500.0, now=IN_SESSION)
    engine.on_fill(signal, first.quantity, 1500.0, now=IN_SESSION)
    second = engine.evaluate(signal, price=1500.0, now=IN_SESSION)
    assert not second.approved
    assert "already holding" in second.reason


def test_max_open_positions(engine):
    cfg = engine._cfg
    for i in range(cfg.max_open_positions):
        engine.on_fill(replace(make_signal(), symbol=f"SYM{i}"), 10, 100.0, now=IN_SESSION)
    decision = engine.evaluate(make_signal(), price=1500.0, now=IN_SESSION)
    assert not decision.approved
    assert "max open positions" in decision.reason


def test_daily_loss_limit_halts_trading(engine):
    signal = make_signal()
    engine.on_fill(signal, 100, 1000.0, now=IN_SESSION)
    # A 30% adverse move against a 100k position blows through the 25k limit.
    engine.on_close("INFY", 700.0)
    assert engine.halted
    decision = engine.evaluate(make_signal(), price=1500.0, now=IN_SESSION)
    assert not decision.approved
    assert "halted" in decision.reason


def test_consecutive_rejects_trip_the_kill_switch(engine):
    for _ in range(engine._cfg.max_consecutive_rejects):
        engine.on_reject("bad securityId")
    assert engine.halted
    assert "consecutive broker rejects" in engine.halt_reason


def test_order_rate_limit(engine):
    for _ in range(engine._cfg.max_orders_per_minute):
        engine.on_order_sent(IN_SESSION)
    decision = engine.evaluate(make_signal(), price=1500.0, now=IN_SESSION)
    assert not decision.approved
    assert "rate limit" in decision.reason


def test_rate_limit_window_rolls_off(engine):
    stale = IN_SESSION - timedelta(minutes=5)
    for _ in range(engine._cfg.max_orders_per_minute):
        engine.on_order_sent(stale)
    assert engine.evaluate(make_signal(), price=1500.0, now=IN_SESSION).approved


def test_gross_notional_cap():
    cfg = RiskConfig(max_gross_notional=100_000, max_notional_per_trade=100_000)
    engine = RiskEngine(cfg, min_confidence=0.65)
    engine.on_fill(make_signal(), 100, 1000.0, now=IN_SESSION)  # 100k, cap reached
    decision = engine.evaluate(replace(make_signal(), symbol="TCS"), price=1500.0, now=IN_SESSION)
    assert not decision.approved
    assert "gross notional" in decision.reason


def test_new_day_resets_pnl_and_halt(engine):
    engine.halt("test halt")
    engine.realised_pnl = -50_000
    tomorrow = IN_SESSION + timedelta(days=1)
    decision = engine.evaluate(make_signal(), price=1500.0, now=tomorrow)
    assert decision.approved
    assert engine.realised_pnl == 0.0


def test_incoherent_config_is_rejected_at_load():
    with pytest.raises(ValueError):
        RiskConfig(stop_loss_pct=0.05, take_profit_pct=0.02)
