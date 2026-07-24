import os
import csv
import json
import time
import signal
import threading
import logging
from datetime import datetime
import pandas as pd
import ta
import requests
from flask import Flask, render_template, request, redirect
from binance.client import Client
from binance.enums import *
from dotenv import load_dotenv

try:
    from binance.helpers import round_step_size
except ImportError:
    from decimal import Decimal, ROUND_DOWN

    def round_step_size(value, step_size):
        d_value = Decimal(str(value))
        d_step = Decimal(str(step_size))
        return float((d_value / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step)

load_dotenv()

API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
DRY_RUN = os.getenv('DRY_RUN', 'false').strip().lower() == 'true'
USE_TESTNET = os.getenv('USE_TESTNET', 'false').strip().lower() == 'true'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, 'position_state.json')
TRADE_LOG_FILE = os.path.join(BASE_DIR, 'trade_log.csv')

client = Client(API_KEY, API_SECRET, testnet=USE_TESTNET)
app = Flask(__name__)

state_lock = threading.Lock()
shutdown_event = threading.Event()
position_state = {}


def load_position_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.error(f"position_state.json unreadable ({e}), starting empty")
        return {}


def save_position_state():
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(position_state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def append_trade_log(symbol, event, price, qty):
    is_new = not os.path.exists(TRADE_LOG_FILE) or os.path.getsize(TRADE_LOG_FILE) == 0
    with open(TRADE_LOG_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(['time', 'symbol', 'event', 'price', 'qty'])
        writer.writerow([datetime.now().isoformat(), symbol, event, price, qty])


def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={
            'chat_id': TELEGRAM_CHAT_ID, 'text': msg
        })
    except Exception:
        pass


# Strategien
class Strategy:
    def signal(self, df): pass

class RSIStrategy(Strategy):
    def signal(self, df):
        rsi = ta.momentum.RSIIndicator(df['close'], window=14).rsi().iloc[-1]
        return ('BUY' if rsi < 30 else 'SELL' if rsi > 70 else 'HOLD'), rsi

class EMAStrategy(Strategy):
    def signal(self, df):
        short = df['close'].ewm(span=5).mean()
        long = df['close'].ewm(span=20).mean()
        if short.iloc[-1] > long.iloc[-1] and short.iloc[-2] <= long.iloc[-2]: return 'BUY', None
        if short.iloc[-1] < long.iloc[-1] and short.iloc[-2] >= long.iloc[-2]: return 'SELL', None
        return 'HOLD', None

class MACDStrategy(Strategy):
    def signal(self, df):
        macd = ta.trend.MACD(df['close'])
        diff = macd.macd_diff()
        if diff.iloc[-1] > 0 and diff.iloc[-2] <= 0: return 'BUY', None
        if diff.iloc[-1] < 0 and diff.iloc[-2] >= 0: return 'SELL', None
        return 'HOLD', None

class BollingerStrategy(Strategy):
    def signal(self, df):
        bb = ta.volatility.BollingerBands(df['close'])
        if df['close'].iloc[-1] > bb.bollinger_hband().iloc[-1]: return 'SELL', None
        if df['close'].iloc[-1] < bb.bollinger_lband().iloc[-1]: return 'BUY', None
        return 'HOLD', None

class MomentumStrategy(Strategy):
    def signal(self, df):
        roc = ta.momentum.ROCIndicator(df['close'], window=5).roc()
        if roc.iloc[-1] > 3: return 'BUY', None
        if roc.iloc[-1] < -3: return 'SELL', None
        return 'HOLD', None

strategy_classes = {
    'RSI': RSIStrategy,
    'EMA': EMAStrategy,
    'MACD': MACDStrategy,
    'BOLL': BollingerStrategy,
    'MOM': MomentumStrategy
}


# TradingPair mit Thread
class TradingPair(threading.Thread):
    def __init__(self, symbol, strategy_name='RSI', sl=2.0, tp=4.0):
        super().__init__(daemon=True)
        self.symbol = symbol
        self.strategy_name = strategy_name
        self.strategy = strategy_classes[strategy_name]()
        self.sl_percent = sl
        self.tp_percent = tp
        self.status = {}
        self.tick_size = None
        self.step_size = None
        self.min_notional = 0.0

        existing = position_state.get(symbol)
        self.position = existing if existing and existing.get('open') else None
        if self.position:
            logging.info(f"[{symbol}] Restored open position: {self.position}")

    def update_config(self, strategy_name, sl, tp):
        self.strategy_name = strategy_name
        self.strategy = strategy_classes[strategy_name]()
        self.sl_percent = sl
        self.tp_percent = tp

    def _load_symbol_filters(self):
        while not shutdown_event.is_set():
            try:
                info = client.get_symbol_info(self.symbol)
                tick = step = notional = None
                for f in info.get('filters', []):
                    ftype = f.get('filterType')
                    if ftype == 'PRICE_FILTER':
                        tick = float(f['tickSize'])
                    elif ftype == 'LOT_SIZE':
                        step = float(f['stepSize'])
                    elif ftype in ('MIN_NOTIONAL', 'NOTIONAL'):
                        notional = float(f.get('minNotional', f.get('notional', 0)))
                if not tick or not step:
                    raise ValueError("missing PRICE_FILTER/LOT_SIZE in symbol info")
                self.tick_size, self.step_size = tick, step
                self.min_notional = notional or 0.0
                logging.info(f"[{self.symbol}] filters loaded: tick={tick} step={step} minNotional={self.min_notional}")
                return True
            except Exception as e:
                logging.error(f"[{self.symbol}] failed to load symbol filters: {e}")
                shutdown_event.wait(10)
        return False

    def run(self):
        if not self._load_symbol_filters():
            return
        while not shutdown_event.is_set():
            try:
                self.tick()
            except Exception as e:
                logging.error(f"[{self.symbol}] {e}")
                send_telegram(f"[{self.symbol}] {e}")
            shutdown_event.wait(60)

    def fetch_data(self):
        klines = client.get_klines(symbol=self.symbol, interval='1m', limit=100)
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'qav', 'not', 'tbbav', 'tbqav', 'ignore'
        ])
        df['close'] = df['close'].astype(float)
        return df

    def tick(self):
        df = self.fetch_data()
        price = float(df['close'].iloc[-1])

        if self.position:
            self.status = {
                'signal': 'HOLD (position open)',
                'price': price,
                'rsi': None,
                'time': datetime.now(),
                'strategy': self.strategy_name,
                'sl': self.sl_percent,
                'tp': self.tp_percent,
                'position_open': True,
                'entry_price': self.position.get('entry_price')
            }
            self.check_position_closed(price)
            return

        sig, rsi = self.strategy.signal(df)
        self.status = {
            'signal': sig,
            'price': price,
            'rsi': round(rsi, 2) if rsi is not None else None,
            'time': datetime.now(),
            'strategy': self.strategy_name,
            'sl': self.sl_percent,
            'tp': self.tp_percent,
            'position_open': False,
            'entry_price': None
        }

        if sig == 'BUY':
            self.try_buy(price)

    def check_position_closed(self, price):
        pos = self.position
        closed = False
        reason = None

        if DRY_RUN:
            tp_price = pos.get('tp_price')
            sl_price = pos.get('sl_price')
            if tp_price and price >= tp_price:
                closed, reason = True, 'TP'
            elif sl_price and price <= sl_price:
                closed, reason = True, 'SL'
        else:
            oco_id = pos.get('oco_order_list_id')
            try:
                oco = client.v3_get_order_list(orderListId=oco_id)
                if oco.get('listOrderStatus') == 'ALL_DONE':
                    closed, reason = True, 'OCO_FILLED'
            except Exception as e:
                logging.error(f"[{self.symbol}] failed to check OCO status: {e}")
                return

        if closed:
            logging.info(f"[{self.symbol}] position closed ({reason}) qty={pos.get('qty')} entry={pos.get('entry_price')}")
            send_telegram(f"[{self.symbol}] position closed ({reason})")
            append_trade_log(self.symbol, f'CLOSE_{reason}', price, pos.get('qty'))
            with state_lock:
                position_state[self.symbol] = {'open': False}
                save_position_state()
            self.position = None

    def try_buy(self, price):
        with state_lock:
            open_symbols = {s for s, p in position_state.items() if p.get('open')}
            if self.symbol in open_symbols:
                return

            try:
                free = float(client.get_asset_balance(asset='USDC')['free'])
            except Exception as e:
                logging.error(f"[{self.symbol}] failed to fetch balance: {e}")
                return

            eligible = max(len(pairs) - len(open_symbols), 1)
            usdc_amount = free / eligible
            if usdc_amount <= 0:
                return

            buy_price = round_step_size(price, self.tick_size)
            qty = round_step_size(usdc_amount / price, self.step_size)
            if qty <= 0:
                return
            if self.min_notional and qty * buy_price < self.min_notional:
                logging.info(f"[{self.symbol}] order below MIN_NOTIONAL ({qty * buy_price:.2f} < {self.min_notional}), skipping")
                return

            sl_price = round_step_size(buy_price * (1 - self.sl_percent / 100), self.tick_size)
            tp_price = round_step_size(buy_price * (1 + self.tp_percent / 100), self.tick_size)

            try:
                if DRY_RUN:
                    order_id = f"DRYRUN-{int(time.time() * 1000)}"
                    oco_id = f"DRYRUN-{int(time.time() * 1000)}"
                    logging.info(f"[DRY RUN][{self.symbol}] would BUY qty={qty} @ {buy_price} | SL {sl_price} TP {tp_price}")
                else:
                    order = client.order_market_buy(symbol=self.symbol, quantity=qty)
                    order_id = order.get('orderId')
                    oco = client.create_oco_order(
                        symbol=self.symbol,
                        side=SIDE_SELL,
                        quantity=qty,
                        aboveType=ORDER_TYPE_LIMIT_MAKER,
                        abovePrice=str(tp_price),
                        belowType=ORDER_TYPE_STOP_LOSS_LIMIT,
                        belowStopPrice=str(sl_price),
                        belowPrice=str(sl_price),
                        belowTimeInForce=TIME_IN_FORCE_GTC
                    )
                    oco_id = oco.get('orderListId')
            except Exception as e:
                logging.error(f"[{self.symbol}] Order Error: {e}")
                send_telegram(f"[{self.symbol}] Order Error: {e}")
                return

            self.position = {
                'open': True,
                'entry_price': buy_price,
                'qty': qty,
                'buy_order_id': order_id,
                'oco_order_list_id': oco_id,
                'sl_percent': self.sl_percent,
                'tp_percent': self.tp_percent,
                'sl_price': sl_price,
                'tp_price': tp_price,
                'opened_at': datetime.now().isoformat()
            }
            position_state[self.symbol] = self.position
            save_position_state()

        send_telegram(f"[{self.symbol}] BUY at {buy_price} | SL {sl_price} | TP {tp_price}")
        append_trade_log(self.symbol, 'BUY', buy_price, qty)


