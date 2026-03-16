"""
Lighthouse Trading - Order Executor
Executes validated signals on the correct exchange after all safety checks.

Signal Flow
-----------
Signal → Kill Switch check → ESL check → Resolve exchange →
Set leverage → Calculate size → Execute order → Log trade → Telegram alert
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from app.exchanges.base import BaseExchange, OrderResult
from app.models.bot import Bot
from app.models.signal import Signal
from app.models.trade import Trade, TradeLog
from app.notifications.telegram import TelegramNotifier
from app.safety.kill_switch import KillSwitch
from app.services.position_manager import PositionManager

logger = logging.getLogger(__name__)


class OrderExecutor:
    """
    Routes an accepted signal to the correct exchange connector
    and records the result.
    """

    def __init__(
        self,
        exchanges: Dict[str, BaseExchange],
        kill_switch: KillSwitch,
        trade_log: TradeLog,
        telegram: TelegramNotifier,
        position_manager: Optional[PositionManager] = None,
    ) -> None:
        self._exchanges = exchanges
        self._kill_switch = kill_switch
        self._trade_log = trade_log
        self._telegram = telegram
        self._position_manager = position_manager

    # ── Public API ───────────────────────────────────────────────────────────

    async def execute(self, signal: Signal, bot: Bot) -> Trade:
        """
        Execute a validated signal for the given bot.

        Returns the Trade record (success or failure).
        Never raises — errors are captured in the trade record and
        surfaced via Telegram.
        """
        # ── Safety gate 1: Kill Switch ────────────────────────────────────
        try:
            self._kill_switch.check_and_raise()
        except RuntimeError as exc:
            return await self._record_error(signal, bot, str(exc))

        # ── Resolve exchange ──────────────────────────────────────────────
        exchange = self._exchanges.get(bot.exchange)
        if exchange is None:
            msg = f"No connector registered for exchange '{bot.exchange}'"
            logger.error(msg)
            return await self._record_error(signal, bot, msg)

        # ── Set leverage ──────────────────────────────────────────────────
        # Inject signal price into paper exchange
        if hasattr(exchange, "set_price") and getattr(signal, "price", None):
            exchange.set_price(bot.pair, signal.price)

        try:
            await exchange.set_leverage(bot.pair, bot.leverage)
        except Exception as exc:
            logger.warning("set_leverage failed (non-fatal): %s", exc)

        # ── Safety gate 2: Position guard (prevent double-entry) ─────────
        if not signal.is_close() and self._position_manager:
            existing = self._position_manager.get_position(bot.id)
            if existing is not None:
                side_match = (signal.is_long() and existing.side == "long") or \
                             (signal.is_short() and existing.side == "short")
                if side_match:
                    msg = (
                        f"Position guard: bot '{bot.name}' already has an open "
                        f"{existing.side} position. Signal rejected to prevent double-entry."
                    )
                    logger.warning(msg)
                    return await self._record_error(signal, bot, msg)

        # ── Determine order size (skip for close signals) ─────────────────
        if signal.is_close():
            size = 0.0  # close_position handles its own sizing
        else:
            try:
                size = await self._resolve_size(signal, bot, exchange)
            except Exception as exc:
                return await self._record_error(signal, bot, f"Size calculation error: {exc}")

            if size <= 0:
                return await self._record_error(signal, bot, "Calculated size is zero or negative")

        # ── Execute order ─────────────────────────────────────────────────
        result: Optional[OrderResult] = None
        error: Optional[str] = None
        try:
            result = await self._dispatch(signal, bot, exchange, size)
            logger.info(
                "Order executed: bot=%s %s %s size=%.6f fill=%.4f",
                bot.name,
                signal.action,
                bot.pair,
                size,
                result.fill_price or 0,
            )
        except Exception as exc:
            error = str(exc)
            logger.error("Order execution failed: %s", error)

        # ── Build trade record ────────────────────────────────────────────
        trade = Trade.from_execution(
            bot_id=bot.id,
            bot_name=bot.name,
            exchange=bot.exchange,
            pair=bot.pair,
            action=signal.action,
            order_size=signal.order_size,
            position_size=signal.position_size,
            signal_timestamp=signal.timestamp,
            execution_result=result.raw if result else {},
            fill_price=result.fill_price if result else None,
            quantity=result.size if result else None,
            fees=result.fees if result else None,
            error=error,
        )

        # ── Update position manager ───────────────────────────────────────────
        if self._position_manager and not error and result:
            try:
                if signal.is_close():
                    self._position_manager.close_position(
                        bot.id, bot.pair, result.fill_price or 0.0
                    )
                elif signal.is_long():
                    self._position_manager.open_position(
                        bot_id=bot.id,
                        symbol=bot.pair,
                        side="long",
                        size=result.size or 0.0,
                        entry_price=result.fill_price or 0.0,
                        leverage=bot.leverage,
                    )
                elif signal.is_short():
                    self._position_manager.open_position(
                        bot_id=bot.id,
                        symbol=bot.pair,
                        side="short",
                        size=result.size or 0.0,
                        entry_price=result.fill_price or 0.0,
                        leverage=bot.leverage,
                    )
            except Exception as exc:
                logger.error("Position manager update failed: %s", exc)

        # ── Persist ───────────────────────────────────────────────────────
        try:
            self._trade_log.append(trade)
        except Exception as exc:
            logger.error("Trade log write failed: %s", exc)

        # ── Telegram alert ────────────────────────────────────────────────
        if error:
            await self._telegram.send_error(f"Order {bot.name}/{bot.pair}", error)
        else:
            await self._telegram.send_trade_alert(
                action=signal.action,
                symbol=bot.pair,
                exchange=bot.exchange,
                fill_price=result.fill_price if result else None,
                size=result.size if result else size,
                bot_name=bot.name,
            )

        return trade

    # ── Private helpers ──────────────────────────────────────────────────────

    async def _dispatch(
        self,
        signal: Signal,
        bot: Bot,
        exchange: BaseExchange,
        size: float,
    ) -> OrderResult:
        """Map signal action → exchange call."""
        action = signal.action.lower()

        if signal.is_close():
            return await exchange.close_position(bot.pair)

        if action == "buy":
            return await exchange.market_buy(bot.pair, size)
        elif action == "sell":
            return await exchange.market_sell(bot.pair, size)
        else:
            raise ValueError(f"Unknown action: {action}")

    async def _resolve_size(
        self,
        signal: Signal,
        bot: Bot,
        exchange: BaseExchange,
    ) -> float:
        """
        Convert order_size (e.g. '100%', '0.5') to a concrete base-asset qty.

        '100%' means 'use 100% of available quote balance at current price'.
        A plain float is treated as base-asset units directly.
        """
        order_size_str = signal.order_size.strip()

        if order_size_str.endswith("%"):
            # Percentage of available balance
            fraction = float(order_size_str[:-1]) / 100.0
            balance = await exchange.get_balance()
            available = balance.available
            if available <= 0:
                raise ValueError("Available balance is zero")
            price = await exchange.get_price(bot.pair)
            if price <= 0:
                raise ValueError(f"Got zero price for {bot.pair}")
            notional = available * fraction * bot.leverage
            size = notional / price
            return round(size, 6)
        else:
            return round(float(order_size_str), 6)

    async def _record_error(self, signal: Signal, bot: Bot, error: str) -> Trade:
        """Create and persist a failed trade record."""
        trade = Trade.from_execution(
            bot_id=bot.id,
            bot_name=bot.name,
            exchange=bot.exchange,
            pair=bot.pair,
            action=signal.action,
            order_size=signal.order_size,
            position_size=signal.position_size,
            signal_timestamp=signal.timestamp,
            execution_result={},
            error=error,
        )
        try:
            self._trade_log.append(trade)
        except Exception as exc:
            logger.error("Trade log write failed during error record: %s", exc)
        await self._telegram.send_error(f"Bot {bot.name}", error)
        return trade
