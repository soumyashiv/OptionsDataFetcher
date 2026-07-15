"""
nse_option_chain_fetcher.py — Production-grade NSE Option Chain Poller
=======================================================================

Fetches the NSE option chain for a configurable symbol every N seconds
(default: 60 s).  Reuses the same NSEClient across the entire lifetime
of the process so the underlying curl_cffi session stays warm.

Usage
-----
    python nse_option_chain_fetcher.py
    python nse_option_chain_fetcher.py --symbol BANKNIFTY --interval 30 --window 10

Environment overrides (via .env or shell exports)
--------------------------------------------------
    INDIAOPT_SYMBOLS=NIFTY
    INDIAOPT_POLL_INTERVAL_SECONDS=60
    INDIAOPT_MAX_RETRIES=3
    INDIAOPT_FETCH_TIMEOUT=15.0
    INDIAOPT_LOG_LEVEL=INFO
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from indiaopt import (
    CircuitOpenError,
    FetchError,
    FetchTimeoutError,
    IndiaOptError,
    NetworkError,
    NSEClient,
    ParseError,
    RateLimitError,
    Settings,
)
from indiaopt.models.option_chain import OptionChainResult, OptionChainRow

# ── Logging setup ─────────────────────────────────────────────────────────────

def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure and return the module-level logger with timestamps."""
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-8s  [NSE] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    return logging.getLogger("nse_fetcher")


# ── Display helpers ───────────────────────────────────────────────────────────

def _fmt(value: object, width: int = 10, precision: int = 2) -> str:
    """Format a numeric value for fixed-width tabular display."""
    if value is None:
        return "-".center(width)
    if isinstance(value, float):
        return f"{value:{width}.{precision}f}"
    return f"{value:{width},}"


def display_summary(result: OptionChainResult, logger: logging.Logger) -> None:
    """Log a concise summary of the option chain result."""
    logger.info(
        "── %s %s  spot=%.2f  ATM=%.0f  PCR=%.2f  max-pain=%.0f  "
        "CE-OI=%s  PE-OI=%s  expiry=%s ──",
        result.exchange,
        result.symbol,
        result.spot_price or 0,
        result.atm_strike or 0,
        result.pcr or 0,
        result.max_pain_strike or 0,
        f"{result.total_call_oi:,}",
        f"{result.total_put_oi:,}",
        result.expiry or "?",
    )


def display_strikes(rows: list[OptionChainRow], logger: logging.Logger) -> None:
    """Log a formatted option chain table for the given strike rows."""
    header = (
        f"{'STRIKE':>9} │ "
        f"{'CE-OI':>10} {'CE-COI':>10} {'CE-LTP':>8} {'CE-IV':>7} {'CE-VOL':>9} │ "
        f"{'PE-OI':>10} {'PE-COI':>10} {'PE-LTP':>8} {'PE-IV':>7} {'PE-VOL':>9}"
    )
    sep = "─" * len(header)
    logger.info(sep)
    logger.info(header)
    logger.info(sep)
    for row in rows:
        line = (
            f"{row.strike:>9.0f} │ "
            f"{_fmt(row.call_oi)} "
            f"{_fmt(row.call_coi)} "
            f"{_fmt(row.call_ltp, 8)} "
            f"{_fmt(row.call_iv, 7)} "
            f"{_fmt(row.call_vol, 9)} │ "
            f"{_fmt(row.put_oi)} "
            f"{_fmt(row.put_coi)} "
            f"{_fmt(row.put_ltp, 8)} "
            f"{_fmt(row.put_iv, 7)} "
            f"{_fmt(row.put_vol, 9)}"
        )
        logger.info(line)
    logger.info(sep)


# ── Output / Persistence helpers ─────────────────────────────────────────────

def save_result(
    result: OptionChainResult,
    *,
    json_path: Path | None,
    csv_path: Path | None,
    logger: logging.Logger,
) -> None:
    """Persist *result* to JSON and/or CSV files if paths are provided.

    Each call **appends** one snapshot to the CSV so you accumulate a
    time-series. JSON is **overwritten** each cycle so the file always
    holds the latest snapshot (easy to read with external tools).

    Args:
        result:    Parsed option chain result from the package.
        json_path: Destination ``*.json`` file, or ``None`` to skip.
        csv_path:  Destination ``*.csv`` file, or ``None`` to skip.
        logger:    Configured logger.
    """
    if json_path is not None:
        try:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            with json_path.open("w", encoding="utf-8") as fh:
                json.dump(result.to_dict(), fh, indent=2, default=str)
            logger.debug("JSON written -> %s", json_path)
        except OSError as exc:
            logger.error("Failed to write JSON to %s: %s", json_path, exc)

    if csv_path is not None:
        try:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not csv_path.exists()
            # Build flat rows: one row per strike with top-level metadata
            flat_rows = [
                {
                    "fetched_at": result.fetched_at.isoformat(),
                    "exchange": result.exchange,
                    "symbol": result.symbol,
                    "expiry": result.expiry,
                    "spot_price": result.spot_price,
                    "atm_strike": result.atm_strike,
                    "pcr": result.pcr,
                    **row.to_dict(),
                }
                for row in result.data
            ]
            with csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=flat_rows[0].keys())
                if write_header:
                    writer.writeheader()
                writer.writerows(flat_rows)
            logger.debug("CSV appended -> %s  (%d rows)", csv_path, len(flat_rows))
        except OSError as exc:
            logger.error("Failed to write CSV to %s: %s", csv_path, exc)


# ── Core fetch logic ──────────────────────────────────────────────────────────

