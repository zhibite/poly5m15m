"""Execution, leg-risk and persistence tests for the 5m/15m arb bot."""
from __future__ import annotations

import json
import time

import pytest

from src.arb.config import ArbSettings
from src.arb.executor import PairExecutor
from src.arb.models import (
    ArbMarket,
    ArbPair,
    Book,
    Fill,
    Kind,
    LegPlan,
    Level,
    Opportunity,
)
from src.arb.store import ArbStore
from src.market.models import Side


def _cfg(**overrides) -> ArbSettings:
    base = dict(
        PAPER_TRADING=True,
        MAX_NOTIONAL_PER_PAIR=1_000.0,
        MAX_OPEN_NOTIONAL=10_000.0,
        MAX_OPEN_POSITIONS=10,
        MIN_EDGE_USD=0.0,
        MIN_EDGE_PCT=0.0,
        SAME_MARKET_MIN_EDGE_USD=0.0,
        SAME_MARKET_MIN_EDGE_PCT=0.0,
        FEE_BUFFER_PCT=0.0,
        FEE_BUFFER_USD=0.0,
        MAX_DAILY_LOSS_USD=1_000.0,
        UNWIND_RETRIES=1,
        COOLDOWN_S=0.0,
        MAX_QUOTE_AGE_S=10.0,
        SELL_SLIPPAGE_PCT=2.0,
    )
    base.update(overrides)
    return ArbSettings(**base)


def _market(interval: str, start_ts: int, up: str, down: str) -> ArbMarket:
    return ArbMarket(
        asset="BTC",
        interval=interval,
        slug=f"btc-updown-{interval}-{start_ts}",
        condition_id=f"cid-{interval}",
        token_up=up,
        token_down=down,
        start_ts=start_ts,
        end_ts=start_ts + (300 if interval == "5m" else 900),
    )


def _pair(end_ts: int) -> ArbPair:
    m5 = _market("5m", end_ts - 300, "up5", "down5")
    m15 = _market("15m", end_ts - 900, "up15", "down15")
    return ArbPair(
        asset="BTC",
        end_ts=end_ts,
        m5=m5,
        m15=m15,
        strike_5m=69_500.0,
        strike_15m=69_600.0,
        direction_5m=Side.UP,
        direction_15m=Side.DOWN,
        gap_bps=14.0,
        resolution_ok=True,
    )


def _opportunity(end_ts: int) -> Opportunity:
    pair = _pair(end_ts)
    legs = [
        LegPlan(
            token_id="up5",
            side=Side.UP,
            interval="5m",
            shares=10.0,
            avg_price=0.40,
            worst_price=0.40,
            notional=4.0,
            fee=0.0,
            book_ts=time.time(),
        ),
        LegPlan(
            token_id="down15",
            side=Side.DOWN,
            interval="15m",
            shares=10.0,
            avg_price=0.55,
            worst_price=0.55,
            notional=5.5,
            fee=0.0,
            book_ts=time.time(),
        ),
    ]
    return Opportunity(
        kind=Kind.CROSS,
        asset="BTC",
        end_ts=end_ts,
        ts=time.time(),
        legs=legs,
        shares=10.0,
        gross_cost=9.5,
        fees=0.0,
        floor_profit=0.5,
        edge_pct=0.05,
        pair=pair,
        detail={"strike_5m": 69_500.0, "strike_15m": 69_600.0, "gap_bps": 14.0},
    )


def _book(token: str) -> Book:
    return Book(
        token_id=token,
        bids=[Level(0.40, 100)],
        asks=[Level(0.60, 100)],
        ts=time.time(),
    )


class FakeBooks:
    def __init__(self) -> None:
        self.books = {t: _book(t) for t in ("up5", "down15")}

    async def fetch_books(self, tokens, max_age=0.0):
        return {t: self.books[t] for t in tokens if t in self.books}


class FakeDetector:
    def redetect(self, opp, books):
        return opp


