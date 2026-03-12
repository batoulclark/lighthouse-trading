"""
Lighthouse Trading - State Backup
Saves bot configurations and trade log to two independent locations
with file rotation (default 30 files per location).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class StateBackup:
    """
    Writes timestamped snapshots of system state to two directories.
    Automatically rotates old backups, keeping at most `max_files` per dir.
    """

    def __init__(
        self,
        dir1: str,
        dir2: str,
        max_files: int = 30,
    ) -> None:
        self.dir1 = os.path.expanduser(dir1)
        self.dir2 = os.path.expanduser(dir2)
        self.max_files = max_files

    # ── Public API ───────────────────────────────────────────────────────────

    def save(self, bots: List[Dict[str, Any]], trades: List[Dict[str, Any]]) -> None:
        """Write a snapshot to both backup directories."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bots": bots,
            "trades": trades,
        }
        filename = f"state_{timestamp}.json"

        for directory in (self.dir1, self.dir2):
            try:
                self._write(directory, filename, payload)
                self._rotate(directory)
            except Exception as exc:
                logger.error("Backup failed to %s: %s", directory, exc)

    def latest(self, directory: str | None = None) -> Dict[str, Any] | None:
        """Return the most recent backup, or None if no backups exist."""
        d = os.path.expanduser(directory or self.dir1)
        files = self._list_files(d)
        if not files:
            return None
        with open(files[-1], "r") as fh:
            return json.load(fh)

    # ── Private helpers ──────────────────────────────────────────────────────

    def _write(self, directory: str, filename: str, payload: Dict[str, Any]) -> None:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, filename)
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        logger.debug("Backup written: %s", path)

    def _rotate(self, directory: str) -> None:
        files = self._list_files(directory)
        while len(files) > self.max_files:
            oldest = files.pop(0)
            try:
                os.remove(oldest)
                logger.debug("Backup rotated (deleted): %s", oldest)
            except OSError as exc:
                logger.warning("Could not delete old backup %s: %s", oldest, exc)
                break

    @staticmethod
    def _list_files(directory: str) -> List[str]:
        pattern = os.path.join(directory, "state_*.json")
        return sorted(glob.glob(pattern))
