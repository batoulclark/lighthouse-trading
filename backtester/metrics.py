"""
metrics.py — Calculate comprehensive trading performance metrics.

Usage
-----
    from backtester.metrics import calculate_metrics
    metrics = calculate_metrics(trades, equity_curve, initial_capital=10_000)
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backtester.models import Trade


def calculate_metrics(
    trades: List[Trade],
    equity_curve: pd.Series,
    initial_capital: float,
    periods_per_year: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Compute all trading performance metrics.

    Parameters
    ----------
    trades : list of Trade
        Completed trades produced by BacktestEngine.
    equity_curve : pd.Series
        Bar-by-bar equity values (DatetimeIndex).
    initial_capital : float
        Starting capital in USD.
    periods_per_year : int or None
        Bars per year for annualised calculations.  Auto-detected from
        equity_curve if None.

    Returns
    -------
    dict
        All metrics described in the module docstring.
    """
    m: Dict[str, Any] = {}

    final_equity = float(equity_curve.iloc[-1]) if len(equity_curve) > 0 else initial_capital

    # ── Basic P&L ──────────────────────────────────────────────────────── #
    net_profit_usd = final_equity - initial_capital
    net_profit_pct = net_profit_usd / initial_capital * 100.0 if initial_capital else 0.0

    m["net_profit_usd"]  = round(net_profit_usd, 2)
    m["net_profit_pct"]  = round(net_profit_pct, 4)
    m["final_equity"]    = round(final_equity, 2)
    m["initial_capital"] = round(initial_capital, 2)

    # ── Trade counts ───────────────────────────────────────────────────── #
    total_trades = len(trades)
    winners      = [t for t in trades if t.pnl > 0]
    losers       = [t for t in trades if t.pnl <= 0]

    m["total_trades"]  = total_trades
    m["winning_trades"] = len(winners)
    m["losing_trades"]  = len(losers)
    m["win_rate_pct"]  = round(len(winners) / total_trades * 100, 2) if total_trades else 0.0

    # ── Avg win / loss ──────────────────────────────────────────────────── #
    avg_win  = float(np.mean([t.pnl for t in winners])) if winners else 0.0
    avg_loss = float(np.mean([t.pnl for t in losers]))  if losers  else 0.0
    avg_trade = float(np.mean([t.pnl for t in trades])) if trades  else 0.0

    m["avg_win_usd"]  = round(avg_win, 2)
    m["avg_loss_usd"] = round(avg_loss, 2)
    m["avg_trade_usd"] = round(avg_trade, 2)

    # ── Profit factor ─────────────────────────────────────────────────── #
    gross_profit = sum(t.pnl for t in winners)
    gross_loss   = abs(sum(t.pnl for t in losers))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    m["gross_profit"]   = round(gross_profit, 2)
    m["gross_loss"]     = round(gross_loss, 2)
    m["profit_factor"]  = round(profit_factor, 4)

    # ── Drawdown ────────────────────────────────────────────────────────── #
    peak = equity_curve.cummax()
    drawdown_usd = equity_curve - peak          # negative values
    drawdown_pct = drawdown_usd / peak * 100    # % drawdown

    max_dd_usd = float(drawdown_usd.min()) if len(drawdown_usd) else 0.0
    max_dd_pct = float(drawdown_pct.min()) if len(drawdown_pct) else 0.0

    m["max_drawdown_usd"] = round(max_dd_usd, 2)
    m["max_drawdown_pct"] = round(max_dd_pct, 4)

    # ── Duration stats ─────────────────────────────────────────────────── #
    durations: List[float] = []
    for t in trades:
        if pd.notna(t.entry_time) and pd.notna(t.exit_time):
            dur_hours = (t.exit_time - t.entry_time).total_seconds() / 3600
            durations.append(dur_hours)

    avg_duration_hours = float(np.mean(durations)) if durations else 0.0
    m["avg_trade_duration_hours"] = round(avg_duration_hours, 2)

    # ── Periods per year (auto-detect) ────────────────────────────────── #
    if periods_per_year is None:
        periods_per_year = _estimate_periods_per_year(equity_curve)
    m["periods_per_year"] = periods_per_year

    # ── Returns series for ratio calculations ─────────────────────────── #
    returns = equity_curve.pct_change().dropna()

    # ── Sharpe ratio ───────────────────────────────────────────────────── #
    sharpe = _sharpe(returns, periods_per_year)
    m["sharpe_ratio"] = round(sharpe, 4)

    # ── Sortino ratio ──────────────────────────────────────────────────── #
    sortino = _sortino(returns, periods_per_year)
    m["sortino_ratio"] = round(sortino, 4)

    # ── CAGR ────────────────────────────────────────────────────────────── #
    cagr = _cagr(equity_curve, initial_capital)
    m["cagr_pct"] = round(cagr * 100, 4)

    # ── Calmar ratio ────────────────────────────────────────────────────── #
    if max_dd_pct != 0:
        calmar = abs(cagr * 100 / max_dd_pct)
    else:
        calmar = float("inf") if cagr > 0 else 0.0
    m["calmar_ratio"] = round(calmar, 4)

    # ── Recovery factor ─────────────────────────────────────────────────── #
    recovery = net_profit_usd / abs(max_dd_usd) if max_dd_usd != 0 else float("inf")
    m["recovery_factor"] = round(recovery, 4)

    # ── Expectancy ──────────────────────────────────────────────────────── #
    wr = len(winners) / total_trades if total_trades else 0.0
    lr = 1 - wr
    expectancy = (wr * avg_win) + (lr * avg_loss)
    m["expectancy_usd"] = round(expectancy, 2)

    # ── Consecutive wins / losses ───────────────────────────────────────── #
    max_consec_wins, max_consec_losses = _consecutive_streaks(trades)
    m["max_consecutive_wins"]   = max_consec_wins
    m["max_consecutive_losses"] = max_consec_losses

    # ── Exit reason breakdown ───────────────────────────────────────────── #
    exit_reasons: Dict[str, int] = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1
    m["exit_reasons"] = exit_reasons

    # ── Commission paid ─────────────────────────────────────────────────── #
    total_commission = sum(t.commission for t in trades)
    m["total_commission_usd"] = round(total_commission, 2)

    return m