# Konfiguration für Paare
pair_configs = {
    'BTCUSDC': {'strategy': 'RSI', 'sl': 2.0, 'tp': 4.0},
    'ETHUSDC': {'strategy': 'EMA', 'sl': 2.0, 'tp': 4.0},
    'BNBUSDC': {'strategy': 'MACD', 'sl': 2.0, 'tp': 4.0},
    'SOLUSDC': {'strategy': 'BOLL', 'sl': 2.0, 'tp': 4.0},
    'ADAUSDC': {'strategy': 'MOM', 'sl': 2.0, 'tp': 4.0}
}

pairs = {}


@app.route('/', methods=['GET', 'POST'])
def dashboard():
    if request.method == 'POST':
        for symbol in pair_configs.keys():
            strategy = request.form.get(f'strategy_{symbol}')
            sl_raw = request.form.get(f'sl_{symbol}')
            tp_raw = request.form.get(f'tp_{symbol}')
            try:
                sl = float(sl_raw)
                tp = float(tp_raw)
                if strategy not in strategy_classes:
                    raise ValueError(f"unknown strategy '{strategy}'")
                if not (0 < sl <= 50):
                    raise ValueError(f"sl out of range: {sl}")
                if not (0 < tp <= 100):
                    raise ValueError(f"tp out of range: {tp}")
            except (TypeError, ValueError) as e:
                logging.warning(f"Rejected config update for {symbol}: {e}")
                continue
            pair_configs[symbol] = {'strategy': strategy, 'sl': sl, 'tp': tp}
            if symbol in pairs:
                pairs[symbol].update_config(strategy, sl, tp)
        return redirect('/')
    statuses = {sym: pair.status for sym, pair in pairs.items()}
    return render_template('dashboard.html', configs=pair_configs, statuses=statuses)


def handle_signal(signum, frame):
    logging.info("Shutdown signal received")
    shutdown_event.set()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    position_state = load_position_state()

    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, AttributeError):
        pass

    mode_tags = ''.join(t for t in [' [DRY RUN]' if DRY_RUN else '', ' [TESTNET]' if USE_TESTNET else ''])
    send_telegram(f"Bot started{mode_tags}")

    for symbol, conf in pair_configs.items():
        bot = TradingPair(symbol, conf['strategy'], conf['sl'], conf['tp'])
        bot.start()
        pairs[symbol] = bot

    try:
        app.run(host='0.0.0.0', port=5000)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        for pair in pairs.values():
            pair.join(timeout=5)
        logging.info("Shutdown complete")
