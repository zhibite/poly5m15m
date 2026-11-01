"""Paper-trading multi-market coverage (2026-11-01).

The bot now runs 7 assets × 5m/15m with all four martingale tiers as GTD
BUY orders, no stop-loss, and per-(market, round) trade records. These
tests pin the paper fill model and the market-scoped history queries.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.config import strategy as _strategy
from src.execution.paper_broker import PaperBroker
from src.markets import market_spec, market_specs
from src.persistence.db import DB, init_db
from src.strategy.engine import StrategyEngine
from src.strategy.gatekeeper import GateKeeper
from src.strategy.round_state import Position, RoundState
from src.web import status as status_mod
from src.market.market_ws import PriceCache


UP = "UPTOKEN"
DOWN = "DOWNTOKEN"


class _Cache:
    def __init__(self, price: float = 100_000.0) -> None:
        self.last_price = price
        self.ts_ms = int(time.time() * 1000)


class _Mkt:
    def __init__(self, *, window_sec: int = 900, round_ts: int | None = None) -> None:
        now = time.time()
        self.token_up_id = UP
        self.token_down_id = DOWN
        # RoundState reads the MarketInfo alias properties.
        self.up_token = UP
        self.down_token = DOWN
        self.slug = "test-market"
        self.condition_id = "0xcid"
        self.tick_size = 0.01
        self.round_start_ts = int(now // window_sec * window_sec) if round_ts is None else round_ts
        self.window_start = datetime.fromtimestamp(
            self.round_start_ts, tz=timezone.utc
        )
        self.window_end = self.window_start + timedelta(seconds=window_sec)
        self.btc_spot_at_window_open = 100_000.0
        self.btc_spot_now = 100_000.0
        self._up_mid = 0.5
        self._down_mid = 0.5


def _make_engine(
    *, window_sec: int = 900, t1_window: float = 300.0, db=None
) -> tuple[StrategyEngine, PaperBroker, PriceCache]:
    pc = PriceCache()
    pc.midpoint[UP] = 0.42
    pc.midpoint[DOWN] = 0.58
    broker = PaperBroker(
        market="BTC-15m" if window_sec == 900 else "BTC-5m",
        asset="BTC",
        interval="15m" if window_sec == 900 else "5m",
        price_cache=pc,
        db=db,
        t1_entry_window_s=t1_window,
    )
    eng = StrategyEngine(
        _Cache(),
        pc,
        broker,
        t1_entry_window_s=t1_window,
        t4_min_remaining_s=80 if window_sec == 300 else 240,
        t4_drift_bypass_min_remaining_s=180 if window_sec == 300 else 540,
    )
    broker.engine = eng
    # Start the round "now" so the T1 time gate is open regardless of
    # where the wall clock sits inside a 15-minute boundary.
    mkt = _Mkt(window_sec=window_sec, round_ts=int(time.time()))
    rs = RoundState(round_start_ts=mkt.round_start_ts, market=mkt)
    eng._round = rs
    eng._gate = GateKeeper(100_000.0)
    broker.begin_round(rs.round_start_ts, mkt)
    return eng, broker, pc


# ---------------------------------------------------------------------------
# Market catalog
# ---------------------------------------------------------------------------


def test_catalog_has_seven_assets_and_both_intervals() -> None:
    specs = market_specs()
    keys = {s.key for s in specs}
    for asset in ("BTC", "ETH", "BNB", "DOGE", "SOL", "XRP", "HYPE"):
        assert f"{asset}-5m" in keys
        assert f"{asset}-15m" in keys
    assert len(specs) == 14
    for spec in specs:
        if spec.interval == "5m":
            assert spec.t1_entry_window_s == pytest.approx(60.0)
            assert spec.t4_min_remaining_s == pytest.approx(80.0)
        else:
            assert spec.t1_entry_window_s == pytest.approx(300.0)
            assert spec.t4_min_remaining_s == pytest.approx(240.0)


def test_strategy_defaults_are_gtd_no_stop() -> None:
    cfg = _strategy()
    assert cfg.tier_notionals == [2.0, 4.0, 8.0, 16.0]
    assert cfg.STOP_LOSS_ENABLED is False
    assert cfg.TIER2_DROP_PCT == pytest.approx(0.20)
    assert cfg.TIER3_DROP_PCT == pytest.approx(0.40)
    assert cfg.TIER4_DROP_PCT == pytest.approx(0.60)
    assert cfg.TP_PROFIT_PCT == pytest.approx(0.20)
    assert cfg.TP_DECREMENT_PCT == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# Paper fill model
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_t1_is_a_resting_gtd_and_fills_when_mid_crosses() -> None:
    eng, broker, pc = _make_engine()
    _run(eng._evaluate())

    # Pre-armed at mid 0.42 (cap 0.40 × 1.10 = 0.44) but NOT filled: the
    # GTD rests at 0.40 and waits for the mid to cross.
    assert eng._round.position.entry_token == UP
    assert len(broker._orders) == 1
    order = next(iter(broker._orders.values()))
    assert order.side == "BUY" and order.price == pytest.approx(0.40)
    assert eng._round.position.total_qty == 0.0

    pc.midpoint[UP] = 0.39
    broker.tick()
    pos = eng._round.position
    # 2u at 0.40 = 5 shares, maker fill at the limit.
    assert pos.total_qty == pytest.approx(5.0)
    assert pos.avg_cost == pytest.approx(0.40)
    assert broker._orders == {}


def test_average_down_then_take_profit() -> None:
    eng, broker, pc = _make_engine()

    # T1 resting → fill at 0.40.
    _run(eng._evaluate())
    pc.midpoint[UP] = 0.39
    broker.tick()
    pos = eng._round.position
    assert pos.total_qty == pytest.approx(5.0)

    # T2 fires when mid drops into the pre-arm zone (0.32 × 1.10 = 0.352).
    pc.midpoint[UP] = 0.34
    _run(eng._evaluate())
    t2 = [o for o in broker._orders.values() if o.tier == 2]
    assert len(t2) == 1
    assert t2[0].price == pytest.approx(0.32)
    assert broker._orders[t2[0].order_id].side == "BUY"

    # Mid crosses the T2 limit → maker fill, 4u / 0.32 = 12.5 shares.
    pc.midpoint[UP] = 0.31
    broker.tick()
    pos = eng._round.position
    assert pos.total_qty == pytest.approx(17.5)
    assert pos.avg_cost == pytest.approx(6.0 / 17.5)

    # N=1 averaging tier → TP target = avg × 1.17.
    target = pos.tp_target()
    assert target == pytest.approx((6.0 / 17.5) * 1.17)
    fire_line = target * 0.90
    pc.midpoint[UP] = fire_line + 0.001
    _run(eng._evaluate())
    tps = [o for o in broker._orders.values() if o.side == "SELL"]
    assert len(tps) == 1
    assert tps[0].price == pytest.approx(target)

    # Mid crosses the TP target → full exit, position flat.
    pc.midpoint[UP] = target + 0.001
    broker.tick()
    pos = eng._round.position
    assert pos.total_qty == pytest.approx(0.0)
    assert broker._orders == {}


def test_t1_gtd_expiry_releases_entry_slot_for_a_retry() -> None:
    eng, broker, pc = _make_engine()
    _run(eng._evaluate())
    order = next(iter(broker._orders.values()))
    order.expires_at = time.time() - 1
    broker.tick()

    pos = eng._round.position
    assert pos.entry_token is None and pos.entry_side is None
    assert broker._orders == {}

    # Still inside the T1 window (round started seconds ago) → retry.
    _run(eng._evaluate())
    assert eng._round.position.entry_token == UP
    assert len(broker._orders) == 1


def test_5m_t1_window_closes_after_one_minute() -> None:
    eng, broker, pc = _make_engine(window_sec=300, t1_window=60.0)
    eng._round.round_start_ts = int(time.time()) - 61
    _run(eng._evaluate())
    assert broker._orders == {}
    assert eng._round.position.entry_token is None


def test_resting_t1_is_cancelled_at_the_entry_deadline() -> None:
    """The 5m 'enter within 1 minute' cutoff also applies to a resting GTD."""
    eng, broker, pc = _make_engine(window_sec=300, t1_window=60.0)
    _run(eng._evaluate())
    order = next(iter(broker._orders.values()))
    assert order.tier == 1
    # Pretend the order was placed 61 s ago; the deadline has passed.
    order.round_start_ts = int(time.time()) - 61
    broker.tick()
    assert broker._orders == {}
    assert eng._round.position.entry_token is None


def test_no_stop_loss_even_after_t4() -> None:
    """STOP_LOSS_ENABLED=false must keep the losing ladder untouched."""
    eng, broker, pc = _make_engine()
    _run(eng._evaluate())
    pc.midpoint[UP] = 0.39
    broker.tick()
    # Force a fake T4 fill in the tier state.
    pos = eng._round.position
    pos.tiers[3].placed = True
    pos.tiers[3].filled = True
    pos.tiers[3].size_filled = 100.0
    pos.tiers[3].cost_filled = 5.0
    pos.record_fill(pos.tiers[3], 0.05, 100.0)
    pc.midpoint[UP] = 0.01
    _run(eng._evaluate())
    assert not [o for o in broker._orders.values() if o.side == "SELL"]
    assert pos.total_qty > 0


# ---------------------------------------------------------------------------
# Per-market history + full-loss settlement
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "paper.db")
    yield c
    c.close()


def test_rounds_are_keyed_by_market_at_the_same_timestamp(conn) -> None:
    """A 5m and a 15m round can share round_start_ts; records must not merge."""
    db = DB(conn)
    ts = 1_000
    for market, asset, interval, token in (
        ("BTC-15m", "BTC", "15m", "BTC15UP"),
        ("BTC-5m", "BTC", "5m", "BTC5UP"),
    ):
        db.upsert_round(
            round_start_ts=ts, slug=f"slug-{market}", condition_id="0xcid",
            up_token=token, down_token=token + "D",
            market=market, asset=asset, interval=interval,
        )
        db.insert_fill(
            "f-" + market, "o-" + market, ts, token, "BUY", 0.40, 5.0,
            1_000_000, market=market, asset=asset, interval=interval,
        )
    rounds = status_mod.rounds_with_pnl(conn)
    assert len(rounds) == 2
    assert {r["market"] for r in rounds} == {"BTC-15m", "BTC-5m"}
    assert {r["interval"] for r in rounds} == {"15m", "5m"}

    only_5m = status_mod.rounds_with_pnl(conn, market="BTC-5m")
    assert len(only_5m) == 1
    assert only_5m[0]["asset"] == "BTC" and only_5m[0]["interval"] == "5m"

    fills = status_mod.recent_fills(conn, market="BTC-5m")
    assert len(fills) == 1 and fills[0]["market"] == "BTC-5m"


def test_build_status_scopes_to_the_requested_market() -> None:
    class _WM:
        def __init__(self, mkt):
            self._mkt = mkt

        def get(self, asset):  # noqa: ARG002
            return SimpleNamespace(market=self._mkt)

        def window_period_s(self) -> int:
            return 300

        def seconds_until_next(self) -> float:
            return 120.0

    mkt = _Mkt(window_sec=300)
    pc = PriceCache()
    pc.midpoint[UP] = 0.35
    pc.midpoint[DOWN] = 0.65
    spec = market_spec("BTC", "5m")
    eng = SimpleNamespace(
        _round=SimpleNamespace(
            round_start_ts=mkt.round_start_ts, slug=mkt.slug,
            position=None, skip_reason=None,
        ),
        _gate=None,
    )
    rt = SimpleNamespace(
        spec=spec, wm=_WM(mkt), engine=eng, broker=None,
        current_market=mkt, binance_cache=_Cache(),
    )
    state = SimpleNamespace(
        markets={"BTC-5m": rt},
        price_cache=pc,
        asset_caches={"BTC": _Cache()},
        binance_cache=_Cache(),
        order_manager=None,
    )
    out = status_mod.build_status(state, market="BTC-5m")
    assert out["market"] == "BTC-5m"
    assert out["asset"] == "BTC"
    assert out["interval"] == "5m"
    assert out["prices"]["up_mid"] == pytest.approx(0.35)
    assert out["window"]["period_s"] == 300
    assert out["config"]["t1_entry_window_s"] == pytest.approx(60.0)


def test_resolution_loop_scopes_by_market() -> None:
    from src.main import _resolve_rounds_once

    class _PM:
        def __init__(self):
            self.calls = []

        async def get_market_resolution(self, cid, slug=None):
            self.calls.append((cid, slug))
            return {"resolved": True, "up_wins": True}

    class _DB:
        def __init__(self):
            self.recorded = []

        def unresolved_rounds(self, **_kw):
            return [
                # (round_ts, cid, slug, up, down, market, asset, interval)
                (1_000, "0x5m", "slug-5m", "UP", "DOWN", "BTC-5m", "BTC", "5m"),
                (1_000, "0x15m", "slug-15m", "UP", "DOWN", "BTC-15m", "BTC", "15m"),
            ]

        def set_round_resolution(self, round_ts, up_won, *, market="BTC-15m"):
            self.recorded.append((round_ts, up_won, market))

    pm, db = _PM(), _DB()
    state = SimpleNamespace(pm_client=pm, db=db)
    resolved = asyncio.run(_resolve_rounds_once(state, {}))
    assert resolved == 2
    assert {r[2] for r in db.recorded} == {"BTC-5m", "BTC-15m"}


def test_full_ladder_end_to_end_without_stop_loss(conn) -> None:
    """Drive T1→T4 through the paper broker, then settle as a full loss."""
    db = DB(conn)
    eng, broker, pc = _make_engine(db=db)
    round_ts = eng._round.round_start_ts

    # T1 @0.40, T2 @0.32, T3 @0.24, T4 @0.16; each tier is a resting GTD.
    for arm_mid, fill_mid in (
        (0.42, 0.39),   # T1
        (0.34, 0.31),   # T2
        (0.25, 0.23),   # T3
        (0.17, 0.15),   # T4
    ):
        pc.midpoint[UP] = arm_mid
        _run(eng._evaluate())
        assert broker._orders, f"tier was not armed at mid={arm_mid}"
        pc.midpoint[UP] = fill_mid
        broker.tick()

    pos = eng._round.position
    assert all(t.is_complete for t in pos.tiers)
    assert pos.total_cost == pytest.approx(30.0, abs=0.01)
    assert pos.total_qty == pytest.approx(5 + 12.5 + 8 / 0.24 + 100, rel=1e-6)
    assert broker._orders == {}

    # No stop-loss: a collapse to 0.01 produces no SELL order.
    pc.midpoint[UP] = 0.01
    _run(eng._evaluate())
    assert not [o for o in broker._orders.values() if o.side == "SELL"]

    # Settlement: the market loses, every share is worthless.
    db.set_round_resolution(round_ts, up_won=False, market="BTC-15m")
    row = status_mod.rounds_with_pnl(
        conn, market="BTC-15m"
    )[0]
    assert row["status"] == "closed"
    assert row["settlement_usd"] == pytest.approx(-pos.total_cost)
    assert row["pnl_usd"] == pytest.approx(-pos.total_cost)
    assert row["fill_count"] == 4


def test_unsold_full_ladder_books_the_entire_loss(conn) -> None:
    """All four tiers filled, market loses, no SELL → full 30u loss booked."""
    db = DB(conn)
    round_ts = int(time.time()) - 900
    market = "BTC-15m"
    db.upsert_round(
        round_start_ts=round_ts, slug="lost", condition_id="0xcid",
        up_token="UP", down_token="DOWN",
        market=market, asset="BTC", interval="15m",
    )
    # 2 + 4 + 8 + 16 = 30u deployed across the four fills.
    fills = [
        ("T1", 0.40, 5.0),      # 2.0
        ("T2", 0.32, 12.5),     # 4.0
        ("T3", 0.24, 33.3333),  # 8.0
        ("T4", 0.16, 100.0),    # 16.0
    ]
    for i, (tier, px, sz) in enumerate(fills):
        db.insert_order(
            client_order_id=f"o{i}", server_order_id=f"o{i}",
            round_start_ts=round_ts, side="BUY", token_id="UP",
            tier_name=tier, price=px, size=sz, lifetime_s=300,
            expires_at=round_ts + 300, placed_at=round_ts,
            market=market, asset="BTC", interval="15m",
        )
        db.insert_fill(
            f"f{i}", f"o{i}", round_ts, "UP", "BUY", px, sz,
            ts_ms=(round_ts + i) * 1000,
            market=market, asset="BTC", interval="15m",
        )
    db.set_round_resolution(round_ts, up_won=False, market=market)

    r = status_mod.rounds_with_pnl(conn)[0]
    assert r["status"] == "closed"
    assert r["resolved"] is True and r["up_won"] is False
    # Everything is lost at settlement; PnL is the full invested notional.
    assert r["settlement_usd"] == pytest.approx(-30.0)
    assert r["pnl_usd"] == pytest.approx(-30.0)
    assert r["open_qty"] == 0.0


def test_sell_fill_shows_its_own_realized_pnl(conn) -> None:
    """成交明细 SELL rows carry the fill's realised PnL vs round avg cost."""
    db = DB(conn)
    round_ts = 5_000
    db.upsert_round(
        round_start_ts=round_ts, slug="tp-round", condition_id="0xcid",
        up_token="UP", down_token="DOWN",
        market="BTC-15m", asset="BTC", interval="15m",
    )
    db.insert_order(
        client_order_id="c1", server_order_id="c1", round_start_ts=round_ts,
        side="BUY", token_id="UP", tier_name="T1", price=0.40, size=5.0,
        lifetime_s=300, expires_at=round_ts + 300, placed_at=round_ts,
        market="BTC-15m", asset="BTC", interval="15m",
    )
    buy_fee = 5.0 * 0.07 * 0.40 * (1 - 0.40)
    sell_fee = 5.0 * 0.07 * 0.48 * (1 - 0.48)
    db.insert_fill(
        "f-buy", "c1", round_ts, "UP", "BUY", 0.40, 5.0,
        ts_ms=1_000_000, trader_side="TAKER", fee=buy_fee,
        market="BTC-15m", asset="BTC", interval="15m",
    )
    db.insert_fill(
        "f-sell", "c1", round_ts, "UP", "SELL", 0.48, 5.0,
        ts_ms=2_000_000, trader_side="TAKER", fee=sell_fee,
        market="BTC-15m", asset="BTC", interval="15m",
    )

    by_id = {f["fill_id"]: f for f in status_mod.recent_fills(conn)}
    assert by_id["f-buy"]["realized_pnl_usd"] is None
    basis = 0.40 * 5.0 + buy_fee
    expected = (0.48 * 5.0 - sell_fee) - basis
    assert by_id["f-sell"]["realized_pnl_usd"] == pytest.approx(expected)
    assert by_id["f-sell"]["realized_pnl_pct"] == pytest.approx(
        expected / basis
    )


