"""
Lighthouse Trading - Webhook Receiver
POST /webhook/{bot_id} — receives TradingView alerts and routes them for execution.

The {bot_id} path parameter is the bot's UUID (used for quick lookup).
The payload's bot_id field must match the bot's webhook_secret.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.services.signal_processor import SignalProcessor, SignalValidationError
from config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """Extract real client IP, honouring X-Forwarded-For when behind a proxy."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ── Endpoint ─────────────────────────────────────────────────────────────────

@router.post("/webhook/{bot_id}")
async def receive_webhook(
    bot_id: str,
    request: Request,
) -> JSONResponse:
    """
    Receive a TradingView / Signum v2 webhook signal.

    Path param `bot_id` is the bot's UUID (for routing).
    The JSON payload must include `bot_id` set to the bot's webhook_secret
    for authentication.
    """
    # ── Parse JSON ────────────────────────────────────────────────────────
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    client_ip = _client_ip(request)
    logger.debug("Webhook from %s for bot %s: %s", client_ip, bot_id, payload)

    # ── Inject bot_id into payload if not present (convenience) ──────────
    if "bot_id" not in payload:
        payload["bot_id"] = bot_id

    # ── Validate + parse signal ───────────────────────────────────────────
    processor: SignalProcessor = request.app.state.signal_processor

    try:
        signal, bot = processor.process(
            payload=payload,
            client_ip=client_ip,
            allowed_ips=settings.allowed_ips,
        )
    except SignalValidationError as exc:
        logger.warning("Signal validation failed (IP=%s): %s", client_ip, exc.reason)
        raise HTTPException(status_code=exc.status_code, detail=exc.reason)

    # ── Execute order ─────────────────────────────────────────────────────
    executor = request.app.state.order_executor
    trade = await executor.execute(signal, bot)

    if trade.error:
        return JSONResponse(
            {
                "status": "error",
                "bot_id": bot.id,
                "bot_name": bot.name,
                "error": trade.error,
            },
            status_code=422,
        )

    return JSONResponse(
        {
            "status": "ok",
            "bot_id": bot.id,
            "bot_name": bot.name,
            "action": signal.action,
            "pair": bot.pair,
            "fill_price": trade.fill_price,
            "quantity": trade.quantity,
            "timestamp": trade.timestamp,
        }
    )
