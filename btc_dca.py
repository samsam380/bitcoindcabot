#!/usr/bin/env python3
"""Daily Bitcoin DCA on Binance spot.

Buys a fixed amount of quote currency (default $21 USDT) worth of BTC, records
the fill in a CSV ledger, and reports the result over Telegram.

Usage:
    python btc_dca.py              # place today's buy
    python btc_dca.py --dry-run    # full preflight, no order placed
    python btc_dca.py --stats      # print ledger summary, touches no funds
    python btc_dca.py --backfill   # import past buys from Binance into ledger
"""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import requests
from binance.client import Client
from binance.exceptions import (
    BinanceAPIException,
    BinanceOrderException,
    BinanceRequestException,
)
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = os.getenv("DCA_SYMBOL", "BTCUSDT")
AMOUNT = float(os.getenv("DCA_AMOUNT", "21"))
# Optional label so several runs a day each get their own idempotency key.
SLOT = os.getenv("DCA_SLOT", "")
RECV_WINDOW = int(os.getenv("DCA_RECV_WINDOW", "10000"))
MAX_RETRIES = int(os.getenv("DCA_MAX_RETRIES", "5"))
LOW_BALANCE_DAYS = float(os.getenv("DCA_LOW_BALANCE_DAYS", "7"))
# Nudge to move coins off the exchange once the balance passes this. 0 disables.
WITHDRAW_REMINDER = float(os.getenv("DCA_WITHDRAW_REMINDER", "0.01"))

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.getenv("DCA_LOG_FILE", os.path.join(HERE, "btc_dca_run.log"))
LEDGER_FILE = os.getenv("DCA_LEDGER_FILE", os.path.join(HERE, "btc_dca_ledger.csv"))

LEDGER_FIELDS = [
    "timestamp_utc",
    "order_id",
    "symbol",
    "quote_spent",
    "base_qty",
    "avg_price",
    "fee",
    "fee_asset",
    "source",
]

# Transient Binance error codes worth retrying: disconnects, timeouts, rate
# limits and clock drift. Everything else is a real rejection.
RETRYABLE_CODES = {-1000, -1001, -1003, -1006, -1007, -1015, -1016, -1021}

logger = logging.getLogger("btc_dca")


def setup_logging():
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)


# 📲 Telegram notify
def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing. Skipping notification.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    for attempt in range(3):
        try:
            resp = requests.post(url, data=payload, timeout=15)
            if resp.ok:
                return
            logger.error(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"❌ Failed to send Telegram message: {e}")
        if attempt < 2:
            time.sleep(2 ** attempt)


def is_retryable(exc):
    if isinstance(exc, (requests.exceptions.RequestException, BinanceRequestException)):
        return True
    if isinstance(exc, BinanceAPIException):
        status = getattr(exc, "status_code", 0) or 0
        return exc.code in RETRYABLE_CODES or status >= 500
    return False


def with_retries(fn, what, client=None, retries=None):
    """Call fn(), retrying transient failures with exponential backoff."""
    retries = MAX_RETRIES if retries is None else retries
    last = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            if not is_retryable(e):
                raise
            # A drifted clock is the classic silent killer on a long-lived VPS.
            if client is not None and isinstance(e, BinanceAPIException) and e.code == -1021:
                sync_time(client)
            if attempt == retries - 1:
                break
            delay = 2 ** attempt
            logger.warning(f"{what} failed ({e}); retrying in {delay}s")
            time.sleep(delay)
    raise last


def sync_time(client):
    """Align the signing timestamp with Binance's clock."""
    try:
        server_ms = client.get_server_time()["serverTime"]
        client.timestamp_offset = server_ms - int(time.time() * 1000)
        logger.info(f"Clock offset vs Binance: {client.timestamp_offset} ms")
    except Exception as e:
        logger.warning(f"Could not sync time with Binance: {e}")


