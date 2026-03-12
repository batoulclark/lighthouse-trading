"""
Tests for scripts/auto_optimize.py

Covers:
- CLI argument parsing
- Symbol / timeframe defaults
- Report file generation and structure
- Param comparison logic
- Telegram summary sending
- Error handling (fetch failure, optimizer failure)
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make sure project root is on sys.path
_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from scripts.auto_optimize import (
    DEFAULT_SYMBOLS,
    DEFAULT_TIMEFRAMES,
    STRATEGIES,
    _compare_params,
    _clean_metrics,
    _parse_args,
    run_optimization,
    _send_telegram_summary,
    REPORTS_DIR,
)


# ── CLI parsing ───────────────────────────────────────────────────────────────

class TestParseArgs:
    def test_defaults(self):
        args = _parse_args.__wrapped__() if hasattr(_parse_args, "__wrapped__") else None
        # Test by calling with sys.argv patched
        with patch("sys.argv", ["auto_optimize.py"]):
            args = _parse_args()
        assert args.strategy == "gaussian"
        assert args.top_n == 5
        assert args.workers == 1
        assert args.sort_by == "sharpe_ratio"
        assert args.capital == 10_000.0

    def test_symbols_arg(self):
        with patch("sys.argv", ["auto_optimize.py", "--symbols", "BTCUSDT,ETHUSDT"]):
            args = _parse_args()
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        assert syms == ["BTCUSDT", "ETHUSDT"]

    def test_timeframes_arg(self):
        with patch("sys.argv", ["auto_optimize.py", "--timeframes", "1d,4h"]):
            args = _parse_args()
        tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
        assert tfs == ["1d", "4h"]

    def test_strategy_choices(self):
        for strategy in STRATEGIES:
            with patch("sys.argv", ["auto_optimize.py", "--strategy", strategy]):
                args = _parse_args()
            assert args.strategy == strategy

    def test_empty_symbols_falls_back_to_defaults(self):
        with patch("sys.argv", ["auto_optimize.py"]):
            args = _parse_args()
        result = [s.strip() for s in args.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS
        assert result == DEFAULT_SYMBOLS

    def test_start_end_dates(self):
        with patch("sys.argv", ["auto_optimize.py", "--start", "2022-01-01", "--end", "2023-01-01"]):
            args = _parse_args()
        assert args.start == "2022-01-01"
        assert args.end == "2023-01-01"

    def test_workers_and_top_n(self):
        with patch("sys.argv", ["auto_optimize.py", "--workers", "4", "--top-n", "10"]):
            args = _parse_args()
        assert args.workers == 4
        assert args.top_n == 10


# ── Param comparison ──────────────────────────────────────────────────────────

class TestCompareParams:
    def test_identical_params_no_changes(self):
        params = {"period": 100, "poles": 3, "multiplier": 2.0}
        changes = _compare_params(params, params.copy())
        assert changes == []

    def test_single_change(self):
        old = {"period": 100, "multiplier": 2.0}
        new = {"period": 200, "multiplier": 2.0}
        changes = _compare_params(old, new)
        assert len(changes) == 1
        assert "period" in changes[0]
        assert "100" in changes[0]
        assert "200" in changes[0]

    def test_multiple_changes(self):
        old = {"period": 100, "multiplier": 2.0, "poles": 3}
        new = {"period": 200, "multiplier": 3.0, "poles": 3}
        changes = _compare_params(old, new)
        assert len(changes) == 2

    def test_missing_key_in_new(self):
        old = {"period": 100, "extra": True}
        new = {"period": 100}
        changes = _compare_params(old, new)
        assert len(changes) == 1
        assert "extra" in changes[0]

    def test_new_key_not_in_old(self):
        old = {"period": 100}
        new = {"period": 100, "new_param": 42}
        changes = _compare_params(old, new)
        assert len(changes) == 1
        assert "new_param" in changes[0]


# ── Clean metrics ─────────────────────────────────────────────────────────────

class TestCleanMetrics:
    def test_nan_replaced_with_none(self):
        raw = {"sharpe_ratio": float("nan"), "total_return_pct": 10.0}
        clean = _clean_metrics(raw)
        assert clean["sharpe_ratio"] is None
        assert clean["total_return_pct"] == 10.0

    def test_nested_dicts_removed(self):
        raw = {"sharpe_ratio": 1.5, "breakdown": {"x": 1}}
        clean = _clean_metrics(raw)
        assert "breakdown" not in clean
        assert "sharpe_ratio" in clean

    def test_inf_preserved(self):
        raw = {"val": float("inf")}
        clean = _clean_metrics(raw)
        assert clean["val"] == float("inf")

    def test_strings_preserved(self):
        raw = {"label": "test", "count": 5}
        clean = _clean_metrics(raw)
        assert clean["label"] == "test"
        assert clean["count"] == 5


# ── run_optimization ──────────────────────────────────────────────────────────

class TestRunOptimization:
    def _mock_df(self, n_rows: int = 200):
        import pandas as pd
        import numpy as np
        idx = pd.date_range("2023-01-01", periods=n_rows, freq="1D")
        df = pd.DataFrame({
            "open":   np.random.uniform(40_000, 50_000, n_rows),
            "high":   np.random.uniform(50_000, 55_000, n_rows),
            "low":    np.random.uniform(35_000, 40_000, n_rows),
            "close":  np.random.uniform(40_000, 50_000, n_rows),
            "volume": np.random.uniform(1_000, 10_000, n_rows),
        }, index=idx)
        return df

    def test_fetch_error_returns_error_dict(self):
        with patch("scripts.auto_optimize.fetch_candles", side_effect=RuntimeError("network")):
            result = run_optimization(
                strategy_name="gaussian",
                symbol="BTCUSDT",
                timeframe="1d",
                start="2023-01-01",
                end="2024-01-01",
                top_n=3,
                workers=1,
                sort_by="sharpe_ratio",
                capital=10_000.0,
            )
        assert "error" in result
        assert result["symbol"] == "BTCUSDT"

    def test_empty_df_returns_error_dict(self):
        import pandas as pd
        with patch("scripts.auto_optimize.fetch_candles", return_value=pd.DataFrame()):
            result = run_optimization(
                strategy_name="gaussian",
                symbol="BTCUSDT",
                timeframe="1d",
                start="2023-01-01",
                end="2024-01-01",
                top_n=3,
                workers=1,
                sort_by="sharpe_ratio",
                capital=10_000.0,
            )
        assert "error" in result

    def test_optimizer_error_returns_error_dict(self):
        df = self._mock_df()
        with patch("scripts.auto_optimize.fetch_candles", return_value=df):
            with patch("scripts.auto_optimize.Optimizer") as MockOpt:
                MockOpt.return_value.run.side_effect = RuntimeError("optimizer crash")
                result = run_optimization(
                    strategy_name="gaussian",
                    symbol="BTCUSDT",
                    timeframe="1d",
                    start="2023-01-01",
                    end="2024-01-01",
                    top_n=3,
                    workers=1,
                    sort_by="sharpe_ratio",
                    capital=10_000.0,
                )
        assert "error" in result

    def test_success_has_required_keys(self):
        df = self._mock_df()
        mock_params  = {"period": 100, "multiplier": 2.0}
        mock_metrics = {"sharpe_ratio": 1.5, "total_return_pct": 20.0}

        with patch("scripts.auto_optimize.fetch_candles", return_value=df):
            with patch("scripts.auto_optimize.Optimizer") as MockOpt:
                MockOpt.return_value.run.return_value = [(mock_params, mock_metrics)]
                result = run_optimization(
                    strategy_name="gaussian",
                    symbol="BTCUSDT",
                    timeframe="1d",
                    start="2023-01-01",
                    end="2024-01-01",
                    top_n=3,
                    workers=1,
                    sort_by="sharpe_ratio",
                    capital=10_000.0,
                )
        assert "error" not in result
        assert result["symbol"] == "BTCUSDT"
        assert result["timeframe"] == "1d"
        assert "best_params" in result
        assert "best_metrics" in result
        assert "current_params" in result
        assert "param_changes" in result
        assert "top_results" in result
        assert result["bars"] == len(df)

    def test_no_results_returns_error_dict(self):
        df = self._mock_df()
        with patch("scripts.auto_optimize.fetch_candles", return_value=df):
            with patch("scripts.auto_optimize.Optimizer") as MockOpt:
                MockOpt.return_value.run.return_value = []
                result = run_optimization(
                    strategy_name="gaussian",
                    symbol="BTCUSDT",
                    timeframe="1d",
                    start="2023-01-01",
                    end="2024-01-01",
                    top_n=3,
                    workers=1,
                    sort_by="sharpe_ratio",
                    capital=10_000.0,
                )
        assert "error" in result


# ── Report generation ─────────────────────────────────────────────────────────

class TestReportGeneration:
    def test_report_saved_as_json(self, tmp_path):
        from scripts.auto_optimize import main
        df = self._mock_df()
        mock_params  = {"period": 100}
        mock_metrics = {"sharpe_ratio": 1.2}

        with patch("sys.argv", ["auto_optimize.py", "--symbols", "BTCUSDT", "--timeframes", "1d"]):
            with patch("scripts.auto_optimize.fetch_candles", return_value=df):
                with patch("scripts.auto_optimize.Optimizer") as MockOpt:
                    MockOpt.return_value.run.return_value = [(mock_params, mock_metrics)]
                    with patch("scripts.auto_optimize.REPORTS_DIR", tmp_path):
                        with patch("scripts.auto_optimize.TelegramNotifier") as MockTG:
                            MockTG.return_value.send = AsyncMock(return_value=True)
                            ret = main()

        assert ret == 0
        reports = list(tmp_path.glob("optimization_*.json"))
        assert len(reports) == 1

        with open(reports[0]) as f:
            data = json.load(f)

        assert "generated_at" in data
        assert "results" in data
        assert data["symbols"] == ["BTCUSDT"]
        assert data["timeframes"] == ["1d"]

    def _mock_df(self, n_rows: int = 200):
        import pandas as pd
        import numpy as np
        idx = pd.date_range("2023-01-01", periods=n_rows, freq="1D")
        return pd.DataFrame({
            "open":   np.random.uniform(40_000, 50_000, n_rows),
            "high":   np.random.uniform(50_000, 55_000, n_rows),
            "low":    np.random.uniform(35_000, 40_000, n_rows),
            "close":  np.random.uniform(40_000, 50_000, n_rows),
            "volume": np.random.uniform(1_000, 10_000, n_rows),
        }, index=pd.date_range("2023-01-01", periods=n_rows, freq="1D"))


# ── Telegram summary ──────────────────────────────────────────────────────────

class TestTelegramSummary:
    def test_sends_summary_with_successes(self):
        tg = AsyncMock()
        tg.send = AsyncMock(return_value=True)

        results = [
            {
                "symbol":       "BTCUSDT",
                "timeframe":    "1d",
                "strategy":     "gaussian",
                "best_metrics": {"sharpe_ratio": 1.5, "total_return_pct": 20.0},
                "param_changes": [],
                "params_changed": False,
            }
        ]
        report_path = Path("/tmp/optimization_20250101.json")

        asyncio.run(_send_telegram_summary(tg, results, report_path))
        tg.send.assert_awaited_once()
        msg = tg.send.call_args[0][0]
        assert "BTCUSDT" in msg
        assert "Auto-Optimizer" in msg

    def test_sends_summary_with_failures(self):
        tg = AsyncMock()
        tg.send = AsyncMock(return_value=True)

        results = [
            {"symbol": "BTCUSDT", "timeframe": "1d", "strategy": "gaussian", "error": "network error"}
        ]
        report_path = Path("/tmp/optimization_20250101.json")

        asyncio.run(_send_telegram_summary(tg, results, report_path))
        tg.send.assert_awaited_once()
        msg = tg.send.call_args[0][0]
        assert "Failed" in msg

    def test_includes_no_auto_apply_warning(self):
        tg = AsyncMock()
        tg.send = AsyncMock(return_value=True)

        asyncio.run(_send_telegram_summary(tg, [], Path("/tmp/opt.json")))
        msg = tg.send.call_args[0][0]
        assert "NOT auto-applied" in msg or "review" in msg.lower()
