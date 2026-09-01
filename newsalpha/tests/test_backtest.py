import csv
from datetime import datetime, timedelta, timezone

import pytest

from newsalpha.backtest import Backtester, BarStore, SignalCache, compute
from newsalpha.config import BacktestConfig, RiskConfig, SentimentConfig
from newsalpha.models import Announcement, Direction, Horizon, Side, Signal, Trade

FILED = datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)


def write_bars(root, symbol, rows):
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{symbol}.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        writer.writerows(rows)


def minute_bars(start, specs):
    """specs: list of (open, high, low, close), one per minute from `start`."""
    return [
        [(start + timedelta(minutes=i)).isoformat(), o, h, low, c, 1000]
        for i, (o, h, low, c) in enumerate(specs)
    ]


@pytest.fixture
def announcement():
    return Announcement(
        uid="bse:1",
        source="bse",
        symbol="INFY",
        headline="Order win",
        body="",
        filed_at=FILED,
        disseminated_at=FILED,
    )


def bullish_signal(uid="bse:1", confidence=0.9):
    return Signal(
        uid=uid,
        symbol="INFY",
        direction=Direction.BULLISH,
        confidence=confidence,
        materiality=4,
        horizon=Horizon.INTRADAY,
        rationale="test",
        engine="test",
    )


def make_tester(tmp_path, **backtest_kwargs):
    cfg = BacktestConfig(
        bars_path=str(tmp_path / "bars"),
        cache_path=str(tmp_path / "cache.jsonl"),
        hold_minutes=10,
        cost_bps=0.0,
        **backtest_kwargs,
    )
    return Backtester(
        cfg,
        RiskConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_notional_per_trade=100_000),
        SentimentConfig(min_confidence=0.65, min_materiality=3),
        engine=None,  # simulate() never scores; scoring is a separate pass
        bars=BarStore(tmp_path / "bars"),
        cache=SignalCache(tmp_path / "cache.jsonl"),
    )


def test_target_exit(tmp_path, announcement):
    # Flat, then a 3% spike - clears the 2% target.
    write_bars(
        tmp_path / "bars",
        "INFY",
        minute_bars(
            FILED, [(100, 100, 100, 100)] * 2 + [(100, 103, 100, 103)] + [(103, 103, 103, 103)] * 5
        ),
    )
    tester = make_tester(tmp_path)
    trades, _ = tester.simulate([announcement], {"bse:1": bullish_signal()}, delay_s=0.0)

    assert len(trades) == 1
    assert trades[0].exit_reason == "TARGET"
    assert trades[0].side is Side.BUY
    assert trades[0].gross_pnl > 0


def test_stop_exit(tmp_path, announcement):
    write_bars(
        tmp_path / "bars",
        "INFY",
        minute_bars(
            FILED, [(100, 100, 100, 100)] * 2 + [(100, 100, 97, 97)] + [(97, 97, 97, 97)] * 5
        ),
    )
    tester = make_tester(tmp_path)
    trades, _ = tester.simulate([announcement], {"bse:1": bullish_signal()}, delay_s=0.0)

    assert trades[0].exit_reason == "STOP"
    assert trades[0].gross_pnl < 0


def test_stop_wins_when_both_are_hit_in_one_bar(tmp_path, announcement):
    """A minute bar does not say which came first. Assume the worse one."""
    write_bars(
        tmp_path / "bars",
        "INFY",
        minute_bars(
            FILED, [(100, 100, 100, 100)] * 2 + [(100, 105, 95, 100)] + [(100, 100, 100, 100)] * 5
        ),
    )
    tester = make_tester(tmp_path)
    trades, _ = tester.simulate([announcement], {"bse:1": bullish_signal()}, delay_s=0.0)
    assert trades[0].exit_reason == "STOP"


def test_time_exit_when_neither_level_is_touched(tmp_path, announcement):
    write_bars(tmp_path / "bars", "INFY", minute_bars(FILED, [(100, 100.2, 99.8, 100)] * 12))
    tester = make_tester(tmp_path)
    trades, _ = tester.simulate([announcement], {"bse:1": bullish_signal()}, delay_s=0.0)
    assert trades[0].exit_reason == "TIME"


def test_entry_never_precedes_the_filing(tmp_path, announcement):
    """The look-ahead guard: bars before the filing must be unreachable."""
    start = FILED - timedelta(minutes=5)
    write_bars(tmp_path / "bars", "INFY", minute_bars(start, [(100, 100, 100, 100)] * 20))
    tester = make_tester(tmp_path)
    trades, _ = tester.simulate([announcement], {"bse:1": bullish_signal()}, delay_s=0.0)
    assert trades[0].entry_at >= FILED


