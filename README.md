# Bitcoin DCA Bot

Buys a fixed dollar amount of Bitcoin on Binance spot at a set time each day,
records every fill, and reports over Telegram. No signals, no timing, no
discretion — it buys the same amount whatever the price is doing.

Stack sats responsibly. 🚀

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env    # then fill in your keys
```

Create the Binance API key with **Spot Trading enabled and withdrawals
disabled**, and restrict it to your server's IP. A key that cannot withdraw
cannot drain you if the server is compromised.

## Usage

```bash
python btc_dca.py              # place today's buy
python btc_dca.py --dry-run    # full preflight, places no order
python btc_dca.py --stats      # ledger summary, touches nothing
python btc_dca.py --backfill   # import past buys from Binance
```

Run `--backfill` once after setting up: it pulls your Binance trade history
into `btc_dca_ledger.csv` so the lifetime stats and cost basis include
everything you have ever bought, not just buys made from here on.

### Daily schedule

```cron
0 9 * * * cd /path/to/bitcoindcabot && /path/to/venv/bin/python btc_dca.py
```

The script exits non-zero when a buy fails, so a cron wrapper or systemd timer
with `OnFailure=` can alert you independently of Telegram.

## How it behaves

- **Spends the exact amount.** Orders use Binance's `quoteOrderQty`, so $21 of
  quote currency is $21 — the exchange handles lot sizing, and there is no
  client-side rounding to drift over or under budget.
- **Cannot double-buy.** Each day's order carries a deterministic client order
  id (`dca-YYYYMMDD`). If cron fires twice, or a retry follows a request that
  actually succeeded, Binance rejects the duplicate and the bot reports the
  original fill instead of buying again.
- **Retries transient failures.** Network blips, 5xx responses, rate limits and
  clock drift are retried with exponential backoff, so a hiccup no longer costs
  you a day. Real rejections (insufficient balance, bad symbol) fail fast.
- **Resyncs its clock.** A drifting server clock causes Binance error -1021 and
  is the usual reason a long-running bot goes quietly dead; the offset is
  corrected on every run and again on that error.
- **Reports what actually happened.** Telegram messages carry the real executed
  price, quantity and fee from the fill, not the pre-trade estimate.
- **Keeps a ledger.** Every buy is appended to `btc_dca_ledger.csv` with
  timestamp, order id, amount, price and fee — enough for cost basis and taxes.
- **Watches your runway.** Warns when the quote balance is down to a week of
  buys, and nudges you to move coins to cold storage once the on-exchange
  balance passes a threshold.

## Configuration

Everything is set through `.env` — see `.env.example` for the full list with
defaults. The common ones are `DCA_AMOUNT`, `DCA_SYMBOL`,
`DCA_WITHDRAW_REMINDER` and `DCA_LOW_BALANCE_DAYS`.

## Files

| File | Purpose |
| --- | --- |
| `btc_dca.py` | The bot |
| `btc_dca_ledger.csv` | Every buy, for cost basis (gitignored) |
| `btc_dca_run.log` | Rotating run log (gitignored) |
| `.env` | Your keys and settings (gitignored) |
