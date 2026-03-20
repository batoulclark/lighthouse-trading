"""
Lighthouse Trading — Strategies API
GET /strategies          → list all strategies with metadata
GET /strategies/pipeline → strategy development pipeline
GET /strategies/{id}     → single strategy detail

PineScript files are read from the strategy_lab directory and parsed
to enrich the hardcoded strategy registry with live script metadata.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/strategies", tags=["strategies"])

# ── Path to strategy lab ─────────────────────────────────────────────────────

STRATEGY_LAB = Path(
    "/home/yaraclawd/.openclaw/workspace-luna/files/strategy_lab"
)

# ── Hardcoded strategy registry ───────────────────────────────────────────────

STRATEGIES: List[Dict[str, Any]] = [
    {
        "id": "btc-gaussian-v10",
        "name": "BTC Gaussian v10",
        "coin": "BTC",
        "pair": "BTCUSDT",
        "timeframe": "1D",
        "tv_return": 3670,
        "tv_mdd": 23.38,
        "tv_pf": 2.21,
        "tv_trades": 166,
        "tv_wr": 33.1,
        "status": "champion",
        "stage": "paper_live",
        "tv_validated": True,
        "pinescript_file": "gaussian_ls_v10.pine",
        "bot_id": "12e0b34e-18b0-4963-b244-81a0a8ed5ce6",
    },
    {
        "id": "btc-gaussian-v11c",
        "name": "BTC Gaussian v11c",
        "coin": "BTC",
        "pair": "BTCUSDT",
        "timeframe": "1D",
        "tv_return_est_low": 5000,
        "tv_return_est_high": 7000,
        "tv_return": None,           # not yet TV-validated
        "tv_mdd": 41.5,
        "tv_pf": 3.11,
        "tv_trades": 66,
        "tv_wr": None,
        "status": "candidate",
        "stage": "tv_pending",
        "tv_validated": False,
        "pinescript_file": "gaussian_ls_v11c.pine",
        "bot_id": None,
    },
    {
        "id": "sol-roc-v2",
        "name": "SOL ROC v2",
        "coin": "SOL",
        "pair": "SOLUSDT",
        "timeframe": "1D",
        "tv_return": 111429,
        "tv_mdd": 45.4,
        "tv_pf": 3.33,
        "tv_trades": None,
        "tv_wr": 68.9,
        "status": "champion",
        "stage": "paper_live",
        "tv_validated": True,
        "pinescript_file": "momentum_roc_longonly.pine",
        "bot_id": "5481e736-0000-0000-0000-000000000000",
    },
    {
        "id": "eth-roc-v2",
        "name": "ETH ROC v2",
        "coin": "ETH",
        "pair": "ETHUSDT",
        "timeframe": "1D",
        "tv_return": 8369,
        "tv_mdd": 26.4,
        "tv_pf": 2.29,
        "tv_trades": None,
        "tv_wr": 50.0,
        "status": "champion",
        "stage": "paper_live",
        "tv_validated": True,
        "pinescript_file": "momentum_roc_improved.pine",
        "bot_id": "f8be3498-0000-0000-0000-000000000000",
    },
    {
        "id": "btc-gaussian-v8",
        "name": "BTC Gaussian v8",
        "coin": "BTC",
        "pair": "BTCUSDT",
        "timeframe": "1D",
        "tv_return": 2060,
        "tv_mdd": 22.99,
        "tv_pf": 2.08,
        "tv_trades": None,
        "tv_wr": None,
        "status": "retired",
        "stage": "retired",
        "tv_validated": True,
        "pinescript_file": "gaussian_ls_v8.pine",
        "bot_id": None,
    },
    {
        "id": "btc-gaussian-v6",
        "name": "BTC Gaussian v6",
        "coin": "BTC",
        "pair": "BTCUSDT",
        "timeframe": "1D",
        "tv_return": 1781,
        "tv_mdd": 24.03,
        "tv_pf": 2.11,
        "tv_trades": None,
        "tv_wr": None,
        "status": "retired",
        "stage": "retired",
        "tv_validated": True,
        "pinescript_file": "gaussian_ls_v6.pine",
        "bot_id": None,
    },
]

# ── Pipeline stage ordering ───────────────────────────────────────────────────

PIPELINE_STAGES = ["research", "optimize", "tv_validate", "paper_test", "go_live"]

# Stage aliases — map strategy `stage` values → pipeline stage keys
STAGE_MAP = {
    "research": "research",
    "optimize": "optimize",
    "tv_pending": "tv_validate",
    "tv_validate": "tv_validate",
    "paper_live": "paper_test",
    "live": "go_live",
    "go_live": "go_live",
    "retired": None,   # excluded from pipeline counts
    "champion": "paper_test",   # champions still counted in paper_test (live-validated)
}

# ── Pine parser ───────────────────────────────────────────────────────────────

def _parse_pine_header(path: Path) -> Dict[str, Any]:
    """
    Extract metadata from a PineScript file.

    Reads the first 60 lines and parses:
    - strategy() call → name
    - //@version= declaration → version
    - input.* declarations → parameter names and defaults
    - Comment lines (// key: value) → arbitrary key/value pairs
    """
    meta: Dict[str, Any] = {
        "pine_version": None,
        "pine_name": None,
        "pine_params": [],
        "pine_notes": [],
    }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[:80]
    except Exception:
        return meta

    params: List[Dict[str, str]] = []

    for line in lines:
        stripped = line.strip()

        # //@version=6
        vm = re.match(r"//\s*@?version\s*=\s*(\d+)", stripped)
        if vm:
            meta["pine_version"] = int(vm.group(1))
            continue

        # strategy("name", ...)
        nm = re.match(r'strategy\s*\(\s*"([^"]+)"', stripped)
        if nm:
            meta["pine_name"] = nm.group(1)
            continue

        # input.int / input.float / input.bool — grab title
        im = re.match(r'(\w+)\s*=\s*input\.\w+\s*\([^,]+,\s*"([^"]+)"', stripped)
        if im:
            params.append({"var": im.group(1), "label": im.group(2)})
            continue

        # // key: value  (comment metadata)
        cm = re.match(r"//\s+([\w\s]+):\s+(.+)", stripped)
        if cm:
            meta["pine_notes"].append(
                {"key": cm.group(1).strip(), "value": cm.group(2).strip()}
            )

    meta["pine_params"] = params
    return meta


def _enrich(strategy: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of strategy dict enriched with parsed pine metadata."""
    result = dict(strategy)
    pine_file = strategy.get("pinescript_file")
    if pine_file:
        path = STRATEGY_LAB / pine_file
        if path.exists():
            result["pine_meta"] = _parse_pine_header(path)
            result["pinescript_available"] = True
        else:
            result["pine_meta"] = None
            result["pinescript_available"] = False
    else:
        result["pine_meta"] = None
        result["pinescript_available"] = False
    return result


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_strategies() -> Dict[str, Any]:
    """Return all strategies with metadata (hardcoded + live pine parsing)."""
    enriched = [_enrich(s) for s in STRATEGIES]
    return {"strategies": enriched, "total": len(enriched)}


@router.get("/pipeline")
async def get_pipeline() -> Dict[str, Any]:
    """
    Return the strategy development pipeline with stage counts.

    Stages (in order):
      research → optimize → tv_validate → paper_test → go_live
    """
    counts: Dict[str, int] = {stage: 0 for stage in PIPELINE_STAGES}
    stage_strategies: Dict[str, List[str]] = {stage: [] for stage in PIPELINE_STAGES}

    for s in STRATEGIES:
        raw_stage = s.get("stage", "research")
        pipeline_stage = STAGE_MAP.get(raw_stage)
        if pipeline_stage:
            counts[pipeline_stage] += 1
            stage_strategies[pipeline_stage].append(s["id"])

    pipeline = [
        {
            "stage": stage,
            "label": stage.replace("_", " ").title(),
            "count": counts[stage],
            "strategy_ids": stage_strategies[stage],
            "order": i + 1,
        }
        for i, stage in enumerate(PIPELINE_STAGES)
    ]

    return {
        "pipeline": pipeline,
        "total_active": sum(
            1 for s in STRATEGIES if s.get("status") != "retired"
        ),
        "total_retired": sum(
            1 for s in STRATEGIES if s.get("status") == "retired"
        ),
    }


@router.get("/{strategy_id}")
async def get_strategy(strategy_id: str) -> Dict[str, Any]:
    """Return detailed data for a single strategy by ID."""
    for s in STRATEGIES:
        if s["id"] == strategy_id:
            return _enrich(s)
    raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")