def test_delay_pushes_the_entry_later(tmp_path, announcement):
    write_bars(
        tmp_path / "bars",
        "INFY",
        minute_bars(FILED, [(100 + i, 100 + i, 100 + i, 100 + i) for i in range(20)]),
    )
    tester = make_tester(tmp_path)

    fast, _ = tester.simulate([announcement], {"bse:1": bullish_signal()}, delay_s=0.0)
    slow, _ = tester.simulate([announcement], {"bse:1": bullish_signal()}, delay_s=300.0)

    assert slow[0].entry_at > fast[0].entry_at
    # Chasing a rising price is exactly the cost of being late.
    assert slow[0].entry_price > fast[0].entry_price


def test_bearish_signal_sells(tmp_path, announcement):
    write_bars(
        tmp_path / "bars",
        "INFY",
        minute_bars(
            FILED, [(100, 100, 100, 100)] * 2 + [(100, 100, 97, 97)] + [(97, 97, 97, 97)] * 5
        ),
    )
    tester = make_tester(tmp_path)
    signal = Signal(
        uid="bse:1",
        symbol="INFY",
        direction=Direction.BEARISH,
        confidence=0.9,
        materiality=4,
        horizon=Horizon.INTRADAY,
        rationale="",
        engine="test",
    )
    trades, _ = tester.simulate([announcement], {"bse:1": signal}, delay_s=0.0)
    assert trades[0].side is Side.SELL
    assert trades[0].exit_reason == "TARGET"
    assert trades[0].gross_pnl > 0


def test_gates_are_reported_not_silently_dropped(tmp_path, announcement):
    write_bars(tmp_path / "bars", "INFY", minute_bars(FILED, [(100, 100, 100, 100)] * 12))
    tester = make_tester(tmp_path)

    _, skipped = tester.simulate([announcement], {"bse:1": bullish_signal(confidence=0.2)}, 0.0)
    assert skipped["low confidence"] == 1

    _, skipped = tester.simulate([announcement], {}, 0.0)
    assert skipped["no signal"] == 1


def test_missing_bar_data_is_skipped_with_a_reason(tmp_path, announcement):
    tester = make_tester(tmp_path)  # no bar files written at all
    trades, skipped = tester.simulate([announcement], {"bse:1": bullish_signal()}, 0.0)
    assert trades == []
    assert skipped["no bar data"] == 1


def test_signal_cache_round_trips(tmp_path):
    cache = SignalCache(tmp_path / "c.jsonl")
    cache.put(bullish_signal())
    reloaded = SignalCache(tmp_path / "c.jsonl")
    restored = reloaded.get("bse:1")
    assert restored is not None
    assert restored.direction is Direction.BULLISH
    assert restored.confidence == 0.9


# --- metrics ---------------------------------------------------------------


def make_trade(pnl_per_share: float, quantity: int = 10) -> Trade:
    return Trade(
        uid="x",
        symbol="INFY",
        side=Side.BUY,
        quantity=quantity,
        entry_at=FILED,
        entry_price=100.0,
        exit_at=FILED + timedelta(minutes=10),
        exit_price=100.0 + pnl_per_share,
        exit_reason="TIME",
        direction=Direction.BULLISH,
        confidence=0.9,
    )


def test_metrics_on_an_empty_list():
    assert compute([]).trades == 0


def test_metrics_basic():
    metrics = compute([make_trade(2), make_trade(-1), make_trade(3)])
    assert metrics.trades == 3
    assert metrics.wins == 2
    assert metrics.hit_rate == pytest.approx(2 / 3)
    assert metrics.net_pnl == pytest.approx(40.0)
    assert metrics.profit_factor == pytest.approx(5.0)


def test_costs_reduce_net_pnl():
    gross = compute([make_trade(2)], cost_bps=0.0)
    net = compute([make_trade(2)], cost_bps=50.0)
    assert net.net_pnl < gross.net_pnl


def test_high_hit_rate_can_still_lose_money():
    """The classic news-strategy failure: many small wins, one huge loss."""
    trades = [make_trade(1) for _ in range(9)] + [make_trade(-20)]
    metrics = compute(trades)
    assert metrics.hit_rate == pytest.approx(0.9)
    assert metrics.net_pnl < 0
    assert metrics.expectancy < 0


def test_max_drawdown_tracks_the_worst_run():
    metrics = compute([make_trade(5), make_trade(-3), make_trade(-4), make_trade(2)])
    assert metrics.max_drawdown == pytest.approx(70.0)
