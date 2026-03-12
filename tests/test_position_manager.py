"""
Tests for PositionManager — open, close, sync, exposure calc, thread safety.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.position_manager import ClosedPosition, Position, PositionManager


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def pm(tmp_path) -> PositionManager:
    return PositionManager(path=str(tmp_path / "positions.json"))


# ── Open position ─────────────────────────────────────────────────────────────

class TestOpenPosition:
    def test_returns_position(self, pm):
        pos = pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        assert isinstance(pos, Position)
        assert pos.bot_id == "bot-1"
        assert pos.symbol == "BTCUSDT"
        assert pos.side == "long"
        assert pos.size == 0.01
        assert pos.entry_price == 65000.0
        assert pos.current_price == 65000.0
        assert pos.leverage == 5
        assert pos.unrealized_pnl == 0.0
        assert pos.opened_at

    def test_persists_to_file(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        retrieved = pm.get_position("bot-1")
        assert retrieved is not None
        assert retrieved.bot_id == "bot-1"

    def test_overwrites_existing_position(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        pm.open_position("bot-1", "ETHUSDT", "short", 0.5, 3000.0, 10)
        pos = pm.get_position("bot-1")
        assert pos.symbol == "ETHUSDT"
        assert pos.side == "short"

    def test_multiple_bots(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        pm.open_position("bot-2", "ETHUSDT", "short", 0.5, 3000.0, 3)
        positions = pm.get_all_positions()
        assert len(positions) == 2


# ── Close position ────────────────────────────────────────────────────────────

class TestClosePosition:
    def test_returns_closed_position(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        closed = pm.close_position("bot-1", "BTCUSDT", 66000.0)
        assert isinstance(closed, ClosedPosition)
        assert closed.bot_id == "bot-1"
        assert closed.exit_price == 66000.0

    def test_pnl_long(self, pm):
        # Long: 0.01 BTC, entry 65000, exit 66000, 5x leverage
        # PnL = (66000 - 65000) * 0.01 * 5 = 50.0
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        closed = pm.close_position("bot-1", "BTCUSDT", 66000.0)
        assert closed.realized_pnl == pytest.approx(50.0)

    def test_pnl_short(self, pm):
        # Short: 0.01 BTC, entry 65000, exit 64000, 5x leverage
        # PnL = (65000 - 64000) * 0.01 * 5 = 50.0
        pm.open_position("bot-1", "BTCUSDT", "short", 0.01, 65000.0, 5)
        closed = pm.close_position("bot-1", "BTCUSDT", 64000.0)
        assert closed.realized_pnl == pytest.approx(50.0)

    def test_pnl_loss(self, pm):
        # Long: 0.01 BTC, entry 65000, exit 64000, 5x leverage
        # PnL = (64000 - 65000) * 0.01 * 5 = -50.0
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        closed = pm.close_position("bot-1", "BTCUSDT", 64000.0)
        assert closed.realized_pnl == pytest.approx(-50.0)

    def test_removes_position(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        pm.close_position("bot-1", "BTCUSDT", 66000.0)
        assert pm.get_position("bot-1") is None

    def test_returns_none_when_not_found(self, pm):
        result = pm.close_position("nonexistent", "BTCUSDT", 65000.0)
        assert result is None

    def test_hold_duration_positive(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        closed = pm.close_position("bot-1", "BTCUSDT", 66000.0)
        assert closed.hold_duration >= 0

    def test_closed_at_is_set(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        closed = pm.close_position("bot-1", "BTCUSDT", 66000.0)
        assert closed.closed_at
        assert closed.opened_at


# ── Get positions ─────────────────────────────────────────────────────────────

class TestGetPositions:
    def test_get_position_not_found(self, pm):
        assert pm.get_position("ghost") is None

    def test_get_all_empty(self, pm):
        assert pm.get_all_positions() == []

    def test_get_all_returns_list(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        pm.open_position("bot-2", "ETHUSDT", "short", 1.0, 3000.0, 3)
        positions = pm.get_all_positions()
        assert len(positions) == 2
        assert all(isinstance(p, Position) for p in positions)


# ── Exposure calculation ──────────────────────────────────────────────────────

class TestExposure:
    def test_zero_when_no_positions(self, pm):
        assert pm.get_total_exposure() == 0.0

    def test_single_position(self, pm):
        # size=0.01, current_price=65000 → notional = 650.0
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        assert pm.get_total_exposure() == pytest.approx(650.0)

    def test_multiple_positions(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)   # 650
        pm.open_position("bot-2", "ETHUSDT", "short", 1.0, 3000.0, 3)   # 3000
        assert pm.get_total_exposure() == pytest.approx(3650.0)


# ── Update price ──────────────────────────────────────────────────────────────

class TestUpdatePrice:
    def test_updates_current_price(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        updated = pm.update_price("bot-1", 66000.0)
        assert updated.current_price == 66000.0

    def test_recalculates_unrealized_pnl_long(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "long", 0.01, 65000.0, 5)
        updated = pm.update_price("bot-1", 66000.0)
        # (66000 - 65000) * 0.01 * 5 = 50.0
        assert updated.unrealized_pnl == pytest.approx(50.0)

    def test_recalculates_unrealized_pnl_short(self, pm):
        pm.open_position("bot-1", "BTCUSDT", "short", 0.01, 65000.0, 5)
        updated = pm.update_price("bot-1", 64000.0)
        # (65000 - 64000) * 0.01 * 5 = 50.0
        assert updated.unrealized_pnl == pytest.approx(50.0)

    def test_returns_none_for_unknown_bot(self, pm):
        result = pm.update_price("ghost", 65000.0)
        assert result is None


# ── Thread safety ─────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_open_positions(self, pm):
        """Multiple threads opening positions concurrently should not corrupt data."""
        errors = []

        def open_pos(bot_id: str) -> None:
            try:
                pm.open_position(bot_id, "BTCUSDT", "long", 0.01, 65000.0, 5)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=open_pos, args=(f"bot-{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        positions = pm.get_all_positions()
        assert len(positions) == 10

    def test_concurrent_open_and_close(self, pm):
        """Open and close from different threads without corruption."""
        for i in range(5):
            pm.open_position(f"bot-{i}", "BTCUSDT", "long", 0.01, 65000.0, 5)

        errors = []

        def close_pos(bot_id: str) -> None:
            try:
                pm.close_position(bot_id, "BTCUSDT", 66000.0)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=close_pos, args=(f"bot-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        assert pm.get_all_positions() == []


# ── Sync from exchange ────────────────────────────────────────────────────────

class TestSyncFromExchange:
    @pytest.mark.asyncio
    async def test_sync_creates_position(self, pm):
        from app.exchanges.base import Position as ExchangePosition

        mock_exchange = MagicMock()
        mock_exchange.get_position = AsyncMock(
            return_value=ExchangePosition(
                symbol="BTCUSDT",
                side="long",
                size=0.05,
                entry_price=65000.0,
                unrealized_pnl=100.0,
                leverage=5,
            )
        )
        mock_bot = MagicMock()
        mock_bot.id = "bot-sync"
        mock_bot.pair = "BTCUSDT"
        mock_bot.leverage = 5

        pos = await pm.sync_from_exchange(mock_exchange, mock_bot)
        assert pos is not None
        assert pos.bot_id == "bot-sync"
        assert pos.side == "long"
        assert pos.size == 0.05
        assert pos.entry_price == 65000.0

    @pytest.mark.asyncio
    async def test_sync_no_position_returns_none(self, pm):
        mock_exchange = MagicMock()
        mock_exchange.get_position = AsyncMock(return_value=None)
        mock_bot = MagicMock()
        mock_bot.id = "bot-sync"
        mock_bot.pair = "BTCUSDT"
        mock_bot.leverage = 5

        pos = await pm.sync_from_exchange(mock_exchange, mock_bot)
        assert pos is None

    @pytest.mark.asyncio
    async def test_sync_clears_stale_local_position(self, pm):
        pm.open_position("bot-sync", "BTCUSDT", "long", 0.01, 65000.0, 5)

        mock_exchange = MagicMock()
        mock_exchange.get_position = AsyncMock(return_value=None)
        mock_bot = MagicMock()
        mock_bot.id = "bot-sync"
        mock_bot.pair = "BTCUSDT"
        mock_bot.leverage = 5

        await pm.sync_from_exchange(mock_exchange, mock_bot)
        assert pm.get_position("bot-sync") is None

    @pytest.mark.asyncio
    async def test_sync_exchange_error_returns_none(self, pm):
        mock_exchange = MagicMock()
        mock_exchange.get_position = AsyncMock(side_effect=Exception("Connection refused"))
        mock_bot = MagicMock()
        mock_bot.id = "bot-sync"
        mock_bot.pair = "BTCUSDT"
        mock_bot.leverage = 5

        pos = await pm.sync_from_exchange(mock_exchange, mock_bot)
        assert pos is None
