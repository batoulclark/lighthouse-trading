"""
regime.py — Market regime classifier.

Classifies the current market environment into one of four regimes:

* TRENDING_UP    — ADX > 25 and price above its SMA (uptrend)
* TRENDING_DOWN  — ADX > 25 and price below its SMA (downtrend)
* SQUEEZE        — Bollinger Band width below its 20-period rolling low
                   (volatility compression, breakout potential)
* CHOPPY         — ADX < 20 and no squeeze (sideways/mean-reverting)

Usage
-----
    from backtester.regime import RegimeDetector
    detector = RegimeDetector()
    regime = detector.classify(close_series, lookback=50)
    series = detector.classify_series(close_series, lookback=50)
"""

from __future__ import annotations

import math
from typing import List

import numpy as np
import pandas as pd

# ── Regime labels ─────────────────────────────────────────────────────────────

TRENDING_UP   = "TRENDING_UP"
TRENDING_DOWN = "TRENDING_DOWN"
CHOPPY        = "CHOPPY"
SQUEEZE       = "SQUEEZE"

# ── Thresholds ────────────────────────────────────────────────────────────────

_ADX_TREND_THRESHOLD  = 25.0   # ADX > this → trending
_ADX_CHOPPY_THRESHOLD = 20.0   # ADX < this → choppy


class RegimeDetector:
    """Classify market regime using ADX, Bollinger Band width, and SMA slope."""

    def classify(self, prices: pd.Series, lookback: int = 50) -> str:
        """Classify the regime at the latest bar.

        Parameters
        ----------
        prices : pd.Series
            Close prices, at least ``lookback + 1`` bars.
        lookback : int
            Window used for all indicator calculations (default 50).

        Returns
        -------
        str
            One of TRENDING_UP, TRENDING_DOWN, CHOPPY, SQUEEZE.
        """
        if len(prices) < lookback + 1:
            return CHOPPY

        window = prices.iloc[-(lookback + lookback):]  # extra bars for warm-up
        result = _classify_one(window, lookback)
        return result

    def classify_series(self, prices: pd.Series, lookback: int = 50) -> pd.Series:
        """Classify regime at every bar, returning a Series of regime labels.

        Bars before sufficient history are labelled CHOPPY.

        Parameters
        ----------
        prices : pd.Series
            Full close-price series.
        lookback : int
            Window used for all indicator calculations (default 50).

        Returns
        -------
        pd.Series
            String series with regime labels, same index as *prices*.
        """
        min_bars = lookback + 1
        labels: List[str] = []

        arr = prices.values.astype(float)
        n   = len(arr)

        for i in range(n):
            if i < min_bars - 1:
                labels.append(CHOPPY)
                continue
            window = arr[: i + 1]
            # Use up to 2*lookback bars for warm-up
            start = max(0, i + 1 - lookback * 2)
            labels.append(_classify_one_arr(arr[start : i + 1], lookback))

        return pd.Series(labels, index=prices.index)


# ── Internal computation ───────────────────────────────────────────────────────

def _classify_one(prices: pd.Series, lookback: int) -> str:
    arr = prices.values.astype(float)
    return _classify_one_arr(arr, lookback)


def _classify_one_arr(arr: np.ndarray, lookback: int) -> str:
    """Classify from a numpy array.  arr must have >= lookback+1 elements.

    Priority order:
    1. TRENDING — ADX above threshold always takes priority over squeeze.
    2. SQUEEZE  — Only tested when ADX is below the trend threshold.
    3. CHOPPY   — Default when nothing else matches.
    """
    n = len(arr)
    if n < lookback + 1:
        return CHOPPY

    close = arr

    # ── ADX (close-price approximation) — checked FIRST ─────────────────── #
    adx_val = _adx(close, lookback)

    # ── SMA slope for direction ───────────────────────────────────────────── #
    sma = _sma(close, lookback)
    slope_bullish = sma[-1] > sma[-2] if len(sma) >= 2 else True

    if adx_val >= _ADX_TREND_THRESHOLD:
        return TRENDING_UP if slope_bullish else TRENDING_DOWN

    # ── Bollinger Band width squeeze — only when not trending ────────────── #
    bb_width = _bb_width(close, lookback)
    if _is_squeeze(close, lookback, bb_width):
        return SQUEEZE

    return CHOPPY


# ── Indicator helpers ──────────────────────────────────────────────────────────

def _sma(close: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average (trailing window)."""
    n = len(close)
    result = np.zeros(n)
    for i in range(period - 1, n):
        result[i] = close[i - period + 1 : i + 1].mean()
    return result


def _bb_width(close: np.ndarray, period: int) -> float:
    """Current Bollinger Band width = (upper - lower) / middle.

    Uses 2 standard deviations.
    """
    if len(close) < period:
        return float("inf")
    window = close[-period:]
    mean = window.mean()
    std  = window.std(ddof=1)
    if mean == 0:
        return 0.0
    return (4 * std) / mean  # (upper - lower) / middle = 4σ / μ


def _is_squeeze(close: np.ndarray, period: int, current_width: float) -> bool:
    """Return True when BB width is at or below the rolling minimum of recent widths.

    Requires at least 2×period bars to compute a meaningful rolling history.
    Returns a plain Python bool (not numpy.bool_) for safe ``is True`` checks.
    """
    if len(close) < period * 2:
        return False

    # Compute BB widths for each of the last period*2 windows
    widths: List[float] = []
    for i in range(period, len(close)):
        w = close[i - period : i]
        mean = float(w.mean())
        if mean == 0:
            continue
        std = float(w.std(ddof=1))
        widths.append((4 * std) / mean)

    if not widths:
        return False

    rolling_low = min(widths[-period:]) if len(widths) >= period else min(widths)
    return bool(current_width <= rolling_low)


def _adx(close: np.ndarray, period: int) -> float:
    """Simplified ADX using only close prices.

    Since we don't have high/low, we use close-to-close changes as a proxy
    for +DM and -DM, and absolute daily change as a proxy for True Range.

    This captures the *strength* of a trend even without OHLC data.
    """
    n = len(close)
    if n < period + 2:
        return 0.0

    changes = np.diff(close)

    dm_pos = np.where(changes > 0, changes, 0.0)
    dm_neg = np.where(changes < 0, -changes, 0.0)
    tr     = np.abs(changes)

    # Wilder smoothing
    tr_smooth   = _wilder_smooth(tr, period)
    dm_pos_sm   = _wilder_smooth(dm_pos, period)
    dm_neg_sm   = _wilder_smooth(dm_neg, period)

    # DI values
    with np.errstate(divide="ignore", invalid="ignore"):
        di_pos = np.where(tr_smooth > 0, dm_pos_sm / tr_smooth * 100.0, 0.0)
        di_neg = np.where(tr_smooth > 0, dm_neg_sm / tr_smooth * 100.0, 0.0)

    # DX
    di_sum  = di_pos + di_neg
    with np.errstate(divide="ignore", invalid="ignore"):
        dx = np.where(di_sum > 0, np.abs(di_pos - di_neg) / di_sum * 100.0, 0.0)

    # ADX = Wilder smooth of DX
    adx_arr = _wilder_smooth(dx, period)
    return float(adx_arr[-1])


def _wilder_smooth(data: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (equivalent to EMA with alpha = 1/period)."""
    n = len(data)
    if n == 0:
        return data.copy()
    result = np.zeros(n)
    alpha = 1.0 / period
    result[0] = data[0]
    for i in range(1, n):
        result[i] = result[i - 1] * (1 - alpha) + data[i] * alpha
    return result
