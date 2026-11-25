# Daily Markets Reporter

Dependency-free Python script that periodically fetches news headlines for a
set of stock tickers and emails a digest containing only items not seen on a
previous run. Scheduled hourly during US market hours (9:30–16:00 ET) via cron.

## Files

| File             | Purpose                                                        |
|------------------|----------------------------------------------------------------|
| `stock_news.py`  | Main script (Python 3.10+ stdlib only)                         |
| `config.json`    | Tickers, recipients, SMTP settings                             |
| `.env`           | SMTP credentials (`SMTP_USER`, `SMTP_PASS`) — mode 600         |
| `run.sh`         | Cron wrapper (absolute python path, cd to this dir)            |
| `state.json`     | Seen-link hashes + last successful run — created at runtime    |
| `cron.log`       | Cron output — created at runtime                               |

## Setup (one-time)

1. **Enable 2-Step Verification** on the Gmail account you will send from:
   <https://myaccount.google.com/security> → *2-Step Verification*.

2. **Create an app password**: <https://myaccount.google.com/apppasswords>
   — generate one named e.g. "stock-news". It is displayed as 16 characters
   with spaces; **remove the spaces** when you enter it.

3. **Fill in `.env`**:
   ```
   SMTP_USER=you@gmail.com
   SMTP_PASS=abcdefghijklmnop        # 16 chars, no spaces
   ```

4. **Fill in `config.json`**:
   - `to`: recipient address(es), e.g. `["you@gmail.com"]`
   - `from`: optional display-name+address; defaults to `SMTP_USER` if empty
   - `tickers`: edit freely (add/remove any ticker)
   - `ticker_labels`: optional human names per ticker

5. **Test a dry run** (prints the digest, sends nothing, touches no state):
   ```
   python3 stock_news.py --dry-run
   ```

6. **Test a real send**:
   ```
   python3 stock_news.py            # sends if there are new items
   python3 stock_news.py --force    # sends even if nothing is new
   ```

## Usage

```
python3 stock_news.py             # fetch, dedupe, email (if anything new)
python3 stock_news.py --dry-run   # fetch + print digest; send nothing
python3 stock_news.py --force     # email even when there is nothing new
```

Exit codes: `0` success / nothing-new / dry-run · `1` config or send failure ·
`2` all tickers failed to fetch.

## Behaviour

- **Sources**: Yahoo Finance RSS first, Google News RSS as fallback per ticker.
- **Dedupe**: SHA-1 of each link is remembered in `state.json`; only unseen
  links are included. State is saved **only after a successful send**, so a
  failed send retries the same items on the next run. Seen hashes are pruned
  after 14 days / 8000 entries.
- **Window**: each run includes items published since the previous successful
  run; the first run looks back `first_run_lookback_hours` (default 12h).
- **Nothing new**: email is skipped unless `send_empty` is `true` or
  `--force` is given.

## Cron

The server timezone is `America/Los_Angeles` (constant 3h behind ET year-round),
so market hours map to:

```
# min  hour   day month weekday  command
30 6-12 * * 1-5  /home/k0k0/opencode/stock-news/run.sh >> /home/k0k0/opencode/stock-news/cron.log 2>&1
# optional close digest at 16:00 ET:
0 13 * * 1-5   /home/k0k0/opencode/stock-news/run.sh >> /home/k0k0/opencode/stock-news/cron.log 2>&1
```

That runs at 9:30, 10:30, …, 15:30 ET, weekdays only.

Manage with:

```
crontab -l                 # view
crontab -e                 # edit
crontab -r                 # remove entirely
```

## Logs

All cron runs append to `cron.log` in this directory. While `.env` /
`config.json` are not filled in, runs will log a clear error
(`SMTP credentials missing` / `no recipients configured`) — that is expected
and harmless; the run simply does not send.