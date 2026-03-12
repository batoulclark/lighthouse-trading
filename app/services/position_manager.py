"""
Lighthouse Trading - Position Manager
Tracks open positions across all bots. Thread-safe with file locking.

Position lifecycle
------------------
Signal (buy/sell) → OrderExecutor → PositionManager.open_position()
Signal (close)    → OrderExecutor → PositionManager.close_position()
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from app.exchanges.base import BaseExchange
    from app.models.bot import Bot

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "data/positions.json"


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class Position:
    bot_id: str
    symbol: str
    side: str           # "long" | "short"
    size: float         # base-asset units
    entry_price: float
    current_price: float
    leverage: int
    unrealized_pnl: float
    opened_at: str      # ISO 8601 UTC

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**d)

    def notional_value(self) -> float:
        """Current position value in quote-asset units."""
        return self.current_price * self.size


@dataclass
class ClosedPosition:
    bot_id: str
    symbol: str
    side: str
    size: float
    entry_price: float
    exit_price: float
    leverage: int
    realized_pnl: float
    opened_at: str
    closed_at: str
    hold_duration: float  # seconds

    def to_dict(self) -> dict:
        return asdict(self)


# ── Manager ───────────────────────────────────────────────────────────────────

class PositionManager:
    """
    Thread-safe position tracker backed by a JSON file.

    Uses threading.RLock for in-process thread safety and fcntl.flock
    for cross-process file locking (Linux/macOS).
    """

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load(self) -> dict:
        """Read positions from disk. Returns {bot_id: position_dict}."""
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    content = fh.read()
                    return json.loads(content) if content.strip() else {}
                except json.JSONDecodeError:
                    return {}
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            return {}

    def _save(self, data: dict) -> None:
        """Atomically write positions to disk."""
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(data, fh, indent=2)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        os.replace(tmp, self.path)

    @staticmethod
    def _calc_unrealized_pnl(pos: Position) -> float:
        if pos.side == "long":
            return round((pos.current_price - pos.entry_price) * pos.size * pos.leverage, 6)
        return round((pos.entry_price - pos.current_price) * pos.size * pos.leverage, 6)

    # ── Public API ────────────────────────────────────────────────────────────

    def open_position(
        self,
        bot_id: str,
        symbol: str,
        side: str,
        size: float,
        entry_price: float,
        leverage: int,
    ) -> Position:
        """Record a newly opened position."""
        pos = Position(
            bot_id=bot_id,
            symbol=symbol,
            side=side,
            size=size,
            entry_price=entry_price,
            current_price=entry_price,
            leverage=leverage,
            unrealized_pnl=0.0,
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            data = self._load()
            data[bot_id] = pos.to_dict()
            self._save(data)

        logger.info(
            "Opened position: bot=%s %s %s size=%.6f entry=%.4f",
            bot_id, side, symbol, size, entry_price,
        )
        return pos

    def close_position(
        self,
        bot_id: str,
        symbol: str,
        exit_price: float,
    ) -> Optional[ClosedPosition]:
        """Close a position and return realized P&L. Returns None if not found."""
        with self._lock:
            data = self._load()
            pos_dict = data.pop(bot_id, None)
            if pos_dict is None:
                logger.warning("close_position: no open position for bot_id=%s", bot_id)
                return None
            self._save(data)

        pos = Position.from_dict(pos_dict)
        closed_at = datetime.now(timezone.utc).isoformat()

        if pos.side == "long":
            pnl = (exit_price - pos.entry_price) * pos.size * pos.leverage
        else:
            pnl = (pos.entry_price - exit_price) * pos.size * pos.leverage

        opened_dt = datetime.fromisoformat(pos.opened_at)
        closed_dt = datetime.fromisoformat(closed_at)
        duration = (closed_dt - opened_dt).total_seconds()

        closed = ClosedPosition(
            bot_id=pos.bot_id,
            symbol=pos.symbol,
            side=pos.side,
            size=pos.size,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            leverage=pos.leverage,
            realized_pnl=round(pnl, 6),
            opened_at=pos.opened_at,
            closed_at=closed_at,
            hold_duration=duration,
        )
        logger.info(
            "Closed position: bot=%s %s pnl=%.4f hold=%.0fs",
            bot_id, symbol, pnl, duration,
        )
        return closed

    def get_position(self, bot_id: str) -> Optional[Position]:
        """Return the open position for a bot, or None."""
        with self._lock:
            data = self._load()
        pos_dict = data.get(bot_id)
        return Position.from_dict(pos_dict) if pos_dict else None

    def get_all_positions(self) -> List[Position]:
        """Return all currently open positions."""
        with self._lock:
            data = self._load()
        return [Position.from_dict(v) for v in data.values()]

    def get_total_exposure(self) -> float:
        """Sum of all position notional values (current_price × size)."""
        return sum(p.notional_value() for p in self.get_all_positions())

    def update_price(self, bot_id: str, current_price: float) -> Optional[Position]:
        """Update current price and recalculate unrealized P&L."""
        with self._lock:
            data = self._load()
            if bot_id not in data:
                return None
            pos = Position.from_dict(data[bot_id])
            pos.current_price = current_price
            pos.unrealized_pnl = self._calc_unrealized_pnl(pos)
            data[bot_id] = pos.to_dict()
            self._save(data)
        return pos

    async def sync_from_exchange(self, exchange: "BaseExchange", bot: "Bot") -> Optional[Position]:
        """
        Fetch the real position from the exchange and upsert it locally.
        Returns the synced Position, or None if the exchange reports no position.
        """
        try:
            raw = await exchange.get_position(bot.pair)
        except Exception as exc:
            logger.error("sync_from_exchange failed for bot=%s: %s", bot.id, exc)
            return None

        if raw is None or raw.side == "none" or raw.size == 0:
            # No position on exchange — remove local record if any
            with self._lock:
                data = self._load()
                if bot.id in data:
                    data.pop(bot.id)
                    self._save(data)
            return None

        side = "long" if raw.size > 0 else "short"
        pos = self.open_position(
            bot_id=bot.id,
            symbol=bot.pair,
            side=side,
            size=abs(raw.size),
            entry_price=raw.entry_price,
            leverage=raw.leverage,
        )
        return pos
