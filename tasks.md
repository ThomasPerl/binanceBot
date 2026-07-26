# Trading Bot — Tasks & Roadmap

Living document. Update this whenever a milestone finishes or a new decision is made, so a fresh session (human or Claude) can pick up context fast. See [CLAUDE.md](CLAUDE.md) for architecture/commands.

## Project constraints / goals (from the project owner, 2026-07-26 planning session)

- **Capital**: 50 USDC, deliberately budgeted as loss capital. The primary goal is a working, educational bot — real profit is a bonus, not the main objective. This should temper how much engineering effort goes into squeezing out returns vs. just having something correct and safe.
- **Deployment target**: a cloud VPS (e.g. Hetzner/AWS), running fully automatically — no manual per-trade confirmation.
- **First live pair**: BTCUSDC.
- **Telegram notifications**: already implemented (`send_telegram()` in `bot.py`) — not an open item.

## Status (as of 2026-07-26)

### Done
- Fixed critical correctness bugs in `src/bot.py`: no more re-buying into an already-open position, real exchange tick/step/min-notional rounding (was hardcoded and would've been rejected by Binance), balance allocation no longer races across threads, positions persist across restarts (`src/position_state.json`), graceful shutdown, dashboard input validation. Also fixed `requirements.txt` (wrong `binance` package vs. `python-binance`, missing `python-dotenv`/`cryptography`).
- Added `DRY_RUN` / `USE_TESTNET` env vars so the bot can be exercised safely before risking real funds.
- Fixed a real `python-binance` API mismatch found during verification: `create_oco_order` needed the current `aboveType`/`belowType` params (not the old `price`/`stopPrice`/`stopLimitPrice`), and OCO status is polled via `v3_get_order_list`, not `get_oco_order`.
- Built `src/backtest.py`: historical backtester replaying all 5 strategies against all 5 configured pairs (25 combos), with fee (0.1% default) + slippage (5bps default) cost modeling, TP/SL detection via intrabar high/low, and win rate / total return / max drawdown / Sharpe / profit factor metrics. Verified: vectorized signal computation matches the live bot's windowed calls exactly (0/100 mismatches in an empirical spot-check), fee/slippage math hand-verified against actual trade output.
- Ran the 90-day/25-combo backtest twice: once against testnet data (`src/backtest_results_testnet.csv`, testnet only retained ~51-58 of the 90 days), once against real mainnet data (`src/backtest_results_mainnet.csv`, full 129,600 candles/symbol = complete 90 days). `src/backtest_results.csv` (the default path) is currently a copy of the mainnet run — the authoritative result.
- Built `src/tune.py`: parameter-sweep tuning on top of `backtest.py`. Generalized `compute_signals()`/`simulate_trades()` in `backtest.py` to accept indicator-parameter overrides and an ATR-based (volatility-adjusted) stop-sizing mode as an alternative to the flat 2%/4% SL/TP, without changing `backtest.py`'s own default output (verified via regression check against the preserved mainnet run — 0 mismatches). `tune.py` sweeps indicator params × exit sizing (3 flat + 3 ATR configs) × timeframe (1m/15m/1h) = 2,520 combos across all 5 symbols × 5 strategies, ranked by Sharpe among combos with ≥20 trades. ATR math hand-verified against a real trade's exact fill price.
- Built `src/validate.py`: out-of-sample check for the tuning sweep's 2 positive leads (SOLUSDC/MOM/1h/ATR, ETHUSDC/MOM/15m/flat). Derives a non-overlapping historical window directly from each lead's actual in-sample cache file (same length, immediately preceding it) rather than recomputing "N days before now" (avoids the date-drift trap hit earlier with `backtest.py`), and looks up in-sample metrics from `tune_results.csv` rather than recomputing them, so the comparison is exact. Result: **both leads failed out-of-sample** (see below) — caught a real flaw in the first version of the verdict logic along the way (Sharpe alone can read positive from daily-resampled-return compounding/volatility effects even when total return and profit factor are net negative; fixed to require all three to agree before calling anything "plausibly holds").
- Built `src/grid_backtest.py`: static price-band grid-trading simulator (backtest/simulation only, no live `bot.py` integration yet, per the project owner's decision to avoid investing in live order-management complexity before a strategy shows promise). Grid opens once at the start of the window (±10% band, 9 slots by default), buys on dips into a slot, sells one level up, cycles. Reuses `compute_metrics()` unchanged. Caught and fixed a real design bug during verification: slots whose buy level sits above the starting reference price were filling on the very first dip even though price never actually traded up there first (a real resting limit order above current price would just execute immediately, not sit validly) — fixed with a "price must have actually traded up to arm this level" gate, verified via a synthetic test. Also avoided repeating the date-drift cache-miss trap (see `validate.py` above) by defaulting to loading whatever's already cached rather than recomputing a relative-to-now date range.
- Built `src/dca_backtest.py`: DCA-with-safety-orders simulator (backtest/simulation only, same scoping as Grid). One active "cycle" at a time — base order, then up to 5 martingale-scaled safety orders as price drops, closed entirely on take-profit above the volume-weighted average entry. Reuses `compute_metrics()`. Verified via 6 algebraic/synthetic tests (per-cycle cash conservation, weight normalization, a zero-cost numeric fixture matched to 4 decimal places, multi-safety-order cascading, same-candle base+TP, HELD_TO_END, and a check that safety orders never fire before price has actually dipped there).

### Tuning sweep result — 2026-07-25 (mainnet, 2,520 combos)
Ran the full default sweep against mainnet data. **2,241/2,520 combos cleared the ≥20-trade filter, and the overwhelming majority of those are still net losers** — tuning parameters alone did not rescue RSI/EMA/MACD/BOLL; on the 1h timeframe especially, several combos are worse than the original 90-day backtest (down to -77%). Full results in `src/tune_results.csv`.

**One narrow bright spot**: the Momentum (ROC) strategy is the only strategy family showing any positive results, concentrated on longer timeframes (15m/1h) with default or near-default parameters and looser/ATR-based stops:
- Best combo overall: **SOLUSDC / MOM / 1h / ATR stops (14-period ATR, 2.0x SL, 4.0x TP)**, default ROC params (window=5, ±3% threshold) — +10.35% return, Sharpe **+0.44**, 79 trades, 36.7% win rate, profit factor 1.07.
- A few other MOM combos (ETHUSDC/MOM/15m, ADAUSDC/MOM/1m) show small positive Sharpe (0.2-0.37) but on thin samples (20-27 trades, right at the filter threshold).
- Every non-MOM strategy's best-of-3-per-group result is still negative or roughly breakeven at best (e.g. BNBUSDC/RSI/1h at only +1.85%/Sharpe 0.20).

**Important caveat — this is NOT yet a validated edge.** A Sharpe of 0.44 is weak by normal trading standards (many practitioners want >1 to consider a strategy live-worthy), and it's the best result out of 2,520 tested combinations on a single historical window — with that many comparisons, some noise-driven "winners" are statistically expected even if none of these strategies have a true edge (the multiple-comparisons/overfitting trap). This result has NOT been tested on a time period outside the one it was found on. **Do not treat this as a strategy to run live yet** — it's a lead worth validating out-of-sample, not a conclusion.

### Out-of-sample validation result — 2026-07-26
Ran `src/validate.py` on both positive leads, testing each on a full window of data immediately preceding (and non-overlapping with) the window that found it — same length, same symbol/interval/params. **Both leads failed.**

| Lead | In-sample (found on) | Out-of-sample (fresh data) |
|---|---|---|
| SOLUSDC/MOM/1h/ATR(2.0,4.0) | Sharpe 0.442, **+10.35%**, profit factor 1.07 | Sharpe 0.258, **-3.51%**, profit factor 0.99 |
| ETHUSDC/MOM/15m/flat(2,4) | Sharpe 0.368, **+2.76%**, profit factor 1.07 | Sharpe 0.034, **-0.90%**, profit factor 0.98 |

Both out-of-sample Sharpe ratios are nominally still positive, but that's misleading on its own — Sharpe is computed from daily-resampled returns and can stay positive on volatility/compounding effects even when the strategy lost money end-to-end (exactly what happened here: both `total_return_pct` and `profit_factor` flipped negative out-of-sample). Full numbers in `src/validation_results.csv`.

**Conclusion: the momentum leads were noise from testing 2,520 combinations, not a real edge.** Combined with the earlier finding that RSI/EMA/MACD/BOLL/MOM all lose money as originally configured, **none of the 5 existing strategies — tuned or not — have a validated edge on these 5 pairs.** The path forward is building genuinely different strategy types (Grid, DCA — see Next Steps), not further tuning of these 5.

### Grid trading backtest result — 2026-07-26 (mainnet, same 90-day/1m window as the original backtest)
Ran `src/grid_backtest.py` with default config (±10% band, 9 slots) across all 5 pairs, zero new API calls (reused the already-cached data). **All 5 pairs lost money**, but through a qualitatively different, more interpretable mechanism than the indicator strategies:

| Symbol | Return | Round trips | Held-to-end (of 9 slots) | Win rate | Broke below band by |
|---|---|---|---|---|---|
| BNBUSDC | -3.14% | 28 | 9 | 75.7% | 5.9% |
| SOLUSDC | -7.75% | 25 | 9 | 73.5% | 23.1% |
| ETHUSDC | -10.13% | 6 | 6 | 50.0% | 29.2% |
| BTCUSDC | -12.66% | 7 | 8 | 46.7% | 18.0% |
| ADAUSDC | -29.51% | 20 | 9 | 68.9% | 39.3% |

Every symbol's price broke decisively below the grid's lower bound at some point in the 90 days (5.9%-39.3% below it) — consistent with the broader declining conditions already visible in the original backtest. This is grid trading's known structural weakness: a long-only, no-stop-loss grid keeps buying dips on the way down, and when the decline doesn't fully recover, those slots sit stuck (`n_held_to_end`) with deep, uncapped losses that outweigh the many small realized gains from actual grid cycling — note BNBUSDC's 75.7% win rate on completed round trips (the grid mechanic itself works fine when price stays in-range), dragged to a net loss by held-to-end losses. **Not yet a validated edge either**, but a structurally different, more diagnosable failure mode than the indicator strategies' "mostly noise." Full results in `src/grid_backtest_results.csv`.

Untested but suggested by this result (not yet built): a stop-loss/circuit-breaker on stuck slots, dynamic grid re-centering, or testing over a more range-bound historical period — this window was adversarial to any long-only approach, not just grid.

### DCA-with-safety-orders backtest result — 2026-07-26 (mainnet, same 90-day/1m window)
Ran `src/dca_backtest.py` with default config (5 max safety orders, 2% first deviation, 1.5x step/volume scale, 2.5% take-profit) across all 5 pairs, zero new API calls. **All 5 pairs lost money again**, with the clearest, most textbook failure mode of the three approaches tried so far:

| Symbol | Return | Cycles (TP / held-to-end) | Win rate | Avg win | Avg loss |
|---|---|---|---|---|---|
| BTCUSDC | -6.48% | 3 (2/1) | 66.7% | +2.35% | -6.85% |
| BNBUSDC | -7.21% | 10 (9/1) | 90.0% | +2.35% | -9.15% |
| ETHUSDC | -7.92% | 3 (2/1) | 66.7% | +2.35% | -8.51% |
| SOLUSDC | -8.65% | 7 (6/1) | 85.7% | +2.35% | -9.80% |
| ADAUSDC | -29.31% | 8 (7/1) | 87.5% | +2.35% | -30.69% |

Win rates are high (67-90%!) and every take-profit win is a near-identical **+2.35%** (exactly the cost-inclusive take-profit formula, confirming the mechanic works precisely as designed) — but every symbol has exactly **one** held-to-end cycle, maxed out on all 5 safety orders (the full ~24% worst-case cumulative drop), stuck at data-end without recovering. That single tail loss per symbol is 3-13x the size of a typical win and wipes out several cycles' worth of gains by itself — the textbook risk of martingale-style position sizing: it wins often and small, then loses rarely but big, in exactly the scenario (a sustained decline that doesn't recover in time) this 90-day window kept producing for every strategy type tried. Full results in `src/dca_backtest_results.csv`.

**Pattern across all three approaches (indicator strategies, Grid, DCA)**: this specific 90-day window was broadly declining across all 5 pairs, which structurally disadvantages any long-only, buy-the-dip mechanism regardless of its sophistication. Worth remembering when interpreting any future result on this same window — and a strong argument for testing whatever comes next on a different/longer historical period, not just this one.

### Key finding — confirmed on both testnet (2026-07-24) and mainnet (2026-07-25)
**Every single one of the 25 symbol × strategy combinations lost money after fees/slippage, on both datasets.** This is now a decisively confirmed result, not just a testnet artifact — the mainnet run had much larger sample sizes (most combos 50-150 trades vs. testnet's 2-3) and still showed uniform losses (-2% to -66% over 90 days).

Notably, the *specific* ranking of "least bad" / "worst" combo was **not stable** between the two datasets (e.g. BTCUSDC/RSI was the single worst combo on testnet at -50.7%, but mid-pack on mainnet at -24.7%; ADAUSDC looked fine on testnet but was the worst symbol on mainnet, -53% to -62% across strategies). That instability is itself informative — a real edge tends to hold up reasonably consistently across overlapping windows; a combo flipping from best to worst depending on which historical slice you use looks like noise, not signal.

Currently-live assignments on mainnet: BTCUSDC/RSI -24.7%, ETHUSDC/EMA -33.0%, BNBUSDC/MACD -23.2% (best multi-trade combo, still a loser), SOLUSDC/BOLL -29.0%, ADAUSDC/MOM -2.3% (only 1 trade, not meaningful). Across nearly every combo: average win (~+3.5%) is bigger than average loss (~-2.3%), but win rates are so low (mostly 28-34%) that expectancy is negative anyway — these single-indicator signals on 1-minute candles look like mostly noise, not a real edge.

**Conclusion: none of the 5 existing strategies, as currently configured, should be run with real capital.**

## Next steps (not started yet, in priority order)

Decision from the 2026-07-26 planning session, updated after out-of-sample validation failed both momentum leads and BOTH Grid and DCA backtests came back net-negative: all three approaches tried so far (5 tuned indicator strategies, Grid, DCA) fail on this 90-day window, and now there's a clear, recurring pattern — every one of them is a long-only, buy-the-dip mechanism, and this specific window was broadly declining across all 5 pairs. That's a strong signal the window itself, not just the strategies, deserves scrutiny before trying yet another variant.

1. **Test on a different/longer historical window before building anything else.** All three failures share the same root cause candidate: a declining 90-day period disadvantages any long-only approach structurally, independent of sophistication. Before inventing a 4th strategy type, re-run Grid and/or DCA (cheapest to re-run, no new code needed — just point `--days`/`--refresh-cache` at a different period, or fetch a longer/older window the way `validate.py` did for the momentum leads) over a period that wasn't a sustained decline, to separate "these mechanisms don't work" from "nothing long-only works in a bear window."

2. **If a more favorable window changes the picture**: that result still needs the same out-of-sample validation treatment the momentum leads got (`src/validate.py`'s pattern — its `TARGETS` list currently assumes the single-signal `simulate_trades()` shape, so validating a Grid/DCA lead needs either a small adaptation or a parallel script following the same "derive window from the actual cache file, don't recompute relative to now" principle) before being trusted.

3. **If it doesn't change the picture**: worth trying a stop-loss/circuit-breaker on stuck Grid slots or DCA cycles (neither has one currently, by design, specifically to see this raw risk directly — now seen clearly in both), or accepting that a long-only mechanism isn't viable for this bot's goals and considering something structurally different (e.g. a short-capable or market-neutral approach) — a bigger conversation, not a parameter tweak.

4. **Only once a strategy — existing or new — shows a validated, out-of-sample, non-random edge**: run it live via `USE_TESTNET=true` for a real-order trial (not just a backtest simulation), then move to small real capital on BTCUSDC (the designated first live pair), fully automated.

5. **Prepare VPS deployment** (not yet in the repo): a systemd service for continuous operation with automatic restart after reboot/crash; a safe way to get `.env` onto the server without committing it to the repo; and basic monitoring/log rotation so `trade_log.csv` and friends don't grow unbounded.

## Outstanding housekeeping
- ~~**Security**: two real Binance API keys leaked in the chat transcript on 2026-07-24.~~ **Resolved 2026-07-26** — confirmed rotated by the project owner.
- `bot.py` is currently configured with `DRY_RUN=true`, `USE_TESTNET=true` — safe, simulates trades against testnet market data only.

## Reference
- Architecture, setup, and commands: [CLAUDE.md](CLAUDE.md)
- Backtest usage: `python backtest.py --help` (in `src/`)
- Tuning sweep usage: `python tune.py --help` (in `src/`)
- Latest backtest output: `src/backtest_results.csv` (= mainnet run). Testnet and mainnet runs also preserved separately as `src/backtest_results_testnet.csv` / `src/backtest_results_mainnet.csv` for comparison.
- Latest tuning sweep output: `src/tune_results.csv` (2,520 rows, one per tested combo — not just winners)
- Out-of-sample validation: `python validate.py` (in `src/`, edit the `TARGETS` list to add more leads). Latest output: `src/validation_results.csv`
- Grid trading backtest: `python grid_backtest.py --help` (in `src/`). Latest output: `src/grid_backtest_results.csv`
- DCA-with-safety-orders backtest: `python dca_backtest.py --help` (in `src/`). Latest output: `src/dca_backtest_results.csv`
