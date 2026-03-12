"""
gaussian_channel.py — Gaussian Channel trend-following strategy (Lighthouse v7).

Theory
------
A Gaussian filter (IIR low-pass) is applied to price to produce a smooth
"channel centre".  Upper/lower bands are offset by `multiplier * ATR`.
When price crosses above the upper band we go LONG; when it crosses below
the lower band we go SHORT.

Optional filters
----------------
* MACD histogram  — only trade in the direction of the MACD histogram sign
* Time filter     — skip signals outside preferred trading hours (UTC)
* Funding filter  — skip longs when funding rate is extremely positive (expensive)

Parameters (default_params / param_ranges)
-----------------------------------------
period     : Gaussian filter window length (default 100)
poles      : Filter poles 1–4; higher = smoother but laggier (default 3)
multiplier : Band offset in ATR units (default 2.0)
atr_period : ATR lookback for band width (default 14)
use_macd   : bool — apply MACD histogram filter (default False)
macd_fast  : MACD fast EMA period (default 12)
macd_slow  : MACD slow EMA period (default 26)
macd_signal: MACD signal EMA period (default 9)
sl_pct     : Stop-loss as fraction of entry price (default 0.05 = 5%)
tp_pct     : Take-profit as fraction (0 = disabled, default 0)
use_time_filter : bool — only trade during certain hours (default False)
trade_hours_start: int — start hour UTC inclusive (default 1)
trade_hours_end  : int — end hour UTC exclusive (default 23)
use_funding_filter: bool — skip longs above funding threshold (default False)
funding_threshold : float — max acceptable funding rate per 8h (default 0.001)
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backtester.strategy_base import Action, Signal, StrategyBase, HOLD_SIGNAL


class GaussianChannelStrategy(StrategyBase):
    """Gaussian Channel trend-following strategy."""

    name        = "gaussian_channel"
    description = "Gaussian Channel with ATR bands + optional MACD / time / funding filters"

    default_params: Dict[str, Any] = {
        "period":              100,
        "poles":               3,
        "multiplier":          2.0,
        "atr_period":          14,
        "use_macd":            False,
        "macd_fast":           12,
        "macd_slow":           26,
        "macd_signal":         9,
        "sl_pct":              0.05,
        "tp_pct":              0.0,
        "use_time_filter":     False,
        "trade_hours_start":   1,
        "trade_hours_end":     23,
        "use_funding_filter":  False,
        "funding_threshold":   0.001,
    }

    param_ranges: Dict[str, List[Any]] = {
        "period":     [50, 75, 100, 125, 144, 175, 200, 250, 300],
        "poles":      [1, 2, 3, 4],
        "multiplier": [1.0, 1.5, 2.0, 2.5, 3.0],
        "use_macd":   [True, False],
        "sl_pct":     [0.02, 0.05, 0.10],
    }

    def __init__(self) -> None:
        super().__init__()
        self._prev_above_upper: Optional[bool] = None
        self._prev_below_lower: Optional[bool] = None

    # ------------------------------------------------------------------ #
    # StrategyBase interface                                               #
    # ------------------------------------------------------------------ #

    def on_candle(self, candle: pd.Series, history: pd.DataFrame) -> Signal:
        p = self.params
        period    = int(p["period"])
        poles     = int(p["poles"])
        mult      = float(p["multiplier"])
        atr_p     = int(p["atr_period"])

        # Need enough history
        min_bars = max(period * 2, atr_p + 1, p.get("macd_slow", 26) + p.get("macd_signal", 9))
        if len(history) < min_bars:
            return HOLD_SIGNAL

        close = history["close"].values.astype(float)
        high  = history["high"].values.astype(float)
        low   = history["low"].values.astype(float)

        # ── Gaussian filter ──────────────────────────────────────────── #
        gauss = _gaussian_filter(close, period, poles)

        # ── ATR ──────────────────────────────────────────────────────── #
        atr = _atr(high, low, close, atr_p)

        # ── Bands ────────────────────────────────────────────────────── #
        upper = gauss + mult * atr
        lower = gauss - mult * atr

        current_close = close[-1]
        current_upper = upper[-1]
        current_lower = lower[-1]

        above_upper = current_close > current_upper
        below_lower = current_close < current_lower

        # ── Time filter ──────────────────────────────────────────────── #
        if p.get("use_time_filter", False):
            ts = candle.name
            if hasattr(ts, "hour"):
                h = ts.hour
                start = int(p.get("trade_hours_start", 1))
                end   = int(p.get("trade_hours_end", 23))
                if not (start <= h < end):
                    self._prev_above_upper = above_upper
                    self._prev_below_lower = below_lower
                    return HOLD_SIGNAL

        # ── Funding filter ────────────────────────────────────────────── #
        if p.get("use_funding_filter", False):
            fr_col = "funding_rate"
            if fr_col in candle.index:
                fr = float(candle[fr_col])
                threshold = float(p.get("funding_threshold", 0.001))
                if fr > threshold and above_upper:
                    # Funding too expensive for longs
                    self._prev_above_upper = above_upper
                    self._prev_below_lower = below_lower
                    return HOLD_SIGNAL

        # ── MACD filter ───────────────────────────────────────────────── #
        macd_bullish: Optional[bool] = None
        if p.get("use_macd", False):
            fast_p   = int(p.get("macd_fast", 12))
            slow_p   = int(p.get("macd_slow", 26))
            signal_p = int(p.get("macd_signal", 9))
            if len(close) >= slow_p + signal_p:
                macd_line, signal_line, hist = _macd(close, fast_p, slow_p, signal_p)
                macd_bullish = hist[-1] > 0

        # ── Signal generation ─────────────────────────────────────────── #
        prev_above = self._prev_above_upper
        prev_below = self._prev_below_lower

        # Cross above upper band → LONG
        crossed_above = (prev_above is not None and not prev_above and above_upper)
        # Cross below lower band → SHORT
        crossed_below = (prev_below is not None and not prev_below and below_lower)
        # Re-enter lower band (was below, now not) → close short / potential long reversal
        recross_upper = (prev_above is not None and prev_above and not above_upper)
        recross_lower = (prev_below is not None and prev_below and not below_lower)

        self._prev_above_upper = above_upper
        self._prev_below_lower = below_lower

        sl_pct = float(p.get("sl_pct", 0.05))
        tp_pct = float(p.get("tp_pct", 0.0))

        def make_long() -> Signal:
            sl = current_close * (1 - sl_pct) if sl_pct > 0 else None
            tp = current_close * (1 + tp_pct) if tp_pct > 0 else None
            return Signal(action=Action.LONG, sl=sl, tp=tp,
                          comment=f"gc_cross_above upper={current_upper:.4f}")

        def make_short() -> Signal:
            sl = current_close * (1 + sl_pct) if sl_pct > 0 else None
            tp = current_close * (1 - tp_pct) if tp_pct > 0 else None
            return Signal(action=Action.SHORT, sl=sl, tp=tp,
                          comment=f"gc_cross_below lower={current_lower:.4f}")

        if crossed_above:
            if macd_bullish is False:
                return HOLD_SIGNAL  # MACD disagrees
            return make_long()

        if crossed_below:
            if macd_bullish is True:
                return HOLD_SIGNAL  # MACD disagrees
            return make_short()

        if recross_upper:
            return Signal(action=Action.CLOSE, comment="price_left_upper_band")

        if recross_lower:
            return Signal(action=Action.CLOSE, comment="price_left_lower_band")

        return HOLD_SIGNAL


# ──────────────────────────────────────────────────────────────────────────────
# DSP helpers
# ──────────────────────────────────────────────────────────────────────────────

def _gaussian_filter(prices: np.ndarray, period: int, poles: int) -> np.ndarray:
    """
    Ehlers / JW-style multi-pole Gaussian IIR filter.

    Each pole applies a single-pole EMA with alpha derived from the Gaussian
    beta coefficient.  The result is a smooth low-pass filter whose impulse
    response approximates a Gaussian curve.

    Reference: John Ehlers — "Cybernetic Analysis for Stocks and Futures" ch.15
    """
    poles = max(1, min(poles, 4))
    # Beta and alpha coefficients (Ehlers formula)
    beta = (1 - math.cos(2 * math.pi / period)) / (math.pow(2, 1 / poles) - 1)
    alpha = -beta + math.sqrt(beta * beta + 2 * beta)
    alpha = min(alpha, 1.0)

    n = len(prices)
    filtered = prices.copy()

    for _ in range(poles):
        f = filtered.copy()
        for i in range(1, n):
            f[i] = alpha * filtered[i] + (1 - alpha) * f[i - 1]
        filtered = f

    return filtered


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Wilder's ATR — returns array same length as inputs."""
    n = len(close)
    if n < 2:
        return np.zeros(n)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i]  - close[i - 1]),
        )
    atr_arr = np.zeros(n)
    atr_arr[period - 1] = float(np.mean(tr[:period]))
    for i in range(period, n):
        atr_arr[i] = (atr_arr[i - 1] * (period - 1) + tr[i]) / period
    # Forward-fill the initial NaN gap
    if period > 1:
        atr_arr[:period - 1] = atr_arr[period - 1]
    return atr_arr


def _ema(data: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average."""
    k = 2.0 / (period + 1)
    result = np.zeros_like(data)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = data[i] * k + result[i - 1] * (1 - k)
    return result


def _macd(
    close: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
):
    """Returns (macd_line, signal_line, histogram) as np arrays."""
    fast_ema   = _ema(close, fast)
    slow_ema   = _ema(close, slow)
    macd_line  = fast_ema - slow_ema
    signal     = _ema(macd_line, signal_period)
    hist       = macd_line - signal
    return macd_line, signal, hist
