"""
Lighthouse Trading - Telegram Notifications
Sends alerts to a Telegram chat using the Bot API (HTTP, no extra deps).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# Maximum length Telegram accepts in a single message
_MAX_LEN = 4096


class TelegramNotifier:
    """
    Async-friendly Telegram notifier.
    Uses requests in a thread pool so we don't block the event loop.
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        if not self._enabled:
            logger.warning(
                "Telegram notifier disabled — TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID not configured."
            )

    # ── Public async interface ───────────────────────────────────────────────

    async def send(self, text: str) -> bool:
        """
        Send `text` to the configured chat.
        Returns True on success, False on failure (never raises).
        """
        if not self._enabled:
            logger.debug("Telegram disabled; would have sent: %s", text[:120])
            return False

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_sync, text)

    async def send_trade_alert(
        self,
        action: str,
        symbol: str,
        exchange: str,
        fill_price: Optional[float],
        size: float,
        pnl: Optional[float] = None,
        bot_name: str = "",
    ) -> bool:
        emoji = "🟢" if action.lower() == "buy" else "🔴"
        lines = [
            f"{emoji} *{action.upper()}* `{symbol}` on *{exchange}*",
            f"Bot: {bot_name}" if bot_name else "",
            f"Size: `{size:.6f}`",
            f"Fill: `${fill_price:,.4f}`" if fill_price else "Fill: market",
            f"PnL: `${pnl:+,.2f}`" if pnl is not None else "",
        ]
        return await self.send("\n".join(l for l in lines if l))

    async def send_error(self, context: str, error: str) -> bool:
        return await self.send(f"❌ *ERROR* in `{context}`:\n```\n{error[:500]}\n```")

    async def send_kill_switch_alert(self, reason: str) -> bool:
        return await self.send(
            f"🚨 *KILL SWITCH ACTIVATED*\nReason: {reason}\n"
            f"All trading is halted. Remove the KILL\\_SWITCH file to resume."
        )

    async def send_esl_warning(
        self, exchange: str, loss_pct: float, equity: float, upnl: float
    ) -> bool:
        return await self.send(
            f"⚠️ *ESL WARNING* on *{exchange}*\n"
            f"Drawdown: `{loss_pct:.1f}%`\n"
            f"Equity: `${equity:,.2f}` | UPnL: `${upnl:,.2f}`"
        )

    async def send_startup(self, host: str, port: int) -> bool:
        return await self.send(
            f"🔦 *Lighthouse Trading* started\n"
            f"Listening on `{host}:{port}`"
        )

    async def send_shutdown(self) -> bool:
        return await self.send("🔦 *Lighthouse Trading* shutting down")

    # ── Sync worker (runs in thread pool) ────────────────────────────────────

    def _send_sync(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        # Truncate if needed
        if len(text) > _MAX_LEN:
            text = text[: _MAX_LEN - 3] + "..."
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            logger.warning(
                "Telegram send failed: %d %s", resp.status_code, resp.text[:200]
            )
            # Retry without Markdown if parse error
            if resp.status_code == 400 and "can't parse" in resp.text:
                payload["parse_mode"] = None
                payload.pop("parse_mode")
                resp2 = requests.post(url, json=payload, timeout=10)
                return resp2.status_code == 200
        except requests.RequestException as exc:
            logger.error("Telegram request exception: %s", exc)
        return False
