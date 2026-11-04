"""File-backed emergency-stop switch for the arbitrage bot.

The state lives in ``data/arb_emergency_stop.json``. The engine reloads
that file every tick (see ``ArbEngine._sync_killswitch``), so writing it
stops a running process; deleting it releases the stop. The file also
survives a restart/redeploy, so the operator must explicitly resume.

Engaging the switch halts new orders only. Existing positions are still
settled normally; an emergency stop is not a market sell.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.infra.logging import get_logger

log = get_logger(__name__)

_DATA_DIR = Path(
    os.getenv("POLYMARKET_DATA_DIR")
    or (Path(__file__).resolve().parents[2] / "data")
)
STATE_PATH = Path(
    os.getenv("POLYGTD_KILLSWITCH_FILE") or (_DATA_DIR / "arb_emergency_stop.json")
)


@dataclass
class KillSwitch:
    """Shared flag read by the engine and the dashboard status."""

    active: bool = False
    since: float = 0.0
    reason: str = ""
    path: Path = field(default=STATE_PATH, repr=False)

    # ── mutations ───────────────────────────────────────────────────────
    def engage(self, reason: str = "manual") -> None:
        if not self.active:
            self.since = time.time()
        self.active = True
        self.reason = reason or "manual"
        self._save()

    def release(self) -> None:
        self.active = False
        self.since = 0.0
        self.reason = ""
        self._delete()

    # ── read ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "since": self.since or None,
            "reason": self.reason or None,
        }

    # ── persistence ─────────────────────────────────────────────────────
    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"active": self.active, "since": self.since, "reason": self.reason},
                indent=2,
            )
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".killswitch.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:  # noqa: BLE001
            log.warning("killswitch.persist_failed", error=str(exc))

    def _delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("killswitch.unlink_failed", error=str(exc))

    @classmethod
    def load(cls, path: Path | None = None) -> "KillSwitch":
        target = Path(path) if path is not None else STATE_PATH
        ks = cls(path=target)
        if not target.exists():
            return ks
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("killswitch.state_unreadable", path=str(target), error=str(exc))
            return ks
        if isinstance(raw, dict) and raw.get("active"):
            ks.active = True
            ks.since = float(raw.get("since") or 0.0)
            ks.reason = str(raw.get("reason") or "manual")
            log.warning(
                "killswitch.restored_stopped",
                since=ks.since,
                reason=ks.reason,
                note="bot stays stopped after restart until the file is removed",
            )
        return ks
