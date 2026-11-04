"""The file-based kill switch must work on a running process, not only at boot."""
from __future__ import annotations

import json

from src.config import ArbSettings
from src.engine import ArbEngine
from src.killswitch import KillSwitch


def _engine(path) -> tuple[ArbEngine, KillSwitch]:
    ks = KillSwitch(path=path)
    engine = ArbEngine(
        ArbSettings(), None, None, None, None, None, None, None, None,
        killswitch=ks,
    )
    return engine, ks


def test_killswitch_file_engages_and_releases_live(tmp_path):
    path = tmp_path / "arb_emergency_stop.json"
    engine, ks = _engine(path)

    assert engine._sync_killswitch() is False

    path.write_text(json.dumps({"active": True, "reason": "manual"}), encoding="utf-8")
    assert engine._sync_killswitch() is True
    assert ks.active is True
    assert ks.reason == "manual"

    path.unlink()
    assert engine._sync_killswitch() is False
    assert ks.active is False


def test_killswitch_corrupt_file_keeps_last_state(tmp_path):
    path = tmp_path / "arb_emergency_stop.json"
    engine, ks = _engine(path)
    path.write_text(json.dumps({"active": True}), encoding="utf-8")
    assert engine._sync_killswitch() is True

    path.write_text("{not json", encoding="utf-8")
    assert engine._sync_killswitch() is True  # fail safe: stays stopped
