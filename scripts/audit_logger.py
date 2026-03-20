"""
Lighthouse Trading — Audit Trail Logger
========================================
Tails the lighthouse-trading systemd journal, parses structured events,
and appends them to data/audit_trail.json with rotation when the file
exceeds 10 MB (keeping the last 3 rotations).

Run as a standalone process (does NOT touch the live service):
    python scripts/audit_logger.py [--seed] [--verbose]

Options:
  --seed     Seed the audit trail from existing trades.json / bots.json before
             starting the live tail.
  --verbose  Set log level to DEBUG.

The script is safe to start/stop at any time; it never restarts the service.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Paths ─────────────────────────────────────────────────────────────────────

_ROOT       = Path(__file__).resolve().parents[1]  # lighthouse-trading/
_AUDIT_FILE = _ROOT / "data" / "audit_trail.json"
_TRADES_FILE = _ROOT / "data" / "trades.json"
_BOTS_FILE   = _ROOT / "data" / "bots.json"

_ROTATE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_ROTATIONS     = 3
_SERVICE_NAME      = "lighthouse-trading"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  audit_logger — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("audit_logger")

# ── Event-type constants ──────────────────────────────────────────────────────

EVT_TRADE        = "trade_execution"
EVT_KILL_ON      = "kill_switch_activated"
EVT_KILL_OFF     = "kill_switch_deactivated"
EVT_BOT_ENABLED  = "bot_enabled"
EVT_BOT_DISABLED = "bot_disabled"
EVT_WEBHOOK      = "webhook_received"
EVT_ERROR        = "error"
EVT_SYSTEM       = "system"

# ── Severity mapping ──────────────────────────────────────────────────────────

_SEVERITY: Dict[str, str] = {
    EVT_TRADE:        "info",
    EVT_KILL_ON:      "critical",
    EVT_KILL_OFF:     "warning",
    EVT_BOT_ENABLED:  "info",
    EVT_BOT_DISABLED: "warning",
    EVT_WEBHOOK:      "info",
    EVT_ERROR:        "error",
    EVT_SYSTEM:       "info",
}

# ── Journal log patterns ──────────────────────────────────────────────────────
# Each pattern: (event_type, compiled_regex, detail_extractor_fn)

def _trade_details(m: re.Match) -> Dict[str, Any]:
    return {
        "bot_name": m.group("bot"),
        "action":   m.group("action"),
        "pair":     m.group("pair"),
        "size":     m.group("size"),
        "fill":     m.group("fill"),
    }

def _kill_on_details(m: re.Match) -> Dict[str, Any]:
    return {"reason": m.group("reason") if "reason" in m.groupdict() else ""}

def _kill_off_details(m: re.Match) -> Dict[str, Any]:
    return {}

def _bot_details(m: re.Match) -> Dict[str, Any]:
    d = {"bot_id": m.group("bot_id")}
    if "bot_name" in m.groupdict():
        d["bot_name"] = m.group("bot_name")
    return d

def _webhook_details(m: re.Match) -> Dict[str, Any]:
    return {
        "bot_id": m.group("bot_id"),
        "ip":     m.group("ip") if "ip" in m.groupdict() else "",
    }

def _error_details(m: re.Match) -> Dict[str, Any]:
    return {"message": m.group("msg") if "msg" in m.groupdict() else m.group(0)}


_PATTERNS: List[tuple] = [
    # Trade executed (order_executor info log)
    (
        EVT_TRADE,
        re.compile(
            r"Order executed: bot=(?P<bot>.+?) (?P<action>buy|sell|close) "
            r"(?P<pair>\S+) size=(?P<size>[\d.]+) fill=(?P<fill>[\d.]+)",
            re.IGNORECASE,
        ),
        _trade_details,
    ),
    # Kill switch activated
    (
        EVT_KILL_ON,
        re.compile(r"KILL SWITCH ACTIVATED.*?reason:\s*(?P<reason>.+)", re.IGNORECASE),
        _kill_on_details,
    ),
    # Kill switch deactivated
    (
        EVT_KILL_OFF,
        re.compile(r"Kill switch DEACTIVATED", re.IGNORECASE),
        _kill_off_details,
    ),
    # Bot created / enabled (bots API)
    (
        EVT_BOT_ENABLED,
        re.compile(r"Bot created:\s*(?P<bot_name>.+?)\s*\((?P<bot_id>[0-9a-f-]{36})\)", re.IGNORECASE),
        _bot_details,
    ),
    # Bot updated (could be enable/disable)
    (
        EVT_BOT_DISABLED,
        re.compile(r"Bot updated:\s*(?P<bot_id>[0-9a-f-]{36})", re.IGNORECASE),
        _bot_details,
    ),
    # Webhook received
    (
        EVT_WEBHOOK,
        re.compile(
            r"Webhook from (?P<ip>\S+) for bot (?P<bot_id>[0-9a-f-]{36})",
            re.IGNORECASE,
        ),
        _webhook_details,
    ),
    # Errors (logger.error lines)
    (
        EVT_ERROR,
        re.compile(r"\bERROR\b.+?—\s*(?P<msg>.+)", re.IGNORECASE),
        _error_details,
    ),
]


# ── Audit file helpers ────────────────────────────────────────────────────────

def _load_entries() -> List[Dict]:
    """Load existing audit entries or return empty list."""
    if not _AUDIT_FILE.exists():
        return []
    try:
        data = json.loads(_AUDIT_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_entries(entries: List[Dict]) -> None:
    """Persist entries to the audit file (atomic write)."""
    _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _AUDIT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    tmp.replace(_AUDIT_FILE)


def _maybe_rotate() -> None:
    """Rotate the audit file if it exceeds _ROTATE_SIZE_BYTES."""
    if not _AUDIT_FILE.exists():
        return
    if _AUDIT_FILE.stat().st_size < _ROTATE_SIZE_BYTES:
        return

    log.warning("Audit file exceeds 10 MB — rotating")

    # Shift existing rotations: .3 is deleted, .2→.3, .1→.2, current→.1
    for i in range(_MAX_ROTATIONS, 0, -1):
        old = _AUDIT_FILE.with_suffix(f".json.{i}")
        if old.exists():
            if i == _MAX_ROTATIONS:
                old.unlink()
            else:
                old.rename(_AUDIT_FILE.with_suffix(f".json.{i + 1}"))

    shutil.copy2(_AUDIT_FILE, _AUDIT_FILE.with_suffix(".json.1"))
    # Start fresh
    _save_entries([])
    log.info("Rotation complete. New file started.")


def _make_entry(
    event_type: str,
    details: Dict[str, Any],
    source: str = "journal",
    ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a structured audit entry."""
    return {
        "timestamp":  ts or datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "details":    details,
        "source":     source,
        "severity":   _SEVERITY.get(event_type, "info"),
    }


