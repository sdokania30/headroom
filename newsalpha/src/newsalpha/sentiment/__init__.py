"""Sentiment scoring."""

from ..config import SentimentConfig
from .base import SentimentEngine
from .llm import LLMSentimentEngine
from .rules import PreScore, RulesEngine, prescreen

__all__ = [
    "LLMSentimentEngine",
    "PreScore",
    "RulesEngine",
    "SentimentEngine",
    "build_engine",
    "prescreen",
]


def build_engine(cfg: SentimentConfig, api_key: str = "") -> SentimentEngine:
    """Pick an engine from config. ``hybrid`` is rules-prescreen + LLM."""
    if cfg.engine == "rules":
        return RulesEngine()  # type: ignore[return-value]
    return LLMSentimentEngine(cfg, api_key=api_key)  # type: ignore[return-value]
