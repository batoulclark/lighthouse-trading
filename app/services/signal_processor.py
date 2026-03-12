"""
Lighthouse Trading - Signal Processor
Validates and routes incoming TradingView signals.

Responsibilities
----------------
1. Validate schema version == "2"
2. Authenticate bot_id against the BotStore
3. Verify ticker matches the bot's configured pair
4. Deduplicate: reject the same timestamp within 60 seconds
5. Rate-limit: max 1 signal per 5 seconds per bot
6. IP allowlist check (delegated to caller via header)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from app.models.bot import Bot, BotStore
from app.models.signal import Signal

logger = logging.getLogger(__name__)

# Seconds within which a duplicate timestamp is rejected
_DEDUP_WINDOW_SECS = 60
# Minimum seconds between signals for the same bot
_RATE_LIMIT_SECS = 5


class SignalValidationError(Exception):
    """Raised when a signal fails validation."""

    def __init__(self, reason: str, status_code: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class SignalProcessor:
    """
    Stateful signal validator and router.
    Maintains per-bot deduplication and rate-limit windows.
    """

    def __init__(self, bot_store: BotStore) -> None:
        self.bot_store = bot_store
        # bot_id → set of timestamps seen within dedup window
        self._seen_timestamps: Dict[str, Dict[str, float]] = defaultdict(dict)
        # bot_id → epoch time of last accepted signal
        self._last_signal_time: Dict[str, float] = {}

    # ── Public API ───────────────────────────────────────────────────────────

    def process(self, payload: dict, client_ip: str, allowed_ips: list) -> Tuple[Signal, Bot]:
        """
        Parse, validate and route a raw webhook payload.

        Returns
        -------
        (signal, bot)  on success

        Raises
        ------
        SignalValidationError  on any validation failure
        """
        # 1. Validate schema
        schema = str(payload.get("schema", ""))
        if schema != "2":
            raise SignalValidationError(
                f"Unsupported schema version '{schema}'. Expected '2'.", 400
            )

        # 2. IP allowlist
        if allowed_ips and client_ip not in allowed_ips:
            logger.warning("Rejected signal from unauthorised IP: %s", client_ip)
            raise SignalValidationError(
                f"IP {client_ip} not in allowlist.", 403
            )

        # 3. Parse signal
        try:
            signal = Signal.from_dict(payload)
        except (KeyError, ValueError, TypeError) as exc:
            raise SignalValidationError(f"Invalid signal payload: {exc}", 400)

        # 4. Lookup bot by webhook_secret
        bot = self.bot_store.get_by_secret(signal.bot_id)
        if bot is None:
            logger.warning("Unknown bot_id in signal: %s", signal.bot_id)
            raise SignalValidationError("Unknown bot_id.", 401)

        if not bot.enabled:
            raise SignalValidationError(f"Bot '{bot.name}' is disabled.", 403)

        # 5. Verify ticker matches bot pair
        if signal.ticker != bot.pair:
            raise SignalValidationError(
                f"Ticker '{signal.ticker}' does not match bot pair '{bot.pair}'.", 400
            )

        # 6. Validate action
        if signal.action not in ("buy", "sell"):
            raise SignalValidationError(
                f"Invalid action '{signal.action}'. Must be 'buy' or 'sell'.", 400
            )

        # 7. Validate position_size
        if signal.position_size not in ("1", "0", "-1"):
            raise SignalValidationError(
                f"Invalid position_size '{signal.position_size}'. Must be 1, 0, or -1.", 400
            )

        now = datetime.now(timezone.utc).timestamp()

        # 8. Rate limit
        last = self._last_signal_time.get(bot.id, 0.0)
        elapsed = now - last
        if elapsed < _RATE_LIMIT_SECS:
            raise SignalValidationError(
                f"Rate limit: wait {_RATE_LIMIT_SECS - elapsed:.1f}s before next signal.",
                429,
            )

        # 9. Deduplication
        self._evict_old(bot.id, now)
        if signal.timestamp in self._seen_timestamps[bot.id]:
            raise SignalValidationError(
                f"Duplicate signal: timestamp '{signal.timestamp}' already processed.", 409
            )

        # ── Accept ───────────────────────────────────────────────────────────
        self._seen_timestamps[bot.id][signal.timestamp] = now
        self._last_signal_time[bot.id] = now

        logger.info(
            "Signal accepted: bot=%s ticker=%s action=%s position=%s",
            bot.name,
            signal.ticker,
            signal.action,
            signal.position_size,
        )
        return signal, bot

    # ── Private helpers ──────────────────────────────────────────────────────

    def _evict_old(self, bot_id: str, now: float) -> None:
        """Remove timestamps older than the dedup window."""
        cutoff = now - _DEDUP_WINDOW_SECS
        self._seen_timestamps[bot_id] = {
            ts: seen_at
            for ts, seen_at in self._seen_timestamps[bot_id].items()
            if seen_at >= cutoff
        }
