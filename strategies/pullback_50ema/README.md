# 4H 50-EMA Pullback — Formalised Ruleset

Executable specification of the pullback/entry rules, the intraday RVOL execution
filter and the Chandelier exit. Stdlib-only Python; no pandas, no numpy, no network.

```
strategies/pullback_50ema/
  strategy.py        indicators + the three gates
  test_strategy.py   29 unit tests (unittest)
  demo.py            synthetic end-to-end walkthrough
```

Run:

```bash
cd strategies/pullback_50ema
python3 -m unittest test_strategy -v
python3 demo.py
```

---

## 1. Rule → code mapping

| # | Source rule | Formalisation | Config knob | Code |
|---|---|---|---|---|
| 1 | Uptrend above / downtrend below the 4H 50 EMA | `close` on one side of EMA50 for N consecutive bars | `trend_confirm_bars = 3` | `scan_4h` run tracking |
| 1b | "Initial separation" | peak `abs(close − EMA) / ATR` during the run ≥ threshold | `min_separation_atr = 1.0`, `atr_period = 14` | `run_max_sep` |
| 2 | Pullback toward the EMA | ≥2 **consecutive** counter-trend candles (red in an uptrend, green in a downtrend); a doji ends the sequence | `min_pullback_bars = 2` | `Candle.is_counter_trend` |
| 2b | Cancellation: "gone through the 50 EMA" | candle **body** entirely on the far side of the EMA → `CANCELLED_EMA_SLICE`. A close through the EMA that does not fully slice → `CANCELLED_TREND_FLIP` | — | `_slices_ema` |
| 2c | "Must respect / bounce from the 50 EMA zone" | wick may pierce the EMA; optionally require the pullback to reach within `k × ATR` of it | `require_ema_touch = False`, `ema_zone_atr = 0.5` | `_touches_ema_zone` |
| 3 | Entry 1 pip beyond the wick of the **first** counter-trend bar | LONG: `first_pullback.high + tick`; SHORT: `first_pullback.low − tick` | `tick_size`, `entry_anchor = "first"` | `_arm` |
| 3b | "…or the lowest wick of that pullback sequence" | alternative anchor over the whole sequence | `entry_anchor = "extreme"` | `_arm` |
| 4 | Daily cumulative RVOL ≥ 9% within the first 10 minutes | Σ volume of bars opening in `[open, open+10m)` ÷ 20-day ADV ≥ 0.09 | `threshold = 0.09`, `window_minutes = 10` | `evaluate_rvol_gate` |
| 5 | Hold until an intraday candle closes below the Chandelier Exit | LONG: `HH(22) − 3×ATR(22)`, exit on `close < line`; SHORT mirrored | `period = 22`, `multiplier = 3.0`, `ratchet = True` | `find_chandelier_exit` |

### Setup lifecycle

```
        confirmed trend + separation
                    │
        first counter-trend candle
                    │
              [pullback open]  ──► body slices the EMA ──► CANCELLED_EMA_SLICE
                    │            ──► close flips sides   ──► CANCELLED_TREND_FLIP
                    │            ──► > max_pullback_bars ──► EXPIRED_PULLBACK_TOO_LONG
          ≥2 counter-trend bars
                    │
                 [ARMED]  ── resting stop order at anchor ± 1 tick
                    │
        price trades through the level      no breakout within trigger_valid_bars
                    │                                      │
              TRIGGERED                          EXPIRED_NO_BREAKOUT
```

The trigger is checked **before** the bar is classified, because a resting stop
order fills intrabar — the bar that ends the pullback can be the bar that fills.

---

## 2. Resolved ambiguities

The source rules are underspecified in seven places. Each was closed with an
explicit, overridable assumption rather than left implicit.

