#!/usr/bin/env python3
"""
run_walkforward.py — Walk-forward validation CLI for Lighthouse Trading.

Usage
-----
    python scripts/run_walkforward.py \\
        --symbol BTCUSDT \\
        --timeframe 1d \\
        --train-days 365 \\
        --test-days 90 \\
        --strategy gaussian

Output
------
  • Per-fold summary table printed to stdout
  • Final statistics: avg test return, worst/best fold, consistency score
  • JSON report saved to data/reports/walkforward_{symbol}_{timeframe}_{date}.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from app.services.data_pipeline import DataPipeline
from backtester.walk_forward import WalkForwardAnalysis, WalkForwardReport
from backtester.strategies.gaussian_channel import GaussianChannelStrategy
from backtester.strategies.example_strategy import MACrossStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_walkforward")

# ── Strategy registry ─────────────────────────────────────────────────────────

STRATEGIES = {
    "gaussian": GaussianChannelStrategy,
    "ma_cross": MACrossStrategy,
}

# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Lighthouse Trading — Walk-Forward Validation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--symbol",      default="BTCUSDT",
                   help="Trading symbol (default: BTCUSDT)")
    p.add_argument("--timeframe",   default="1d", choices=["1h", "4h", "1d"],
                   help="Candle timeframe (default: 1d)")
    p.add_argument("--train-days",  type=int, default=365,
                   help="Training window size in days (default: 365)")
    p.add_argument("--test-days",   type=int, default=90,
                   help="Test window size in days (default: 90)")
    p.add_argument("--windows",     type=int, default=None,
                   help="Number of WF windows (auto-calculated from train/test days if omitted)")
    p.add_argument("--strategy",    default="gaussian", choices=list(STRATEGIES.keys()),
                   help="Strategy to use (default: gaussian)")
    p.add_argument("--capital",     type=float, default=10_000.0,
                   help="Initial capital USD (default: 10000)")
    p.add_argument("--commission",  type=float, default=0.0004,
                   help="Commission per side (default: 0.0004)")
    p.add_argument("--slippage",    type=float, default=0.0001,
                   help="Slippage per side (default: 0.0001)")
    p.add_argument("--workers",     type=int, default=1,
                   help="Optimiser parallel workers (default: 1)")
    p.add_argument("--start",       default=None,
                   help="Override start date YYYY-MM-DD (default: auto from train+test days × windows)")
    p.add_argument("--end",         default=None,
                   help="Override end date YYYY-MM-DD (default: today)")
    p.add_argument("--no-cache",    action="store_true",
                   help="Ignore local Parquet cache and re-fetch from Binance")
    p.add_argument("--report-dir",  default=str(_PROJ_ROOT / "data" / "reports"),
                   help="Directory for JSON report output")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    strategy_cls = STRATEGIES[args.strategy]

    # ── 1. Determine date range ──────────────────────────────────────────── #
    end_date = args.end or date.today().isoformat()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Auto-calculate number of windows from train/test sizes if not supplied
    windows = args.windows
    if windows is None:
        # Heuristic: fit as many non-overlapping folds as data allows
        total_days = args.train_days + args.test_days
        windows = max(3, total_days // args.test_days - 1)
        logger.info("Auto-calculated windows=%d", windows)

    # Calculate required lookback: train_days + windows * test_days
    required_days = args.train_days + windows * args.test_days
    if args.start:
        start_date = args.start
    else:
        start_dt = end_dt - timedelta(days=required_days + 30)  # +30 buffer
        start_date = start_dt.strftime("%Y-%m-%d")

    logger.info(
        "Walk-forward: symbol=%s tf=%s strategy=%s windows=%d train=%dd test=%dd",
        args.symbol, args.timeframe, args.strategy, windows,
        args.train_days, args.test_days,
    )
    logger.info("Date range: %s → %s", start_date, end_date)

    # ── 2. Fetch data ────────────────────────────────────────────────────── #
    pipeline = DataPipeline()

    logger.info("Fetching/updating cache for %s %s…", args.symbol, args.timeframe)
    if args.no_cache:
        # Remove existing cache so fetch_candles re-downloads from Binance
        cache_path = os.path.join(pipeline.cache_dir, f"{args.symbol.upper()}_{args.timeframe}.parquet")
        if os.path.exists(cache_path):
            os.remove(cache_path)
            logger.info("Cleared cache: %s", cache_path)
    df = pipeline.fetch_candles(args.symbol, args.timeframe, start_date, end_date)

    if df is None or df.empty:
        logger.error("No data returned. Check symbol/date range and internet.")
        sys.exit(1)

    logger.info(
        "Loaded %d candles (%s → %s)",
        len(df), df.index[0].strftime("%Y-%m-%d"), df.index[-1].strftime("%Y-%m-%d"),
    )

    if len(df) < 50:
        logger.error("Not enough data for walk-forward analysis (need ≥50 candles).")
        sys.exit(1)

    # ── 3. Calculate train_pct from day counts ───────────────────────────── #
    # WalkForwardAnalysis uses train_pct per window; convert from day counts
    train_pct = args.train_days / (args.train_days + args.test_days)

    # ── 4. Run walk-forward analysis ─────────────────────────────────────── #
    print(f"\nRunning walk-forward analysis ({windows} windows)…")
    wfa = WalkForwardAnalysis(
        strategy_cls=strategy_cls,
        df=df,
        train_pct=train_pct,
        windows=windows,
        initial_capital=args.capital,
        commission=args.commission,
        slippage_pct=args.slippage,
        workers=args.workers,
    )
    report = wfa.run()

    # ── 5. Print per-fold summary table ─────────────────────────────────── #
    print(_format_fold_table(report, args))

    # ── 6. Print final statistics ────────────────────────────────────────── #
    stats = _compute_stats(report)
    print(_format_stats(stats))

    # ── 7. Save JSON report ──────────────────────────────────────────────── #
    report_path = _save_report(report, stats, args)
    print(f"\nReport saved → {report_path}")


# ── Formatting helpers ────────────────────────────────────────────────────────

def _format_fold_table(report: WalkForwardReport, args: argparse.Namespace) -> str:
    lines = [
        "",
        "═" * 80,
        f"  WALK-FORWARD RESULTS  {args.symbol} {args.timeframe} | strategy={args.strategy}",
        "═" * 80,
        f"  {'Fold':>4}  {'Train Start':>12}  {'Train End':>10}  "
        f"{'Test Start':>10}  {'Test End':>10}  "
        f"{'Train Ret%':>10}  {'Test Ret%':>9}  {'OOS Sharpe':>10}  Params",
        "  " + "─" * 76,
    ]
    for w in report.windows:
        train_ret = w.in_sample.get("net_profit_pct", 0)
        test_ret = w.out_of_sample.get("net_profit_pct", 0)
        oos_sharpe = w.out_of_sample.get("sharpe_ratio", 0)
        flag = " ⚠" if w.is_overfit else ""
        param_str = _short_params(w.best_params)
        lines.append(
            f"  {w.window_idx:>4}  "
            f"{str(w.train_start.date()):>12}  "
            f"{str(w.train_end.date()):>10}  "
            f"{str(w.test_start.date()):>10}  "
            f"{str(w.test_end.date()):>10}  "
            f"{float(train_ret):>+10.2f}%  "
            f"{float(test_ret):>+9.2f}%  "
            f"{float(oos_sharpe):>10.3f}"
            f"{flag}  {param_str}"
        )
    lines.append("═" * 80)
    return "\n".join(lines)


def _format_stats(stats: Dict[str, Any]) -> str:
    lines = [
        "",
        "  SUMMARY STATISTICS",
        "  " + "─" * 40,
        f"  Windows run:         {stats['n_windows']}",
        f"  Avg test return:     {stats['avg_test_return']:+.2f}%",
        f"  Best fold:           #{stats['best_fold_idx']}  ({stats['best_fold_return']:+.2f}%)",
        f"  Worst fold:          #{stats['worst_fold_idx']}  ({stats['worst_fold_return']:+.2f}%)",
        f"  Consistency score:   {stats['consistency_score']:.2f}  "
        f"({stats['positive_folds']}/{stats['n_windows']} positive folds)",
        f"  Overfitting flags:   {stats['overfit_flags']}/{stats['n_windows']}",
        f"  Efficiency ratio:    {stats['efficiency_ratio']:.4f}",
        "",
    ]
    if stats.get("combined_oos"):
        oos = stats["combined_oos"]
        lines += [
            "  Combined OOS Performance:",
            f"    Net return:  {float(oos.get('net_profit_pct', 0)):+.2f}%",
            f"    Sharpe:      {float(oos.get('sharpe_ratio', 0)):.3f}",
            f"    Max DD:      {float(oos.get('max_drawdown_pct', 0)):.2f}%",
            f"    Profit factor: {float(oos.get('profit_factor', 0)):.3f}",
        ]
    return "\n".join(lines)


def _short_params(params: Dict[str, Any]) -> str:
    """Compact key=value string for the most important params."""
    important = ["period", "multiplier", "poles", "sl_pct", "fast", "slow"]
    parts = [f"{k}={v}" for k, v in params.items() if k in important]
    if not parts:
        parts = [f"{k}={v}" for k, v in list(params.items())[:3]]
    return " ".join(parts)


def _compute_stats(report: WalkForwardReport) -> Dict[str, Any]:
    if not report.windows:
        return {
            "n_windows": 0, "avg_test_return": 0.0,
            "best_fold_idx": 0, "best_fold_return": 0.0,
            "worst_fold_idx": 0, "worst_fold_return": 0.0,
            "consistency_score": 0.0, "positive_folds": 0,
            "overfit_flags": 0, "efficiency_ratio": 0.0,
            "combined_oos": {},
        }

    returns = [
        float(w.out_of_sample.get("net_profit_pct", 0))
        for w in report.windows
    ]
    best_idx = int(max(range(len(returns)), key=lambda i: returns[i]))
    worst_idx = int(min(range(len(returns)), key=lambda i: returns[i]))
    positive = sum(1 for r in returns if r > 0)

    return {
        "n_windows": len(report.windows),
        "avg_test_return": round(sum(returns) / len(returns), 4),
        "best_fold_idx": report.windows[best_idx].window_idx,
        "best_fold_return": round(returns[best_idx], 4),
        "worst_fold_idx": report.windows[worst_idx].window_idx,
        "worst_fold_return": round(returns[worst_idx], 4),
        "consistency_score": round(positive / len(returns), 4),
        "positive_folds": positive,
        "overfit_flags": report.overfitting_flags,
        "efficiency_ratio": report.efficiency_ratio,
        "combined_oos": report.combined_oos,
    }


def _save_report(
    report: WalkForwardReport,
    stats: Dict[str, Any],
    args: argparse.Namespace,
) -> str:
    today = date.today().isoformat()
    filename = f"walkforward_{args.symbol}_{args.timeframe}_{today}.json"
    os.makedirs(args.report_dir, exist_ok=True)
    path = os.path.join(args.report_dir, filename)

    payload: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "strategy": args.strategy,
        "train_days": args.train_days,
        "test_days": args.test_days,
        "windows": [
            {
                "window_idx": w.window_idx,
                "train_start": str(w.train_start.date()),
                "train_end": str(w.train_end.date()),
                "test_start": str(w.test_start.date()),
                "test_end": str(w.test_end.date()),
                "best_params": w.best_params,
                "in_sample": {k: float(v) for k, v in w.in_sample.items()
                              if isinstance(v, (int, float))},
                "out_of_sample": {k: float(v) for k, v in w.out_of_sample.items()
                                  if isinstance(v, (int, float))},
                "is_overfit": w.is_overfit,
            }
            for w in report.windows
        ],
        "summary": stats,
        "combined_oos": {k: float(v) for k, v in report.combined_oos.items()
                         if isinstance(v, (int, float))},
        "efficiency_ratio": report.efficiency_ratio,
        "overfitting_flags": report.overfitting_flags,
    }

    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)

    return path


if __name__ == "__main__":
    main()
