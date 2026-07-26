"""Out-of-sample validation for specific leads found by tune.py's parameter sweep.

Not a new sweep -- checks a small, hand-picked list of already-known combos
(TARGETS below) against a historical window that was NOT part of the sweep
that found them, to see if their Sharpe ratio holds up or was just noise
from testing thousands of combinations on one window (the multiple-comparisons
trap: the best of 2,520 tested combos is expected to look good by chance
alone even if none of them have a real edge).

The out-of-sample window is derived from the actual in-sample cache file on
disk (its exact min/max open_time), not recomputed as "N days before now" --
that would drift depending on when this script happens to run, same trap
hit earlier with backtest.py's date-range handling.
"""
import os
import json
import logging

import pandas as pd

from backtest import compute_signals, compute_atr, simulate_trades, compute_metrics, load_klines_cached

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'historical_data')
TUNE_RESULTS_CSV = os.path.join(BASE_DIR, 'tune_results.csv')
OUTPUT_CSV = os.path.join(BASE_DIR, 'validation_results.csv')

FEE = 0.001
SLIPPAGE_BPS = 5.0
INITIAL_EQUITY = 1000.0
WARMUP_BARS = 200
RISK_FREE_RATE = 0.0

TARGETS = [
    {
        'symbol': 'SOLUSDC', 'strategy': 'MOM', 'interval': '1h',
        'indicator_params': {'window': 5, 'buy_threshold': 3, 'sell_threshold': -3},
        'exit_mode': 'atr', 'exit_params': {'atr_window': 14, 'sl_mult': 2.0, 'tp_mult': 4.0},
    },
    {
        'symbol': 'ETHUSDC', 'strategy': 'MOM', 'interval': '15m',
        'indicator_params': {'window': 5, 'buy_threshold': 3, 'sell_threshold': -3},
        'exit_mode': 'flat', 'exit_params': {'sl_pct': 2.0, 'tp_pct': 4.0},
    },
]


def find_in_sample_cache(symbol, interval):
    matches = [f for f in os.listdir(CACHE_DIR) if f.startswith(f"{symbol}_{interval}_") and f.endswith('.csv')]
    if not matches:
        raise RuntimeError(f"No cached in-sample data found for {symbol}/{interval} in {CACHE_DIR} "
                            f"-- run tune.py's sweep first so there's a known in-sample window to validate against.")
    matches.sort()
    return os.path.join(CACHE_DIR, matches[-1])


def lookup_in_sample_metrics(target):
    df = pd.read_csv(TUNE_RESULTS_CSV)
    subset = df[(df.symbol == target['symbol']) & (df.strategy == target['strategy']) &
                (df.interval == target['interval']) & (df.exit_mode == target['exit_mode'])]
    for _, row in subset.iterrows():
        if json.loads(row['indicator_params']) == target['indicator_params'] and \
           json.loads(row['exit_params']) == target['exit_params']:
            return row.to_dict()
    raise RuntimeError(f"No matching row in {TUNE_RESULTS_CSV} for {target} -- "
                        f"re-run tune.py's sweep or check TARGETS matches an actual tested combo.")


def run_combo(df, target):
    signals = compute_signals(df, target['strategy'], params=target['indicator_params'])
    if target['exit_mode'] == 'atr':
        atr = compute_atr(df, window=target['exit_params']['atr_window'])
        trades, equity_curve = simulate_trades(
            df, signals, fee=FEE, slippage_bps=SLIPPAGE_BPS, initial_equity=INITIAL_EQUITY, warmup_bars=WARMUP_BARS,
            atr=atr, sl_atr_mult=target['exit_params']['sl_mult'], tp_atr_mult=target['exit_params']['tp_mult'],
        )
    else:
        trades, equity_curve = simulate_trades(
            df, signals, fee=FEE, slippage_bps=SLIPPAGE_BPS, initial_equity=INITIAL_EQUITY, warmup_bars=WARMUP_BARS,
            sl_pct=target['exit_params']['sl_pct'], tp_pct=target['exit_params']['tp_pct'],
        )
    return compute_metrics(trades, equity_curve, INITIAL_EQUITY, RISK_FREE_RATE)


