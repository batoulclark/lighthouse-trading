"""
Lighthouse Trading - FastAPI Entry Point
Bootstraps the application, wires dependencies, and starts the server.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import Dict

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import bots, dashboard, health, web, webhooks
from app.exchanges.base import BaseExchange
from app.models.bot import BotStore
from app.models.trade import TradeLog
from app.notifications.telegram import TelegramNotifier
from app.notifications.telegram_commands import TelegramCommandHandler
from app.safety.emergency_sl import EmergencyStopLoss
from app.safety.kill_switch import KillSwitch
from app.safety.state_backup import StateBackup
from app.services.order_executor import OrderExecutor
from app.services.position_manager import PositionManager
from app.services.monitor import MonitorService
from app.services.signal_processor import SignalProcessor
from config import settings

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ── Lifespan (startup / shutdown) ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire all dependencies and start background tasks."""

    # ── Core stores ───────────────────────────────────────────────────────
    bot_store = BotStore(settings.bots_file)
    trade_log = TradeLog(settings.trades_file)

    # ── Safety components ─────────────────────────────────────────────────
    kill_switch = KillSwitch(settings.kill_switch_file)

    backup = StateBackup(
        dir1=settings.backup_dir_1,
        dir2=settings.backup_dir_2,
        max_files=settings.backup_rotation,
    )

    # ── Telegram ──────────────────────────────────────────────────────────
    telegram = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )

    # ── Exchange connectors ───────────────────────────────────────────────
    exchanges: Dict[str, BaseExchange] = {}

    if settings.exchange_mode == "paper":
        from app.exchanges.paper import PaperExchange
        exchanges["paper"] = PaperExchange(
            starting_balance=settings.paper_starting_balance,
            trades_file=settings.paper_trades_file,
        )
        logger.info(
            "Paper trading mode — PaperExchange registered (balance=%.2f USDT)",
            settings.paper_starting_balance,
        )

    if settings.hl_private_key and settings.hl_account_address:
        try:
            from app.exchanges.hyperliquid import HyperliquidExchange
            exchanges["hyperliquid"] = HyperliquidExchange(
                private_key=settings.hl_private_key,
                account_address=settings.hl_account_address,
                testnet=settings.hl_testnet,
            )
            logger.info("Hyperliquid connector registered")
        except Exception as exc:
            logger.error("Failed to initialise Hyperliquid connector: %s", exc)
    else:
        logger.warning("Hyperliquid credentials not configured — connector skipped")

    if settings.binance_api_key and settings.binance_api_secret:
        try:
            from app.exchanges.binance_exchange import BinanceExchange
            exchanges["binance"] = BinanceExchange(
                api_key=settings.binance_api_key,
                api_secret=settings.binance_api_secret,
            )
            logger.info("Binance connector registered")
        except Exception as exc:
            logger.error("Failed to initialise Binance connector: %s", exc)
    else:
        logger.warning("Binance credentials not configured — connector skipped")

    # ── Services ──────────────────────────────────────────────────────────
    signal_processor = SignalProcessor(bot_store)

    position_manager = PositionManager()

    order_executor = OrderExecutor(
        exchanges=exchanges,
        kill_switch=kill_switch,
        trade_log=trade_log,
        telegram=telegram,
        position_manager=position_manager,
    )

    # ── Telegram command handler ───────────────────────────────────────────
    telegram_commands = TelegramCommandHandler(
        bot_token=settings.telegram_bot_token,
        allowed_chat_id=settings.telegram_chat_id,
        kill_switch=kill_switch,
        bot_store=bot_store,
        trade_log=trade_log,
        position_manager=position_manager,
    )

    # ── Monitoring service ────────────────────────────────────────────────
    monitor = MonitorService(
        kill_switch=kill_switch,
        position_manager=position_manager,
        bot_store=bot_store,
        telegram=telegram,
        trade_log=trade_log,
        exchanges=exchanges,
    )

    # ── Emergency Stop-Loss monitor ───────────────────────────────────────
    esl = EmergencyStopLoss(
        kill_switch=kill_switch,
        warn_pct=settings.esl_warn_pct,
        critical_pct=settings.esl_critical_pct,
        catastrophic_pct=settings.esl_catastrophic_pct,
        interval_seconds=60,
    )
    esl.set_alert_fn(telegram.send)

    # Register all bots' symbols with ESL
    for bot in bot_store.all():
        exchange = exchanges.get(bot.exchange)
        if exchange:
            esl.register_exchange(exchange, [bot.pair])

    # ── Attach to app.state ───────────────────────────────────────────────
    app.state.bot_store = bot_store
    app.state.trade_log = trade_log
    app.state.kill_switch = kill_switch
    app.state.telegram = telegram
    app.state.exchanges = exchanges
    app.state.signal_processor = signal_processor
    app.state.order_executor = order_executor
    app.state.position_manager = position_manager
    app.state.telegram_commands = telegram_commands
    app.state.esl = esl
    app.state.backup = backup
    app.state.monitor = monitor

    # ── Start background tasks ────────────────────────────────────────────
    await esl.start()
    await monitor.start()
    telegram_commands.start()

    # ── Startup notification ──────────────────────────────────────────────
    await telegram.send_startup(settings.host, settings.port)
    logger.info(
        "Lighthouse Trading started — %s:%d", settings.host, settings.port
    )

    # ── Initial backup ────────────────────────────────────────────────────
    try:
        backup.save(
            bots=[b.to_dict() for b in bot_store.all()],
            trades=trade_log.all(),
        )
    except Exception as exc:
        logger.warning("Initial backup failed: %s", exc)

    yield  # ← application runs here

    # ── Shutdown ──────────────────────────────────────────────────────────
    await esl.stop()
    await monitor.stop()
    telegram_commands.stop()
    await telegram.send_shutdown()

    # Final backup on shutdown
    try:
        backup.save(
            bots=[b.to_dict() for b in bot_store.all()],
            trades=trade_log.all(),
        )
    except Exception as exc:
        logger.warning("Shutdown backup failed: %s", exc)

    logger.info("Lighthouse Trading stopped")


# ── Application factory ───────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Lighthouse Trading",
        description="Personal automated trading platform — TradingView → Exchange",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    app.include_router(web.router)       # GET / (dashboard HTML) — registered first
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(bots.router)
    app.include_router(dashboard.router)

    return app


app = create_app()


# ── Dev entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
