#!/usr/bin/env python3
"""
Lighthouse Trading — Risk Correlation Matrix
=============================================
Computes Pearson correlation between each pair of bots' daily PnL series.

Usage
-----
    python3 scripts/correlation_analysis.py [--api-url URL]

Output
------
    data/correlation_matrix.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_REPO_ROOT    = Path(__file__).resolve().parents[1]
_OUTPUT_FILE  = _REPO_ROOT / "data" / "correlation_matrix.json"
_DEFAULT_URL  = "http://127.0.0.1:8420/dashboard/trades?limit=500"

# ── numpy (optional) ─────────────────────────────────────────────────────────
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    logger.warning("numpy not available — using pure-Python correlation fallback")


# ── Math helpers ──────────────────────────────────────────────────────────────

def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _pearson_pure(x: list[float], y: list[float]) -> float | None:
    """Pure-Python Pearson r. Returns None if correlation is undefined."""
    n = len(x)
    if n < 2:
        return None
    mx, my = _mean(x), _mean(y)
    dx = [v - mx for v in x]
    dy = [v - my for v in y]
    num   = sum(a * b for a, b in zip(dx, dy))
    den_x = sum(a * a for a in dx) ** 0.5
    den_y = sum(a * a for a in dy) ** 0.5
    if den_x == 0 or den_y == 0:
        return None  # constant series → undefined correlation
    return num / (den_x * den_y)


def _pearson(x: list[float], y: list[float]) -> float | None:
    """Pearson r using numpy when available, pure-Python otherwise."""
    if len(x) < 2:
        return None
    if _HAS_NUMPY:
        xa, ya = np.array(x, dtype=float), np.array(y, dtype=float)
        if xa.std() == 0 or ya.std() == 0:
            return None
        matrix = np.corrcoef(xa, ya)
        val = float(matrix[0, 1])
        return None if (val != val) else round(val, 4)   # NaN guard
    return _pearson_pure(x, y)


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_trades(api_url: str) -> list[dict]:
    """Fetch trade list from the Lighthouse API."""
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


# ── PnL aggregation ───────────────────────────────────────────────────────────

def _extract_pnl(trade: dict) -> float | None:
    """Extract the best available PnL value from a trade record."""
    # Top-level pnl (may be null on open-leg entries)
    pnl = trade.get("pnl")
    if pnl is not None:
        try:
            return float(pnl)
        except (TypeError, ValueError):
            pass
    # Fallback: execution_result.pnl
    exec_result = trade.get("execution_result") or {}
    exec_pnl = exec_result.get("pnl")
    if exec_pnl is not None:
        try:
            return float(exec_pnl)
        except (TypeError, ValueError):
            pass
    return None


def build_daily_pnl(trades: list[dict]) -> dict[str, dict[str, float]]:
    """
    Returns: { bot_name: { "YYYY-MM-DD": total_pnl, ... }, ... }

    Only trades with extractable PnL are included.
    """
    pnl_by_bot: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for trade in trades:
        pnl = _extract_pnl(trade)
        if pnl is None:
            continue

        bot_name = trade.get("bot_name") or trade.get("bot_id") or "unknown"
        ts_raw   = trade.get("timestamp") or trade.get("signal_timestamp") or ""

        # Parse date from ISO timestamp
        try:
            date_str = ts_raw[:10]  # "YYYY-MM-DD"
            datetime.strptime(date_str, "%Y-%m-%d")  # validate
        except (ValueError, IndexError):
            logger.debug("Skipping trade with unparseable timestamp: %s", ts_raw)
            continue

        pnl_by_bot[bot_name][date_str] += pnl

    # Convert defaultdicts to plain dicts
    return {bot: dict(daily) for bot, daily in pnl_by_bot.items()}


# ── Correlation matrix ────────────────────────────────────────────────────────

def compute_correlation_matrix(
    daily_pnl: dict[str, dict[str, float]]
) -> tuple[list[str], list[list[float | None]]]:
    """
    Compute pairwise Pearson correlations.

    Returns (bot_names, matrix) where matrix[i][j] = corr(bot_i, bot_j).
    Diagonal is always 1.0.  Value is None when undefined (< 2 shared days,
    or one series is constant).
    """
    bots = sorted(daily_pnl.keys())
    n    = len(bots)
    matrix: list[list[float | None]] = [[None] * n for _ in range(n)]

    for i in range(n):
        matrix[i][i] = 1.0  # self-correlation

        for j in range(i + 1, n):
            bot_a, bot_b = bots[i], bots[j]
            # Shared dates only
            shared_dates = sorted(
                set(daily_pnl[bot_a].keys()) & set(daily_pnl[bot_b].keys())
            )
            if len(shared_dates) < 2:
                # Not enough overlap to compute correlation
                matrix[i][j] = matrix[j][i] = None
                continue

            x = [daily_pnl[bot_a][d] for d in shared_dates]
            y = [daily_pnl[bot_b][d] for d in shared_dates]
            r = _pearson(x, y)
            if r is not None:
                r = round(r, 4)
            matrix[i][j] = matrix[j][i] = r

    return bots, matrix


# ── Interpretation ────────────────────────────────────────────────────────────

def build_interpretation(
    bots: list[str],
    matrix: list[list[float | None]],
) -> str:
    """Generate a human-readable correlation interpretation string."""
    n = len(bots)
    if n == 0:
        return "No bots with sufficient trade data to analyse."
    if n == 1:
        return f"Only one active bot ({bots[0]}). No pairwise correlation to compute."

    # Collect all defined off-diagonal pairs
    pairs: list[tuple[float, str, str]] = []
    for i in range(n):
        for j in range(i + 1, n):
            r = matrix[i][j]
            if r is not None:
                pairs.append((abs(r), bots[i], bots[j], r))  # type: ignore[arg-type]

    if not pairs:
        return (
            "Insufficient overlapping data to compute any pairwise correlation. "
            "Bots may be trading on different days."
        )

    # Sort by absolute correlation (descending)
    pairs.sort(key=lambda p: p[0], reverse=True)

    lines: list[str] = []

    # Highlight highest correlation
    _, a, b, r_val = pairs[0]  # type: ignore[misc]
    if abs(r_val) >= 0.7:
        level = "High"
        note  = "diversification benefit is limited"
    elif abs(r_val) >= 0.4:
        level = "Moderate"
        note  = "some diversification benefit exists"
    else:
        level = "Low"
        note  = "these bots are well-diversified"

    lines.append(
        f"{level} correlation between {a} and {b} ({r_val:+.2f}) means {note}."
    )

    # Highlight lowest correlation (best diversifier), if different pair
    _, a2, b2, r_val2 = pairs[-1]  # type: ignore[misc]
    if (a2, b2) != (a, b):
        if abs(r_val2) < 0.3:
            lines.append(
                f"{a2} and {b2} show near-zero correlation ({r_val2:+.2f}), "
                "maximising diversification."
            )
        elif abs(r_val2) < 0.5:
            lines.append(
                f"{a2} and {b2} have the lowest correlation in the portfolio ({r_val2:+.2f})."
            )

    # Warn about any negative correlations (natural hedge)
    neg_pairs = [(a, b, r) for _, a, b, r in pairs if r < -0.4]
    for a, b, r in neg_pairs[:2]:
        lines.append(
            f"{a} and {b} are negatively correlated ({r:+.2f}) — natural hedge."
        )

    return " ".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(api_url: str = _DEFAULT_URL) -> dict[str, Any]:
    """Execute the full analysis pipeline and return the result dict."""

    logger.info("Fetching trades from %s …", api_url)
    trades = fetch_trades(api_url)
    logger.info("Received %d trades", len(trades))

    daily_pnl = build_daily_pnl(trades)
    logger.info("Bots with PnL data: %s", list(daily_pnl.keys()))

    # Edge case: no bots at all
    if not daily_pnl:
        result: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "bots": [],
            "matrix": [],
            "interpretation": (
                "No trade PnL data found. "
                "Ensure bots have completed at least one closed trade."
            ),
            "meta": {
                "total_trades_fetched": len(trades),
                "numpy_used": _HAS_NUMPY,
            },
        }
        _save(result)
        return result

    bots, matrix = compute_correlation_matrix(daily_pnl)
    interpretation = build_interpretation(bots, matrix)

    # Build per-bot stats for context
    bot_stats: dict[str, Any] = {}
    for bot in bots:
        daily = daily_pnl[bot]
        values = list(daily.values())
        bot_stats[bot] = {
            "trading_days": len(daily),
            "total_pnl": round(sum(values), 4),
            "avg_daily_pnl": round(_mean(values), 4) if values else 0.0,
        }

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bots": bots,
        "matrix": matrix,
        "interpretation": interpretation,
        "bot_stats": bot_stats,
        "meta": {
            "total_trades_fetched": len(trades),
            "bots_with_pnl_data": len(bots),
            "numpy_used": _HAS_NUMPY,
        },
    }

    _save(result)
    return result


def _save(result: dict[str, Any]) -> None:
    _OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_FILE.write_text(json.dumps(result, indent=2, default=str))
    logger.info("Saved correlation matrix → %s", _OUTPUT_FILE)


# ── CLI entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Compute per-bot daily PnL correlation matrix."
    )
    parser.add_argument(
        "--api-url",
        default=_DEFAULT_URL,
        help="Trades API endpoint (default: %(default)s)",
    )
    args = parser.parse_args()

    result = run(api_url=args.api_url)

    # Pretty-print summary
    print("\n── Correlation Matrix ──────────────────────────────────────")
    bots   = result["bots"]
    matrix = result["matrix"]
    if bots:
        header = f"{'':22}" + "".join(f"{b[:10]:>12}" for b in bots)
        print(header)
        for i, bot in enumerate(bots):
            row_vals = "".join(
                f"{'N/A':>12}" if matrix[i][j] is None else f"{matrix[i][j]:>12.4f}"
                for j in range(len(bots))
            )
            print(f"{bot[:22]:22}{row_vals}")
    print()
    print("Interpretation:", result["interpretation"])
    print()
    print(f"Saved to: {_OUTPUT_FILE}")
    sys.exit(0)
