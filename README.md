# HOW TO USE — NSE & BSE Option Chain Fetchers

> Powered by the `indiaopt` package — an enterprise-grade library for Indian stock market data.

---

## Overview

This repository contains two production-ready polling scripts:

| Script | Exchange | Default Symbol | API Used |
|---|---|---|---|
| `nse_option_chain_fetcher.py` | NSE | `NIFTY` (index) | `NSEClient.fetch_option_chain()` |
| `bse_option_chain_fetcher.py` | BSE | `999920` (SENSEX) | `BSEClient.fetch_option_chain()` |

Both scripts:
- Fetch a full option chain snapshot every **60 seconds** (configurable).
- Reuse a **single HTTP session** for the entire process — no reconnection overhead.
- Handle **rate limits, network failures, and exchange downtime** automatically via the built-in circuit breaker and retry policy.
- Display a clean tabular view of strikes around the ATM level.
- Shut down cleanly on **Ctrl+C** or `SIGTERM`.

---

## Requirements

- **Python 3.10+**
- The `indiaopt` package (already installed in this project)
- No additional dependencies are needed — everything used by the scripts is part of the package.

---

## Installation

### 1. Install the `indiaopt` package

If you are running inside this repository, install it in editable mode:

```bash
pip install -e .[all]
```

Or install the published version from PyPI:

```bash
pip install indiaopt
```

### 2. (Optional) Create a virtual environment first

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install indiaopt
```

---

## Configuration

Both scripts read configuration from three sources in priority order:

1. **Command-line arguments** (highest priority)
2. **Environment variables** prefixed with `INDIAOPT_`
3. A **`.env` file** in the current working directory
4. **Built-in defaults** (lowest priority)

### All configurable settings

| CLI Argument | Env Variable | Default | Description |
|---|---|---|---|
| `--symbol` | `INDIAOPT_SYMBOLS` | `NIFTY` | NSE symbol to watch |
| `--scrip` | _(BSE only)_ | `999920` | BSE scrip code |
| `--interval` | `INDIAOPT_POLL_INTERVAL_SECONDS` | `60` | Seconds between fetches (min 10) |
| `--window` | _(script only)_ | `5` | ATM ± N strikes to display |
| `--equity` | _(script only)_ | `False` | Flag: treat as equity not index |
| `--log-level` | `INDIAOPT_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `--save-json` | _(script only)_ | _None_ | File path to overwrite latest snapshot as JSON |
| `--save-csv` | _(script only)_ | _None_ | File path to append all snapshots to CSV |
| _(n/a)_ | `INDIAOPT_MAX_RETRIES` | `3` | Max retry attempts per fetch |
| _(n/a)_ | `INDIAOPT_FETCH_TIMEOUT` | `15.0` | Per-request HTTP timeout (s) |
| _(n/a)_ | `INDIAOPT_PROXY_URLS` | _(none)_ | Comma-separated proxy URLs |

### Example `.env` file

```ini
INDIAOPT_MAX_RETRIES=5
INDIAOPT_FETCH_TIMEOUT=20.0
INDIAOPT_LOG_LEVEL=DEBUG
INDIAOPT_PROXY_URLS=http://proxy1:8080,http://proxy2:8080
```

---

## Running the NSE Fetcher

### Step 1 — Basic run (NIFTY, 60 s interval)
```bash
python nse_option_chain_fetcher.py
```

### Step 2 — Custom symbol and interval
```bash
python nse_option_chain_fetcher.py --symbol BANKNIFTY --interval 30
```

### Step 3 — Show more strikes and enable debug logging
```bash
python nse_option_chain_fetcher.py --symbol NIFTY --window 10 --log-level DEBUG
```

### Step 4 — Equity derivative and saving to files
```bash
python nse_option_chain_fetcher.py --symbol RELIANCE --equity --interval 60 --save-json data/reliance.json --save-csv data/reliance.csv
```

### Step 5 — Stop the fetcher
Press `Ctrl+C` — the script will finish the current cycle and exit cleanly.

### Expected terminal output

