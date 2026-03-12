"""
Tests for the Dashboard API — /dashboard, /dashboard/bot/{id},
/dashboard/equity, /dashboard/trades.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.bot import Bot, BotStore
from app.models.trade import TradeLog
from app.safety.kill_switch import KillSwitch
from app.services.performance import PerformanceTracker


# ── Dummy lifespan ────────────────────────────────────────────────────────────

def _dummy_lifespan(app):
    @asynccontextmanager
    async def _inner(app):
        yield
    return _inner(app)


# ── Trade helper ──────────────────────────────────────────────────────────────

def _trade(
    *,
    bot_id: str = "bot-abc",
    bot_name: str = "Test Bot",
    pair: str = "BTCUSDT",
    pnl: float | None = 100.0,
    days_ago: int = 1,
) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "timestamp": ts,
        "bot_id": bot_id,
        "bot_name": bot_name,
        "exchange": "hyperliquid",
        "pair": pair,
        "action": "buy",
        "order_size": "100%",
        "position_size": "1",
        "fill_price": 50000.0,
        "quantity": 0.01,
        "fees": 0.5,
        "pnl": pnl,
        "execution_result": {},
        "signal_timestamp": ts,
        "error": None,
    }


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_dir(tmp_path):
    return tmp_path


@pytest.fixture()
def sample_bot(tmp_dir) -> Bot:
    return Bot.create(
        name="Test Bot",
        exchange="hyperliquid",
        pair="BTCUSDT",
        leverage=3,
        webhook_secret="test-secret",
    )


@pytest.fixture()
def bot_store(tmp_dir, sample_bot) -> BotStore:
    store = BotStore(str(tmp_dir / "bots.json"))
    store.add(sample_bot)
    return store


@pytest.fixture()
def trade_log_with_data(tmp_dir) -> TradeLog:
    log = TradeLog(str(tmp_dir / "trades.json"))
    # Pre-populate with some trades via direct write
    trades_data = [
        _trade(pnl=200.0,  days_ago=5),
        _trade(pnl=-50.0,  days_ago=4),
        _trade(pnl=100.0,  days_ago=3),
        _trade(pnl=None,   days_ago=2),   # error trade
        _trade(pnl=75.0,   days_ago=1),
    ]
    import os
    os.makedirs(os.path.dirname(log.path) or ".", exist_ok=True)
    with open(log.path, "w") as f:
        json.dump(trades_data, f)
    return log


@pytest.fixture()
def kill_switch(tmp_dir) -> KillSwitch:
    return KillSwitch(str(tmp_dir / "KILL_SWITCH"))


@pytest.fixture()
def app_client(bot_store, trade_log_with_data, kill_switch, sample_bot):
    """Build a TestClient with dashboard router wired and state pre-loaded."""
    from main import create_app

    application = create_app()

    application.state.bot_store  = bot_store
    application.state.trade_log  = trade_log_with_data
    application.state.kill_switch = kill_switch

    with patch("main.lifespan", new_callable=lambda: _dummy_lifespan):
        client = TestClient(application, raise_server_exceptions=True)
        yield client, sample_bot


# ── /dashboard ────────────────────────────────────────────────────────────────

class TestDashboardOverview:
    def test_status_200(self, app_client):
        client, _ = app_client
        resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_has_required_keys(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard").json()
        assert "timestamp" in data
        assert "kill_switch_active" in data
        assert "bot_count" in data
        assert "bots" in data
        assert "performance" in data

    def test_kill_switch_false_by_default(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard").json()
        assert data["kill_switch_active"] is False

    def test_kill_switch_true_when_active(self, app_client):
        client, _ = app_client
        client.app.state.kill_switch.activate("test")
        data = client.get("/dashboard").json()
        assert data["kill_switch_active"] is True
        client.app.state.kill_switch.deactivate()

    def test_bot_count_matches(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard").json()
        assert data["bot_count"] == 1

    def test_performance_has_total_pnl(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard").json()
        assert "total_pnl" in data["performance"]


# ── /dashboard/bot/{bot_id} ───────────────────────────────────────────────────

class TestDashboardBot:
    def test_valid_bot_200(self, app_client):
        client, bot = app_client
        resp = client.get(f"/dashboard/bot/{bot.id}")
        assert resp.status_code == 200

    def test_unknown_bot_404(self, app_client):
        client, _ = app_client
        resp = client.get("/dashboard/bot/nonexistent-uuid")
        assert resp.status_code == 404

    def test_response_has_bot_info(self, app_client):
        client, bot = app_client
        data = client.get(f"/dashboard/bot/{bot.id}").json()
        assert data["bot"]["id"] == bot.id
        assert data["bot"]["name"] == bot.name

    def test_response_has_performance(self, app_client):
        client, bot = app_client
        data = client.get(f"/dashboard/bot/{bot.id}").json()
        assert "performance" in data
        assert "win_rate" in data["performance"]

    def test_response_has_daily_pnl(self, app_client):
        client, bot = app_client
        data = client.get(f"/dashboard/bot/{bot.id}").json()
        assert "daily_pnl" in data
        assert isinstance(data["daily_pnl"], list)


# ── /dashboard/equity ─────────────────────────────────────────────────────────

class TestDashboardEquity:
    def test_status_200(self, app_client):
        client, _ = app_client
        resp = client.get("/dashboard/equity")
        assert resp.status_code == 200

    def test_equity_curve_key(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard/equity").json()
        assert "equity_curve" in data
        assert isinstance(data["equity_curve"], list)

    def test_each_entry_has_date_equity(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard/equity").json()
        curve = data["equity_curve"]
        if curve:
            assert "date" in curve[0]
            assert "equity" in curve[0]

    def test_equity_ascending_by_date(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard/equity").json()
        dates = [p["date"] for p in data["equity_curve"]]
        assert dates == sorted(dates)


# ── /dashboard/trades ─────────────────────────────────────────────────────────

class TestDashboardTrades:
    def test_status_200(self, app_client):
        client, _ = app_client
        resp = client.get("/dashboard/trades")
        assert resp.status_code == 200

    def test_has_trades_and_total(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard/trades").json()
        assert "trades" in data
        assert "total" in data

    def test_default_limit_applied(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard/trades").json()
        assert data["total"] <= 100

    def test_limit_query_param(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard/trades?limit=2").json()
        assert len(data["trades"]) <= 2

    def test_bot_id_filter(self, app_client):
        client, bot = app_client
        data = client.get(f"/dashboard/trades?bot_id={bot.id}").json()
        # All returned trades should match bot_id (or empty list)
        for t in data["trades"]:
            assert t["bot_id"] == bot.id

    def test_date_from_filter(self, app_client):
        client, _ = app_client
        future = "2099-01-01"
        data = client.get(f"/dashboard/trades?date_from={future}").json()
        assert data["total"] == 0

    def test_date_to_filter(self, app_client):
        client, _ = app_client
        past = "1990-01-01"
        data = client.get(f"/dashboard/trades?date_to={past}").json()
        assert data["total"] == 0

    def test_most_recent_first(self, app_client):
        client, _ = app_client
        data = client.get("/dashboard/trades").json()
        timestamps = [t["timestamp"] for t in data["trades"]]
        assert timestamps == sorted(timestamps, reverse=True)


# ── Alert Generator smoke test ────────────────────────────────────────────────

class TestAlertGenerator:
    def test_generate_returns_pine_code(self):
        from app.pinescript.alert_generator import AlertGenerator
        gen = AlertGenerator()
        code = gen.generate(bot_id="my-secret")
        assert "//@version=6" in code
        assert "my-secret" in code
        assert "alert(" in code
        assert "alertcondition(" in code

    def test_generate_contains_strategy_logic(self):
        from app.pinescript.alert_generator import AlertGenerator
        gen = AlertGenerator({"period": 144, "poles": 2})
        code = gen.generate(bot_id="test-bot")
        assert "gaussianFilter" in code
        assert "ta.atr(" in code
        assert "cross_above_upper" in code
        assert "cross_below_lower" in code

    def test_generate_alert_json_schema(self):
        from app.pinescript.alert_generator import AlertGenerator
        gen = AlertGenerator()
        code = gen.generate(bot_id="bot-xyz")
        assert '"schema":"2"' in code
        assert "bot-xyz" in code
        assert "{{timenow}}" in code
        assert "{{ticker}}" in code

    def test_save_writes_file(self, tmp_path):
        from app.pinescript.alert_generator import AlertGenerator
        gen = AlertGenerator()
        path = str(tmp_path / "test.pine")
        gen.save(path, bot_id="saved-bot")
        import os
        assert os.path.exists(path)
        with open(path) as f:
            content = f.read()
        assert "saved-bot" in content
        assert "//@version=6" in content
