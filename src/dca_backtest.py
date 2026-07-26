"""Backtests a DCA-with-safety-orders strategy.

Simulation only -- no live bot.py integration yet. One active "cycle" at a
time (unlike grid_backtest.py's many-simultaneous-slots): a base order,
then up to max_safety_orders further buys as price drops (each bigger than
the last, martingale-style), all averaged into one position, closed
entirely at once on take-profit. No indicator-signal gating on when a cycle
starts (a new cycle's base order fires immediately once the previous one
closes -- isolates the DCA mechanic itself) and no stop-loss in this first
version (mirrors how grid_backtest.py was scoped, for comparability, and to
see the raw risk of averaging down directly).

cycle_capital = current equity at the start of each cycle (100% of equity
in), so this strategy COMPOUNDS cycle-to-cycle -- unlike grid_backtest.py's
deliberately fixed capital_per_slot. This means DCA's total_return_pct is
closer to backtest.py's compounding convention than to grid_backtest.py's,
and DCA-vs-Grid comparisons inherit the same non-comparability caveat
grid_backtest.py already documents for Grid-vs-backtest.py.

take_profit_pct here means "% above the cost-inclusive average entry price"
(avg_entry_eff, which already has fee/slippage baked in), NOT "% above the
raw entry price" the way backtest.py/grid_backtest.py compute their TP --
so a nominal take_profit_pct only loses the exit-side cost when realized,
not the full round-trip cost. Intentional (avoids a second parallel
raw-price tracking system), not a bug -- see the cost-inclusive-TP test in
_dca_verify-style checks.

Same date-drift/cache-miss trap as grid_backtest.py avoids: defaults to
loading whatever's already cached rather than recomputing a relative-to-now
date range; --refresh-cache opts into a live fetch.
"""
import os
import sys
import time
import logging
import argparse

import numpy as np
import pandas as pd

from backtest import compute_metrics, compute_date_range, pair_configs, USE_TESTNET
from grid_backtest import find_cached_csv, load_klines_for_grid as load_klines_for_dca

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TP_ROUND_TRIP_WARN_MULTIPLE = 3.0


def parse_args():
    parser = argparse.ArgumentParser(description="Backtest a DCA-with-safety-orders strategy.")
    parser.add_argument('--symbols', default=None, help='Comma-separated symbols (default: all pairs in bot.py)')
    parser.add_argument('--interval', default='1m')
    parser.add_argument('--days', type=int, default=90, help='Only used with --refresh-cache')
    parser.add_argument('--max-safety-orders', type=int, default=5, dest='max_safety_orders')
    parser.add_argument('--price-deviation-pct', type=float, default=2.0, dest='price_deviation_pct')
    parser.add_argument('--step-scale', type=float, default=1.5, dest='step_scale')
    parser.add_argument('--volume-scale', type=float, default=1.5, dest='volume_scale')
    parser.add_argument('--take-profit-pct', type=float, default=2.5, dest='take_profit_pct')
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
    args.output_csv = args.output_csv or os.path.join(BASE_DIR, 'dca_backtest_results.csv')
    return args


def print_banner(args):
    mode = 'TESTNET' if USE_TESTNET else 'MAINNET (live market data, read-only)'
    print(f"DCA backtest source: Binance {mode}")
    print(f"symbols={args.symbols} interval={args.interval} max_safety_orders={args.max_safety_orders} "
          f"price_deviation_pct={args.price_deviation_pct} step_scale={args.step_scale} "
          f"volume_scale={args.volume_scale} take_profit_pct={args.take_profit_pct}")
    print(f"fee={args.fee} slippage_bps={args.slippage_bps} initial_equity={args.initial_equity}\n")
    if USE_TESTNET:
        logging.warning("USE_TESTNET is enabled - testnet has much shallower historical depth than mainnet.")


def precompute_dca_schedule(max_safety_orders, price_deviation_pct, step_scale, volume_scale):
    if max_safety_orders < 0:
        raise ValueError("max_safety_orders must be >= 0")
    if price_deviation_pct <= 0 or step_scale <= 0 or volume_scale <= 0:
        raise ValueError("price_deviation_pct, step_scale, volume_scale must all be > 0")

    weights = np.array([volume_scale ** k for k in range(max_safety_orders + 1)])
    deviations = np.array([price_deviation_pct * (step_scale ** (k - 1)) for k in range(1, max_safety_orders + 1)])
    if np.any(deviations >= 100):
        raise ValueError(f"a safety order deviation reaches >=100% ({deviations}); "
                          f"reduce max_safety_orders/price_deviation_pct/step_scale")
    total_weight = weights.sum()
    return weights, deviations, total_weight


