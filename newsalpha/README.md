# newsalpha

Event-driven trading on Indian corporate filings: pull exchange announcements the
moment they publish, have Claude judge what they mean, size the trade against hard
risk limits, and route it to DhanHQ — measuring latency at every stage, because
latency is the entire premise.

```
BSE / NSE filings ──► dedupe ──► regex prescreen ──► Claude ──► risk gate ──► broker
                                       │                │          │            │
                                       └──────── event timing engine ───────────┘
                                                        │
                                                    journal (JSONL)
```

---

## Read this before you run it

**One correction to the premise, up front.** DhanHQ is a *broker* API — quotes,
positions, order placement. Its published v2 API does not document a
corporate-announcements endpoint. The filings themselves come from the exchanges
(BSE and NSE), which is where this pulls them from. Dhan does the half it is
actually good at: pricing and execution. If your Dhan plan does expose a filings
endpoint, point `feeds.dhan_ann_path` at it and flip `feeds.dhan: true` — the
adapter is there and normalises into the same shape.

**On the edge being traded.** Exchange filings are public disclosures, and reading
them faster than a newswire does is a legitimate infrastructure edge — this is not
trading on non-public information. But the edge is an empirical claim, not a given.
The `latency-report` command and the backtester's delay sweep exist specifically to
test it. Run those before you commit money: if P&L barely changes between a 1-second
and a 30-second delay, the strategy is not a latency strategy and the engineering
spend belongs elsewhere.

**Defaults are deliberately timid.** Paper broker, live trading disarmed, small
size, and a rules-based prescreen that discards ~90% of the feed before it costs
an API call. Every one of those is a decision you should re-make deliberately
rather than inherit.

---

## Install

```bash
cd newsalpha
python -m pip install -e ".[dev]"
cp config.example.yaml config.yaml
cp .env.example .env          # fill in your keys; never commit this
```

Requires Python 3.10+.

## Use

```bash
# 1. Score live filings, place nothing. Only needs ANTHROPIC_API_KEY.
newsalpha -c config.yaml scan

# 2. Record filings for a few sessions so you have something to backtest.
newsalpha -c config.yaml capture -o data/announcements.jsonl

# 3. Replay them, with the latency sweep.
newsalpha -c config.yaml backtest

# 4. Paper trade the full pipeline.
newsalpha -c config.yaml paper

# 5. Live. Needs Dhan credentials AND execution.live_trading_armed: true.
newsalpha -c config.yaml live

# Timing summary from a journal.
newsalpha latency-report -j journal/timing.jsonl
```

Start at `scan`. Do not skip step 2 — a strategy validated on somebody else's
captured data is not validated.

---

## How the five pieces are built

### 1. Data ingestion (`ingest/`)

`PollingFeed` owns the poll loop, exponential error backoff, jittered intervals
and cross-source de-duplication, so every adapter behaves identically when the
network misbehaves. Adapters normalise into one `Announcement`, matching field
names case-insensitively across several candidates — these exchange endpoints are
undocumented and unversioned, so a rename should degrade one field rather than
break ingestion.

`ReplayFeed` satisfies the same interface from a JSONL file. That is what makes
the backtest worth anything: research and production run identical code from
ingest onward.

### 2. Sentiment (`sentiment/`)

Two stages, and the first one is the important one commercially.

**Prescreen** (`rules.py`) is compiled regex: microseconds, no network. Most of
the feed is compliance paperwork — newspaper publications, trading-window
closures, certificates under some regulation — and it is discarded before it
costs an API call or, more importantly, a millisecond. Noise patterns override
keyword hits, so "newspaper publication of the buyback notice" is correctly not a
buyback.

**Claude** (`llm.py`) reads what survives and returns a structured verdict:
direction, confidence, materiality 0–5, horizon, rationale, and the figures that
drove the call. Three choices worth knowing about:

- **Model `claude-opus-5` at `effort: low`, with adaptive thinking left on.** Not
  thinking-disabled — on Opus 5 that degrades format adherence and can leak
  reasoning into the response. Low effort buys the latency back without that.
- **Structured outputs**, so the response validates against a schema instead of
  being regex-scraped out of prose. If the API rejects the format, it falls back
  to raw-JSON parsing once and remembers, rather than failing every filing.
- **Prompt caching** on the system prompt, which is long, stable and identical on
  every call. Only the filing text varies.

The engine never sees the account and never decides size. It answers one question.

### 3. Event timing (`timing/`, `clock.py`)

Four intervals, kept strictly separate because conflating them is how people
convince themselves they have an edge:

| Interval | Span | Yours to fix? |
|---|---|---|
| `exchange_lag` | company filed → exchange published | No, but it bounds everything |
| `ingest_lag` | exchange published → you received | **Yes** — this repays engineering |
| `decision_lag` | received → order sent | **Yes** — per-stage in `Stopwatch` |
| `press_edge` | exchange published → newswire pickup | This *is* the edge |

