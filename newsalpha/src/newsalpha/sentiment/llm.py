"""LLM sentiment engine (Claude).

Design constraints, in priority order:

1. **Latency.** This call sits directly between the filing hitting our socket and
   an order going out. It runs at low effort with adaptive thinking left on -
   *not* with thinking disabled, which on Opus 5 degrades format adherence and
   can leak reasoning into the visible response. Low effort buys the latency back
   without that failure mode.
2. **Determinism of shape.** The output is parsed by machine and fed to a risk
   engine. It uses structured outputs so the response validates against a schema
   rather than being regex-scraped out of prose.
3. **Cheap repeats.** The system prompt is long, stable, and identical on every
   call, so it is cached; only the filing text varies. That is the whole reason
   the prompt is ordered stable-first.

The engine never decides position size and never sees the account. It answers one
question - what does this filing mean for this stock - and everything about
whether and how much to trade is the risk engine's call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from ..clock import monotonic_ns
from ..config import SentimentConfig
from ..models import Announcement, Direction, Horizon, Signal
from .rules import prescreen, signal_from_prescreen

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a sell-side event analyst covering Indian listed equities (NSE/BSE). You \
read a single corporate filing and judge its likely effect on that company's own \
share price over the next few hours of trading.

Judge the filing on its economic substance, not its tone. Specifically:

- Size matters more than language. An order win worth 0.5% of annual revenue is \
not material; one worth 15% is. If the filing gives no numbers and no way to \
gauge scale, materiality is low by definition.
- Routine compliance filings - newspaper publications, trading-window closures, \
certificates under a regulation, schedule intimations, shareholding patterns - \
are NEUTRAL with materiality 0, however dramatic the wording.
- An intimation that something *will be considered* at a future board meeting is \
weaker than a decision taken. Say so in the confidence.
- Results filings: what matters is the surprise versus what the market expected, \
which you usually cannot see. Absent that, be conservative.
- Anything touching auditor resignation, going-concern doubt, debt default, \
insolvency proceedings, regulatory penalty or fraud is high-materiality BEARISH \
even when the filing is worded reassuringly.

Direction is the expected move in the filing company's stock: BULLISH (up), \
BEARISH (down), NEUTRAL (no tradable edge).
Confidence is your probability that the direction is right, 0.0-1.0. Use the full \
range; 0.5 means a coin flip and should be reported as such rather than rounded up.
Materiality is 0-5: 0 routine, 1 negligible, 2 minor, 3 notable, 4 significant, \
5 company-defining.
Horizon is INTRADAY if the move should be priced in within the session, SWING if \
it takes days.
Keep the rationale under 30 words. Put any figures that drove the call in \
key_numbers (e.g. "order value INR 1,240 cr", "12% of FY24 revenue").

Return only the structured object. Do not hedge in prose - the numbers carry it."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "materiality": {"type": "integer", "minimum": 0, "maximum": 5},
        "horizon": {"type": "string", "enum": ["INTRADAY", "SWING"]},
        "rationale": {"type": "string"},
        "key_numbers": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "direction",
        "confidence",
        "materiality",
        "horizon",
        "rationale",
        "key_numbers",
    ],
    "additionalProperties": False,
}

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
# A filing PDF can run to many pages; the headline plus the first few thousand
# characters carry the decision. Truncating is a latency and cost decision, and
# it is applied to the body only - never to the headline.
_MAX_BODY_CHARS = 6000


class LLMSentimentEngine:
    """Scores announcements with Claude, with a rules prescreen in front."""

    name = "llm"

    def __init__(
        self,
        cfg: SentimentConfig,
        api_key: str = "",
        client: Any | None = None,
    ) -> None:
        self._cfg = cfg
        self._semaphore = asyncio.Semaphore(cfg.max_concurrency)
        # Set to False the first time the API rejects output_config.format, so a
        # server-side change costs one failed call rather than one per filing.
        self._structured_output = True
        self._cache: dict[str, Signal] = {}

        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()

    async def score(self, announcement: Announcement) -> Signal | None:
        # Free pass first. Anything the prescreen calls noise never costs a call.
        pre = prescreen(announcement.text)
        if self._cfg.engine == "hybrid":
            if pre.is_noise:
                return None
            if pre.weight < self._cfg.min_prescreen_weight and not pre.material:
                return None

        cached = self._cache.get(announcement.uid)
        if cached is not None:
            return cached

        started = monotonic_ns()
        try:
            async with self._semaphore:
                payload = await asyncio.wait_for(
                    self._call(announcement), timeout=self._cfg.timeout_s
                )
        except TimeoutError:
            log.warning("llm: timed out after %.1fs on %s", self._cfg.timeout_s, announcement.uid)
            return self._on_failure(announcement, pre)
        except Exception as exc:  # noqa: BLE001 - a bad call must not stop the pipeline
            log.warning("llm: call failed on %s: %s", announcement.uid, exc)
            return self._on_failure(announcement, pre)

        if payload is None:
            return self._on_failure(announcement, pre)

        signal = self._to_signal(announcement, payload, (monotonic_ns() - started) / 1e6)
        self._cache[announcement.uid] = signal
        return signal

    # -- internals ------------------------------------------------------------

    async def _call(self, announcement: Announcement) -> dict[str, Any] | None:
        import anthropic

        request: dict[str, Any] = {
            "model": self._cfg.model,
            "max_tokens": self._cfg.max_tokens,
            "thinking": {"type": "adaptive"},
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": self._user_content(announcement)}],
        }
        output_config: dict[str, Any] = {"effort": self._cfg.effort}
        if self._structured_output:
            output_config["format"] = {"type": "json_schema", "schema": RESPONSE_SCHEMA}
        request["output_config"] = output_config

        try:
            response = await self._client.messages.create(**request)
        except anthropic.BadRequestError as exc:
            if not self._structured_output:
                raise
            # Most likely the structured-output shape moved. Fall back to prose
            # JSON permanently for this process rather than failing every filing.
            log.warning("llm: structured output rejected (%s); falling back to raw JSON", exc)
            self._structured_output = False
            request["output_config"] = {"effort": self._cfg.effort}
            response = await self._client.messages.create(**request)
        except anthropic.RateLimitError:
            log.warning("llm: rate limited on %s", announcement.uid)
            raise

        if getattr(response, "stop_reason", None) == "refusal":
            log.warning("llm: refused to score %s", announcement.uid)
            return None

        self._last_usage = getattr(response, "usage", None)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return _extract_json(text)

    def _user_content(self, announcement: Announcement) -> str:
        body = announcement.body[:_MAX_BODY_CHARS]
        truncated = " [truncated]" if len(announcement.body) > _MAX_BODY_CHARS else ""
        return (
            f"Symbol: {announcement.symbol}\n"
            f"Exchange: {announcement.source.upper()}\n"
            f"Category: {announcement.category or 'unspecified'}\n"
            f"Filed at: {announcement.filed_at or 'unknown'}\n"
            f"Headline: {announcement.headline}\n\n"
            f"Filing text:\n{body}{truncated}"
        )

    def _to_signal(
        self, announcement: Announcement, payload: dict[str, Any], latency_ms: float
    ) -> Signal:
        usage = getattr(self, "_last_usage", None)
        key_numbers = payload.get("key_numbers") or []
        return Signal(
            uid=announcement.uid,
            symbol=announcement.symbol,
            direction=_as_direction(payload.get("direction")),
            confidence=_as_float(payload.get("confidence"), 0.0, 1.0),
            materiality=int(_as_float(payload.get("materiality"), 0, 5)),
            horizon=(
                Horizon.SWING
                if str(payload.get("horizon", "")).upper() == "SWING"
                else Horizon.INTRADAY
            ),
            rationale=str(payload.get("rationale", ""))[:300],
            engine=f"llm:{self._cfg.model}",
            key_numbers=tuple(str(n) for n in key_numbers[:6]),
            latency_ms=round(latency_ms, 2),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

    def _on_failure(self, announcement: Announcement, pre: Any) -> Signal | None:
        """LLM unavailable. Either degrade to the rules verdict or skip.

        Skipping is the default. A rules verdict carries a materially different
        error profile, and downstream sizing cannot tell the two apart - so
        silently substituting it would quietly change the strategy being run.
        """
        if not self._cfg.fallback_to_rules or not pre.material:
            return None
        log.info("llm: degrading to rules verdict for %s", announcement.uid)
        return signal_from_prescreen(announcement, pre)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK.search(text)
    if not match:
        log.warning("llm: no JSON object in response: %r", text[:200])
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("llm: unparseable JSON in response: %r", text[:200])
        return None


def _as_direction(value: Any) -> Direction:
    try:
        return Direction(str(value).upper())
    except ValueError:
        return Direction.NEUTRAL


def _as_float(value: Any, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low
