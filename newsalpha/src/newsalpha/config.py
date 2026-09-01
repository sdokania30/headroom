"""Configuration.

Layered, in increasing precedence: field defaults -> ``config.yaml`` -> environment
variables (``NEWSALPHA__SECTION__FIELD``) -> explicit CLI overrides.

Secrets live only in the environment. Nothing that reads a credential ever reads
it from the YAML file, so the config is safe to commit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeedConfig(BaseModel):
    """Announcement sources.

    Note on sourcing: DhanHQ is a broker API - it gives you quotes, positions and
    order placement. The corporate filings themselves come from the exchanges'
    own announcement feeds (BSE / NSE), which is where this pulls them from. If
    your Dhan plan exposes an announcements endpoint, point ``dhan_ann_path`` at
    it and enable ``dhan``; the normaliser handles it the same way.
    """

    bse: bool = True
    nse: bool = True
    dhan: bool = False

    poll_interval_s: float = Field(default=1.0, ge=0.2)
    # Backoff applied after an error, capped, so a feed outage doesn't turn into
    # a request flood against the exchange.
    error_backoff_max_s: float = Field(default=30.0, ge=1.0)
    http_timeout_s: float = Field(default=3.0, gt=0)

    # Empty list = accept every symbol. A populated list is a hard allowlist,
    # applied at ingest so unwanted names never reach the LLM.
    symbols: list[str] = Field(default_factory=list)
    # Categories worth paying an LLM call for. Empty = all.
    categories: list[str] = Field(default_factory=list)

    dhan_ann_path: str = "/v2/announcements"
    lookback_minutes: int = Field(default=15, ge=1)


class SentimentConfig(BaseModel):
    """Two-stage scoring: a free deterministic prescreen, then the LLM."""

    engine: Literal["rules", "llm", "hybrid"] = "hybrid"

    model: str = "claude-opus-5"
    # Effort, not thinking-off: on Opus 5 disabling thinking degrades tool and
    # format adherence. Low effort keeps latency down without that failure mode.
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    max_tokens: int = Field(default=1024, ge=256)
    timeout_s: float = Field(default=6.0, gt=0)
    max_concurrency: int = Field(default=8, ge=1)

    # Prescreen gates. An announcement scoring below these never reaches the LLM.
    min_prescreen_weight: float = Field(default=0.5, ge=0.0)
    # Signal gates. Below these the signal is dropped before the risk engine.
    min_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    min_materiality: int = Field(default=3, ge=0, le=5)

    # If the LLM call times out, fall back to the rules verdict rather than
    # dropping the event. Off by default: a rules-only verdict is much weaker
    # evidence and sizing does not know the difference.
    fallback_to_rules: bool = False


class RiskConfig(BaseModel):
    """Hard limits. Every one of these is enforced on every order, in both
    paper and live mode - the paper path is only useful if it is the same path.
    """

    equity: float = Field(default=1_000_000.0, gt=0)

    # Fraction of equity risked per trade, assuming the stop is hit.
    #
    # Note how this interacts with the cap below: sizing is risk/stop_loss_pct, so
    # at a 1% stop a 0.5% risk budget asks for 50% of equity in one intraday
    # position and max_notional_per_trade clamps every trade to the same size -
    # which silently disables confidence-based sizing. Keep risk/stop_loss_pct
    # below max_notional_per_trade/equity so the cap binds only at top confidence.
    risk_per_trade_pct: float = Field(default=0.002, gt=0, le=0.05)
    max_notional_per_trade: float = Field(default=200_000.0, gt=0)
    max_gross_notional: float = Field(default=1_000_000.0, gt=0)
    max_open_positions: int = Field(default=5, ge=1)
    max_positions_per_symbol: int = Field(default=1, ge=1)

    stop_loss_pct: float = Field(default=0.01, gt=0, le=0.2)
    take_profit_pct: float = Field(default=0.02, gt=0, le=0.5)

    # Kill switches.
    daily_loss_limit: float = Field(default=25_000.0, gt=0)
    max_consecutive_rejects: int = Field(default=5, ge=1)
    max_orders_per_minute: int = Field(default=10, ge=1)

    # Trading window, IST, inclusive start / exclusive end. Orders outside it are
    # rejected: an announcement at 15:29 is not an intraday opportunity.
    session_start: str = "09:20"
    session_end: str = "15:10"

    min_price: float = Field(default=20.0, ge=0)
    max_price: float = Field(default=100_000.0, gt=0)
    denylist: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_coherent(self) -> RiskConfig:
        if self.max_notional_per_trade > self.max_gross_notional:
            raise ValueError("max_notional_per_trade cannot exceed max_gross_notional")
        if self.take_profit_pct <= self.stop_loss_pct:
            raise ValueError("take_profit_pct must exceed stop_loss_pct for a positive expectancy")
        return self


class ExecutionConfig(BaseModel):
    broker: Literal["paper", "dhan"] = "paper"
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    product_type: Literal["INTRADAY", "CNC", "MARGIN"] = "INTRADAY"
    # Modelled slippage for the paper broker, in basis points of the touch price.
    slippage_bps: float = Field(default=8.0, ge=0)
    dhan_base_url: str = "https://api.dhan.co"
    http_timeout_s: float = Field(default=3.0, gt=0)
    # Live trading requires this to be explicitly flipped *and* broker="dhan".
    # Two independent switches, because one is too easy to leave on.
    live_trading_armed: bool = False


class BacktestConfig(BaseModel):
    announcements_path: str = "data/announcements.jsonl"
    bars_path: str = "data/bars"
    # Assumed delay between the filing timestamp and your fill, in seconds. The
    # sweep in `backtest.engine` re-runs the strategy across a list of these to
    # show how much of the edge is actually latency-dependent.
    execution_delay_s: float = 2.0
    delay_sweep_s: list[float] = Field(default_factory=lambda: [0.0, 1.0, 5.0, 30.0, 300.0])
    hold_minutes: int = Field(default=30, ge=1)
    cost_bps: float = Field(default=12.0, ge=0)
    cache_path: str = "data/sentiment_cache.jsonl"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEWSALPHA__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    feeds: FeedConfig = Field(default_factory=FeedConfig)
    sentiment: SentimentConfig = Field(default_factory=SentimentConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)

    log_level: str = "INFO"
    journal_dir: str = "journal"

    # --- credentials, environment only ---------------------------------------
    @property
    def dhan_client_id(self) -> str:
        return os.environ.get("DHAN_CLIENT_ID", "")

    @property
    def dhan_access_token(self) -> str:
        return os.environ.get("DHAN_ACCESS_TOKEN", "")

    @property
    def anthropic_api_key(self) -> str:
        return os.environ.get("ANTHROPIC_API_KEY", "")

    def require_live_credentials(self) -> None:
        """Fail loudly at startup rather than at the first order."""
        missing = [
            name
            for name, value in (
                ("DHAN_CLIENT_ID", self.dhan_client_id),
                ("DHAN_ACCESS_TOKEN", self.dhan_access_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"live mode needs {', '.join(missing)} in the environment")


def load_settings(path: str | Path | None = None) -> Settings:
    """Build settings from an optional YAML file plus the environment."""
    data: dict[str, Any] = {}
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        loaded = yaml.safe_load(p.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{p} must contain a YAML mapping at the top level")
        data = loaded
    return Settings(**data)
