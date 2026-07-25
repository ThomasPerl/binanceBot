"""Parameter-sweep tuning for bot.py's strategies, built on top of backtest.py.

Sweeps indicator parameters, ATR-based (volatility-adjusted) stop-sizing as an
alternative to backtest.py's flat SL/TP percentages, and candle timeframe --
i.e. "does a different setting of the same 5 strategies do better", not "would
a different strategy (e.g. multi-indicator confirmation) do better" -- that's
a separate, not-yet-built exercise.

Results are ranked by Sharpe ratio, restricted to combos with enough trades
(--min-trades) to not be noise -- a great-looking 2-trade run is not evidence.
"""
import os
import sys
import json
import time
import logging
import argparse

import pandas as pd

from backtest import (
    compute_signals, simulate_trades, compute_metrics, compute_atr,
    load_klines_cached, compute_date_range, pair_configs, strategy_classes, USE_TESTNET,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INDICATOR_GRIDS = {
    'RSI': [{'window': w, 'buy_threshold': b, 'sell_threshold': s}
            for w in (10, 14) for b in (20, 30) for s in (70, 80)],           # 8, includes current default (14,30,70)
    'EMA': [{'short_span': 5, 'long_span': 20}, {'short_span': 9, 'long_span': 21},
            {'short_span': 12, 'long_span': 26}, {'short_span': 5, 'long_span': 13}],   # 4
    'MACD': [{'fast': 12, 'slow': 26, 'signal': 9}, {'fast': 5, 'slow': 35, 'signal': 5},
             {'fast': 8, 'slow': 21, 'signal': 5}, {'fast': 12, 'slow': 26, 'signal': 5}],  # 4
    'BOLL': [{'window': w, 'window_dev': d} for w in (14, 20) for d in (1.5, 2, 2.5)],  # 6, includes current default (20,2)
    'MOM': [{'window': w, 'buy_threshold': b, 'sell_threshold': -b}
            for w in (5, 10) for b in (2, 3, 5)],                             # 6, includes current default (5,3,-3)
}

EXIT_GRID = [
    {'mode': 'flat', 'sl_pct': 2.0, 'tp_pct': 4.0},   # current bot.py default
    {'mode': 'flat', 'sl_pct': 1.0, 'tp_pct': 2.0},
    {'mode': 'flat', 'sl_pct': 3.0, 'tp_pct': 6.0},
    {'mode': 'atr', 'atr_window': 14, 'sl_mult': 1.0, 'tp_mult': 2.0},
    {'mode': 'atr', 'atr_window': 14, 'sl_mult': 1.5, 'tp_mult': 3.0},
    {'mode': 'atr', 'atr_window': 14, 'sl_mult': 2.0, 'tp_mult': 4.0},
]

DEFAULT_INTERVALS = ['1m', '15m', '1h']
INTERVAL_DAYS = {'1m': 90, '15m': 180, '1h': 365}


def parse_args():
    parser = argparse.ArgumentParser(description="Parameter-sweep tuning for bot.py's strategies.")
    parser.add_argument('--symbols', default=None, help='Comma-separated symbols (default: all pairs in bot.py)')
    parser.add_argument('--strategies', default=None, help='Comma-separated strategy codes (default: all)')
    parser.add_argument('--intervals', default=None, help='Comma-separated intervals (default: 1m,15m,1h)')
    parser.add_argument('--days', type=int, default=None, help='Override lookback days uniformly for all intervals')
    parser.add_argument('--fee', type=float, default=0.001)
    parser.add_argument('--slippage-bps', type=float, default=5.0, dest='slippage_bps')
    parser.add_argument('--initial-equity', type=float, default=1000.0, dest='initial_equity')
    parser.add_argument('--warmup-bars', type=int, default=200, dest='warmup_bars')
    parser.add_argument('--risk-free-rate', type=float, default=0.0, dest='risk_free_rate')
    parser.add_argument('--cache-dir', default=None, dest='cache_dir')
    parser.add_argument('--refresh-cache', action='store_true', dest='refresh_cache')
    parser.add_argument('--min-trades', type=int, default=20, dest='min_trades')
    parser.add_argument('--top-n', type=int, default=3, dest='top_n')
    parser.add_argument('--global-top-n', type=int, default=20, dest='global_top_n')
    parser.add_argument('--output-csv', default=None, dest='output_csv')
    args = parser.parse_args()

    args.symbols = args.symbols.split(',') if args.symbols else list(pair_configs.keys())
    args.strategies = args.strategies.split(',') if args.strategies else list(strategy_classes.keys())
    args.intervals = args.intervals.split(',') if args.intervals else list(DEFAULT_INTERVALS)
    args.cache_dir = args.cache_dir or os.path.join(BASE_DIR, 'historical_data')
    args.output_csv = args.output_csv or os.path.join(BASE_DIR, 'tune_results.csv')
    return args


def resolve_interval_days(args):
    if args.days is not None:
        return {iv: args.days for iv in args.intervals}
    return {iv: INTERVAL_DAYS.get(iv, 90) for iv in args.intervals}


def print_banner(args, interval_days):
    mode = 'TESTNET' if USE_TESTNET else 'MAINNET (live market data, read-only)'
    total_combos = sum(len(INDICATOR_GRIDS[c]) for c in args.strategies) * len(EXIT_GRID) * len(args.intervals)
    print(f"Tune source: Binance {mode}")
    print(f"symbols={args.symbols} strategies={args.strategies} intervals={args.intervals} "
          f"days_per_interval={interval_days}")
    print(f"fee={args.fee} slippage_bps={args.slippage_bps} min_trades={args.min_trades}")
    print(f"~{total_combos} combos/symbol x {len(args.symbols)} symbols = ~{total_combos * len(args.symbols)} rows\n")
    if USE_TESTNET:
        logging.warning("USE_TESTNET is enabled - testnet has much shallower historical depth than mainnet.")


def run_sweep_for_symbol(symbol, strategy_codes, args, interval_days):
    results = []
    for interval in args.intervals:
        start, end = compute_date_range(interval_days[interval])
        df = load_klines_cached(symbol, interval, start, end, args.cache_dir, args.refresh_cache)
        atr_cache = {}

        for code in strategy_codes:
            for ind_params in INDICATOR_GRIDS[code]:
                signals = compute_signals(df, code, params=ind_params)

                for exit_cfg in EXIT_GRID:
                    if exit_cfg['mode'] == 'atr':
                        w = exit_cfg['atr_window']
                        if w not in atr_cache:
                            atr_cache[w] = compute_atr(df, window=w)
                        exit_kwargs = {'atr': atr_cache[w], 'sl_atr_mult': exit_cfg['sl_mult'],
                                        'tp_atr_mult': exit_cfg['tp_mult']}
                        exit_params = {'atr_window': w, 'sl_mult': exit_cfg['sl_mult'], 'tp_mult': exit_cfg['tp_mult']}
                    else:
                        exit_kwargs = {'sl_pct': exit_cfg['sl_pct'], 'tp_pct': exit_cfg['tp_pct']}
                        exit_params = {'sl_pct': exit_cfg['sl_pct'], 'tp_pct': exit_cfg['tp_pct']}

                    trades, equity_curve = simulate_trades(
                        df, signals, fee=args.fee, slippage_bps=args.slippage_bps,
                        initial_equity=args.initial_equity, warmup_bars=args.warmup_bars, **exit_kwargs
                    )
                    metrics = compute_metrics(trades, equity_curve, args.initial_equity, args.risk_free_rate)
                    results.append({
                        'symbol': symbol, 'strategy': code, 'interval': interval,
                        'indicator_params': json.dumps(ind_params),
                        'exit_mode': exit_cfg['mode'], 'exit_params': json.dumps(exit_params),
                        **metrics,
                    })
    return results


def rank_results(df, min_trades, top_n, global_top_n):
    eligible = df[df['n_trades'] >= min_trades].sort_values('sharpe', ascending=False)
    per_group = eligible.groupby(['symbol', 'strategy'], group_keys=False).head(top_n)
    global_top = eligible.head(global_top_n)
    return per_group, global_top, len(eligible)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    args = parse_args()

    unknown_symbols = [s for s in args.symbols if s not in pair_configs]
    if unknown_symbols:
        sys.exit(f"Unknown symbols (not in bot.py's pair_configs): {unknown_symbols}")
    unknown_strategies = [s for s in args.strategies if s not in strategy_classes]
    if unknown_strategies:
        sys.exit(f"Unknown strategy codes: {unknown_strategies}. Valid: {list(strategy_classes.keys())}")

    interval_days = resolve_interval_days(args)
    print_banner(args, interval_days)
    os.makedirs(args.cache_dir, exist_ok=True)

    t0 = time.time()
    all_results = []
    for symbol in args.symbols:
        all_results.extend(run_sweep_for_symbol(symbol, args.strategies, args, interval_days))
    elapsed = time.time() - t0

    results_df = pd.DataFrame(all_results)
    results_df.to_csv(args.output_csv, index=False)

    per_group, global_top, n_eligible = rank_results(results_df, args.min_trades, args.top_n, args.global_top_n)
    print(f"{n_eligible}/{len(results_df)} combos cleared the >={args.min_trades}-trade filter\n")

    print(f"--- Top {args.top_n} per (symbol, strategy) by Sharpe ---")
    print(per_group.to_string(index=False))
    print(f"\n--- Global top {args.global_top_n} by Sharpe ---")
    print(global_top.to_string(index=False))

    print(f"\nWrote {len(results_df)} rows to {args.output_csv} ({elapsed:.1f}s)")


if __name__ == '__main__':
    main()
