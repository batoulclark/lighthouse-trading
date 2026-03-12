"""
Tests for PaperExchange — paper trading connector.
"""

from __future__ import annotations

import json
import pytest

from app.exchanges.base import Balance, OrderResult, Position
from app.exchanges.paper import PaperExchange


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def exchange(tmp_path) -> PaperExchange:
    """Fresh PaperExchange with 10 000 USDT starting balance."""
    return PaperExchange(
        starting_balance=10_000.0,
        trades_file=str(tmp_path / "paper_trades.json"),
    )


# ── 1. Initial balance ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_initial_balance(exchange):
    bal = await exchange.get_balance()
    assert isinstance(bal, Balance)
    assert bal.total == 10_000.0
    assert bal.available == 10_000.0
    assert bal.asset == "USDT"


# ── 2. get_price stores and returns price ─────────────────────────────────────

@pytest.mark.asyncio
async def test_get_price_stores_value(exchange):
    price = await exchange.get_price("BTCUSDT", price=45_000.0)
    assert price == 45_000.0
    # Retrieve without providing price — should return stored value
    price2 = await exchange.get_price("BTCUSDT")
    assert price2 == 45_000.0


# ── 3. get_price unknown symbol returns 0 ────────────────────────────────────

@pytest.mark.asyncio
async def test_get_price_unknown_symbol(exchange):
    price = await exchange.get_price("UNKNOWN")
    assert price == 0.0


# ── 4. market_buy creates long position ──────────────────────────────────────

@pytest.mark.asyncio
async def test_market_buy_creates_long(exchange):
    result = await exchange.market_buy("BTCUSDT", size=0.1, price=50_000.0)

    assert isinstance(result, OrderResult)
    assert result.side == "buy"
    assert result.size == 0.1
    assert result.fill_price == 50_000.0
    assert result.exchange == "paper"

    pos = await exchange.get_position("BTCUSDT")
    assert pos is not None
    assert pos.side == "long"
    assert pos.size == 0.1
    assert pos.entry_price == 50_000.0


# ── 5. market_buy deducts cost from balance ───────────────────────────────────

@pytest.mark.asyncio
async def test_market_buy_deducts_balance(exchange):
    await exchange.market_buy("BTCUSDT", size=0.1, price=50_000.0)
    bal = await exchange.get_balance()
    assert bal.total == pytest.approx(10_000.0 - 0.1 * 50_000.0)


# ── 6. market_sell creates short position ────────────────────────────────────

@pytest.mark.asyncio
async def test_market_sell_creates_short(exchange):
    result = await exchange.market_sell("ETHUSDT", size=1.0, price=3_000.0)

    assert result.side == "sell"
    assert result.fill_price == 3_000.0

    pos = await exchange.get_position("ETHUSDT")
    assert pos is not None
    assert pos.side == "short"
    assert pos.size == 1.0


# ── 7. close_position long — profitable trade ─────────────────────────────────

@pytest.mark.asyncio
async def test_close_long_profitable(exchange):
    await exchange.market_buy("BTCUSDT", size=0.1, price=50_000.0)
    balance_before = (await exchange.get_balance()).total

    result = await exchange.close_position("BTCUSDT", price=55_000.0)

    assert result.side == "close"
    assert result.fill_price == 55_000.0
    assert result.raw["pnl"] == pytest.approx(500.0)   # (55k-50k)*0.1

    balance_after = (await exchange.get_balance()).total
    assert balance_after > balance_before
    # Net gain relative to starting capital
    assert balance_after == pytest.approx(10_000.0 + 500.0)


# ── 8. close_position short — profitable trade ───────────────────────────────

@pytest.mark.asyncio
async def test_close_short_profitable(exchange):
    await exchange.market_sell("ETHUSDT", size=2.0, price=3_000.0)
    result = await exchange.close_position("ETHUSDT", price=2_500.0)

    assert result.raw["pnl"] == pytest.approx(1_000.0)   # (3k-2.5k)*2
    bal = await exchange.get_balance()
    assert bal.total == pytest.approx(10_000.0 + 1_000.0)


# ── 9. close_position when no position open ──────────────────────────────────

@pytest.mark.asyncio
async def test_close_no_position(exchange):
    result = await exchange.close_position("BTCUSDT")
    assert result.order_id == "no_position"
    assert result.size == 0.0


