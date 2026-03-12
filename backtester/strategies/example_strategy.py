"""
example_strategy.py — Simple moving-average crossover example strategy.

A fast SMA crossing above the slow SMA triggers a LONG entry.
Crossing below triggers a SHORT.  Demonstrates the StrategyBase interface.

Parameters
----------
fast_period : int  — fast SMA window (default 20)
slow_period : int  — slow SMA window (default 50)
sl_pct      : float — stop-loss % below/above entry (default 0.03 = 3%)
tp_pct      : float — take-profit %, 0 to disable (default 0.0)
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd

from backtester.strategy_base import Action, Signal, StrategyBase, HOLD_SIGNAL


class MACrossStrategy(StrategyBase):
    """Simple SMA crossover trend-following strategy."""

    name        = "ma_cross"
    description = "Fast/slow SMA crossover — long on golden cross, short on death cross"

    default_params: Dict[str, Any] = {
        "fast_period": 20,
        "slow_period": 50,
        "sl_pct":      0.03,
        "tp_pct":      0.0,
    }

    param_ranges: Dict[str, List[Any]] = {
        "fast_period": [10, 15, 20, 25, 30],
        "slow_period": [40, 50, 60, 100, 200],
        "sl_pct":      [0.02, 0.03, 0.05],
    }

    def _validate_params(self) -> None:
        if self.params["fast_period"] >= self.params["slow_period"]:
            raise ValueError("fast_period must be < slow_period")

    def on_candle(self, candle: pd.Series, history: pd.DataFrame) -> Signal:
        fast_p = int(self.params["fast_period"])
        slow_p = int(self.params["slow_period"])
        sl_pct = float(self.params["sl_pct"])
        tp_pct = float(self.params["tp_pct"])

        if len(history) < slow_p + 1:
            return HOLD_SIGNAL

        close = history["close"].values.astype(float)

        fast_now  = float(np.mean(close[-fast_p:]))
        slow_now  = float(np.mean(close[-slow_p:]))
        fast_prev = float(np.mean(close[-(fast_p + 1):-1]))
        slow_prev = float(np.mean(close[-(slow_p + 1):-1]))

        price = float(candle["close"])

        # Golden cross: fast crosses above slow
        if fast_prev <= slow_prev and fast_now > slow_now:
            sl = price * (1 - sl_pct) if sl_pct > 0 else None
            tp = price * (1 + tp_pct) if tp_pct > 0 else None
            return Signal(
                action=Action.LONG, sl=sl, tp=tp,
                comment=f"golden_cross fast={fast_now:.4f} slow={slow_now:.4f}",
            )

        # Death cross: fast crosses below slow
        if fast_prev >= slow_prev and fast_now < slow_now:
            sl = price * (1 + sl_pct) if sl_pct > 0 else None
            tp = price * (1 - tp_pct) if tp_pct > 0 else None
            return Signal(
                action=Action.SHORT, sl=sl, tp=tp,
                comment=f"death_cross fast={fast_now:.4f} slow={slow_now:.4f}",
            )

        return HOLD_SIGNAL
