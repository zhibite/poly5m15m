"""FastAPI dashboard for the 5m/15m arbitrage bot.

The bot process starts this app on ``ARB_WEB_PORT`` (default 8031).
The engine writes ``data/arb_status.json`` once a second; this app reads
that snapshot, so the web layer never touches engine state directly.

Routes:
    GET  /                 Chinese dashboard
    GET  /trades           Chinese fills/positions page
    GET  /login            Chinese login page
    POST /api/auth/login   JSON login, sets HttpOnly cookie
    POST /api/auth/logout  clears the session
    GET  /api/auth/me      session state
    GET  /api/status       status JSON (login required when a password is set)
    GET  /api/health       public health probe
    GET  /healthz          public health probe
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config import ArbSettings, arb_settings
from src.infra.logging import get_logger
from src.web import auth

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version() -> str:
    """Content hash so a redeploy cannot pair new HTML with stale CSS/JS."""
    digest = hashlib.md5()
    try:
        for name in ("style.css", "auth-nav.js", "arb.js"):
            path = STATIC_DIR / name
            if path.exists():
                digest.update(path.read_bytes())
    except Exception:  # noqa: BLE001
        return "1"
    return digest.hexdigest()[:10] or "1"


ASSET_VERSION = _asset_version()


class LoginPayload(BaseModel):
    username: str = ""
    password: str = ""


def _safe_next(value: str | None) -> str:
    """Only allow same-site relative redirects (no open redirect)."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _read_template(name: str) -> str:
    path = TEMPLATES_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8").replace(
            "__ASSET_VERSION__", ASSET_VERSION
        )
    return f"<h1>{name} not found</h1>"


def _render(name: str) -> HTMLResponse:
    return HTMLResponse(content=_read_template(name))


def _load_status(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {"error": "状态文件尚未生成"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"状态文件读取失败：{exc}"}


def create_app(cfg: ArbSettings | None = None) -> FastAPI:
    cfg = cfg or arb_settings()
    app = FastAPI(
        title="Polymarket 5m/15m 套利看板",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    sessions = auth.SessionStore()
    status_path = cfg.status_path

    @app.middleware("http")
    async def _static_revalidate(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    def _session(request: Request):
        return sessions.get(request.cookies.get(auth.SESSION_COOKIE))

    def _login_redirect(request: Request) -> RedirectResponse | None:
        if auth.auth_required(cfg) and _session(request) is None:
            return RedirectResponse(
                f"/login?next={request.url.path}", status_code=302
            )
        return None

    def _require_api_auth(request: Request) -> None:
        if auth.auth_required(cfg) and _session(request) is None:
            raise HTTPException(status_code=401, detail="需要登录")

    # ── public probes ───────────────────────────────────────────────────
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/health")
    async def api_health() -> dict:
        return {"ok": True}

    # ── auth ────────────────────────────────────────────────────────────
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if _session(request) is not None:
            return RedirectResponse(
                _safe_next(request.query_params.get("next")), status_code=302
            )
        return _render("login.html")

    @app.get("/api/auth/me")
    async def api_auth_me(request: Request) -> dict:
        session = _session(request)
        return {
            "authenticated": session is not None,
            "user": session.user if session else None,
            "auth_required": auth.auth_required(cfg),
            "credentials_configured": auth.credentials_configured(cfg),
        }

    @app.post("/api/auth/login")
    async def api_auth_login(
        payload: LoginPayload, request: Request, response: Response
    ) -> dict:
        ip = auth.client_ip(request)
        if not auth.credentials_configured(cfg):
            raise HTTPException(
                status_code=503,
                detail="管理员尚未设置 ARB_WEB_PASSWORD（或 WEB_PASSWORD），无法登录。"
                       "请在部署环境变量中设置后重启。",
            )
        if sessions.login_blocked(ip):
            raise HTTPException(
                status_code=429, detail="登录失败次数过多，请 5 分钟后再试"
            )
        if not auth.verify_credentials(payload.username, payload.password, cfg):
            sessions.record_failure(ip)
            log.warning("web.login_failed", user=payload.username, ip=ip)
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        ttl = auth.session_ttl_s(cfg)
        token = sessions.create(payload.username, ttl)
        auth.set_session_cookie(response, token, request, ttl)
        sessions.reset_failures(ip)
        log.info("web.login_ok", user=payload.username, ip=ip)
        return {"ok": True, "user": payload.username}

    @app.post("/api/auth/logout")
    async def api_auth_logout(
        request: Request, response: Response
    ) -> dict:
        sessions.delete(request.cookies.get(auth.SESSION_COOKIE))
        auth.clear_session_cookie(response)
        return {"ok": True}

    # ── pages ───────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        redirect = _login_redirect(request)
        if redirect is not None:
            return redirect
        return _render("dashboard.html")

    @app.get("/trades", response_class=HTMLResponse)
    async def trades_page(request: Request):
        redirect = _login_redirect(request)
        if redirect is not None:
            return redirect
        return _render("trades.html")

    # ── API ─────────────────────────────────────────────────────────────
    @app.get("/api/status")
    async def api_status(request: Request) -> dict:
        _require_api_auth(request)
        return _load_status(status_path)

    return app
