"""
Tests for TelegramCommandHandler — command parsing, security, response formatting.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest

from app.notifications.telegram_commands import TelegramCommandHandler
from app.services.position_manager import Position, PositionManager


# ── Fixtures ──────────────────────────────────────────────────────────────────

ALLOWED_CHAT_ID = "7422563444"
OTHER_CHAT_ID = "9999999999"


def make_handler(tmp_path) -> TelegramCommandHandler:
    """Build a TelegramCommandHandler with mocked dependencies."""
    kill_switch = MagicMock()
    kill_switch.is_active.return_value = False

    bot_store = MagicMock()
    bot_store.all.return_value = []

    trade_log = MagicMock()
    trade_log.all.return_value = []

    position_manager = MagicMock(spec=PositionManager)
    position_manager.get_all_positions.return_value = []
    position_manager.get_total_exposure.return_value = 0.0
    position_manager.get_position.return_value = None

    handler = TelegramCommandHandler(
        bot_token="fake-token",
        allowed_chat_id=ALLOWED_CHAT_ID,
        kill_switch=kill_switch,
        bot_store=bot_store,
        trade_log=trade_log,
        position_manager=position_manager,
    )
    return handler


def _make_update(text: str, chat_id: str = ALLOWED_CHAT_ID, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "chat": {"id": int(chat_id), "type": "private"},
            "from": {"id": int(chat_id)},
        },
    }


# ── Security ──────────────────────────────────────────────────────────────────

class TestSecurity:
    def test_ignores_unauthorized_chat_id(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append((chat_id, text))
        handler._handle_update(_make_update("/status", chat_id=OTHER_CHAT_ID))
        assert replies == [], "Should not reply to unauthorized chat_id"

    def test_responds_to_authorized_chat_id(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append((chat_id, text))
        handler._handle_update(_make_update("/help", chat_id=ALLOWED_CHAT_ID))
        assert len(replies) == 1
        assert replies[0][0] == ALLOWED_CHAT_ID

    def test_ignores_empty_message(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append((chat_id, text))
        handler._handle_update(_make_update("", chat_id=ALLOWED_CHAT_ID))
        assert replies == []

    def test_ignores_update_without_message(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append((chat_id, text))
        handler._handle_update({"update_id": 1})  # no message key
        assert replies == []


# ── Command parsing ───────────────────────────────────────────────────────────

class TestCommandParsing:
    def test_help_command(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/help"))
        assert len(replies) == 1
        assert "/status" in replies[0]
        assert "/kill" in replies[0]

    def test_unknown_command(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/foobar"))
        assert len(replies) == 1
        assert "Unknown command" in replies[0] or "help" in replies[0].lower()

    def test_status_command(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/status"))
        assert len(replies) == 1
        assert "Status" in replies[0] or "status" in replies[0].lower() or "Kill" in replies[0]

    def test_pnl_command(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/pnl"))
        assert len(replies) == 1
        assert "P&L" in replies[0] or "pnl" in replies[0].lower() or "$" in replies[0]

    def test_trades_command_default(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/trades"))
        assert len(replies) == 1
        assert "trade" in replies[0].lower() or "No trades" in replies[0]

    def test_trades_command_with_n(self, tmp_path):
        handler = make_handler(tmp_path)
        # Populate some trades
        handler._trade_log.all.return_value = [
            {"action": "buy", "pair": "BTCUSDT", "fill_price": 65000.0, "pnl": 100.0, "error": None}
            for _ in range(10)
        ]
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/trades 3"))
        assert len(replies) == 1

    def test_bot_command_missing_name(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/bot"))
        assert len(replies) == 1
        assert "Usage" in replies[0] or "usage" in replies[0].lower()

    def test_bot_command_not_found(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/bot nonexistent"))
        assert len(replies) == 1
        assert "not found" in replies[0].lower()


# ── Kill switch flow ──────────────────────────────────────────────────────────

class TestKillSwitchFlow:
    def test_kill_sends_confirmation_request(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/kill"))
        assert len(replies) == 1
        assert "CONFIRM" in replies[0]

    def test_kill_confirmed_activates_kill_switch(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/kill"))
        handler._handle_update(_make_update("CONFIRM"))
        handler._kill_switch.activate.assert_called_once()
        assert any("ACTIVATED" in r or "activated" in r.lower() for r in replies)

    def test_kill_wrong_confirmation_cancels(self, tmp_path):
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/kill"))
        handler._handle_update(_make_update("no"))
        handler._kill_switch.activate.assert_not_called()
        assert any("cancel" in r.lower() for r in replies)

    def test_kill_already_active(self, tmp_path):
        handler = make_handler(tmp_path)
        handler._kill_switch.is_active.return_value = True
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/kill"))
        assert len(replies) == 1
        handler._kill_switch.activate.assert_not_called()

    def test_unkill_deactivates(self, tmp_path):
        handler = make_handler(tmp_path)
        handler._kill_switch.is_active.return_value = True
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/unkill"))
        handler._kill_switch.deactivate.assert_called_once()

    def test_unkill_when_not_active(self, tmp_path):
        handler = make_handler(tmp_path)
        handler._kill_switch.is_active.return_value = False
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/unkill"))
        handler._kill_switch.deactivate.assert_not_called()


# ── Response formatting ───────────────────────────────────────────────────────

class TestResponseFormatting:
    def test_status_includes_kill_switch_state(self, tmp_path):
        handler = make_handler(tmp_path)
        handler._kill_switch.is_active.return_value = True
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/status"))
        assert "ACTIVE" in replies[0] or "active" in replies[0].lower()

    def test_status_includes_bot_names(self, tmp_path):
        handler = make_handler(tmp_path)
        mock_bot = MagicMock()
        mock_bot.id = "bot-1"
        mock_bot.name = "MyTestBot"
        mock_bot.exchange = "hyperliquid"
        mock_bot.pair = "BTCUSDT"
        mock_bot.enabled = True
        handler._bot_store.all.return_value = [mock_bot]
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/status"))
        assert "MyTestBot" in replies[0]

    def test_status_includes_position_info(self, tmp_path):
        handler = make_handler(tmp_path)
        mock_bot = MagicMock()
        mock_bot.id = "bot-1"
        mock_bot.name = "TestBot"
        mock_bot.exchange = "hyperliquid"
        mock_bot.pair = "BTCUSDT"
        mock_bot.enabled = True
        handler._bot_store.all.return_value = [mock_bot]

        mock_pos = MagicMock(spec=Position)
        mock_pos.bot_id = "bot-1"
        mock_pos.side = "long"
        mock_pos.size = 0.01
        mock_pos.entry_price = 65000.0
        mock_pos.current_price = 65500.0
        mock_pos.unrealized_pnl = 25.0
        handler._positions.get_all_positions.return_value = [mock_pos]
        handler._positions.get_total_exposure.return_value = 655.0

        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/status"))
        assert "LONG" in replies[0] or "long" in replies[0].lower()

    def test_command_with_bot_username_suffix(self, tmp_path):
        """Commands like /help@MyBotName should work."""
        handler = make_handler(tmp_path)
        replies = []
        handler._reply = lambda chat_id, text: replies.append(text)
        handler._handle_update(_make_update("/help@LighthouseBot"))
        assert len(replies) == 1
        assert "/status" in replies[0]


# ── Disabled handler ──────────────────────────────────────────────────────────

class TestDisabledHandler:
    def test_start_does_not_start_thread_when_disabled(self, tmp_path):
        handler = TelegramCommandHandler(
            bot_token="",  # empty → disabled
            allowed_chat_id=ALLOWED_CHAT_ID,
            kill_switch=MagicMock(),
            bot_store=MagicMock(),
            trade_log=MagicMock(),
            position_manager=MagicMock(),
        )
        handler.start()
        assert handler._thread is None

    def test_enabled_flag_true_when_configured(self, tmp_path):
        handler = make_handler(tmp_path)
        assert handler._enabled is True

    def test_enabled_flag_false_when_no_token(self, tmp_path):
        handler = TelegramCommandHandler(
            bot_token="",
            allowed_chat_id=ALLOWED_CHAT_ID,
            kill_switch=MagicMock(),
            bot_store=MagicMock(),
            trade_log=MagicMock(),
            position_manager=MagicMock(),
        )
        assert handler._enabled is False
