"""Backtests a static price-band grid-trading strategy.

Simulation only -- no live bot.py integration yet. This is a structurally
different strategy from the 5 indicator strategies (no single BUY/SELL
signal from signal(df); instead N staggered buy/sell levels across a price
range), so it gets its own simulator here rather than being forced into
compute_signals()/simulate_trades().

The grid is opened once at the start of the window and never re-centered --
a deliberate first-pass simplification, not an oversight. Capital is split
evenly across grid slots up front and stays fixed (not resized as profit
accumulates) -- realized profit sits as idle cash rather than being
redeployed into bigger positions. This means total_return_pct here is NOT
directly apples-to-apples against backtest.py's single-position results
(which are ~fully deployed whenever in a trade): if price only ever visits
a couple of the grid's cells, most of initial_equity sits untouched the
whole run. n_round_trips/n_held_to_end are reported separately so a modest
return caused by "price barely moved" reads differently from "grid has no
edge" or "price crashed and slots are stuck holding."

Slots above the starting reference price only "arm" (become eligible to
fill their buy leg) once price has actually traded up to their level --
otherwise a slot placed above where the grid opened could fill on the very
first dip despite the market never genuinely visiting that price, which a
real resting limit order placed at grid-open could never do.

Default data loading intentionally does NOT recompute a date range relative
to "now" (compute_date_range) -- that's the exact cache-miss/date-drift trap
already hit twice this session (backtest.py, tune.py mainnet reruns): the
cache filename is day-granular, so running this on a different day than the
cache was built silently falls through to a live fetch. Instead this loads
whatever's already cached on disk by default; --refresh-cache opts into a
fresh relative-to-now fetch.
"""
import os
import sys
import time
import logging
import argparse

import numpy as np
import pandas as pd

from backtest import load_klines_cached, compute_metrics, compute_date_range, pair_configs, USE_TESTNET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FEE_ROUND_TRIP_WARN_MULTIPLE = 3.0


def parse_args():
    parser = argparse.ArgumentParser(description="Backtest a static price-band grid-trading strategy.")
    parser.add_argument('--symbols', default=None, help='Comma-separated symbols (default: all pairs in bot.py)')
    parser.add_argument('--interval', default='1m')
    parser.add_argument('--days', type=int, default=90, help='Only used with --refresh-cache')
    parser.add_argument('--range-pct', type=float, default=10.0, dest='range_pct')
    parser.add_argument('--num-levels', type=int, default=10, dest='num_levels')
    parser.add_argument('--fee', type=float, default=0.001)
    parser.add_argument('--slippage-bps', type=float, default=5.0, dest='slippage_bps')
    parser.add_argument('--initial-equity', type=float, default=1000.0, dest='initial_equity')
    parser.add_argument('--cache-dir', default=None, dest='cache_dir')
    parser.add_argument('--refresh-cache', action='store_true', dest='refresh_cache')
    parser.add_argument('--output-csv', default=None, dest='output_csv')
    parser.add_argument('--risk-free-rate', type=float, default=0.0, dest='risk_free_rate')
    args = parser.parse_args()

    args.symbols = args.symbols.split(',') if args.symbols else list(pair_configs.keys())
    args.cache_dir = args.cache_dir or os.path.join(BASE_DIR, 'historical_data')
    args.output_csv = args.output_csv or os.path.join(BASE_DIR, 'grid_backtest_results.csv')
    return args


def print_banner(args):
    mode = 'TESTNET' if USE_TESTNET else 'MAINNET (live market data, read-only)'
    print(f"Grid backtest source: Binance {mode}")
    print(f"symbols={args.symbols} interval={args.interval} range_pct={args.range_pct} num_levels={args.num_levels}")
    print(f"fee={args.fee} slippage_bps={args.slippage_bps} initial_equity={args.initial_equity}\n")
    if USE_TESTNET:
        logging.warning("USE_TESTNET is enabled - testnet has much shallower historical depth than mainnet.")


