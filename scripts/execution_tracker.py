#!/usr/bin/env python3
"""
Lighthouse Trading — Execution Quality Tracker
================================================
Measures the quality of trade execution by analysing:
  - Signal-to-execution latency (signal_timestamp → fill timestamp)
  - Slippage (signal price vs fill price, when available)
  - Per-bot fill rate and missed-signal count
  - System-level aggregates
  - Webhook uptime from the monitor service

Output: data/execution_metrics.json

Usage:
    python scripts/execution_tracker.py              # analyses today's trades
    python scripts/execution_tracker.py --all        # analyses all trades
    python scripts/execution_tracker.py --days 7     # analyses last N days
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL        = "http://127.0.0.1:8420"
TRADES_ENDPOINT = f"{BASE_URL}/dashboard/trades"
MONITOR_ENDPOINT = f"{BASE_URL}/monitor/status"
OUTPUT_FILE     = os.path.join(
    os.path.dirname(__file__), "..", "data", "execution_metrics.json"
)
REQUEST_TIMEOUT = 10  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("execution_tracker")


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_trades(limit: int = 500) -> List[Dict[str, Any]]:
    """Fetch the most recent trades from the dashboard API."""
    try:
        resp = requests.get(
            TRADES_ENDPOINT,
            params={"limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("trades", [])
    except requests.exceptions.ConnectionError:
        logger.error("Cannot connect to Lighthouse Trading at %s — is the server running?", BASE_URL)
        return []
    except requests.exceptions.Timeout:
        logger.error("Request to %s timed out after %ds", TRADES_ENDPOINT, REQUEST_TIMEOUT)
        return []
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to fetch trades: %s", exc)
        return []
    except (ValueError, KeyError) as exc:
        logger.error("Unexpected trades response format: %s", exc)
        return []


def fetch_monitor_status() -> Dict[str, Any]:
    """Fetch the latest monitor status snapshot."""
    try:
        resp = requests.get(MONITOR_ENDPOINT, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Could not fetch monitor status: %s", exc)
        return {}


# ── Time helpers ──────────────────────────────────────────────────────────────

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string, returning None on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _latency_ms(trade: Dict[str, Any]) -> Optional[float]:
    """
    Return signal-to-execution latency in milliseconds.
    Latency = fill timestamp − signal_timestamp
    """
    fill_dt   = _parse_iso(trade.get("timestamp"))
    signal_dt = _parse_iso(trade.get("signal_timestamp"))
    if fill_dt is None or signal_dt is None:
        return None
    delta_ms = (fill_dt - signal_dt).total_seconds() * 1000.0
    # Sanity guard: reject obviously wrong values (negative or > 1 hour)
    if delta_ms < 0 or delta_ms > 3_600_000:
        return None
    return round(delta_ms, 2)


def _slippage(trade: Dict[str, Any]) -> Optional[float]:
    """
    Estimate slippage as (fill_price − signal_price) / signal_price × 100 (%).
    Requires both fill_price and a signal_price in the execution_result.
    Paper trades will usually show 0 slippage.
    """
    fill_price = trade.get("fill_price")
    if fill_price is None:
        return None

    # Try to find a reference signal price in execution_result
    exec_result = trade.get("execution_result") or {}
    signal_price = exec_result.get("signal_price") or exec_result.get("entry_price")

    if signal_price is None or signal_price == 0:
        return None

    try:
        slippage_pct = abs(float(fill_price) - float(signal_price)) / float(signal_price) * 100.0
        return round(slippage_pct, 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _is_fill(trade: Dict[str, Any]) -> bool:
    """A trade counts as a successful fill if it has no error and has a fill_price."""
    return trade.get("error") is None and trade.get("fill_price") is not None


# ── Period filtering ──────────────────────────────────────────────────────────

def filter_period(
    trades: List[Dict[str, Any]],
    days: Optional[int],
    today_only: bool,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Filter trades to the requested period.

    Returns (filtered_trades, period_label).
    """
    now = datetime.now(timezone.utc)

    if today_only:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = now.date().isoformat()
    elif days is not None:
        start = now - timedelta(days=days)
        label = f"last_{days}_days"
    else:
        return trades, "all"

    filtered = []
    for t in trades:
        dt = _parse_iso(t.get("timestamp"))
        if dt is not None and dt >= start:
            filtered.append(t)

    return filtered, label


# ── Webhook uptime ────────────────────────────────────────────────────────────

def compute_webhook_uptime(monitor: Dict[str, Any]) -> Optional[float]:
    """
    Estimate webhook uptime from the monitor status.

    The monitor tracks 'webhook_times' per bot (last observed latency in ms).
    If a bot has a recent webhook time it is considered "up".  Bots listed in
    'stale_bots' are considered "down".

    Returns uptime as a percentage (0–100), or None if no data.
    """
    webhook_times = monitor.get("webhook_times") or {}
    stale_bots    = set(monitor.get("stale_bots") or [])
    enabled_bots  = monitor.get("enabled_bots") or list(webhook_times.keys())

    if not enabled_bots:
        return None

    up_count = sum(
        1 for name in enabled_bots if name not in stale_bots
    )
    return round(up_count / len(enabled_bots) * 100.0, 1)


# ── Core analysis ─────────────────────────────────────────────────────────────