def interpret(oos_sharpe, oos_total_return_pct, oos_profit_factor):
    """Sharpe alone can mislead here: it's computed from daily-resampled returns and
    can stay positive on volatility/compounding effects even when the strategy actually
    lost money end-to-end. Require the return and profit factor to agree before calling
    anything promising."""
    if pd.isna(oos_sharpe):
        return "INCONCLUSIVE (not enough trades/variance to compute Sharpe)"
    profitable = oos_total_return_pct > 0 and (pd.isna(oos_profit_factor) or oos_profit_factor > 1)
    if oos_sharpe > 0 and profitable:
        return "EDGE PLAUSIBLY HOLDS (positive Sharpe AND profitable out-of-sample) - worth further scrutiny"
    if oos_sharpe > 0 and not profitable:
        return "EDGE DID NOT HOLD (Sharpe positive but return/profit-factor negative out-of-sample - net losing) - likely noise from the sweep"
    if oos_sharpe > -0.2:
        return "INCONCLUSIVE (roughly flat out-of-sample)"
    return "EDGE DID NOT HOLD (clearly negative out-of-sample) - likely noise from the sweep"


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    rows = []

    for target in TARGETS:
        in_sample_path = find_in_sample_cache(target['symbol'], target['interval'])
        in_sample_df = pd.read_csv(in_sample_path, parse_dates=['open_time'])
        in_sample_df['open_time'] = pd.to_datetime(in_sample_df['open_time'], utc=True)

        in_sample_start = in_sample_df['open_time'].min()
        in_sample_end = in_sample_df['open_time'].max()
        window_length = in_sample_end - in_sample_start
        oos_end = in_sample_start
        oos_start = in_sample_start - window_length

        logging.info(f"[{target['symbol']}/{target['interval']}] in-sample window: "
                     f"{in_sample_start.date()} -> {in_sample_end.date()}")
        logging.info(f"[{target['symbol']}/{target['interval']}] out-of-sample window: "
                     f"{oos_start.date()} -> {oos_end.date()}")

        oos_df = load_klines_cached(target['symbol'], target['interval'], oos_start, oos_end, CACHE_DIR)
        oos_metrics = run_combo(oos_df, target)
        in_sample_metrics = lookup_in_sample_metrics(target)

        verdict = interpret(oos_metrics['sharpe'], oos_metrics['total_return_pct'], oos_metrics['profit_factor'])
        print(f"\n=== {target['symbol']} / {target['strategy']} / {target['interval']} "
              f"/ {target['exit_mode']} {target['exit_params']} ===")
        print(f"indicator_params: {target['indicator_params']}")
        compare_df = pd.DataFrame([
            {'window': 'in-sample', **{k: in_sample_metrics[k] for k in
             ['n_trades', 'win_rate_pct', 'total_return_pct', 'sharpe', 'max_drawdown_pct', 'profit_factor']}},
            {'window': 'out-of-sample', **{k: oos_metrics[k] for k in
             ['n_trades', 'win_rate_pct', 'total_return_pct', 'sharpe', 'max_drawdown_pct', 'profit_factor']}},
        ])
        print(compare_df.to_string(index=False))
        print(f"Verdict: {verdict}")

        rows.append({
            'symbol': target['symbol'], 'strategy': target['strategy'], 'interval': target['interval'],
            'indicator_params': json.dumps(target['indicator_params']),
            'exit_mode': target['exit_mode'], 'exit_params': json.dumps(target['exit_params']),
            'in_sample_start': str(in_sample_start.date()), 'in_sample_end': str(in_sample_end.date()),
            'oos_start': str(oos_start.date()), 'oos_end': str(oos_end.date()),
            'in_sample_n_trades': in_sample_metrics['n_trades'], 'in_sample_sharpe': in_sample_metrics['sharpe'],
            'in_sample_total_return_pct': in_sample_metrics['total_return_pct'],
            'oos_n_trades': oos_metrics['n_trades'], 'oos_sharpe': oos_metrics['sharpe'],
            'oos_total_return_pct': oos_metrics['total_return_pct'],
            'verdict': verdict,
        })

    pd.DataFrame(rows).to_csv(OUTPUT_CSV, index=False)
    print(f"\nWrote {len(rows)} rows to {OUTPUT_CSV}")


if __name__ == '__main__':
    main()
