# 4H 50-EMA Pullback → RVOL → 5-Min ORB — Long Only

Two deliverables from one ruleset:

| Path | What it is |
|---|---|
| `pine/rvol_hod_swing_long.pine` | **The tradeable strategy.** Pine Script v6, long only, for a 5-minute chart. |
| `strategy.py` | Reference implementation of the same logic in Python — the executable spec the Pine was derived from. |
| `backtest.py` | **Walk-forward backtester + CLI.** Trade blotter, P&L, performance summary, signal funnel. |
| `test_strategy.py` | 46 unit tests of the reference implementation. |
| `test_backtest.py` | 32 tests of the backtest engine, the sweep and the filters. |
| `test_pine_parity.py` | 11 tests asserting three implementations of the 4H filter agree. |
| `demo.py` | Synthetic end-to-end walkthrough. |

```bash
cd strategies/pullback_50ema
python3 -m unittest test_strategy test_pine_parity test_backtest -v
python3 backtest.py --demo
```

---

## 1. The Pine strategy

Apply to a **5-minute chart**. Long only. Positions are held as swings, across days.

### Entry — all three, inside the first 15 minutes of the session

1. **4H setup live** (`useH4`, default on) — the 4H 50-EMA pullback is armed or has broken out within the last 6 bars.
2. **RVOL gate** — cumulative session volume ≥ 9% of the 20-day ADV, latched on a bar that has already **closed**.
3. **5-min ORB** — a buy-stop 1 tick above the **opening-range high** (the first 5 minutes) fills.

### The two windows

They are separate inputs and do different jobs:

| Input | Default | Job |
|---|---|---|
| `orMinutes` | 5 | Defines the breakout **level**: 1 tick above the high of the first 5 minutes. |
| `entryWindow` | 15 | Defines how long the order may **work**. |
| `rvolMinutes` | 15 | Defines how long the RVOL gate may **latch**. Set to 10 for the original "RVOL within 10 minutes" rule. |

On a 5-minute chart that lays out as:

| Bar | Clock | What happens |
|---|---|---|
| 1 | 09:15–09:20 | Sets the opening range. Volume accumulates. If RVOL ≥ 9% at this close, the buy-stop goes in at OR high + 1 tick. |
| 2 | 09:20–09:25 | Order live — fills on a break of the OR high. If RVOL only reaches 9% at *this* close, the order goes in now instead. |
| 3 | 09:25–09:30 | Last fillable bar. |
| 4 | 09:30+ | Window shut, any working order cancelled, day done. |

So you get **two chances to fill**, and the RVOL has until bar 2's close to latch. That is the practical difference from a 10-minute window, which allowed one fill attempt and demanded the RVOL latch on bar 1.

An order can never fill on the bar that placed it — it is a resting stop, and it only becomes live on the following bar. The code enforces this on both sides (`orderMayWork` in Pine, the latch-before-fill ordering in `find_orb_entry`).

`entryWindow` must be greater than `orMinutes` or nothing can ever fill; the status table flags `BAD CONFIG` if you set it that way.

### Backtest signals on the chart

Turn on **Backtest visuals** in the settings (all on by default):

- **BUY label** below the entry bar with the fill price.
- **SELL label** above the exit bar with the exit price, the **profit booked** in currency and %, and the exit reason (`Chandelier`, `Init stop`, `EOD`).
- **Dashed connector** from entry to exit, green for a win, red for a loss.
- **Performance table** (bottom right): net profit, return on capital, closed trades, win rate, profit factor, avg win / avg loss, max drawdown, open P&L.
- **Trade blotter** (bottom left): the last N closed trades — entry, exit, P&L, and why it closed.

TradingView's own Strategy Tester panel gives the full report; these read the same `strategy.closedtrades.*` values and put them on the chart so you can eyeball each signal against the bars that produced it.

### Exit

- **Chandelier Exit**, `HH(22) − 3 × ATR(22)`, exit on an intraday **close** below the line. Ratcheted by default.
- **Initial ATR stop** at `entry − 1.5 × ATR`, intrabar, until the trail is meaningful.

### Key inputs

| Input | Default | Note |
|---|---|---|
| `sessTime` | `0915-1530` | NSE cash. Chart timezone must match. |
| `orMinutes` | `5` | Opening range — sets the breakout level. |
| `entryWindow` | `15` | How long the order may work. The main sensitivity knob. |
| `rvolMinutes` | `15` | How long the RVOL gate may latch. |
| `rvolPct` | `9.0` | Share of 20-day ADV. |
| `useH4` | `on` | Toggle the 4H filter to measure what it is worth. |
| `maxOrAtr` | `0` (off) | Skip days whose opening range is wider than N × daily ATR — the gap-up guard. |
| `chandTF` | `D` | **See below.** Empty string = chart timeframe. |
| `eodExit` | `off` | This is a swing strategy. |

