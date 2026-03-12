"""
Lighthouse Trading - Signal model
Matches the Signum webhook schema v2 sent by TradingView.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass
class Signal:
    """
    Signum schema v2 payload.

    Fields
    ------
    bot_id          : webhook_secret of the target bot (used for routing)
    ticker          : symbol exactly as configured on TradingView, e.g. BTCUSDT
    action          : "buy" or "sell"
    order_size      : size string, e.g. "100%" or "0.5"
    position_size   : desired position direction after execution:
                        "1"  = long
                        "0"  = flat (close)
                       "-1"  = short
    timestamp       : ISO 8601 string — used for deduplication
    schema          : must equal "2"
    comment         : optional freeform string from the alert
    """

    bot_id: str
    ticker: str
    action: Literal["buy", "sell"]
    order_size: str
    position_size: str
    timestamp: str
    schema: str
    comment: Optional[str] = None
    price: Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Signal":
        price_raw = data.get("price")
        price_val = float(price_raw) if price_raw is not None else None
        return cls(
            bot_id=str(data["bot_id"]),
            ticker=str(data["ticker"]).upper().replace("-", "").replace("/", ""),
            action=str(data["action"]).lower(),  # type: ignore[arg-type]
            order_size=str(data.get("order_size", "100%")),
            position_size=str(data.get("position_size", "1")),
            timestamp=str(data["timestamp"]),
            schema=str(data.get("schema", "2")),
            comment=data.get("comment"),
            price=price_val,
        )

    def to_dict(self) -> dict:
        return {
            "bot_id": self.bot_id,
            "ticker": self.ticker,
            "action": self.action,
            "order_size": self.order_size,
            "position_size": self.position_size,
            "timestamp": self.timestamp,
            "schema": self.schema,
            "comment": self.comment,
        }

    # ── Validation helpers ───────────────────────────────────────────────────

    def is_close(self) -> bool:
        """Return True when this signal closes the entire position."""
        return self.position_size == "0"

    def is_long(self) -> bool:
        return self.position_size == "1"

    def is_short(self) -> bool:
        return self.position_size == "-1"

    def size_fraction(self) -> float:
        """
        Convert order_size to a 0..1 fraction.
        '100%' → 1.0, '50%' → 0.5, '0.5' → 0.5, '1' → 1.0
        """
        s = self.order_size.strip()
        if s.endswith("%"):
            return float(s[:-1]) / 100.0
        val = float(s)
        # If given as a multiplier > 1, interpret as percentage points
        return val / 100.0 if val > 1 else val
