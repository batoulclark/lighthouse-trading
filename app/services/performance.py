"""
performance.py — Live trading performance tracker.

Reads from data/trades.json and computes P&L, drawdown, win rate,
profit factor, Sharpe ratio, and equity curve.

Usage
-----
    from app.services.performance import PerformanceTracker
    tracker = PerformanceTracker("data/trades.json")
    print(tracker.get_summary())
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PerformanceTracker:
    """Reads the live trade log and computes performance metrics."""

    def __init__(self, trades_file: str) -> None:
        self.trades_file = trades_file

    # ── Public API ────────────────────────────────────────────────────────────

    def get_summary(self, bot_id: Optional[str] = None) -> Dict[str, Any]:
        """Return aggregated performance metrics.

        Parameters
        ----------
        bot_id : str or None
            Filter to a specific bot.  None = all bots.

        Returns
        -------
        dict
            Keys: total_pnl, win_rate, profit_factor, max_drawdown_pct,
                  sharpe_ratio, total_trades, winning_trades, losing_trades,
                  avg_win, avg_loss, gross_profit, gross_loss.
        """
        trades = self._load(bot_id)

        closed = [t for t in trades if t.get("pnl") is not None]
        winners = [t for t in closed if (t["pnl"] or 0) > 0]
        losers  = [t for t in closed if (t["pnl"] or 0) <= 0]

        total_trades  = len(closed)
        win_rate      = len(winners) / total_trades if total_trades else 0.0
        gross_profit  = sum(t["pnl"] for t in winners) if winners else 0.0
        gross_loss    = abs(sum(t["pnl"] for t in losers)) if losers else 0.0
        # Return 0.0 when undefined (no losing trades) to keep JSON-serializable
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0
        total_pnl     = gross_profit - gross_loss
        avg_win       = gross_profit / len(winners) if winners else 0.0
        avg_loss      = -(gross_loss / len(losers)) if losers else 0.0

        # Drawdown & Sharpe from equity curve
        equity = self._build_equity_curve(trades)
        max_dd_pct = _max_drawdown_pct(equity)
        sharpe     = _sharpe_ratio(equity)

        return {
            "total_pnl":      round(total_pnl, 4),
            "total_trades":   total_trades,
            "winning_trades": len(winners),
            "losing_trades":  len(losers),
            "win_rate":       round(win_rate, 4),
            "gross_profit":   round(gross_profit, 4),
            "gross_loss":     round(gross_loss, 4),
            "profit_factor":  round(profit_factor, 4),
            "avg_win":        round(avg_win, 4),
            "avg_loss":       round(avg_loss, 4),
            "max_drawdown_pct": round(max_dd_pct, 4),
            "sharpe_ratio":   round(sharpe, 4),
        }

    def get_daily_pnl(self, bot_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return daily P&L as a list of {date, pnl} dicts sorted ascending.

        Parameters
        ----------
        bot_id : str or None
            Filter to a specific bot.
        """
        trades = self._load(bot_id)
        daily: Dict[str, float] = defaultdict(float)

        for t in trades:
            pnl = t.get("pnl")
            if pnl is None:
                continue
            try:
                dt = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))
                date_key = dt.date().isoformat()
            except (KeyError, ValueError):
                continue
            daily[date_key] += pnl

        return [
            {"date": d, "pnl": round(v, 4)}
            for d, v in sorted(daily.items())
        ]

    def get_equity_curve(self, bot_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return cumulative equity curve as {date, equity} dicts.

        The initial equity is 0 and each closed trade adds its P&L.
        """
        trades = self._load(bot_id)
        equity = self._build_equity_curve(trades)
        return [{"date": d, "equity": round(v, 4)} for d, v in equity]

    def get_trade_stats(self, bot_id: Optional[str] = None) -> Dict[str, Any]:
        """Return per-pair and per-bot breakdown with counts and P&L.

        Parameters
        ----------
        bot_id : str or None
            Filter to a specific bot.
        """
        trades = self._load(bot_id)

        by_pair: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"trades": 0, "pnl": 0.0, "wins": 0}
        )
        by_bot: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"trades": 0, "pnl": 0.0, "wins": 0, "name": ""}
        )

        for t in trades:
            pnl = t.get("pnl") or 0.0
            pair = t.get("pair", "unknown")
            bid  = t.get("bot_id", "unknown")
            bname = t.get("bot_name", bid)

            by_pair[pair]["trades"] += 1
            by_pair[pair]["pnl"] += pnl
            if pnl > 0:
                by_pair[pair]["wins"] += 1

            by_bot[bid]["trades"] += 1
            by_bot[bid]["pnl"] += pnl
            by_bot[bid]["name"] = bname
            if pnl > 0:
                by_bot[bid]["wins"] += 1

        def _wr(d: dict) -> float:
            return d["wins"] / d["trades"] if d["trades"] else 0.0

        return {
            "by_pair": {
                p: {
                    "trades": v["trades"],
                    "pnl": round(v["pnl"], 4),
                    "win_rate": round(_wr(v), 4),
                }
                for p, v in by_pair.items()
            },
            "by_bot": {
                b: {
                    "name": v["name"],
                    "trades": v["trades"],
                    "pnl": round(v["pnl"], 4),
                    "win_rate": round(_wr(v), 4),
                }
                for b, v in by_bot.items()
            },
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load(self, bot_id: Optional[str] = None) -> List[dict]:
        """Load all trades from the JSON file, optionally filtered by bot_id."""
        if not os.path.exists(self.trades_file):
            return []
        try:
            with open(self.trades_file, "r") as fh:
                trades: List[dict] = json.load(fh)
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not load trades file: %s", self.trades_file)
            return []

        if bot_id is not None:
            trades = [t for t in trades if t.get("bot_id") == bot_id]
        return trades

    def _build_equity_curve(self, trades: List[dict]) -> List[tuple[str, float]]:
        """Build a sorted list of (date_str, cumulative_equity) from trades.

        Only trades with a non-None pnl contribute to the curve.
        """
        points: List[tuple[str, float]] = []
        cumulative = 0.0

        sorted_trades = sorted(
            (t for t in trades if t.get("pnl") is not None),
            key=lambda t: t.get("timestamp", ""),
        )

        for t in sorted_trades:
            cumulative += t["pnl"] or 0.0
            try:
                dt = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))
                points.append((dt.date().isoformat(), round(cumulative, 4)))
            except (KeyError, ValueError):
                continue

        return points


# ── Standalone math helpers ────────────────────────────────────────────────────

def _max_drawdown_pct(equity_curve: List[tuple[str, float]]) -> float:
    """Compute max drawdown as a percentage of the peak equity.

    Returns 0.0 for empty or all-zero curves.
    """
    if len(equity_curve) < 2:
        return 0.0

    values = [v for _, v in equity_curve]
    peak = values[0]
    max_dd = 0.0

    for v in values[1:]:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

    return max_dd


def _sharpe_ratio(
    equity_curve: List[tuple[str, float]],
    periods_per_year: int = 252,
    risk_free: float = 0.0,
) -> float:
    """Annualised Sharpe ratio computed from equity curve returns.

    Returns 0.0 when there is insufficient data.
    """
    if len(equity_curve) < 3:
        return 0.0

    values = [v for _, v in equity_curve]
    returns: List[float] = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev == 0:
            continue
        returns.append((values[i] - prev) / prev)

    if len(returns) < 2:
        return 0.0

    n = len(returns)
    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(variance)

    if std_r == 0:
        return 0.0

    excess = mean_r - risk_free / periods_per_year
    return excess / std_r * math.sqrt(periods_per_year)