### Why the Chandelier defaults to the daily timeframe

`HH(22) − 3×ATR(22)` on a 5-minute chart is a 110-minute lookback. That is a day-trade stop — it will flush almost every position the same session and the "swing" never happens. The source rule says *"hold the swing position until an intraday candle closes below the Chandelier Exit"*, so the line is computed on the **daily** timeframe and tested against **every intraday close**. That is my reading, not something the source states. Set `chandTF` to `""` to get the literal chart-timeframe version and compare.

### Repainting

Every higher-timeframe read uses `barmerge.lookahead_off`. The 4H state machine runs at chart scope and advances only when a 4H bar has **closed**, using that closed bar's OHLC. The ADV uses `ta.sma(volume, 20)[1]`, excluding today's incomplete daily bar. Nothing reads a value that was not available at the time.

---

## 2. Backtesting offline

`backtest.py` runs the same rules bar for bar without TradingView, so you can sweep parameters, diff configurations and get a signal-by-signal CSV.

```bash
python3 backtest.py --demo                      # synthetic data, no input needed
python3 backtest.py --csv bars_5m.csv           # your own 5-minute OHLCV
python3 backtest.py --csv bars_5m.csv --out trades.csv --window 30 --no-h4
```

**Input CSV**: header row, then `timestamp,open,high,low,close,volume`. Timestamps ISO-8601 (`2026-01-05 09:15:00`) or epoch seconds, in exchange local time, 5-minute bars.

**Flags**: `--or` `--window` `--rvol-window` `--rvol` `--no-h4` `--max-or-atr` `--capital` `--tick` `--commission` `--slippage` `--eod-exit` `--out` `--list`.

**Output** — three blocks:

1. **Blotter**: every trade as BUY time/price → SELL time/price, quantity, net P&L in currency and %, bars held, worst adverse excursion, and the exit reason.
2. **Performance**: net profit, return on capital, gross profit/loss, costs paid, profit factor, win rate, avg win / avg loss, expectancy, best/worst, max drawdown.
3. **Signal funnel**: sessions tested → days with no RVOL → days blocked by the 4H filter → days with too wide an open → days armed but never filled → days filled. This is the block to read first. It tells you *where* the strategy is losing its opportunities, which matters more than the P&L when you are tuning.

`--out` writes two rows per trade (BUY and SELL) so the file drops straight into a spreadsheet.

### Parameter sweep

```bash
python3 backtest.py --csv bars_5m.csv --sweep
python3 backtest.py --csv bars_5m.csv --sweep --sweep-window 10,15,30,60 --sweep-rvol 5,7,9,12 --sweep-h4 both
```

One row per combination of opening range × entry window × RVOL threshold × 4H filter, with trades, win rate, net P&L, return, profit factor, expectancy, max drawdown, days filled and days rejected by RVOL. Combinations where the entry window is not longer than the opening range can never fill and are skipped rather than reported as empty rows.

Two things to hold onto when reading the grid:

- **Trade count before P&L.** A cell with three trades is noise however good its profit factor looks.
- **Plateau, not peak.** The single best cell in any grid is the one most likely to be overfit. What you want is a region where the neighbours are also decent — that is a parameter you can trust out of sample.

The funnel column is often the most informative part. On synthetic data, widening the window from 10 to 30 minutes drops "days with no RVOL" from 124/160 to 7/160 — the threshold is not really measuring unusual volume at 10 minutes so much as measuring how much of the day has elapsed.

### The gap-up guard

`--max-or-atr` (Pine: `maxOrAtr`) skips a day when `orHigh − orLow` exceeds N × daily ATR. On a gap-up the first 5-minute bar can be enormous, which puts the buy-stop above an already-extended high and the 1.5×ATR initial stop uncomfortably far below it. Off by default; try `1.0`–`2.0` and compare.

### What the engine models

| | |
|---|---|
| Fills | Resting buy-stop, filled at the level or the bar open if it gapped through — whichever is worse |
| Slippage | 2 ticks per side (`--slippage`) |
| Commission | 0.03% per side (`--commission`) |
| Sizing | Whole shares, full equity, compounding |
| Position carry | Swings are held across sessions; a new entry cannot open while one is live |
| Chandelier | Daily `HH(22) − 3×ATR(22)` from completed sessions plus today's running high, tested on every 5-min close |
| 4H filter | State advances only on closed 4H bars, anchored to the session open |
| ADV | Trailing 20 completed sessions, never including today |

