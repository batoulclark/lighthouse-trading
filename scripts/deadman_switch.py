#!/usr/bin/env python3
"""
Lighthouse Trading - Dead Man's Switch
======================================
Runs every 10 minutes via cron. Monitors the trading system health endpoint.
If no successful health check in 30 minutes, activates kill switch automatically.

Cron: */10 * * * * /usr/bin/python3 /home/yaraclawd/lighthouse-trading/scripts/deadman_switch.py >> /home/yaraclawd/logs/lighthouse_deadman.log 2>&1
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
HEALTH_URL        = "http://127.0.0.1:8420/health"
KILL_SWITCH_URL   = "http://127.0.0.1:8420/bots/kill-switch/activate"
STATE_FILE        = "/tmp/lighthouse_deadman_state.json"
ALERT_FILE        = "/tmp/lighthouse_deadman_alert.txt"
LOG_FILE          = "/home/yaraclawd/logs/lighthouse_deadman.log"
MAX_GAP_SECONDS   = 30 * 60   # 30 minutes — trigger kill switch if no health for this long
REQUEST_TIMEOUT   = 10        # seconds per HTTP request

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_ts() -> float:
    return time.time()

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg: str):
    """Print with timestamp — stdout goes to the log file via cron redirect."""
    print(f"[{now_iso()}] {msg}", flush=True)

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "last_healthy_ts": None,
            "consecutive_failures": 0,
            "consecutive_successes": 0,
            "kill_switch_activated": False,
            "kill_switch_activated_at": None,
            "total_checks": 0,
        }

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def write_alert(reason: str):
    with open(ALERT_FILE, "w") as f:
        f.write(f"LIGHTHOUSE DEAD MAN'S SWITCH — ALERT\n")
        f.write(f"{'=' * 50}\n")
        f.write(f"Timestamp : {now_iso()}\n")
        f.write(f"Reason    : {reason}\n")
        f.write(f"Action    : Kill switch activated via {KILL_SWITCH_URL}\n")
        f.write(f"\nTrading has been halted automatically.\n")
        f.write(f"Manual deactivation required before resuming.\n")
        f.write(f"{'=' * 50}\n")

def check_health() -> bool:
    """Returns True if health endpoint responds with HTTP 2xx."""
    try:
        resp = requests.get(HEALTH_URL, timeout=REQUEST_TIMEOUT)
        if resp.status_code < 300:
            log(f"HEALTH OK  — HTTP {resp.status_code}")
            return True
        else:
            log(f"HEALTH BAD — HTTP {resp.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        log(f"HEALTH FAIL — Connection refused (is the server running?)")
        return False
    except requests.exceptions.Timeout:
        log(f"HEALTH FAIL — Request timed out after {REQUEST_TIMEOUT}s")
        return False
    except Exception as e:
        log(f"HEALTH FAIL — Unexpected error: {e}")
        return False

def activate_kill_switch() -> bool:
    """POSTs to kill switch endpoint. Returns True on success."""
    try:
        resp = requests.post(KILL_SWITCH_URL, timeout=REQUEST_TIMEOUT, json={
            "source": "deadman_switch",
            "reason": "No health check response for 30+ minutes",
            "timestamp": now_iso(),
        })
        if resp.status_code < 300:
            log(f"KILL SWITCH ACTIVATED — HTTP {resp.status_code}")
            return True
        else:
            log(f"KILL SWITCH FAILED — HTTP {resp.status_code}: {resp.text[:200]}")
            return False
    except requests.exceptions.ConnectionError:
        log(f"KILL SWITCH FAILED — Cannot reach kill switch endpoint (server may be completely down)")
        return False
    except Exception as e:
        log(f"KILL SWITCH FAILED — {e}")
        return False

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("Dead man's switch starting")

    state = load_state()
    state["total_checks"] = state.get("total_checks", 0) + 1

    healthy = check_health()

    if healthy:
        state["last_healthy_ts"] = now_ts()
        state["consecutive_failures"] = 0
        state["consecutive_successes"] = state.get("consecutive_successes", 0) + 1

        if state.get("kill_switch_activated"):
            # Health recovered but we do NOT auto-resume — require manual deactivation
            log(
                f"⚠️  Health RECOVERED after kill switch activation — "
                f"NOT auto-resuming. Manual deactivation required."
            )
            # Keep the alert file so operator notices
        else:
            log(
                f"✅ System healthy — consecutive successes: {state['consecutive_successes']}, "
                f"failures: {state['consecutive_failures']}"
            )
            # Clean up alert file if exists and no kill switch active
            if os.path.exists(ALERT_FILE):
                os.remove(ALERT_FILE)
                log("Alert file removed (system recovered)")

    else:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["consecutive_successes"] = 0

        last_healthy = state.get("last_healthy_ts")
        gap = now_ts() - last_healthy if last_healthy else None

        log(
            f"❌ Health check failed — consecutive failures: {state['consecutive_failures']}, "
            f"last healthy: {datetime.fromtimestamp(last_healthy, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if last_healthy else 'NEVER'}, "
            f"gap: {int(gap)}s" if gap else f"gap: unknown (no prior healthy state)"
        )

        should_trigger = (
            not state.get("kill_switch_activated") and (
                last_healthy is None or gap >= MAX_GAP_SECONDS
            )
        )

        if should_trigger:
            reason = (
                f"No successful health check for {int(gap)}s (limit: {MAX_GAP_SECONDS}s)"
                if gap
                else "Health endpoint has never responded — system may have never started"
            )
            log(f"🚨 TRIGGERING KILL SWITCH — {reason}")

            # Write alert regardless of whether kill switch POST succeeds
            write_alert(reason)

            activated = activate_kill_switch()

            state["kill_switch_activated"] = True
            state["kill_switch_activated_at"] = now_iso()

            if not activated:
                log("⚠️  Kill switch POST failed (server may be fully down), but alert file written")
        else:
            if state.get("kill_switch_activated"):
                log(f"Kill switch already active — skipping re-activation. Manual deactivation required.")
            else:
                remaining = MAX_GAP_SECONDS - (gap or 0)
                log(f"⏳ Within tolerance — kill switch in ~{int(remaining)}s if no recovery")

    save_state(state)
    log(f"State saved — checks: {state['total_checks']}, kill_active: {state.get('kill_switch_activated', False)}")
    log("Dead man's switch done")


if __name__ == "__main__":
    main()