async def fetch_and_display(
    client: NSEClient,
    symbol: str,
    is_index: bool,
    atm_window: int,
    logger: logging.Logger,
    *,
    json_path: Path | None = None,
    csv_path: Path | None = None,
) -> None:
    """Fetch a single option chain snapshot, display it, and optionally save.

    Propagates exceptions so the polling loop can handle back-off and circuit
    state correctly.

    Args:
        client:     Long-lived NSEClient instance (session reuse).
        symbol:     NSE trading symbol, e.g. ``"NIFTY"``.
        is_index:   ``True`` for index derivatives, ``False`` for equity.
        atm_window: Number of strikes on each side of ATM to display.
        logger:     Configured logger.
        json_path:  Optional path to save latest snapshot as JSON (overwritten).
        csv_path:   Optional path to append this snapshot to a CSV.
    """
    result: OptionChainResult = await client.fetch_option_chain(symbol, is_index=is_index)
    display_summary(result, logger)
    rows = result.atm_window(n=atm_window)
    display_strikes(rows, logger)
    save_result(result, json_path=json_path, csv_path=csv_path, logger=logger)


# ── Polling loop ──────────────────────────────────────────────────────────────

async def polling_loop(
    symbol: str,
    interval: int,
    is_index: bool,
    atm_window: int,
    settings: Settings,
    logger: logging.Logger,
    stop_event: asyncio.Event,
    *,
    json_path: Path | None = None,
    csv_path: Path | None = None,
) -> None:
    """Continuously fetch NSE option chain data on a fixed interval.

    Uses a single long-lived NSEClient so the underlying HTTP session
    (with its NSE cookies and warm-up state) is kept alive for the entire
    process lifetime.

    Args:
        symbol:     NSE trading symbol to poll.
        interval:   Seconds between fetches (minimum 10 s via Settings).
        is_index:   ``True`` for index instruments, ``False`` for equity.
        atm_window: Number of strikes on each side of ATM to display.
        settings:   indiaopt Settings instance.
        logger:     Configured logger.
        stop_event: Set this event to request a graceful shutdown.
        json_path:  Save latest snapshot as JSON on each cycle (overwritten).
        csv_path:   Append each snapshot to this CSV (one row per strike).
    """
    logger.info("Starting NSE option chain poller — symbol=%s  interval=%ds", symbol, interval)

    async with NSEClient(settings=settings) as client:
        logger.info("NSEClient ready (session warm-up complete).")
        cycle = 0

        while not stop_event.is_set():
            cycle += 1
            logger.info("── Cycle %d  [%s] ──", cycle, datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))

            try:
                await fetch_and_display(
                    client, symbol, is_index, atm_window, logger,
                    json_path=json_path,
                    csv_path=csv_path,
                )

            except CircuitOpenError as exc:
                # Don't hammer a dead exchange — just wait out the interval
                logger.warning("Circuit OPEN for %s: %s", symbol, exc)
                logger.warning("Will retry after circuit closes (~%ds).", exc.failures)

            except RateLimitError as exc:
                wait = exc.retry_after or interval
                logger.warning("Rate-limited by NSE: %s  Waiting %.0fs.", exc, wait)
                # We sleep below anyway; just log the extra context.

            except FetchTimeoutError as exc:
                logger.error("Fetch timed out: %s", exc)

            except (NetworkError, ParseError) as exc:
                logger.error("%s: %s", type(exc).__name__, exc)

            except FetchError as exc:
                logger.error("Fetch failed (all retries exhausted): %s", exc)
                logger.error("Circuit stats: %s", client.circuit_stats())

            except IndiaOptError as exc:
                logger.error("Unexpected library error: %s", exc)

            except Exception as exc:  # noqa: BLE001
                logger.exception("Unhandled exception in fetch cycle: %s", exc)

            # ── Wait for next cycle or stop signal ─────────────────────────
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=float(interval))

    logger.info("Polling loop stopped cleanly.")


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="NSE Option Chain Fetcher — powered by indiaopt",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbol", default="NIFTY", help="NSE trading symbol")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds (>= 10)")
    parser.add_argument("--window", type=int, default=5, help="Strikes above/below ATM to display")
    parser.add_argument("--equity", action="store_true", help="Treat symbol as equity (not index)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument(
        "--save-json",
        metavar="FILE",
        default=None,
        help="Path to save latest snapshot as JSON on every cycle (overwritten each time, e.g. nifty.json)",
    )
    parser.add_argument(
        "--save-csv",
        metavar="FILE",
        default=None,
        help="Path to append every snapshot to a CSV file (time-series, e.g. nifty.csv)",
    )
    return parser.parse_args()


async def main() -> None:
    """Async entry point."""
    args = parse_args()
    logger = configure_logging(args.log_level)

    # Build Settings from args (env vars / .env are also read automatically)
    settings = Settings(
        symbols=[args.symbol],
        poll_interval_seconds=max(10, args.interval),
        log_level=args.log_level,
    )

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        logger.info("Shutdown signal received — stopping after current cycle…")
        stop_event.set()

    # Register graceful shutdown handlers for Ctrl+C and SIGTERM
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, RuntimeError):
            # Windows fallback — signal handlers may not work in asyncio on Windows
            signal.signal(sig, _handle_signal)  # type: ignore[arg-type]

    await polling_loop(
        symbol=args.symbol.upper(),
        interval=settings.poll_interval_seconds,
        is_index=not args.equity,
        atm_window=args.window,
        settings=settings,
        logger=logger,
        stop_event=stop_event,
        json_path=Path(args.save_json) if args.save_json else None,
        csv_path=Path(args.save_csv) if args.save_csv else None,
    )


if __name__ == "__main__":
    asyncio.run(main())
