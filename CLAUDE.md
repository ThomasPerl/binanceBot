# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Binance spot trading bot with a Flask dashboard, plus a standalone historical backtester ([src/backtest.py](src/backtest.py)). All live-trading logic lives in [src/bot.py](src/bot.py); there is no test suite, build system, or linter configured.

**See [tasks.md](tasks.md) for current project status, backtest findings, and the roadmap** — check it at the start of a session to know what's already been tried and what's next.

## Running

```bash
cd src
pip install -r requirements.txt
python bot.py
```

The app requires a `src/.env` file (git-ignored) with:

```
API_KEY=              # Binance API key
API_SECRET=           # Binance API secret
TELEGRAM_TOKEN=       # Telegram bot token, used for trade/error notifications
TELEGRAM_CHAT_ID=     # Telegram chat to notify
DRY_RUN=               # true|false (default false) — simulate signals/orders, never call order-placing endpoints
USE_TESTNET=            # true|false (default false) — point the Binance client at testnet.binance.vision
```

Dashboard is served at `http://localhost:5000/` (Flask, `host='0.0.0.0'`, port 5000).

There are no automated tests, lint config, or CI in this repo. For manual verification, run with `DRY_RUN=true` first — it exercises the full signal → position-open → persistence → position-close path against real market data without placing real orders (balance reads still hit the real account, since `get_asset_balance` is a signed endpoint, so valid API keys are still required even in dry-run).

## Architecture

Everything is in [src/bot.py](src/bot.py), structured around these pieces:

- **Strategy classes** (`RSIStrategy`, `EMAStrategy`, `MACDStrategy`, `BollingerStrategy`, `MomentumStrategy`) — each implements `signal(df)` and returns a `('BUY'|'SELL'|'HOLD', value_or_None)` tuple computed from a pandas OHLCV DataFrame using the `ta` technical-analysis library. New strategies must be added to both the class hierarchy and the `strategy_classes` dict (keyed by short code, e.g. `'RSI'`) so the dashboard's strategy dropdown and `TradingPair.update_config` can reference them by name.

- **`TradingPair`** — a `threading.Thread` subclass, one instance per trading pair (e.g. `BTCUSDC`). At thread start it blocks (with 10s retries) until `client.get_symbol_info(symbol)` succeeds, caching `tick_size`/`step_size`/`min_notional` for the lifetime of the thread — these drive all price/qty rounding via `binance.helpers.round_step_size` (Decimal step-floor rounding), replacing naive `round()`. The loop then runs every 60s (`shutdown_event.wait(60)`, which wakes instantly on shutdown): if `self.position` is set, it only calls `check_position_closed()` and skips signal evaluation entirely — **a pair never re-buys while it already holds a position**, regardless of what the strategy currently signals. Only when flat does it evaluate the strategy and call `try_buy()` on a BUY signal.

- **Position tracking + persistence** (`position_state`, `src/position_state.json`): a module-level dict, keyed by symbol, holding `open`, `entry_price`, `qty`, `buy_order_id`, `oco_order_list_id`, the `sl_percent`/`tp_percent`/`sl_price`/`tp_price` in effect when the position was opened, and `opened_at`. Loaded once at startup (before any thread starts) so a restart doesn't forget an open position — each `TradingPair.__init__` restores its own entry if present. `save_position_state()` writes atomically (`.tmp` + `os.replace`) after every open/close, never on HOLD/error ticks. All reads/writes to `position_state`, plus the balance-check-and-buy critical section in `try_buy()`, are guarded by one module-level `state_lock` — this is what keeps the 5 pair threads from racing on the same USDC balance.

- **`try_buy()`**: computes `usdc_amount = free_balance / (num_pairs - num_currently_open)` (only still-flat pairs count against the divisor), floors qty/price to the symbol's step/tick size, skips the order if `qty * price < min_notional`, then places a market buy + OCO sell (or synthesizes fake IDs under `DRY_RUN`) and records the result into `position_state`.

- **`check_position_closed()`**: called every tick while a position is open. Live/testnet mode polls `client.v3_get_order_list(orderListId=...)` and treats `listOrderStatus == 'ALL_DONE'` as closed (this OCO status endpoint, not the older `get_oco_order`, is what the current `python-binance` exposes). `DRY_RUN` mode instead compares the latest close price against the recorded `sl_price`/`tp_price`. Either way, a close clears `self.position`, updates+persists `position_state`, notifies via Telegram, and appends a row to `trade_log.csv` (write-only audit trail — nothing reads it back).

- **OCO order shape**: `create_oco_order` uses the current `aboveType`/`belowType` parameter schema (`ORDER_TYPE_LIMIT_MAKER` above at `tp_price`, `ORDER_TYPE_STOP_LOSS_LIMIT` below at `sl_price`), not the older `price`/`stopPrice`/`stopLimitPrice` params — those were removed from the live API and will be rejected if reintroduced.

- **Global `pairs` dict** (`symbol -> TradingPair`) is populated once at startup from the `pair_configs` dict (symbol -> `{strategy, sl, tp}`) and shared between the trading threads and the Flask request thread — this is the sole mechanism connecting the dashboard to the running bots.

