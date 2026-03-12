"""
Lighthouse Trading - Binance Futures Exchange Connector
Uses python-binance (binance.client.Client).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, FUTURE_ORDER_TYPE_MARKET

from app.exchanges.base import Balance, BaseExchange, OrderResult, Position

logger = logging.getLogger(__name__)


class BinanceExchange(BaseExchange):
    """Connector for Binance USDT-M Perpetual Futures."""

    name = "binance"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._client = Client(api_key, api_secret)
        logger.info("BinanceExchange initialised")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _fmt(self, symbol: str) -> str:
        return symbol.upper().replace("-", "").replace("/", "")

    def _parse_order(self, raw: Dict[str, Any], side: str, symbol: str) -> OrderResult:
        fills = raw.get("fills", [])
        fill_price: Optional[float] = None
        fees: Optional[float] = None
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            if total_qty > 0:
                fill_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty
                fees = sum(float(f.get("commission", 0)) for f in fills)
        elif raw.get("avgPrice"):
            fill_price = float(raw["avgPrice"])

        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side=side,
            size=float(raw.get("executedQty", raw.get("origQty", 0))),
            fill_price=fill_price,
            order_id=str(raw.get("orderId", "")),
            fees=fees,
            raw=raw,
        )

    # ── Core trading operations ──────────────────────────────────────────────

    async def market_buy(self, symbol: str, size: float) -> OrderResult:
        sym = self._fmt(symbol)
        logger.info("Binance market_buy %s qty=%.6f", sym, size)
        raw = self._client.futures_create_order(
            symbol=sym,
            side=SIDE_BUY,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=size,
        )
        return self._parse_order(raw, "buy", symbol)

    async def market_sell(self, symbol: str, size: float) -> OrderResult:
        sym = self._fmt(symbol)
        logger.info("Binance market_sell %s qty=%.6f", sym, size)
        raw = self._client.futures_create_order(
            symbol=sym,
            side=SIDE_SELL,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=size,
        )
        return self._parse_order(raw, "sell", symbol)

    async def close_position(self, symbol: str) -> OrderResult:
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

        sym = self._fmt(symbol)
        side = SIDE_SELL if position.side == "long" else SIDE_BUY
        raw = self._client.futures_create_order(
            symbol=sym,
            side=side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=position.size,
            reduceOnly=True,
        )
        return self._parse_order(raw, "close", symbol)

    # ── Account queries ──────────────────────────────────────────────────────

    async def get_position(self, symbol: str) -> Optional[Position]:
        sym = self._fmt(symbol)
        try:
            positions = self._client.futures_position_information(symbol=sym)
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                if amt == 0:
                    continue
                return Position(
                    symbol=symbol,
                    side="long" if amt > 0 else "short",
                    size=abs(amt),
                    entry_price=float(p.get("entryPrice", 0)),
                    unrealized_pnl=float(p.get("unRealizedProfit", 0)),
                    leverage=int(p.get("leverage", 1)),
                )
        except Exception as exc:
            logger.error("get_position error: %s", exc)
        return None

    async def get_balance(self, asset: str = "USDT") -> Balance:
        try:
            balances = self._client.futures_account_balance()
            for b in balances:
                if b.get("asset") == asset:
                    return Balance(
                        asset=asset,
                        total=float(b.get("balance", 0)),
                        available=float(b.get("availableBalance", 0)),
                    )
        except Exception as exc:
            logger.error("get_balance error: %s", exc)
        return Balance(asset=asset, total=0.0, available=0.0)

    async def get_equity(self) -> float:
        try:
            account = self._client.futures_account()
            return float(account.get("totalWalletBalance", 0))
        except Exception as exc:
            logger.error("get_equity error: %s", exc)
            return 0.0

    # ── Configuration ────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        sym = self._fmt(symbol)
        logger.info("Binance set_leverage %s x%d", sym, leverage)
        self._client.futures_change_leverage(symbol=sym, leverage=leverage)

    # ── Utilities ────────────────────────────────────────────────────────────

    async def get_price(self, symbol: str) -> float:
        sym = self._fmt(symbol)
        try:
            ticker = self._client.futures_symbol_ticker(symbol=sym)
            return float(ticker.get("price", 0))
        except Exception as exc:
            logger.error("get_price error: %s", exc)
            return 0.0

    def symbol_for_pair(self, pair: str) -> str:
        return self._fmt(pair)
