"""
Lighthouse Trading - Bot model
A bot maps a TradingView webhook to a specific exchange + trading pair.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class Bot:
    id: str
    name: str
    exchange: str          # "hyperliquid" | "binance"
    pair: str              # e.g. "BTCUSDT"
    base_asset: str        # e.g. "BTC"
    quote_asset: str       # e.g. "USDT"
    leverage: int
    enabled: bool
    webhook_secret: str    # bot_id field value sent by TradingView
    created_at: str        # ISO 8601

    @classmethod
    def create(
        cls,
        name: str,
        exchange: str,
        pair: str,
        leverage: int = 1,
        webhook_secret: Optional[str] = None,
    ) -> "Bot":
        """Factory that generates a new Bot with a fresh id."""
        # Naively split pair into base/quote; covers BTCUSDT, BTC-USDT, BTC/USDT
        cleaned = pair.replace("-", "").replace("/", "").upper()
        # Guess base/quote by common quote lengths (USDT=4, USDC=4, BTC=3, ETH=3)
        for q in ("USDT", "USDC", "BUSD", "BTC", "ETH", "BNB"):
            if cleaned.endswith(q):
                base = cleaned[: -len(q)]
                quote = q
                break
        else:
            base = cleaned
            quote = ""

        return cls(
            id=str(uuid.uuid4()),
            name=name,
            exchange=exchange.lower(),
            pair=cleaned,
            base_asset=base,
            quote_asset=quote,
            leverage=leverage,
            enabled=True,
            webhook_secret=webhook_secret or str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Bot":
        return cls(**data)


class BotStore:
    """File-backed CRUD store for Bots."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._bots: dict[str, Bot] = {}
        self._load()

    # ── Private helpers ──────────────────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r") as fh:
            raw = json.load(fh)
        self._bots = {b["id"]: Bot.from_dict(b) for b in raw}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump([b.to_dict() for b in self._bots.values()], fh, indent=2)

    # ── Public API ───────────────────────────────────────────────────────────

    def all(self) -> List[Bot]:
        return list(self._bots.values())

    def get(self, bot_id: str) -> Optional[Bot]:
        return self._bots.get(bot_id)

    def get_by_secret(self, secret: str) -> Optional[Bot]:
        for bot in self._bots.values():
            if bot.webhook_secret == secret:
                return bot
        return None

    def add(self, bot: Bot) -> None:
        self._bots[bot.id] = bot
        self._save()

    def update(self, bot: Bot) -> None:
        self._bots[bot.id] = bot
        self._save()

    def delete(self, bot_id: str) -> bool:
        if bot_id not in self._bots:
            return False
        del self._bots[bot_id]
        self._save()
        return True

    def reload(self) -> None:
        self._load()