# ──────────────────────────────────────────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────────────────────────────────────────

def _sharpe(returns: pd.Series, periods_per_year: int, risk_free: float = 0.0) -> float:
    """Annualised Sharpe ratio (assume 0 risk-free rate by default)."""
    if returns.empty or returns.std() == 0:
        return 0.0
    excess = returns - risk_free / periods_per_year
    return float(excess.mean() / excess.std() * math.sqrt(periods_per_year))


def _sortino(returns: pd.Series, periods_per_year: int, risk_free: float = 0.0) -> float:
    """Annualised Sortino ratio (downside deviation)."""
    if returns.empty:
        return 0.0
    excess = returns - risk_free / periods_per_year
    downside = excess[excess < 0]
    if downside.empty or downside.std() == 0:
        return float("inf") if excess.mean() > 0 else 0.0
    downside_std = math.sqrt((downside ** 2).mean())  # RMS of downside returns
    return float(excess.mean() / downside_std * math.sqrt(periods_per_year))


def _cagr(equity_curve: pd.Series, initial_capital: float) -> float:
    """Compound annual growth rate."""
    if len(equity_curve) < 2 or initial_capital <= 0:
        return 0.0
    total_return = float(equity_curve.iloc[-1]) / initial_capital
    if total_return <= 0:
        return -1.0
    start = equity_curve.index[0]
    end   = equity_curve.index[-1]
    years = (end - start).total_seconds() / (365.25 * 86400)
    if years <= 0:
        return 0.0
    return total_return ** (1 / years) - 1


def _estimate_periods_per_year(equity_curve: pd.Series) -> int:
    """Estimate bars-per-year from the index frequency."""
    if len(equity_curve) < 2:
        return 252  # default: daily
    delta_secs = (equity_curve.index[-1] - equity_curve.index[0]).total_seconds()
    avg_bar_secs = delta_secs / (len(equity_curve) - 1)

    year_secs = 365.25 * 86400
    # Round to nearest standard: 525600 (1m), 105120 (5m), 35040 (15m),
    # 8760 (1h), 2190 (4h), 365 (1d)
    periods = year_secs / avg_bar_secs
    return max(1, int(round(periods)))


def _consecutive_streaks(trades: List[Trade]):
    """Return (max_consecutive_wins, max_consecutive_losses)."""
    if not trades:
        return 0, 0
    max_wins = max_losses = 0
    cur_wins = cur_losses = 0
    for t in trades:
        if t.pnl > 0:
            cur_wins   += 1
            cur_losses  = 0
        else:
            cur_losses += 1
            cur_wins    = 0
        max_wins   = max(max_wins,   cur_wins)
        max_losses = max(max_losses, cur_losses)
    return max_wins, max_losses


def metrics_to_string(m: Dict[str, Any]) -> str:
    """Format a metrics dict as a human-readable string."""
    lines = [
        "═" * 52,
        "  BACKTEST RESULTS",
        "═" * 52,
        f"  Net Profit:          ${m['net_profit_usd']:>12,.2f}  ({m['net_profit_pct']:.2f}%)",
        f"  Final Equity:        ${m['final_equity']:>12,.2f}",
        f"  CAGR:                {m['cagr_pct']:>12.2f}%",
        "─" * 52,
        f"  Total Trades:        {m['total_trades']:>12}",
        f"  Win Rate:            {m['win_rate_pct']:>12.2f}%",
        f"  Avg Win:             ${m['avg_win_usd']:>12,.2f}",
        f"  Avg Loss:            ${m['avg_loss_usd']:>12,.2f}",
        f"  Profit Factor:       {m['profit_factor']:>12.4f}",
        f"  Expectancy:          ${m['expectancy_usd']:>12,.2f}",
        "─" * 52,
        f"  Max Drawdown:        ${m['max_drawdown_usd']:>12,.2f}  ({m['max_drawdown_pct']:.2f}%)",
        f"  Recovery Factor:     {m['recovery_factor']:>12.4f}",
        "─" * 52,
        f"  Sharpe Ratio:        {m['sharpe_ratio']:>12.4f}",
        f"  Sortino Ratio:       {m['sortino_ratio']:>12.4f}",
        f"  Calmar Ratio:        {m['calmar_ratio']:>12.4f}",
        "─" * 52,
        f"  Avg Trade Duration:  {m['avg_trade_duration_hours']:>10.1f} h",
        f"  Max Consec. Wins:    {m['max_consecutive_wins']:>12}",
        f"  Max Consec. Losses:  {m['max_consecutive_losses']:>12}",
        f"  Total Commission:    ${m['total_commission_usd']:>12,.2f}",
        "═" * 52,
    ]
    return "\n".join(lines)