```
2026-07-15 20:00:00  INFO      [NSE] Starting NSE option chain poller — symbol=NIFTY  interval=60s
2026-07-15 20:00:05  INFO      [NSE] NSEClient ready (session warm-up complete).
2026-07-15 20:00:05  INFO      [NSE] ── Cycle 1  [14:30:05 UTC] ──
2026-07-15 20:00:07  INFO      [NSE] ── NSE NIFTY  spot=24850.30  ATM=24850  PCR=1.23  max-pain=24800  CE-OI=2,341,200  PE-OI=2,879,800  expiry=27-Jun-2024 ──
2026-07-15 20:00:07  INFO      [NSE] ─────────────────────────────────────────────────────────────────────────────────
2026-07-15 20:00:07  INFO      [NSE]    STRIKE │     CE-OI     CE-COI   CE-LTP    CE-IV    CE-VOL │     PE-OI     PE-COI   PE-LTP    PE-IV    PE-VOL
2026-07-15 20:00:07  INFO      [NSE] ─────────────────────────────────────────────────────────────────────────────────
2026-07-15 20:00:07  INFO      [NSE]     24600 │     45,200      1,200    15.50    18.20     3,400 │    120,100     -5,000   210.50    22.10    15,200
2026-07-15 20:00:07  INFO      [NSE]     24700 │     78,100      3,400    35.00    17.50     8,200 │     98,000     -3,200   155.00    21.30    12,100
2026-07-15 20:00:07  INFO      [NSE]     24800 │    145,000      8,700    75.00    16.80    22,000 │     78,000      2,100    95.00    20.50    18,000
2026-07-15 20:00:07  INFO      [NSE]     24900 │    201,000     12,000   130.00    16.20    35,000 │     55,000      1,200    55.00    19.80    12,000
2026-07-15 20:00:07  INFO      [NSE]     25000 │    350,000     25,000   200.00    15.80    65,000 │     30,000        800    25.00    18.90     8,000
```

---

## Running the BSE Fetcher

### Step 1 — Basic run (SENSEX, 60 s interval)
```bash
python bse_option_chain_fetcher.py
```

### Step 2 — Custom scrip and interval
```bash
python bse_option_chain_fetcher.py --scrip 999901 --interval 30
```
> `999901` is the BSE scrip code for **BANKEX** index derivatives.

### Step 3 — Enable debug logging and saving
```bash
python bse_option_chain_fetcher.py --scrip 999920 --log-level DEBUG --save-json sensex.json
```

### Step 4 — Stop the fetcher
Press `Ctrl+C`.

### Expected terminal output

```
2026-07-15 20:00:00  INFO      [BSE] Starting BSE option chain poller — scrip=999920  interval=60s
2026-07-15 20:00:03  INFO      [BSE] BSEClient ready.
2026-07-15 20:00:03  INFO      [BSE] ── Cycle 1  [14:30:03 UTC] ──
2026-07-15 20:00:05  INFO      [BSE] ── BSE 999920  spot=81234.50  ATM=81200  PCR=0.98  max-pain=81000  CE-OI=542,000  PE-OI=531,000  expiry=27-Jun-2024 ──
2026-07-15 20:00:05  INFO      [BSE] ─────────────────────────────────────────────────────────────────────────────────
2026-07-15 20:00:05  INFO      [BSE]    STRIKE │     CE-OI ...
```

---

## Customization

### Change the symbol (NSE)
```python
# In the script or via CLI:
python nse_option_chain_fetcher.py --symbol BANKNIFTY
python nse_option_chain_fetcher.py --symbol RELIANCE --equity
```

### Change the scrip code (BSE)
```python
python bse_option_chain_fetcher.py --scrip 999901   # BANKEX
```

### Change the polling interval
```bash
python nse_option_chain_fetcher.py --interval 30   # Every 30 seconds
```

### Access specific fields from OptionChainResult
```python
from indiaopt import NSEClient

async with NSEClient() as client:
    result = await client.fetch_option_chain("NIFTY")

    print(result.spot_price)       # Underlying spot price
    print(result.atm_strike)       # At-the-money strike
    print(result.pcr)              # Overall Put-Call ratio
    print(result.max_pain_strike)  # Max pain (highest total OI)
    print(result.total_call_oi)    # Total call OI across all strikes
    print(result.total_put_oi)     # Total put OI across all strikes
    print(result.expiry)           # Nearest expiry date string
    print(result.expiry_dates)     # All available expiry dates

    # Iterate all strikes
    for row in result.data:
        print(row.strike, row.call_oi, row.put_oi, row.call_ltp, row.put_ltp)

    # Get ATM ± 5 strikes only
    for row in result.atm_window(n=5):
        print(row.strike, row.call_oi, row.put_oi)
```

### Save data to CSV
```python
import csv
from indiaopt import NSEClient

async with NSEClient() as client:
    result = await client.fetch_option_chain("NIFTY")

with open("nifty_chain.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=result.data[0].to_dict().keys())
    writer.writeheader()
    for row in result.data:
        writer.writerow(row.to_dict())
```

