"""
Lighthouse Trading - Hyperliquid Exchange Connector
Uses the official hyperliquid-python-sdk.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants as hl_constants

from app.exchanges.base import Balance, BaseExchange, OrderResult, Position

logger = logging.getLogger(__name__)


class HyperliquidExchange(BaseExchange):
    """
    Connector for Hyperliquid perps.

    The SDK's Exchange and Info classes are synchronous; we run them in a
    thread pool via asyncio to keep the FastAPI event loop unblocked.
    """

    name = "hyperliquid"

    def __init__(
        self,
        private_key: str,
        account_address: str,
        testnet: bool = True,
    ) -> None:
        self.account_address = account_address
        self.testnet = testnet

        base_url = (
            hl_constants.TESTNET_API_URL if testnet else hl_constants.MAINNET_API_URL
        )

        self._wallet = eth_account.Account.from_key(private_key)
        self._info = Info(base_url, skip_ws=True)
        self._exchange = Exchange(
            self._wallet,
            base_url,
            account_address=account_address,
        )

        logger.info(
            "HyperliquidExchange initialised — %s, account=%s",
            "testnet" if testnet else "mainnet",
            account_address,
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _coin(self, symbol: str) -> str:
        """Strip quote suffix to get the coin name Hyperliquid expects."""
        for suffix in ("USDT", "USDC", "USD"):
            if symbol.upper().endswith(suffix):
                return symbol.upper()[: -len(suffix)]
        return symbol.upper()

    def _parse_fill(self, response: Dict[str, Any]) -> Optional[float]:
        """Extract average fill price from an order response."""
        try:
            statuses = response.get("response", {}).get("data", {}).get("statuses", [])
            if statuses:
                filled = statuses[0].get("filled", {})
                avg_px = filled.get("avgPx")
                if avg_px:
                    return float(avg_px)
        except Exception:
            pass
        return None

    # ── Core trading operations ──────────────────────────────────────────────

    async def market_buy(self, symbol: str, size: float) -> OrderResult:
        coin = self._coin(symbol)
        logger.info("HL market_buy %s size=%.6f", coin, size)
        result = self._exchange.market_open(coin, is_buy=True, sz=size, slippage=0.05)
        fill_price = self._parse_fill(result)
        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side="buy",
            size=size,
            fill_price=fill_price,
            order_id=str(result),
            fees=None,
            raw=result,
        )

    async def market_sell(self, symbol: str, size: float) -> OrderResult:
        coin = self._coin(symbol)
        logger.info("HL market_sell %s size=%.6f", coin, size)
        result = self._exchange.market_open(coin, is_buy=False, sz=size, slippage=0.05)
        fill_price = self._parse_fill(result)
        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side="sell",
            size=size,
            fill_price=fill_price,
            order_id=str(result),
            fees=None,
            raw=result,
        )

    async def close_position(self, symbol: str) -> OrderResult:
        coin = self._coin(symbol)
        position = await self.get_position(symbol)
        if position is None or position.side == "none" or position.size == 0:
            return OrderResult(
                exchange=self.name,
                symbol=symbol,
                side="close",
                size=0.0,
                fill_price=None,
                order_id="no_position",
                fees=None,
                raw={"info": "no open position"},
            )

        is_buy = position.side == "short"  # buy to close short, sell to close long
        result = self._exchange.market_close(
            coin, sz=None, slippage=0.05  # sz=None → close full position
        )
        fill_price = self._parse_fill(result)
        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side="close",
            size=position.size,
            fill_price=fill_price,
            order_id=str(result),
            fees=None,
            raw=result,
        )

    # ── Account queries ──────────────────────────────────────────────────────

    async def get_position(self, symbol: str) -> Optional[Position]:
        coin = self._coin(symbol)
        try:
            user_state = self._info.user_state(self.account_address)
            positions = user_state.get("assetPositions", [])
            for pos_entry in positions:
                pos = pos_entry.get("position", {})
                if pos.get("coin") == coin:
                    szi = float(pos.get("szi", 0))
                    if szi == 0:
                        return None
                    entry_px = float(pos.get("entryPx", 0) or 0)
                    upnl = float(pos.get("unrealizedPnl", 0) or 0)
                    leverage = int(pos.get("leverage", {}).get("value", 1) or 1)
                    return Position(
                        symbol=symbol,
                        side="long" if szi > 0 else "short",
                        size=abs(szi),
                        entry_price=entry_px,
                        unrealized_pnl=upnl,
                        leverage=leverage,
                    )
        except Exception as exc:
            logger.error("get_position error: %s", exc)
        return None

    async def get_balance(self, asset: str = "USDT") -> Balance:
        try:
            user_state = self._info.user_state(self.account_address)
            margin_summary = user_state.get("marginSummary", {})
            total = float(margin_summary.get("accountValue", 0))
            withdrawable = float(user_state.get("withdrawable", total))
            return Balance(asset=asset, total=total, available=withdrawable)
        except Exception as exc:
            logger.error("get_balance error: %s", exc)
            return Balance(asset=asset, total=0.0, available=0.0)

    async def get_equity(self) -> float:
        bal = await self.get_balance()
        return bal.total

    # ── Configuration ────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        coin = self._coin(symbol)
        logger.info("HL set_leverage %s x%d", coin, leverage)
        self._exchange.update_leverage(leverage, coin, is_cross=True)

    # ── Utilities ────────────────────────────────────────────────────────────

    async def get_price(self, symbol: str) -> float:
        coin = self._coin(symbol)
        try:
            mids = self._info.all_mids()
            return float(mids.get(coin, 0))
        except Exception as exc:
            logger.error("get_price error: %s", exc)
            return 0.0

    def symbol_for_pair(self, pair: str) -> str:
        return pair.upper()
