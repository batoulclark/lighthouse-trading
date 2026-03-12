"""
Lighthouse Trading - Trade log model
Every executed order is appended to the trade log.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class Trade:
    timestamp: str          # ISO 8601 UTC
    bot_id: str             # UUID of the bot
    bot_name: str
    exchange: str
    pair: str
    action: str             # "buy" | "sell"
    order_size: str
    position_size: str      # "1" | "0" | "-1"
    fill_price: Optional[float]
    quantity: Optional[float]
    fees: Optional[float]
    pnl: Optional[float]
    execution_result: Dict[str, Any]  # raw exchange response
    signal_timestamp: str   # timestamp from the incoming signal
    error: Optional[str] = None

    @classmethod
    def from_execution(
        cls,
        bot_id: str,
        bot_name: str,
        exchange: str,
        pair: str,
        action: str,
        order_size: str,
        position_size: str,
        signal_timestamp: str,
        execution_result: Dict[str, Any],
        fill_price: Optional[float] = None,
        quantity: Optional[float] = None,
        fees: Optional[float] = None,
        pnl: Optional[float] = None,
        error: Optional[str] = None,
    ) -> "Trade":
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            bot_id=bot_id,
            bot_name=bot_name,
            exchange=exchange,
            pair=pair,
            action=action,
            order_size=order_size,
            position_size=position_size,
            fill_price=fill_price,
            quantity=quantity,
            fees=fees,
            pnl=pnl,
            execution_result=execution_result,
            signal_timestamp=signal_timestamp,
            error=error,
        )

    def to_dict(self) -> dict:
        return asdict(self)


class TradeLog:
    """Append-only JSON trade log."""

    def __init__(self, path: str) -> None:
        self.path = path

    def append(self, trade: Trade) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        trades = self._load()
        trades.append(trade.to_dict())
        with open(self.path, "w") as fh:
            json.dump(trades, fh, indent=2)

    def all(self) -> List[dict]:
        return self._load()

    def _load(self) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r") as fh:
            try:
                return json.load(fh)
            except json.JSONDecodeError:
                return []