# ── 10. close_position removes position ──────────────────────────────────────

@pytest.mark.asyncio
async def test_close_removes_position(exchange):
    await exchange.market_buy("BTCUSDT", size=0.5, price=40_000.0)
    await exchange.close_position("BTCUSDT", price=40_000.0)
    pos = await exchange.get_position("BTCUSDT")
    assert pos is None


# ── 11. set_leverage stores value without error ───────────────────────────────

@pytest.mark.asyncio
async def test_set_leverage(exchange):
    await exchange.set_leverage("BTCUSDT", 10)
    # No exception; leverage stored internally
    assert exchange._leverage.get("BTCUSDT") == 10


# ── 12. Leverage reflected in position ───────────────────────────────────────

@pytest.mark.asyncio
async def test_leverage_in_position(exchange):
    await exchange.set_leverage("BTCUSDT", 5)
    await exchange.market_buy("BTCUSDT", size=0.1, price=50_000.0)
    pos = await exchange.get_position("BTCUSDT")
    assert pos.leverage == 5


# ── 13. Trade log persisted to file ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_trade_log_persisted(exchange):
    await exchange.market_buy("BTCUSDT", size=0.1, price=50_000.0)
    await exchange.close_position("BTCUSDT", price=51_000.0)

    with open(exchange._trades_file) as f:
        trades = json.load(f)

    assert len(trades) == 2
    assert trades[0]["side"] == "buy"
    assert trades[1]["side"] == "close"
    assert "timestamp" in trades[0]
    assert "order_id" in trades[0]


# ── 14. get_equity returns balance ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_equity(exchange):
    equity = await exchange.get_equity()
    assert equity == 10_000.0


# ── 15. market_buy without price raises ValueError ───────────────────────────

@pytest.mark.asyncio
async def test_market_buy_no_price_raises(exchange):
    with pytest.raises(ValueError, match="no price available"):
        await exchange.market_buy("BTCUSDT", size=0.1)


# ── 16. unrealized_pnl computed correctly ────────────────────────────────────

@pytest.mark.asyncio
async def test_unrealized_pnl_long(exchange):
    await exchange.market_buy("BTCUSDT", size=1.0, price=40_000.0)
    # Update last known price
    await exchange.get_price("BTCUSDT", price=42_000.0)
    pos = await exchange.get_position("BTCUSDT")
    assert pos.unrealized_pnl == pytest.approx(2_000.0)


# ── 17. symbol_for_pair normalises to upper-case ─────────────────────────────

def test_symbol_for_pair(exchange):
    assert exchange.symbol_for_pair("btcusdt") == "BTCUSDT"
    assert exchange.symbol_for_pair("ETHusdt") == "ETHUSDT"


# ── 18. Multiple independent positions ───────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_positions(exchange):
    await exchange.market_buy("BTCUSDT", size=0.1, price=50_000.0)
    await exchange.market_sell("ETHUSDT", size=1.0, price=3_000.0)

    btc_pos = await exchange.get_position("BTCUSDT")
    eth_pos = await exchange.get_position("ETHUSDT")

    assert btc_pos is not None and btc_pos.side == "long"
    assert eth_pos is not None and eth_pos.side == "short"


# ── 19. Closing one position doesn't affect another ──────────────────────────

@pytest.mark.asyncio
async def test_close_one_leaves_other(exchange):
    await exchange.market_buy("BTCUSDT", size=0.1, price=50_000.0)
    await exchange.market_sell("ETHUSDT", size=1.0, price=3_000.0)
    await exchange.close_position("BTCUSDT", price=50_000.0)

    btc_pos = await exchange.get_position("BTCUSDT")
    eth_pos = await exchange.get_position("ETHUSDT")

    assert btc_pos is None
    assert eth_pos is not None


# ── 20. Order IDs are unique ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_ids_unique(exchange):
    r1 = await exchange.market_buy("BTCUSDT", size=0.1, price=50_000.0)
    await exchange.close_position("BTCUSDT", price=51_000.0)
    r2 = await exchange.market_buy("BTCUSDT", size=0.2, price=52_000.0)

    assert r1.order_id != r2.order_id
    assert r1.order_id.startswith("paper-")
