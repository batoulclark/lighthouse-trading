"""
Tests for OrderExecutor — order routing, size resolution, error handling.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.exchanges.base import Balance, OrderResult, Position
from app.models.bot import Bot
from app.models.signal import Signal
from app.models.trade import TradeLog
from app.notifications.telegram import TelegramNotifier
from app.safety.kill_switch import KillSwitch
from app.services.order_executor import OrderExecutor


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sample_bot() -> Bot:
    return Bot.create(
        name="Executor Test Bot",
        exchange="hyperliquid",
        pair="BTCUSDT",
        leverage=5,
        webhook_secret="exec-secret",
    )


@pytest.fixture()
def kill_switch(tmp_path) -> KillSwitch:
    return KillSwitch(str(tmp_path / "KILL_SWITCH"))


@pytest.fixture()
def trade_log(tmp_path) -> TradeLog:
    return TradeLog(str(tmp_path / "trades.json"))


@pytest.fixture()
def telegram() -> TelegramNotifier:
    t = MagicMock(spec=TelegramNotifier)
    t.send_trade_alert = AsyncMock(return_value=True)
    t.send_error = AsyncMock(return_value=True)
    return t


@pytest.fixture()
def mock_exchange() -> MagicMock:
    exc = MagicMock()
    exc.name = "hyperliquid"
    exc.set_leverage = AsyncMock()
    exc.get_balance = AsyncMock(
        return_value=Balance(asset="USDT", total=10000.0, available=8000.0)
    )
    exc.get_price = AsyncMock(return_value=65000.0)
    exc.market_buy = AsyncMock(
        return_value=OrderResult(
            exchange="hyperliquid",
            symbol="BTCUSDT",
            side="buy",
            size=0.001,
            fill_price=65000.0,
            order_id="ord-1",
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
            order_id="ord-2",
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
            order_id="ord-3",
            fees=None,
            raw={"status": "ok"},
        )
    )
    return exc


@pytest.fixture()
def executor(mock_exchange, kill_switch, trade_log, telegram) -> OrderExecutor:
    return OrderExecutor(
        exchanges={"hyperliquid": mock_exchange},
        kill_switch=kill_switch,
        trade_log=trade_log,
        telegram=telegram,
    )


def _signal(
    action: str = "buy",
    position_size: str = "1",
    order_size: str = "100%",
) -> Signal:
    return Signal(
        bot_id="exec-secret",
        ticker="BTCUSDT",
        action=action,
        order_size=order_size,
        position_size=position_size,
        timestamp="2024-01-01T00:00:00+00:00",
        schema="2",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestOrderExecutor:
    @pytest.mark.asyncio
    async def test_buy_signal_calls_market_buy(
        self, executor, sample_bot, mock_exchange
    ):
        trade = await executor.execute(_signal("buy", "1"), sample_bot)
        mock_exchange.market_buy.assert_called_once()
        assert trade.error is None
        assert trade.fill_price == 65000.0

    @pytest.mark.asyncio
    async def test_sell_signal_calls_market_sell(
        self, executor, sample_bot, mock_exchange
    ):
        trade = await executor.execute(_signal("sell", "-1"), sample_bot)
        mock_exchange.market_sell.assert_called_once()
        assert trade.error is None

    @pytest.mark.asyncio
    async def test_close_signal_calls_close_position(
        self, executor, sample_bot, mock_exchange
    ):
        trade = await executor.execute(_signal("sell", "0"), sample_bot)
        mock_exchange.close_position.assert_called_once()
        assert trade.error is None

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_order(
        self, executor, sample_bot, kill_switch, mock_exchange
    ):
        kill_switch.activate("test")
        trade = await executor.execute(_signal(), sample_bot)
        mock_exchange.market_buy.assert_not_called()
        assert trade.error is not None
        assert "kill switch" in trade.error.lower()
        kill_switch.deactivate()

    @pytest.mark.asyncio
    async def test_unknown_exchange_records_error(
        self, executor, sample_bot
    ):
        bot = Bot.create("x", "unknown_exchange", "BTCUSDT", 1, "s")
        trade = await executor.execute(_signal(), bot)
        assert trade.error is not None

    @pytest.mark.asyncio
    async def test_trade_logged_on_success(
        self, executor, sample_bot, trade_log
    ):
        await executor.execute(_signal(), sample_bot)
        trades = trade_log.all()
        assert len(trades) == 1
        assert trades[0]["action"] == "buy"

    @pytest.mark.asyncio
    async def test_trade_logged_on_error(
        self, executor, sample_bot, trade_log, kill_switch
    ):
        kill_switch.activate("test")
        await executor.execute(_signal(), sample_bot)
        trades = trade_log.all()
        assert len(trades) == 1
        assert trades[0]["error"] is not None
        kill_switch.deactivate()

    @pytest.mark.asyncio
    async def test_size_resolution_percentage(
        self, executor, sample_bot, mock_exchange
    ):
        """100% of 8000 USDT available at 65000 * 5x leverage = ~0.615 BTC"""
        trade = await executor.execute(_signal("buy", "1", "100%"), sample_bot)
        call_args = mock_exchange.market_buy.call_args
        size = call_args[0][1] if call_args[0] else call_args[1]["size"]
        # 8000 * 1.0 * 5 / 65000 ≈ 0.615
        assert size == pytest.approx(0.615384, rel=1e-3)

    @pytest.mark.asyncio
    async def test_size_resolution_fixed(
        self, executor, sample_bot, mock_exchange
    ):
        trade = await executor.execute(_signal("buy", "1", "0.05"), sample_bot)
        call_args = mock_exchange.market_buy.call_args
        size = call_args[0][1] if call_args[0] else call_args[1]["size"]
        assert size == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_leverage_set_before_order(
        self, executor, sample_bot, mock_exchange
    ):
        await executor.execute(_signal(), sample_bot)
        mock_exchange.set_leverage.assert_called_once_with("BTCUSDT", 5)

    @pytest.mark.asyncio
    async def test_telegram_alert_sent_on_success(
        self, executor, sample_bot, telegram
    ):
        await executor.execute(_signal(), sample_bot)
        telegram.send_trade_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_telegram_error_sent_on_failure(
        self, executor, sample_bot, telegram, kill_switch
    ):
        kill_switch.activate("test")
        await executor.execute(_signal(), sample_bot)
        telegram.send_error.assert_called()
        kill_switch.deactivate()

    @pytest.mark.asyncio
    async def test_exchange_exception_records_error(
        self, executor, sample_bot, mock_exchange, trade_log
    ):
        mock_exchange.market_buy.side_effect = Exception("Connection refused")
        trade = await executor.execute(_signal(), sample_bot)
        assert trade.error is not None
        assert "Connection refused" in trade.error
