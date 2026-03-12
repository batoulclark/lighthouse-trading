"""
strategy_base.py — Abstract strategy interface for the backtesting engine.

Every strategy must subclass StrategyBase and implement:
  - init(params)    — accept parameter dict, validate, store as self.params
  - on_candle(candle, history) -> Signal  — produce a trading signal
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import pandas as pd


class Action(str, Enum):
    """Trading action returned by a strategy."""
    LONG  = "LONG"
    SHORT = "SHORT"
    CLOSE = "CLOSE"
    HOLD  = "HOLD"


@dataclass
class Signal:
    """
    Signal produced by a strategy on each candle.

    Attributes
    ----------
    action : Action
        What to do (LONG / SHORT / CLOSE / HOLD).
    size : float
        Position size as a fraction of capital (0.0–1.0) or fixed USD amount,
        depending on the engine's ``size_mode`` setting.  Default = 1.0 (full
        capital).
    sl : float or None
        Stop-loss price.  None means no SL.
    tp : float or None
        Take-profit price.  None means no TP.
    comment : str
        Human-readable reason for the signal (used in trade log).
    """
    action:  Action
    size:    float         = 1.0
    sl:      Optional[float] = None
    tp:      Optional[float] = None
    comment: str           = ""


# Singleton for "do nothing" — avoids allocating a new object every bar
HOLD_SIGNAL = Signal(action=Action.HOLD)


class StrategyBase(abc.ABC):
    """
    Abstract base class for all backtesting strategies.

    Subclasses must define class-level attributes:
      name          : str                  — unique identifier
      description   : str                  — one-liner
      default_params: dict                 — default parameter values
      param_ranges  : dict[str, list]      — values to sweep during optimisation

    And implement:
      init(params)
      on_candle(candle, history) -> Signal
    """

    # ------------------------------------------------------------------ #
    # Class-level metadata — override in subclasses                       #
    # ------------------------------------------------------------------ #
    name:           str            = "base"
    description:    str            = "Abstract strategy"
    default_params: Dict[str, Any] = {}
    param_ranges:   Dict[str, List[Any]] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def __init__(self) -> None:
        self.params: Dict[str, Any] = dict(self.default_params)

    def init(self, params: Dict[str, Any]) -> None:
        """
        Merge *params* into defaults and store on self.params.
        Called by the engine before starting the simulation.
        """
        merged = dict(self.default_params)
        merged.update(params)
        self.params = merged
        self._validate_params()

    def _validate_params(self) -> None:
        """Override to raise ValueError for illegal parameter combinations."""

    # ------------------------------------------------------------------ #
    # Core interface                                                       #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def on_candle(self, candle: pd.Series, history: pd.DataFrame) -> Signal:
        """
        Called once per candle with the **closed** candle data.

        Parameters
        ----------
        candle : pd.Series
            Current candle with at least: open, high, low, close, volume.
            May also contain extra columns added by data_fetcher (e.g. funding_rate).
        history : pd.DataFrame
            All candles up to and including *candle* (current row is the last).

        Returns
        -------
        Signal
            Trading instruction for this bar.
        """

    # ------------------------------------------------------------------ #
    # Helpers available to strategies                                     #
    # ------------------------------------------------------------------ #

    def get_param(self, key: str, default: Any = None) -> Any:
        """Convenience accessor for self.params."""
        return self.params.get(key, default)
