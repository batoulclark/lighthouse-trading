"""
Lighthouse Trading - Telegram Command Interface
Jean controls bots from his phone via Telegram long polling.

Security: only messages from TELEGRAM_CHAT_ID are processed.
Transport: long-polling getUpdates (NOT webhooks — avoids port conflict).
Threading: runs in a background daemon thread.

Available commands
------------------
/status       — all bots, positions, total P&L
/bot <name>   — specific bot details
/kill         — activate kill switch (requires CONFIRM reply)
/unkill       — deactivate kill switch
/trades [n]   — last n trades (default 5)
/pnl          — today's P&L + total P&L
/help         — list commands
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

import requests

if TYPE_CHECKING:
    from app.models.bot import BotStore
    from app.models.trade import TradeLog
    from app.safety.kill_switch import KillSwitch
    from app.services.position_manager import PositionManager

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"
_POLL_TIMEOUT = 30   # seconds — Telegram long-poll timeout
_CONFIRM_TOKEN = "CONFIRM"


class TelegramCommandHandler:
    """
    Polls Telegram for updates and dispatches commands.
    Runs in a background daemon thread.
    """

    def __init__(
        self,
        bot_token: str,
        allowed_chat_id: str,
        kill_switch: "KillSwitch",
        bot_store: "BotStore",
        trade_log: "TradeLog",
        position_manager: "PositionManager",
    ) -> None:
        self._token = bot_token
        self._chat_id = str(allowed_chat_id)
        self._kill_switch = kill_switch
        self._bot_store = bot_store
        self._trade_log = trade_log
        self._positions = position_manager
        self._enabled = bool(bot_token and allowed_chat_id)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._offset: int = 0
        # Pending confirmations: chat_id → pending command name
        self._pending_confirm: Dict[str, str] = {}

        if not self._enabled:
            logger.warning(
                "TelegramCommandHandler disabled — TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID not configured."
            )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background polling thread."""
        # Disabled: Foufi token conflicts with OpenClaw gateway polling.
        # Commands will work once Lighthouse gets a dedicated bot token
        # that no other service polls on.
        logger.info("TelegramCommandHandler disabled (token conflict avoidance)")
        return
        if not self._enabled or self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="telegram-cmd-poll",
            daemon=True,
        )
        self._thread.start()
        logger.info("TelegramCommandHandler started (polling)")

    def stop(self) -> None:
        """Signal the polling thread to stop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=_POLL_TIMEOUT + 5)
        logger.info("TelegramCommandHandler stopped")

    # ── Polling loop ──────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Main loop: long-poll getUpdates, dispatch each message."""
        while self._running:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    self._handle_update(update)
            except requests.RequestException as exc:
                logger.warning("Telegram poll error: %s — retrying in 5s", exc)
                time.sleep(5)
            except Exception as exc:
                logger.error("Unexpected error in poll loop: %s", exc, exc_info=True)
                time.sleep(5)

    def _get_updates(self) -> List[Dict[str, Any]]:
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        resp = requests.get(
            url,
            params={"offset": self._offset, "timeout": _POLL_TIMEOUT},
            timeout=_POLL_TIMEOUT + 10,
        )
        if resp.status_code != 200:
            logger.warning("getUpdates returned %d", resp.status_code)
            return []
        data = resp.json()
        return data.get("result", [])

    # ── Update dispatcher ─────────────────────────────────────────────────────

    def _handle_update(self, update: Dict[str, Any]) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()

        # ── Security gate: only respond to the configured chat_id ──────────
        if chat_id != self._chat_id:
            logger.warning("Ignoring message from unauthorized chat_id=%s", chat_id)
            return

        if not text:
            return

        # ── Confirmation flow ───────────────────────────────────────────────
        pending = self._pending_confirm.get(chat_id)
        if pending and text == _CONFIRM_TOKEN:
            self._pending_confirm.pop(chat_id, None)
            self._execute_confirmed(chat_id, pending)
            return
        elif pending:
            self._pending_confirm.pop(chat_id, None)
            self._reply(chat_id, "Confirmation cancelled.")
            return

        # ── Route command ───────────────────────────────────────────────────
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]  # strip bot username suffix

        handlers: Dict[str, Callable] = {
            "/status":  self._cmd_status,
            "/bot":     self._cmd_bot,
            "/kill":    self._cmd_kill,
            "/unkill":  self._cmd_unkill,
            "/trades":  self._cmd_trades,
            "/pnl":     self._cmd_pnl,
            "/help":    self._cmd_help,
        }

        # Only respond to messages starting with /
        if not cmd.startswith("/"):
            logger.debug("Ignoring non-command message: %s", text[:50])
            return

        handler = handlers.get(cmd)
        if handler:
            try:
                handler(chat_id, parts[1:])
            except Exception as exc:
                logger.error("Command handler error for %s: %s", cmd, exc)
                self._reply(chat_id, f"❌ Error: {exc}")
        else:
            self._reply(chat_id, f"Unknown command: {cmd}\nType /help for available commands.")

    def _execute_confirmed(self, chat_id: str, command: str) -> None:
        """Execute a command that required confirmation."""
        if command == "kill":
            self._kill_switch.activate("Telegram /kill command")
            self._reply(chat_id, "🚨 Kill switch ACTIVATED. All trading halted.")
        else:
            self._reply(chat_id, f"Unknown confirmed command: {command}")

    # ── Command handlers ──────────────────────────────────────────────────────

    def _cmd_status(self, chat_id: str, args: List[str]) -> None:
        bots = self._bot_store.all()
        positions = {p.bot_id: p for p in self._positions.get_all_positions()}
        trades = self._trade_log.all()

        total_pnl = sum(t.get("pnl") or 0.0 for t in trades)
        total_exposure = self._positions.get_total_exposure()
        ks = "🔴 ACTIVE" if self._kill_switch.is_active() else "🟢 inactive"

        lines = [
            "📊 *Lighthouse Status*",
            f"Kill Switch: {ks}",
            f"Bots: {len(bots)} | Positions: {len(positions)}",
            f"Total Exposure: `${total_exposure:,.2f}`",
            f"Cumulative P&L: `${total_pnl:+,.2f}`",
            "",
        ]

        for bot in bots:
            pos = positions.get(bot.id)
            status_icon = "🟢" if bot.enabled else "⚫"
            line = f"{status_icon} *{bot.name}* ({bot.exchange}/{bot.pair})"
            if pos:
                line += f"\n  Position: {pos.side.upper()} {pos.size:.6f} @ ${pos.entry_price:,.4f}"
                line += f" | UPnL: `${pos.unrealized_pnl:+,.2f}`"
            lines.append(line)

        self._reply(chat_id, "\n".join(lines))

    def _cmd_bot(self, chat_id: str, args: List[str]) -> None:
        if not args:
            self._reply(chat_id, "Usage: /bot <name>")
            return

        name = " ".join(args).lower()
        bots = self._bot_store.all()
        bot = next((b for b in bots if b.name.lower() == name), None)

        if not bot:
            self._reply(chat_id, f"Bot '{name}' not found.")
            return

        pos = self._positions.get_position(bot.id)
        trades = [t for t in self._trade_log.all() if t.get("bot_id") == bot.id]
        total_pnl = sum(t.get("pnl") or 0.0 for t in trades)
        win_trades = [t for t in trades if (t.get("pnl") or 0) > 0]

        lines = [
            f"🤖 *{bot.name}*",
            f"Exchange: {bot.exchange} | Pair: {bot.pair}",
            f"Leverage: {bot.leverage}x | Enabled: {'Yes' if bot.enabled else 'No'}",
            f"Total Trades: {len(trades)} | Wins: {len(win_trades)}",
            f"Total P&L: `${total_pnl:+,.2f}`",
        ]
        if pos:
            lines += [
                "",
                f"📈 Open Position: {pos.side.upper()}",
                f"Size: {pos.size:.6f} | Entry: ${pos.entry_price:,.4f}",
                f"Current: ${pos.current_price:,.4f}",
                f"UPnL: `${pos.unrealized_pnl:+,.2f}`",
            ]
        else:
            lines.append("No open position.")

        self._reply(chat_id, "\n".join(lines))

    def _cmd_kill(self, chat_id: str, args: List[str]) -> None:
        if self._kill_switch.is_active():
            self._reply(chat_id, "Kill switch is already active.")
            return
        self._pending_confirm[chat_id] = "kill"
        self._reply(
            chat_id,
            "⚠️ *Kill switch will halt ALL trading.*\n"
            f"Type `{_CONFIRM_TOKEN}` to activate, or anything else to cancel.",
        )

    def _cmd_unkill(self, chat_id: str, args: List[str]) -> None:
        if not self._kill_switch.is_active():
            self._reply(chat_id, "Kill switch is not active.")
            return
        self._kill_switch.deactivate()
        self._reply(chat_id, "✅ Kill switch deactivated. Trading resumed.")

    def _cmd_trades(self, chat_id: str, args: List[str]) -> None:
        n = 5
        if args:
            try:
                n = int(args[0])
            except ValueError:
                pass
        n = max(1, min(n, 50))

        trades = self._trade_log.all()[-n:]
        if not trades:
            self._reply(chat_id, "No trades recorded yet.")
            return

        lines = [f"📋 *Last {len(trades)} Trades*"]
        for t in reversed(trades):
            pnl_str = f"`${t.get('pnl', 0) or 0:+.2f}`" if t.get("pnl") is not None else ""
            err = " ❌" if t.get("error") else ""
            lines.append(
                f"{t.get('action','?').upper()} {t.get('pair','?')} "
                f"@ `${t.get('fill_price') or 0:,.4f}` {pnl_str}{err}"
            )
        self._reply(chat_id, "\n".join(lines))

    def _cmd_pnl(self, chat_id: str, args: List[str]) -> None:
        from datetime import date, timezone as tz
        today = date.today().isoformat()

        trades = self._trade_log.all()
        today_trades = [
            t for t in trades
            if t.get("timestamp", "").startswith(today)
        ]

        today_pnl = sum(t.get("pnl") or 0.0 for t in today_trades)
        total_pnl = sum(t.get("pnl") or 0.0 for t in trades)

        unrealized = sum(
            p.unrealized_pnl for p in self._positions.get_all_positions()
        )

        lines = [
            "💰 *P&L Summary*",
            f"Today ({today}): `${today_pnl:+,.2f}` ({len(today_trades)} trades)",
            f"Total realized: `${total_pnl:+,.2f}`",
            f"Unrealized: `${unrealized:+,.2f}`",
            f"Net: `${total_pnl + unrealized:+,.2f}`",
        ]
        self._reply(chat_id, "\n".join(lines))

    def _cmd_help(self, chat_id: str, args: List[str]) -> None:
        self._reply(
            chat_id,
            "🔦 *Lighthouse Commands*\n"
            "/status — system overview\n"
            "/bot <name> — bot details\n"
            "/kill — activate kill switch\n"
            "/unkill — deactivate kill switch\n"
            "/trades [n] — last n trades (default 5)\n"
            "/pnl — P&L summary\n"
            "/help — this message",
        )

    # ── Telegram send ─────────────────────────────────────────────────────────

    def _reply(self, chat_id: str, text: str) -> None:
        """Send a message to chat_id (synchronous, called from worker thread)."""
        if len(text) > 4096:
            text = text[:4093] + "..."
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                logger.warning("Telegram reply failed: %d %s", resp.status_code, resp.text[:100])
                # Retry without Markdown
                if "can't parse" in resp.text:
                    payload.pop("parse_mode", None)
                    requests.post(url, json=payload, timeout=10)
        except requests.RequestException as exc:
            logger.error("Telegram reply error: %s", exc)
