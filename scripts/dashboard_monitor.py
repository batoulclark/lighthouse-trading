#!/usr/bin/env python3
"""
Lighthouse Trading - Dashboard Monitor
Cron job: runs periodically to check health, webhooks, data integrity, and service status.

Usage:  python3 scripts/dashboard_monitor.py
Cron:   */15 * * * * /usr/bin/python3 /home/yaraclawd/lighthouse-trading/scripts/dashboard_monitor.py
Output: /home/yaraclawd/lighthouse-trading/data/monitor_status.json
Log:    /home/yaraclawd/logs/lighthouse_monitor.log
Alert:  /tmp/lighthouse_alert.txt  (created when health fails 3× consecutively)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    print("[ERROR] requests library not found. Install with: pip install requests")
    sys.exit(1)

# ── Paths ──────────────────────────────────────────────────────────────────────

BASE_DIR          = Path(__file__).parent.parent
DATA_DIR          = BASE_DIR / "data"
LOGS_DIR          = Path("/home/yaraclawd/logs")
MONITOR_LOG       = LOGS_DIR / "lighthouse_monitor.log"
STATUS_FILE       = DATA_DIR / "monitor_status.json"
ALERT_FILE        = Path("/tmp/lighthouse_alert.txt")
HEALTH_STATE_FILE = DATA_DIR / "monitor_health_state.json"   # tracks consecutive failures

BOTS_FILE         = DATA_DIR / "bots.json"
TRADES_FILE       = DATA_DIR / "trades.json"
POSITIONS_FILE    = DATA_DIR / "positions.json"

BASE_URL          = "http://127.0.0.1:8420"
SERVICE_NAME      = "lighthouse-trading.service"

REQUEST_TIMEOUT   = 10      # seconds per HTTP call
STALE_HOURS       = 48      # flag bot if no trade in this many hours
HEALTH_FAIL_LIMIT = 3       # consecutive failures before alert

# ── Logging ────────────────────────────────────────────────────────────────────

LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(MONITOR_LOG),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("lighthouse_monitor")


# ── Health State (persists consecutive failure count) ─────────────────────────

def _load_health_state() -> Dict[str, Any]:
    if HEALTH_STATE_FILE.exists():
        try:
            return json.loads(HEALTH_STATE_FILE.read_text())
        except Exception:
            pass
    return {"consecutive_failures": 0, "last_alert": None}


def _save_health_state(state: Dict[str, Any]) -> None:
    try:
        HEALTH_STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log.warning("Could not save health state: %s", exc)


# ── 1. Health Check ────────────────────────────────────────────────────────────

def check_health() -> bool:
    """
    Hit GET /health.  Track consecutive failures; create alert file after 3.
    Returns True if healthy.
    """
    url = f"{BASE_URL}/health"
    state = _load_health_state()

    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        healthy = data.get("status") == "ok"
        if healthy:
            log.info("Health OK — kill_switch=%s bots=%s",
                     data.get("kill_switch_active"), data.get("bots_loaded"))
            state["consecutive_failures"] = 0
            # Clear stale alert if service has recovered
            if ALERT_FILE.exists():
                ALERT_FILE.unlink(missing_ok=True)
                log.info("Health recovered — alert file removed.")
        else:
            log.warning("Health endpoint returned non-ok status: %s", data)
            state["consecutive_failures"] += 1
    except Exception as exc:
        log.error("Health check FAILED (%s): %s", url, exc)
        state["consecutive_failures"] += 1
        healthy = False

    failures = state["consecutive_failures"]
    log.info("Consecutive health failures: %d", failures)

    if failures >= HEALTH_FAIL_LIMIT:
        _trigger_alert(failures)
        state["last_alert"] = datetime.now(timezone.utc).isoformat()

    _save_health_state(state)
    return healthy


def _trigger_alert(failures: int) -> None:
    """Write alert file."""
    msg = (
        f"[LIGHTHOUSE ALERT] Health check has failed {failures} consecutive times.\n"
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n"
        f"URL: {BASE_URL}/health\n"
        f"Service: {SERVICE_NAME}\n"
        f"Action: Check 'systemctl status {SERVICE_NAME}' and the monitor log.\n"
    )
    try:
        ALERT_FILE.write_text(msg)
        log.error("ALERT CREATED at %s (failures=%d)", ALERT_FILE, failures)
    except Exception as exc:
        log.error("Could not write alert file: %s", exc)


# ── 2. Webhook Verification ────────────────────────────────────────────────────

def verify_webhooks(bots: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """
    Send a lightweight test payload to each active bot's webhook endpoint.
    Returns {bot_name: response_time_ms} — None if unreachable / 5xx.
    """
    results: Dict[str, Optional[float]] = {}
    active = [b for b in bots if b.get("enabled")]

    if not active:
        log.info("No enabled bots — skipping webhook verification.")
        return results

    for bot in active:
        bot_id = bot.get("id", "")
        bot_name = bot.get("name", bot_id)
        webhook_secret = bot.get("webhook_secret", "")
        url = f"{BASE_URL}/webhook/{bot_id}"

        # Minimal test payload — uses wrong action so it validates auth but won't
        # trigger a real order.  Sends action "ping" which the validator will reject
        # with 422 (not 500), confirming the endpoint is alive.
        payload = {
            "bot_id": webhook_secret,
            "action": "ping",          # deliberately invalid action → 422 expected
            "pair": bot.get("pair", "BTCUSDT"),
            "_monitor_test": True,
        }

        try:
            start = time.monotonic()
            resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code >= 500:
                log.error("Webhook FAIL %s → HTTP %d (%.0f ms)",
                          bot_name, resp.status_code, elapsed_ms)
                results[bot_name] = None
            else:
                log.info("Webhook OK %s → HTTP %d (%.0f ms)",
                         bot_name, resp.status_code, elapsed_ms)
                results[bot_name] = round(elapsed_ms, 1)
        except Exception as exc:
            log.error("Webhook ERROR %s: %s", bot_name, exc)
            results[bot_name] = None

    return results


# ── 3. Stale Signal Detection ──────────────────────────────────────────────────

def detect_stale_bots(
    bots: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
) -> List[str]:
    """
    For each enabled bot, find the most recent trade.
    If no trade in the last STALE_HOURS hours → flag as stale.
    Also uses /dashboard/trades API if the server is reachable (falls back to file).
    Returns list of stale bot names.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=STALE_HOURS)

    # Build {bot_id: latest_trade_timestamp}
    latest: Dict[str, datetime] = {}
    for trade in trades:
        bot_id = trade.get("bot_id", "")
        ts_str = trade.get("timestamp") or trade.get("signal_timestamp")
        if not ts_str:
            continue
        try:
            # Handle both offset-aware and naive timestamps
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if bot_id not in latest or ts > latest[bot_id]:
                latest[bot_id] = ts
        except (ValueError, TypeError):
            continue

    stale: List[str] = []
    enabled_bots = [b for b in bots if b.get("enabled")]

    for bot in enabled_bots:
        bot_id   = bot.get("id", "")
        bot_name = bot.get("name", bot_id)
        last_ts  = latest.get(bot_id)

        if last_ts is None:
            log.warning("STALE [no trades ever] — %s", bot_name)
            stale.append(bot_name)
        elif last_ts < cutoff:
            age_h = (now - last_ts).total_seconds() / 3600
            log.warning("STALE [%.1fh ago] — %s", age_h, bot_name)
            stale.append(bot_name)
        else:
            age_h = (now - last_ts).total_seconds() / 3600
            log.info("Signal fresh (%.1fh ago) — %s", age_h, bot_name)

    return stale


