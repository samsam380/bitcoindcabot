import os
import logging
from datetime import datetime
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

# ✅ Set up logging to file + console
log_file = os.path.join(os.path.dirname(__file__), 'btc_dca_run.log')
logger = logging.getLogger()
logger.setLevel(logging.INFO)

fh = logging.FileHandler(log_file)
fh.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

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

def buy_btc_dca(amount_usdt=21):
    try:
        btc_price = float(client.get_symbol_ticker(symbol="BTCUSDT")['price'])
        btc_amount = amount_usdt / btc_price

        symbol_info = client.get_symbol_info('BTCUSDT')
        lot_size_filter = next(f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE')
        min_qty = float(lot_size_filter['minQty'])
        step_size = float(lot_size_filter['stepSize'])

        step_size_str = '{:f}'.format(step_size)
        precision = len(step_size_str.split('.')[1].rstrip('0')) if '.' in step_size_str else 0
        btc_amount = round(btc_amount, precision)

        if btc_amount < min_qty:
            message = f"⚠️ Skipped: {btc_amount} BTC is below minimum {min_qty} BTC."
            logging.warning(message)
            send_telegram_message(message)
            return

        logging.info(f"Buying ${amount_usdt} BTC at ${btc_price:.2f} (~{btc_amount} BTC)")

        order = client.order_market_buy(
            symbol='BTCUSDT',
            quantity=str(btc_amount)
        )

        success_msg = f"✅ Bought ${amount_usdt} BTC at ${btc_price:.2f} (~{btc_amount} BTC)"
        logging.info("✅ Order complete:")
        logging.info(order)
        send_telegram_message(success_msg)

    except BinanceAPIException as e:
        error_msg = f"❌ Binance API Exception: {e}"
        logging.error(error_msg)
        send_telegram_message(error_msg)
    except BinanceOrderException as e:
        error_msg = f"❌ Binance Order Exception: {e}"
        logging.error(error_msg)
        send_telegram_message(error_msg)
    except Exception as e:
        error_msg = f"❌ General Exception: {e}"
        logging.error(error_msg)
        send_telegram_message(error_msg)

if __name__ == "__main__":
    buy_btc_dca()


