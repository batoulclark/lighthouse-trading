"""
Tests for the safety system: KillSwitch, EmergencyStopLoss, StateBackup.
"""

from __future__ import annotations

import asyncio
import os
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.exchanges.base import Balance, OrderResult, Position
from app.safety.emergency_sl import EmergencyStopLoss
from app.safety.kill_switch import KillSwitch
from app.safety.state_backup import StateBackup


# ── KillSwitch tests ──────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_inactive_by_default(self, tmp_path):
        ks = KillSwitch(str(tmp_path / "KILL_SWITCH"))
        assert not ks.is_active()

    def test_activate_creates_file(self, tmp_path):
        path = str(tmp_path / "KILL_SWITCH")
        ks = KillSwitch(path)
        ks.activate("test reason")
        assert os.path.exists(path)
        assert ks.is_active()

    def test_activate_stores_reason(self, tmp_path):
        path = str(tmp_path / "KILL_SWITCH")
        ks = KillSwitch(path)
        ks.activate("ESL catastrophic 35%")
        reason = ks.read_reason()
        assert "ESL catastrophic" in reason

    def test_deactivate_removes_file(self, tmp_path):
        path = str(tmp_path / "KILL_SWITCH")
        ks = KillSwitch(path)
        ks.activate("x")
        assert ks.deactivate() is True
        assert not ks.is_active()

    def test_deactivate_when_inactive_returns_false(self, tmp_path):
        ks = KillSwitch(str(tmp_path / "KILL_SWITCH"))
        assert ks.deactivate() is False

    def test_check_and_raise_when_active(self, tmp_path):
        path = str(tmp_path / "KILL_SWITCH")
        ks = KillSwitch(path)
        ks.activate("test")
        with pytest.raises(RuntimeError, match="Kill switch is active"):
            ks.check_and_raise()

    def test_check_and_raise_when_inactive(self, tmp_path):
        ks = KillSwitch(str(tmp_path / "KILL_SWITCH"))
        ks.check_and_raise()  # should not raise

    def test_multiple_checks_log_once(self, tmp_path, caplog):
        import logging
        path = str(tmp_path / "KILL_SWITCH")
        ks = KillSwitch(path)
        ks.activate("test")
        with pytest.raises(RuntimeError):
            ks.check_and_raise()
        with pytest.raises(RuntimeError):
            ks.check_and_raise()
        # _notified flag prevents repeated critical logs
        assert ks._notified is True


# ── EmergencyStopLoss tests ───────────────────────────────────────────────────

def _make_exchange(
    equity: float,
    positions: List[Position],
) -> MagicMock:
    exc = MagicMock()
    exc.name = "test_exchange"
    exc.get_equity = AsyncMock(return_value=equity)
    exc.get_position = AsyncMock(side_effect=lambda sym: next(
        (p for p in positions if p.symbol == sym), None
    ))
    exc.close_position = AsyncMock(
        return_value=OrderResult(
            exchange="test_exchange",
            symbol="BTCUSDT",
            side="close",
            size=0.1,
            fill_price=60000.0,
            order_id="esl-close",
            fees=None,
            raw={},
        )
    )
    return exc


