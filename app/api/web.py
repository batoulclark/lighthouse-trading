"""
Lighthouse Trading — Web dashboard router.

Routes
------
GET /                → Single-page HTML trading dashboard
GET /monitor/alerts  → Recent monitor alerts (JSON)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"])

_HTML_PATH = Path(__file__).resolve().parent.parent / "web" / "dashboard.html"


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_dashboard() -> HTMLResponse:
    """Serve the single-page HTML trading dashboard."""
    try:
        html = _HTML_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Could not read dashboard HTML: %s", exc)
        return HTMLResponse(
            "<html><body><h1>Dashboard unavailable</h1></body></html>",
            status_code=503,
        )
    return HTMLResponse(html)


# ── Monitor alerts endpoint ───────────────────────────────────────────────────

@router.get("/monitor/alerts", tags=["monitoring"])
async def get_alerts(request: Request) -> JSONResponse:
    """Return up to 100 most-recent monitor alerts, newest first."""
    monitor = getattr(request.app.state, "monitor", None)
    if monitor is None:
        return JSONResponse({"alerts": [], "total": 0})

    alerts: List[Dict[str, Any]] = monitor.get_alert_history(limit=100)
    return JSONResponse({"alerts": alerts, "total": len(alerts)})