def sats(btc):
    return int(round(btc * 100_000_000))


def fmt_usd(x):
    return f"${x:,.2f}"


def read_ledger():
    if not os.path.exists(LEDGER_FILE):
        return []
    with open(LEDGER_FILE, newline="") as f:
        return list(csv.DictReader(f))


def write_ledger(rows):
    tmp = LEDGER_FILE + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, LEDGER_FILE)


def append_ledger(row):
    """Append a fill, skipping orders already recorded."""
    existing = read_ledger()
    if any(r.get("order_id") == str(row["order_id"]) for r in existing):
        logger.info(f"Order {row['order_id']} already in ledger; not re-recording.")
        return
    is_new = not os.path.exists(LEDGER_FILE)
    with open(LEDGER_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def ledger_stats(symbol=None):
    total_quote = 0.0
    total_base = 0.0
    count = 0
    first = None
    for row in read_ledger():
        if symbol and row.get("symbol") != symbol:
            continue
        try:
            total_quote += float(row["quote_spent"])
            total_base += float(row["base_qty"])
        except (KeyError, TypeError, ValueError):
            continue
        count += 1
        if first is None:
            first = row.get("timestamp_utc")
    avg = total_quote / total_base if total_base else 0.0
    return {
        "buys": count,
        "quote": total_quote,
        "base": total_base,
        "avg_price": avg,
        "first": first,
    }


def parse_fill(order):
    """Pull the real executed numbers out of an order response."""
    base_qty = float(order.get("executedQty", 0) or 0)
    quote_spent = float(order.get("cummulativeQuoteQty", 0) or 0)
    avg_price = quote_spent / base_qty if base_qty else 0.0
    fee = 0.0
    fee_assets = set()
    for fill in order.get("fills", []) or []:
        fee += float(fill.get("commission", 0) or 0)
        if fill.get("commissionAsset"):
            fee_assets.add(fill["commissionAsset"])
    fee_asset = fee_assets.pop() if len(fee_assets) == 1 else ("MIXED" if fee_assets else "")
    return base_qty, quote_spent, avg_price, fee, fee_asset


def client_order_id(now):
    """Deterministic per-day id so a retry can never double-buy."""
    stamp = now.strftime("%Y%m%d")
    return f"dca-{stamp}-{SLOT}" if SLOT else f"dca-{stamp}"


def get_free(client, asset):
    bal = with_retries(
        lambda: client.get_asset_balance(asset=asset, recvWindow=RECV_WINDOW),
        f"balance {asset}",
        client,
    )
    return float(bal["free"]) if bal else 0.0


def min_notional_for(symbol_info):
    for f in symbol_info["filters"]:
        if f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
            value = f.get("minNotional")
            if value is not None:
                return float(value)
    return 0.0


def build_report(base_asset, quote_asset, base_qty, quote_spent, avg_price, fee,
                 fee_asset, stats, quote_free, base_free, dry_run):
    header = "🧪 DRY RUN — no order placed" if dry_run else "✅ DCA filled"
    lines = [
        f"{header} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        f"Spent: {fmt_usd(quote_spent)} {quote_asset}",
        f"Got: {base_qty:.8f} {base_asset} ({sats(base_qty):,} sats)",
        f"Price: {fmt_usd(avg_price)}",
    ]
    if fee:
        lines.append(f"Fee: {fee:.8f} {fee_asset}")
    if stats["base"]:
        lines += [
            "",
            f"Lifetime: {stats['base']:.8f} {base_asset} ({sats(stats['base']):,} sats)",
            f"Invested: {fmt_usd(stats['quote'])} over "
            f"{stats['buys']} buy{'s' if stats['buys'] != 1 else ''}",
            f"Avg cost: {fmt_usd(stats['avg_price'])}",
        ]
    lines.append("")
    runway = quote_free / AMOUNT if AMOUNT else 0
    lines.append(f"{quote_asset} left: {fmt_usd(quote_free)} ({int(runway)} more buys)")
    if runway < LOW_BALANCE_DAYS:
        lines.append(f"⚠️ Top up {quote_asset} — under {LOW_BALANCE_DAYS:g} buys left.")
    if WITHDRAW_REMINDER and base_free >= WITHDRAW_REMINDER:
        lines.append(
            f"🔐 {base_free:.8f} {base_asset} sitting on Binance — "
            "time to move it to cold storage."
        )
    return "\n".join(lines)


def buy_btc_dca(dry_run=False):
    now = datetime.now(timezone.utc)
    client = Client(API_KEY, API_SECRET)
    sync_time(client)

    symbol_info = with_retries(
        lambda: client.get_symbol_info(SYMBOL), "get_symbol_info", client
    )
    if not symbol_info:
        raise RuntimeError(f"Unknown symbol {SYMBOL}")
    base_asset = symbol_info["baseAsset"]
    quote_asset = symbol_info["quoteAsset"]

    min_notional = min_notional_for(symbol_info)
    if AMOUNT < min_notional:
        raise RuntimeError(
            f"DCA_AMOUNT {AMOUNT} is below the {SYMBOL} minimum of {min_notional}"
        )

    quote_free = get_free(client, quote_asset)
    if quote_free < AMOUNT:
        raise RuntimeError(
            f"Insufficient {quote_asset}: have {quote_free:.2f}, need {AMOUNT:.2f}"
        )

    order_id = client_order_id(now)

    if dry_run:
        price = float(
            with_retries(
                lambda: client.get_symbol_ticker(symbol=SYMBOL), "ticker", client
            )["price"]
        )
        base_qty, quote_spent, avg_price, fee, fee_asset = (
            AMOUNT / price, AMOUNT, price, 0.0, ""
        )
        logger.info(f"Dry run: would buy {fmt_usd(AMOUNT)} of {SYMBOL} as {order_id}")
    else:
        logger.info(f"Buying {fmt_usd(AMOUNT)} of {SYMBOL} (order id {order_id})")
        # quoteOrderQty spends the exact amount; Binance handles lot sizing, so
        # there is no client-side rounding to drift over or under budget.
        def place():
            return client.order_market_buy(
                symbol=SYMBOL,
                quoteOrderQty=f"{AMOUNT:.8f}".rstrip("0").rstrip("."),
                newClientOrderId=order_id,
                recvWindow=RECV_WINDOW,
            )

        try:
            order = with_retries(place, "order_market_buy", client)
        except BinanceAPIException as e:
            if "duplicate" in str(e.message).lower():
                # Already bought under this id — a retry or a second cron fire.
                logger.warning(f"Order {order_id} already exists; reporting existing fill.")
                order = with_retries(
                    lambda: client.get_order(
                        symbol=SYMBOL, origClientOrderId=order_id, recvWindow=RECV_WINDOW
                    ),
                    "get_order",
                    client,
                )
            else:
                raise
        logger.info(f"Order response: {order}")
        base_qty, quote_spent, avg_price, fee, fee_asset = parse_fill(order)
        if base_qty <= 0:
            raise RuntimeError(f"Order {order_id} did not fill: {order}")

        append_ledger(
            {
                "timestamp_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
                "order_id": order.get("orderId", order_id),
                "symbol": SYMBOL,
                "quote_spent": f"{quote_spent:.8f}",
                "base_qty": f"{base_qty:.8f}",
                "avg_price": f"{avg_price:.2f}",
                "fee": f"{fee:.8f}",
                "fee_asset": fee_asset,
                "source": "dca",
            }
        )

    stats = ledger_stats(SYMBOL)
    quote_free = get_free(client, quote_asset)
    base_free = get_free(client, base_asset)

    report = build_report(
        base_asset, quote_asset, base_qty, quote_spent, avg_price, fee, fee_asset,
        stats, quote_free, base_free, dry_run,
    )
    logger.info(report.replace("\n", " | "))
    send_telegram_message(report)


def backfill():
    """Import past buys from Binance trade history into the ledger."""
    client = Client(API_KEY, API_SECRET)
    sync_time(client)

    trades = []
    from_id = 0
    while True:
        batch = with_retries(
            lambda: client.get_my_trades(
                symbol=SYMBOL, fromId=from_id, limit=1000, recvWindow=RECV_WINDOW
            ),
            "get_my_trades",
            client,
        )
        if not batch:
            break
        trades.extend(batch)
        from_id = max(int(t["id"]) for t in batch) + 1
        if len(batch) < 1000:
            break

    orders = {}
    for t in trades:
        if not t.get("isBuyer"):
            continue
        oid = str(t["orderId"])
        agg = orders.setdefault(
            oid,
            {"qty": 0.0, "quote": 0.0, "fee": 0.0, "assets": set(), "time": int(t["time"])},
        )
        agg["qty"] += float(t["qty"])
        agg["quote"] += float(t["quoteQty"])
        agg["fee"] += float(t["commission"])
        agg["assets"].add(t["commissionAsset"])
        agg["time"] = min(agg["time"], int(t["time"]))

    existing = read_ledger()
    known = {r.get("order_id") for r in existing}
    added = []
    for oid, agg in orders.items():
        if oid in known or agg["qty"] <= 0:
            continue
        assets = agg["assets"]
        added.append(
            {
                "timestamp_utc": datetime.fromtimestamp(
                    agg["time"] / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "order_id": oid,
                "symbol": SYMBOL,
                "quote_spent": f"{agg['quote']:.8f}",
                "base_qty": f"{agg['qty']:.8f}",
                "avg_price": f"{agg['quote'] / agg['qty']:.2f}",
                "fee": f"{agg['fee']:.8f}",
                "fee_asset": next(iter(assets)) if len(assets) == 1 else "MIXED",
                "source": "backfill",
            }
        )

    rows = sorted(existing + added, key=lambda r: r["timestamp_utc"])
    write_ledger(rows)
    logger.info(f"Backfill added {len(added)} orders ({len(rows)} total in ledger).")
    print_stats()


def print_stats():
    stats = ledger_stats(SYMBOL)
    if not stats["buys"]:
        logger.info("Ledger is empty. Run --backfill to import Binance history.")
        return
    logger.info(f"Buys recorded : {stats['buys']} (since {stats['first']})")
    logger.info(f"Total invested: {fmt_usd(stats['quote'])}")
    logger.info(f"Total stacked : {stats['base']:.8f} ({sats(stats['base']):,} sats)")
    logger.info(f"Average cost  : {fmt_usd(stats['avg_price'])}")


def main():
    parser = argparse.ArgumentParser(description="Binance Bitcoin DCA bot")
    parser.add_argument("--dry-run", action="store_true", help="preflight without buying")
    parser.add_argument("--stats", action="store_true", help="print ledger summary")
    parser.add_argument("--backfill", action="store_true", help="import Binance history")
    args = parser.parse_args()

    setup_logging()

    if args.stats:
        print_stats()
        return 0

    if not API_KEY or not API_SECRET:
        logger.error("BINANCE_API_KEY / BINANCE_API_SECRET are not set.")
        return 1

    dry_run = args.dry_run or os.getenv("DCA_DRY_RUN") in ("1", "true", "True")

    try:
        if args.backfill:
            backfill()
        else:
            buy_btc_dca(dry_run=dry_run)
        return 0
    except (BinanceAPIException, BinanceOrderException, BinanceRequestException) as e:
        msg = f"❌ Binance error: {e}"
        logger.error(msg, exc_info=e)
    except Exception as e:
        msg = f"❌ DCA failed: {e}"
        logger.error(msg, exc_info=e)

    send_telegram_message(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
