"""
Lighthouse Trading - Emergency Stop-Loss (ESL)
Monitors unrealized PnL vs equity and escalates through three thresholds.

Thresholds (configurable via .env):
  ESL_WARN_PCT        = 15  → Telegram warning only
  ESL_CRITICAL_PCT    = 20  → Force-close ALL open positions
  ESL_CATASTROPHIC_PCT= 30  → Force-close ALL positions + activate kill switch
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from app.exchanges.base import BaseExchange, Position
from app.safety.kill_switch import KillSwitch

logger = logging.getLogger(__name__)

# Type alias for the Telegram send function to avoid circular imports
_TelegramSendFn = "Callable[[str], Awaitable[None]]"


class EmergencyStopLoss:
    """
    Background task that polls all active exchange connectors every
    `interval_seconds` and enforces drawdown limits.
    """

    def __init__(
        self,
        kill_switch: KillSwitch,
        warn_pct: float = 15.0,
        critical_pct: float = 20.0,
        catastrophic_pct: float = 30.0,
        interval_seconds: int = 60,
    ) -> None:
        self.kill_switch = kill_switch
        self.warn_pct = warn_pct
        self.critical_pct = critical_pct
        self.catastrophic_pct = catastrophic_pct
        self.interval = interval_seconds

        # Injected at startup
        self._exchanges: List[BaseExchange] = []
        self._send_alert: Optional[callable] = None  # async fn(str)
        self._watched_symbols: List[str] = []

        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ── Setup ────────────────────────────────────────────────────────────────

    def register_exchange(self, exchange: BaseExchange, symbols: List[str]) -> None:
        self._exchanges.append(exchange)
        self._watched_symbols.extend(symbols)

    def set_alert_fn(self, fn: callable) -> None:
        """Set the async function used to send Telegram alerts."""
        self._send_alert = fn

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="esl_monitor")
        logger.info(
            "ESL monitor started (warn=%.0f%%, critical=%.0f%%, catastrophic=%.0f%%)",
            self.warn_pct,
            self.critical_pct,
            self.catastrophic_pct,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Main loop ────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ESL monitor error: %s", exc)
            await asyncio.sleep(self.interval)

    async def _check_all(self) -> None:
        for exchange in self._exchanges:
            try:
                await self._check_exchange(exchange)
            except Exception as exc:
                logger.error("ESL check failed for %s: %s", exchange.name, exc)

    async def _check_exchange(self, exchange: BaseExchange) -> None:
        equity = await exchange.get_equity()
        if equity <= 0:
            logger.debug("ESL: equity=0 for %s, skipping", exchange.name)
            return

        # Gather all open positions
        open_positions: List[Position] = []
        for symbol in set(self._watched_symbols):
            pos = await exchange.get_position(symbol)
            if pos and pos.size > 0:
                open_positions.append(pos)

        if not open_positions:
            return

        total_upnl = sum(p.unrealized_pnl for p in open_positions)
        loss_pct = (-total_upnl / equity) * 100  # positive = loss

        logger.debug(
            "ESL %s: equity=%.2f upnl=%.2f loss_pct=%.2f%%",
            exchange.name,
            equity,
            total_upnl,
            loss_pct,
        )

        now = datetime.now(timezone.utc).isoformat()

        if loss_pct >= self.catastrophic_pct:
            msg = (
                f"🔴 CATASTROPHIC DRAWDOWN {loss_pct:.1f}% on {exchange.name}!\n"
                f"Equity: ${equity:,.2f} | UPnL: ${total_upnl:,.2f}\n"
                f"FORCE-CLOSING ALL POSITIONS + ACTIVATING KILL SWITCH\n"
                f"Time: {now}"
            )
            logger.critical(msg)
            await self._send(msg)
            await self._close_all(exchange, open_positions)
            self.kill_switch.activate(f"ESL catastrophic {loss_pct:.1f}%")

        elif loss_pct >= self.critical_pct:
            msg = (
                f"🟠 CRITICAL DRAWDOWN {loss_pct:.1f}% on {exchange.name}!\n"
                f"Equity: ${equity:,.2f} | UPnL: ${total_upnl:,.2f}\n"
                f"FORCE-CLOSING ALL POSITIONS\n"
                f"Time: {now}"
            )
            logger.error(msg)
            await self._send(msg)
            await self._close_all(exchange, open_positions)

        elif loss_pct >= self.warn_pct:
            msg = (
                f"⚠️ ESL WARNING: {loss_pct:.1f}% drawdown on {exchange.name}\n"
                f"Equity: ${equity:,.2f} | UPnL: ${total_upnl:,.2f}\n"
                f"Time: {now}"
            )
            logger.warning(msg)
            await self._send(msg)

    async def _close_all(
        self, exchange: BaseExchange, positions: List[Position]
    ) -> None:
        for pos in positions:
            try:
                result = await exchange.close_position(pos.symbol)
                logger.info(
                    "ESL closed %s on %s: fill_price=%s",
                    pos.symbol,
                    exchange.name,
                    result.fill_price,
                )
                await self._send(
                    f"✅ ESL closed {pos.symbol} on {exchange.name} "
                    f"@ {result.fill_price or 'market'}"
                )
            except Exception as exc:
                logger.error("ESL failed to close %s: %s", pos.symbol, exc)
                await self._send(
                    f"❌ ESL FAILED to close {pos.symbol} on {exchange.name}: {exc}"
                )

    async def _send(self, message: str) -> None:
        if self._send_alert:
            try:
                await self._send_alert(message)
            except Exception as exc:
                logger.error("ESL alert send failed: %s", exc)

    # ── Manual trigger (for tests / admin) ───────────────────────────────────

    async def check_now(self) -> None:
        """Run a single ESL check immediately (blocking)."""
        await self._check_all()
