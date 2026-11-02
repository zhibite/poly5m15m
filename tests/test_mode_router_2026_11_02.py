"""Live/paper mode routing and mode-tagged trade records (2026-11-02)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.config import strategy as _strategy
from src.execution.mode_router import ModeRouter
from src.execution.paper_broker import PaperBroker
from src.market.market_ws import PriceCache
from src.market.models import MarketInfo
from src.persistence.db import DB, init_db
from src.strategy.engine import ActionOpenT1, ActionOpenTN, StrategyEngine
from src.strategy.round_state import RoundState
from src.web import status as status_mod


def _round():
    now = datetime.now(timezone.utc)
    return RoundState(
        round_start_ts=1_000,
        market=MarketInfo(
            asset="BTC", slug="btc-updown-15m-1000", condition_id="0xcid",
            token_up_id="UP", token_down_id="DOWN", tick_size=0.01,
            window_start=now, window_end=now,
        ),
    )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def __call__(self, action):
        self.calls.append((self.name, action))

    async def stop_all_watchers(self):
        self.calls.append((self.name + ":stop", None))


class _Live(_Recorder):
    name = "live"


class _Paper(_Recorder):
    name = "paper"
    async def cancel_all_open(self, reason="stop"):  # noqa: ARG002
        self.calls.append(("paper:cancel", None))


def _router(monkeypatch, *, paper: bool):
    monkeypatch.setattr(_strategy(), "PAPER_TRADING", paper)
    state = SimpleNamespace(engine=SimpleNamespace(_round=_round()))
    state.router = None
    live, paper_exec = _Live(), _Paper()
    router = ModeRouter(state, live, paper_exec)
    router.engine = state.engine
    return router, state, live, paper_exec


def test_mode_router_locks_the_round_mode_at_t1(monkeypatch) -> None:
    router, state, live, paper_exec = _router(monkeypatch, paper=True)
    round_state = state.engine._round
    t1 = ActionOpenT1(side="UP", token="UP", price=0.40, notional=1.0)
    tn = ActionOpenTN(
        tier=2, token="UP", price=0.32, notional=2.0, trigger_price=0.352
    )

    asyncio.run(router(t1))
    assert round_state.mode == "paper"
    assert paper_exec.calls and paper_exec.calls[0][0] == "paper"
    assert not live.calls

    # Switching the config mid-round must NOT move an open position.
    monkeypatch.setattr(_strategy(), "PAPER_TRADING", False)
    asyncio.run(router(tn))
    assert paper_exec.calls[-1][0] == "paper"
    assert not live.calls

    # A fresh round picks up the new configured mode.
    state.engine._round = _round()
    asyncio.run(router(t1))
    assert state.engine._round.mode == "live"
    assert live.calls and live.calls[-1][0] == "live"


def test_paper_t1_is_a_taker_fill_and_records_paper_mode(tmp_path) -> None:
    conn = init_db(tmp_path / "paper.db")
    db = DB(conn)
    pc = PriceCache()
    pc.midpoint["UP"] = 0.39
    mkt = MarketInfo(
        asset="BTC", slug="btc-updown-15m-1000", condition_id="0xcid",
        token_up_id="UP", token_down_id="DOWN", tick_size=0.01,
        window_start=datetime.now(timezone.utc),
        window_end=datetime.now(timezone.utc),
    )
    eng = StrategyEngine.__new__(StrategyEngine)
    eng._round = RoundState(round_start_ts=1_000, market=mkt)
    broker = PaperBroker(
        market="BTC-15m", asset="BTC", interval="15m",
        price_cache=pc, db=db, t1_entry_window_s=300,
    )
    broker.engine = eng
    broker.begin_round(1_000, mkt)

    asyncio.run(broker(ActionOpenT1(
        side="UP", token="UP", price=0.40, notional=2.0
    )))

    pos = eng._round.position
    # GTD size = notional / limit (2.0 / 0.40 = 5 shares), filled at mid.
    assert pos.total_qty == pytest.approx(2.0 / 0.40)
    assert pos.avg_cost == pytest.approx(0.39)

    rounds = status_mod.rounds_with_pnl(conn)
    assert rounds and rounds[0]["mode"] == "paper"
    fills = status_mod.recent_fills(conn)
    assert fills and fills[0]["mode"] == "paper"
    assert len(status_mod.recent_fills(conn, mode="paper")) == 1
    assert status_mod.recent_fills(conn, mode="live") == []
    conn.close()


def test_db_mode_filter_defaults_to_live_for_legacy_rows(tmp_path) -> None:
    conn = init_db(tmp_path / "legacy.db")
    db = DB(conn)
    db.upsert_round(round_start_ts=2_000, slug="live-round")
    db.insert_fill("f1", "o1", 2_000, "UP", "BUY", 0.40, 5.0, 1_000_000)
    r = status_mod.rounds_with_pnl(conn)[0]
    assert r["mode"] == "live"
    assert status_mod.recent_fills(conn)[0]["mode"] == "live"
    assert len(status_mod.rounds_with_pnl(conn, mode="paper")) == 0
    conn.close()


def test_paper_t2_is_a_resting_gtd_and_fills_on_cross(tmp_path) -> None:
    conn = init_db(tmp_path / "paper_t2.db")
    db = DB(conn)
    pc = PriceCache()
    pc.midpoint["UP"] = 0.39
    mkt = MarketInfo(
        asset="BTC", slug="btc-updown-15m-1000", condition_id="0xcid",
        token_up_id="UP", token_down_id="DOWN", tick_size=0.01,
        window_start=datetime.now(timezone.utc),
        window_end=datetime.now(timezone.utc),
    )
    eng = StrategyEngine.__new__(StrategyEngine)
    eng._round = RoundState(round_start_ts=1_000, market=mkt)
    broker = PaperBroker(
        market="BTC-15m", asset="BTC", interval="15m",
        price_cache=pc, db=db, t1_entry_window_s=300,
    )
    broker.engine = eng
    broker.begin_round(1_000, mkt)

    asyncio.run(broker(ActionOpenT1(
        side="UP", token="UP", price=0.40, notional=2.0
    )))
    # T2 pre-armed above its limit: the GTD rests.
    asyncio.run(broker(ActionOpenTN(
        tier=2, token="UP", price=0.312, notional=2.0,
        trigger_price=0.3432,
    )))
    assert len(broker._orders) == 1
    pc.midpoint["UP"] = 0.30
    broker.tick()

    pos = eng._round.position
    assert pos.tiers[1].size_filled == pytest.approx(2.0 / 0.312)
    assert pos.total_qty > 1.0 / 0.39
    fills = status_mod.recent_fills(conn)
    assert any(f["tier"] == "T2" and f["mode"] == "paper" for f in fills)
    conn.close()
