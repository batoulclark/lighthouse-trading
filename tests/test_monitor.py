"""
Tests for app/services/monitor.py

Covers:
- Alert generation for each check type
- Threshold logic (drawdown)
- Alert history persistence and rotation
- Connectivity error handling
- start() / stop() lifecycle
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.bot import Bot, BotStore
from app.models.trade import Trade, TradeLog
from app.safety.kill_switch import KillSwitch
from app.services.monitor import AlertLevel, MonitorService
from app.services.position_manager import Position, PositionManager


# ── Helpers / fixtures ────────────────────────────────────────────────────────

def _make_bot(enabled: bool = True, tmp_dir=None) -> Bot:
    return Bot.create(
        name="Test Bot",
        exchange="hyperliquid",
        pair="BTCUSDT",
        leverage=3,
    )


def _make_monitor(
    tmp_path,
    *,
    ks_active: bool = False,
    positions: List[Position] | None = None,
    trades: List[dict] | None = None,
    bots: List[Bot] | None = None,
    exchanges: dict | None = None,
) -> MonitorService:
    """Factory that wires up a MonitorService with mocked dependencies."""
    alerts_file = str(tmp_path / "alerts.json")

    # Kill switch
    ks_path = str(tmp_path / "KILL_SWITCH")
    ks = KillSwitch(ks_path)
    if ks_active:
        ks.activate("test")

    # Position manager
    pm = MagicMock(spec=PositionManager)
    pm.get_all_positions.return_value = positions or []

    # Bot store
    bs = MagicMock(spec=BotStore)
    bs.all.return_value = bots or []

    # Trade log
    tl = MagicMock(spec=TradeLog)
    tl.all.return_value = trades or []

    # Telegram (silent)
    tg = AsyncMock()
    tg.send = AsyncMock(return_value=True)

    return MonitorService(
        kill_switch=ks,
        position_manager=pm,
        bot_store=bs,
        telegram=tg,
        trade_log=tl,
        exchanges=exchanges or {},
        alerts_file=alerts_file,
        interval_seconds=9999,  # prevent auto-loop during tests
    )


def _open_position(
    bot_id: str = "bot-1",
    symbol: str = "BTCUSDT",
    days_ago: float = 1.0,
) -> Position:
    opened_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return Position(
        bot_id=bot_id,
        symbol=symbol,
        side="long",
        size=0.1,
        entry_price=50_000.0,
        current_price=51_000.0,
        leverage=3,
        unrealized_pnl=300.0,
        opened_at=opened_at,
    )


# ── Kill switch check ─────────────────────────────────────────────────────────

class TestKillSwitchCheck:
    def test_no_alert_when_inactive(self, tmp_path):
        m = _make_monitor(tmp_path, ks_active=False)
        alerts = asyncio.run(m._check_kill_switch())
        assert alerts == []

    def test_warning_when_active(self, tmp_path):
        m = _make_monitor(tmp_path, ks_active=True)
        alerts = asyncio.run(m._check_kill_switch())
        assert len(alerts) == 1
        assert alerts[0]["level"] == AlertLevel.WARNING
        assert alerts[0]["check"] == "kill_switch"


# ── Connectivity check ────────────────────────────────────────────────────────

class TestConnectivityCheck:
    def test_no_alert_when_no_exchanges(self, tmp_path):
        m = _make_monitor(tmp_path, exchanges={})
        alerts = asyncio.run(m._check_connectivity())
        assert alerts == []

    def test_no_alert_on_success(self, tmp_path):
        exchange = AsyncMock()
        exchange.get_equity = AsyncMock(return_value=10_000.0)
        m = _make_monitor(tmp_path, exchanges={"hl": exchange})
        alerts = asyncio.run(m._check_connectivity())
        assert alerts == []

    def test_warning_on_error(self, tmp_path):
        exchange = AsyncMock()
        exchange.get_equity = AsyncMock(side_effect=RuntimeError("connection refused"))
        m = _make_monitor(tmp_path, exchanges={"hl": exchange})
        alerts = asyncio.run(m._check_connectivity())
        assert len(alerts) == 1
        assert alerts[0]["level"] == AlertLevel.WARNING
        assert "hl" in alerts[0]["message"]

    def test_critical_on_timeout(self, tmp_path):
        exchange = AsyncMock()
        exchange.get_equity = AsyncMock(side_effect=asyncio.TimeoutError())
        m = _make_monitor(tmp_path, exchanges={"hl": exchange})

        # Patch wait_for to raise TimeoutError directly
        async def _mock_wait_for(coro, timeout):
            raise asyncio.TimeoutError()

        with patch("app.services.monitor.asyncio.wait_for", _mock_wait_for):
            alerts = asyncio.run(m._check_connectivity())
        assert len(alerts) == 1
        assert alerts[0]["level"] == AlertLevel.CRITICAL


# ── Position health check ─────────────────────────────────────────────────────

class TestPositionHealthCheck:
    def test_no_alert_fresh_position(self, tmp_path):
        pos = _open_position(days_ago=1)
        m = _make_monitor(tmp_path, positions=[pos])
        alerts = asyncio.run(m._check_position_health())
        assert alerts == []

    def test_warning_for_stale_position(self, tmp_path):
        pos = _open_position(days_ago=8)  # > 7 days
        m = _make_monitor(tmp_path, positions=[pos])
        alerts = asyncio.run(m._check_position_health())
        assert len(alerts) == 1
        assert alerts[0]["level"] == AlertLevel.WARNING
        assert alerts[0]["check"] == "position_health"
        assert alerts[0]["age_days"] >= 8

    def test_no_positions_no_alert(self, tmp_path):
        m = _make_monitor(tmp_path, positions=[])
        alerts = asyncio.run(m._check_position_health())
        assert alerts == []

    def test_exactly_at_threshold_no_alert(self, tmp_path):
        pos = _open_position(days_ago=7)  # exactly 7 days — not over threshold
        m = _make_monitor(tmp_path, positions=[pos])
        alerts = asyncio.run(m._check_position_health())
        # 7 days exactly is not *greater than* 7 days
        assert alerts == []


# ── Drawdown check ────────────────────────────────────────────────────────────

class TestDrawdownCheck:
    def _make_exchange(self, equity: float) -> AsyncMock:
        ex = AsyncMock()
        ex.get_equity = AsyncMock(return_value=equity)
        return ex

    def _run_drawdown(self, monitor, equity_value: float, exchange_name: str = "hl"):
        monitor.exchanges[exchange_name].get_equity = AsyncMock(return_value=equity_value)
        return asyncio.run(monitor._check_drawdown())

    def test_no_alert_below_threshold(self, tmp_path):
        ex = self._make_exchange(10_000.0)
        m = _make_monitor(tmp_path, exchanges={"hl": ex})
        m._peak_equity["hl"] = 10_000.0
        ex.get_equity = AsyncMock(return_value=9_500.0)  # 5% down — below 10%
        alerts = asyncio.run(m._check_drawdown())
        assert alerts == []

    def test_warning_at_10pct(self, tmp_path):
        ex = self._make_exchange(9_000.0)
        m = _make_monitor(tmp_path, exchanges={"hl": ex})
        m._peak_equity["hl"] = 10_000.0
        alerts = asyncio.run(m._check_drawdown())
        assert len(alerts) == 1
        assert alerts[0]["level"] == AlertLevel.WARNING
        assert alerts[0]["drawdown_pct"] == pytest.approx(10.0)

    def test_warning_at_15pct(self, tmp_path):
        ex = self._make_exchange(8_500.0)
        m = _make_monitor(tmp_path, exchanges={"hl": ex})
        m._peak_equity["hl"] = 10_000.0
        alerts = asyncio.run(m._check_drawdown())
        assert len(alerts) == 1
        assert alerts[0]["level"] == AlertLevel.WARNING
        assert alerts[0]["drawdown_pct"] == pytest.approx(15.0)

    def test_critical_at_20pct(self, tmp_path):
        ex = self._make_exchange(8_000.0)
        m = _make_monitor(tmp_path, exchanges={"hl": ex})
        m._peak_equity["hl"] = 10_000.0
        alerts = asyncio.run(m._check_drawdown())
        assert len(alerts) == 1
        assert alerts[0]["level"] == AlertLevel.CRITICAL

    def test_peak_updated_on_higher_equity(self, tmp_path):
        ex = self._make_exchange(12_000.0)
        m = _make_monitor(tmp_path, exchanges={"hl": ex})
        m._peak_equity["hl"] = 10_000.0
        asyncio.run(m._check_drawdown())
        assert m._peak_equity["hl"] == 12_000.0

    def test_zero_equity_skipped(self, tmp_path):
        ex = self._make_exchange(0.0)
        m = _make_monitor(tmp_path, exchanges={"hl": ex})
        alerts = asyncio.run(m._check_drawdown())
        assert alerts == []


# ── Stale signal check ────────────────────────────────────────────────────────

class TestStaleSignalCheck:
    def _trade(self, hours_ago: float) -> dict:
        ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
        return {"timestamp": ts, "bot_id": "b1", "pair": "BTCUSDT"}

    def test_no_alert_when_no_bots(self, tmp_path):
        m = _make_monitor(tmp_path, bots=[], trades=[self._trade(30)])
        alerts = asyncio.run(m._check_stale_signals())
        assert alerts == []

    def test_no_alert_recent_trade(self, tmp_path):
        bot = _make_bot(enabled=True)
        m = _make_monitor(tmp_path, bots=[bot], trades=[self._trade(2)])
        alerts = asyncio.run(m._check_stale_signals())
        assert alerts == []

    def test_warning_stale_signals(self, tmp_path):
        bot = _make_bot(enabled=True)
        m = _make_monitor(tmp_path, bots=[bot], trades=[self._trade(26)])
        alerts = asyncio.run(m._check_stale_signals())
        assert len(alerts) == 1
        assert alerts[0]["level"] == AlertLevel.WARNING
        assert alerts[0]["check"] == "stale_signals"

    def test_no_alert_no_trades_ever(self, tmp_path):
        bot = _make_bot(enabled=True)
        m = _make_monitor(tmp_path, bots=[bot], trades=[])
        alerts = asyncio.run(m._check_stale_signals())
        assert alerts == []

    def test_only_disabled_bots_no_alert(self, tmp_path):
        bot = _make_bot(enabled=True)
        bot.enabled = False
        m = _make_monitor(tmp_path, bots=[bot], trades=[self._trade(30)])
        alerts = asyncio.run(m._check_stale_signals())
        assert alerts == []


# ── Alert persistence ─────────────────────────────────────────────────────────

class TestAlertPersistence:
    def test_alert_saved_to_file(self, tmp_path):
        m = _make_monitor(tmp_path, ks_active=True)
        asyncio.run(m.check_all())
        alerts_file = m.alerts_file
        assert os.path.exists(alerts_file)
        with open(alerts_file) as f:
            data = json.load(f)
        assert len(data) >= 1

    def test_rotation_at_1000(self, tmp_path):
        m = _make_monitor(tmp_path)
        # Pre-populate with 1000 entries
        old = [{"timestamp": "2020-01-01T00:00:00", "level": "INFO", "check": "test", "message": "old"}]
        m._load_alerts()  # ensure file can be read
        # Write 1000 old alerts directly
        alerts_file = m.alerts_file
        with open(alerts_file, "w") as f:
            json.dump(old * 1000, f)
        # Add one more
        new_alert = m._make_alert(level=AlertLevel.INFO, check="test", message="new")
        m._persist_alert(new_alert)
        with open(alerts_file) as f:
            data = json.load(f)
        assert len(data) == 1000
        assert data[-1]["message"] == "new"

    def test_get_alert_history_newest_first(self, tmp_path):
        m = _make_monitor(tmp_path)
        older = m._make_alert(level=AlertLevel.INFO, check="c", message="older")
        older["timestamp"] = "2020-01-01T00:00:00"
        newer = m._make_alert(level=AlertLevel.INFO, check="c", message="newer")
        newer["timestamp"] = "2025-01-01T00:00:00"
        m._persist_alert(older)
        m._persist_alert(newer)

        history = m.get_alert_history(limit=10)
        assert history[0]["message"] == "newer"
        assert history[1]["message"] == "older"

    def test_get_alert_history_respects_limit(self, tmp_path):
        m = _make_monitor(tmp_path)
        for i in range(10):
            m._persist_alert(m._make_alert(AlertLevel.INFO, "c", f"alert {i}"))
        history = m.get_alert_history(limit=3)
        assert len(history) == 3


# ── Telegram notification ─────────────────────────────────────────────────────

class TestTelegramNotification:
    def test_telegram_called_for_warning(self, tmp_path):
        m = _make_monitor(tmp_path, ks_active=True)
        asyncio.run(m.check_all())
        m.telegram.send.assert_awaited()

    def test_telegram_not_called_for_info_only(self, tmp_path):
        """No alerts → no Telegram messages."""
        m = _make_monitor(tmp_path)
        asyncio.run(m.check_all())
        m.telegram.send.assert_not_awaited()


# ── check_all integration ─────────────────────────────────────────────────────

class TestCheckAll:
    def test_returns_list(self, tmp_path):
        m = _make_monitor(tmp_path)
        result = asyncio.run(m.check_all())
        assert isinstance(result, list)

    def test_multiple_checks_combined(self, tmp_path):
        # Kill switch active + stale position
        pos = _open_position(days_ago=10)
        bot = _make_bot(enabled=True)
        m = _make_monitor(tmp_path, ks_active=True, positions=[pos], bots=[bot])
        alerts = asyncio.run(m.check_all())
        checks = {a["check"] for a in alerts}
        assert "kill_switch" in checks
        assert "position_health" in checks


# ── Lifecycle ─────────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_start_stop(self, tmp_path):
        m = _make_monitor(tmp_path)

        async def _run():
            await m.start()
            assert m._running is True
            assert m._task is not None
            await m.stop()
            assert m._running is False

        asyncio.run(_run())

    def test_double_start_is_safe(self, tmp_path):
        m = _make_monitor(tmp_path)

        async def _run():
            await m.start()
            task1 = m._task
            await m.start()  # second call is a no-op
            assert m._task is task1
            await m.stop()

        asyncio.run(_run())
