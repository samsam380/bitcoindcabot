import os
import time
import logging
from datetime import datetime, timedelta
from binance.client import Client
from dotenv import load_dotenv
from binance.exceptions import BinanceAPIException, BinanceOrderException
import requests

# Load environment variables
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = Client(API_KEY, API_SECRET)

# ✅ Logging to file + console
log_file = 'btc_dca2_run.log'
logger = logging.getLogger()
logger.setLevel(logging.INFO)

fh = logging.FileHandler(log_file)
fh.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

if logger.hasHandlers():
    logger.handlers.clear()

logger.addHandler(fh)
logger.addHandler(ch)

# 📲 Telegram notify
def send_telegram_message(message):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logging.warning("Telegram credentials missing. Skipping notification.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        requests.post(url, data=payload)
    except Exception as e:
        logging.error(f"❌ Failed to send Telegram message: {e}")

# Track last buy datetime
import re

log_file_path = '/media/sam/PortableSSD/bots/btc_dca2_run.log'
last_buy_time = None

def load_last_buy_time_from_log():
    global last_buy_time
    try:
        with open(log_file_path, 'r') as log_file:
            lines = log_file.readlines()[::-1]  # reverse search
            for line in lines:
                if "✅ BTC DCA2: Bought" in line:
                    # Extract timestamp using regex
                    match = re.match(r'\[(.*?)\]', line)
                    if match:
                        timestamp_str = match.group(1)
                        last_buy_time = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                        logging.info(f"📦 Loaded last buy time from log: {last_buy_time}")
                        return
        logging.info("ℹ️ No previous buy found in log.")
    except Exception as e:
        logging.warning(f"⚠️ Failed to read last buy time from log: {e}")

# check 24 hour price average

def get_24h_average_price():
    try:
        klines = client.get_klines(symbol='BTCUSDT', interval=Client.KLINE_INTERVAL_1HOUR, limit=24)
        prices = [ (float(k[2]) + float(k[3])) / 2 for k in klines ]  # Average of high and low per hour
        avg_24h = sum(prices) / len(prices)
        return avg_24h
    except Exception as e:
        logging.error(f"❌ Failed to fetch 24h price data: {e}")
        send_telegram_message(f"❌ Failed to fetch 24h average price: {e}")
        return None

def execute_buy(amount_usdt=15):
    try:
        btc_price = float(client.get_symbol_ticker(symbol="BTCUSDT")['price'])
        btc_amount = amount_usdt / btc_price

        symbol_info = client.get_symbol_info('BTCUSDT')
        lot_size_filter = next(f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE')
        min_qty = float(lot_size_filter['minQty'])
        step_size = float(lot_size_filter['stepSize'])

        precision = len('{:f}'.format(step_size).split('.')[1].rstrip('0'))
        btc_amount = round(btc_amount, precision)

        if btc_amount < min_qty:
            msg = f"⚠️ Buy skipped: {btc_amount} BTC is below min qty {min_qty} BTC."
            logging.warning(msg)
            send_telegram_message(msg)
            return False

        logging.info(f"📉 Buying ${amount_usdt} BTC at ${btc_price:.2f} (~{btc_amount} BTC)")
        order = client.order_market_buy(
            symbol='BTCUSDT',
            quantity=str(btc_amount)
        )

        msg = f"✅ BTC DCA2: Bought ${amount_usdt} BTC at ${btc_price:.2f} (~{btc_amount} BTC)"
        logging.info("✅ Order executed:")
        logging.info(order)
        send_telegram_message(msg)
        return True

    except BinanceAPIException as e:
        msg = f"❌ Binance API Exception: {e}"
        logging.error(msg)
        send_telegram_message(msg)
    except BinanceOrderException as e:
        msg = f"❌ Binance Order Exception: {e}"
        logging.error(msg)
        send_telegram_message(msg)
    except Exception as e:
        msg = f"❌ General Exception: {e}"
        logging.error(msg)
        send_telegram_message(msg)
    return False

def should_buy():
    global last_buy_time

    try:
        avg_price = get_24h_average_price()
        if not avg_price:
            return False
        current_price = float(client.get_symbol_ticker(symbol="BTCUSDT")['price'])

        price_drop_threshold = avg_price * 0.99
        now = datetime.now()

        logging.info(f"Current: ${current_price:.2f}, 24h Avg: ${avg_price:.2f}, Trigger Below: ${price_drop_threshold:.2f}")

        if last_buy_time and (now - last_buy_time) < timedelta(hours=24):
            time_remaining = 24 - (now - last_buy_time).total_seconds() / 3600
            logging.info(f"⏳ Bought recently. Waiting {time_remaining:.2f} more hours.")
            return False

        if current_price < price_drop_threshold:
            logging.info("✅ Dip detected AND 24h elapsed. Executing buy.")
            return True
        else:
            logging.info("❌ No dip. Price not low enough to trigger buy.")
            return False

    except Exception as e:
        msg = f"❌ Price check failed: {e}"
        logging.error(msg)
        send_telegram_message(msg)
        return False

if __name__ == "__main__":
    logging.info("🚀 BTC DCA 2.0 Bot Started — checking every 15 minutes")

    load_last_buy_time_from_log() 

    while True:
        if should_buy():
            if execute_buy():
                last_buy_time = datetime.now()
        time.sleep(900)

