#!/usr/bin/env python3
"""
auto_optimize.py — Weekly automated strategy optimization.

Runs backtests across configured symbols + timeframes to find optimal strategy
parameters, saves a JSON report, and sends a Telegram summary.

Parameters are NOT auto-deployed — Jean must review and apply manually.

Usage
-----
    python scripts/auto_optimize.py
    python scripts/auto_optimize.py --symbols BTCUSDT,ETHUSDT --timeframes 1d,4h
    python scripts/auto_optimize.py --strategy gaussian --top-n 5 --workers 2
    python scripts/auto_optimize.py --symbols BTCUSDT --timeframes 4h --start 2024-01-01
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Project root on path ──────────────────────────────────────────────────────

_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from backtester.data_fetcher import fetch_candles
from backtester.optimizer import Optimizer
from backtester.strategies.gaussian_channel import GaussianChannelStrategy
from backtester.strategies.example_strategy import MACrossStrategy
from app.notifications.telegram import TelegramNotifier
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("auto_optimize")

# ── Strategy registry ─────────────────────────────────────────────────────────

STRATEGIES: Dict[str, Any] = {
    "gaussian": GaussianChannelStrategy,
    "ma_cross": MACrossStrategy,
}

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_SYMBOLS: List[str]    = ["BTCUSDT", "ETHUSDT"]
DEFAULT_TIMEFRAMES: List[str] = ["1d", "4h"]
DEFAULT_START_DATE: str       = "2023-01-01"
DEFAULT_END_DATE: str         = datetime.now(timezone.utc).strftime("%Y-%m-%d")

REPORTS_DIR = _PROJ_ROOT / "data" / "reports"


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Auto-optimizer for Lighthouse Trading strategies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols (default: BTCUSDT,ETHUSDT)",
    )
    p.add_argument(
        "--timeframes",
        default="",
        help="Comma-separated timeframes (default: 1d,4h)",
    )
    p.add_argument(
        "--strategy",
        default="gaussian",
        choices=list(STRATEGIES),
        help="Strategy to optimize (default: gaussian)",
    )
    p.add_argument(
        "--start",
        default=DEFAULT_START_DATE,
        help="Start date YYYY-MM-DD (default: 2023-01-01)",
    )
    p.add_argument(
        "--end",
        default=DEFAULT_END_DATE,
        help="End date YYYY-MM-DD (default: today)",
    )
    p.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Keep top-N param combinations per run (default: 5)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel optimizer workers (default: 1)",
    )
    p.add_argument(
        "--sort-by",
        default="sharpe_ratio",
        help="Metric to rank results by (default: sharpe_ratio)",
    )
    p.add_argument(
        "--capital",
        type=float,
        default=10_000.0,
        help="Initial capital for backtest (default: 10000)",
    )
    return p.parse_args()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compare_params(
    current: Dict[str, Any],
    new_best: Dict[str, Any],
) -> List[str]:
    """Return human-readable descriptions of changed parameters."""
    changes: List[str] = []
    for key in sorted(set(current) | set(new_best)):
        old_val = current.get(key, "<missing>")
        new_val = new_best.get(key, "<missing>")
        if old_val != new_val:
            changes.append(f"  {key}: {old_val!r} → {new_val!r}")
    return changes


def _clean_metrics(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Strip non-serialisable / NaN values from a metrics dict."""
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            continue  # skip nested dicts
        if isinstance(v, float) and v != v:  # NaN check
            out[k] = None
        elif isinstance(v, (int, float, str, bool, type(None))):
            out[k] = v
    return out


# ── Core optimisation ─────────────────────────────────────────────────────────

