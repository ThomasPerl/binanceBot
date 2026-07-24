"""Backtests bot.py's strategies against historical Binance data.

Each (symbol, strategy) combination is simulated in isolation with its own
starting equity, fully in/out per trade. This deliberately ignores the live
bot's shared-balance-across-concurrent-positions logic (try_buy() in bot.py
divides free USDC across currently-open pairs) -- that's a portfolio-level
concern, not a strategy-quality one, so it's out of scope here by design.

Indicators are computed once per symbol as full vectorized series rather
than replaying bot.py's live 100-candle rolling window at every tick (that
window size is an artifact of how many klines the live loop happens to
pull, not part of any strategy's math, and windowed recomputation at every
one of ~130k candles x 25 combos would be far too slow). See --warmup-bars.
"""
import os
import sys
import time
import math
import logging
import argparse
from datetime import datetime, timezone, timedelta

import pandas as pd
import ta

from bot import client, strategy_classes, pair_configs, USE_TESTNET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INTERVAL_MINUTES = {
    '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
    '1h': 60, '2h': 120, '4h': 240, '6h': 360, '8h': 480, '12h': 720, '1d': 1440,
}


def parse_args():
    parser = argparse.ArgumentParser(description="Backtest bot.py's strategies against historical Binance data.")
    parser.add_argument('--symbols', default=None, help='Comma-separated symbols (default: all pairs in bot.py)')
    parser.add_argument('--strategies', default=None, help='Comma-separated strategy codes (default: all)')
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--interval', default='1m')
    parser.add_argument('--fee', type=float, default=0.001, help='Fraction per trade leg, e.g. 0.001 = 0.1%%')
    parser.add_argument('--slippage-bps', type=float, default=5.0, dest='slippage_bps')
    parser.add_argument('--initial-equity', type=float, default=1000.0, dest='initial_equity')
    parser.add_argument('--warmup-bars', type=int, default=200, dest='warmup_bars')
    parser.add_argument('--cache-dir', default=None, dest='cache_dir')
    parser.add_argument('--refresh-cache', action='store_true', dest='refresh_cache')
    parser.add_argument('--output-csv', default=None, dest='output_csv')
    parser.add_argument('--risk-free-rate', type=float, default=0.0, dest='risk_free_rate')
    args = parser.parse_args()

    args.symbols = args.symbols.split(',') if args.symbols else list(pair_configs.keys())
    args.strategies = args.strategies.split(',') if args.strategies else list(strategy_classes.keys())
    args.cache_dir = args.cache_dir or os.path.join(BASE_DIR, 'historical_data')
    args.output_csv = args.output_csv or os.path.join(BASE_DIR, 'backtest_results.csv')
    return args


def print_banner(args):
    mode = 'TESTNET' if USE_TESTNET else 'MAINNET (live market data, read-only)'
    print(f"Backtest source: Binance {mode}")
    print(f"symbols={args.symbols} strategies={args.strategies} days={args.days} interval={args.interval}")
    print(f"fee={args.fee} slippage_bps={args.slippage_bps} initial_equity={args.initial_equity}\n")
    if USE_TESTNET:
        logging.warning("USE_TESTNET is enabled - testnet has much shallower historical depth than mainnet; "
                         "results may be based on very little data.")


def compute_date_range(days):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start, end


def cache_path(symbol, interval, start, end, cache_dir):
    return os.path.join(cache_dir, f"{symbol}_{interval}_{start:%Y%m%d}_{end:%Y%m%d}.csv")


def load_klines_cached(symbol, interval, start, end, cache_dir, force_refresh=False):
    path = cache_path(symbol, interval, start, end, cache_dir)

    if not force_refresh and os.path.exists(path):
        df = pd.read_csv(path, parse_dates=['open_time'])
        df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
        logging.info(f"[{symbol}] loaded {len(df)} cached candles from {os.path.basename(path)}")
        return df

    logging.info(f"[{symbol}] fetching historical klines {start.date()} -> {end.date()} ...")
    raw = client.get_historical_klines(
        symbol, interval,
        start_str=str(int(start.timestamp() * 1000)),
        end_str=str(int(end.timestamp() * 1000)),
    )
    if not raw:
        raise RuntimeError(f"No historical klines returned for {symbol} ({interval}, {start.date()}..{end.date()})")

    df = pd.DataFrame(raw, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'qav', 'trades', 'tbbav', 'tbqav', 'ignore'
    ])[['open_time', 'open', 'high', 'low', 'close', 'volume']].copy()
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    df = df.sort_values('open_time').drop_duplicates(subset='open_time').reset_index(drop=True)

    os.makedirs(cache_dir, exist_ok=True)
    df.to_csv(path, index=False)
    logging.info(f"[{symbol}] cached {len(df)} candles to {os.path.basename(path)}")

    interval_minutes = INTERVAL_MINUTES.get(interval)
    if interval_minutes:
        expected = (end - start).total_seconds() / 60 / interval_minutes
        if len(df) < expected * 0.5:
            logging.warning(f"[{symbol}] only got {len(df)} candles, expected ~{int(expected)} - "
                             f"symbol may have a shorter listing history than the requested range")
    return df


