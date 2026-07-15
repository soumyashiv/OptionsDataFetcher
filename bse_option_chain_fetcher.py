"""
bse_option_chain_fetcher.py — Production-grade BSE Option Chain Poller
=======================================================================

Fetches the BSE option chain for a configurable scrip code every N seconds
(default: 60 s).  Reuses the same BSEClient across the entire lifetime
of the process so the underlying curl_cffi session stays warm.

BSE uses numeric scrip codes rather than symbol names:
    999920  → SENSEX index derivatives
    999901  → BANKEX index derivatives

Usage
-----
    python bse_option_chain_fetcher.py
    python bse_option_chain_fetcher.py --scrip 999920 --interval 30 --window 10

Environment overrides (via .env or shell exports)
--------------------------------------------------
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
    BSEClient,
    CircuitOpenError,
    FetchError,
    FetchTimeoutError,
    IndiaOptError,
    NetworkError,
    ParseError,
    RateLimitError,
    Settings,
)
from indiaopt.models.option_chain import OptionChainResult, OptionChainRow

# ── Logging setup ─────────────────────────────────────────────────────────────

def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure and return the module-level logger with timestamps."""
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-8s  [BSE] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    return logging.getLogger("bse_fetcher")


# ── Display helpers ───────────────────────────────────────────────────────────

def _fmt(value: object, width: int = 10, precision: int = 2) -> str:
    """Format a numeric value for fixed-width tabular display."""
    if value is None:
        return "-".center(width)
    if isinstance(value, float):
        return f"{value:{width}.{precision}f}"
    return f"{value:{width},}"


def display_summary(result: OptionChainResult, logger: logging.Logger) -> None:
    """Log a concise summary of the BSE option chain result."""
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
    client: BSEClient,
    scrip: str,
    is_index: bool,
    atm_window: int,
    logger: logging.Logger,
    *,
    json_path: Path | None = None,
    csv_path: Path | None = None,
) -> None:
    """Fetch a single BSE option chain snapshot, display it, and optionally save.

    Args:
        client:     Long-lived BSEClient instance (session reuse).
        scrip:      BSE scrip code, e.g. ``"999920"`` for SENSEX derivatives.
        is_index:   ``True`` for index derivatives, ``False`` for equity.
        atm_window: Number of strikes on each side of ATM to display.
        logger:     Configured logger.
        json_path:  Optional path to save latest snapshot as JSON (overwritten).
        csv_path:   Optional path to append this snapshot to a CSV.
    """
    result: OptionChainResult = await client.fetch_option_chain(scrip, is_index=is_index)
    display_summary(result, logger)
    rows = result.atm_window(n=atm_window)
    display_strikes(rows, logger)
    save_result(result, json_path=json_path, csv_path=csv_path, logger=logger)


# ── Polling loop ──────────────────────────────────────────────────────────────

async def polling_loop(
    scrip: str,
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
    """Continuously fetch BSE option chain data on a fixed interval.

    Uses a single long-lived BSEClient so the underlying HTTP session
    stays alive for the entire process lifetime.

    Args:
        scrip:      BSE scrip code to poll.
        interval:   Seconds between fetches.
        is_index:   ``True`` for index instruments.
        atm_window: Number of strikes on each side of ATM to display.
        settings:   indiaopt Settings instance.
        logger:     Configured logger.
        stop_event: Set this event to request a graceful shutdown.
        json_path:  Save latest snapshot as JSON on each cycle (overwritten).
        csv_path:   Append each snapshot to this CSV (one row per strike).
    """
    logger.info("Starting BSE option chain poller — scrip=%s  interval=%ds", scrip, interval)

    async with BSEClient(settings=settings) as client:
        logger.info("BSEClient ready.")
        cycle = 0

        while not stop_event.is_set():
            cycle += 1
            logger.info("── Cycle %d  [%s] ──", cycle, datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))

            try:
                await fetch_and_display(
                    client, scrip, is_index, atm_window, logger,
                    json_path=json_path,
                    csv_path=csv_path,
                )

            except CircuitOpenError as exc:
                logger.warning("Circuit OPEN for %s: %s", scrip, exc)

            except RateLimitError as exc:
                wait = exc.retry_after or interval
                logger.warning("Rate-limited by BSE: %s  Waiting %.0fs.", exc, wait)

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
        description="BSE Option Chain Fetcher — powered by indiaopt",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--scrip",
        default="999920",
        help="BSE scrip code (e.g. 999920 = SENSEX, 999901 = BANKEX)",
    )
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds (>= 10)")
    parser.add_argument("--window", type=int, default=5, help="Strikes above/below ATM to display")
    parser.add_argument("--equity", action="store_true", help="Treat scrip as equity (not index)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument(
        "--save-json",
        metavar="FILE",
        default=None,
        help="Path to save latest snapshot as JSON on every cycle (overwritten each time, e.g. sensex.json)",
    )
    parser.add_argument(
        "--save-csv",
        metavar="FILE",
        default=None,
        help="Path to append every snapshot to a CSV file (time-series, e.g. sensex.csv)",
    )
    return parser.parse_args()


async def main() -> None:
    """Async entry point."""
    args = parse_args()
    logger = configure_logging(args.log_level)

    settings = Settings(
        poll_interval_seconds=max(10, args.interval),
        log_level=args.log_level,
    )

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        logger.info("Shutdown signal received — stopping after current cycle…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, _handle_signal)  # type: ignore[arg-type]

    await polling_loop(
        scrip=args.scrip,
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