# ---------------------------------------------------------------------------
# Window-end settlement (2026-11-01 operator report: unsold rounds lingered)
# ---------------------------------------------------------------------------


class _SettleDB:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool, str]] = []

    def set_round_resolution_if_unresolved(self, ts, up_won, *, market):
        self.calls.append((int(ts), bool(up_won), market))
        return True


def _settle_state(monkeypatch, db):
    from src import main as main_mod

    monkeypatch.setattr(
        main_mod, "STATE", SimpleNamespace(db=db), raising=False
    )
    return main_mod


def _prev_round(*, open_px=100.0, qty=5.0, side="UP", cost=2.0, ts=1_000):
    return SimpleNamespace(
        round_start_ts=ts,
        market=SimpleNamespace(
            btc_spot_at_window_open=open_px,
            token_up_id=UP,
            token_down_id=DOWN,
        ),
        position=SimpleNamespace(
            total_qty=qty, entry_side=side, total_cost=cost
        ),
    )


def _settle_state_with_prices(
    monkeypatch, db, *, up_px=0.0, down_px=0.0, age_s=0.0
):
    from src import main as main_mod

    pc = PriceCache()
    if up_px > 0:
        pc.last_trade_price[UP] = up_px
        pc.last_trade_ts[UP] = time.time() - age_s
    if down_px > 0:
        pc.last_trade_price[DOWN] = down_px
        pc.last_trade_ts[DOWN] = time.time() - age_s
    monkeypatch.setattr(
        main_mod,
        "STATE",
        SimpleNamespace(db=db, price_cache=pc),
        raising=False,
    )
    return main_mod


