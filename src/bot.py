import os
import time
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

load_dotenv()

GCP_PROJECT_ID = os.getenv('GCP_PROJECT_ID')
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE')
STORAGE_BUCKET_NAME = os.getenv('STORAGE_BUCKET_NAME')


API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

client = Client(API_KEY, API_SECRET)
app = Flask(__name__)

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
        self.running = True

    def update_config(self, strategy_name, sl, tp):
        self.strategy_name = strategy_name
        self.strategy = strategy_classes[strategy_name]()
        self.sl_percent = sl
        self.tp_percent = tp

    def run(self):
        while self.running:
            try:
                usdc = float(client.get_asset_balance(asset='USDC')['free']) / len(pairs)
                self.trade(usdc)
            except Exception as e:
                logging.error(f"[{self.symbol}] {e}")
                send_telegram(f"[{self.symbol}] {e}")
            time.sleep(60)

    def fetch_data(self):
        klines = client.get_klines(symbol=self.symbol, interval='1m', limit=100)
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'qav', 'not', 'tbbav', 'tbqav', 'ignore'
        ])
        df['close'] = df['close'].astype(float)
        return df

    def trade(self, usdt_amount):
        df = self.fetch_data()
        signal, rsi = self.strategy.signal(df)
        price = df['close'].iloc[-1]
        qty = round(usdt_amount / price, 6)
        sl = round(price * (1 - self.sl_percent / 100), 2)
        tp = round(price * (1 + self.tp_percent / 100), 2)

        self.status = {
            'signal': signal,
            'price': price,
            'rsi': round(rsi, 2) if rsi else None,
            'time': datetime.now(),
            'strategy': self.strategy_name,
            'sl': self.sl_percent,
            'tp': self.tp_percent
        }

        if signal == 'BUY':
            try:
                client.order_market_buy(symbol=self.symbol, quantity=qty)
                client.create_oco_order(
                    symbol=self.symbol,
                    side=SIDE_SELL,
                    quantity=qty,
                    price=str(tp),
                    stopPrice=str(sl),
                    stopLimitPrice=str(sl),
                    stopLimitTimeInForce=TIME_IN_FORCE_GTC
                )
                send_telegram(f"[{self.symbol}] BUY at {price} | SL {sl} | TP {tp}")
            except Exception as e:
                logging.error(f"[{self.symbol}] Order Error: {e}")
                send_telegram(f"[{self.symbol}] Order Error: {e}")

def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={
            'chat_id': TELEGRAM_CHAT_ID, 'text': msg
        })
    except:
        pass

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
            sl = float(request.form.get(f'sl_{symbol}'))
            tp = float(request.form.get(f'tp_{symbol}'))
            pair_configs[symbol] = {'strategy': strategy, 'sl': sl, 'tp': tp}
            if symbol in pairs:
                pairs[symbol].update_config(strategy, sl, tp)
        return redirect('/')
    statuses = {sym: pair.status for sym, pair in pairs.items()}
    return render_template('dashboard.html', configs=pair_configs, statuses=statuses)

if __name__ == '__main__':
    send_telegram("Bot started")
    logging.basicConfig(level=logging.INFO)
    for symbol, conf in pair_configs.items():
        bot = TradingPair(symbol, conf['strategy'], conf['sl'], conf['tp'])
        bot.start()
        pairs[symbol] = bot
    app.run(host='0.0.0.0', port=5000)