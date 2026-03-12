"""
engine.py — Core event-driven backtesting engine.

Usage
-----
    engine = BacktestEngine(strategy, df, initial_capital=10_000, commission=0.0004)
    result = engine.run()
    print(result.metrics)
    result.equity_curve.plot()
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from backtester.strategy_base import Action, Signal, StrategyBase
from backtester.metrics import calculate_metrics
from backtester.models import Trade, BacktestResult


# ──────────────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Event-driven backtesting engine.

    Parameters
    ----------
    strategy : StrategyBase
        Instantiated (but not yet initialised) strategy.
    df : pd.DataFrame
        OHLCV data with a DatetimeIndex and columns: open, high, low, close, volume.
        Extra columns (funding_rate, etc.) are passed through to the strategy.
    initial_capital : float
        Starting equity in USD.
    commission : float
        Round-trip commission rate, e.g. 0.0004 = 0.04% per side (0.08% RT).
        Applied per leg (entry + exit each charged once).
    slippage_pct : float
        Additional slippage modelled as a fraction of price per leg.
    size_mode : str
        "fraction" — signal.size treated as fraction of current equity (default).
        "fixed_usd" — signal.size treated as fixed USD notional.
    funding_col : str or None
        Name of the funding rate column in df.  If present, funding is deducted
        from open positions every candle (rate expressed as fraction per candle).
    strategy_params : dict
        Parameters forwarded to strategy.init().
    warmup_bars : int
        Minimum history required before the strategy is asked for signals.
        Signals are HOLD during warmup.
    """

    def __init__(
        self,
        strategy: StrategyBase,
        df: pd.DataFrame,
        initial_capital: float = 10_000.0,
        commission: float = 0.0004,
        slippage_pct: float = 0.0001,
        size_mode: str = "fraction",
        funding_col: Optional[str] = None,
        strategy_params: Optional[Dict] = None,
        warmup_bars: int = 0,
    ) -> None:
        self.strategy = strategy
        self.df = df.copy()
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage_pct = slippage_pct
        self.size_mode = size_mode
        self.funding_col = funding_col
        self.warmup_bars = warmup_bars

        params = strategy_params or {}
        self.strategy.init(params)

        self._validate_df()

    # ------------------------------------------------------------------ #
    # Validation                                                           #
    # ------------------------------------------------------------------ #

    def _validate_df(self) -> None:
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(self.df.columns)
        if missing:
            raise ValueError(f"DataFrame missing required columns: {missing}")
        if not isinstance(self.df.index, pd.DatetimeIndex):
            raise TypeError("DataFrame must have a DatetimeIndex")

    # ------------------------------------------------------------------ #
    # Main simulation loop                                                 #
    # ------------------------------------------------------------------ #

    def run(self) -> BacktestResult:
        """
        Iterate bar-by-bar, collect signals, simulate fills, track P&L.

        Returns a BacktestResult with full trade log, equity curve and metrics.
        """
        df = self.df
        n = len(df)

        equity = self.initial_capital
        equity_curve: List[float] = []

        # Open position state
        in_position:   bool           = False
        direction:     str            = ""        # "LONG" | "SHORT"
        entry_price:   float          = 0.0
        entry_time:    pd.Timestamp   = pd.NaT
        position_usd:  float          = 0.0       # notional at entry
        sl_price:      Optional[float] = None
        tp_price:      Optional[float] = None
        entry_comment: str            = ""

        trades:     List[Trade] = []
        trade_id:   int         = 0

        for i, (timestamp, candle) in enumerate(df.iterrows()):
            history = df.iloc[: i + 1]

            # ── 1. Check SL / TP before asking strategy ──────────────── #
            exit_reason: Optional[str] = None
            exit_price_override: Optional[float] = None

            if in_position:
                hi = candle["high"]
                lo = candle["low"]

                if direction == "LONG":
                    if sl_price and lo <= sl_price:
                        exit_reason = "sl"
                        exit_price_override = sl_price
                    elif tp_price and hi >= tp_price:
                        exit_reason = "tp"
                        exit_price_override = tp_price
                else:  # SHORT
                    if sl_price and hi >= sl_price:
                        exit_reason = "sl"
                        exit_price_override = sl_price
                    elif tp_price and lo <= tp_price:
                        exit_reason = "tp"
                        exit_price_override = tp_price

            # ── 2. Get strategy signal (skip during warmup) ───────────── #
            if i < self.warmup_bars:
                signal = Signal(action=Action.HOLD)
            else:
                signal = self.strategy.on_candle(candle, history)

            # ── 3. Determine close action from either SL/TP or signal ─── #
            close_now = (
                exit_reason is not None
                or (in_position and signal.action == Action.CLOSE)
                or (in_position and direction == "LONG"  and signal.action == Action.SHORT)
                or (in_position and direction == "SHORT" and signal.action == Action.LONG)
            )

            # ── 4. Close current position ─────────────────────────────── #
            if in_position and close_now:
                raw_exit = float(exit_price_override or candle["close"])
                # Apply slippage (adverse)
                if direction == "LONG":
                    exit_price = raw_exit * (1 - self.slippage_pct)
                else:
                    exit_price = raw_exit * (1 + self.slippage_pct)

                comm_exit = position_usd * self.commission
                pnl_raw = self._calc_pnl(direction, entry_price, exit_price, position_usd)
                pnl_net = pnl_raw - comm_exit
                equity += pnl_net

                if entry_price != 0:
                    pnl_pct = pnl_net / (position_usd) * 100
                else:
                    pnl_pct = 0.0

                total_comm = position_usd * self.commission * 2  # entry + exit
                trades.append(Trade(
                    trade_id    = trade_id,
                    direction   = direction,
                    entry_time  = entry_time,
                    exit_time   = timestamp,
                    entry_price = entry_price,
                    exit_price  = exit_price,
                    size_usd    = position_usd,
                    pnl         = pnl_net,
                    pnl_pct     = pnl_pct,
                    commission  = total_comm,
                    exit_reason = exit_reason or "signal",
                    comment     = entry_comment,
                ))
                trade_id += 1
                in_position = False
                direction   = ""
                sl_price    = None
                tp_price    = None

            # ── 5. Open new position (if signal says so and no conflict) ── #
            open_long  = signal.action == Action.LONG  and not in_position
            open_short = signal.action == Action.SHORT and not in_position
            # Also handle reverse: closed above, now open opposite
            if not in_position and close_now and signal.action in (Action.LONG, Action.SHORT):
                open_long  = signal.action == Action.LONG
                open_short = signal.action == Action.SHORT

            if open_long or open_short:
                raw_entry = float(candle["close"])
                new_dir   = "LONG" if open_long else "SHORT"

                # Slippage
                if new_dir == "LONG":
                    entry_price = raw_entry * (1 + self.slippage_pct)
                else:
                    entry_price = raw_entry * (1 - self.slippage_pct)

                # Size
                if self.size_mode == "fraction":
                    size_frac   = min(max(float(signal.size), 0.01), 1.0)
                    position_usd = equity * size_frac
                else:
                    position_usd = float(signal.size)

                comm_entry = position_usd * self.commission
                equity -= comm_entry

                in_position   = True
                direction     = new_dir
                entry_time    = timestamp
                sl_price      = signal.sl
                tp_price      = signal.tp
                entry_comment = signal.comment

            # ── 6. Deduct funding rate (if column present) ────────────── #
            if in_position and self.funding_col and self.funding_col in candle.index:
                funding_rate = float(candle[self.funding_col])
                if direction == "LONG":
                    equity -= position_usd * abs(funding_rate)
                else:
                    equity += position_usd * abs(funding_rate)

            equity_curve.append(equity)

        # ── 7. Force-close any open position at last bar ─────────────── #
        if in_position:
            last_ts     = df.index[-1]
            last_candle = df.iloc[-1]
            raw_exit    = float(last_candle["close"])
            if direction == "LONG":
                exit_price = raw_exit * (1 - self.slippage_pct)
            else:
                exit_price = raw_exit * (1 + self.slippage_pct)

            comm_exit = position_usd * self.commission
            pnl_raw   = self._calc_pnl(direction, entry_price, exit_price, position_usd)
            pnl_net   = pnl_raw - comm_exit
            equity   += pnl_net
            pnl_pct   = pnl_net / position_usd * 100 if position_usd else 0.0
            total_comm = position_usd * self.commission * 2

            trades.append(Trade(
                trade_id    = trade_id,
                direction   = direction,
                entry_time  = entry_time,
                exit_time   = last_ts,
                entry_price = entry_price,
                exit_price  = exit_price,
                size_usd    = position_usd,
                pnl         = pnl_net,
                pnl_pct     = pnl_pct,
                commission  = total_comm,
                exit_reason = "end_of_data",
                comment     = entry_comment,
            ))
            equity_curve[-1] = equity

        # ── 8. Build outputs ─────────────────────────────────────────── #
        equity_series = pd.Series(equity_curve, index=df.index, name="equity")
        drawdown = self._calc_drawdown(equity_series)
        metrics  = calculate_metrics(trades, equity_series, self.initial_capital)

        return BacktestResult(
            trades       = trades,
            equity_curve = equity_series,
            drawdown     = drawdown,
            metrics      = metrics,
            params       = dict(self.strategy.params),
        )

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _calc_pnl(direction: str, entry: float, exit_p: float, notional: float) -> float:
        """Raw P&L before commission."""
        if entry == 0:
            return 0.0
        ret = (exit_p - entry) / entry
        if direction == "SHORT":
            ret = -ret
        return ret * notional

    @staticmethod
    def _calc_drawdown(equity: pd.Series) -> pd.Series:
        """Drawdown in USD from running peak."""
        peak = equity.cummax()
        return equity - peak