def _runtime(*, close_px=101.0, cache_px=0.0):
    ctx = SimpleNamespace(prev_window_btc_close=close_px)

    class _WM:
        def get(self, asset):  # noqa: ARG002
            return ctx

    return SimpleNamespace(
        key="BTC-5m",
        spec=SimpleNamespace(asset="BTC"),
        wm=_WM(),
        binance_cache=SimpleNamespace(last_price=cache_px, ts_ms=0),
    )


def test_rollover_settles_unsold_position_from_boundary_prices(monkeypatch) -> None:
    db = _SettleDB()
    main_mod = _settle_state(monkeypatch, db)

    main_mod._settle_round_at_rollover(
        _runtime(close_px=101.0), _prev_round(open_px=100.0)
    )
    assert db.calls == [(1_000, True, "BTC-5m")]

    db.calls.clear()
    main_mod._settle_round_at_rollover(
        _runtime(close_px=99.0), _prev_round(open_px=100.0)
    )
    assert db.calls == [(1_000, False, "BTC-5m")]


def test_rollover_without_prices_books_the_held_side_as_a_loss(monkeypatch) -> None:
    db = _SettleDB()
    main_mod = _settle_state(monkeypatch, db)

    # No close price, holding DOWN -> DOWN must lose (up_won=True).
    main_mod._settle_round_at_rollover(
        _runtime(close_px=0.0),
        _prev_round(open_px=100.0, side="DOWN"),
    )
    assert db.calls == [(1_000, True, "BTC-5m")]

    db.calls.clear()
    # Holding UP -> UP must lose (up_won=False).
    main_mod._settle_round_at_rollover(
        _runtime(close_px=0.0),
        _prev_round(open_px=100.0, side="UP"),
    )
    assert db.calls == [(1_000, False, "BTC-5m")]


