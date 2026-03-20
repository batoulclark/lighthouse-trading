"""
Lighthouse Trading — Daily P&L Report Generator
================================================
Fetches closed trades from the live API or a local file, computes daily
and aggregate performance metrics, and writes a structured JSON report.

Usage
-----
    python3 scripts/daily_pnl_report.py [--url URL] [--output PATH] [--limit N]

Can also be imported and called programmatically:
    from scripts.daily_pnl_report import generate_report, generate_report_from_file
    report = generate_report()                            # via HTTP (standalone)
    report = generate_report_from_file("/path/to/trades.json")  # direct file read
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("daily_pnl_report")

# ── Defaults ──────────────────────────────────────────────────────────────────

_HERE           = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT   = os.path.normpath(os.path.join(_HERE, ".."))

DEFAULT_API_URL     = "http://127.0.0.1:8420/dashboard/trades?limit=500"
DEFAULT_TRADES_FILE = os.path.join(_PROJECT_ROOT, "data", "trades.json")
DEFAULT_OUTPUT      = os.path.join(_PROJECT_ROOT, "data", "daily_report.json")

TRADING_DAYS_PER_YEAR = 252


# ── Trade helpers ─────────────────────────────────────────────────────────────

def _extract_pnl(trade: Dict[str, Any]) -> Optional[float]:
    """
    Return the realised PnL for a trade, or None if unavailable.

    Only returns a value for genuine closes where execution_result.side == 'close'
    and execution_result.pnl is a number.  Top-level pnl is checked first for
    exchanges that populate it directly.
    """
    # 1. Top-level pnl (some exchanges set this)
    pnl = trade.get("pnl")
    if pnl is not None:
        try:
            return float(pnl)
        except (TypeError, ValueError):
            pass

    # 2. execution_result.pnl — only when side == 'close' (genuine close)
    er   = trade.get("execution_result") or {}
    side = (er.get("side") or "").lower()
    if side == "close":
        er_pnl = er.get("pnl")
        if er_pnl is not None:
            try:
                return float(er_pnl)
            except (TypeError, ValueError):
                pass

    return None


def _is_closed_trade(trade: Dict[str, Any]) -> bool:
    """
    True only for trades that represent a genuine position close with a
    realised PnL (including zero-PnL closes).

    Filters out:
    - Entry (buy) signals
    - Failed exits (error, no open position, zero quantity)
    """
    er   = trade.get("execution_result") or {}
    side = (er.get("side") or "").lower()

    # execution_result.side == 'close' is the definitive marker
    if side == "close":
        # Must have a numeric pnl (can be 0.0 for no-move closes)
        pnl = er.get("pnl")
        return pnl is not None

    # Fallback: top-level pnl set explicitly (non-null)
    top_pnl = trade.get("pnl")
    if top_pnl is not None:
        try:
            float(top_pnl)
            return True
        except (TypeError, ValueError):
            pass

    return False


def _trade_date(trade: Dict[str, Any]) -> str:
    """Return YYYY-MM-DD from a trade's timestamp."""
    ts = trade.get("timestamp", "")
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except (ValueError, AttributeError):
        return str(ts)[:10]


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_trades_from_api(url: str, timeout: int = 10) -> List[Dict[str, Any]]:
    """Fetch trade list from the Lighthouse dashboard API."""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data   = resp.json()
        trades = data.get("trades", [])
        logger.info("Fetched %d trades from %s", len(trades), url)
        return trades
    except requests.RequestException as exc:
        logger.error("Failed to fetch trades from API: %s", exc)
        return []
    except (ValueError, KeyError) as exc:
        logger.error("Unexpected API response format: %s", exc)
        return []


