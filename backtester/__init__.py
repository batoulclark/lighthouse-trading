"""
Lighthouse Trading — Backtesting Engine
========================================
Strategy tester, optimizer, walk-forward validator, and report generator.

Quick start:
    from backtester.engine import BacktestEngine
    from backtester.data_fetcher import fetch_candles
    from backtester.strategies.gaussian_channel import GaussianChannelStrategy

    df = fetch_candles("BTCUSDT", "4h", "2023-01-01", "2026-01-01")
    strategy = GaussianChannelStrategy()
    engine = BacktestEngine(strategy, df, initial_capital=10_000)
    result = engine.run()
    print(result.metrics)
"""

from backtester.engine import BacktestEngine
from backtester.models import BacktestResult, Trade
from backtester.strategy_base import StrategyBase, Signal, Action
from backtester.metrics import calculate_metrics

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "Trade",
    "StrategyBase",
    "Signal",
    "Action",
    "calculate_metrics",
]