def run_optimization(
    strategy_name: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    top_n: int,
    workers: int,
    sort_by: str,
    capital: float,
) -> Dict[str, Any]:
    """Run the optimizer for one (strategy, symbol, timeframe) combination."""
    strategy_cls = STRATEGIES[strategy_name]

    logger.info("Fetching candles: %s %s  %s → %s", symbol, timeframe, start, end)
    try:
        df = fetch_candles(symbol, timeframe, start, end, source="binance")
    except Exception as exc:
        logger.error("Failed to fetch candles for %s/%s: %s", symbol, timeframe, exc)
        return {"symbol": symbol, "timeframe": timeframe, "strategy": strategy_name, "error": str(exc)}

    if df is None or df.empty:
        logger.warning("No candle data for %s/%s", symbol, timeframe)
        return {"symbol": symbol, "timeframe": timeframe, "strategy": strategy_name, "error": "no data"}

    logger.info(
        "Running optimizer: %s %s/%s (%d bars, workers=%d)",
        strategy_name, symbol, timeframe, len(df), workers,
    )

    opt = Optimizer(
        strategy_cls=strategy_cls,
        df=df,
        initial_capital=capital,
        workers=workers,
    )

    try:
        results = opt.run(sort_by=sort_by, top_n=top_n)
    except Exception as exc:
        logger.error("Optimizer failed for %s/%s: %s", symbol, timeframe, exc)
        return {"symbol": symbol, "timeframe": timeframe, "strategy": strategy_name, "error": str(exc)}

    if not results:
        return {
            "symbol": symbol, "timeframe": timeframe, "strategy": strategy_name,
            "error": "no valid results",
        }

    best_params, best_metrics_raw = results[0]
    best_params = dict(best_params)
    best_metrics = _clean_metrics(dict(best_metrics_raw))

    current_params: Dict[str, Any] = dict(strategy_cls().default_params)
    changes = _compare_params(current_params, best_params)

    top_results = [
        {"params": dict(p), "metrics": _clean_metrics(dict(m))}
        for p, m in results
    ]

    if changes:
        logger.info(
            "%s/%s: %d param change(s) suggested:\n%s",
            symbol, timeframe, len(changes), "\n".join(changes),
        )
    else:
        logger.info("%s/%s: current params are already optimal", symbol, timeframe)

    return {
        "symbol":         symbol,
        "timeframe":      timeframe,
        "strategy":       strategy_name,
        "bars":           len(df),
        "best_params":    best_params,
        "best_metrics":   best_metrics,
        "current_params": current_params,
        "param_changes":  changes,
        "params_changed": len(changes) > 0,
        "top_results":    top_results,
    }


# ── Telegram summary ──────────────────────────────────────────────────────────

async def _send_telegram_summary(
    telegram: TelegramNotifier,
    results: List[Dict[str, Any]],
    report_path: Path,
) -> None:
    """Send a concise optimization summary via Telegram."""
    success = [r for r in results if "error" not in r]
    failed  = [r for r in results if "error" in r]

    lines: List[str] = [
        "📊 *Auto-Optimizer Complete*\n",
        f"✅ Completed: {len(success)}  |  ❌ Failed: {len(failed)}",
        f"Report: `{report_path.name}`\n",
    ]

    for r in success:
        metrics = r.get("best_metrics", {})
        changes = r.get("param_changes", [])
        sharpe  = metrics.get("sharpe_ratio")
        ret_pct = metrics.get("total_return_pct")

        header = f"*{r['symbol']} {r['timeframe']}* ({r['strategy']})"
        if sharpe is not None:
            header += f"  Sharpe: `{sharpe:.2f}`"
        if ret_pct is not None:
            header += f"  Return: `{ret_pct:.1f}%`"
        lines.append(header)

        if changes:
            lines.append(f"  ⚠️ {len(changes)} param change(s) suggested")
        else:
            lines.append("  ✓ Current params are optimal")

    if failed:
        failed_ids = ", ".join(f"{r.get('symbol','?')}/{r.get('timeframe','?')}" for r in failed)
        lines.append(f"\n❌ Failed: {failed_ids}")

    lines.append("\n_Parameters NOT auto-applied — review required._")

    await telegram.send("\n".join(lines))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    args = _parse_args()

    symbols    = [s.strip() for s in args.symbols.split(",")    if s.strip()] or DEFAULT_SYMBOLS
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()] or DEFAULT_TIMEFRAMES

    logger.info(
        "Auto-optimizer starting  strategy=%s  symbols=%s  timeframes=%s  %s → %s",
        args.strategy, symbols, timeframes, args.start, args.end,
    )

    results: List[Dict[str, Any]] = []

    for symbol in symbols:
        for timeframe in timeframes:
            result = run_optimization(
                strategy_name=args.strategy,
                symbol=symbol,
                timeframe=timeframe,
                start=args.start,
                end=args.end,
                top_n=args.top_n,
                workers=args.workers,
                sort_by=args.sort_by,
                capital=args.capital,
            )
            results.append(result)

    # ── Save JSON report ──────────────────────────────────────────────────────
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"optimization_{date_str}.json"

    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy":     args.strategy,
        "symbols":      symbols,
        "timeframes":   timeframes,
        "date_range":   {"start": args.start, "end": args.end},
        "results":      results,
    }

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    logger.info("Report saved → %s", report_path)

    # ── Telegram summary ──────────────────────────────────────────────────────
    telegram = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )
    asyncio.run(_send_telegram_summary(telegram, results, report_path))

    return 0


if __name__ == "__main__":
    sys.exit(main())