def fetch_trades_from_file(path: str) -> List[Dict[str, Any]]:
    """Load trade list directly from the local trades JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Trades file is a plain list; API endpoint wraps in {"trades": [...]}
        if isinstance(data, list):
            trades = data
        else:
            trades = data.get("trades", [])
        logger.info("Loaded %d trades from %s", len(trades), path)
        return trades
    except (OSError, ValueError) as exc:
        logger.error("Failed to load trades file: %s", exc)
        return []


# ── Daily grouping ────────────────────────────────────────────────────────────

def build_daily_data(
    closed_trades: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """
    Group closed trades by date.

    Returns
    -------
    daily_rows : list of {date, pnl, cumulative, trades}
    date_pnl   : mapping date → daily pnl  (used by metric calculators)
    """
    date_pnl:    Dict[str, float] = defaultdict(float)
    date_trades: Dict[str, int]   = defaultdict(int)

    for trade in closed_trades:
        date = _trade_date(trade)
        if not date:
            continue
        pnl = _extract_pnl(trade)
        pnl = pnl if pnl is not None else 0.0
        date_pnl[date]    += pnl
        date_trades[date] += 1

    sorted_dates = sorted(date_pnl.keys())
    cumulative   = 0.0
    daily_rows: List[Dict[str, Any]] = []

    for date in sorted_dates:
        pnl        = date_pnl[date]
        cumulative += pnl
        daily_rows.append({
            "date":       date,
            "pnl":        round(pnl, 6),
            "cumulative": round(cumulative, 6),
            "trades":     date_trades[date],
        })

    return daily_rows, dict(date_pnl)


# ── Performance metrics ───────────────────────────────────────────────────────

def calc_sharpe(daily_pnls: List[float], annualise: bool = True) -> Optional[float]:
    """
    Annualised Sharpe ratio (risk-free rate = 0).
    Returns None when there is insufficient data or zero volatility.
    """
    n = len(daily_pnls)
    if n < 2:
        return None
    mean     = sum(daily_pnls) / n
    variance = sum((x - mean) ** 2 for x in daily_pnls) / (n - 1)
    std      = math.sqrt(variance)
    if std == 0:
        return None
    sharpe = mean / std
    if annualise:
        sharpe *= math.sqrt(TRADING_DAYS_PER_YEAR)
    return round(sharpe, 4)


def calc_sortino(daily_pnls: List[float], annualise: bool = True) -> Optional[float]:
    """
    Annualised Sortino ratio (target return = 0, downside deviation only).
    Returns None when there is insufficient data or no losing days.
    """
    n = len(daily_pnls)
    if n < 2:
        return None
    mean         = sum(daily_pnls) / n
    downside_sq  = [min(x, 0.0) ** 2 for x in daily_pnls]
    dd_var       = sum(downside_sq) / n
    dd_std       = math.sqrt(dd_var)
    if dd_std == 0:
        return None
    sortino = mean / dd_std
    if annualise:
        sortino *= math.sqrt(TRADING_DAYS_PER_YEAR)
    return round(sortino, 4)


def calc_max_drawdown(cumulative_pnls: List[float]) -> Tuple[float, int]:
    """
    Compute maximum drawdown (absolute) and max duration in calendar days.

    Returns
    -------
    max_dd   : float — worst peak-to-trough drop (absolute value)
    duration : int   — longest consecutive days spent below the previous peak
    """
    if not cumulative_pnls:
        return 0.0, 0

    peak           = cumulative_pnls[0]
    max_dd         = 0.0
    max_duration   = 0
    current_dur    = 0

    for val in cumulative_pnls:
        if val >= peak:
            peak        = val
            current_dur = 0
        else:
            current_dur += 1
            dd           = peak - val
            max_dd       = max(max_dd, dd)
            max_duration = max(max_duration, current_dur)

    return round(max_dd, 6), max_duration


def calc_calmar(total_pnl: float, max_dd: float, n_days: int) -> Optional[float]:
    """
    Calmar ratio = annualised return / max drawdown.
    Returns None if max drawdown or day count is zero.
    """
    if max_dd == 0 or n_days == 0:
        return None
    annual_return = total_pnl * (TRADING_DAYS_PER_YEAR / n_days)
    return round(annual_return / max_dd, 4)


def calc_recovery_factor(total_pnl: float, max_dd: float) -> Optional[float]:
    """Net profit / max drawdown (> 1 means profit exceeded worst drawdown)."""
    if max_dd == 0:
        return None
    return round(total_pnl / max_dd, 4)


def calc_streak(sorted_daily_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Return current win/loss streak counting backward from the most-recent day.

    Win day  : pnl > 0
    Loss day : pnl < 0
    Flat day : pnl == 0 — breaks the streak
    """
    if not sorted_daily_rows:
        return {"type": "none", "count": 0}

    last_pnl = sorted_daily_rows[-1]["pnl"]
    if last_pnl == 0:
        return {"type": "none", "count": 0}

    streak_type  = "win" if last_pnl > 0 else "loss"
    streak_count = 0

    for row in reversed(sorted_daily_rows):
        pnl = row["pnl"]
        if streak_type == "win"  and pnl > 0:
            streak_count += 1
        elif streak_type == "loss" and pnl < 0:
            streak_count += 1
        else:
            break

    return {"type": streak_type, "count": streak_count}