# ── 4. Data Integrity ──────────────────────────────────────────────────────────

def check_data_integrity() -> bool:
    """
    Verify trades.json, bots.json, positions.json:
      - File exists
      - Non-zero size
      - Valid JSON
    Returns True if all pass.
    """
    files = {
        "trades.json":    TRADES_FILE,
        "bots.json":      BOTS_FILE,
        "positions.json": POSITIONS_FILE,
    }
    all_ok = True

    for name, path in files.items():
        if not path.exists():
            log.error("DATA INTEGRITY — %s does not exist!", name)
            all_ok = False
            continue

        size = path.stat().st_size
        if size == 0:
            log.error("DATA INTEGRITY — %s is 0 bytes (corrupted)!", name)
            all_ok = False
            continue

        try:
            content = path.read_text(encoding="utf-8")
            json.loads(content)
            log.info("Data OK — %s (%d bytes)", name, size)
        except json.JSONDecodeError as exc:
            log.error("DATA INTEGRITY — %s is not valid JSON: %s", name, exc)
            all_ok = False
        except Exception as exc:
            log.error("DATA INTEGRITY — could not read %s: %s", name, exc)
            all_ok = False

    return all_ok


# ── 5. Systemd Service Check ───────────────────────────────────────────────────

def check_service() -> bool:
    """
    Verify lighthouse-trading.service is active.
    If not, attempt a restart and log the result.
    Returns True if active (or successfully restarted).
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True, text=True, timeout=10,
        )
        active = result.stdout.strip() == "active"
    except FileNotFoundError:
        log.warning("systemctl not available (not a systemd system?)")
        return True   # don't penalise non-systemd environments
    except subprocess.TimeoutExpired:
        log.error("systemctl timed out checking %s", SERVICE_NAME)
        return False
    except Exception as exc:
        log.error("Service check error: %s", exc)
        return False

    if active:
        log.info("Service %s is active.", SERVICE_NAME)
        return True

    log.error("Service %s is NOT active — attempting restart...", SERVICE_NAME)
    try:
        restart = subprocess.run(
            ["systemctl", "restart", SERVICE_NAME],
            capture_output=True, text=True, timeout=30,
        )
        if restart.returncode == 0:
            log.info("Service restart succeeded.")
            return True
        else:
            log.error("Service restart FAILED (rc=%d): %s",
                      restart.returncode, restart.stderr.strip())
            return False
    except Exception as exc:
        log.error("Service restart exception: %s", exc)
        return False


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_json_file(path: Path) -> Any:
    """Load a JSON file, returning None on error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Could not load %s: %s", path, exc)
        return None


