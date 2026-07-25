# Trading Bot — Tasks & Roadmap

Living document. Update this whenever a milestone finishes or a new decision is made, so a fresh session (human or Claude) can pick up context fast. See [CLAUDE.md](CLAUDE.md) for architecture/commands.

## Status (as of 2026-07-25)

### Done
- Fixed critical correctness bugs in `src/bot.py`: no more re-buying into an already-open position, real exchange tick/step/min-notional rounding (was hardcoded and would've been rejected by Binance), balance allocation no longer races across threads, positions persist across restarts (`src/position_state.json`), graceful shutdown, dashboard input validation. Also fixed `requirements.txt` (wrong `binance` package vs. `python-binance`, missing `python-dotenv`/`cryptography`).
- Added `DRY_RUN` / `USE_TESTNET` env vars so the bot can be exercised safely before risking real funds.
- Fixed a real `python-binance` API mismatch found during verification: `create_oco_order` needed the current `aboveType`/`belowType` params (not the old `price`/`stopPrice`/`stopLimitPrice`), and OCO status is polled via `v3_get_order_list`, not `get_oco_order`.
- Built `src/backtest.py`: historical backtester replaying all 5 strategies against all 5 configured pairs (25 combos), with fee (0.1% default) + slippage (5bps default) cost modeling, TP/SL detection via intrabar high/low, and win rate / total return / max drawdown / Sharpe / profit factor metrics. Verified: vectorized signal computation matches the live bot's windowed calls exactly (0/100 mismatches in an empirical spot-check), fee/slippage math hand-verified against actual trade output.
- Ran the 90-day/25-combo backtest twice: once against testnet data (`src/backtest_results_testnet.csv`, testnet only retained ~51-58 of the 90 days), once against real mainnet data (`src/backtest_results_mainnet.csv`, full 129,600 candles/symbol = complete 90 days). `src/backtest_results.csv` (the default path) is currently a copy of the mainnet run — the authoritative result.
- Built `src/tune.py`: parameter-sweep tuning on top of `backtest.py`. Generalized `compute_signals()`/`simulate_trades()` in `backtest.py` to accept indicator-parameter overrides and an ATR-based (volatility-adjusted) stop-sizing mode as an alternative to the flat 2%/4% SL/TP, without changing `backtest.py`'s own default output (verified via regression check against the preserved mainnet run — 0 mismatches). `tune.py` sweeps indicator params × exit sizing (3 flat + 3 ATR configs) × timeframe (1m/15m/1h) = 2,520 combos across all 5 symbols × 5 strategies, ranked by Sharpe among combos with ≥20 trades. ATR math hand-verified against a real trade's exact fill price.

### Tuning sweep result — 2026-07-25 (mainnet, 2,520 combos)
Ran the full default sweep against mainnet data. **2,241/2,520 combos cleared the ≥20-trade filter, and the overwhelming majority of those are still net losers** — tuning parameters alone did not rescue RSI/EMA/MACD/BOLL; on the 1h timeframe especially, several combos are worse than the original 90-day backtest (down to -77%). Full results in `src/tune_results.csv`.

**One narrow bright spot**: the Momentum (ROC) strategy is the only strategy family showing any positive results, concentrated on longer timeframes (15m/1h) with default or near-default parameters and looser/ATR-based stops:
- Best combo overall: **SOLUSDC / MOM / 1h / ATR stops (14-period ATR, 2.0x SL, 4.0x TP)**, default ROC params (window=5, ±3% threshold) — +10.35% return, Sharpe **+0.44**, 79 trades, 36.7% win rate, profit factor 1.07.
- A few other MOM combos (ETHUSDC/MOM/15m, ADAUSDC/MOM/1m) show small positive Sharpe (0.2-0.37) but on thin samples (20-27 trades, right at the filter threshold).
- Every non-MOM strategy's best-of-3-per-group result is still negative or roughly breakeven at best (e.g. BNBUSDC/RSI/1h at only +1.85%/Sharpe 0.20).

**Important caveat — this is NOT yet a validated edge.** A Sharpe of 0.44 is weak by normal trading standards (many practitioners want >1 to consider a strategy live-worthy), and it's the best result out of 2,520 tested combinations on a single historical window — with that many comparisons, some noise-driven "winners" are statistically expected even if none of these strategies have a true edge (the multiple-comparisons/overfitting trap). This result has NOT been tested on a time period outside the one it was found on. **Do not treat this as a strategy to run live yet** — it's a lead worth validating out-of-sample (see Next Steps #1), not a conclusion.

### Key finding — confirmed on both testnet (2026-07-24) and mainnet (2026-07-25)
**Every single one of the 25 symbol × strategy combinations lost money after fees/slippage, on both datasets.** This is now a decisively confirmed result, not just a testnet artifact — the mainnet run had much larger sample sizes (most combos 50-150 trades vs. testnet's 2-3) and still showed uniform losses (-2% to -66% over 90 days).

Notably, the *specific* ranking of "least bad" / "worst" combo was **not stable** between the two datasets (e.g. BTCUSDC/RSI was the single worst combo on testnet at -50.7%, but mid-pack on mainnet at -24.7%; ADAUSDC looked fine on testnet but was the worst symbol on mainnet, -53% to -62% across strategies). That instability is itself informative — a real edge tends to hold up reasonably consistently across overlapping windows; a combo flipping from best to worst depending on which historical slice you use looks like noise, not signal.

Currently-live assignments on mainnet: BTCUSDC/RSI -24.7%, ETHUSDC/EMA -33.0%, BNBUSDC/MACD -23.2% (best multi-trade combo, still a loser), SOLUSDC/BOLL -29.0%, ADAUSDC/MOM -2.3% (only 1 trade, not meaningful). Across nearly every combo: average win (~+3.5%) is bigger than average loss (~-2.3%), but win rates are so low (mostly 28-34%) that expectancy is negative anyway — these single-indicator signals on 1-minute candles look like mostly noise, not a real edge.

**Conclusion: none of the 5 existing strategies, as currently configured, should be run with real capital.**

## Next steps (not started yet, in priority order)

1. **Walk-forward / out-of-sample validation of the SOLUSDC/MOM/1h/ATR(2.0,4.0) lead** (and maybe the ETHUSDC/MOM/15m one) — test on a historical window that was NOT part of the sweep (e.g. the 90-365 days before the current lookback windows), or split the existing window into a search half and a held-out half. If the edge doesn't hold up out-of-sample, it was noise from testing 2,520 combos, and momentum-based strategies should be deprioritized too. `tune.py` doesn't support a separate validation window yet — would need a `--start`/`--end` (or similar) option, since it currently only computes ranges relative to "now".
2. **If the momentum lead survives validation**: consider whether multi-indicator confirmation (e.g. requiring a trend filter alongside ROC) could improve on Sharpe 0.44 — explicitly deferred from the tuning round, now potentially worth revisiting for this one strategy family specifically rather than all 5.
3. **If it doesn't survive validation**: the honest conclusion is that none of RSI/EMA/MACD/BOLL/MOM — tuned or not — have a real edge on these 5 pairs, and a genuinely different approach (not a parameter variant of these 5) would be needed.
4. Only after a strategy shows a real, validated, out-of-sample edge: run it live via `USE_TESTNET=true` for a real-order trial (not just a backtest simulation), then consider small real capital.

## Outstanding housekeeping
- **Security**: two real Binance API keys (stored as `API_KEY_REAL`/`API_SECRET_REAL` in `src/.env`) were accidentally displayed in plaintext in the chat transcript on 2026-07-24 due to an incomplete masking regex while checking `.env` contents. Rotate/regenerate these on Binance if not already done.
- `bot.py` is currently configured with `DRY_RUN=true`, `USE_TESTNET=true` — safe, simulates trades against testnet market data only.

## Reference
- Architecture, setup, and commands: [CLAUDE.md](CLAUDE.md)
- Backtest usage: `python backtest.py --help` (in `src/`)
- Tuning sweep usage: `python tune.py --help` (in `src/`)
- Latest backtest output: `src/backtest_results.csv` (= mainnet run). Testnet and mainnet runs also preserved separately as `src/backtest_results_testnet.csv` / `src/backtest_results_mainnet.csv` for comparison.
- Latest tuning sweep output: `src/tune_results.csv` (2,520 rows, one per tested combo — not just winners)
