"""
Lighthouse Trading - Paper Trading Exchange Connector
Simulates order execution in memory; persists trade log to data/paper_trades.json.
No real API keys required — suitable for full-pipeline dry runs.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.exchanges.base import Balance, BaseExchange, OrderResult, Position

logger = logging.getLogger(__name__)

_PAPER_TRADES_FILE = os.getenv("PAPER_TRADES_FILE", "data/paper_trades.json")
_STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE", "10000"))


class PaperExchange(BaseExchange):
    """
    Paper trading exchange — all orders are simulated.

    Positions are kept in memory and every trade event is appended to
    ``data/paper_trades.json`` so the full history survives restarts.
    """

    name = "paper"

    def __init__(
        self,
        starting_balance: float = _STARTING_BALANCE,
        trades_file: str = _PAPER_TRADES_FILE,
    ) -> None:
        self._trades_file = trades_file
        self._positions: Dict[str, Dict[str, Any]] = {}   # symbol → position dict
        self._leverage: Dict[str, int] = {}               # symbol → leverage
        self._last_price: Dict[str, float] = {}           # symbol → last known price
        self._balance = starting_balance
        self._starting_balance = starting_balance

        # Ensure data directory exists
        os.makedirs(os.path.dirname(self._trades_file) or ".", exist_ok=True)

        logger.info(
            "PaperExchange initialised — starting balance=%.2f USDT, file=%s",
            starting_balance,
            trades_file,
        )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load_trades(self) -> list:
        if os.path.exists(self._trades_file):
            try:
                with open(self._trades_file) as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _append_trade(self, record: Dict[str, Any]) -> None:
        trades = self._load_trades()
        trades.append(record)
        try:
            with open(self._trades_file, "w") as f:
                json.dump(trades, f, indent=2)
        except Exception as exc:
            logger.error("Failed to persist paper trade: %s", exc)

    def _make_order_id(self) -> str:
        return f"paper-{uuid.uuid4().hex[:12]}"

    # ── Core trading operations ──────────────────────────────────────────────

    async def market_buy(
        self, symbol: str, size: float, price: Optional[float] = None
    ) -> OrderResult:
        fill_price = price or self._last_price.get(symbol, 0.0)
        if fill_price <= 0:
            raise ValueError(
                f"PaperExchange.market_buy: no price available for {symbol}. "
                "Pass price= or call get_price() first."
            )

        cost = fill_price * size
        self._balance -= cost

        self._positions[symbol] = {
            "symbol": symbol,
            "side": "long",
            "size": size,
            "entry_price": fill_price,
            "leverage": self._leverage.get(symbol, 1),
        }
        self._last_price[symbol] = fill_price

        order_id = self._make_order_id()
        record = {
            "timestamp": self._now(),
            "order_id": order_id,
            "exchange": self.name,
            "symbol": symbol,
            "side": "buy",
            "size": size,
            "fill_price": fill_price,
            "cost": cost,
            "balance_after": self._balance,
        }
        self._append_trade(record)
        logger.info("PAPER BUY  %s size=%.6f @ %.4f  balance=%.2f", symbol, size, fill_price, self._balance)

        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side="buy",
            size=size,
            fill_price=fill_price,
            order_id=order_id,
            fees=0.0,
            raw=record,
        )

    async def market_sell(
        self, symbol: str, size: float, price: Optional[float] = None
    ) -> OrderResult:
        fill_price = price or self._last_price.get(symbol, 0.0)
        if fill_price <= 0:
            raise ValueError(
                f"PaperExchange.market_sell: no price available for {symbol}. "
                "Pass price= or call get_price() first."
            )

        proceeds = fill_price * size
        self._balance += proceeds

        self._positions[symbol] = {
            "symbol": symbol,
            "side": "short",
            "size": size,
            "entry_price": fill_price,
            "leverage": self._leverage.get(symbol, 1),
        }
        self._last_price[symbol] = fill_price

        order_id = self._make_order_id()
        record = {
            "timestamp": self._now(),
            "order_id": order_id,
            "exchange": self.name,
            "symbol": symbol,
            "side": "sell",
            "size": size,
            "fill_price": fill_price,
            "proceeds": proceeds,
            "balance_after": self._balance,
        }
        self._append_trade(record)
        logger.info("PAPER SELL %s size=%.6f @ %.4f  balance=%.2f", symbol, size, fill_price, self._balance)

        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side="sell",
            size=size,
            fill_price=fill_price,
            order_id=order_id,
            fees=0.0,
            raw=record,
        )

    async def close_position(
        self, symbol: str, price: Optional[float] = None
    ) -> OrderResult:
        pos = self._positions.get(symbol)
        if pos is None or pos["side"] == "none":
            return OrderResult(
                exchange=self.name,
                symbol=symbol,
                side="close",
                size=0.0,
                fill_price=None,
                order_id="no_position",
                fees=None,
                raw={"info": "no open position"},
            )

        exit_price = price or self._last_price.get(symbol, pos["entry_price"])
        size = pos["size"]
        entry_price = pos["entry_price"]
        side = pos["side"]

        if side == "long":
            pnl = (exit_price - entry_price) * size
            self._balance += entry_price * size + pnl  # return notional + profit
        else:  # short
            pnl = (entry_price - exit_price) * size
            self._balance -= entry_price * size - pnl  # return notional + profit (short)

        self._last_price[symbol] = exit_price
        self._positions.pop(symbol, None)

        order_id = self._make_order_id()
        record = {
            "timestamp": self._now(),
            "order_id": order_id,
            "exchange": self.name,
            "symbol": symbol,
            "side": "close",
            "size": size,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "balance_after": self._balance,
        }
        self._append_trade(record)
        logger.info(
            "PAPER CLOSE %s %s size=%.6f entry=%.4f exit=%.4f pnl=%.4f  balance=%.2f",
            side, symbol, size, entry_price, exit_price, pnl, self._balance,
        )

        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side="close",
            size=size,
            fill_price=exit_price,
            order_id=order_id,
            fees=0.0,
            raw=record,
        )

    # ── Account queries ──────────────────────────────────────────────────────

    async def get_position(self, symbol: str) -> Optional[Position]:
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        current_price = self._last_price.get(symbol, pos["entry_price"])
        size = pos["size"]
        entry_price = pos["entry_price"]
        side = pos["side"]

        if side == "long":
            upnl = (current_price - entry_price) * size
        else:
            upnl = (entry_price - current_price) * size

        return Position(
            symbol=symbol,
            side=side,
            size=size,
            entry_price=entry_price,
            unrealized_pnl=upnl,
            leverage=pos.get("leverage", 1),
        )

    async def get_balance(self, asset: str = "USDT") -> Balance:
        return Balance(asset=asset, total=self._balance, available=self._balance)

    async def get_equity(self) -> float:
        return self._balance

    # ── Configuration ────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self._leverage[symbol] = leverage
        logger.info("PAPER set_leverage %s x%d (stored, no real effect)", symbol, leverage)

    # ── Utilities ────────────────────────────────────────────────────────────

    async def get_price(self, symbol: str, price: Optional[float] = None) -> float:
        """Return price. If `price` is provided, store it as last known and return it."""
        if price is not None:
            self._last_price[symbol] = price
            return price
        return self._last_price.get(symbol, 0.0)

    def symbol_for_pair(self, pair: str) -> str:
        return pair.upper()
