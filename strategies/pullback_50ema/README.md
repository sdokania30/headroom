# 4H 50-EMA Pullback → RVOL → 10-Min HOD — Long Only

Two deliverables from one ruleset:

| Path | What it is |
|---|---|
| `pine/rvol_hod_swing_long.pine` | **The tradeable strategy.** Pine Script v6, long only, for a 5-minute chart. |
| `strategy.py` | Reference implementation of the same logic in Python — the executable spec the Pine was derived from. |
| `test_strategy.py` | 32 unit tests of the reference implementation. |
| `test_pine_parity.py` | 7 tests asserting the Pine state machine and the Python reference fire identically. |
| `demo.py` | Synthetic end-to-end walkthrough. |

```bash
cd strategies/pullback_50ema
python3 -m unittest test_strategy test_pine_parity -v
python3 demo.py
```

---

## 1. The Pine strategy

Apply to a **5-minute chart**. Long only. Positions are held as swings, across days.

### Entry — all three, inside the first 10 minutes of the session

1. **4H setup live** (`useH4`, default on) — the 4H 50-EMA pullback is armed or has broken out within the last 6 bars.
2. **RVOL gate** — cumulative session volume ≥ 9% of the 20-day ADV, latched on a completed bar inside the window.
3. **10-min HOD** — a buy-stop 1 tick above the running high of day fills.

### The consequence of a strict 10-minute window

On a 5-minute chart the window holds exactly two bars:

| Bar | Clock | What happens |
|---|---|---|
| 1 | 09:15–09:20 | Volume accumulates. If RVOL ≥ 9% at this close, the buy-stop is placed at bar 1's high + 1 tick. |
| 2 | 09:20–09:25 | The order is live. A break of bar 1's high fills it. |
| 3 | 09:25+ | Window shut. Any working order is cancelled. |

So **the RVOL must breach on bar 1.** A breach at bar 2's close leaves no bar inside the window for a fill, and the day is skipped. That is the strict reading of "RVOL within 10 minutes" *and* "entry within 10 min HOD" together — the order is only ever placed while the next bar still opens inside the window (`orderMayWork`).

If that proves too selective in backtest, the lever is `orMinutes`. At 15 you get two fillable bars, at 30 you get five. Nothing else needs to change.

### Exit

- **Chandelier Exit**, `HH(22) − 3 × ATR(22)`, exit on an intraday **close** below the line. Ratcheted by default.
- **Initial ATR stop** at `entry − 1.5 × ATR`, intrabar, until the trail is meaningful.

### Key inputs

| Input | Default | Note |
|---|---|---|
| `sessTime` | `0915-1530` | NSE cash. Chart timezone must match. |
| `orMinutes` | `10` | Entry window length. The main sensitivity knob. |
| `rvolPct` | `9.0` | Share of 20-day ADV. |
| `useH4` | `on` | Toggle the 4H filter to measure what it is worth. |
| `chandTF` | `D` | **See below.** Empty string = chart timeframe. |
| `eodExit` | `off` | This is a swing strategy. |

### Why the Chandelier defaults to the daily timeframe

`HH(22) − 3×ATR(22)` on a 5-minute chart is a 110-minute lookback. That is a day-trade stop — it will flush almost every position the same session and the "swing" never happens. The source rule says *"hold the swing position until an intraday candle closes below the Chandelier Exit"*, so the line is computed on the **daily** timeframe and tested against **every intraday close**. That is my reading, not something the source states. Set `chandTF` to `""` to get the literal chart-timeframe version and compare.

### Repainting

Every higher-timeframe read uses `barmerge.lookahead_off`. The 4H state machine runs at chart scope and advances only when a 4H bar has **closed**, using that closed bar's OHLC. The ADV uses `ta.sma(volume, 20)[1]`, excluding today's incomplete daily bar. Nothing reads a value that was not available at the time.

---

## 2. Rule → code mapping

| # | Source rule | Formalisation | Config knob |
|---|---|---|---|
| 1 | Uptrend above the 4H 50 EMA | `close` above EMA50 for N consecutive bars | `trendBars = 3` |
| 1b | "Initial separation" | peak `abs(close − EMA) / ATR` during the run ≥ threshold | `sepATR = 1.0`, `atrLenH4 = 14` |
| 2 | Pullback toward the EMA | ≥2 **consecutive** red candles; a doji ends the sequence | `minPB = 2` |
| 2b | Cancellation: "gone through the 50 EMA" | candle **body** entirely below the EMA, or a close below it | — |
| 2c | "Respect / bounce from the 50 EMA zone" | wicks may pierce the EMA freely | — |
| 3 | Entry 1 pip above the first pullback bar's wick | `first_pullback.high + syminfo.mintick` | — |
| 4 | Daily cumulative RVOL ≥ 9% in the first 10 minutes | Σ session volume ÷ 20-day ADV ≥ 0.09, latched inside the window | `rvolPct`, `advLen`, `orMinutes` |
| 4b | Entry within the 10-min HOD | buy-stop 1 tick above the running HOD, working only while the next bar opens inside the window | `orMinutes` |
| 5 | Hold until an intraday candle closes below the Chandelier Exit | `HH(22) − 3×ATR(22)` on the daily, tested on intraday closes | `chandTF`, `chandLen`, `chandMult` |

