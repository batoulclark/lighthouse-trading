"""
strategies/ — Built-in strategy implementations.

Available strategies
--------------------
GaussianChannelStrategy   — Gaussian Channel trend-following (our v7)
MACrossStrategy           — Simple moving-average crossover (example)
"""

from backtester.strategies.gaussian_channel import GaussianChannelStrategy
from backtester.strategies.example_strategy import MACrossStrategy

__all__ = ["GaussianChannelStrategy", "MACrossStrategy"]