class TestEmergencyStopLoss:
    def _make_esl(self, tmp_path, warn=15, critical=20, catastrophic=30):
        ks = KillSwitch(str(tmp_path / "KILL_SWITCH"))
        esl = EmergencyStopLoss(
            kill_switch=ks,
            warn_pct=warn,
            critical_pct=critical,
            catastrophic_pct=catastrophic,
            interval_seconds=3600,
        )
        alerts = []
        async def _send(msg):
            alerts.append(msg)
        esl.set_alert_fn(_send)
        return esl, ks, alerts

    @pytest.mark.asyncio
    async def test_no_alert_within_safe_range(self, tmp_path):
        esl, ks, alerts = self._make_esl(tmp_path)
        # 5% loss → safe
        pos = Position("BTCUSDT", "long", 0.1, 60000, -300.0, 1)
        exc = _make_exchange(equity=10000, positions=[pos])
        esl.register_exchange(exc, ["BTCUSDT"])
        await esl.check_now()
        assert not alerts
        assert not ks.is_active()

    @pytest.mark.asyncio
    async def test_warn_alert_at_threshold(self, tmp_path):
        esl, ks, alerts = self._make_esl(tmp_path)
        # 16% loss → warn
        pos = Position("BTCUSDT", "long", 0.1, 60000, -1600.0, 1)
        exc = _make_exchange(equity=10000, positions=[pos])
        esl.register_exchange(exc, ["BTCUSDT"])
        await esl.check_now()
        assert any("WARNING" in a for a in alerts)
        assert not ks.is_active()

    @pytest.mark.asyncio
    async def test_critical_closes_positions(self, tmp_path):
        esl, ks, alerts = self._make_esl(tmp_path)
        # 22% loss → critical
        pos = Position("BTCUSDT", "long", 0.1, 60000, -2200.0, 1)
        exc = _make_exchange(equity=10000, positions=[pos])
        esl.register_exchange(exc, ["BTCUSDT"])
        await esl.check_now()
        exc.close_position.assert_called_once()
        assert any("CRITICAL" in a for a in alerts)
        assert not ks.is_active()

    @pytest.mark.asyncio
    async def test_catastrophic_closes_and_kills(self, tmp_path):
        esl, ks, alerts = self._make_esl(tmp_path)
        # 32% loss → catastrophic
        pos = Position("BTCUSDT", "long", 0.1, 60000, -3200.0, 1)
        exc = _make_exchange(equity=10000, positions=[pos])
        esl.register_exchange(exc, ["BTCUSDT"])
        await esl.check_now()
        exc.close_position.assert_called_once()
        assert ks.is_active()
        assert any("CATASTROPHIC" in a for a in alerts)

    @pytest.mark.asyncio
    async def test_no_positions_no_check(self, tmp_path):
        esl, ks, alerts = self._make_esl(tmp_path)
        exc = _make_exchange(equity=10000, positions=[])
        esl.register_exchange(exc, ["BTCUSDT"])
        await esl.check_now()
        assert not alerts
        assert not ks.is_active()

    @pytest.mark.asyncio
    async def test_zero_equity_skipped(self, tmp_path):
        esl, ks, alerts = self._make_esl(tmp_path)
        pos = Position("BTCUSDT", "long", 0.1, 60000, -9999.0, 1)
        exc = _make_exchange(equity=0.0, positions=[pos])
        esl.register_exchange(exc, ["BTCUSDT"])
        await esl.check_now()
        assert not alerts


# ── StateBackup tests ─────────────────────────────────────────────────────────

class TestStateBackup:
    def test_save_creates_files_in_both_dirs(self, tmp_path):
        d1 = str(tmp_path / "backup1")
        d2 = str(tmp_path / "backup2")
        backup = StateBackup(d1, d2, max_files=5)
        backup.save(bots=[{"id": "1"}], trades=[{"ts": "now"}])
        assert len(os.listdir(d1)) == 1
        assert len(os.listdir(d2)) == 1

    def test_rotation_removes_old_files(self, tmp_path):
        d1 = str(tmp_path / "backup1")
        backup = StateBackup(d1, d1, max_files=3)
        for i in range(5):
            backup.save(bots=[], trades=[])
        files = [f for f in os.listdir(d1) if f.startswith("state_")]
        assert len(files) <= 3

    def test_latest_returns_most_recent(self, tmp_path):
        d1 = str(tmp_path / "backup1")
        backup = StateBackup(d1, d1, max_files=10)
        backup.save(bots=[{"id": "1"}], trades=[])
        backup.save(bots=[{"id": "2"}], trades=[])
        latest = backup.latest(d1)
        assert latest is not None
        # Most recent save had bot id "2"
        assert latest["bots"][0]["id"] == "2"

    def test_latest_when_empty_returns_none(self, tmp_path):
        d1 = str(tmp_path / "empty_dir")
        backup = StateBackup(d1, d1, max_files=10)
        assert backup.latest(d1) is None
