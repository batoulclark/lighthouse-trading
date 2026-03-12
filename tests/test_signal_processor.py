"""
Tests for SignalProcessor — validation, deduplication, rate limiting.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.models.bot import Bot, BotStore
from app.models.signal import Signal
from app.services.signal_processor import SignalProcessor, SignalValidationError


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def bot() -> Bot:
    return Bot.create(
        name="Test Bot",
        exchange="hyperliquid",
        pair="BTCUSDT",
        leverage=1,
        webhook_secret="my-secret-token",
    )


@pytest.fixture()
def store(bot, tmp_path) -> BotStore:
    s = BotStore(str(tmp_path / "bots.json"))
    s.add(bot)
    return s


@pytest.fixture()
def processor(store) -> SignalProcessor:
    return SignalProcessor(store)


@pytest.fixture()
def allowed_ips():
    return ["1.2.3.4", "5.6.7.8"]


def _payload(
    secret: str = "my-secret-token",
    ticker: str = "BTCUSDT",
    action: str = "buy",
    position_size: str = "1",
    schema: str = "2",
    ts: str | None = None,
) -> dict:
    return {
        "bot_id": secret,
        "ticker": ticker,
        "action": action,
        "order_size": "100%",
        "position_size": position_size,
        "timestamp": ts or datetime.now(timezone.utc).isoformat(),
        "schema": schema,
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSignalValidation:
    def test_valid_buy(self, processor, allowed_ips):
        sig, bot = processor.process(_payload(), "1.2.3.4", allowed_ips)
        assert sig.action == "buy"
        assert bot.exchange == "hyperliquid"

    def test_valid_sell(self, processor, allowed_ips):
        sig, bot = processor.process(
            _payload(action="sell", position_size="-1"), "1.2.3.4", allowed_ips
        )
        assert sig.action == "sell"

    def test_valid_close(self, processor, allowed_ips):
        sig, _ = processor.process(
            _payload(action="sell", position_size="0"), "1.2.3.4", allowed_ips
        )
        assert sig.is_close()

    def test_wrong_schema_raises(self, processor, allowed_ips):
        with pytest.raises(SignalValidationError) as exc_info:
            processor.process(_payload(schema="1"), "1.2.3.4", allowed_ips)
        assert exc_info.value.status_code == 400
        assert "schema" in str(exc_info.value).lower()

    def test_unknown_secret_raises(self, processor, allowed_ips):
        with pytest.raises(SignalValidationError) as exc_info:
            processor.process(_payload(secret="bad-secret"), "1.2.3.4", allowed_ips)
        assert exc_info.value.status_code == 401

    def test_wrong_ticker_raises(self, processor, allowed_ips):
        with pytest.raises(SignalValidationError) as exc_info:
            processor.process(_payload(ticker="ETHUSDT"), "1.2.3.4", allowed_ips)
        assert exc_info.value.status_code == 400

    def test_invalid_action_raises(self, processor, allowed_ips):
        payload = _payload()
        payload["action"] = "hold"
        with pytest.raises(SignalValidationError):
            processor.process(payload, "1.2.3.4", allowed_ips)

    def test_invalid_position_size_raises(self, processor, allowed_ips):
        payload = _payload()
        payload["position_size"] = "2"
        with pytest.raises(SignalValidationError):
            processor.process(payload, "1.2.3.4", allowed_ips)

    def test_ip_not_in_allowlist_raises(self, processor, allowed_ips):
        with pytest.raises(SignalValidationError) as exc_info:
            processor.process(_payload(), "9.9.9.9", allowed_ips)
        assert exc_info.value.status_code == 403

    def test_empty_allowed_ips_allows_all(self, processor):
        """Empty allowlist = no IP filtering."""
        sig, _ = processor.process(_payload(), "9.9.9.9", [])
        assert sig.action == "buy"

    def test_disabled_bot_raises(self, processor, store, bot, allowed_ips):
        bot.enabled = False
        store.update(bot)
        with pytest.raises(SignalValidationError) as exc_info:
            processor.process(_payload(), "1.2.3.4", allowed_ips)
        assert exc_info.value.status_code == 403


class TestDeduplication:
    def test_same_timestamp_rejected(self, processor, allowed_ips):
        ts = "2024-01-01T00:00:00+00:00"
        processor.process(_payload(ts=ts), "1.2.3.4", allowed_ips)
        # Reset rate limiter so dedup check is reached
        processor._last_signal_time.clear()
        with pytest.raises(SignalValidationError) as exc_info:
            processor.process(_payload(ts=ts), "1.2.3.4", allowed_ips)
        assert exc_info.value.status_code == 409

    def test_different_timestamps_accepted(self, processor, allowed_ips):
        ts1 = "2024-01-01T00:00:00+00:00"
        ts2 = "2024-01-01T00:00:01+00:00"
        sig1, _ = processor.process(_payload(ts=ts1), "1.2.3.4", allowed_ips)
        # Need to bypass rate limit
        processor._last_signal_time = {}
        sig2, _ = processor.process(_payload(ts=ts2), "1.2.3.4", allowed_ips)
        assert sig1.timestamp != sig2.timestamp


class TestRateLimit:
    def test_rapid_signals_rejected(self, processor, allowed_ips):
        """Two signals within 5s should fail (second is rate-limited)."""
        ts1 = "2024-06-01T00:00:00+00:00"
        ts2 = "2024-06-01T00:00:01+00:00"
        processor.process(_payload(ts=ts1), "1.2.3.4", allowed_ips)
        with pytest.raises(SignalValidationError) as exc_info:
            processor.process(_payload(ts=ts2), "1.2.3.4", allowed_ips)
        assert exc_info.value.status_code == 429

    def test_signal_after_rate_limit_window_accepted(self, processor, allowed_ips, bot):
        """Signal accepted after rate-limit window has passed."""
        ts1 = "2024-06-01T00:00:00+00:00"
        processor.process(_payload(ts=ts1), "1.2.3.4", allowed_ips)
        # Simulate 6 seconds passing
        processor._last_signal_time[bot.id] -= 10
        ts2 = "2024-06-01T00:00:10+00:00"
        sig, _ = processor.process(_payload(ts=ts2), "1.2.3.4", allowed_ips)
        assert sig.timestamp == ts2


class TestSignalModel:
    def test_size_fraction_percentage(self):
        sig = Signal(
            bot_id="x", ticker="BTCUSDT", action="buy",
            order_size="50%", position_size="1",
            timestamp="now", schema="2",
        )
        assert sig.size_fraction() == pytest.approx(0.5)

    def test_size_fraction_decimal(self):
        sig = Signal(
            bot_id="x", ticker="BTCUSDT", action="buy",
            order_size="0.5", position_size="1",
            timestamp="now", schema="2",
        )
        assert sig.size_fraction() == pytest.approx(0.5)

    def test_size_fraction_full(self):
        sig = Signal(
            bot_id="x", ticker="BTCUSDT", action="buy",
            order_size="100%", position_size="1",
            timestamp="now", schema="2",
        )
        assert sig.size_fraction() == pytest.approx(1.0)
