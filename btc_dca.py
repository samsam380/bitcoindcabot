import os
import logging
from datetime import datetime
from binance.client import Client
from dotenv import load_dotenv
from binance.exceptions import BinanceAPIException, BinanceOrderException

# Load environment variables
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

client = Client(API_KEY, API_SECRET)

# ✅ Set up logging to file
log_file = os.path.join(os.path.dirname(__file__), 'btc_dca_run.log')
logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

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
            logging.warning(f"Calculated amount {btc_amount} BTC is below minimum {min_qty} BTC. Skipping order.")
            return

        logging.info(f"Buying ${amount_usdt} BTC at ${btc_price:.2f} (~{btc_amount} BTC)")

        order = client.order_market_buy(
            symbol='BTCUSDT',
            quantity=str(btc_amount)
        )

        logging.info("✅ Order complete:")
        logging.info(order)

    except BinanceAPIException as e:
        logging.error(f"❌ Binance API Exception: {e}")
    except BinanceOrderException as e:
        logging.error(f"❌ Binance Order Exception: {e}")
    except Exception as e:
        logging.error(f"❌ General Exception: {e}")

if __name__ == "__main__":
    buy_btc_dca()