def test_price_cache_latest_prefers_the_freshest_source() -> None:
    pc = PriceCache()
    pc.last_trade_price[UP] = 0.40
    pc.last_trade_ts[UP] = 100.0
    pc.midpoint[UP] = 0.50
    pc.midpoint_ts[UP] = 200.0
    assert pc.latest(UP) == (0.50, 200.0, "midpoint")
    pc.last_trade_ts[UP] = 300.0
    assert pc.latest(UP) == (0.40, 300.0, "last_trade")


def test_rollover_prefers_last_polymarket_updown_prices(monkeypatch) -> None:
    """The Polymarket book says DOWN even though the underlying rose."""
    db = _SettleDB()
    main_mod = _settle_state_with_prices(
        monkeypatch, db, up_px=0.02, down_px=0.98
    )
    main_mod._settle_round_at_rollover(
        _runtime(close_px=101.0), _prev_round(open_px=100.0, side="UP")
    )
    assert db.calls == [(1_000, False, "BTC-5m")]


def test_rollover_near_50_50_token_prices_fall_back_to_underlying(
    monkeypatch,
) -> None:
    db = _SettleDB()
    main_mod = _settle_state_with_prices(
        monkeypatch, db, up_px=0.51, down_px=0.49
    )
    # Token prices are too close to call; the underlying fell -> DOWN.
    main_mod._settle_round_at_rollover(
        _runtime(close_px=99.0), _prev_round(open_px=100.0)
    )
    assert db.calls == [(1_000, False, "BTC-5m")]


