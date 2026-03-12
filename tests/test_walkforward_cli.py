"""
Tests for the walk-forward CLI script (scripts/run_walkforward.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

_PROJ_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = str(_PROJ_ROOT / "scripts" / "run_walkforward.py")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_dummy_df(n: int = 400) -> pd.DataFrame:
    """Generate a simple synthetic OHLCV DataFrame with DatetimeIndex."""
    import numpy as np

    rng = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    close = 30000 + np.cumsum(np.random.randn(n) * 200)
    high = close * 1.01
    low = close * 0.99
    open_ = close * 0.998
    volume = np.abs(np.random.randn(n) * 1000) + 500

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=rng,
    )
    return df


# ── Build-parser tests ────────────────────────────────────────────────────────

class TestBuildParser:
    def test_defaults(self):
        sys.path.insert(0, str(_PROJ_ROOT))
        from scripts.run_walkforward import build_parser
        parser = build_parser()
        args = parser.parse_args([])
        assert args.symbol == "BTCUSDT"
        assert args.timeframe == "1d"
        assert args.train_days == 365
        assert args.test_days == 90
        assert args.strategy == "gaussian"

    def test_custom_args(self):
        from scripts.run_walkforward import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "--symbol", "ETHUSDT",
            "--timeframe", "4h",
            "--train-days", "180",
            "--test-days", "60",
            "--strategy", "ma_cross",
            "--capital", "5000",
        ])
        assert args.symbol == "ETHUSDT"
        assert args.timeframe == "4h"
        assert args.train_days == 180
        assert args.test_days == 60
        assert args.strategy == "ma_cross"
        assert args.capital == 5000.0


# ── _compute_stats ────────────────────────────────────────────────────────────

class TestComputeStats:
    def test_empty_report(self):
        from scripts.run_walkforward import _compute_stats
        from backtester.walk_forward import WalkForwardReport
        empty_report = WalkForwardReport(
            windows=[], combined_oos={}, efficiency_ratio=0.0, overfitting_flags=0
        )
        stats = _compute_stats(empty_report)
        assert stats["n_windows"] == 0
        assert stats["avg_test_return"] == 0.0

    def test_with_windows(self):
        from scripts.run_walkforward import _compute_stats
        from backtester.walk_forward import WalkForwardReport, WFWindow
        import pandas as pd

        w1 = WFWindow(
            window_idx=0,
            train_start=pd.Timestamp("2022-01-01"),
            train_end=pd.Timestamp("2022-06-30"),
            test_start=pd.Timestamp("2022-07-01"),
            test_end=pd.Timestamp("2022-09-30"),
            best_params={"period": 100},
            in_sample={"net_profit_pct": 15.0, "profit_factor": 1.5},
            out_of_sample={"net_profit_pct": 8.0, "profit_factor": 1.2},
            is_overfit=False,
        )
        w2 = WFWindow(
            window_idx=1,
            train_start=pd.Timestamp("2022-01-01"),
            train_end=pd.Timestamp("2022-09-30"),
            test_start=pd.Timestamp("2022-10-01"),
            test_end=pd.Timestamp("2022-12-31"),
            best_params={"period": 100},
            in_sample={"net_profit_pct": 20.0, "profit_factor": 1.8},
            out_of_sample={"net_profit_pct": -3.0, "profit_factor": 0.8},
            is_overfit=True,
        )
        report = WalkForwardReport(
            windows=[w1, w2],
            combined_oos={"net_profit_pct": 5.0},
            efficiency_ratio=0.75,
            overfitting_flags=1,
        )
        stats = _compute_stats(report)
        assert stats["n_windows"] == 2
        assert stats["avg_test_return"] == pytest.approx(2.5)
        assert stats["best_fold_return"] == pytest.approx(8.0)
        assert stats["worst_fold_return"] == pytest.approx(-3.0)
        assert stats["positive_folds"] == 1
        assert stats["consistency_score"] == pytest.approx(0.5)
        assert stats["overfit_flags"] == 1


# ── _save_report ──────────────────────────────────────────────────────────────

class TestSaveReport:
    def test_creates_json_file(self, tmp_path):
        from scripts.run_walkforward import _save_report, _compute_stats
        from backtester.walk_forward import WalkForwardReport

        report = WalkForwardReport(
            windows=[], combined_oos={}, efficiency_ratio=0.0, overfitting_flags=0
        )
        stats = _compute_stats(report)

        args = MagicMock()
        args.symbol = "BTCUSDT"
        args.timeframe = "1d"
        args.strategy = "gaussian"
        args.train_days = 365
        args.test_days = 90
        args.report_dir = str(tmp_path / "reports")

        path = _save_report(report, stats, args)
        assert os.path.exists(path)
        assert path.endswith(".json")

    def test_json_content(self, tmp_path):
        from scripts.run_walkforward import _save_report, _compute_stats
        from backtester.walk_forward import WalkForwardReport

        report = WalkForwardReport(
            windows=[], combined_oos={}, efficiency_ratio=0.5, overfitting_flags=0
        )
        stats = _compute_stats(report)

        args = MagicMock()
        args.symbol = "ETHUSDT"
        args.timeframe = "4h"
        args.strategy = "gaussian"
        args.train_days = 180
        args.test_days = 60
        args.report_dir = str(tmp_path / "reports")

        path = _save_report(report, stats, args)
        with open(path) as fh:
            data = json.load(fh)

        assert data["symbol"] == "ETHUSDT"
        assert data["timeframe"] == "4h"
        assert data["strategy"] == "gaussian"
        assert "generated_at" in data
        assert "summary" in data
        assert "windows" in data

    def test_filename_includes_date(self, tmp_path):
        from scripts.run_walkforward import _save_report, _compute_stats
        from backtester.walk_forward import WalkForwardReport

        report = WalkForwardReport(
            windows=[], combined_oos={}, efficiency_ratio=0.0, overfitting_flags=0
        )
        stats = _compute_stats(report)

        args = MagicMock()
        args.symbol = "BTCUSDT"
        args.timeframe = "1d"
        args.strategy = "gaussian"
        args.train_days = 365
        args.test_days = 90
        args.report_dir = str(tmp_path / "reports")

        path = _save_report(report, stats, args)
        today = date.today().isoformat()
        assert today in path


# ── CLI integration (with mocked data pipeline + WFA) ────────────────────────

class TestCLIIntegration:
    def test_basic_invocation_with_mocked_components(self, tmp_path):
        """Run main() with mocked data pipeline and WFA to verify it completes."""
        sys.path.insert(0, str(_PROJ_ROOT))
        from scripts.run_walkforward import main as wf_main
        from backtester.walk_forward import WalkForwardReport, WFWindow

        dummy_df = _make_dummy_df(400)

        mock_wf_report = WalkForwardReport(
            windows=[],
            combined_oos={},
            efficiency_ratio=0.0,
            overfitting_flags=0,
        )

        with patch("scripts.run_walkforward.DataPipeline") as MockPipeline, \
             patch("scripts.run_walkforward.WalkForwardAnalysis") as MockWFA, \
             patch("sys.argv", [
                 "run_walkforward.py",
                 "--symbol", "BTCUSDT",
                 "--timeframe", "1d",
                 "--train-days", "60",
                 "--test-days", "20",
                 "--windows", "2",
                 "--report-dir", str(tmp_path / "reports"),
             ]):

            mock_pipeline_inst = MagicMock()
            mock_pipeline_inst.fetch_candles.return_value = dummy_df
            mock_pipeline_inst.cache_dir = str(tmp_path / "candles")
            MockPipeline.return_value = mock_pipeline_inst

            mock_wfa_inst = MagicMock()
            mock_wfa_inst.run.return_value = mock_wf_report
            MockWFA.return_value = mock_wfa_inst

            wf_main()  # Should not raise

            mock_pipeline_inst.fetch_candles.assert_called_once()
            mock_wfa_inst.run.assert_called_once()
