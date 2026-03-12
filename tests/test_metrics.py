"""
test_metrics.py — Tests for the metrics calculator with known data.
"""

import math

import numpy as np
import pandas as pd
import pytest

from backtester.models import Trade
from backtester.metrics import (
    calculate_metrics,
    _sharpe,
    _sortino,
    _cagr,
    _consecutive_streaks,
    metrics_to_string,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers to build known trade / equity data
# ──────────────────────────────────────────────────────────────────────────────

def make_equity(values, freq="1d") -> pd.Series:
    idx = pd.date_range("2023-01-01", periods=len(values), freq=freq, tz="UTC")
    return pd.Series(values, index=idx, name="equity")


def make_trade(pnl: float, direction: str = "LONG", duration_h: int = 24, idx: int = 0) -> Trade:
    from datetime import timedelta
    entry = pd.Timestamp("2023-01-01", tz="UTC") + timedelta(days=idx)
    exit_ = entry + timedelta(hours=duration_h)
    size  = 1000.0
    return Trade(
        trade_id    = idx,
        direction   = direction,
        entry_time  = entry,
        exit_time   = exit_,
        entry_price = 100.0,
        exit_price  = 100.0 + pnl / (size / 100),
        size_usd    = size,
        pnl         = pnl,
        pnl_pct     = pnl / size * 100,
        commission  = 0.0,
        exit_reason = "signal",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Tests: basic metrics
# ──────────────────────────────────────────────────────────────────────────────

class TestBasicMetrics:

    def test_net_profit_positive(self):
        equity = make_equity([10_000, 10_200, 10_500, 11_000])
        m = calculate_metrics([], equity, initial_capital=10_000)
        assert m["net_profit_usd"] == pytest.approx(1000.0, abs=0.01)
        assert m["net_profit_pct"] == pytest.approx(10.0, abs=0.01)

    def test_net_profit_negative(self):
        equity = make_equity([10_000, 9_800, 9_500, 9_000])
        m = calculate_metrics([], equity, initial_capital=10_000)
        assert m["net_profit_usd"] == pytest.approx(-1000.0, abs=0.01)
        assert m["net_profit_pct"] < 0

    def test_zero_profit(self):
        equity = make_equity([10_000] * 30)
        m = calculate_metrics([], equity, initial_capital=10_000)
        assert m["net_profit_usd"] == pytest.approx(0.0, abs=0.01)

    def test_final_equity_matches_last_bar(self):
        equity = make_equity([10_000, 10_500, 11_234.56])
        m = calculate_metrics([], equity, 10_000)
        assert m["final_equity"] == pytest.approx(11_234.56, abs=0.01)


class TestTradeMetrics:

    def test_total_trades(self):
        trades = [make_trade(100, idx=i) for i in range(7)]
        equity = make_equity([10_000 + i * 100 for i in range(8)])
        m = calculate_metrics(trades, equity, 10_000)
        assert m["total_trades"] == 7

    def test_win_rate(self):
        trades = [make_trade(50, idx=i) for i in range(3)] + [make_trade(-50, idx=i+3) for i in range(2)]
        equity = make_equity([10_000 + i * 10 for i in range(6)])
        m = calculate_metrics(trades, equity, 10_000)
        assert m["winning_trades"] == 3
        assert m["losing_trades"]  == 2
        assert m["win_rate_pct"]   == pytest.approx(60.0, abs=0.01)

    def test_avg_win_avg_loss(self):
        wins  = [make_trade(100, idx=i)    for i in range(3)]
        loses = [make_trade(-50,  idx=i+3) for i in range(2)]
        equity = make_equity([10_000 + i * 20 for i in range(6)])
        m = calculate_metrics(wins + loses, equity, 10_000)
        assert m["avg_win_usd"]  == pytest.approx(100.0, abs=0.01)
        assert m["avg_loss_usd"] == pytest.approx(-50.0, abs=0.01)

    def test_profit_factor(self):
        # 3 wins × $100 = $300 gross profit; 2 losses × $50 = $100 gross loss → PF = 3.0
        wins  = [make_trade(100, idx=i)    for i in range(3)]
        loses = [make_trade(-50,  idx=i+3) for i in range(2)]
        equity = make_equity([10_000 + i * 40 for i in range(6)])
        m = calculate_metrics(wins + loses, equity, 10_000)
        assert m["profit_factor"] == pytest.approx(3.0, abs=0.001)

    def test_profit_factor_no_losers(self):
        trades = [make_trade(100, idx=i) for i in range(5)]
        equity = make_equity([10_000 + i * 100 for i in range(6)])
        m = calculate_metrics(trades, equity, 10_000)
        assert m["profit_factor"] == float("inf")

    def test_expectancy(self):
        # 2 wins $100, 1 loss $-40 → wr=0.667 → expectancy = 0.667*100 + 0.333*(-40) ≈ 53.33
        trades = [make_trade(100, idx=0), make_trade(100, idx=1), make_trade(-40, idx=2)]
        equity = make_equity([10_000, 10_100, 10_200, 10_160])
        m = calculate_metrics(trades, equity, 10_000)
        expected = (2/3 * 100) + (1/3 * -40)
        assert m["expectancy_usd"] == pytest.approx(expected, abs=0.5)


class TestDrawdown:

    def test_max_drawdown_known(self):
        # peak 12000, then drops to 10000 → dd = -2000, -16.67%
        equity = make_equity([10_000, 11_000, 12_000, 11_500, 10_000, 10_500])
        m = calculate_metrics([], equity, 10_000)
        assert m["max_drawdown_usd"] == pytest.approx(-2000.0, abs=1.0)
        assert m["max_drawdown_pct"] == pytest.approx(-100 * 2000 / 12000, abs=0.1)

    def test_no_drawdown_in_uptrend(self):
        equity = make_equity([10_000, 10_100, 10_200, 10_300, 10_400])
        m = calculate_metrics([], equity, 10_000)
        assert m["max_drawdown_usd"] == pytest.approx(0.0, abs=0.01)

    def test_recovery_factor(self):
        equity = make_equity([10_000, 12_000, 10_000, 13_000])  # dd = -2000, net = +3000
        m = calculate_metrics([], equity, 10_000)
        # recovery = net_profit / |max_dd| = 3000 / 2000 = 1.5
        assert m["recovery_factor"] == pytest.approx(1.5, abs=0.01)


class TestRatios:

    def test_sharpe_positive_for_uptrend(self):
        rng    = np.random.default_rng(0)
        values = np.cumprod(1 + rng.normal(0.001, 0.005, 500)) * 10_000
        equity = make_equity(values, freq="4h")
        m = calculate_metrics([], equity, 10_000)
        assert m["sharpe_ratio"] > 0

    def test_sharpe_zero_for_flat(self):
        equity = make_equity([10_000] * 100)
        m = calculate_metrics([], equity, 10_000)
        assert m["sharpe_ratio"] == pytest.approx(0.0, abs=0.001)

    def test_sortino_geq_sharpe_for_uptrend(self):
        """Sortino ≥ Sharpe when gains > losses (fewer downside deviations)."""
        rng    = np.random.default_rng(7)
        values = np.cumprod(1 + np.abs(rng.normal(0.002, 0.003, 300))) * 10_000
        equity = make_equity(values)
        m = calculate_metrics([], equity, 10_000)
        assert m["sortino_ratio"] >= m["sharpe_ratio"] - 0.01  # allow tiny float error

    def test_cagr_sign(self):
        equity_up   = make_equity([10_000, 11_000, 12_000, 13_000])
        equity_down = make_equity([10_000,  9_000,  8_000,  7_000])
        m_up   = calculate_metrics([], equity_up,   10_000)
        m_down = calculate_metrics([], equity_down, 10_000)
        assert m_up["cagr_pct"]   > 0
        assert m_down["cagr_pct"] < 0

    def test_calmar_positive_profit(self):
        rng    = np.random.default_rng(3)
        values = np.cumprod(1 + rng.normal(0.002, 0.005, 300)) * 10_000
        equity = make_equity(values)
        m = calculate_metrics([], equity, 10_000)
        # Calmar is meaningful only when there's a drawdown
        if m["max_drawdown_pct"] < 0:
            assert m["calmar_ratio"] >= 0


class TestConsecutiveStreaks:

    def test_all_wins(self):
        trades = [make_trade(10, idx=i) for i in range(5)]
        w, l = _consecutive_streaks(trades)
        assert w == 5
        assert l == 0

    def test_alternating(self):
        trades = [make_trade(10 if i % 2 == 0 else -10, idx=i) for i in range(6)]
        w, l = _consecutive_streaks(trades)
        assert w == 1
        assert l == 1

    def test_streak_in_middle(self):
        pnls  = [10, 10, -5, -5, -5, 10]
        trades = [make_trade(p, idx=i) for i, p in enumerate(pnls)]
        w, l = _consecutive_streaks(trades)
        assert l == 3
        assert w == 2

    def test_empty_trades(self):
        w, l = _consecutive_streaks([])
        assert w == 0
        assert l == 0


class TestDurationStats:

    def test_avg_duration(self):
        trades = [make_trade(10, idx=i, duration_h=8) for i in range(5)]
        equity = make_equity([10_000 + i * 10 for i in range(6)])
        m = calculate_metrics(trades, equity, 10_000)
        assert m["avg_trade_duration_hours"] == pytest.approx(8.0, abs=0.1)


class TestMetricsString:

    def test_to_string_runs(self):
        equity = make_equity([10_000, 10_500, 10_300, 11_000])
        m = calculate_metrics([], equity, 10_000)
        s = metrics_to_string(m)
        assert "BACKTEST RESULTS" in s
        assert "Net Profit" in s
