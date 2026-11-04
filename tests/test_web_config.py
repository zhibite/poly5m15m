"""Config page, runtime overrides, and stop/resume API tests."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from src import runtime_config
from src.config import ArbSettings
from src.web.app import create_app


def _status_file(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "mode": "paper",
                "stats": {},
                "pairs": [],
                "markets": [],
                "positions": [],
                "fills": [],
                "opportunities": [],
                "killswitch": {"active": False},
                "health": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def _settings(tmp_path) -> ArbSettings:
    return ArbSettings(
        PAPER_TRADING=True,
        WEB_USERNAME="admin",
        WEB_PASSWORD="secret",
        WEB_SESSION_TTL_S=3600,
        STATUS_PATH=str(_status_file(tmp_path)),
        KILLSWITCH_PATH=str(tmp_path / "kill.json"),
    )


def _logged_in(tmp_path, monkeypatch) -> tuple[TestClient, ArbSettings]:
    monkeypatch.setattr(runtime_config, "OVERRIDES_PATH", tmp_path / "runtime.json")
    cfg = _settings(tmp_path)
    client = TestClient(create_app(cfg))
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
    )
    assert r.status_code == 200
    return client, cfg


def test_config_requires_login(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "OVERRIDES_PATH", tmp_path / "runtime.json")
    client = TestClient(create_app(_settings(tmp_path)))
    r = client.get("/config", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")
    assert client.get("/api/config").status_code == 401


def test_config_page_is_chinese(tmp_path, monkeypatch):
    client, _ = _logged_in(tmp_path, monkeypatch)
    r = client.get("/config")
    assert r.status_code == 200
    assert "运行配置" in r.text
    assert "保存配置" in r.text
    assert "停止开新仓" in r.text


def test_update_runtime_config_and_persist(tmp_path, monkeypatch):
    client, cfg = _logged_in(tmp_path, monkeypatch)
    r = client.post(
        "/api/config",
        json={
            "values": {
                "MAX_NOTIONAL_PER_PAIR": 7.5,
                "ALLOW_COMPLETE_SET": False,
            }
        },
    )
    assert r.status_code == 200
    assert cfg.MAX_NOTIONAL_PER_PAIR == 7.5
    assert cfg.ALLOW_COMPLETE_SET is False

    saved = json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))
    assert saved["MAX_NOTIONAL_PER_PAIR"] == 7.5
    assert saved["ALLOW_COMPLETE_SET"] is False

    payload = client.get("/api/config").json()
    field = next(f for f in payload["fields"] if f["key"] == "MAX_NOTIONAL_PER_PAIR")
    assert field["value"] == 7.5


def test_read_only_and_invalid_values_are_rejected(tmp_path, monkeypatch):
    client, cfg = _logged_in(tmp_path, monkeypatch)
    assert client.post(
        "/api/config", json={"values": {"WEB_PORT": 9999}}
    ).status_code == 400
    assert client.post(
        "/api/config", json={"values": {"MAX_NOTIONAL_PER_PAIR": -1}}
    ).status_code == 400
    assert client.post(
        "/api/config", json={"values": {"NOT_A_FIELD": 1}}
    ).status_code == 400
    assert cfg.MAX_NOTIONAL_PER_PAIR == 20.0


def test_stop_and_resume_control(tmp_path, monkeypatch):
    client, _ = _logged_in(tmp_path, monkeypatch)
    assert client.get("/api/control").json()["active"] is False

    stopped = client.post("/api/control/stop")
    assert stopped.status_code == 200
    assert stopped.json()["active"] is True
    assert (tmp_path / "kill.json").exists()

    started = client.post("/api/control/start")
    assert started.status_code == 200
    assert started.json()["active"] is False
    assert not (tmp_path / "kill.json").exists()


def test_apply_overrides_on_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "OVERRIDES_PATH", tmp_path / "runtime.json")
    runtime_config.save_overrides({"MAX_NOTIONAL_PER_PAIR": 6.5})
    cfg = ArbSettings(PAPER_TRADING=True)
    applied = runtime_config.apply_overrides(cfg)
    assert "MAX_NOTIONAL_PER_PAIR" in applied
    assert cfg.MAX_NOTIONAL_PER_PAIR == 6.5
