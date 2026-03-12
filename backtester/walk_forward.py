"""
walk_forward.py — Walk-forward validation for backtesting strategies.

Walk-forward analysis splits historical data into overlapping train/test windows,
optimises parameters on each training window, and evaluates out-of-sample
performance on the subsequent test window.

Usage
-----
    from backtester.walk_forward import WalkForwardAnalysis
    from backtester.strategies.gaussian_channel import GaussianChannelStrategy

    wfa = WalkForwardAnalysis(
        strategy_cls    = GaussianChannelStrategy,
        df              = df,
        train_pct       = 0.7,    # 70% of each window for training
        windows         = 5,      # number of rolling windows
        initial_capital = 10_000,
    )
    report = wfa.run()
    print(report.summary())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type

import pandas as pd

from backtester.engine import BacktestEngine
from backtester.models import BacktestResult
from backtester.metrics import calculate_metrics, metrics_to_string
from backtester.optimizer import Optimizer
from backtester.strategy_base import StrategyBase

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class WFWindow:
    """A single walk-forward train/test window."""
    window_idx:       int
    train_start:      pd.Timestamp
    train_end:        pd.Timestamp
    test_start:       pd.Timestamp
    test_end:         pd.Timestamp
    best_params:      Dict[str, Any]
    in_sample:        Dict[str, Any]    # metrics on training data with best params
    out_of_sample:    Dict[str, Any]    # metrics on test data with best params
    is_overfit:       bool              # True if OOS performance is much worse


@dataclass
class WalkForwardReport:
    """Aggregated walk-forward results."""
    windows:          List[WFWindow]
    combined_oos:     Dict[str, Any]   # metrics over all out-of-sample periods chained
    efficiency_ratio: float            # avg(OOS profit factor) / avg(IS profit factor)
    overfitting_flags: int             # how many windows flagged as overfit

    def summary(self) -> str:
        lines = [
            "═" * 60,
            "  WALK-FORWARD ANALYSIS SUMMARY",
            "═" * 60,
            f"  Windows:             {len(self.windows)}",
            f"  Overfit flags:       {self.overfitting_flags} / {len(self.windows)}",
            f"  Efficiency ratio:    {self.efficiency_ratio:.4f}  (1.0 = perfect)",
            "",
            "  Combined Out-of-Sample Performance:",
        ]
        for k in ("net_profit_pct", "sharpe_ratio", "profit_factor", "max_drawdown_pct", "win_rate_pct"):
            if k in self.combined_oos:
                lines.append(f"    {k:<28} {self.combined_oos[k]}")
        lines.append("═" * 60)
        lines.append("")
        lines.append("  Per-Window Results (IS vs OOS):")
        lines.append(f"  {'Win':>3}  {'IS PF':>7}  {'OOS PF':>8}  {'IS Sharpe':>10}  {'OOS Sharpe':>11}  Overfit")
        lines.append("  " + "─" * 55)
        for w in self.windows:
            is_pf  = w.in_sample.get("profit_factor", 0)
            oos_pf = w.out_of_sample.get("profit_factor", 0)
            is_sh  = w.in_sample.get("sharpe_ratio", 0)
            oos_sh = w.out_of_sample.get("sharpe_ratio", 0)
            flag   = " ⚠ OVERFIT" if w.is_overfit else ""
            lines.append(
                f"  {w.window_idx:>3}  {is_pf:>7.3f}  {oos_pf:>8.3f}"
                f"  {is_sh:>10.3f}  {oos_sh:>11.3f}{flag}"
            )
        lines.append("═" * 60)
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Walk-forward engine
# ──────────────────────────────────────────────────────────────────────────────

class WalkForwardAnalysis:
    """
    Rolling walk-forward analysis.

    Parameters
    ----------
    strategy_cls : type[StrategyBase]
        Strategy class to optimise and test.
    df : pd.DataFrame
        Full OHLCV history (DatetimeIndex).
    train_pct : float
        Fraction of each window used for training (0 < train_pct < 1).
    windows : int
        Number of rolling windows.  The full dataset is divided into
        (windows + 1) equal segments: first window uses segments 1..N
        for training and segment N+1 for testing, then rolls forward.
    initial_capital : float
    commission : float
    slippage_pct : float
    opt_sort_by : str
        Metric used to select best params during optimisation.
    overfit_threshold : float
        OOS profit_factor / IS profit_factor below this → flagged as overfit.
    workers : int
        Multiprocessing workers for inner optimiser.
    """

    def __init__(
        self,
        strategy_cls: Type[StrategyBase],
        df: pd.DataFrame,
        train_pct: float = 0.7,
        windows: int = 5,
        initial_capital: float = 10_000.0,
        commission: float = 0.0004,
        slippage_pct: float = 0.0001,
        opt_sort_by: str = "sharpe_ratio",
        overfit_threshold: float = 0.5,
        workers: int = 1,
    ) -> None:
        if not 0 < train_pct < 1:
            raise ValueError("train_pct must be between 0 and 1 (exclusive)")
        if windows < 1:
            raise ValueError("windows must be >= 1")
        if len(df) < 50:
            raise ValueError("DataFrame too short for walk-forward analysis")

        self.strategy_cls       = strategy_cls
        self.df                 = df.copy()
        self.train_pct          = train_pct
        self.windows            = windows
        self.initial_capital    = initial_capital
        self.commission         = commission
        self.slippage_pct       = slippage_pct
        self.opt_sort_by        = opt_sort_by
        self.overfit_threshold  = overfit_threshold
        self.workers            = workers

    # ------------------------------------------------------------------ #

    def run(self) -> WalkForwardReport:
        """Execute the walk-forward analysis and return a full report."""
        df = self.df
        n  = len(df)
        wf_windows: List[WFWindow] = []
        oos_equity_pieces: List[pd.Series] = []

        # Divide data into (windows + 1) equal-size anchor blocks.
        # Each walk-forward step uses anchored expanding windows:
        #   Train: df[0 : train_end_idx]
        #   Test:  df[train_end_idx : test_end_idx]
        total_blocks   = self.windows + 1
        block_size     = n // total_blocks

        for w in range(self.windows):
            test_end_idx   = block_size * (w + 2)        # exclusive
            train_end_idx  = int(test_end_idx * self.train_pct)
            train_start_idx = 0  # anchored walk-forward (expanding window)

            train_df = df.iloc[train_start_idx:train_end_idx]
            test_df  = df.iloc[train_end_idx:test_end_idx]

            if len(train_df) < 50 or len(test_df) < 10:
                logger.warning("Window %d: insufficient data, skipping.", w)
                continue

            logger.info(
                "WFA window %d: train %s→%s (%d bars) | test %s→%s (%d bars)",
                w,
                train_df.index[0].date(), train_df.index[-1].date(), len(train_df),
                test_df.index[0].date(),  test_df.index[-1].date(),  len(test_df),
            )

            # ── Optimise on training data ─────────────────────────────── #
            opt = Optimizer(
                strategy_cls   = self.strategy_cls,
                df             = train_df,
                initial_capital= self.initial_capital,
                commission     = self.commission,
                slippage_pct   = self.slippage_pct,
                workers        = self.workers,
            )
            opt.run(sort_by=self.opt_sort_by)

            if not opt.results:
                logger.warning("Window %d: optimiser returned no results, skipping.", w)
                continue

            best_params = opt.best_params(sort_by=self.opt_sort_by)
            _, is_metrics = opt.results[0]

            # ── Evaluate on test (out-of-sample) data ─────────────────── #
            oos_strategy = self.strategy_cls()
            oos_engine = BacktestEngine(
                strategy        = oos_strategy,
                df              = test_df,
                initial_capital = self.initial_capital,
                commission      = self.commission,
                slippage_pct    = self.slippage_pct,
                strategy_params = best_params,
            )
            oos_result = oos_engine.run()
            oos_metrics = oos_result.metrics
            oos_equity_pieces.append(oos_result.equity_curve)

            # ── Overfit detection ─────────────────────────────────────── #
            is_pf  = float(is_metrics.get("profit_factor", 0))
            oos_pf = float(oos_metrics.get("profit_factor", 0))
            is_overfit = (
                is_pf > 0
                and oos_pf / is_pf < self.overfit_threshold
            ) or (is_pf > 1.5 and oos_pf < 1.0)

            wf_windows.append(WFWindow(
                window_idx    = w,
                train_start   = train_df.index[0],
                train_end     = train_df.index[-1],
                test_start    = test_df.index[0],
                test_end      = test_df.index[-1],
                best_params   = best_params,
                in_sample     = is_metrics,
                out_of_sample = oos_metrics,
                is_overfit    = is_overfit,
            ))

        # ── Combined OOS equity curve ─────────────────────────────────── #
        if oos_equity_pieces:
            # Scale each piece so they chain together (relative returns)
            combined_equity_values: List[float] = []
            running_equity = self.initial_capital
            for piece in oos_equity_pieces:
                piece_initial = float(piece.iloc[0])
                for val in piece.values:
                    combined_equity_values.append(running_equity * val / piece_initial)
                running_equity = combined_equity_values[-1]

            combined_index = pd.DatetimeIndex(
                [ts for piece in oos_equity_pieces for ts in piece.index]
            )
            combined_equity = pd.Series(combined_equity_values, index=combined_index)
            combined_oos_metrics = calculate_metrics(
                trades          = [],    # no per-trade detail across windows
                equity_curve    = combined_equity,
                initial_capital = self.initial_capital,
            )
        else:
            combined_oos_metrics = {}

        # ── Efficiency ratio ─────────────────────────────────────────── #
        is_pf_vals  = [w.in_sample.get("profit_factor", 1) for w in wf_windows]
        oos_pf_vals = [w.out_of_sample.get("profit_factor", 0) for w in wf_windows]
        if is_pf_vals and any(v > 0 for v in is_pf_vals):
            avg_is  = sum(is_pf_vals) / len(is_pf_vals)
            avg_oos = sum(oos_pf_vals) / len(oos_pf_vals)
            efficiency = avg_oos / avg_is if avg_is > 0 else 0.0
        else:
            efficiency = 0.0

        overfit_count = sum(1 for w in wf_windows if w.is_overfit)

        return WalkForwardReport(
            windows           = wf_windows,
            combined_oos      = combined_oos_metrics,
            efficiency_ratio  = round(efficiency, 4),
            overfitting_flags = overfit_count,
        )
