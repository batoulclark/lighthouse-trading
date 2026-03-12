"""
Tests for PerformanceTracker — P&L, drawdown, win rate, equity curve, Sharpe.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.services.performance import PerformanceTracker, _max_drawdown_pct, _sharpe_ratio


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _trade(
    *,
    bot_id: str = "bot-1",
    bot_name: str = "Bot One",
    pair: str = "BTCUSDT",
    action: str = "buy",
    pnl: float | None = None,
    days_ago: int = 0,
) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "timestamp":      ts,
        "bot_id":         bot_id,
        "bot_name":       bot_name,
        "exchange":       "hyperliquid",
        "pair":           pair,
        "action":         action,
        "order_size":     "100%",
        "position_size":  "1",
        "fill_price":     50000.0,
        "quantity":       0.01,
        "fees":           0.5,
        "pnl":            pnl,
        "execution_result": {},
        "signal_timestamp": ts,
        "error":          None,
    }


@pytest.fixture()
def trades_file(tmp_path) -> str:
    return str(tmp_path / "trades.json")


@pytest.fixture()
def tracker_empty(trades_file) -> PerformanceTracker:
    return PerformanceTracker(trades_file)


@pytest.fixture()
def trades_with_pnl(trades_file) -> tuple[PerformanceTracker, list[dict]]:
    trades = [
        _trade(pnl=200.0,  days_ago=5),
        _trade(pnl=-50.0,  days_ago=4),
        _trade(pnl=100.0,  days_ago=3),
        _trade(pnl=-25.0,  days_ago=2),
        _trade(pnl=75.0,   days_ago=1),
    ]
    with open(trades_file, "w") as f:
        json.dump(trades, f)
    return PerformanceTracker(trades_file), trades


# ── Empty state ───────────────────────────────────────────────────────────────

class TestEmptyState:
    def test_summary_no_file(self, tracker_empty):
        s = tracker_empty.get_summary()
        assert s["total_trades"] == 0
        assert s["total_pnl"] == 0.0
        assert s["win_rate"] == 0.0
        assert s["profit_factor"] == 0.0  # 0 when no trades (JSON-safe)

    def test_daily_pnl_empty(self, tracker_empty):
        assert tracker_empty.get_daily_pnl() == []

    def test_equity_curve_empty(self, tracker_empty):
        assert tracker_empty.get_equity_curve() == []

    def test_trade_stats_empty(self, tracker_empty):
        stats = tracker_empty.get_trade_stats()
        assert stats["by_pair"] == {}
        assert stats["by_bot"] == {}

    def test_summary_corrupt_file(self, trades_file):
        with open(trades_file, "w") as f:
            f.write("not json")
        tracker = PerformanceTracker(trades_file)
        s = tracker.get_summary()
        assert s["total_trades"] == 0


# ── Metrics calculation ───────────────────────────────────────────────────────

class TestMetrics:
    def test_total_pnl(self, trades_with_pnl):
        tracker, _ = trades_with_pnl
        s = tracker.get_summary()
        assert s["total_pnl"] == pytest.approx(300.0)  # 200-50+100-25+75

    def test_win_rate(self, trades_with_pnl):
        tracker, _ = trades_with_pnl
        s = tracker.get_summary()
        assert s["win_rate"] == pytest.approx(0.6)  # 3/5

    def test_total_trades(self, trades_with_pnl):
        tracker, _ = trades_with_pnl
        s = tracker.get_summary()
        assert s["total_trades"] == 5

    def test_winning_losing_counts(self, trades_with_pnl):
        tracker, _ = trades_with_pnl
        s = tracker.get_summary()
        assert s["winning_trades"] == 3
        assert s["losing_trades"] == 2

    def test_gross_profit_loss(self, trades_with_pnl):
        tracker, _ = trades_with_pnl
        s = tracker.get_summary()
        assert s["gross_profit"] == pytest.approx(375.0)   # 200+100+75
        assert s["gross_loss"]   == pytest.approx(75.0)    # 50+25

    def test_profit_factor(self, trades_with_pnl):
        tracker, _ = trades_with_pnl
        s = tracker.get_summary()
        assert s["profit_factor"] == pytest.approx(375.0 / 75.0)

    def test_avg_win_avg_loss(self, trades_with_pnl):
        tracker, _ = trades_with_pnl
        s = tracker.get_summary()
        assert s["avg_win"] == pytest.approx(375.0 / 3)
        assert s["avg_loss"] < 0  # losses are negative

    def test_max_drawdown_non_negative(self, trades_with_pnl):
        tracker, _ = trades_with_pnl
        s = tracker.get_summary()
        assert s["max_drawdown_pct"] >= 0.0

    def test_sharpe_ratio_returns_float(self, trades_with_pnl):
        tracker, _ = trades_with_pnl
        s = tracker.get_summary()
        assert isinstance(s["sharpe_ratio"], float)


# ── Trades with null pnl ──────────────────────────────────────────────────────

class TestNullPnl:
    def test_null_pnl_excluded(self, trades_file):
        trades = [
            _trade(pnl=100.0),
            _trade(pnl=None),    # error trade — no P&L
            _trade(pnl=-50.0),
        ]
        with open(trades_file, "w") as f:
            json.dump(trades, f)
        tracker = PerformanceTracker(trades_file)
        s = tracker.get_summary()
        assert s["total_trades"] == 2  # only closed trades counted
        assert s["total_pnl"] == pytest.approx(50.0)


# ── Daily P&L ─────────────────────────────────────────────────────────────────

class TestDailyPnl:
    def test_sorted_ascending(self, trades_with_pnl):
        tracker, _ = trades_with_pnl
        daily = tracker.get_daily_pnl()
        dates = [d["date"] for d in daily]
        assert dates == sorted(dates)

    def test_aggregation(self, trades_file):
        """Two trades on the same day should be aggregated."""
        today = datetime.now(timezone.utc).date().isoformat()
        trades = [
            _trade(pnl=100.0, days_ago=0),
            _trade(pnl=50.0,  days_ago=0),
        ]
        # Force same timestamp date
        for t in trades:
            t["timestamp"] = today + "T12:00:00+00:00"
        with open(trades_file, "w") as f:
            json.dump(trades, f)
        tracker = PerformanceTracker(trades_file)
        daily = tracker.get_daily_pnl()
        assert len(daily) == 1
        assert daily[0]["pnl"] == pytest.approx(150.0)


# ── Equity curve ──────────────────────────────────────────────────────────────

class TestEquityCurve:
    def test_equity_cumulates(self, trades_file):
        trades = [
            _trade(pnl=100.0, days_ago=3),
            _trade(pnl=-50.0, days_ago=2),
            _trade(pnl=75.0,  days_ago=1),
        ]
        with open(trades_file, "w") as f:
            json.dump(trades, f)
        tracker = PerformanceTracker(trades_file)
        curve = tracker.get_equity_curve()
        assert len(curve) == 3
        equities = [p["equity"] for p in curve]
        assert equities[0] == pytest.approx(100.0)
        assert equities[1] == pytest.approx(50.0)
        assert equities[2] == pytest.approx(125.0)

    def test_equity_has_date_key(self, trades_file):
        with open(trades_file, "w") as f:
            json.dump([_trade(pnl=10.0)], f)
        tracker = PerformanceTracker(trades_file)
        curve = tracker.get_equity_curve()
        assert "date" in curve[0]
        assert "equity" in curve[0]


# ── Trade stats ───────────────────────────────────────────────────────────────

class TestTradeStats:
    def test_by_pair(self, trades_file):
        trades = [
            _trade(pair="BTCUSDT", pnl=200.0),
            _trade(pair="ETHUSDT", pnl=-50.0),
            _trade(pair="BTCUSDT", pnl=100.0),
        ]
        with open(trades_file, "w") as f:
            json.dump(trades, f)
        tracker = PerformanceTracker(trades_file)
        stats = tracker.get_trade_stats()
        assert "BTCUSDT" in stats["by_pair"]
        assert stats["by_pair"]["BTCUSDT"]["trades"] == 2
        assert stats["by_pair"]["BTCUSDT"]["pnl"] == pytest.approx(300.0)

    def test_by_bot(self, trades_file):
        trades = [
            _trade(bot_id="bot-1", pnl=100.0),
            _trade(bot_id="bot-2", pnl=-20.0),
        ]
        with open(trades_file, "w") as f:
            json.dump(trades, f)
        tracker = PerformanceTracker(trades_file)
        stats = tracker.get_trade_stats()
        assert "bot-1" in stats["by_bot"]
        assert stats["by_bot"]["bot-1"]["trades"] == 1

    def test_bot_id_filter(self, trades_file):
        trades = [
            _trade(bot_id="bot-1", pnl=100.0),
            _trade(bot_id="bot-2", pnl=50.0),
        ]
        with open(trades_file, "w") as f:
            json.dump(trades, f)
        tracker = PerformanceTracker(trades_file)
        stats = tracker.get_trade_stats(bot_id="bot-1")
        assert "bot-2" not in stats["by_bot"]


# ── Standalone helpers ────────────────────────────────────────────────────────

class TestHelpers:
    def test_max_drawdown_empty(self):
        assert _max_drawdown_pct([]) == 0.0

    def test_max_drawdown_flat(self):
        curve = [("2024-01-01", 100.0), ("2024-01-02", 100.0)]
        assert _max_drawdown_pct(curve) == pytest.approx(0.0)

    def test_max_drawdown_simple(self):
        # peak=100, drops to 80 → 20% drawdown
        curve = [
            ("2024-01-01", 100.0),
            ("2024-01-02", 80.0),
            ("2024-01-03", 90.0),
        ]
        dd = _max_drawdown_pct(curve)
        assert dd == pytest.approx(20.0)

    def test_sharpe_empty(self):
        assert _sharpe_ratio([]) == 0.0

    def test_sharpe_returns_float(self):
        curve = [("2024-01-0" + str(i), float(100 + i * 10)) for i in range(1, 8)]
        s = _sharpe_ratio(curve)
        assert isinstance(s, float)
