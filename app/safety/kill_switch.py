"""
Lighthouse Trading - Kill Switch
A file-based emergency stop that blocks ALL trade execution.

Usage
-----
Create a file named KILL_SWITCH (path configurable) to halt trading.
Delete the file to resume trading.
The kill switch state is checked before every order.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class KillSwitch:
    """
    File-based kill switch.

    If the kill-switch file exists, is_active() returns True and all
    order attempts are refused.
    """

    def __init__(self, kill_switch_file: str) -> None:
        self.path = kill_switch_file
        self._notified: bool = False  # avoid spam on repeated checks

    # ── Public API ───────────────────────────────────────────────────────────

    def is_active(self) -> bool:
        """Return True if the kill switch file exists."""
        return os.path.exists(self.path)

    def activate(self, reason: str = "manual") -> None:
        """Create the kill switch file with a reason + timestamp."""
        with open(self.path, "w") as fh:
            fh.write(f"Kill switch activated: {reason}\n")
            fh.write(f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n")
        logger.critical("KILL SWITCH ACTIVATED — reason: %s", reason)

    def deactivate(self) -> bool:
        """Remove the kill switch file. Returns True if it was active."""
        if os.path.exists(self.path):
            os.remove(self.path)
            self._notified = False
            logger.warning("Kill switch DEACTIVATED")
            return True
        return False

    def read_reason(self) -> str:
        """Return the contents of the kill switch file, or empty string."""
        if not self.is_active():
            return ""
        try:
            with open(self.path, "r") as fh:
                return fh.read()
        except OSError:
            return "(unreadable)"

    def check_and_raise(self) -> None:
        """
        Raise RuntimeError if the kill switch is active.
        Logs a critical message on the first check after activation.
        """
        if self.is_active():
            if not self._notified:
                logger.critical(
                    "Kill switch is ACTIVE — all trading halted. Reason: %s",
                    self.read_reason(),
                )
                self._notified = True
            raise RuntimeError(
                f"Kill switch is active — trading halted. "
                f"Remove '{self.path}' to resume."
            )
        self._notified = False
