"""
Lighthouse Trading — Dashboard API router.

Endpoints
---------
GET /dashboard              — Overall system status (all bots, total P&L, positions)
GET /dashboard/bot/{bot_id} — Single bot performance metrics
GET /dashboard/equity       — Equity curve [{date, equity}]
GET /dashboard/trades       — Trade history with optional filters
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.services.performance import PerformanceTracker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ── Helper ────────────────────────────────────────────────────────────────────

def _tracker(request: Request) -> PerformanceTracker:
    """Return a PerformanceTracker backed by the live trades file."""
    trade_log = getattr(request.app.state, "trade_log", None)
    path = trade_log.path if trade_log is not None else "data/trades.json"
    return PerformanceTracker(path)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def dashboard_overview(request: Request) -> JSONResponse:
    """Return overall system status.

    Includes kill-switch state, active bot count, total P&L, and a
    per-bot summary pulled from the live trade log.
    """
    state = request.app.state

    kill_switch_active = False
    if hasattr(state, "kill_switch"):
        kill_switch_active = state.kill_switch.is_active()

    bots: List[Dict[str, Any]] = []
    if hasattr(state, "bot_store"):
        for bot in state.bot_store.all():
            bots.append({
                "id":       bot.id,
                "name":     bot.name,
                "exchange": bot.exchange,
                "pair":     bot.pair,
                "enabled":  bot.enabled,
                "leverage": bot.leverage,
            })

    tracker = _tracker(request)
    summary = tracker.get_summary()

    return JSONResponse({
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "kill_switch_active": kill_switch_active,
        "bot_count":          len(bots),
        "bots":               bots,
        "performance":        summary,
    })


@router.get("/bot/{bot_id}")
async def dashboard_bot(bot_id: str, request: Request) -> JSONResponse:
    """Return performance metrics for a single bot.

    Returns 404 if the bot_id is not found in the bot store.
    """
    state = request.app.state

    bot_info: Optional[Dict[str, Any]] = None
    if hasattr(state, "bot_store"):
        bot = state.bot_store.get(bot_id)
        if bot is not None:
            bot_info = {
                "id":       bot.id,
                "name":     bot.name,
                "exchange": bot.exchange,
                "pair":     bot.pair,
                "enabled":  bot.enabled,
                "leverage": bot.leverage,
            }

    if bot_info is None:
        return JSONResponse(
            {"detail": f"Bot '{bot_id}' not found"},
            status_code=404,
        )

    tracker = _tracker(request)
    summary    = tracker.get_summary(bot_id=bot_id)
    daily_pnl  = tracker.get_daily_pnl(bot_id=bot_id)
    trade_stats = tracker.get_trade_stats(bot_id=bot_id)

    return JSONResponse({
        "bot":         bot_info,
        "performance": summary,
        "daily_pnl":   daily_pnl,
        "stats":       trade_stats,
    })


@router.get("/equity")
async def dashboard_equity(request: Request) -> JSONResponse:
    """Return the cumulative equity curve as [{date, equity}] sorted by date."""
    tracker = _tracker(request)
    curve   = tracker.get_equity_curve()
    return JSONResponse({"equity_curve": curve})


@router.get("/trades")
async def dashboard_trades(
    request: Request,
    bot_id:    Optional[str] = Query(None, description="Filter by bot ID"),
    date_from: Optional[str] = Query(None, description="ISO date lower bound (inclusive)"),
    date_to:   Optional[str] = Query(None, description="ISO date upper bound (inclusive)"),
    limit:     int           = Query(100,  ge=1, le=10_000, description="Max records"),
) -> JSONResponse:
    """Return trade history with optional filters.

    Query parameters
    ----------------
    bot_id    : Filter to a specific bot UUID.
    date_from : Return only trades on or after this date (YYYY-MM-DD).
    date_to   : Return only trades on or before this date (YYYY-MM-DD).
    limit     : Maximum number of records to return (default 100, max 10000).
    """
    tracker = _tracker(request)
    trades  = tracker._load(bot_id=bot_id)

    # Date filters
    if date_from:
        trades = [t for t in trades if _trade_date(t) >= date_from]
    if date_to:
        trades = [t for t in trades if _trade_date(t) <= date_to]

    # Most recent first, then apply limit
    trades = sorted(trades, key=lambda t: t.get("timestamp", ""), reverse=True)
    trades = trades[:limit]

    return JSONResponse({
        "total":  len(trades),
        "trades": trades,
    })


# ── Utility ───────────────────────────────────────────────────────────────────

def _trade_date(trade: dict) -> str:
    """Extract ISO date (YYYY-MM-DD) from a trade dict's timestamp field."""
    ts = trade.get("timestamp", "")
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except ValueError:
        return ts[:10]  # best-effort slice