def _write_status(status: Dict[str, Any]) -> None:
    """Write the JSON summary to STATUS_FILE."""
    try:
        STATUS_FILE.write_text(json.dumps(status, indent=2, default=str))
        log.info("Status written to %s", STATUS_FILE)
    except Exception as exc:
        log.error("Could not write status file: %s", exc)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> Dict[str, Any]:
    run_ts = datetime.now(timezone.utc).isoformat()
    log.info("=" * 60)
    log.info("LIGHTHOUSE MONITOR RUN — %s", run_ts)
    log.info("=" * 60)

    # --- Load data files ---
    bots      = _load_json_file(BOTS_FILE)   or []
    trades    = _load_json_file(TRADES_FILE)  or []
    # positions loaded only for integrity check

    # --- Run checks ---
    health_ok        = check_health()
    data_integrity   = check_data_integrity()
    service_active   = check_service()
    webhook_times    = verify_webhooks(bots)
    stale_bots       = detect_stale_bots(bots, trades)

    enabled_bots     = [b.get("name", b.get("id", "?")) for b in bots if b.get("enabled")]
    bots_checked     = len(enabled_bots)

    # --- Compose summary ---
    status: Dict[str, Any] = {
        "timestamp":        run_ts,
        "health_ok":        health_ok,
        "bots_checked":     bots_checked,
        "stale_bots":       stale_bots,
        "webhook_times":    webhook_times,
        "data_integrity_ok": data_integrity,
        "service_active":   service_active,
        # extra context
        "enabled_bots":     enabled_bots,
        "alert_active":     ALERT_FILE.exists(),
    }

    _write_status(status)

    # --- Console summary ---
    log.info("-" * 60)
    log.info("SUMMARY:")
    log.info("  Health:         %s", "OK" if health_ok else "FAIL")
    log.info("  Data integrity: %s", "OK" if data_integrity else "FAIL")
    log.info("  Service:        %s", "active" if service_active else "INACTIVE")
    log.info("  Bots checked:   %d", bots_checked)
    log.info("  Stale bots:     %s", stale_bots or "none")
    log.info("  Webhook times:  %s", {k: (f"{v:.0f}ms" if v else "FAIL") for k, v in webhook_times.items()})
    log.info("  Alert active:   %s", ALERT_FILE.exists())
    log.info("=" * 60)

    return status


if __name__ == "__main__":
    result = main()
    # Exit non-zero if something critical is broken
    if not result.get("health_ok") or not result.get("data_integrity_ok"):
        sys.exit(1)
    sys.exit(0)