def test_rollover_stale_token_prices_fall_back_to_underlying(
    monkeypatch,
) -> None:
    db = _SettleDB()
    main_mod = _settle_state_with_prices(
        monkeypatch, db, up_px=0.98, down_px=0.02, age_s=60.0
    )
    main_mod._settle_round_at_rollover(
        _runtime(close_px=99.0), _prev_round(open_px=100.0)
    )
    assert db.calls == [(1_000, False, "BTC-5m")]


def test_local_resolution_falls_back_to_forced_loss(conn) -> None:
    from src import main as main_mod

    db = DB(conn)
    round_ts = int(time.time()) - 3_600
    db.upsert_round(
        round_start_ts=round_ts, slug="xrp-loss", condition_id="0xcid",
        up_token="UP", down_token="DOWN",
        market="XRP-5m", asset="XRP", interval="5m",
    )
    db.insert_fill(
        "f-x", "o-x", round_ts, "UP", "BUY", 0.40, 5.0,
        ts_ms=round_ts * 1000, market="XRP-5m", asset="XRP", interval="5m",
    )

    class _NoSpot:
        async def fetch_boundary_price(self, ts):
            return None

    state = SimpleNamespace(
        asset_spot={"XRP": _NoSpot()}, db=db, db_conn=conn
    )
    result = asyncio.run(
        main_mod._resolve_round_locally(
            state,
            round_ts=round_ts,
            period_s=300,
            asset="XRP",
            market="XRP-5m",
            up_token="UP",
            down_token="DOWN",
            now=time.time(),
        )
    )
    assert result == (False, "forced_loss")
    row = conn.execute(
        "SELECT resolved, up_won FROM rounds WHERE round_start_ts=? AND market=?",
        (round_ts, "XRP-5m"),
    ).fetchone()
    assert row == (1, 0)


