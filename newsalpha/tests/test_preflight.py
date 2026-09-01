"""Pre-flight: catches the misconfigurations that fail silently at runtime."""

import pytest

from newsalpha.config import Settings
from newsalpha.ingest.instruments import InstrumentMaster
from newsalpha.preflight import FAIL, PASS, WARN, render, run_preflight

SCRIP_ROWS = "\n".join(
    [
        "EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,UNDERLYING_SYMBOL,DISPLAY_NAME,LOT_SIZE,TICK_SIZE,ISIN,SERIES"
    ]
    + [f"NSE,E,{1000 + i},EQUITY,SYM{i},Co {i},1,0.05,INE{i:04d}A01011,EQ" for i in range(1500)]
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    s = Settings()
    s.journal_dir = str(tmp_path / "journal")
    s.execution.instruments_path = str(tmp_path / "scrip.csv")
    return s


def status_for(checks, name):
    return next(c.status for c in checks if c.name == name)


async def run(settings):
    return await run_preflight(settings, live=False, client=None)


async def test_missing_instrument_master_is_a_failure(settings):
    """No scrip master means no NSE signal is ever routable, and the symptom is
    a quiet day rather than an error."""
    checks = await run(settings)
    assert status_for(checks, "instrument master") == FAIL


async def test_populated_instrument_master_passes(settings, tmp_path):
    master = InstrumentMaster(cache_path=settings.execution.instruments_path)
    master.load_from_text(SCRIP_ROWS)
    (tmp_path / "scrip.csv").write_text(SCRIP_ROWS)

    checks = await run(settings)
    assert status_for(checks, "instrument master") == PASS


async def test_truncated_instrument_master_warns(settings, tmp_path):
    head = "\n".join(SCRIP_ROWS.splitlines()[:20])
    (tmp_path / "scrip.csv").write_text(head)
    checks = await run(settings)
    assert status_for(checks, "instrument master") == WARN


async def test_sizing_that_always_hits_the_cap_is_flagged(settings):
    """The subtle one: nothing looks wrong, but the cap binds at every
    confidence level so every trade goes on at maximum size."""
    settings.risk.risk_per_trade_pct = 0.02  # implies 2,000,000 against a 200,000 cap
    checks = await run(settings)
    assert status_for(checks, "position sizing") == WARN


async def test_healthy_sizing_passes(settings):
    checks = await run(settings)
    assert status_for(checks, "position sizing") == PASS


async def test_loss_limit_smaller_than_the_book_is_flagged(settings):
    settings.risk.daily_loss_limit = 1000.0  # book can lose far more than this
    checks = await run(settings)
    assert status_for(checks, "loss limit vs book") == WARN


async def test_empty_holiday_list_warns(settings):
    checks = await run(settings)
    assert status_for(checks, "holiday calendar") == WARN


async def test_configured_holidays_pass(settings):
    settings.risk.holidays = ["2026-10-20"]
    checks = await run(settings)
    assert status_for(checks, "holiday calendar") == PASS


async def test_missing_api_key_fails_unless_rules_only(settings, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert status_for(await run(settings), "anthropic key") == FAIL

    settings.sentiment.engine = "rules"
    assert status_for(await run(settings), "anthropic key") == PASS


async def test_live_without_credentials_fails(settings, monkeypatch):
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    checks = await run_preflight(settings, live=True, client=None)
    assert status_for(checks, "dhan credentials") == FAIL


async def test_live_with_a_disarmed_broker_fails(settings, monkeypatch):
    monkeypatch.setenv("DHAN_CLIENT_ID", "1")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "t")
    settings.execution.broker = "dhan"
    settings.execution.live_trading_armed = False
    checks = await run_preflight(settings, live=True, client=None)
    assert status_for(checks, "broker") == FAIL


async def test_live_fully_armed_passes_the_broker_check(settings, monkeypatch):
    monkeypatch.setenv("DHAN_CLIENT_ID", "1")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "t")
    settings.execution.broker = "dhan"
    settings.execution.live_trading_armed = True
    checks = await run_preflight(settings, live=True, client=None)
    assert status_for(checks, "broker") == PASS


async def test_disabled_shutdown_flatten_warns(settings):
    settings.execution.flatten_on_shutdown = False
    checks = await run(settings)
    assert status_for(checks, "flatten on shutdown") == WARN


async def test_render_exit_code_is_nonzero_only_on_failure(settings, tmp_path):
    failing = await run(settings)  # no instrument master -> FAIL
    _, code = render(failing)
    assert code == 1

    (tmp_path / "scrip.csv").write_text(SCRIP_ROWS)
    settings.risk.holidays = ["2026-10-20"]
    settings.feeds.symbols = ["INFY"]
    passing = await run(settings)
    text, code = render(passing)
    assert code == 0, text
    assert "all checks passed" in text
