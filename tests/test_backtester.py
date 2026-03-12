"""
test_backtester.py — Tests for the backtesting engine core.
"""

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from backtester.engine import BacktestEngine
from backtester.models import BacktestResult
from backtester.strategy_base import Action, Signal, StrategyBase, HOLD_SIGNAL


# ──────────────────────────────────────────────────────────────────────────────
# Test fixtures
# ──────────────────────────────────────────────────────────────────────────────

def make_ohlcv(n: int = 200, start_price: float = 100.0, trend: float = 0.001) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame with a gentle uptrend."""
    rng   = np.random.default_rng(42)
    dates = pd.date_range("2023-01-01", periods=n, freq="4h", tz="UTC")
    close = np.cumprod(1 + rng.normal(trend, 0.01, n)) * start_price
    high  = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low   = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol   = rng.uniform(100, 1000, n)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": vol,
    }, index=dates)


# ──────────────────────────────────────────────────────────────────────────────
# Dummy strategies
# ──────────────────────────────────────────────────────────────────────────────

class AlwaysLongStrategy(StrategyBase):
    """Goes LONG on bar 1 and never closes — useful for deterministic P&L test."""
    name = "always_long"
    default_params = {}

    def on_candle(self, candle, history):
        if len(history) == 1:
            return Signal(action=Action.LONG)
        return HOLD_SIGNAL


class AlwaysShortStrategy(StrategyBase):
    """Goes SHORT on bar 1 and never closes."""
    name = "always_short"
    default_params = {}

    def on_candle(self, candle, history):
        if len(history) == 1:
            return Signal(action=Action.SHORT)
        return HOLD_SIGNAL


class BuyAndCloseStrategy(StrategyBase):
    """Goes LONG on bar 10, closes on bar 20."""
    name = "buy_and_close"
    default_params = {}

    def on_candle(self, candle, history):
        n = len(history)
        if n == 10:
            return Signal(action=Action.LONG, comment="open")
        if n == 20:
            return Signal(action=Action.CLOSE, comment="close")
        return HOLD_SIGNAL


class ReverseStrategy(StrategyBase):
    """LONG on bar 5, reverses to SHORT on bar 15."""
    name = "reverse"
    default_params = {}

    def on_candle(self, candle, history):
        n = len(history)
        if n == 5:
            return Signal(action=Action.LONG)
        if n == 15:
            return Signal(action=Action.SHORT)
        return HOLD_SIGNAL


class SLStrategy(StrategyBase):
    """Goes LONG with a very tight SL to test stop-loss triggering."""
    name = "sl_test"
    default_params = {"sl_pct": 0.001}

    def on_candle(self, candle, history):
        if len(history) == 2:
            price = float(candle["close"])
            sl    = price * (1 - self.params["sl_pct"])
            return Signal(action=Action.LONG, sl=sl)
        return HOLD_SIGNAL


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

class TestBacktestEngine:

    def test_returns_result(self):
        df = make_ohlcv(100)
        result = BacktestEngine(AlwaysLongStrategy(), df).run()
        assert isinstance(result, BacktestResult)

    def test_equity_curve_length(self):
        df = make_ohlcv(100)
        result = BacktestEngine(AlwaysLongStrategy(), df).run()
        assert len(result.equity_curve) == 100

    def test_equity_curve_datetime_index(self):
        df = make_ohlcv(50)
        result = BacktestEngine(AlwaysLongStrategy(), df).run()
        assert isinstance(result.equity_curve.index, pd.DatetimeIndex)

    def test_no_trades_when_hold(self):
        class HoldStrategy(StrategyBase):
            name = "hold"; default_params = {}
            def on_candle(self, c, h): return HOLD_SIGNAL

        df = make_ohlcv(50)
        result = BacktestEngine(HoldStrategy(), df).run()
        assert result.trades == []
        # Equity should drift only due to nothing happening (but commission not paid)
        assert result.equity_curve.iloc[-1] == pytest.approx(10_000.0, abs=1.0)

    def test_long_trade_is_recorded(self):
        df = make_ohlcv(100)
        result = BacktestEngine(BuyAndCloseStrategy(), df).run()
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.direction == "LONG"
        assert trade.exit_reason == "signal"

    def test_end_of_data_close(self):
        df = make_ohlcv(50)
        result = BacktestEngine(AlwaysLongStrategy(), df).run()
        # Position should be force-closed at end
        assert len(result.trades) == 1
        assert result.trades[0].exit_reason == "end_of_data"

    def test_long_pnl_direction(self):
        """In an uptrending market, always-long should profit."""
        df = make_ohlcv(200, trend=0.003)
        result = BacktestEngine(AlwaysLongStrategy(), df, commission=0.0).run()
        trade = result.trades[0]
        assert trade.pnl > 0, f"Expected profit in uptrend, got {trade.pnl}"

    def test_short_pnl_direction(self):
        """In a strong downtrend, short should profit."""
        rng   = np.random.default_rng(1)
        dates = pd.date_range("2023-01-01", periods=100, freq="4h", tz="UTC")
        close = np.cumprod(1 + rng.normal(-0.005, 0.005, 100)) * 100
        high  = close * 1.001
        low   = close * 0.999
        df    = pd.DataFrame({"open": close, "high": high, "low": low,
                              "close": close, "volume": np.ones(100)}, index=dates)
        result = BacktestEngine(AlwaysShortStrategy(), df, commission=0.0).run()
        trade  = result.trades[0]
        assert trade.pnl > 0, f"Expected profit in downtrend, got {trade.pnl}"

    def test_commission_reduces_pnl(self):
        df = make_ohlcv(50)
        r_no_comm = BacktestEngine(BuyAndCloseStrategy(), df, commission=0.0).run()
        r_comm    = BacktestEngine(BuyAndCloseStrategy(), df, commission=0.001).run()
        assert r_comm.trades[0].pnl < r_no_comm.trades[0].pnl

    def test_reverse_creates_two_trades(self):
        df = make_ohlcv(100)
        result = BacktestEngine(ReverseStrategy(), df).run()
        # Bar 5 → LONG, bar 15 → SHORT (closes long + opens short), end → closes short
        assert len(result.trades) == 2
        assert result.trades[0].direction == "LONG"
        assert result.trades[1].direction == "SHORT"

    def test_stop_loss_triggered(self):
        """A tight SL should trigger within the simulation."""
        df = make_ohlcv(100)
        result = BacktestEngine(SLStrategy(), df, slippage_pct=0.0).run()
        sl_trades = [t for t in result.trades if t.exit_reason == "sl"]
        assert len(sl_trades) >= 1, "Expected at least one SL-triggered trade"

    def test_metrics_present(self):
        df = make_ohlcv(100)
        result = BacktestEngine(BuyAndCloseStrategy(), df).run()
        m = result.metrics
        for key in ("net_profit_usd", "win_rate_pct", "sharpe_ratio",
                    "max_drawdown_pct", "profit_factor"):
            assert key in m, f"Missing metric: {key}"

    def test_drawdown_non_positive(self):
        df = make_ohlcv(100)
        result = BacktestEngine(AlwaysLongStrategy(), df).run()
        assert (result.drawdown <= 0).all(), "Drawdown values should be <= 0"

    def test_params_stored_in_result(self):
        df = make_ohlcv(50)
        result = BacktestEngine(BuyAndCloseStrategy(), df).run()
        assert isinstance(result.params, dict)

    def test_invalid_df_raises(self):
        bad_df = pd.DataFrame({"open": [1, 2], "close": [1, 2]})
        with pytest.raises((TypeError, ValueError)):
            BacktestEngine(AlwaysLongStrategy(), bad_df)

    def test_size_fraction(self):
        """Position size = 50% of equity should use half capital."""
        class HalfSize(StrategyBase):
            name = "half"; default_params = {}
            def on_candle(self, c, h):
                if len(h) == 1:
                    return Signal(action=Action.LONG, size=0.5)
                return HOLD_SIGNAL

        df = make_ohlcv(50)
        result = BacktestEngine(HalfSize(), df, commission=0.0).run()
        # Full trade's PnL should be ~half of full-size AlwaysLong
        r_full = BacktestEngine(AlwaysLongStrategy(), df, commission=0.0).run()
        half_pnl = result.trades[0].pnl
        full_pnl = r_full.trades[0].pnl
        assert abs(half_pnl) < abs(full_pnl) * 0.75, "Half-size trade should have smaller PnL"

    def test_warmup_bars(self):
        """No signals should be generated during warmup period."""
        class EarlyLong(StrategyBase):
            name = "early"; default_params = {}
            def on_candle(self, c, h):
                if len(h) == 3:   # would fire on bar 3 without warmup
                    return Signal(action=Action.LONG)
                return HOLD_SIGNAL

        df = make_ohlcv(50)
        result_no_wu = BacktestEngine(EarlyLong(), df, warmup_bars=0).run()
        result_wu    = BacktestEngine(EarlyLong(), df, warmup_bars=10).run()

        # With warmup=10, bar 3 is inside warmup so no trade
        assert len(result_no_wu.trades) >= 1
        assert len(result_wu.trades) == 0
