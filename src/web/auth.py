"""Cookie-session authentication for the Chinese dashboard.

Single-operator, in-memory sessions. Credentials come from
``ArbSettings`` (``ARB_WEB_USERNAME`` / ``ARB_WEB_PASSWORD``, with the
legacy ``WEB_*`` names as a fallback).

The browser gets an HttpOnly, SameSite=Lax session cookie (Secure when
the request arrived over HTTPS). Sessions are lost on restart/redeploy,
which is intentional: there is no session table to maintain.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import Request, Response

from src.config import ArbSettings, arb_settings

SESSION_COOKIE = "arb_session"
DEFAULT_TTL_S = 7 * 24 * 3600
_MAX_FAILURES = 10
_FAILURE_WINDOW_S = 300.0


@dataclass
class Session:
    user: str
    expires_at: float


def session_ttl_s(cfg: ArbSettings | None = None) -> int:
    cfg = cfg or arb_settings()
    try:
        return max(60, int(cfg.WEB_SESSION_TTL_S))
    except Exception:  # noqa: BLE001
        return DEFAULT_TTL_S


def credentials_configured(cfg: ArbSettings | None = None) -> bool:
    cfg = cfg or arb_settings()
    return bool(cfg.effective_web_password)


def auth_required(cfg: ArbSettings | None = None) -> bool:
    """True when the whole dashboard (not just the config surface) needs login."""
    return credentials_configured(cfg)


def verify_credentials(
    username: str, password: str, cfg: ArbSettings | None = None
) -> bool:
    cfg = cfg or arb_settings()
    expected_password = cfg.effective_web_password
    if not expected_password:
        return False
    expected_user = cfg.effective_web_username
    return (
        secrets.compare_digest(username or "", expected_user or "")
        and secrets.compare_digest(password or "", expected_password)
    )


class SessionStore:
    """Per-app session and brute-force state."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}

    def _prune(self, now: float) -> None:
        for token in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            self._sessions.pop(token, None)
        for ip in list(self._failures):
            recent = [t for t in self._failures[ip] if now - t < _FAILURE_WINDOW_S]
            if recent:
                self._failures[ip] = recent
            else:
                self._failures.pop(ip, None)

    def create(self, user: str, ttl_s: int) -> str:
        now = time.time()
        self._prune(now)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = Session(user=user, expires_at=now + ttl_s)
        return token

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = time.time()
        self._prune(now)
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= now:
            self._sessions.pop(token, None)
            return None
        return session

    def delete(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def login_blocked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(ip, []) if now - t < _FAILURE_WINDOW_S]
        self._failures[ip] = recent
        return len(recent) >= _MAX_FAILURES

    def record_failure(self, ip: str) -> None:
        self._failures.setdefault(ip, []).append(time.time())

    def reset_failures(self, ip: str) -> None:
        self._failures.pop(ip, None)


# ---------------------------------------------------------------------------
# Request/cookie helpers
# ---------------------------------------------------------------------------


def is_https(request: Request) -> bool:
    """Trust the proxy's X-Forwarded-Proto (Coolify/Traefik terminates TLS)."""
    forwarded = request.headers.get("x-forwarded-proto", "")
    first = forwarded.split(",")[0].strip().lower()
    return request.url.scheme == "https" or first == "https"


def set_session_cookie(
    response: Response, token: str, request: Request, ttl_s: int
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=ttl_s,
        httponly=True,
        samesite="lax",
        secure=is_https(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"