def find_cached_csv(symbol, interval, cache_dir):
    matches = sorted(f for f in os.listdir(cache_dir)
                      if f.startswith(f"{symbol}_{interval}_") and f.endswith('.csv'))
    if not matches:
        raise RuntimeError(f"No cached data for {symbol}/{interval} in {cache_dir}; "
                            f"run backtest.py first, or pass --refresh-cache to fetch fresh data.")
    return os.path.join(cache_dir, matches[-1])


def load_klines_for_grid(symbol, interval, days, cache_dir, refresh_cache):
    if not refresh_cache:
        path = find_cached_csv(symbol, interval, cache_dir)
        df = pd.read_csv(path, parse_dates=['open_time'])
        df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
        logging.info(f"[{symbol}] loaded {len(df)} cached candles from {os.path.basename(path)}")
        return df
    start, end = compute_date_range(days)
    return load_klines_cached(symbol, interval, start, end, cache_dir, force_refresh=True)


def build_grid_levels(ref_price, range_pct, num_levels):
    if num_levels < 2:
        raise ValueError("num_levels must be >= 2 (need at least 1 buy/sell slot)")
    lower = ref_price * (1 - range_pct / 100)
    upper = ref_price * (1 + range_pct / 100)
    return np.linspace(lower, upper, num_levels)


def simulate_grid_trades(df, *, range_pct, num_levels, fee, slippage_bps, initial_equity):
    n = len(df)
    times = df['open_time'].to_numpy()
    closes = df['close'].to_numpy()
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()

    slippage = slippage_bps / 10000.0
    ref_price = closes[0]
    levels = build_grid_levels(ref_price, range_pct, num_levels)
    num_slots = num_levels - 1
    capital_per_slot = initial_equity / num_slots

    avg_cell_width_pct = (range_pct * 2) / num_slots
    round_trip_cost_pct = 2 * (fee + slippage) * 100
    if avg_cell_width_pct < FEE_ROUND_TRIP_WARN_MULTIPLE * round_trip_cost_pct:
        logging.warning(f"grid cell width (~{avg_cell_width_pct:.2f}%) is less than "
                         f"{FEE_ROUND_TRIP_WARN_MULTIPLE}x round-trip cost (~{round_trip_cost_pct:.2f}%) - "
                         f"every round trip may be a near-guaranteed loser regardless of market direction")

    slot_state = ['EMPTY'] * num_slots
    slot_qty = [0.0] * num_slots
    slot_entry_eff = [0.0] * num_slots
    slot_entry_time = [None] * num_slots

    cash = initial_equity
    equity_curve = [initial_equity] * n
    trades = []
    # A resting buy limit order only makes sense strictly below price that has
    # actually been traded. Slots at/below ref_price are validly "armed" from
    # the start; slots above ref_price only arm once price has genuinely
    # traded up to that level -- otherwise a slot above ref_price would fill
    # on the very first dip even though the market never actually visited
    # that price, which a real limit order placed at grid-open never could.
    running_high = ref_price

    for i in range(1, n):
        running_high = max(running_high, highs[i])
        for s in range(num_slots):
            buy_level = levels[s]
            sell_level = levels[s + 1]

            if slot_state[s] == 'EMPTY':
                if running_high >= buy_level and lows[i] <= buy_level:
                    entry_eff = buy_level * (1 + slippage) * (1 + fee)
                    slot_qty[s] = capital_per_slot / entry_eff
                    slot_entry_eff[s] = entry_eff
                    slot_entry_time[s] = times[i]
                    cash -= capital_per_slot
                    slot_state[s] = 'HOLDING'
            else:
                if highs[i] >= sell_level:
                    exit_eff = sell_level * (1 - slippage) * (1 - fee)
                    proceeds = slot_qty[s] * exit_eff
                    trade_return = exit_eff / slot_entry_eff[s] - 1
                    cash += proceeds
                    trades.append({
                        'slot': s, 'entry_time': slot_entry_time[s], 'exit_time': times[i],
                        'buy_level': buy_level, 'sell_level': sell_level,
                        'entry_eff': slot_entry_eff[s], 'exit_eff': exit_eff, 'qty': slot_qty[s],
                        'return_pct': trade_return * 100, 'pnl_abs': proceeds - capital_per_slot,
                        'reason': 'GRID',
                    })
                    slot_state[s] = 'EMPTY'
                    slot_qty[s] = 0.0

        assert cash >= -1e-9, f"cash went negative at candle {i}: {cash}"
        holding_value = sum(slot_qty[s] * closes[i] for s in range(num_slots) if slot_state[s] == 'HOLDING')
        equity_curve[i] = cash + holding_value

    n_held_to_end = 0
    for s in range(num_slots):
        if slot_state[s] == 'HOLDING':
            n_held_to_end += 1
            exit_eff = closes[-1] * (1 - slippage) * (1 - fee)
            proceeds = slot_qty[s] * exit_eff
            trade_return = exit_eff / slot_entry_eff[s] - 1
            cash += proceeds
            trades.append({
                'slot': s, 'entry_time': slot_entry_time[s], 'exit_time': times[-1],
                'buy_level': levels[s], 'sell_level': levels[s + 1],
                'entry_eff': slot_entry_eff[s], 'exit_eff': exit_eff, 'qty': slot_qty[s],
                'return_pct': trade_return * 100, 'pnl_abs': proceeds - capital_per_slot,
                'reason': 'HELD_TO_END',
            })
    if n_held_to_end:
        equity_curve[-1] = cash

    equity_series = pd.Series(equity_curve, index=pd.DatetimeIndex(times))
    grid_info = {
        'ref_price': ref_price, 'lower': levels[0], 'upper': levels[-1],
        'num_slots': num_slots, 'capital_per_slot': capital_per_slot,
        'n_held_to_end': n_held_to_end,
    }
    return trades, equity_series, grid_info