`press_edge` is measured by correlating a later newswire item back to the filing
that caused it — same symbol, later timestamp, strong headline overlap after
stripping boilerplate. Deliberately conservative: a false match inflates the
apparent edge, which is the one error that would make you trade more.

### 4. Execution (`execution/`)

`RiskEngine` gates every order, in paper and live alike — a paper run only means
something if it exercises the same gates. Session window, denylist, price band,
daily loss limit, order rate limit, position caps, gross notional headroom,
consecutive-reject kill switch. Every rejection carries a specific reason.

Sizing is risk-parity — the stop distance sets the size, not the price — scaled by
the model's confidence, then clamped by the per-trade cap and remaining headroom.

**No live price means no trade.** A missing quote is a rejection, never a guess.

Live trading needs two independent switches (`broker: dhan` *and*
`live_trading_armed: true`). One flag is too easy to leave set in a config you
copied from somewhere.

### 5. Backtesting (`backtest/`)

Built to avoid the two ways event backtests lie:

- **Look-ahead.** Entry is the first bar at or *after* filing time plus delay —
  never the bar containing it. Exits scan bars strictly after the entry bar, so
  the entry candle's own extremes, which may have printed before the filing, can
  never trigger a stop. When a stop and target both fall inside one bar, the stop
  is assumed to have filled first.
- **Free latency.** `delay_sweep_s` re-runs the same signals at 0s / 1s / 5s /
  30s / 300s and reports P&L against delay. **This is the output that decides
  whether the strategy has a premise.** Scoring is cached to disk, so the sweep
  costs one pass of API calls, not five.

The sweep can only resolve delays that cross a bar boundary. On minute bars, 1s /
5s / 30s land in the same candle and return identical numbers — which reads as
"latency doesn't matter" but actually means "this data can't see it". A flat
sub-minute sweep is a data-resolution result, not a strategy result; you need tick
or sub-minute bars to say anything about a sub-minute edge.

Metrics report expectancy and profit factor alongside hit rate, because the
standard way this strategy fails is a 90% hit rate with negative expectancy — many
small wins and one gap that takes the year.

---

## Bar data

`data/bars/{SYMBOL}.csv`, one file per symbol:

```csv
timestamp,open,high,low,close,volume
2026-09-01T09:15:00,1500.0,1502.5,1499.0,1501.2,120000
```

Minute bars or finer. Naive timestamps are read as IST, matching the feeds. On
hourly bars entry and exit land in the same candle and the result is noise.

---

## Testing

```bash
pytest
```

Covers the risk gates, the prescreen lexicon, IST timestamp handling, the
press-correlation window, and the backtester's look-ahead guards.

---

## Known gaps

Honest list of what is *not* done, in rough order of how much it matters:

1. **No exchange holiday calendar.** `in_session` handles weekends and hours, not
   holidays. Wire one in before live use.
2. **NSE symbols carry no `security_id`.** Dhan orders need one; you need an
   instrument-master lookup to map symbol → securityId per segment.
3. **Fills are not tracked to completion.** `DhanBroker.place` returns on the
   broker's `PENDING` ack. Poll `status` or subscribe to the order-update socket
   for real fills, and reconcile positions against the broker rather than trusting
   local state.
4. **No exit management in live mode.** The backtester models stops and targets;
   the live path opens positions and does not manage them. Attach bracket/super
   orders at entry, or run a position manager, before trading real size.
5. **Attachment PDFs are not read.** Filings often put the material detail in an
   attached PDF; only headline and body text are scored today.
6. **The delay sweep is bounded by bar resolution** (above). Minute bars cannot
   resolve a sub-minute edge, which is the range this strategy actually lives in.
7. **`slippage_bps` is a constant.** In the seconds after a material filing the
   book is thin and moving. Paper P&L is a ceiling, not an estimate — replace this
   with your own measured fills as soon as you have any.

---

## Layout

```
src/newsalpha/
  models.py         frozen domain objects; the three timestamps that matter
  config.py         layered config; secrets stay in the environment
  clock.py          per-stage stopwatch, rolling percentiles
  utils.py          IST handling, tolerant field access, JSONL journal
  ingest/           feed loop, BSE/NSE/Dhan adapters, dedupe, replay
  sentiment/        regex prescreen, Claude engine
  timing/           lag decomposition, press-pickup correlation
  execution/        risk gates, order router, paper + Dhan brokers
  backtest/         bar store, replay engine, delay sweep, metrics
  pipeline.py       the live wiring
  cli.py            scan / capture / paper / live / backtest / latency-report
```

## Licence

Apache-2.0.
