"""Live-trading start latch (2026-11-02).

Every process starts PAUSED, regardless of how the previous process
stopped. A redeploy or crash restart must never resume live orders
without an explicit human action.

The state is intentionally in-memory only: the dashboard's
「恢复实盘交易」 control releases it for the CURRENT process, and the
next process start pauses again.

While paused:
  * no T1 entries and no averaging-down orders are placed;
  * take-profit and the T4 stop-loss keep running, so an existing
    position can still be reduced or exited.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TradingPause:
    """One process's live-trading start latch."""

    paused: bool = True
    since: float = field(default_factory=time.time)
    reason: str = "paused on process start"

    def engage(self, reason: str = "manual") -> None:
        if not self.paused:
            self.since = time.time()
        self.paused = True
        self.reason = reason or "manual"

    def release(self, reason: str = "dashboard") -> None:
        self.paused = False
        self.since = 0.0
        self.reason = reason or "dashboard"

    def snapshot(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "since": self.since or None,
            "reason": self.reason or None,
        }

    @classmethod
    def load(cls) -> "TradingPause":
        """Every new process starts paused."""
        return cls()


def load() -> TradingPause:
    """Return a fresh, paused latch for a new process."""
    return TradingPause()