def _append_entry(entry: Dict[str, Any]) -> None:
    """Append one entry and persist (with rotation check)."""
    _maybe_rotate()
    entries = _load_entries()
    entries.append(entry)
    _save_entries(entries)
    log.debug("Appended %s entry", entry["event_type"])


# ── Seed from existing data ───────────────────────────────────────────────────

def seed_from_files() -> None:
    """
    One-shot import of existing trades.json and bots.json into the audit trail.
    Skips entries that already exist (dedup by timestamp+bot_id+action).
    """
    log.info("Seeding audit trail from existing data files…")
    entries    = _load_entries()
    existing   = {
        (e["timestamp"], e["event_type"], e["details"].get("bot_id", ""))
        for e in entries
    }
    new_count  = 0

    # ── trades.json ──────────────────────────────────────────────────────────
    if _TRADES_FILE.exists():
        try:
            trades = json.loads(_TRADES_FILE.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read trades.json: %s", exc)
            trades = []

        for t in trades:
            key = (t.get("timestamp", ""), EVT_TRADE, t.get("bot_id", ""))
            if key in existing:
                continue
            entry = _make_entry(
                event_type=EVT_TRADE,
                details={
                    "bot_id":   t.get("bot_id"),
                    "bot_name": t.get("bot_name"),
                    "action":   t.get("action"),
                    "pair":     t.get("pair"),
                    "fill_price": t.get("fill_price"),
                    "quantity": t.get("quantity"),
                    "pnl":      t.get("pnl"),
                    "error":    t.get("error"),
                    "exchange": t.get("exchange"),
                },
                source="trades.json",
                ts=t.get("timestamp"),
            )
            if t.get("error"):
                entry["severity"] = "error"
            entries.append(entry)
            existing.add(key)
            new_count += 1

    # ── bots.json — snapshot current enable/disable state ────────────────────
    if _BOTS_FILE.exists():
        try:
            bots = json.loads(_BOTS_FILE.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read bots.json: %s", exc)
            bots = []

        for b in bots:
            is_enabled = b.get("enabled", True)
            evt = EVT_BOT_ENABLED if is_enabled else EVT_BOT_DISABLED
            key = (b.get("id", ""), evt, b.get("id", ""))
            entry = _make_entry(
                event_type=evt,
                details={
                    "bot_id":   b.get("id"),
                    "bot_name": b.get("name"),
                    "exchange": b.get("exchange"),
                    "pair":     b.get("pair"),
                },
                source="bots.json",
                ts=datetime.now(timezone.utc).isoformat(),
            )
            entries.append(entry)
            new_count += 1

    _save_entries(entries)
    log.info("Seed complete — %d new entries added", new_count)


# ── Journal tailing ───────────────────────────────────────────────────────────

def _parse_journal_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Try to match a journal line against known patterns.
    Returns an audit entry dict or None.
    """
    for event_type, pattern, extractor in _PATTERNS:
        m = pattern.search(line)
        if m:
            details = extractor(m)
            details["raw"] = line.strip()
            return _make_entry(event_type, details, source="journal")
    return None


def tail_journal() -> None:
    """
    Follow the systemd journal for _SERVICE_NAME and emit audit entries.
    Blocks indefinitely (intended to run as a daemon/cron process).
    """
    log.info("Starting journal tail for unit '%s'", _SERVICE_NAME)

    cmd = [
        "journalctl",
        "-u", _SERVICE_NAME,
        "-f",           # follow
        "-n", "0",      # start from now (skip historical lines)
        "--output", "short-iso",
        "--no-pager",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        log.error("journalctl not found — cannot tail journal")
        return

    log.info("Tailing journal (PID %d)…", proc.pid)

    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if not line:
                continue

            entry = _parse_journal_line(line)
            if entry:
                log.info(
                    "[%s] %s — %s",
                    entry["severity"].upper(),
                    entry["event_type"],
                    json.dumps(entry["details"]),
                )
                _append_entry(entry)

    except KeyboardInterrupt:
        log.info("Interrupted — stopping journal tail")
        proc.terminate()
    except Exception as exc:
        log.error("Journal tail error: %s", exc)
        proc.terminate()
    finally:
        proc.wait(timeout=5)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lighthouse Trading — Audit Trail Logger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Import existing trades.json / bots.json before starting tail",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    log.info("Audit logger starting — output: %s", _AUDIT_FILE)

    if args.seed:
        seed_from_files()

    tail_journal()


if __name__ == "__main__":
    main()
