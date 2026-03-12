"""
test_optimizer.py — Tests for the parameter optimizer.
"""

from typing import Any, Dict, List

import numpy as np
import pandas as pd
import pytest

from backtester.optimizer import Optimizer, _product_dicts
from backtester.strategy_base import Action, Signal, StrategyBase, HOLD_SIGNAL


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_df(n: int = 150, trend: float = 0.001) -> pd.DataFrame:
    rng   = np.random.default_rng(99)
    dates = pd.date_range("2023-01-01", periods=n, freq="4h", tz="UTC")
    close = np.cumprod(1 + rng.normal(trend, 0.01, n)) * 100.0
    high  = close * 1.001
    low   = close * 0.999
    return pd.DataFrame({
        "open":   close, "high": high,
        "low":    low,   "close": close,
        "volume": np.ones(n),
    }, index=dates)


class ParametricStrategy(StrategyBase):
    """Strategy that buys after `wait` bars and closes after another `hold` bars."""
    name = "parametric"
    default_params = {"wait": 10, "hold": 20}
    param_ranges: Dict[str, List[Any]] = {
        "wait": [5, 10, 15],
        "hold": [10, 20],
    }

    def on_candle(self, candle, history):
        n = len(history)
        wait = int(self.params["wait"])
        hold = int(self.params["hold"])
        if n == wait:
            return Signal(action=Action.LONG)
        if n == wait + hold:
            return Signal(action=Action.CLOSE)
        return HOLD_SIGNAL


# ──────────────────────────────────────────────────────────────────────────────
# Tests: _product_dicts
# ──────────────────────────────────────────────────────────────────────────────

class TestProductDicts:

    def test_single_key(self):
        result = list(_product_dicts({"a": [1, 2, 3]}))
        assert result == [{"a": 1}, {"a": 2}, {"a": 3}]

    def test_two_keys(self):
        result = list(_product_dicts({"a": [1, 2], "b": ["x", "y"]}))
        assert len(result) == 4
        assert {"a": 1, "b": "x"} in result
        assert {"a": 2, "b": "y"} in result

    def test_empty_ranges(self):
        result = list(_product_dicts({}))
        assert result == [{}]

    def test_count_matches_product(self):
        ranges = {"a": [1, 2], "b": [3, 4, 5], "c": [True, False]}
        result = list(_product_dicts(ranges))
        assert len(result) == 2 * 3 * 2

    def test_all_unique(self):
        result = list(_product_dicts({"x": [1, 2, 3], "y": [4, 5]}))
        seen = set()
        for r in result:
            key = tuple(sorted(r.items()))
            assert key not in seen, f"Duplicate: {r}"
            seen.add(key)


# ──────────────────────────────────────────────────────────────────────────────
# Tests: Optimizer
# ──────────────────────────────────────────────────────────────────────────────

class TestOptimizer:

    def test_returns_list(self):
        df  = make_df(200)
        opt = Optimizer(ParametricStrategy, df, workers=1)
        results = opt.run()
        assert isinstance(results, list)

    def test_result_count_matches_grid(self):
        """All 3 × 2 = 6 combos should produce a result."""
        df  = make_df(200)
        opt = Optimizer(ParametricStrategy, df, workers=1)
        results = opt.run()
        assert len(results) == 6

    def test_results_are_sorted(self):
        df  = make_df(200)
        opt = Optimizer(ParametricStrategy, df, workers=1)
        results = opt.run(sort_by="net_profit_pct")
        # First result should have highest (or equal) net_profit_pct
        vals = [r[1].get("net_profit_pct", float("-inf")) for r in results]
        for i in range(len(vals) - 1):
            assert vals[i] >= vals[i + 1], f"Not sorted: {vals[i]} < {vals[i+1]} at index {i}"

    def test_results_contain_required_keys(self):
        df  = make_df(200)
        opt = Optimizer(ParametricStrategy, df, workers=1)
        results = opt.run()
        for params, metrics in results:
            assert "wait" in params
            assert "hold" in params
            assert "sharpe_ratio" in metrics
            assert "net_profit_pct" in metrics

    def test_best_params_returns_dict(self):
        df  = make_df(200)
        opt = Optimizer(ParametricStrategy, df, workers=1)
        opt.run()
        best = opt.best_params()
        assert isinstance(best, dict)
        assert "wait" in best
        assert "hold" in best

    def test_best_params_without_run_raises(self):
        df  = make_df(100)
        opt = Optimizer(ParametricStrategy, df, workers=1)
        with pytest.raises(RuntimeError):
            opt.best_params()

    def test_top_n_limits_results(self):
        df  = make_df(200)
        opt = Optimizer(ParametricStrategy, df, workers=1)
        results = opt.run(top_n=3)
        assert len(results) <= 3

    def test_save_csv(self, tmp_path):
        df  = make_df(200)
        opt = Optimizer(ParametricStrategy, df, workers=1)
        opt.run()
        csv_path = tmp_path / "results.csv"
        out = opt.save_csv(str(csv_path))
        assert out.exists()
        content = out.read_text()
        assert "param_wait" in content
        assert "metric_sharpe_ratio" in content

    def test_param_overrides(self):
        """param_overrides should restrict the swept parameters."""
        df  = make_df(200)
        opt = Optimizer(
            ParametricStrategy, df, workers=1,
            param_overrides={"wait": [5, 10]},   # override to 2 values only
        )
        results = opt.run()
        wait_vals = {r[0]["wait"] for r in results}
        assert wait_vals == {5, 10}

    def test_sort_by_sharpe(self):
        df  = make_df(200)
        opt = Optimizer(ParametricStrategy, df, workers=1)
        results = opt.run(sort_by="sharpe_ratio")
        vals = [r[1].get("sharpe_ratio", float("-inf")) for r in results]
        for i in range(len(vals) - 1):
            v_curr = vals[i] if vals[i] != float("inf") else 1e18
            v_next = vals[i+1] if vals[i+1] != float("inf") else 1e18
            assert v_curr >= v_next, f"Not sorted by sharpe at index {i}: {vals[i]} < {vals[i+1]}"
