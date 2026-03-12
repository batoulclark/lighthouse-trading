#!/usr/bin/env python3
"""
run_backtest.py — CLI entry point for the Lighthouse backtesting engine.

Usage examples
--------------
    # Basic backtest
    python scripts/run_backtest.py \\
        --strategy gaussian \\
        --symbol BTCUSDT \\
        --timeframe 4h \\
        --start 2023-01-01 \\
        --end 2026-03-01

    # With optimisation + HTML report
    python scripts/run_backtest.py \\
        --strategy gaussian \\
        --symbol BTCUSDT \\
        --timeframe 4h \\
        --start 2023-01-01 \\
        --end 2026-03-01 \\
        --optimize \\
        --report

    # Walk-forward validation
    python scripts/run_backtest.py \\
        --strategy gaussian \\
        --symbol BTCUSDT \\
        --timeframe 1h \\
        --start 2022-01-01 \\
        --end 2026-03-01 \\
        --walk-forward \\
        --wf-windows 5

    # Generate PineScript
    python scripts/run_backtest.py \\
        --strategy gaussian \\
        --symbol BTCUSDT \\
        --timeframe 4h \\
        --pine \\
        --bot-id my_gc_bot

    # Use Hyperliquid as data source
    python scripts/run_backtest.py \\
        --strategy gaussian \\
        --symbol BTC \\
        --timeframe 4h \\
        --start 2024-01-01 \\
        --end 2026-01-01 \\
        --source hyperliquid
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Make sure the project root is on sys.path
_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from backtester.data_fetcher import fetch_candles
from backtester.engine import BacktestEngine
from backtester.metrics import metrics_to_string
from backtester.strategies.gaussian_channel import GaussianChannelStrategy
from backtester.strategies.example_strategy import MACrossStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_backtest")


# ──────────────────────────────────────────────────────────────────────────────
# Strategy registry
# ──────────────────────────────────────────────────────────────────────────────

STRATEGIES = {
    "gaussian": GaussianChannelStrategy,
    "ma_cross": MACrossStrategy,
}


# ──────────────────────────────────────────────────────────────────────────────
# Argument parser
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Lighthouse Trading — Strategy Backtester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Data
    p.add_argument("--strategy",   default="gaussian", choices=list(STRATEGIES.keys()),
                   help="Strategy to backtest (default: gaussian)")
    p.add_argument("--symbol",     default="BTCUSDT",
                   help="Trading symbol (default: BTCUSDT)")
    p.add_argument("--timeframe",  default="4h",
                   help="Candle timeframe (default: 4h)")
    p.add_argument("--start",      default="2023-01-01",
                   help="Start date YYYY-MM-DD (default: 2023-01-01)")
    p.add_argument("--end",        default="2026-03-01",
                   help="End date YYYY-MM-DD (default: 2026-03-01)")
    p.add_argument("--source",     default="binance", choices=["binance", "hyperliquid"],
                   help="Data source (default: binance)")
    p.add_argument("--no-cache",   action="store_true",
                   help="Disable local data cache")

    # Engine settings
    p.add_argument("--capital",    type=float, default=10_000.0,
                   help="Initial capital in USD (default: 10000)")
    p.add_argument("--commission", type=float, default=0.0004,
                   help="Commission per side as fraction (default: 0.0004 = 0.04%%)")
    p.add_argument("--slippage",   type=float, default=0.0001,
                   help="Slippage per side as fraction (default: 0.0001)")

    # Strategy params override (JSON string)
    p.add_argument("--params",     type=str, default=None,
                   help='Strategy params as JSON, e.g. \'{"period":144,"multiplier":2.5}\'')

    # Optimisation
    p.add_argument("--optimize",   action="store_true",
                   help="Run parameter grid-search optimisation")
    p.add_argument("--opt-sort",   default="sharpe_ratio",
                   help="Metric to sort optimiser results by (default: sharpe_ratio)")
    p.add_argument("--opt-top",    type=int, default=10,
                   help="Show top N optimiser results (default: 10)")
    p.add_argument("--workers",    type=int, default=1,
                   help="Parallel workers for optimiser (default: 1)")

    # Walk-forward
    p.add_argument("--walk-forward", action="store_true",
                   help="Run walk-forward validation")
    p.add_argument("--wf-windows",  type=int, default=5,
                   help="Number of WF windows (default: 5)")
    p.add_argument("--wf-train-pct", type=float, default=0.7,
                   help="Training fraction per WF window (default: 0.7)")

    # Report
    p.add_argument("--report",     action="store_true",
                   help="Generate HTML report")
    p.add_argument("--report-path", type=str, default=None,
                   help="Override HTML report output path")
    p.add_argument("--report-title", type=str, default=None,
                   help="HTML report title override")

    # PineScript
    p.add_argument("--pine",       action="store_true",
                   help="Generate PineScript v5 output file")
    p.add_argument("--pine-path",  type=str, default=None,
                   help="Override .pine output path")
    p.add_argument("--bot-id",     type=str, default="your_bot_id",
                   help="Bot ID embedded in PineScript alert messages")

    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    return p


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── 1. Fetch data ──────────────────────────────────────────────── #
    logger.info("Fetching %s %s %s → %s from %s",
                args.symbol, args.timeframe, args.start, args.end, args.source)
    df = fetch_candles(
        symbol     = args.symbol,
        timeframe  = args.timeframe,
        start_date = args.start,
        end_date   = args.end,
        source     = args.source,
        use_cache  = not args.no_cache,
    )

    if df.empty:
        logger.error("No data returned.  Check symbol/date range and internet connection.")
        sys.exit(1)

    logger.info("Loaded %d candles (%s → %s)", len(df),
                df.index[0].strftime("%Y-%m-%d"), df.index[-1].strftime("%Y-%m-%d"))

    # ── 2. Build strategy ─────────────────────────────────────────── #
    strategy_cls = STRATEGIES[args.strategy]
    strategy     = strategy_cls()

    strategy_params = {}
    if args.params:
        try:
            strategy_params = json.loads(args.params)
        except json.JSONDecodeError as exc:
            logger.error("Invalid --params JSON: %s", exc)
            sys.exit(1)

    # ── 3. Baseline backtest ───────────────────────────────────────── #
    title = args.report_title or f"{strategy.name.upper()} — {args.symbol} {args.timeframe}"

    logger.info("Running backtest: %s on %s %s", strategy.name, args.symbol, args.timeframe)
    engine = BacktestEngine(
        strategy        = strategy,
        df              = df,
        initial_capital = args.capital,
        commission      = args.commission,
        slippage_pct    = args.slippage,
        strategy_params = strategy_params,
    )
    result = engine.run()
    print("\n" + metrics_to_string(result.metrics))

    opt_results = None

    # ── 4. Optimisation ────────────────────────────────────────────── #
    if args.optimize:
        from backtester.optimizer import Optimizer
        print(f"\nRunning grid-search optimisation (sort by {args.opt_sort})…")
        opt = Optimizer(
            strategy_cls    = strategy_cls,
            df              = df,
            initial_capital = args.capital,
            commission      = args.commission,
            slippage_pct    = args.slippage,
            workers         = args.workers,
        )
        opt_results = opt.run(sort_by=args.opt_sort, top_n=args.opt_top)

        # Save CSV
        csv_path = _PROJ_ROOT / "data" / "reports" / f"opt_{args.strategy}_{args.symbol}_{args.timeframe}.csv"
        opt.save_csv(str(csv_path))
        print(f"Optimisation CSV saved → {csv_path}")

        print(f"\nTop {len(opt_results)} parameter combinations:")
        print(f"  {'Rank':>4}  {'Return%':>8}  {'Sharpe':>7}  {'PF':>6}  {'MDD%':>7}  Params")
        print("  " + "─" * 65)
        for rank, (params, metrics) in enumerate(opt_results, 1):
            p_str = " ".join(f"{k}={v}" for k, v in params.items())
            ret   = metrics.get("net_profit_pct", 0)
            sh    = metrics.get("sharpe_ratio", 0)
            pf    = metrics.get("profit_factor", 0)
            mdd   = metrics.get("max_drawdown_pct", 0)
            print(f"  {rank:>4}  {float(ret):>+8.2f}%  {float(sh):>7.3f}  {float(pf):>6.3f}"
                  f"  {float(mdd):>7.2f}%  {p_str}")

        # Re-run with best params and update result
        best_params = opt.best_params(sort_by=args.opt_sort)
        logger.info("Re-running with best params: %s", best_params)
        best_strategy = strategy_cls()
        best_engine   = BacktestEngine(
            strategy        = best_strategy,
            df              = df,
            initial_capital = args.capital,
            commission      = args.commission,
            slippage_pct    = args.slippage,
            strategy_params = best_params,
        )
        result = best_engine.run()
        print("\nBest-params backtest:")
        print(metrics_to_string(result.metrics))

    # ── 5. Walk-forward ────────────────────────────────────────────── #
    if args.walk_forward:
        from backtester.walk_forward import WalkForwardAnalysis
        print(f"\nRunning walk-forward analysis ({args.wf_windows} windows)…")
        wfa = WalkForwardAnalysis(
            strategy_cls    = strategy_cls,
            df              = df,
            train_pct       = args.wf_train_pct,
            windows         = args.wf_windows,
            initial_capital = args.capital,
            commission      = args.commission,
            slippage_pct    = args.slippage,
            workers         = args.workers,
        )
        wf_report = wfa.run()
        print(wf_report.summary())

    # ── 6. HTML report ─────────────────────────────────────────────── #
    if args.report:
        from backtester.report import generate_report
        report_path = args.report_path or str(
            _PROJ_ROOT / "data" / "reports" / f"{args.strategy}_{args.symbol}_{args.timeframe}.html"
        )
        out = generate_report(result, output_path=report_path, title=title, opt_results=opt_results)
        print(f"\nHTML report saved → {out}")

    # ── 7. PineScript ──────────────────────────────────────────────── #
    if args.pine:
        from backtester.pine_generator import generate_pine
        pine_path = args.pine_path or str(
            _PROJ_ROOT / "data" / "reports" / f"{args.strategy}_{args.symbol}_{args.timeframe}.pine"
        )
        code = generate_pine(
            strategy        = result.params and _make_strategy_with_params(strategy_cls, result.params) or strategy,
            symbol          = args.symbol,
            timeframe       = args.timeframe,
            initial_capital = args.capital,
            commission_pct  = args.commission * 100,
            bot_id          = args.bot_id,
            output_path     = pine_path,
        )
        print(f"\nPineScript saved → {pine_path}")
        print(f"  (first 5 lines): {chr(10).join(code.splitlines()[:5])}")


def _make_strategy_with_params(cls, params):
    s = cls()
    s.init(params)
    return s


if __name__ == "__main__":
    main()
