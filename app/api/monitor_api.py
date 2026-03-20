"""
Monitor API — FastAPI router exposing monitoring data to the dashboard.

Endpoints:
  GET /monitor/status        — current monitor_status.json snapshot
  GET /monitor/health-history — last 24h of health check results from log
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["monitoring"])

# ── Paths ─────────────────────────────────────────────────────────────────────

_STATUS_FILE = Path("/home/yaraclawd/lighthouse-trading/data/monitor_status.json")
_MONITOR_LOG = Path("/home/yaraclawd/logs/lighthouse_monitor.log")

# ── /monitor/status ───────────────────────────────────────────────────────────


@router.get("/monitor/status")
async def get_monitor_status() -> JSONResponse:
    """Return the latest monitor status snapshot.

    Reads ``data/monitor_status.json`` written by the monitor cron.
    If the file doesn't exist the monitor hasn't run yet.
    """
    if not _STATUS_FILE.exists():
        return JSONResponse(
            {"status": "no_data", "message": "Monitor has not run yet"}
        )

    try:
        data: Dict[str, Any] = json.loads(_STATUS_FILE.read_text())
        return JSONResponse(data)
    except (json.JSONDecodeError, OSError) as exc:
        return JSONResponse(
            {"status": "error", "message": f"Failed to read status file: {exc}"},
            status_code=500,
        )


# ── /monitor/health-history ───────────────────────────────────────────────────

# Patterns matching lines produced by the monitor script
_RUN_RE = re.compile(
    r"LIGHTHOUSE MONITOR RUN\s*[—-]+\s*(\S+)"
)
_HEALTH_OK_RE = re.compile(r"\[INFO\]\s+Health OK")
_HEALTH_FAIL_RE = re.compile(r"\[(?:ERROR|WARNING)\]\s+Health\s+(?:FAIL|CRITICAL)")
_WEBHOOK_RE = re.compile(r"Webhook times:\s+(.+)$")
_STALE_RE = re.compile(r"Stale bots:\s+(.+)$")


def _parse_health_history(cutoff: datetime) -> List[Dict[str, Any]]:
    """Parse the monitor log and return health-check entries newer than *cutoff*."""
    if not _MONITOR_LOG.exists():
        return []

    checks: List[Dict[str, Any]] = []

    current_ts: Optional[datetime] = None
    current_health_ok: Optional[bool] = None
    current_webhook_times: Optional[Dict[str, Any]] = None
    current_stale_bots: Optional[List[str]] = None

    def _flush() -> None:
        """Commit the current run if it's within our time window."""
        if current_ts is None or current_ts < cutoff:
            return
        checks.append(
            {
                "timestamp": current_ts.isoformat(),
                "health_ok": bool(current_health_ok),
                "webhook_times": current_webhook_times or {},
                "stale_bots": current_stale_bots or [],
            }
        )

    try:
        for raw_line in _MONITOR_LOG.read_text(errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue

            # ── New monitor run block ─────────────────────────────────────
            run_match = _RUN_RE.search(line)
            if run_match:
                _flush()  # commit previous run
                ts_str = run_match.group(1)
                try:
                    current_ts = datetime.fromisoformat(ts_str)
                    if current_ts.tzinfo is None:
                        current_ts = current_ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    current_ts = None
                current_health_ok = None
                current_webhook_times = None
                current_stale_bots = None
                continue

            if current_ts is None:
                continue  # haven't found a run header yet

            # ── Health result ─────────────────────────────────────────────
            if _HEALTH_OK_RE.search(line):
                current_health_ok = True
            elif _HEALTH_FAIL_RE.search(line):
                current_health_ok = False

            # ── Webhook times (from SUMMARY block) ────────────────────────
            wh_match = _WEBHOOK_RE.search(line)
            if wh_match:
                raw_val = wh_match.group(1).strip()
                try:
                    current_webhook_times = ast.literal_eval(raw_val)
                except Exception:
                    current_webhook_times = {"raw": raw_val}

            # ── Stale bots (from SUMMARY block) ──────────────────────────
            stale_match = _STALE_RE.search(line)
            if stale_match:
                raw_val = stale_match.group(1).strip()
                try:
                    parsed = ast.literal_eval(raw_val)
                    current_stale_bots = parsed if isinstance(parsed, list) else [parsed]
                except Exception:
                    # Fallback: treat as comma-separated string
                    current_stale_bots = [s.strip() for s in raw_val.split(",") if s.strip()]

        # Flush the last run
        _flush()

    except OSError:
        pass

    # Return newest-first, capped at 24 h
    return list(reversed(checks))


@router.get("/monitor/health-history")
async def get_health_history() -> JSONResponse:
    """Return health-check results from the last 24 hours.

    Parses ``/home/yaraclawd/logs/lighthouse_monitor.log`` and extracts per-run
    snapshots containing timestamp, health status, webhook response times, and
    stale bot names.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    checks = _parse_health_history(cutoff)
    return JSONResponse({"checks": checks, "count": len(checks)})
