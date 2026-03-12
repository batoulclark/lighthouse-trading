"""
optimizer.py — Grid-search parameter optimiser for backtesting strategies.

Usage
-----
    from backtester.optimizer import Optimizer
    from backtester.strategies.gaussian_channel import GaussianChannelStrategy

    opt = Optimizer(GaussianChannelStrategy, df, initial_capital=10_000)
    results = opt.run(sort_by="sharpe_ratio", top_n=20)
    opt.save_csv("data/reports/opt_results.csv")
"""

from __future__ import annotations

import csv
import itertools
import logging
import multiprocessing as mp
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

import pandas as pd

from backtester.engine import BacktestEngine
from backtester.models import BacktestResult
from backtester.strategy_base import StrategyBase

logger = logging.getLogger(__name__)

# Try to import tqdm; fall back to simple counter
try:
    from tqdm import tqdm as _tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


OptRow = Tuple[Dict[str, Any], Dict[str, Any]]   # (params, metrics)


# ──────────────────────────────────────────────────────────────────────────────
# Worker (must be module-level for multiprocessing pickle)
# ──────────────────────────────────────────────────────────────────────────────

def _run_single_direct(
    strategy_cls,
    df: pd.DataFrame,
    params: Dict[str, Any],
    engine_kwargs: Dict[str, Any],
) -> Optional[OptRow]:
    """Run one backtest directly (no serialisation).  Used in single-threaded mode."""
    try:
        strategy = strategy_cls()
        engine = BacktestEngine(
            strategy=strategy,
            df=df,
            strategy_params=params,
            **engine_kwargs,
        )
        result: BacktestResult = engine.run()
        return (params, result.metrics)
    except Exception as exc:
        logger.debug("Param combo %s failed: %s", params, exc)
        return None


