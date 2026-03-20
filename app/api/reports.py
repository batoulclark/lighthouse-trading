"""
Lighthouse Trading — Reports API router.

Endpoints
---------
GET /reports/daily  — Return the latest daily P&L report.
                      Reads trades directly from the local file (no HTTP loop).
                      Pass ?refresh=true to regenerate from live data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])

# ── Path resolution ───────────────────────────────────────────────────────────

_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
_SCRIPTS_DIR  = os.path.join(_PROJECT_ROOT, "scripts")
_DEFAULT_OUT  = os.path.join(_PROJECT_ROOT, "data", "daily_report.json")
_DEFAULT_TRADES = os.path.join(_PROJECT_ROOT, "data", "trades.json")

# Make scripts/ importable (lazy — only added once)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _trades_file_from_state(request: Request) -> str:
    """Resolve trades file path from app.state.trade_log if available."""
    trade_log = getattr(request.app.state, "trade_log", None)
    if trade_log is not None and hasattr(trade_log, "path"):
        return trade_log.path
    return _DEFAULT_TRADES


def _safe_json_default(obj: object) -> object:
    """JSON serialiser that converts Infinity to the string 'Infinity'."""
    if isinstance(obj, float) and math.isinf(obj):
        return "Infinity" if obj > 0 else "-Infinity"
    raise TypeError(f"Not serialisable: {type(obj)}")


def _run_report(trades_file: str, output: str) -> dict:
    """
    Synchronous helper — runs generate_report_from_file.
    Called via asyncio.to_thread to avoid blocking the event loop.
    """
    from daily_pnl_report import generate_report_from_file  # type: ignore
    return generate_report_from_file(trades_file=trades_file, output=output)


def _load_cached(output: str) -> dict | None:
    """Load the cached report file; returns None on any error."""
    try:
        with open(output, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("/daily")
async def get_daily_report(
    request: Request,
    refresh: bool = Query(
        False,
        description=(
            "Set to true to regenerate the report from the current trades file "
            "before returning. Otherwise the last cached report is served."
        ),
    ),
) -> JSONResponse:
    """
    Return the daily P&L report.

    **Default behaviour** — serves the cached ``data/daily_report.json``.
    If the cache does not exist, the report is generated automatically.

    **With ``?refresh=true``** — always regenerates from ``data/trades.json``
    before responding (runs in a thread pool; does not block the event loop).

    Response schema
    ---------------
    ```json
    {
      "generated_at": "ISO-8601",
      "period":  { "start": "YYYY-MM-DD", "end": "YYYY-MM-DD" },
      "summary": {
        "total_pnl", "sharpe", "sortino", "calmar",
        "max_dd_duration", "recovery_factor",
        "win_days", "loss_days",
        "best_day":  { "date", "pnl" },
        "worst_day": { "date", "pnl" },
        "current_streak": { "type": "win|loss|none", "count": int }
      },
      "daily":       [{ "date", "pnl", "cumulative", "trades" }],
      "attribution": [{ "bot_name", "pnl", "trades", "win_rate", "pf" }]
    }
    ```
    """
    trades_file  = _trades_file_from_state(request)
    cache_exists = os.path.isfile(_DEFAULT_OUT)

    if refresh or not cache_exists:
        logger.info(
            "Generating fresh report (refresh=%s, cache_exists=%s, trades=%s)",
            refresh, cache_exists, trades_file,
        )
        try:
            report = await asyncio.to_thread(_run_report, trades_file, _DEFAULT_OUT)
        except Exception as exc:
            logger.exception("Report generation failed")
            return JSONResponse(
                {
                    "error":        "Report generation failed",
                    "detail":       str(exc),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
                status_code=500,
            )
    else:
        report = _load_cached(_DEFAULT_OUT)
        if report is None:
            logger.warning("Cached report unreadable — regenerating")
            try:
                report = await asyncio.to_thread(_run_report, trades_file, _DEFAULT_OUT)
            except Exception as exc:
                logger.exception("Report generation failed")
                return JSONResponse(
                    {
                        "error":        "Failed to load or generate report",
                        "detail":       str(exc),
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    status_code=500,
                )

    # Use safe serialiser for Infinity values from profit factor
    return JSONResponse(
        content=json.loads(json.dumps(report, default=_safe_json_default))
    )
