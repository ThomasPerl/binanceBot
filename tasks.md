# Trading Bot — Tasks & Roadmap

Living document. Update this whenever a milestone finishes or a new decision is made, so a fresh session (human or Claude) can pick up context fast. See [CLAUDE.md](CLAUDE.md) for architecture/commands.

## Status (as of 2026-07-24)

### Done
- Fixed critical correctness bugs in `src/bot.py`: no more re-buying into an already-open position, real exchange tick/step/min-notional rounding (was hardcoded and would've been rejected by Binance), balance allocation no longer races across threads, positions persist across restarts (`src/position_state.json`), graceful shutdown, dashboard input validation. Also fixed `requirements.txt` (wrong `binance` package vs. `python-binance`, missing `python-dotenv`/`cryptography`).
- Added `DRY_RUN` / `USE_TESTNET` env vars so the bot can be exercised safely before risking real funds.
- Fixed a real `python-binance` API mismatch found during verification: `create_oco_order` needed the current `aboveType`/`belowType` params (not the old `price`/`stopPrice`/`stopLimitPrice`), and OCO status is polled via `v3_get_order_list`, not `get_oco_order`.
- Built `src/backtest.py`: historical backtester replaying all 5 strategies against all 5 configured pairs (25 combos), with fee (0.1% default) + slippage (5bps default) cost modeling, TP/SL detection via intrabar high/low, and win rate / total return / max drawdown / Sharpe / profit factor metrics. Verified: vectorized signal computation matches the live bot's windowed calls exactly (0/100 mismatches in an empirical spot-check), fee/slippage math hand-verified against actual trade output.

### Key finding — 2026-07-24 backtest run
Ran the default 90-day backtest (all 25 symbol × strategy combos) against Binance **testnet** historical data (testnet only actually retained ~51 of the 90 requested days — caveat, see Next Steps #1).

**Every single one of the 25 combinations lost money after fees/slippage.** Full table in `src/backtest_results.csv` (gets overwritten on each run — copy it elsewhere first if you want to preserve a specific run's snapshot). Highlights:
- The **currently-live** BTCUSDC/RSI assignment was the **worst of all 25**: -50.7% return, 17% win rate, -51% max drawdown.
- Other live assignments also bad: ETHUSDC/EMA -31.1%, BNBUSDC/MACD -28.7%, SOLUSDC/BOLL -24.4%.
- Best of the 25 (still net negative): ADAUSDC/MOM at -1.0% — but only 3 trades, not statistically meaningful, just "least bad."
- Pattern across almost every combo: average win (~+3.6%) is bigger than average loss (~-2.3%), but win rates are so low (mostly 17-33%) that expectancy is negative anyway — these single-indicator signals on 1-minute candles look like mostly noise.

## Next steps (not started yet, in priority order)

1. **Re-run the backtest against mainnet historical data** to confirm the negative result holds on real, deeper history, not just testnet's truncated ~51 days. `get_historical_klines` is read-only/public (no balance or order access), so temporarily setting `USE_TESTNET=false` in `.env` for a backtest run is safe and doesn't add live-trading risk. Expectation: the result will likely hold given how one-sided it was, but confirm before making design changes based on it.
2. **Decide whether to keep iterating on these 5 strategies or replace them.** Given the near-uniform failure, candidate directions:
   - Move off 1-minute candles to a higher timeframe (15m/1h) to reduce noise.
   - Replace the flat 2%/4% SL/TP (same for every symbol) with volatility-adjusted stops (e.g. ATR-based).
   - Require multi-indicator confirmation instead of a single trigger, to raise the low win rates.
   - Try different parameters for the existing indicators (RSI thresholds, EMA spans, etc.) — not yet supported by `backtest.py` v1, which only tests the strategies exactly as hardcoded in `bot.py`.
3. **Add a parameter-sweep/grid-search mode to `backtest.py`** if step 2 heads toward tuning existing indicators rather than replacing them outright. Explicitly out of scope for v1.
4. **Walk-forward / out-of-sample validation** once a promising strategy or parameter set is found — don't conclude from a single backtest window; validate before considering real capital.
5. Only after a strategy shows a real, validated, out-of-sample edge: run it live via `USE_TESTNET=true` for a real-order trial (not just a backtest simulation), then consider small real capital.

## Outstanding housekeeping
- **Security**: two real Binance API keys (stored as `API_KEY_REAL`/`API_SECRET_REAL` in `src/.env`) were accidentally displayed in plaintext in the chat transcript on 2026-07-24 due to an incomplete masking regex while checking `.env` contents. Rotate/regenerate these on Binance if not already done.
- `bot.py` is currently configured with `DRY_RUN=true`, `USE_TESTNET=true` — safe, simulates trades against testnet market data only.

## Reference
- Architecture, setup, and commands: [CLAUDE.md](CLAUDE.md)
- Backtest usage: `python backtest.py --help` (in `src/`)
- Latest backtest output: `src/backtest_results.csv`
