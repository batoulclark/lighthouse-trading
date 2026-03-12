"""
Lighthouse Trading - Health endpoint
GET /health — basic liveness check.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Return current server health status."""
    app_state = request.app.state

    kill_switch_active = False
    if hasattr(app_state, "kill_switch"):
        kill_switch_active = app_state.kill_switch.is_active()

    bot_count = 0
    if hasattr(app_state, "bot_store"):
        bot_count = len(app_state.bot_store.all())

    return JSONResponse(
        {
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kill_switch_active": kill_switch_active,
            "bots_loaded": bot_count,
            "version": "1.0.0",
        }
    )
