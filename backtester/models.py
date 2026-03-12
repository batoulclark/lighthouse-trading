"""
models.py — Shared data classes for the backtesting engine.

Kept separate to avoid circular imports between engine.py and metrics.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import pandas as pd


@dataclass
class Trade:
    """Represents a completed (closed) trade."""
    trade_id:     int
    direction:    str              # "LONG" | "SHORT"
    entry_time:   pd.Timestamp
    exit_time:    pd.Timestamp
    entry_price:  float
    exit_price:   float
    size_usd:     float            # notional size in USD at entry
    pnl:          float            # realised P&L in USD (after commission)
    pnl_pct:      float            # P&L as % of entry notional
    commission:   float            # total commission paid (both legs)
    exit_reason:  str              # "signal" | "sl" | "tp" | "end_of_data"
    comment:      str = ""


@dataclass
class BacktestResult:
    """Container for all results produced by BacktestEngine.run()."""
    trades:       List[Trade]
    equity_curve: pd.Series        # index = datetime, values = portfolio equity
    drawdown:     pd.Series        # index = datetime, values = drawdown in USD
    metrics:      Dict[str, Any]   # output of calculate_metrics()
    params:       Dict[str, Any]   # strategy parameters used