def compute_signals(df, strategy_code):
    """Vectorized equivalent of the matching Strategy subclass's signal() in bot.py."""
    close = df['close']
    sig = pd.Series('HOLD', index=df.index)

    if strategy_code == 'RSI':  # RSIStrategy
        rsi = ta.momentum.RSIIndicator(close, window=14).rsi()
        sig[rsi < 30] = 'BUY'
        sig[rsi > 70] = 'SELL'
    elif strategy_code == 'EMA':  # EMAStrategy
        short = close.ewm(span=5).mean()
        long = close.ewm(span=20).mean()
        sig[(short > long) & (short.shift(1) <= long.shift(1))] = 'BUY'
        sig[(short < long) & (short.shift(1) >= long.shift(1))] = 'SELL'
    elif strategy_code == 'MACD':  # MACDStrategy
        diff = ta.trend.MACD(close).macd_diff()
        sig[(diff > 0) & (diff.shift(1) <= 0)] = 'BUY'
        sig[(diff < 0) & (diff.shift(1) >= 0)] = 'SELL'
    elif strategy_code == 'BOLL':  # BollingerStrategy
        bb = ta.volatility.BollingerBands(close)
        sig[close > bb.bollinger_hband()] = 'SELL'
        sig[close < bb.bollinger_lband()] = 'BUY'
    elif strategy_code == 'MOM':  # MomentumStrategy
        roc = ta.momentum.ROCIndicator(close, window=5).roc()
        sig[roc > 3] = 'BUY'
        sig[roc < -3] = 'SELL'
    else:
        raise ValueError(f"unknown strategy code '{strategy_code}'")

    return sig


def simulate_trades(df, signals, sl_pct, tp_pct, fee, slippage_bps, initial_equity, warmup_bars):
    n = len(df)
    times = df['open_time'].to_numpy()
    closes = df['close'].to_numpy()
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    sigs = signals.to_numpy()

    slippage = slippage_bps / 10000.0
    equity = initial_equity
    equity_curve = [initial_equity] * n
    position = None
    trades = []

    for i in range(warmup_bars, n):
        if position is None:
            equity_curve[i] = equity
            if sigs[i] == 'BUY':
                raw_entry = closes[i]
                position = {
                    'entry_time': times[i],
                    'raw_entry': raw_entry,
                    'entry_eff': raw_entry * (1 + slippage) * (1 + fee),
                    'sl_price': raw_entry * (1 - sl_pct / 100),
                    'tp_price': raw_entry * (1 + tp_pct / 100),
                }
            continue

        hit_sl = lows[i] <= position['sl_price']
        hit_tp = highs[i] >= position['tp_price']
        if not (hit_sl or hit_tp):
            equity_curve[i] = equity * (closes[i] / position['raw_entry'])
            continue

        reason = 'SL' if hit_sl else 'TP'
        exit_raw = position['sl_price'] if hit_sl else position['tp_price']
        exit_eff = exit_raw * (1 - slippage) * (1 - fee)
        trade_return = exit_eff / position['entry_eff'] - 1
        equity_before = equity
        equity *= (1 + trade_return)
        trades.append({
            'entry_time': position['entry_time'], 'exit_time': times[i],
            'raw_entry': position['raw_entry'], 'exit_raw': exit_raw, 'reason': reason,
            'return_pct': trade_return * 100,
            'equity_before': equity_before, 'equity_after': equity, 'pnl_abs': equity - equity_before,
        })
        equity_curve[i] = equity
        position = None

    if position is not None:
        exit_raw = closes[-1]
        exit_eff = exit_raw * (1 - slippage) * (1 - fee)
        trade_return = exit_eff / position['entry_eff'] - 1
        equity_before = equity
        equity *= (1 + trade_return)
        trades.append({
            'entry_time': position['entry_time'], 'exit_time': times[-1],
            'raw_entry': position['raw_entry'], 'exit_raw': exit_raw, 'reason': 'HELD_TO_END',
            'return_pct': trade_return * 100,
            'equity_before': equity_before, 'equity_after': equity, 'pnl_abs': equity - equity_before,
        })
        equity_curve[-1] = equity

    equity_series = pd.Series(equity_curve, index=pd.DatetimeIndex(times))
    return trades, equity_series


