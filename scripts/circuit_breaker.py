#!/usr/bin/env python3
"""
Lighthouse Trading — Circuit Breaker / Daily Loss Limit System
==============================================================
Runs every 5 minutes via cron. Checks today's P&L across all bots and
triggers protective actions when loss thresholds are exceeded.

Circuit Breaker Levels:
  Level 1: Daily loss >= LEVEL1_PCT% of capital → LOG WARNING
  Level 2: Daily loss >= LEVEL2_PCT% of capital → Activate kill switch
  Level 3: Daily loss >= LEVEL3_PCT% of capital → Kill switch + disable all bots

Per-bot check:
  If any single bot loses > BOT_MAX_LOSS_PCT% in one day → disable that bot

State file: data/circuit_breaker_state.json
Log file:   /home/yaraclawd/logs/circuit_breaker.log
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "circuit_breaker_state.json"
LOG_FILE = Path("/home/yaraclawd/logs/circuit_breaker.log")
ENV_FILE = BASE_DIR / ".env"

# ─── Load .env manually (avoid dotenv dependency version mismatch) ────────────
def _load_dotenv(path: Path) -> None:
    """Parse key=value lines from a .env file into os.environ (no overwrite)."""
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

_load_dotenv(ENV_FILE)

# ─── Configuration ────────────────────────────────────────────────────────────
BASE_URL = os.getenv("LIGHTHOUSE_URL", "http://127.0.0.1:8420")
API_KEY = os.getenv("LIGHTHOUSE_API_KEY", "")
CAPITAL = float(os.getenv("CAPITAL", "1000"))
LEVEL1_PCT = float(os.getenv("LEVEL1_PCT", "1"))   # warn
LEVEL2_PCT = float(os.getenv("LEVEL2_PCT", "2"))   # kill switch
LEVEL3_PCT = float(os.getenv("LEVEL3_PCT", "5"))   # kill switch + disable all
BOT_MAX_LOSS_PCT = float(os.getenv("BOT_MAX_LOSS_PCT", "3"))  # per-bot threshold
TRADES_LIMIT = int(os.getenv("TRADES_LIMIT", "500"))

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("circuit_breaker")


# ─── HTTP helpers ─────────────────────────────────────────────────────────────
def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def _get(path: str, params: dict | None = None) -> dict | list | None:
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        log.error("GET %s failed: %s", path, exc)
        return None


def _post(path: str, body: dict | None = None) -> dict | None:
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.post(url, headers=_headers(), json=body or {}, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        log.error("POST %s failed: %s", path, exc)
        return None


def _patch(path: str, body: dict) -> dict | None:
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.patch(url, headers=_headers(), json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        log.error("PATCH %s failed: %s", path, exc)
        return None


# ─── State persistence ────────────────────────────────────────────────────────
def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state() -> dict:
    today = _today_utc()
    default = {
        "date": today,
        "daily_pnl": 0.0,
        "level_triggered": 0,
        "bots_disabled": [],
        "capital": CAPITAL,
    }
    if not STATE_FILE.exists():
        return default
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        # Reset if it's a new day
        if state.get("date") != today:
            log.info("New trading day (%s). Resetting circuit breaker state.", today)
            return default
        return state
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read state file: %s. Using defaults.", exc)
        return default


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as exc:
        log.error("Could not save state file: %s", exc)


# ─── P&L calculation ──────────────────────────────────────────────────────────
def fetch_todays_trades() -> list[dict]:
    """Fetch trades and return only those from today (UTC)."""
    data = _get("/dashboard/trades", params={"limit": TRADES_LIMIT})
    if not data or "trades" not in data:
        log.warning("No trades data returned from API.")
        return []

    today = _today_utc()
    today_trades = []
    for trade in data["trades"]:
        ts = trade.get("timestamp", "")
        if ts.startswith(today):
            today_trades.append(trade)
    log.info("Fetched %d trade(s) for today (%s).", len(today_trades), today)
    return today_trades


def calculate_pnl(trades: list[dict]) -> tuple[float, dict[str, float]]:
    """
    Returns (total_pnl, {bot_id: pnl}).
    PnL is extracted from execution_result.pnl (set on close/sell trades).
    """
    total_pnl = 0.0
    bot_pnl: dict[str, float] = {}

    for trade in trades:
        exec_result = trade.get("execution_result") or {}
        raw_pnl = exec_result.get("pnl")

        # Skip open (buy) trades — pnl is None/absent for entries
        if raw_pnl is None:
            continue

        try:
            pnl_val = float(raw_pnl)
        except (TypeError, ValueError):
            continue

        bot_id = trade.get("bot_id", "unknown")
        bot_pnl[bot_id] = bot_pnl.get(bot_id, 0.0) + pnl_val
        total_pnl += pnl_val

    return total_pnl, bot_pnl


# ─── Actions ──────────────────────────────────────────────────────────────────
def activate_kill_switch() -> bool:
    log.warning("CIRCUIT BREAKER: Activating kill switch...")
    result = _post("/bots/kill-switch/activate")
    if result:
        log.warning("Kill switch activated: %s", result)
        return True
    log.error("Failed to activate kill switch.")
    return False


def disable_bot(bot_id: str, bot_name: str, reason: str) -> bool:
    log.warning("CIRCUIT BREAKER: Disabling bot %s (%s) — %s", bot_name, bot_id, reason)
    result = _patch(f"/bots/{bot_id}", {"enabled": False})
    if result:
        log.warning("Bot disabled: %s (%s)", bot_name, bot_id)
        return True
    log.error("Failed to disable bot %s (%s)", bot_name, bot_id)
    return False


def fetch_all_bots() -> list[dict]:
    bots = _get("/bots")
    if not isinstance(bots, list):
        log.error("Could not fetch bot list.")
        return []
    return bots


# ─── Main logic ───────────────────────────────────────────────────────────────
def run() -> None:
    log.info("=" * 60)
    log.info("Circuit breaker check starting.")
    log.info(
        "Config: CAPITAL=%.2f, L1=%.1f%%, L2=%.1f%%, L3=%.1f%%, BOT_MAX=%.1f%%",
        CAPITAL, LEVEL1_PCT, LEVEL2_PCT, LEVEL3_PCT, BOT_MAX_LOSS_PCT,
    )

    state = load_state()

    # Thresholds (loss is negative PnL, so these are negative dollar values)
    level1_threshold = -(CAPITAL * LEVEL1_PCT / 100)   # e.g. -$10
    level2_threshold = -(CAPITAL * LEVEL2_PCT / 100)   # e.g. -$20
    level3_threshold = -(CAPITAL * LEVEL3_PCT / 100)   # e.g. -$50
    bot_max_threshold = -(CAPITAL * BOT_MAX_LOSS_PCT / 100)  # e.g. -$30

    # Fetch and calculate
    trades = fetch_todays_trades()
    total_pnl, bot_pnl = calculate_pnl(trades)

    log.info(
        "Today's total P&L: $%.4f (%.4f%% of capital)",
        total_pnl,
        (total_pnl / CAPITAL * 100),
    )

    # Update state with fresh P&L
    state["daily_pnl"] = total_pnl
    state["capital"] = CAPITAL

    actions_taken = []
    new_bots_disabled = list(state.get("bots_disabled", []))

    # ── Level checks (only escalate, never downgrade) ──────────────────────────
    triggered_level = state.get("level_triggered", 0)

    if total_pnl <= level3_threshold and triggered_level < 3:
        loss_pct = abs(total_pnl) / CAPITAL * 100
        log.warning(
            "LEVEL 3 TRIGGERED: Loss $%.2f (%.2f%% >= %.1f%%). Kill switch + disable all bots.",
            abs(total_pnl), loss_pct, LEVEL3_PCT,
        )
        # Activate kill switch
        if activate_kill_switch():
            actions_taken.append("kill_switch_activated")

        # Disable ALL enabled bots
        all_bots = fetch_all_bots()
        for bot in all_bots:
            bid = bot["id"]
            bname = bot.get("name", bid)
            if bot.get("enabled", False) and bid not in new_bots_disabled:
                if disable_bot(bid, bname, f"Level 3 circuit breaker (loss={loss_pct:.2f}%)"):
                    new_bots_disabled.append(bid)
                    actions_taken.append(f"bot_disabled:{bid}")

        triggered_level = 3

    elif total_pnl <= level2_threshold and triggered_level < 2:
        loss_pct = abs(total_pnl) / CAPITAL * 100
        log.warning(
            "LEVEL 2 TRIGGERED: Loss $%.2f (%.2f%% >= %.1f%%). Activating kill switch.",
            abs(total_pnl), loss_pct, LEVEL2_PCT,
        )
        if activate_kill_switch():
            actions_taken.append("kill_switch_activated")
        triggered_level = 2

    elif total_pnl <= level1_threshold and triggered_level < 1:
        loss_pct = abs(total_pnl) / CAPITAL * 100
        log.warning(
            "LEVEL 1 TRIGGERED: Loss $%.2f (%.2f%% >= %.1f%%). WARNING — monitor closely.",
            abs(total_pnl), loss_pct, LEVEL1_PCT,
        )
        triggered_level = 1
        actions_taken.append("level1_warning")

    else:
        log.info("No circuit breaker level triggered. System nominal.")

    # ── Per-bot loss check ─────────────────────────────────────────────────────
    if bot_pnl:
        all_bots = fetch_all_bots() if triggered_level < 3 else []
        bot_id_to_name = {b["id"]: b.get("name", b["id"]) for b in all_bots}

        for bid, bpnl in bot_pnl.items():
            if bpnl <= bot_max_threshold and bid not in new_bots_disabled:
                loss_pct = abs(bpnl) / CAPITAL * 100
                bname = bot_id_to_name.get(bid, bid)
                log.warning(
                    "PER-BOT LIMIT: Bot '%s' (%s) lost $%.2f (%.2f%% >= %.1f%%). Disabling.",
                    bname, bid, abs(bpnl), loss_pct, BOT_MAX_LOSS_PCT,
                )
                if disable_bot(bid, bname, f"Per-bot loss limit exceeded ({loss_pct:.2f}%)"):
                    new_bots_disabled.append(bid)
                    actions_taken.append(f"bot_disabled:{bid}")
            elif bpnl < 0:
                bname = bot_id_to_name.get(bid, bid) if bot_pnl else bid
                log.info("Bot '%s' (%s): P&L $%.4f (within limits)", bname, bid, bpnl)

    # ── Save state ─────────────────────────────────────────────────────────────
    state["level_triggered"] = triggered_level
    state["bots_disabled"] = new_bots_disabled
    save_state(state)

    if actions_taken:
        log.warning("Actions taken this run: %s", ", ".join(actions_taken))
    log.info("Circuit breaker check complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    run()
