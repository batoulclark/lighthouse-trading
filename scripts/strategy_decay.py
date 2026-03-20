#!/usr/bin/env python3
"""
Lighthouse Trading — Strategy Decay Detector
=============================================
Detects when a strategy is losing its edge by comparing rolling 30-day
metrics against all-time metrics for each bot.

Decay is flagged when:
  - Rolling win rate drops > 10pp below all-time win rate
  - Rolling profit factor drops below 1.0
  - Rolling Sharpe ratio (annualized) drops below 0.5

Usage
-----
    python3 scripts/strategy_decay.py [--api-url URL]

Output
------
    data/strategy_decay.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_REPO_ROOT   = Path(__file__).resolve().parents[1]   # lighthouse-trading/
_OUTPUT_FILE = _REPO_ROOT / "data" / "strategy_decay.json"
_DEFAULT_URL = "http://127.0.0.1:8420/dashboard/trades?limit=500"

# ── Decay thresholds ──────────────────────────────────────────────────────────
_WIN_RATE_DROP_THRESHOLD  = 0.10   # 10 percentage points
_MIN_PROFIT_FACTOR        = 1.0
_MIN_SHARPE               = 0.5
_ROLLING_WINDOW_DAYS      = 30
_MIN_TRADES_FOR_METRICS   = 3      # minimum trades to compute meaningful metrics


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_trades(api_url: str) -> list[dict]:
    """Fetch trade list from the Lighthouse dashboard API."""
    try:
        with urllib.request.urlopen(api_url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data.get("trades", [])
    except urllib.error.URLError as exc:
        logger.error("Cannot reach API at %s: %s", api_url, exc)
        return []
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("Unexpected API response: %s", exc)
        return []


# ── PnL extraction ────────────────────────────────────────────────────────────

def _extract_pnl(trade: dict) -> float | None:
    """Extract the best available PnL value from a trade record.

    Prefers top-level ``pnl``, falls back to ``execution_result.pnl``.
    Returns None if no PnL is available (e.g. open-leg entry).
    """
    pnl = trade.get("pnl")
    if pnl is not None:
        try:
            return float(pnl)
        except (TypeError, ValueError):
            pass

    exec_result = trade.get("execution_result") or {}
    exec_pnl = exec_result.get("pnl")
    if exec_pnl is not None:
        try:
            return float(exec_pnl)
        except (TypeError, ValueError):
            pass

    return None


def _parse_timestamp(ts_raw: str) -> datetime | None:
    """Parse ISO-8601 timestamp into a timezone-aware datetime, or None."""
    if not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ── Trade grouping ────────────────────────────────────────────────────────────

def group_closed_trades(trades: list[dict]) -> dict[str, list[dict]]:
    """Return closed trades grouped by bot.

    A trade is considered "closed" (i.e. a completed round-trip) when it
    has a non-None PnL value (even if zero).  Open-leg entries (buy signals
    with no exit PnL) are excluded.

    Returns
    -------
    {
      bot_id: [
        { "bot_id": str, "bot_name": str, "pnl": float, "dt": datetime },
        ...
      ],
      ...
    }
    """
    by_bot: dict[str, list[dict]] = defaultdict(list)

    for trade in trades:
        pnl = _extract_pnl(trade)
        if pnl is None:
            continue   # open-leg entry — skip

        bot_id   = trade.get("bot_id") or "unknown"
        bot_name = trade.get("bot_name") or bot_id

        ts_raw = trade.get("timestamp") or trade.get("signal_timestamp") or ""
        dt = _parse_timestamp(ts_raw)
        if dt is None:
            logger.debug("Skipping trade with unparseable timestamp: %s", ts_raw)
            continue

        by_bot[bot_id].append({
            "bot_id":   bot_id,
            "bot_name": bot_name,
            "pnl":      pnl,
            "dt":       dt,
        })

    # Sort each bot's trades ascending by timestamp
    for bot_id in by_bot:
        by_bot[bot_id].sort(key=lambda t: t["dt"])

    return dict(by_bot)


# ── Metrics calculation ───────────────────────────────────────────────────────

def _win_rate(trade_list: list[dict]) -> float | None:
    """Fraction of trades with PnL > 0. Returns None if no trades."""
    if not trade_list:
        return None
    wins = sum(1 for t in trade_list if t["pnl"] > 0)
    return wins / len(trade_list)


def _profit_factor(trade_list: list[dict]) -> float | None:
    """Gross profit / gross loss. Returns None if no trades, 0.0 if all losses."""
    if not trade_list:
        return None
    gross_profit = sum(t["pnl"] for t in trade_list if t["pnl"] > 0)
    gross_loss   = abs(sum(t["pnl"] for t in trade_list if t["pnl"] < 0))
    if gross_loss == 0:
        # All winners or all break-even — PF is technically infinite / undefined
        # Return large finite value only if there are actual profits
        return float("inf") if gross_profit > 0 else None
    return gross_profit / gross_loss


def _daily_returns(trade_list: list[dict]) -> dict[str, float]:
    """Aggregate trade PnL into daily buckets { YYYY-MM-DD: pnl }."""
    daily: dict[str, float] = defaultdict(float)
    for t in trade_list:
        day = t["dt"].strftime("%Y-%m-%d")
        daily[day] += t["pnl"]
    return dict(daily)


def _sharpe_annualized(trade_list: list[dict]) -> float | None:
    """Annualized Sharpe ratio from daily PnL returns (risk-free rate = 0).

    Returns None if there are fewer than 2 trading days.
    """
    daily = _daily_returns(trade_list)
    returns = list(daily.values())
    n = len(returns)
    if n < 2:
        return None

    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)  # sample variance
    std_r = math.sqrt(variance)

    if std_r == 0:
        # Constant returns — Sharpe undefined; treat as 0 (no risk-adjusted edge)
        return 0.0

    # Annualise assuming 252 trading days
    return (mean_r / std_r) * math.sqrt(252)


def compute_metrics(trade_list: list[dict]) -> dict[str, Any]:
    """Compute win rate, profit factor, and Sharpe for a list of closed trades.

    Returns a dict with keys: wr, pf, sharpe, trade_count.
    Values are None when there's insufficient data.
    """
    n = len(trade_list)
    if n < _MIN_TRADES_FOR_METRICS:
        return {
            "wr":          None,
            "pf":          None,
            "sharpe":      None,
            "trade_count": n,
        }

    pf = _profit_factor(trade_list)
    # Cap infinite PF at a large sentinel for JSON serialisation
    if pf is not None and math.isinf(pf):
        pf = 9999.0

    return {
        "wr":          round(_win_rate(trade_list) or 0.0, 4),
        "pf":          round(pf, 4) if pf is not None else None,
        "sharpe":      round(_sharpe_annualized(trade_list) or 0.0, 4),
        "trade_count": n,
    }


# ── Decay detection ───────────────────────────────────────────────────────────

def detect_decay(
    alltime: dict[str, Any],
    rolling: dict[str, Any],
    has_sufficient_rolling_data: bool,
) -> tuple[bool, list[str]]:
    """Compare rolling metrics vs all-time to flag strategy decay.

    Parameters
    ----------
    alltime  : all-time metrics dict (wr, pf, sharpe)
    rolling  : rolling-30d metrics dict (wr, pf, sharpe)
    has_sufficient_rolling_data : True if rolling window has >= MIN_TRADES

    Returns
    -------
    (decaying, decay_reasons)
    """
    if not has_sufficient_rolling_data:
        return False, []

    reasons: list[str] = []

    # 1. Win rate drop > 10pp
    at_wr = alltime.get("wr")
    ro_wr = rolling.get("wr")
    if at_wr is not None and ro_wr is not None:
        if (at_wr - ro_wr) > _WIN_RATE_DROP_THRESHOLD:
            drop_pp = round((at_wr - ro_wr) * 100, 1)
            reasons.append(
                f"Win rate dropped {drop_pp}pp below all-time "
                f"({ro_wr*100:.1f}% vs {at_wr*100:.1f}%)"
            )

    # 2. Rolling PF < 1.0
    ro_pf = rolling.get("pf")
    if ro_pf is not None and ro_pf < _MIN_PROFIT_FACTOR:
        reasons.append(
            f"Profit factor below 1.0 (rolling={ro_pf:.2f})"
        )

    # 3. Rolling Sharpe < 0.5
    ro_sharpe = rolling.get("sharpe")
    if ro_sharpe is not None and ro_sharpe < _MIN_SHARPE:
        reasons.append(
            f"Sharpe ratio below 0.5 (rolling={ro_sharpe:.2f})"
        )

    return len(reasons) > 0, reasons


# ── Main analysis ─────────────────────────────────────────────────────────────

def run(api_url: str) -> dict:
    """Fetch trades, compute metrics, detect decay, return result dict."""
    logger.info("Fetching trades from %s", api_url)
    all_trades = fetch_trades(api_url)

    if not all_trades:
        logger.warning("No trades returned from API — writing empty result")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "bots":         [],
            "meta": {
                "total_trades_fetched":  0,
                "rolling_window_days":   _ROLLING_WINDOW_DAYS,
                "min_trades_for_metrics": _MIN_TRADES_FOR_METRICS,
            },
        }

    logger.info("Fetched %d trades total", len(all_trades))

    by_bot = group_closed_trades(all_trades)
    logger.info(
        "Found %d bots with closed trades: %s",
        len(by_bot),
        list(by_bot.keys()),
    )

    now_utc = datetime.now(timezone.utc)
    cutoff  = now_utc - timedelta(days=_ROLLING_WINDOW_DAYS)

    # Collect bot names for bots even with zero closed trades
    all_bot_ids: dict[str, str] = {}  # bot_id → bot_name
    for t in all_trades:
        bid   = t.get("bot_id") or "unknown"
        bname = t.get("bot_name") or bid
        all_bot_ids[bid] = bname

    bot_results: list[dict] = []

    for bot_id, bot_name in sorted(all_bot_ids.items(), key=lambda x: x[1]):
        closed_trades = by_bot.get(bot_id, [])

        # All-time metrics
        alltime_metrics = compute_metrics(closed_trades)

        # Rolling 30-day metrics
        rolling_trades = [t for t in closed_trades if t["dt"] >= cutoff]
        rolling_metrics = compute_metrics(rolling_trades)

        # Data coverage info
        trade_count    = len(closed_trades)
        rolling_count  = len(rolling_trades)
        has_30d_data   = rolling_count >= _MIN_TRADES_FOR_METRICS

        # Oldest trade date
        date_range: dict[str, Any] = {}
        if closed_trades:
            oldest = closed_trades[0]["dt"].strftime("%Y-%m-%d")
            newest = closed_trades[-1]["dt"].strftime("%Y-%m-%d")
            days_of_history = (now_utc - closed_trades[0]["dt"]).days
            date_range = {
                "first_trade": oldest,
                "last_trade":  newest,
                "days_of_history": days_of_history,
            }

        # Determine if we have < 30 days of data
        note: str | None = None
        if trade_count < _MIN_TRADES_FOR_METRICS:
            note = f"Insufficient data: only {trade_count} closed trade(s)"
        elif not has_30d_data:
            note = (
                f"Rolling window has only {rolling_count} trade(s) — "
                "decay detection may be unreliable"
            )
        elif date_range.get("days_of_history", 0) < _ROLLING_WINDOW_DAYS:
            note = (
                f"Less than {_ROLLING_WINDOW_DAYS} days of history "
                f"({date_range.get('days_of_history', 0)} days) — "
                "rolling metrics equal all-time metrics"
            )

        # Decay detection
        decaying, decay_reasons = detect_decay(
            alltime_metrics,
            rolling_metrics,
            has_sufficient_rolling_data=has_30d_data,
        )

        if decaying:
            logger.warning("⚠  DECAY detected for %s: %s", bot_name, decay_reasons)
        else:
            logger.info("✓  %s — no decay detected", bot_name)

        bot_entry: dict[str, Any] = {
            "bot_name":    bot_name,
            "bot_id":      bot_id,
            "alltime":     alltime_metrics,
            "rolling_30d": rolling_metrics,
            "decaying":    decaying,
            "decay_reasons": decay_reasons,
            **date_range,
        }
        if note:
            bot_entry["note"] = note

        bot_results.append(bot_entry)

    # Summary counts
    decaying_bots = [b["bot_name"] for b in bot_results if b["decaying"]]
    if decaying_bots:
        logger.warning(
            "Strategy decay detected in %d bot(s): %s",
            len(decaying_bots),
            decaying_bots,
        )
    else:
        logger.info("No strategy decay detected across %d bot(s)", len(bot_results))

    return {
        "generated_at": now_utc.isoformat(),
        "bots": bot_results,
        "meta": {
            "total_trades_fetched":   len(all_trades),
            "total_bots":             len(bot_results),
            "decaying_bot_count":     len(decaying_bots),
            "decaying_bots":          decaying_bots,
            "rolling_window_days":    _ROLLING_WINDOW_DAYS,
            "min_trades_for_metrics": _MIN_TRADES_FOR_METRICS,
            "thresholds": {
                "win_rate_drop_pp":   _WIN_RATE_DROP_THRESHOLD * 100,
                "min_profit_factor":  _MIN_PROFIT_FACTOR,
                "min_sharpe":         _MIN_SHARPE,
            },
        },
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect strategy decay by comparing rolling vs all-time metrics"
    )
    parser.add_argument(
        "--api-url",
        default=_DEFAULT_URL,
        help=f"Trade history endpoint (default: {_DEFAULT_URL})",
    )
    parser.add_argument(
        "--output",
        default=str(_OUTPUT_FILE),
        help=f"Output JSON path (default: {_OUTPUT_FILE})",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress INFO log output",
    )
    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    result = run(api_url=args.api_url)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2))
    logger.info("Written to %s", output_path)

    # Exit 1 if any bot is decaying (useful for cron/alerting)
    has_decay = result.get("meta", {}).get("decaying_bot_count", 0) > 0
    return 1 if has_decay else 0


if __name__ == "__main__":
    sys.exit(main())
