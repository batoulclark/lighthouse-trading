"""
Lighthouse Trading — Growth Scanner Watchlist API router.

Endpoints
---------
POST /watchlist            — Receive scanner results (from Luna's cron)
GET  /watchlist            — Return current watchlist
GET  /watchlist/history    — Return historical scans (last N days)
GET  /watchlist/flagged    — Return only coins scoring >= threshold
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

WATCHLIST_FILE = "data/watchlist.json"
WATCHLIST_HISTORY_FILE = "data/watchlist_history.json"
FLAG_THRESHOLD = 70  # Coins scoring >= this get flagged for deployment


# ── Models ────────────────────────────────────────────────────────────────────

class CoinScan(BaseModel):
    symbol: str                     # e.g. "SOL/USDT"
    score: float                    # 0-100 growth score
    price: Optional[float] = None   # Current price
    roc_30: Optional[float] = None  # ROC(30) value
    roc_asymmetry: Optional[float] = None
    price_multiple: Optional[float] = None
    volume_24h: Optional[float] = None
    trend: Optional[str] = None     # "bullish" / "bearish" / "neutral"
    details: Optional[Dict[str, Any]] = None  # Extra metrics from scanner


class ScanSubmission(BaseModel):
    scanner_version: Optional[str] = "1.0"
    timestamp: Optional[str] = None  # ISO timestamp, defaults to now
    coins: List[CoinScan]


# ── File I/O ──────────────────────────────────────────────────────────────────

def _load_json(path: str, default: Any = None) -> Any:
    """Load JSON file, return default if missing or corrupt."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def _save_json(path: str, data: Any) -> None:
    """Atomically write JSON file."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("")
async def submit_scan(submission: ScanSubmission) -> JSONResponse:
    """
    Receive growth scanner results from Luna's cron job.

    Stores the latest scan as the current watchlist and appends
    to history (keeping last 90 days).
    """
    now = datetime.now(timezone.utc).isoformat()
    scan_ts = submission.timestamp or now

    # Build current watchlist
    watchlist = {
        "updated_at": scan_ts,
        "scanner_version": submission.scanner_version,
        "coin_count": len(submission.coins),
        "flag_threshold": FLAG_THRESHOLD,
        "coins": [],
    }

    flagged = []
    for coin in submission.coins:
        entry = {
            "symbol": coin.symbol,
            "score": round(coin.score, 1),
            "price": coin.price,
            "roc_30": round(coin.roc_30, 2) if coin.roc_30 is not None else None,
            "roc_asymmetry": round(coin.roc_asymmetry, 2) if coin.roc_asymmetry is not None else None,
            "price_multiple": round(coin.price_multiple, 1) if coin.price_multiple is not None else None,
            "volume_24h": coin.volume_24h,
            "trend": coin.trend,
            "flagged": coin.score >= FLAG_THRESHOLD,
            "details": coin.details,
        }
        watchlist["coins"].append(entry)
        if coin.score >= FLAG_THRESHOLD:
            flagged.append(entry)

    # Sort by score descending
    watchlist["coins"].sort(key=lambda c: c["score"], reverse=True)
    watchlist["flagged_count"] = len(flagged)

    # Save current watchlist
    _save_json(WATCHLIST_FILE, watchlist)

    # Append to history (keep last 90 entries)
    history = _load_json(WATCHLIST_HISTORY_FILE, [])
    history.append({
        "timestamp": scan_ts,
        "coins": watchlist["coins"],
    })
    history = history[-90:]  # Keep last 90 scans
    _save_json(WATCHLIST_HISTORY_FILE, history)

    logger.info(
        "Watchlist updated: %d coins scanned, %d flagged (>=%d)",
        len(submission.coins), len(flagged), FLAG_THRESHOLD,
    )

    return JSONResponse({
        "status": "ok",
        "coins_received": len(submission.coins),
        "flagged": len(flagged),
        "flagged_coins": [c["symbol"] for c in flagged],
        "timestamp": scan_ts,
    })


@router.get("")
async def get_watchlist() -> JSONResponse:
    """Return the current watchlist (latest scan results)."""
    watchlist = _load_json(WATCHLIST_FILE, {
        "updated_at": None,
        "coins": [],
        "coin_count": 0,
        "flagged_count": 0,
        "flag_threshold": FLAG_THRESHOLD,
    })
    return JSONResponse(watchlist)


@router.get("/flagged")
async def get_flagged(
    threshold: int = Query(FLAG_THRESHOLD, ge=0, le=100, description="Min score to flag"),
) -> JSONResponse:
    """Return only coins that meet the flag threshold."""
    watchlist = _load_json(WATCHLIST_FILE, {"coins": []})
    flagged = [c for c in watchlist.get("coins", []) if c.get("score", 0) >= threshold]

    return JSONResponse({
        "threshold": threshold,
        "count": len(flagged),
        "coins": flagged,
        "updated_at": watchlist.get("updated_at"),
    })


@router.get("/history")
async def get_history(
    days: int = Query(7, ge=1, le=90, description="Number of recent scans to return"),
) -> JSONResponse:
    """Return historical scan results (last N scans)."""
    history = _load_json(WATCHLIST_HISTORY_FILE, [])
    recent = history[-days:]

    return JSONResponse({
        "total_scans": len(history),
        "returned": len(recent),
        "scans": recent,
    })