def run_grid_backtest_for_symbol(symbol, args, df=None):
    if df is None:
        df = load_klines_for_grid(symbol, args.interval, args.days, args.cache_dir, args.refresh_cache)

    trades, equity_curve, grid_info = simulate_grid_trades(
        df, range_pct=args.range_pct, num_levels=args.num_levels,
        fee=args.fee, slippage_bps=args.slippage_bps, initial_equity=args.initial_equity,
    )
    metrics = compute_metrics(trades, equity_curve, args.initial_equity, args.risk_free_rate)
    n_round_trips = sum(1 for t in trades if t['reason'] == 'GRID')

    return {
        'symbol': symbol,
        'ref_price': round(grid_info['ref_price'], 6), 'lower': round(grid_info['lower'], 6),
        'upper': round(grid_info['upper'], 6), 'realized_low': round(df['low'].min(), 6),
        'realized_high': round(df['high'].max(), 6),
        'num_slots': grid_info['num_slots'], 'capital_per_slot': round(grid_info['capital_per_slot'], 2),
        'n_round_trips': n_round_trips, 'n_held_to_end': grid_info['n_held_to_end'],
        **metrics,
    }


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    args = parse_args()

    unknown_symbols = [s for s in args.symbols if s not in pair_configs]
    if unknown_symbols:
        sys.exit(f"Unknown symbols (not in bot.py's pair_configs): {unknown_symbols}")

    print_banner(args)
    os.makedirs(args.cache_dir, exist_ok=True)

    t0 = time.time()
    results = [run_grid_backtest_for_symbol(symbol, args) for symbol in args.symbols]
    elapsed = time.time() - t0

    results_df = pd.DataFrame(results).sort_values('total_return_pct', ascending=False)
    print()
    print(results_df.to_string(index=False))
    results_df.to_csv(args.output_csv, index=False)
    print(f"\nWrote {len(results_df)} rows to {args.output_csv} ({elapsed:.1f}s)")


if __name__ == '__main__':
    main()