# ── Per-bot attribution ───────────────────────────────────────────────────────

def build_attribution(closed_trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Return per-bot performance summary sorted by total PnL descending.

    Each entry: {bot_name, pnl, trades, win_rate, pf}
    pf  = profit factor = gross_profit / |gross_loss|
    """
    bot_data: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "gross_profit": 0.0, "gross_loss": 0.0}
    )

    for trade in closed_trades:
        bot_name = trade.get("bot_name") or trade.get("bot_id") or "Unknown"
        pnl      = _extract_pnl(trade)
        pnl      = pnl if pnl is not None else 0.0
        d        = bot_data[bot_name]
        d["pnl"]    += pnl
        d["trades"] += 1
        if pnl > 0:
            d["wins"]         += 1
            d["gross_profit"] += pnl
        elif pnl < 0:
            d["gross_loss"]   += abs(pnl)

    attribution = []
    for bot_name, d in bot_data.items():
        n_trades = d["trades"]
        win_rate = round(d["wins"] / n_trades, 4) if n_trades else 0.0

        gp = d["gross_profit"]
        gl = d["gross_loss"]
        if gl > 0:
            pf: Optional[float] = round(gp / gl, 4)
        elif gp > 0:
            pf = None  # infinite PF — no losses
        else:
            pf = None  # no trades with non-zero PnL

        attribution.append({
            "bot_name": bot_name,
            "pnl":      round(d["pnl"], 6),
            "trades":   n_trades,
            "win_rate": win_rate,
            "pf":       pf,
        })

    attribution.sort(key=lambda x: x["pnl"], reverse=True)
    return attribution


# ── Summary ───────────────────────────────────────────────────────────────────

def build_summary(daily_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute aggregate summary metrics from daily rows."""

    empty = {
        "total_pnl":       0.0,
        "sharpe":          None,
        "sortino":         None,
        "calmar":          None,
        "max_dd_duration": 0,
        "recovery_factor": None,
        "win_days":        0,
        "loss_days":       0,
        "best_day":        None,
        "worst_day":       None,
        "current_streak":  {"type": "none", "count": 0},
    }

    if not daily_rows:
        return empty

    daily_pnl_values = [row["pnl"] for row in daily_rows]
    cumulative_vals  = [row["cumulative"] for row in daily_rows]
    total_pnl        = cumulative_vals[-1]

    win_days  = sum(1 for v in daily_pnl_values if v > 0)
    loss_days = sum(1 for v in daily_pnl_values if v < 0)

    best_row  = max(daily_rows, key=lambda r: r["pnl"])
    worst_row = min(daily_rows, key=lambda r: r["pnl"])

    max_dd, max_dd_dur = calc_max_drawdown(cumulative_vals)

    return {
        "total_pnl":       round(total_pnl, 6),
        "sharpe":          calc_sharpe(daily_pnl_values),
        "sortino":         calc_sortino(daily_pnl_values),
        "calmar":          calc_calmar(total_pnl, max_dd, len(daily_rows)),
        "max_dd_duration": max_dd_dur,
        "recovery_factor": calc_recovery_factor(total_pnl, max_dd),
        "win_days":        win_days,
        "loss_days":       loss_days,
        "best_day":        {"date": best_row["date"],  "pnl": best_row["pnl"]},
        "worst_day":       {"date": worst_row["date"], "pnl": worst_row["pnl"]},
        "current_streak":  calc_streak(daily_rows),
    }


# ── Core builder (shared) ─────────────────────────────────────────────────────

def _build_report(all_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the full report dict from a list of raw trades."""
    closed_trades = [t for t in all_trades if _is_closed_trade(t)]

    logger.info(
        "Total trades: %d  |  Closed (with PnL): %d",
        len(all_trades), len(closed_trades),
    )

    dates = sorted(filter(None, (_trade_date(t) for t in closed_trades)))
    period = {
        "start": dates[0]  if dates else None,
        "end":   dates[-1] if dates else None,
    }

    daily_rows, _ = build_daily_data(closed_trades)
    summary       = build_summary(daily_rows)
    attribution   = build_attribution(closed_trades)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period":        period,
        "summary":       summary,
        "daily":         daily_rows,
        "attribution":   attribution,
    }


def _write_report(report: Dict[str, Any], output: str) -> None:
    """Serialise and write report to disk (JSON, handles Infinity)."""
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)

    def _default(obj: Any) -> Any:
        if isinstance(obj, float) and math.isinf(obj):
            return "Infinity" if obj > 0 else "-Infinity"
        raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")

    try:
        with open(output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=_default)
        logger.info("Report written → %s", output)
    except OSError as exc:
        logger.error("Failed to write report: %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def generate_report(
    api_url: str = DEFAULT_API_URL,
    output:  str = DEFAULT_OUTPUT,
) -> Dict[str, Any]:
    """
    Fetch trades from the live HTTP API, compute all metrics, write JSON.

    NOTE: Uses synchronous requests.get — do NOT call this inside an async
    FastAPI handler directly (blocks the event loop). Use
    generate_report_from_file() inside async handlers, or run this in a
    thread via asyncio.to_thread().
    """
    all_trades = fetch_trades_from_api(api_url)
    report     = _build_report(all_trades)
    _write_report(report, output)
    return report


def generate_report_from_file(
    trades_file: str = DEFAULT_TRADES_FILE,
    output:      str = DEFAULT_OUTPUT,
) -> Dict[str, Any]:
    """
    Load trades directly from the local JSON file, compute metrics, write JSON.

    Safe to call from an async FastAPI handler — no I/O loop is blocked
    (file read is fast; JSON parsing stays in-process).
    """
    all_trades = fetch_trades_from_file(trades_file)
    report     = _build_report(all_trades)
    _write_report(report, output)
    return report


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate daily P&L report from Lighthouse Trading.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--url",
        default=None,
        help=f"Trades API endpoint (default: {DEFAULT_API_URL})",
    )
    source.add_argument(
        "--file",
        default=None,
        help=f"Read trades directly from local JSON file (default: {DEFAULT_TRADES_FILE})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max trades to fetch via API (ignored for --file)",
    )
    parser.add_argument(
        "--print",
        dest="print_report",
        action="store_true",
        help="Print the full report JSON to stdout after writing",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.file:
        report = generate_report_from_file(trades_file=args.file, output=args.output)
    else:
        url = args.url or DEFAULT_API_URL
        if "limit=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}limit={args.limit}"
        report = generate_report(api_url=url, output=args.output)

    if args.print_report:
        def _serialise(obj: Any) -> Any:
            if isinstance(obj, float) and math.isinf(obj):
                return "Infinity" if obj > 0 else "-Infinity"
            raise TypeError
        print(json.dumps(report, indent=2, default=_serialise))
    else:
        s = report["summary"]
        print(
            f"\n📊 Daily P&L Report\n"
            f"   Period        : {report['period']['start']} → {report['period']['end']}\n"
            f"   Total PnL     : {s['total_pnl']}\n"
            f"   Sharpe        : {s['sharpe']}\n"
            f"   Sortino       : {s['sortino']}\n"
            f"   Calmar        : {s['calmar']}\n"
            f"   Max DD days   : {s['max_dd_duration']}\n"
            f"   Recovery fac. : {s['recovery_factor']}\n"
            f"   Win/Loss days : {s['win_days']} / {s['loss_days']}\n"
            f"   Best day      : {s['best_day']}\n"
            f"   Worst day     : {s['worst_day']}\n"
            f"   Streak        : {s['current_streak']}\n"
            f"   Bots          : {len(report['attribution'])}\n"
            f"   Written to    : {args.output}\n"
        )
