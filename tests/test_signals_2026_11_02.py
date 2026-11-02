"""External-signal gates (2026-11-02).

Covers the digital-option model/edge, side-aware adverse z, trend and
volatility regime, CLOB microstructure/CVD, 5m/macro/event vetoes, the
log-only mode and the engine's T1 veto.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.config import strategy as _strategy
from src.feeds.binance_spot import RingBuffer
from src.market.market_ws import PriceCache
from src.market.models import MarketInfo
from src.strategy.engine import ActionOpenT1, StrategyEngine
from src.strategy.event_calendar import EventCalendar
from src.strategy.gatekeeper import GateKeeper
from src.strategy.round_state import RoundState
from src.strategy.signals import RegimeState, SignalDecision, SignalEngine


def _engine(pc: PriceCache | None = None, ring: RingBuffer | None = None):
    return SignalEngine(
        ring=ring or RingBuffer(retention_s=1200),
        price_cache=pc or PriceCache(),
    )


def _flat_regime(engine: SignalEngine, trend: float = 0.0) -> None:
    def _fake(sigma_hint: float) -> RegimeState:
        return RegimeState(
            trend_score=trend,
            sigma_1m=max(0.0005, float(sigma_hint or 0.0)),
            bars=10,
            ts=time.time(),
        )

    engine._refresh_regime = _fake  # type: ignore[method-assign]


def test_digital_model_fair_value_and_edge() -> None:
    engine = _engine()
    _flat_regime(engine)

    at_strike = engine.evaluate(
        kind="T2", side="UP", token="UP", mid=0.35,
        spot=100.0, strike=100.0, remaining_s=600.0, sigma_1m=0.001,
    )
    assert at_strike.snapshot["fair_up"] == pytest.approx(0.5, abs=1e-6)
    assert at_strike.snapshot["edge"] == pytest.approx(0.15, abs=1e-6)
    assert at_strike.snapshot["adverse_z"] == pytest.approx(0.0, abs=1e-6)
    assert at_strike.allowed is True

    # Spot well below the strike: UP is now the adverse side.
    up_bad = engine.evaluate(
        kind="T2", side="UP", token="UP", mid=0.35,
        spot=97.0, strike=100.0, remaining_s=600.0, sigma_1m=0.001,
    )
    assert up_bad.snapshot["adverse_z"] > 2.0
    assert "strike_z" in up_bad.reasons
    assert "edge" in up_bad.reasons
    assert up_bad.allowed is False

    # The same spot confirms DOWN instead.
    down_ok = engine.evaluate(
        kind="T2", side="DOWN", token="DOWN", mid=0.35,
        spot=97.0, strike=100.0, remaining_s=600.0, sigma_1m=0.001,
    )
    assert down_ok.snapshot["adverse_z"] < 0
    assert "strike_z" not in down_ok.reasons
    assert down_ok.snapshot["edge"] > 0.0


def test_trend_gate_and_log_only(monkeypatch) -> None:
    engine = _engine()
    _flat_regime(engine, trend=-0.9)  # strong downtrend
    cfg = _strategy()
    monkeypatch.setattr(cfg, "SIGNAL_MAX_TREND_AGAINST", 0.70)
    monkeypatch.setattr(cfg, "SIGNAL_GATES_LOG_ONLY", False)

    blocked = engine.evaluate(
        kind="T2", side="UP", token="UP", mid=0.35,
        spot=100.0, strike=100.0, remaining_s=600.0, sigma_1m=0.001,
    )
    assert "trend_against" in blocked.reasons
    assert blocked.allowed is False

    monkeypatch.setattr(cfg, "SIGNAL_GATES_LOG_ONLY", True)
    logged = engine.evaluate(
        kind="T2", side="UP", token="UP", mid=0.35,
        spot=100.0, strike=100.0, remaining_s=600.0, sigma_1m=0.001,
    )
    assert "trend_against" in logged.reasons
    assert logged.would_block is True
    assert logged.allowed is True
    assert logged.mode == "log"


def test_microstructure_and_cvd_gate() -> None:
    pc = PriceCache()
    pc.set_best_bid_ask("UP", 0.30, 0.40)  # 28.6% spread
    pc.bid_depth["UP"] = 10.0
    pc.ask_depth["UP"] = 100.0
    pc.record_trade("UP", -50.0)
    pc.record_trade("UP", -50.0)

    engine = _engine(pc)
    _flat_regime(engine)
    decision = engine.evaluate(
        kind="T2", side="UP", token="UP", mid=0.35,
        spot=100.0, strike=100.0, remaining_s=600.0, sigma_1m=0.001,
    )
    assert "spread_wide" in decision.reasons
    assert "book_ask_heavy" in decision.reasons
    assert "sell_flow" in decision.reasons
    assert decision.allowed is False


def test_5m_macro_and_event_vetoes(monkeypatch) -> None:
    class _Fake5m:
        def snapshot(self, side):  # noqa: ARG002
            return {
                "available": True, "up_mid": 0.20, "down_mid": 0.80,
                "up_change_60": -0.10, "down_change_60": 0.10,
                "confirm": -0.90,
            }

    class _FakeMacro:
        def snapshot(self):
            return SimpleNamespace(to_dict=lambda: {
                "funding_rate": 0.002,
                "open_interest": 1.0,
                "oi_change_pct": 8.0,
                "dvol": 90.0,
                "dvol_change_pct": 30.0,
                "errors": [],
            })

    class _FakeEvents:
        def snapshot(self):
            return {
                "event_blackout": True,
                "event_name": "FOMC",
                "event_end": time.time() + 60,
            }

    engine = SignalEngine(
        ring=RingBuffer(retention_s=1200),
        price_cache=PriceCache(),
        macro=_FakeMacro(),
        tf5m=_Fake5m(),
        events=_FakeEvents(),
    )
    _flat_regime(engine)
    decision = engine.evaluate(
        kind="T2", side="UP", token="UP", mid=0.35,
        spot=100.0, strike=100.0, remaining_s=600.0, sigma_1m=0.001,
    )
    assert "5m_against" in decision.reasons
    assert "funding_extreme" in decision.reasons
    assert "oi_spike" in decision.reasons
    assert "dvol_extreme" in decision.reasons
    assert any(r.startswith("event_blackout") for r in decision.reasons)
    assert decision.allowed is False


def test_5m_gate_can_be_disabled_for_t1_only(monkeypatch) -> None:
    class _Bearish5m:
        def snapshot(self, side):  # noqa: ARG002
            return {"available": True, "confirm": -0.90}

    engine = SignalEngine(
        ring=RingBuffer(retention_s=1200),
        price_cache=PriceCache(),
        tf5m=_Bearish5m(),
    )
    _flat_regime(engine)
    cfg = _strategy()
    monkeypatch.setattr(cfg, "SIGNAL_GATES_LOG_ONLY", False)
    monkeypatch.setattr(cfg, "SIGNAL_5M_GATE_ENABLED", True)
    monkeypatch.setattr(cfg, "SIGNAL_5M_T1_ENABLED", False)

    t1 = engine.evaluate(
        kind="T1", side="UP", token="UP", mid=0.35,
        spot=100.0, strike=100.0, remaining_s=600.0, sigma_1m=0.001,
    )
    assert "5m_against" not in t1.reasons
    assert t1.allowed is True

    t2 = engine.evaluate(
        kind="T2", side="UP", token="UP", mid=0.35,
        spot=100.0, strike=100.0, remaining_s=600.0, sigma_1m=0.001,
    )
    assert "5m_against" in t2.reasons
    assert t2.allowed is False

    monkeypatch.setattr(cfg, "SIGNAL_5M_T1_ENABLED", True)
    t1_on = engine.evaluate(
        kind="T1", side="UP", token="UP", mid=0.35,
        spot=100.0, strike=100.0, remaining_s=600.0, sigma_1m=0.001,
    )
    assert "5m_against" in t1_on.reasons
    assert t1_on.allowed is False


def test_event_calendar_loads_and_matches(tmp_path) -> None:
    path = tmp_path / "events.json"
    now = time.time()
    path.write_text(json.dumps({
        "events": [{
            "name": "CPI",
            "start_ts": now - 60,
            "end_ts": now + 60,
        }]
    }), encoding="utf-8")
    cal = EventCalendar(
        path=path, pre_buffer_s=0.0, post_buffer_s=0.0
    )
    assert cal.active(now)["name"] == "CPI"
    assert cal.active(now + 3600) is None


class _StubSignals:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def evaluate(self, **kwargs):  # noqa: ARG002
        return SignalDecision(
            allowed=self.allowed,
            would_block=not self.allowed,
            mode="enforce",
            reasons=() if self.allowed else ("test_veto",),
            size_multiplier=1.0,
            snapshot={"ts": time.time()},
        )


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


def test_engine_t1_is_vetoed_by_signal_gate() -> None:
    actions: list[object] = []

    async def handler(action: object) -> None:
        actions.append(action)

    eng = StrategyEngine(
        _Binance(), _Prices(), handler, signals=_StubSignals(False)
    )
    asyncio.run(_evaluate_once(eng))
    assert actions == []
    assert eng._round.position.entry_token is None

    eng2 = StrategyEngine(
        _Binance(), _Prices(), handler, signals=_StubSignals(True)
    )
    actions.clear()
    asyncio.run(_evaluate_once(eng2))
    assert any(isinstance(a, ActionOpenT1) for a in actions)


# ---------------------------------------------------------------------------
# Backtest replay mirrors the spot/regime gates
# ---------------------------------------------------------------------------

_BT_START = 1_800_000_000


def _btc_bars(overrides: dict[int, float]) -> list:
    from src.backtest.model import BtcBar

    bars = []
    for off in range(-180, 1080, 60):
        p = overrides.get(off, 100_000.0)
        bars.append(BtcBar((_BT_START + off) * 1000, p, p * 1.0001,
                           p * 0.9999, p))
    return bars


def _btc_window(up_path: dict[int, float], btc: list):
    from src.backtest.model import PricePoint, WindowData

    offsets = sorted(up_path)
    up, down = [], []
    last = None
    for off in range(-60, 960, 5):
        for o in offsets:
            if o <= off:
                last = up_path[o]
        if last is None:
            continue
        up.append(PricePoint((_BT_START + off) * 1000, last))
        down.append(PricePoint((_BT_START + off) * 1000, 1.0 - last))
    return WindowData(
        start_ts=_BT_START, slug="btc-updown-15m-test", up_won=True,
        up=up, down=down, btc=btc,
    )


def test_backtest_signal_gate_blocks_a_trending_t1() -> None:
    from src.backtest.model import Params, replay

    w = _btc_window(
        {0: 0.50, 70: 0.39, 900: 0.39},
        _btc_bars({off: 99_900.0 for off in range(60, 1080, 60)}),
    )
    off = replay(w, Params(signal_gates_enabled=False))
    assert off.side == "UP"

    on = replay(w, Params(signal_gates_enabled=True))
    assert on.side is None
    assert on.exit == "NO_ENTRY"


def test_backtest_signal_gate_blocks_a_trending_t2() -> None:
    from src.backtest.model import Params, replay

    w = _btc_window(
        {0: 0.50, 10: 0.39, 60: 0.39, 70: 0.30, 900: 0.30},
        _btc_bars({off: 99_900.0 for off in range(60, 1080, 60)}),
    )
    off = replay(w, Params(signal_gates_enabled=False))
    assert any(f.tier == "T2" and f.shares > 0 for f in off.fills)

    on = replay(w, Params(signal_gates_enabled=True))
    assert off.side == "UP"
    assert not any(f.tier == "T2" and f.shares > 0 for f in on.fills)