def simulate_dca_trades(df, *, max_safety_orders, price_deviation_pct, step_scale, volume_scale,
                         take_profit_pct, fee, slippage_bps, initial_equity):
    weights, deviations, total_weight = precompute_dca_schedule(
        max_safety_orders, price_deviation_pct, step_scale, volume_scale
    )

    round_trip_cost_pct = 2 * (fee + slippage_bps / 10000) * 100
    if take_profit_pct < TP_ROUND_TRIP_WARN_MULTIPLE * round_trip_cost_pct:
        logging.warning(f"take_profit_pct ({take_profit_pct:.2f}%) is less than "
                         f"{TP_ROUND_TRIP_WARN_MULTIPLE}x round-trip cost (~{round_trip_cost_pct:.2f}%) - "
                         f"cycles may be near-guaranteed losers regardless of market direction")

    n = len(df)
    times = df['open_time'].to_numpy()
    opens = df['open'].to_numpy()
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()

    slippage = slippage_bps / 10000.0
    cash = initial_equity
    equity_curve = [initial_equity] * n
    trades = []
    cycle = None

    n_tp = 0
    n_held_to_end = 0
    fills_per_cycle = []

    for i in range(n):
        if cycle is None:
            cycle_capital = cash
            raw = opens[i]
            entry_eff = raw * (1 + slippage) * (1 + fee)
            dollar = cycle_capital * weights[0] / total_weight
            qty = dollar / entry_eff
            cash -= dollar
            cycle = {
                'cycle_capital': cycle_capital, 'entry_time': times[i], 'n_fills': 1,
                'total_qty': qty, 'total_capital_deployed': dollar,
                'avg_entry_eff': entry_eff, 'last_fill_raw': raw,
            }

        tp_trigger = cycle['avg_entry_eff'] * (1 + take_profit_pct / 100)
        if highs[i] >= tp_trigger:
            exit_eff = tp_trigger * (1 - slippage) * (1 - fee)
            proceeds = cycle['total_qty'] * exit_eff
            pnl_abs = proceeds - cycle['total_capital_deployed']
            return_pct = (proceeds / cycle['total_capital_deployed'] - 1) * 100
            cash += proceeds
            trades.append({
                'entry_time': cycle['entry_time'], 'exit_time': times[i],
                'entry_avg_eff': cycle['avg_entry_eff'], 'exit_eff': exit_eff,
                'total_qty': cycle['total_qty'], 'total_capital_deployed': cycle['total_capital_deployed'],
                'return_pct': return_pct, 'pnl_abs': pnl_abs, 'reason': 'TP', 'n_fills': cycle['n_fills'],
            })
            n_tp += 1
            fills_per_cycle.append(cycle['n_fills'])
            cycle = None
        elif cycle['n_fills'] <= max_safety_orders:
            k = cycle['n_fills']
            so_trigger = cycle['last_fill_raw'] * (1 - deviations[k - 1] / 100)
            if lows[i] <= so_trigger:
                entry_eff = so_trigger * (1 + slippage) * (1 + fee)
                dollar = cycle['cycle_capital'] * weights[k] / total_weight
                qty = dollar / entry_eff
                cash -= dollar
                new_qty = cycle['total_qty'] + qty
                cycle['avg_entry_eff'] = (cycle['avg_entry_eff'] * cycle['total_qty'] + entry_eff * qty) / new_qty
                cycle['total_qty'] = new_qty
                cycle['total_capital_deployed'] += dollar
                cycle['last_fill_raw'] = so_trigger
                cycle['n_fills'] += 1

        assert cash >= -1e-9, f"cash went negative at candle {i}: {cash}"
        equity_curve[i] = cash + (cycle['total_qty'] * closes[i] if cycle else 0.0)

    if cycle is not None:
        exit_eff = closes[-1] * (1 - slippage) * (1 - fee)
        proceeds = cycle['total_qty'] * exit_eff
        pnl_abs = proceeds - cycle['total_capital_deployed']
        return_pct = (proceeds / cycle['total_capital_deployed'] - 1) * 100
        cash += proceeds
        trades.append({
            'entry_time': cycle['entry_time'], 'exit_time': times[-1],
            'entry_avg_eff': cycle['avg_entry_eff'], 'exit_eff': exit_eff,
            'total_qty': cycle['total_qty'], 'total_capital_deployed': cycle['total_capital_deployed'],
            'return_pct': return_pct, 'pnl_abs': pnl_abs, 'reason': 'HELD_TO_END', 'n_fills': cycle['n_fills'],
        })
        n_held_to_end += 1
        fills_per_cycle.append(cycle['n_fills'])
        equity_curve[-1] = cash

    n_cycles = len(trades)
    dca_stats = {
        'n_cycles': n_cycles, 'n_tp': n_tp, 'n_held_to_end': n_held_to_end,
        'avg_n_fills': round(sum(fills_per_cycle) / n_cycles, 3) if n_cycles else float('nan'),
        'max_n_fills_used': max(fills_per_cycle) if fills_per_cycle else 0,
        'pct_cycles_with_so': round(sum(1 for f in fills_per_cycle if f > 1) / n_cycles * 100, 2) if n_cycles else float('nan'),
        'worst_case_cycle_drop_pct': round((1 - np.prod(1 - deviations / 100)) * 100, 2) if max_safety_orders > 0 else 0.0,
    }

    equity_series = pd.Series(equity_curve, index=pd.DatetimeIndex(times))
    return trades, equity_series, dca_stats


def run_dca_backtest_for_symbol(symbol, args, df=None):
    if df is None:
        df = load_klines_for_dca(symbol, args.interval, args.days, args.cache_dir, args.refresh_cache)

    trades, equity_curve, dca_stats = simulate_dca_trades(
        df, max_safety_orders=args.max_safety_orders, price_deviation_pct=args.price_deviation_pct,
        step_scale=args.step_scale, volume_scale=args.volume_scale, take_profit_pct=args.take_profit_pct,
        fee=args.fee, slippage_bps=args.slippage_bps, initial_equity=args.initial_equity,
    )
    metrics = compute_metrics(trades, equity_curve, args.initial_equity, args.risk_free_rate)
    realized_dd = ((df['close'] / df['close'].cummax() - 1).min()) * 100

    return {
        'symbol': symbol,
        'realized_price_max_drawdown_pct': round(realized_dd, 2),
        **dca_stats,
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
    results = [run_dca_backtest_for_symbol(symbol, args) for symbol in args.symbols]
    elapsed = time.time() - t0

    results_df = pd.DataFrame(results).sort_values('total_return_pct', ascending=False)
    print()
    print(results_df.to_string(index=False))
    results_df.to_csv(args.output_csv, index=False)
    print(f"\nWrote {len(results_df)} rows to {args.output_csv} ({elapsed:.1f}s)")


if __name__ == '__main__':
    main()