def compute_sharpe(equity_curve, risk_free_rate):
    daily = equity_curve.resample('1D').last().ffill()
    daily_returns = daily.pct_change().dropna()
    if len(daily_returns) < 2 or daily_returns.std(ddof=1) == 0:
        return float('nan')
    return (daily_returns.mean() - risk_free_rate / 365) / daily_returns.std(ddof=1) * math.sqrt(365)


def compute_metrics(trades, equity_curve, initial_equity, risk_free_rate=0.0):
    n_trades = len(trades)
    if n_trades == 0:
        return {
            'n_trades': 0, 'win_rate_pct': float('nan'), 'total_return_pct': 0.0,
            'max_drawdown_pct': 0.0, 'sharpe': float('nan'), 'profit_factor': float('nan'),
            'avg_win_pct': float('nan'), 'avg_loss_pct': float('nan'),
        }

    returns = [t['return_pct'] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    pnl_abs = [t['pnl_abs'] for t in trades]
    gross_profit = sum(p for p in pnl_abs if p > 0)
    gross_loss = -sum(p for p in pnl_abs if p < 0)

    if gross_loss == 0:
        profit_factor = float('inf') if gross_profit > 0 else float('nan')
    else:
        profit_factor = gross_profit / gross_loss

    cummax = equity_curve.cummax()
    max_drawdown_pct = ((equity_curve - cummax) / cummax).min() * 100

    return {
        'n_trades': n_trades,
        'win_rate_pct': round(len(wins) / n_trades * 100, 2),
        'total_return_pct': round((equity_curve.iloc[-1] / initial_equity - 1) * 100, 2),
        'max_drawdown_pct': round(max_drawdown_pct, 2),
        'sharpe': round(compute_sharpe(equity_curve, risk_free_rate), 3),
        'profit_factor': round(profit_factor, 3) if math.isfinite(profit_factor) else profit_factor,
        'avg_win_pct': round(sum(wins) / len(wins), 3) if wins else float('nan'),
        'avg_loss_pct': round(sum(losses) / len(losses), 3) if losses else float('nan'),
    }


def run_backtest_for_symbol(symbol, strategy_codes, args, start, end):
    df = load_klines_cached(symbol, args.interval, start, end, args.cache_dir, args.refresh_cache)
    sl_pct = pair_configs[symbol]['sl']
    tp_pct = pair_configs[symbol]['tp']

    results = []
    for code in strategy_codes:
        signals = compute_signals(df, code)
        trades, equity_curve = simulate_trades(
            df, signals, sl_pct, tp_pct, args.fee, args.slippage_bps, args.initial_equity, args.warmup_bars
        )
        metrics = compute_metrics(trades, equity_curve, args.initial_equity, args.risk_free_rate)
        results.append({'symbol': symbol, 'strategy': code, **metrics})
    return results


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    args = parse_args()

    unknown_symbols = [s for s in args.symbols if s not in pair_configs]
    if unknown_symbols:
        sys.exit(f"Unknown symbols (not in bot.py's pair_configs): {unknown_symbols}")
    unknown_strategies = [s for s in args.strategies if s not in strategy_classes]
    if unknown_strategies:
        sys.exit(f"Unknown strategy codes: {unknown_strategies}. Valid: {list(strategy_classes.keys())}")

    print_banner(args)
    start, end = compute_date_range(args.days)
    os.makedirs(args.cache_dir, exist_ok=True)

    t0 = time.time()
    all_results = []
    for symbol in args.symbols:
        all_results.extend(run_backtest_for_symbol(symbol, args.strategies, args, start, end))
    elapsed = time.time() - t0

    results_df = pd.DataFrame(all_results).sort_values('total_return_pct', ascending=False)
    print()
    print(results_df.to_string(index=False))
    results_df.to_csv(args.output_csv, index=False)
    print(f"\nWrote {len(results_df)} rows to {args.output_csv} ({elapsed:.1f}s)")


if __name__ == '__main__':
    main()