### Save data to JSON
```python
import json
from indiaopt import NSEClient

async with NSEClient() as client:
    result = await client.fetch_option_chain("NIFTY")

with open("nifty_chain.json", "w") as f:
    json.dump(result.to_dict(), f, indent=2)
```

### Save data to SQLite
```python
import sqlite3
from indiaopt import NSEClient

async with NSEClient() as client:
    result = await client.fetch_option_chain("NIFTY")

conn = sqlite3.connect("options.db")
conn.execute("""
    CREATE TABLE IF NOT EXISTS option_chain (
        fetched_at TEXT, symbol TEXT, exchange TEXT, expiry TEXT,
        strike REAL, call_oi INTEGER, call_coi INTEGER, call_ltp REAL,
        call_iv REAL, call_vol INTEGER, put_oi INTEGER, put_coi INTEGER,
        put_ltp REAL, put_iv REAL, put_vol INTEGER
    )
""")
rows = [
    (
        result.fetched_at.isoformat(), result.symbol, result.exchange, result.expiry,
        row.strike, row.call_oi, row.call_coi, row.call_ltp,
        row.call_iv, row.call_vol, row.put_oi, row.put_coi,
        row.put_ltp, row.put_iv, row.put_vol,
    )
    for row in result.data
]
conn.executemany("INSERT INTO option_chain VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
conn.commit()
conn.close()
```

### Integrate with another project
```python
from indiaopt import NSEClient, Settings

# Override settings programmatically
settings = Settings(
    max_retries=5,
    fetch_timeout=20.0,
    circuit_failure_threshold=3,
    circuit_backoff_seconds=120.0,
)

async with NSEClient(settings=settings) as client:
    result = await client.fetch_option_chain("NIFTY")
    # Pass result to your own processing pipeline
    await your_pipeline.process(result)
```

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'indiaopt'`
Install the package:
```bash
pip install indiaopt
# or from this repo:
pip install -e .
```

### `pydantic_core.ValidationError: async_timeout must be greater than fetch_timeout`
Your fetch timeout is larger than the async timeout. Fix in `.env`:
```ini
INDIAOPT_FETCH_TIMEOUT=15.0
# async_timeout defaults to 60.0 which is > 15.0 ✓
```

### `FetchError: All N attempts failed`
- The exchange may be down for maintenance (typically 2:00–8:00 AM IST).
- Your IP may be rate-limited. Try adding proxy URLs via `INDIAOPT_PROXY_URLS`.
- Enable debug logging: `--log-level DEBUG` to see the raw HTTP responses.

### `CircuitOpenError: Circuit breaker OPEN`
Too many consecutive failures opened the circuit. The library will automatically retry after the backoff period (default 60 s). To reset manually:
```python
client.reset_circuit("NIFTY")
```

### `ParseError: Invalid JSON from NSE`
NSE returned a CAPTCHA or maintenance page instead of JSON. This happens when:
- Your IP is flagged as a bot.
- NSE is under maintenance.
- You are fetching too aggressively. Increase the interval.

### `FetchTimeoutError`
Network is slow or the exchange is overloaded. Increase the timeout:
```ini
INDIAOPT_FETCH_TIMEOUT=30.0
```

### Invalid symbol / scrip
- NSE symbols must be uppercase: `NIFTY`, `BANKNIFTY`, `RELIANCE`
- BSE uses numeric scrip codes: `999920` (SENSEX), `999901` (BANKEX)
- For equity derivatives, pass `--equity` flag.

---

## Best Practices for Production

1. **Use a `.env` file** — never hardcode credentials or proxy URLs.
2. **Set a polling interval ≥ 60 s** — fetching more often risks IP bans.
3. **Use proxy rotation** — for sustained production loads, configure `INDIAOPT_PROXY_URLS`.
4. **Monitor circuit breaker stats** — call `client.circuit_stats()` in your monitoring pipeline.
5. **Log to a file** — redirect stdout or configure a file handler for long-running processes:
   ```bash
   python nse_option_chain_fetcher.py >> logs/nse_fetcher.log 2>&1
   ```
6. **Run as a systemd service** on Linux for auto-restart and log management.
7. **Do not share the NSEClient across threads** — it is not designed for concurrent access across multiple event loops.
8. **Reset the settings cache** when testing:
   ```python
   from indiaopt.config.settings import get_settings
   get_settings.cache_clear()
   ```