def test_resolution_loop_uses_boundary_prices_when_gamma_is_late(conn) -> None:
    from src import main as main_mod

    db = DB(conn)
    round_ts = int(time.time()) - 3_600
    db.upsert_round(
        round_start_ts=round_ts, slug="xrp-local", condition_id="0xcid",
        up_token="UP", down_token="DOWN",
        market="XRP-5m", asset="XRP", interval="5m",
    )
    db.insert_fill(
        "f-local", "o-local", round_ts, "UP", "BUY", 0.40, 5.0,
        ts_ms=round_ts * 1000, market="XRP-5m", asset="XRP", interval="5m",
    )

    class _PM:
        async def get_market_resolution(self, cid, slug=None):  # noqa: ARG002
            return {"resolved": False, "up_wins": None}

    class _Spot:
        def __init__(self):
            self.prices = [100.0, 101.0]

        async def fetch_boundary_price(self, ts):  # noqa: ARG002
            return self.prices.pop(0)

    state = SimpleNamespace(
        pm_client=_PM(), db=db, db_conn=conn,
        asset_spot={"XRP": _Spot()},
    )
    attempts: dict = {}
    assert asyncio.run(main_mod._resolve_rounds_once(state, attempts)) == 1
    row = conn.execute(
        "SELECT resolved, up_won FROM rounds WHERE round_start_ts=? AND market=?",
        (round_ts, "XRP-5m"),
    ).fetchone()
    assert row == (1, 1), "close 101 > open 100 should resolve UP"
    assert attempts == {}


def test_rounds_pagination_is_stable_and_counted(conn) -> None:
    db = DB(conn)
    for i in range(45):
        ts = 1_000 + i * 300
        db.upsert_round(
            round_start_ts=ts, slug=f"r{i}",
            market="BTC-5m", asset="BTC", interval="5m",
        )
        db.insert_fill(
            f"f{i}", f"o{i}", ts, "UP", "BUY", 0.40, 5.0,
            ts_ms=ts * 1000,
            market="BTC-5m", asset="BTC", interval="5m",
        )

    assert status_mod.count_rounds(conn) == 45
    page1 = status_mod.rounds_with_pnl(conn, 20, offset=0)
    page2 = status_mod.rounds_with_pnl(conn, 20, offset=20)
    page3 = status_mod.rounds_with_pnl(conn, 20, offset=40)
    ts = [r["round_start_ts"] for r in page1 + page2 + page3]
    assert len(ts) == 45
    assert len(set(ts)) == 45, "round pages overlap or skip rows"
    # Newest first.
    assert ts[0] == 1_000 + 44 * 300
    assert ts[-1] == 1_000
    assert len(page3) == 5