Nothing reads a value that was not available at that moment. The one place it differs from Pine is intrabar sequencing: Pine's broker emulator makes its own assumption about whether the high or the low came first, so a bar that touches both the entry stop and the initial stop may be resolved differently. Expect the two to agree closely but not exactly.

---

## 3. Rule → code mapping

| # | Source rule | Formalisation | Config knob |
|---|---|---|---|
| 1 | Uptrend above the 4H 50 EMA | `close` above EMA50 for N consecutive bars | `trendBars = 3` |
| 1b | "Initial separation" | peak `abs(close − EMA) / ATR` during the run ≥ threshold | `sepATR = 1.0`, `atrLenH4 = 14` |
| 2 | Pullback toward the EMA | ≥2 **consecutive** red candles; a doji ends the sequence | `minPB = 2` |
| 2b | Cancellation: "gone through the 50 EMA" | candle **body** entirely below the EMA, or a close below it | — |
| 2c | "Respect / bounce from the 50 EMA zone" | wicks may pierce the EMA freely | — |
| 3 | Entry 1 pip above the first pullback bar's wick | `first_pullback.high + syminfo.mintick` | — |
| 4 | Daily cumulative RVOL ≥ 9% early in the session | Σ session volume ÷ 20-day ADV ≥ 0.09, latched on a closed bar inside `rvolMinutes` | `rvolPct`, `advLen`, `rvolMinutes` |
| 4b | Entry on the opening-range breakout | buy-stop 1 tick above the 5-minute opening-range high, working only on bars opening in `[orMinutes, entryWindow)` | `orMinutes`, `entryWindow` |
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

## 4. Resolved ambiguities

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

## 5. What is still open

1. **Not compiled on TradingView.** The Pine has never been through the TV compiler — no TV access from this environment. The *logic* is verified: `test_pine_parity.py` transliterates the Pine state machine line-for-line and asserts it fires identical breakouts to the tested Python reference across 60 random walks of 400 bars (579 matching breakouts) plus six hand-built edge cases, and does the same for the intraday entry across six window configurations and 720 synthetic sessions (88 matching fills). Syntax is the remaining risk, and it is a paste-and-see.
2. **Nothing is backtested on real data.** `backtest.py --demo` runs on a synthetic random walk — the machinery works, the numbers are noise. Point it at real bars before drawing any conclusion. No parameter sweep, no out-of-sample split, no multi-symbol test.
3. **Position sizing is 100% of equity per trade.** Change `default_qty_type` / `default_qty_value` before this means anything about returns.
4. **Commission 0.03% and 2-tick slippage** are placeholders. Set them to your actual costs — an opening-range strategy is slippage-sensitive and these assumptions move the equity curve materially.
5. **The window is still tight.** Two fill attempts per day. Backtest `entryWindow` at 15 / 30 / 60 before concluding the edge is absent.
6. **The gap-up guard is off by default.** `maxOrAtr` / `--max-or-atr` exists and is tested, but no value is chosen for you — it needs real data to calibrate.
7. **The 4H bar boundary on NSE is awkward.** A 6h15m session does not divide into 4H bars cleanly. Verify what TradingView actually builds for your symbol before trusting `h4TF = 240`.

---

## 6. Python reference usage

```python
# Parameter sweep
from backtest import BacktestConfig, format_sweep, load_csv, run_sweep
from strategy import StrategyConfig

rows = run_sweep(load_csv("bars_5m.csv"), BacktestConfig(), StrategyConfig(),
                 or_list=[5], window_list=[10, 15, 30, 60],
                 rvol_list=[5, 7, 9, 12], h4_list=[True, False])
print(format_sweep(rows))

# Programmatic backtest
from backtest import BacktestConfig, load_csv, run_backtest, summarise, format_blotter

result = run_backtest(load_csv("bars_5m.csv"), BacktestConfig(entry_window_minutes=30))
print(format_blotter(result))
print(summarise(result).profit_factor)

# Single-day reference path
from strategy import Candle, StrategyConfig, TICK_NSE_EQUITY, scan_4h, build_trade_plan

cfg = StrategyConfig(tick_size=TICK_NSE_EQUITY)   # long_only=True by default

for setup in scan_4h(h4_candles, cfg):
    print(setup.direction, setup.status, setup.entry_price, setup.protective_stop)

plan = build_trade_plan(h4_candles, minute_candles, session_open, adv, cfg,
                        or_minutes=5, entry_window_minutes=15, rvol_window_minutes=15)
plan.entry        # IntradayEntry: filled, level, fill_ts, opening_range, rvol, reason
plan.executable   # triggered 4H setup AND the ORB entry actually filled
```

`Candle` is `(ts, open, high, low, close, volume)`; timestamps are the bar's **open** time. Bring your own data — this module never fetches.
