"""Live-trading start latch (2026-11-02).

Every process must start with new live entries paused. These tests pin:
  * the latch default / engage / release semantics (in-memory only);
  * the engine gates T1 and averaging on the latch while leaving TP/SL;
  * the dashboard control endpoints pause/resume the latch.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.market.models import MarketInfo
from src.strategy.engine import ActionOpenT1, StrategyEngine
from src.strategy.gatekeeper import GateKeeper
from src.strategy.round_state import RoundState
from src.strategy.trading_pause import TradingPause


def test_latch_defaults_to_paused_and_toggles() -> None:
    latch = TradingPause()
    assert latch.paused is True
    assert latch.snapshot()["paused"] is True

    latch.release("dashboard")
    assert latch.paused is False
    assert latch.snapshot()["since"] is None

    latch.engage("test")
    assert latch.paused is True
    assert latch.snapshot()["reason"] == "test"


def test_engine_honours_latch_and_legacy_config() -> None:
    eng = StrategyEngine.__new__(StrategyEngine)
    eng._pause = TradingPause(paused=True)
    eng._s = SimpleNamespace(PAUSE_NEW_ENTRIES=False)
    assert eng._trading_paused() is True

    eng._pause.paused = False
    assert eng._trading_paused() is False

    # Legacy PAUSE_NEW_ENTRIES still blocks entries.
    eng._s = SimpleNamespace(PAUSE_NEW_ENTRIES=True)
    assert eng._trading_paused() is True


class _Binance:
    last_price = 64_000.0
    ts_ms = 0


class _Prices:
    def get_midpoint(self, token: str) -> float:  # noqa: ARG002
        return 0.39


async def _evaluate_once(eng: StrategyEngine) -> None:
    now = datetime.now(timezone.utc)
    eng._round = RoundState(
        round_start_ts=int(time.time()) - 5,
        market=MarketInfo(
            asset="BTC",
            slug="btc-updown-15m-test",
            condition_id="0xdeadbeef",
            token_up_id="UP",
            token_down_id="DOWN",
            tick_size=0.01,
            window_start=now,
            window_end=now + timedelta(minutes=15),
        ),
    )
    eng._gate = GateKeeper(64_000.0)
    await eng._evaluate()


def test_engine_does_not_open_t1_while_paused_then_does_after_resume() -> None:
    actions: list[object] = []

    async def handler(action: object) -> None:
        actions.append(action)

    latch = TradingPause()
    eng = StrategyEngine(
        _Binance(), _Prices(), handler, trading_pause=latch
    )

    asyncio.run(_evaluate_once(eng))
    assert actions == [], "T1 must not fire while the start latch is paused"
    assert eng._round.position.entry_token is None

    latch.release("dashboard")
    asyncio.run(_evaluate_once(eng))
    assert any(isinstance(a, ActionOpenT1) for a in actions), (
        "T1 must fire after an explicit resume"
    )


def test_control_endpoints_pause_and_resume() -> None:
    from src.config import strategy
    from src.web import app as app_mod
    from src.web import auth

    old_user, old_pass = strategy().WEB_USERNAME, strategy().WEB_PASSWORD
    strategy().WEB_USERNAME = "admin"
    strategy().WEB_PASSWORD = "test-pass"
    auth._sessions.clear()
    auth._failures.clear()

    latch = TradingPause()
    fake_state = SimpleNamespace(
        trading_pause=latch,
        killswitch=SimpleNamespace(
            active=False, since=0.0, reason="",
            snapshot=lambda: {"active": False, "since": None, "reason": None},
        ),
    )
    old_state = app_mod._bot_state
    app_mod.set_state(fake_state)
    try:
        client = TestClient(app_mod.app)

        # Login is mandatory for controls.
        assert client.post("/api/control/resume").status_code == 401
        r = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test-pass"},
        )
        assert r.status_code == 200

        assert latch.paused is True
        r = client.post("/api/control/resume")
        assert r.status_code == 200
        assert latch.paused is False
        assert r.json()["trading_pause"]["effective_paused"] is False

        r = client.post("/api/control/pause", json={"reason": "test"})
        assert r.status_code == 200
        assert latch.paused is True
        assert client.get("/api/control").json()[
            "trading_pause"
        ]["paused"] is True
    finally:
        app_mod.set_state(old_state)
        strategy().WEB_USERNAME = old_user
        strategy().WEB_PASSWORD = old_pass
        auth._sessions.clear()
        auth._failures.clear()
