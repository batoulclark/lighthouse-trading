"""
Lighthouse Trading - Bot Management API
CRUD endpoints for managing trading bots.

Authentication: all write endpoints require the LIGHTHOUSE_API_KEY header.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.models.bot import Bot, BotStore
from config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bots", tags=["bots"])


# ── Auth dependency ──────────────────────────────────────────────────────────

def _require_api_key(request: Request) -> None:
    if not settings.api_key:
        return  # no key configured → open (not recommended for production)
    key = request.headers.get("X-API-Key") or request.headers.get("Authorization", "").removeprefix("Bearer ")
    if key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── Request/response schemas ─────────────────────────────────────────────────

class CreateBotRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    exchange: str = Field(..., pattern="^(hyperliquid|binance)$")
    pair: str = Field(..., min_length=2, max_length=20)
    leverage: int = Field(default=1, ge=1, le=125)
    webhook_secret: Optional[str] = Field(default=None)


class UpdateBotRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    leverage: Optional[int] = Field(default=None, ge=1, le=125)
    enabled: Optional[bool] = Field(default=None)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_store(request: Request) -> BotStore:
    return request.app.state.bot_store


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("", response_model=None)
async def list_bots(request: Request) -> JSONResponse:
    """List all configured bots (webhook secrets masked)."""
    store = _get_store(request)
    bots = store.all()
    return JSONResponse(
        [_mask(b.to_dict()) for b in bots]
    )


@router.get("/{bot_id}", response_model=None)
async def get_bot(bot_id: str, request: Request) -> JSONResponse:
    """Get a single bot by its UUID."""
    store = _get_store(request)
    bot = store.get(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return JSONResponse(_mask(bot.to_dict()))


@router.post("", response_model=None, status_code=201)
async def create_bot(
    body: CreateBotRequest,
    request: Request,
    _auth: None = Depends(_require_api_key),
) -> JSONResponse:
    """Create a new bot."""
    store = _get_store(request)
    bot = Bot.create(
        name=body.name,
        exchange=body.exchange,
        pair=body.pair,
        leverage=body.leverage,
        webhook_secret=body.webhook_secret,
    )
    store.add(bot)
    logger.info("Bot created: %s (%s)", bot.name, bot.id)
    return JSONResponse(bot.to_dict(), status_code=201)


@router.patch("/{bot_id}", response_model=None)
async def update_bot(
    bot_id: str,
    body: UpdateBotRequest,
    request: Request,
    _auth: None = Depends(_require_api_key),
) -> JSONResponse:
    """Update mutable bot fields."""
    store = _get_store(request)
    bot = store.get(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")

    if body.name is not None:
        bot.name = body.name
    if body.leverage is not None:
        bot.leverage = body.leverage
    if body.enabled is not None:
        bot.enabled = body.enabled

    store.update(bot)
    logger.info("Bot updated: %s", bot_id)
    return JSONResponse(bot.to_dict())


@router.delete("/{bot_id}", response_model=None, status_code=204)
async def delete_bot(
    bot_id: str,
    request: Request,
    _auth: None = Depends(_require_api_key),
) -> JSONResponse:
    """Delete a bot by UUID."""
    store = _get_store(request)
    deleted = store.delete(bot_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Bot not found")
    logger.info("Bot deleted: %s", bot_id)
    return JSONResponse(None, status_code=204)


# ── Kill switch endpoints ────────────────────────────────────────────────────

@router.post("/kill-switch/activate", response_model=None)
async def activate_kill_switch(
    request: Request,
    _auth: None = Depends(_require_api_key),
) -> JSONResponse:
    """Manually activate the kill switch to halt all trading."""
    ks = request.app.state.kill_switch
    ks.activate("manual API call")
    telegram = request.app.state.telegram
    await telegram.send_kill_switch_alert("Manual API activation")
    return JSONResponse({"status": "kill_switch_activated"})


@router.post("/kill-switch/deactivate", response_model=None)
async def deactivate_kill_switch(
    request: Request,
    _auth: None = Depends(_require_api_key),
) -> JSONResponse:
    """Deactivate the kill switch to resume trading."""
    ks = request.app.state.kill_switch
    was_active = ks.deactivate()
    return JSONResponse(
        {"status": "deactivated" if was_active else "was_not_active"}
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mask(bot_dict: dict) -> dict:
    """Partially mask the webhook secret for safety."""
    secret = bot_dict.get("webhook_secret", "")
    if len(secret) > 8:
        bot_dict["webhook_secret"] = secret[:4] + "****" + secret[-4:]
    return bot_dict