def _run_single(args: Tuple) -> Optional[OptRow]:
    """Run one backtest from pickled args.  Used in multi-process mode."""
    import pickle
    (strategy_cls, df_pickle, params, engine_kwargs) = args
    try:
        df = pickle.loads(df_pickle)
        return _run_single_direct(strategy_cls, df, params, engine_kwargs)
    except Exception as exc:
        logger.debug("Param combo %s failed: %s", params, exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Optimiser class
# ──────────────────────────────────────────────────────────────────────────────

class Optimizer:
    """
    Grid-search optimiser over a strategy's param_ranges.

    Parameters
    ----------
    strategy_cls : type[StrategyBase]
        Strategy class (not instance).
    df : pd.DataFrame
        OHLCV data for the full optimisation window.
    initial_capital : float
    commission : float
    slippage_pct : float
    param_overrides : dict or None
        If provided, only the keys in param_overrides are swept; other
        param_ranges are fixed at the strategy's default_params values.
    workers : int
        Number of parallel processes.  0 = auto (cpu_count / 2).
    """

    def __init__(
        self,
        strategy_cls: Type[StrategyBase],
        df: pd.DataFrame,
        initial_capital: float = 10_000.0,
        commission: float = 0.0004,
        slippage_pct: float = 0.0001,
        param_overrides: Optional[Dict[str, List[Any]]] = None,
        workers: int = 0,
    ) -> None:
        self.strategy_cls    = strategy_cls
        self.df              = df
        self.initial_capital = initial_capital
        self.commission      = commission
        self.slippage_pct    = slippage_pct
        self.workers         = workers or max(1, (mp.cpu_count() or 2) // 2)
        self.results: List[OptRow] = []

        # Determine parameter grid
        inst = strategy_cls()
        ranges = dict(inst.param_ranges)
        if param_overrides:
            ranges.update(param_overrides)
        self._param_ranges = ranges

    # ------------------------------------------------------------------ #

    def run(
        self,
        sort_by: str = "sharpe_ratio",
        top_n: Optional[int] = None,
    ) -> List[OptRow]:
        """
        Execute the full grid search.

        Parameters
        ----------
        sort_by : str
            Metric key to rank results by (higher = better assumed).
        top_n : int or None
            If set, only the top-N results are kept.

        Returns
        -------
        List of (params, metrics) tuples, sorted best-first.
        """
        combos = list(_product_dicts(self._param_ranges))
        n_combos = len(combos)
        logger.info("Optimizer: %d parameter combinations × %d workers", n_combos, self.workers)

        engine_kwargs = {
            "initial_capital": self.initial_capital,
            "commission":      self.commission,
            "slippage_pct":    self.slippage_pct,
        }

        results: List[OptRow] = []

        if self.workers == 1:
            # ── Single-threaded: pass df directly, no serialisation ──── #
            iterator = (_tqdm(combos, total=n_combos) if HAS_TQDM else combos)
            done = 0
            for params in iterator:
                row = _run_single_direct(self.strategy_cls, self.df, params, engine_kwargs)
                if row:
                    results.append(row)
                if not HAS_TQDM:
                    done += 1
                    if done % max(1, n_combos // 20) == 0:
                        print(f"  Optimizer progress: {done}/{n_combos} ({done/n_combos*100:.0f}%)")
        else:
            # ── Multi-process: pickle the df once ─────────────────────── #
            import pickle
            df_pickle = pickle.dumps(self.df)
            task_args = [
                (self.strategy_cls, df_pickle, params, engine_kwargs)
                for params in combos
            ]
            ctx = mp.get_context("spawn")
            with ctx.Pool(processes=self.workers) as pool:
                if HAS_TQDM:
                    it = _tqdm(pool.imap_unordered(_run_single, task_args), total=n_combos)
                    for row in it:
                        if row:
                            results.append(row)
                else:
                    done = 0
                    for row in pool.imap_unordered(_run_single, task_args):
                        if row:
                            results.append(row)
                        done += 1
                        if done % max(1, n_combos // 20) == 0:
                            print(f"  Optimizer progress: {done}/{n_combos} ({done/n_combos*100:.0f}%)")

        # Sort
        def sort_key(row: OptRow) -> float:
            val = row[1].get(sort_by, float("-inf"))
            if val == float("inf"):
                return 1e18
            if val != val:  # NaN
                return float("-inf")
            return float(val)

        results.sort(key=sort_key, reverse=True)

        if top_n:
            results = results[:top_n]

        self.results = results
        logger.info("Optimizer done: %d valid results (sorted by %s)", len(results), sort_by)
        return results

    # ------------------------------------------------------------------ #

    def save_csv(self, path: str) -> Path:
        """Save optimisation results to a CSV file."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)

        if not self.results:
            logger.warning("No results to save.")
            return out

        # Flatten params + metrics into one row dict
        rows = []
        for params, metrics in self.results:
            row: Dict[str, Any] = {}
            for k, v in params.items():
                row[f"param_{k}"] = v
            for k, v in metrics.items():
                if not isinstance(v, dict):
                    row[f"metric_{k}"] = v
            rows.append(row)

        fieldnames = list(rows[0].keys())
        with open(out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        logger.info("Saved optimizer results → %s (%d rows)", out, len(rows))
        return out

    # ------------------------------------------------------------------ #

    def best_params(self, sort_by: str = "sharpe_ratio") -> Dict[str, Any]:
        """Return the parameter dict of the best result."""
        if not self.results:
            raise RuntimeError("No results.  Call run() first.")
        return dict(self.results[0][0])


# ──────────────────────────────────────────────────────────────────────────────
# Utility
# ──────────────────────────────────────────────────────────────────────────────

def _product_dicts(ranges: Dict[str, List[Any]]):
    """
    Yield every combination of values across all param_ranges keys.

    Example: {"a": [1,2], "b": ["x","y"]} yields
      {"a":1,"b":"x"}, {"a":1,"b":"y"}, {"a":2,"b":"x"}, {"a":2,"b":"y"}
    """
    if not ranges:
        yield {}
        return
    keys = list(ranges.keys())
    for combo in itertools.product(*[ranges[k] for k in keys]):
        yield dict(zip(keys, combo))
