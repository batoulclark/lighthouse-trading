"""
Tests for the webhook endpoint.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.bot import Bot, BotStore
from app.models.trade import Trade, TradeLog
from app.notifications.telegram import TelegramNotifier
from app.safety.kill_switch import KillSwitch
from app.services.order_executor import OrderExecutor
from app.services.signal_processor import SignalProcessor
from config import settings


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_dir(tmp_path):
    return tmp_path


@pytest.fixture()
def sample_bot(tmp_dir) -> Bot:
    bot = Bot.create(
        name="BTC Test Bot",
        exchange="hyperliquid",
        pair="BTCUSDT",
        leverage=3,
        webhook_secret="test-secret-abc123",
    )
    return bot


@pytest.fixture()
def bot_store(tmp_dir, sample_bot) -> BotStore:
    path = str(tmp_dir / "bots.json")
    store = BotStore(path)
    store.add(sample_bot)
    return store


@pytest.fixture()
def trade_log(tmp_dir) -> TradeLog:
    return TradeLog(str(tmp_dir / "trades.json"))


@pytest.fixture()
def kill_switch(tmp_dir) -> KillSwitch:
    return KillSwitch(str(tmp_dir / "KILL_SWITCH"))


@pytest.fixture()
def telegram() -> TelegramNotifier:
    notifier = MagicMock(spec=TelegramNotifier)
    notifier.send = AsyncMock(return_value=True)
    notifier.send_trade_alert = AsyncMock(return_value=True)
    notifier.send_error = AsyncMock(return_value=True)
    notifier.send_startup = AsyncMock(return_value=True)
    notifier.send_shutdown = AsyncMock(return_value=True)
    return notifier


@pytest.fixture()
def mock_exchange():
    from app.exchanges.base import Balance, OrderResult, Position
    exc = AsyncMock()
    exc.name = "hyperliquid"
    exc.market_buy = AsyncMock(
        return_value=OrderResult(
            exchange="hyperliquid",
            symbol="BTCUSDT",
            side="buy",
            size=0.001,
            fill_price=65000.0,
            order_id="test-order-1",
            fees=0.065,
            raw={"status": "ok"},
        )
    )
    exc.market_sell = AsyncMock(
        return_value=OrderResult(
            exchange="hyperliquid",
            symbol="BTCUSDT",
            side="sell",
            size=0.001,
            fill_price=65000.0,
            order_id="test-order-2",
            fees=0.065,
            raw={"status": "ok"},
        )
    )
    exc.close_position = AsyncMock(
        return_value=OrderResult(
            exchange="hyperliquid",
            symbol="BTCUSDT",
            side="close",
            size=0.001,
            fill_price=65000.0,
            order_id="test-order-3",
            fees=0.065,
            raw={"status": "ok"},
        )
    )
    exc.get_balance = AsyncMock(
        return_value=Balance(asset="USDT", total=10000.0, available=8000.0)
    )
    exc.get_price = AsyncMock(return_value=65000.0)
    exc.set_leverage = AsyncMock()
    return exc


@pytest.fixture()
def app_client(bot_store, trade_log, kill_switch, telegram, mock_exchange, sample_bot):
    """Build a TestClient with all dependencies wired."""
    from main import create_app
    from app.safety.emergency_sl import EmergencyStopLoss
    from app.safety.state_backup import StateBackup

    application = create_app()

    # Override the lifespan by manually setting state
    application.state.bot_store = bot_store
    application.state.trade_log = trade_log
    application.state.kill_switch = kill_switch
    application.state.telegram = telegram
    application.state.exchanges = {"hyperliquid": mock_exchange}
    application.state.signal_processor = SignalProcessor(bot_store)
    application.state.order_executor = OrderExecutor(
        exchanges={"hyperliquid": mock_exchange},
        kill_switch=kill_switch,
        trade_log=trade_log,
        telegram=telegram,
    )
    application.state.esl = MagicMock()
    application.state.backup = MagicMock()

    # Patch the lifespan so TestClient doesn't re-run it
    with patch("main.lifespan", new_callable=lambda: _dummy_lifespan):
        client = TestClient(application, raise_server_exceptions=False)
        yield client


def _dummy_lifespan(app):
    """No-op lifespan for tests."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _inner(app):
        yield

    return _inner(app)


# ── Helper ────────────────────────────────────────────────────────────────────

def _signal_payload(
    secret: str = "test-secret-abc123",
    ticker: str = "BTCUSDT",
    action: str = "buy",
    position_size: str = "1",
    order_size: str = "100%",
    ts: str | None = None,
) -> Dict[str, Any]:
    return {
        "bot_id": secret,
        "ticker": ticker,
        "action": action,
        "order_size": order_size,
        "position_size": position_size,
        "timestamp": ts or datetime.now(timezone.utc).isoformat(),
        "schema": "2",
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestWebhookEndpoint:
    def test_valid_buy_signal(self, app_client, sample_bot, mock_exchange):
        payload = _signal_payload()
        resp = app_client.post(f"/webhook/{sample_bot.id}", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["action"] == "buy"
        assert data["pair"] == "BTCUSDT"

    def test_valid_sell_signal(self, app_client, sample_bot, mock_exchange):
        payload = _signal_payload(action="sell", position_size="-1")
        resp = app_client.post(f"/webhook/{sample_bot.id}", json=payload)
        assert resp.status_code == 200
        assert resp.json()["action"] == "sell"

    def test_invalid_json_body(self, app_client, sample_bot):
        resp = app_client.post(
            f"/webhook/{sample_bot.id}",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_wrong_schema_version(self, app_client, sample_bot):
        payload = _signal_payload()
        payload["schema"] = "1"
        resp = app_client.post(f"/webhook/{sample_bot.id}", json=payload)
        assert resp.status_code == 400
        assert "schema" in resp.json()["detail"].lower()

    def test_unknown_bot_secret(self, app_client, sample_bot):
        payload = _signal_payload(secret="wrong-secret")
        resp = app_client.post(f"/webhook/{sample_bot.id}", json=payload)
        assert resp.status_code == 401

    def test_wrong_ticker(self, app_client, sample_bot):
        payload = _signal_payload(ticker="ETHUSDT")
        resp = app_client.post(f"/webhook/{sample_bot.id}", json=payload)
        assert resp.status_code == 400
        assert "ticker" in resp.json()["detail"].lower() or "pair" in resp.json()["detail"].lower()

    def test_duplicate_timestamp_rejected(self, app_client, sample_bot, mock_exchange):
        ts = "2024-01-01T12:00:00+00:00"
        payload = _signal_payload(ts=ts)
        # First signal — should succeed
        resp1 = app_client.post(f"/webhook/{sample_bot.id}", json=payload)
        assert resp1.status_code == 200
        # Second with same timestamp — should be rejected
        resp2 = app_client.post(f"/webhook/{sample_bot.id}", json=payload)
        assert resp2.status_code == 409

    def test_kill_switch_blocks_execution(self, app_client, sample_bot, kill_switch):
        kill_switch.activate("test")
        payload = _signal_payload()
        resp = app_client.post(f"/webhook/{sample_bot.id}", json=payload)
        # Signal is validated OK, but execution returns error
        assert resp.status_code == 422
        assert "kill switch" in resp.json()["error"].lower()
        kill_switch.deactivate()

    def test_health_endpoint(self, app_client):
        resp = app_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