def analyse(
    trades: List[Dict[str, Any]],
    monitor: Dict[str, Any],
    period: str,
) -> Dict[str, Any]:
    """
    Compute execution quality metrics from the trade list.

    Parameters
    ----------
    trades  : trades already filtered to the desired period
    monitor : monitor status snapshot (may be empty)
    period  : human-readable period label

    Returns
    -------
    metrics dict matching the documented JSON schema.
    """
    # ── Per-bot accumulators ───────────────────────────────────────────────
    bot_meta:      Dict[str, Dict[str, str]]   = {}   # bot_id → {name, id}
    bot_signals:   Dict[str, int]              = defaultdict(int)
    bot_fills:     Dict[str, int]              = defaultdict(int)
    bot_latencies: Dict[str, List[float]]      = defaultdict(list)
    bot_slippages: Dict[str, List[float]]      = defaultdict(list)

    all_latencies: List[float] = []

    for trade in trades:
        bot_id   = trade.get("bot_id",   "unknown")
        bot_name = trade.get("bot_name", "unknown")

        # Store bot metadata (last-seen name wins)
        bot_meta[bot_id] = {"bot_id": bot_id, "bot_name": bot_name}

        # Every trade record represents a signal that was received
        bot_signals[bot_id] += 1

        # Fill check
        if _is_fill(trade):
            bot_fills[bot_id] += 1

        # Latency
        lat = _latency_ms(trade)
        if lat is not None:
            bot_latencies[bot_id].append(lat)
            all_latencies.append(lat)

        # Slippage
        slip = _slippage(trade)
        if slip is not None:
            bot_slippages[bot_id].append(slip)

    # ── Per-bot summary ────────────────────────────────────────────────────
    per_bot: List[Dict[str, Any]] = []
    all_bot_ids = set(bot_signals) | set(bot_meta)

    for bot_id in sorted(all_bot_ids):
        signals   = bot_signals.get(bot_id, 0)
        fills     = bot_fills.get(bot_id, 0)
        missed    = signals - fills
        latencies = bot_latencies.get(bot_id, [])
        slippages = bot_slippages.get(bot_id, [])
        meta      = bot_meta.get(bot_id, {"bot_id": bot_id, "bot_name": "unknown"})

        fill_rate = round(fills / signals * 100.0, 2) if signals > 0 else 0.0
        avg_lat   = round(sum(latencies) / len(latencies), 2) if latencies else None
        avg_slip  = round(sum(slippages) / len(slippages), 4) if slippages else None

        entry: Dict[str, Any] = {
            "bot_name":      meta["bot_name"],
            "bot_id":        bot_id,
            "signals":       signals,
            "fills":         fills,
            "fill_rate_pct": fill_rate,
            "avg_latency_ms": avg_lat,
            "missed":        max(missed, 0),
        }
        if avg_slip is not None:
            entry["avg_slippage_pct"] = avg_slip

        per_bot.append(entry)

    # ── System-level aggregates ────────────────────────────────────────────
    total_signals = sum(bot_signals.values())
    total_fills   = sum(bot_fills.values())
    fill_rate_sys = round(total_fills / total_signals * 100.0, 2) if total_signals > 0 else 0.0
    avg_lat_sys   = round(sum(all_latencies) / len(all_latencies), 2) if all_latencies else None
    webhook_uptime = compute_webhook_uptime(monitor)

    system: Dict[str, Any] = {
        "total_signals":     total_signals,
        "total_fills":       total_fills,
        "fill_rate_pct":     fill_rate_sys,
        "avg_latency_ms":    avg_lat_sys,
        "webhook_uptime_pct": webhook_uptime,
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period":       period,
        "system":       system,
        "per_bot":      per_bot,
    }


# ── Output ────────────────────────────────────────────────────────────────────

def write_output(metrics: Dict[str, Any], path: str) -> None:
    """Write metrics to JSON, creating parent directories if needed."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(metrics, fh, indent=2)
    logger.info("Execution metrics written to %s", path)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lighthouse Trading — Execution Quality Tracker")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--today", action="store_true", default=True,
        help="Analyse only today's trades (default)",
    )
    group.add_argument(
        "--all", action="store_true",
        help="Analyse all trades in the log",
    )
    group.add_argument(
        "--days", type=int, metavar="N",
        help="Analyse trades from the last N days",
    )
    parser.add_argument(
        "--limit", type=int, default=500,
        help="Maximum number of trades to fetch (default: 500)",
    )
    parser.add_argument(
        "--output", type=str, default=OUTPUT_FILE,
        help="Output JSON path (default: data/execution_metrics.json)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress non-error log output",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    # Determine period
    if args.all:
        today_only, days = False, None
    elif args.days is not None:
        today_only, days = False, args.days
    else:
        today_only, days = True, None

    logger.info("Fetching up to %d trades…", args.limit)
    trades = fetch_trades(limit=args.limit)
    logger.info("Fetched %d trade records", len(trades))

    monitor = fetch_monitor_status()

    filtered, period = filter_period(trades, days=days, today_only=today_only)
    logger.info("Analysing %d trades for period: %s", len(filtered), period)

    metrics = analyse(filtered, monitor, period)

    # Log summary
    sys_m = metrics["system"]
    logger.info(
        "System: %d signals | %d fills | %.1f%% fill rate | latency=%s ms | uptime=%s%%",
        sys_m["total_signals"],
        sys_m["total_fills"],
        sys_m["fill_rate_pct"],
        sys_m["avg_latency_ms"],
        sys_m["webhook_uptime_pct"],
    )
    for bot in metrics["per_bot"]:
        logger.info(
            "  %-25s  signals=%-3d  fills=%-3d  fill_rate=%-6s%%  lat=%s ms  missed=%d",
            bot["bot_name"],
            bot["signals"],
            bot["fills"],
            bot["fill_rate_pct"],
            bot["avg_latency_ms"],
            bot["missed"],
        )

    write_output(metrics, args.output)


if __name__ == "__main__":
    main()