### 4H setup lifecycle

```
        confirmed uptrend + >=1 ATR separation
                        │
             first red (counter-trend) candle
                        │
                  [pullback open] ──► body below the EMA   ──► cancelled
                        │           ──► close below the EMA ──► cancelled
                        │           ──► > maxPB red bars    ──► cancelled
                  >=2 red bars
                        │
                    [ARMED]  ── buy-stop at first red bar's high + 1 tick
                        │
        high >= trigger                  no breakout within validBars
                        │                             │
               setup READY for                    cancelled
             `validBars` more bars
```

---

## 3. Resolved ambiguities

The source rules were underspecified in eight places. Each is closed with an explicit, overridable choice.

| Ambiguity | Choice taken | Override |
|---|---|---|
| Long or short? The worked example was a short, the execution section said long | **Long only.** Confirmed by you. The Python reference defaults to `long_only=True`; set `False` to scan shorts. | `StrategyConfig.long_only` |
| "Slice completely through the EMA" — wick or body? | **Body.** A wick through the EMA is the bounce the setup wants; a body fully through cancels. | — |
| "Establish a trend" — how many bars? | 3 consecutive closes above the EMA. | `trendBars` |
| "Initial separation" is unquantified | ≥1.0 × ATR(14) peak distance. Without it, any bar hugging the EMA qualifies. | `sepATR` |
| "1 pip" | `syminfo.mintick` in Pine. Python takes an explicit `tick_size`, presets `TICK_FX_5DP`, `TICK_FX_JPY`, `TICK_NSE_EQUITY`. | — |
| "RVOL ≥ 9%" — ratio or share? | Share of **20-day ADV**. A ratio would be written as ×, not %. | `rvolPct`, `advLen` |
| Chandelier parameters and timeframe unstated | Chande's defaults (22, 3×, ratcheting), computed on the **daily**, tested on intraday closes. | `chandTF`, `chandLen`, `chandMult`, `ratchet` |
| No initial stop in the source ruleset | Added: `entry − 1.5 × ATR`, intrabar. The Chandelier is a trailing exit and does not protect the open of the trade. | `useInitStop`, `initATR` |

---

## 4. What is still open

1. **Not compiled on TradingView.** The Pine has never been through the TV compiler — no TV access from this environment. The *logic* is verified: `test_pine_parity.py` transliterates the Pine state machine line-for-line and asserts it fires identical breakouts to the tested Python reference across 60 random walks of 400 bars (579 matching breakouts) plus six hand-built edge cases. Syntax is the remaining risk, and it is a paste-and-see.
2. **Nothing is backtested.** Every number is a default, not a fitted parameter. No walk-forward, no parameter sweep, no out-of-sample split.
3. **Position sizing is 100% of equity per trade.** Change `default_qty_type` / `default_qty_value` before this means anything about returns.
4. **Commission 0.03% and 2-tick slippage** are placeholders. Set them to your actual costs — an opening-range strategy is slippage-sensitive and these assumptions move the equity curve materially.
5. **`orMinutes = 10` is severe on a 5-min chart** (see §1). Backtest 10 / 15 / 30 before concluding the edge is absent.
6. **Gap-up days are not handled specially.** If the stock gaps and bar 1 is a large range, the buy-stop sits above an already-extended high. Consider a max-extension filter.
7. **The 4H bar boundary on NSE is awkward.** A 6h15m session does not divide into 4H bars cleanly. Verify what TradingView actually builds for your symbol before trusting `h4TF = 240`.

---

## 5. Python reference usage

```python
from strategy import Candle, StrategyConfig, TICK_NSE_EQUITY, scan_4h, build_trade_plan

cfg = StrategyConfig(tick_size=TICK_NSE_EQUITY)   # long_only=True by default

for setup in scan_4h(h4_candles, cfg):
    print(setup.direction, setup.status, setup.entry_price, setup.protective_stop)

plan = build_trade_plan(h4_candles, minute_candles, session_open, adv, cfg)
plan.executable   # triggered setup AND RVOL breach
```

`Candle` is `(ts, open, high, low, close, volume)`; timestamps are the bar's **open** time. Bring your own data — this module never fetches.
