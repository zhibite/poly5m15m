"""Chinese dashboard login/session tests."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

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


def _client(tmp_path, password: str = "secret") -> TestClient:
    cfg = ArbSettings(
        PAPER_TRADING=True,
        WEB_USERNAME="admin",
        WEB_PASSWORD=password,
        WEB_SESSION_TTL_S=3600,
        STATUS_PATH=str(_status_file(tmp_path)),
    )
    return TestClient(create_app(cfg))


def test_health_is_public(tmp_path):
    client = _client(tmp_path)
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_dashboard_requires_login(tmp_path):
    client = _client(tmp_path)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")

    r = client.get("/api/status")
    assert r.status_code == 401
    assert r.json()["detail"] == "需要登录"


def test_login_page_is_chinese(tmp_path):
    client = _client(tmp_path)
    r = client.get("/login")
    assert r.status_code == 200
    assert "登录" in r.text
    assert "用户名" in r.text
    assert "密码" in r.text


def test_wrong_password_is_rejected(tmp_path):
    client = _client(tmp_path)
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrong"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "用户名或密码错误"


def test_login_dashboard_trades_and_logout(tmp_path):
    client = _client(tmp_path)
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
    )
    assert r.status_code == 200
    assert client.cookies.get("arb_session")

    page = client.get("/")
    assert page.status_code == 200
    assert "套利看板" in page.text
    assert "UP/DOWN 实时价格" in page.text
    assert "配对信号" in page.text
    assert "最近仓位" in page.text

    trades = client.get("/trades")
    assert trades.status_code == 200
    assert "成交记录" in trades.text
    assert "最近成交" in trades.text

    config = client.get("/config")
    assert config.status_code == 200
    assert "运行配置" in config.text
    assert "保存配置" in config.text

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["mode"] == "paper"

    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 302


def test_no_password_dashboard_is_public(tmp_path, monkeypatch):
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    client = _client(tmp_path, password="")
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 200
    # The login page stays reachable and explains that no password is set.
    assert client.get("/login").status_code == 200
