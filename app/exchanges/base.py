"""
Lighthouse Trading - Abstract Exchange Interface
All exchange connectors must implement this protocol.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Position:
    symbol: str
    side: str             # "long" | "short" | "none"
    size: float           # in base-asset units
    entry_price: float
    unrealized_pnl: float
    leverage: int


@dataclass
class Balance:
    asset: str
    total: float
    available: float


@dataclass
class OrderResult:
    exchange: str
    symbol: str
    side: str             # "buy" | "sell"
    size: float
    fill_price: Optional[float]
    order_id: str
    fees: Optional[float]
    raw: Dict[str, Any]


class BaseExchange(ABC):
    """Abstract base class for exchange connectors."""

    name: str = "base"

    # ── Core trading operations ──────────────────────────────────────────────

    @abstractmethod
    async def market_buy(self, symbol: str, size: float) -> OrderResult:
        """Open a long position / buy `size` units of `symbol`."""

    @abstractmethod
    async def market_sell(self, symbol: str, size: float) -> OrderResult:
        """Open a short position / sell `size` units of `symbol`."""

    @abstractmethod
    async def close_position(self, symbol: str) -> OrderResult:
        """Close the entire position for `symbol` at market price."""

    # ── Account queries ──────────────────────────────────────────────────────

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Return current position for `symbol`, or None if flat."""

    @abstractmethod
    async def get_balance(self, asset: str = "USDT") -> Balance:
        """Return account balance for `asset`."""

    @abstractmethod
    async def get_equity(self) -> float:
        """Return total account equity in USD."""

    # ── Configuration ────────────────────────────────────────────────────────

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for `symbol`."""

    # ── Utilities ────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_price(self, symbol: str) -> float:
        """Return the latest mid price for `symbol`."""

    def symbol_for_pair(self, pair: str) -> str:
        """Normalise a pair string to the format expected by this exchange."""
        return pair