class FakeBroker:
    def __init__(self, buy_sizes: dict[str, float], sell_ok: bool = True) -> None:
        self.buy_sizes = buy_sizes
        self.sell_ok = sell_ok
        self.sells: list[tuple[str, float]] = []

    async def buy(self, token_id, amount_usdc, limit_price, tick_size, book):
        size = float(self.buy_sizes.get(token_id, 0.0))
        if size <= 0:
            return None
        return Fill(
            token_id=token_id,
            side="BUY",
            price=0.50,
            size=size,
            fee=0.0,
            ts=time.time(),
            note="fake",
        )

    async def sell(self, token_id, shares, reference_price, tick_size, book):
        if not self.sell_ok:
            return None
        self.sells.append((token_id, shares))
        return Fill(
            token_id=token_id,
            side="SELL",
            price=0.40,
            size=shares,
            fee=0.0,
            ts=time.time(),
            note="fake_unwind",
        )


@pytest.mark.asyncio
async def test_executor_matches_and_unwinds_excess(tmp_path):
    store = ArbStore(tmp_path / "arb.db")
    end_ts = int(time.time()) + 100
    opp = _opportunity(end_ts)
    broker = FakeBroker({"up5": 10.0, "down15": 8.0})
    executor = PairExecutor(
        _cfg(), store, FakeBooks(), broker, FakeDetector()
    )

    result = await executor.execute(opp)

    assert result.ok is True
    assert result.matched_shares == pytest.approx(8.0)
    assert result.buy_cost == pytest.approx(9.0)   # 10*0.5 + 8*0.5
    assert result.sell_proceeds == pytest.approx(0.8)  # 2 excess * 0.4
    assert broker.sells == [("up5", 2.0)]
    positions = store.open_positions()
    assert len(positions) == 1
    assert positions[0]["shares"] == pytest.approx(8.0)
    detail = json.loads(positions[0]["detail_json"])
    assert detail["legs"][0]["bought"] == pytest.approx(10.0)
    assert detail["legs"][0]["sold"] == pytest.approx(2.0)
    store.close()


@pytest.mark.asyncio
async def test_executor_unwinds_a_single_filled_leg_and_books_the_loss(tmp_path):
    store = ArbStore(tmp_path / "arb.db")
    end_ts = int(time.time()) + 100
    opp = _opportunity(end_ts)
    broker = FakeBroker({"up5": 10.0, "down15": 0.0})
    executor = PairExecutor(
        _cfg(), store, FakeBooks(), broker, FakeDetector()
    )

    result = await executor.execute(opp)

    assert result.ok is False
    assert result.error == "zero_matched"
    assert result.matched_shares == 0.0
    assert broker.sells == [("up5", 10.0)]
    positions = store.recent_positions(1)
    assert positions[0]["status"] == "settled"
    assert positions[0]["pnl"] == pytest.approx(-1.0)  # 4.0 - 5.0
    store.close()


@pytest.mark.asyncio
async def test_executor_marks_naked_excess_when_unwind_fails(tmp_path):
    store = ArbStore(tmp_path / "arb.db")
    end_ts = int(time.time()) + 100
    opp = _opportunity(end_ts)
    broker = FakeBroker({"up5": 10.0, "down15": 0.0}, sell_ok=False)
    executor = PairExecutor(
        _cfg(), store, FakeBooks(), broker, FakeDetector()
    )

    result = await executor.execute(opp)

    assert result.ok is False
    positions = store.open_positions()
    assert len(positions) == 1
    detail = json.loads(positions[0]["detail_json"])
    assert detail["leg_risk"] is True
    assert positions[0]["kind"] == "cross"
    store.close()


def test_store_settlement_and_stats(tmp_path):
    store = ArbStore(tmp_path / "arb.db")
    position_id = store.create_position(
        asset="BTC",
        end_ts=int(time.time()),
        kind="cross",
        shares=10.0,
        buy_cost=9.5,
        sell_proceeds=0.0,
        fees=0.2,
        detail={"legs": []},
    )
    assert len(store.open_positions()) == 1
    store.settle_position(
        position_id,
        end_ts=int(time.time()),
        up_won_5m=True,
        up_won_15m=False,
        payout=10.0,
        pnl=10.0 - 9.5 - 0.2,
        invariant_ok=True,
    )
    assert store.open_positions() == []
    stats = store.stats()
    assert stats["settled_positions"] == 1
    assert stats["realized_pnl"] == pytest.approx(0.3)
    store.close()