- **Flask dashboard** (`/`, GET+POST) renders [src/templates/dashboard.html](src/templates/dashboard.html), listing each pair's current strategy/SL/TP plus live status (signal, price, RSI, open-position indicator, timestamp) pulled from `TradingPair.status`. POSTing the form validates each symbol's strategy name and sl/tp range server-side before applying (invalid input is logged and skipped rather than crashing the route), updates `pair_configs`, and calls `update_config()` on the corresponding live `TradingPair` thread.

- **`send_telegram(msg)`** — fire-and-forget notification helper (swallows all exceptions) used for startup, trade open/close, and per-pair error reporting.

- **Shutdown**: `SIGINT` (and `SIGTERM` where the platform supports it) sets a module-level `shutdown_event`, which every thread's loop and sleep check, so Ctrl+C stops all pair threads promptly; `app.run()` is wrapped so `KeyboardInterrupt` triggers the same clean join. Open OCO orders are intentionally never cancelled on shutdown — persisted state exists specifically so open positions survive a restart.

## Backtesting

`src/backtest.py` replays all 5 strategies against all 5 configured pairs (25 combos) over historical Binance klines, to measure whether any of them actually have an edge before running them live. Run via `python backtest.py [--symbols ...] [--strategies ...] [--days N] ...` (`--help` for all options); it imports `client`/`strategy_classes`/`pair_configs` directly from `bot.py` rather than duplicating config, but reimplements each strategy's math as a vectorized full-series computation (verified to match the live windowed calls exactly) since replaying bot.py's literal rolling-100-candle recompute at every historical tick would be far too slow. Historical klines are cached per symbol/interval/date-range under `src/historical_data/` (git-ignored); results are written to `src/backtest_results.csv` (overwritten each run).

`src/tune.py` builds on `backtest.py` for parameter-sweep tuning: `compute_signals()`/`simulate_trades()` in `backtest.py` accept optional indicator-parameter overrides and an ATR-based (volatility-adjusted) stop-sizing mode as an alternative to flat percentage SL/TP — both default to reproducing `backtest.py`'s original hardcoded behavior when not overridden, so its own single-run output is unaffected. `tune.py` sweeps indicator params × exit-sizing config × timeframe (small hardcoded grids — `INDICATOR_GRIDS`/`EXIT_GRID`/`DEFAULT_INTERVALS` at the top of the file) per symbol/strategy, ranks by Sharpe among combos with enough trades to be meaningful (`--min-trades`, default 20), and writes every tested combo (not just winners) to `src/tune_results.csv`. Run via `python tune.py --help`.

`src/validate.py` out-of-sample-checks specific leads found by `tune.py` (edit its `TARGETS` list to add more) against a historical window that was *not* part of the sweep that found them — derived from the actual in-sample cache file's date range (same length, immediately preceding it), not recomputed relative to "now" (that would drift depending on when the script runs). In-sample metrics are looked up from `tune_results.csv` rather than recomputed, for an exact comparison. Its verdict logic requires Sharpe, total return, *and* profit factor to all agree before calling a lead promising — Sharpe alone can read positive (daily-resampled-return compounding/volatility effects) even when a strategy lost money end-to-end, which is what actually happened to both leads tested so far. Run via `python validate.py`.

`src/grid_backtest.py` backtests a static price-band grid-trading strategy — structurally different from the signal-based strategies (staggered buy/sell levels across a price range, not a single BUY/SELL signal), so it has its own `simulate_grid_trades()` rather than using `compute_signals()`/`simulate_trades()`, though it reuses `compute_metrics()` unchanged. The grid opens once at the start of the window and never re-centers; slots above the starting reference price only "arm" (become eligible to fill) once price has actually traded up to that level first, matching how a real resting limit order works. Defaults to loading whatever's already cached on disk rather than recomputing a relative-to-now date range (the same date-drift/cache-miss trap `validate.py` avoids), since `--refresh-cache` is an explicit opt-in for a live fetch. Simulation-only, no live `bot.py` integration yet. Run via `python grid_backtest.py --help`.

`src/dca_backtest.py` backtests a DCA-with-safety-orders strategy — one active "cycle" at a time (base order + up to N martingale-scaled safety orders as price drops, all averaged into one position, closed entirely on take-profit above the volume-weighted average entry), unlike Grid's many-simultaneous-slots model, so it has its own `simulate_dca_trades()`. Reuses `find_cached_csv`/`load_klines_for_grid` directly from `grid_backtest.py` (renamed on import) rather than a third copy of the cache-lookup logic, and `compute_metrics()` from `backtest.py`. Note `take_profit_pct` here means "% above the cost-inclusive average entry price," not "% above the raw entry price" the way `backtest.py`/`grid_backtest.py` compute their TP — a documented, deliberate difference (avoids a second parallel raw-price tracking system), not a bug. Simulation-only, no live `bot.py` integration yet. Run via `python dca_backtest.py --help`.

See [tasks.md](tasks.md) for the latest findings from all five.

## Notes for changes

- Trading pairs and their default strategy/SL/TP are hardcoded in `pair_configs` near the bottom of [src/bot.py](src/bot.py); adding a pair means adding an entry there.
- `python-binance`'s API surface has moved on from what older bot code (and older tutorials) assume — verify method/param names (e.g. `create_oco_order`'s `aboveType`/`belowType`, `v3_get_order_list` for OCO status) against the installed version before changing order-placement code.
- `src/position_state.json` is runtime-generated state, not meant to be hand-edited except for manual testing (e.g. forcing a close scenario).