| Ambiguity | Assumption taken | Override |
|---|---|---|
| "Slice completely through the EMA" — wick or body? | **Body**. A wick through the EMA is the bounce the setup wants; a body fully through is the cancel. | `_slices_ema` |
| "Establish a trend" — how many bars? | 3 consecutive closes on one side of EMA50. | `trend_confirm_bars` |
| "Initial separation" is unquantified | ≥1.0 × ATR(14) peak distance from the EMA during the run. Without this, any bar hugging the EMA qualifies. | `min_separation_atr` |
| Entry anchor: "first counter-trend bar" **or** "lowest wick of the sequence" | Default to **first** (the primary instruction); `extreme` available. In practice they coincide, since the first pullback bar usually prints the sequence extreme. | `entry_anchor` |
| "1 pip" is an FX unit; RVOL/market-open/Chandelier are equity concepts | `tick_size` is explicit. Presets: `TICK_FX_5DP` (0.0001), `TICK_FX_JPY` (0.01), `TICK_NSE_EQUITY` (0.05). | `tick_size` |
| "RVOL ≥ 9%" — ratio or share? | Share of **20-day average daily volume**, not a classic RVOL ratio (a ratio would be expressed as ×, not %). | `threshold`, `average_daily_volume(lookback=…)` |
| Chandelier parameters unstated | Chande's defaults: 22-period, 3× ATR, ratcheting. Exit on **close** through the line, per "an intraday candle closes below". | `period`, `multiplier`, `ratchet` |

---

## 3. Gaps in the source rules — decide these before risking capital

1. **Direction contradiction.** The worked example is a **short**; the combined
   execution section says "Enter **Long** on the RVOL breach". Both directions are
   implemented symmetrically, but the intended live direction is unresolved. If
   the strategy is equities-only (RVOL, market open, swing hold), shorts may not
   be available at all — in which case Stage 1 should be restricted to LONG.
2. **No initial stop is defined.** The Chandelier is a *trailing* exit and needs
   ~22 bars before it prints; it does not protect the first minutes of the trade.
   `Setup.protective_stop` supplies a placeholder (pullback extreme ± 1 tick) but
   it is an addition, not part of the source ruleset.
3. **No position sizing / risk-per-trade rule.** Nothing here sizes a position.
4. **Timeframe mismatch is unhandled.** The setup lives on a 4H chart; the RVOL
   gate lives on a specific session open. Which trading day counts as "the
   breakout day" when the 4H trigger fires mid-session, or overnight, is not
   specified. `build_trade_plan` takes the session open as an explicit argument
   rather than guessing.
5. **No trigger-level expiry in the source.** Defaulted to 3 bars
   (`trigger_valid_bars`); a stale pending order is otherwise unbounded.
6. **RVOL gate is a filter, not a signal.** "Enter on the RVOL breach" is
   implemented as: the 4H level must be taken out *and* the gate must pass. The
   fill price used is the 4H trigger level, not the price at the breach minute.
7. **Unvalidated.** Every number here is a default, not a backtested parameter.
   Nothing in this module has been run against real market data.

---

## 4. Usage

```python
from strategy import (
    Candle, Direction, StrategyConfig, TICK_NSE_EQUITY,
    scan_4h, average_daily_volume, evaluate_rvol_gate, find_chandelier_exit,
    build_trade_plan,
)

cfg = StrategyConfig(tick_size=TICK_NSE_EQUITY)

# Stage 1-3
for setup in scan_4h(h4_candles, cfg):
    print(setup.direction, setup.status, setup.entry_price, setup.protective_stop)

# Stage 4
adv = average_daily_volume(daily_candles, lookback=20)
gate = evaluate_rvol_gate(minute_candles, session_open, adv, threshold=0.09, window_minutes=10)

# Stage 5
exit_signal = find_chandelier_exit(minute_candles, Direction.LONG, entry_index=gate_index)

# All three at once
plan = build_trade_plan(h4_candles, minute_candles, session_open, adv, cfg)
plan.executable  # triggered setup AND RVOL breach
```

`Candle` is `(ts, open, high, low, close, volume)`; timestamps are treated as the
bar's **open** time. Bring your own data source — this module never fetches.
