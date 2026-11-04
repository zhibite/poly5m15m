"""End-to-end paper flow: discovery -> detection -> execution -> settlement."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from src.arb.books import BookService
from src.arb.config import ArbSettings
from src.arb.detector import ArbDetector
from src.arb.discovery import ArbDiscovery
from src.arb.executor import PairExecutor, PaperBroker
from src.arb.settlement import SettlementWatcher
from src.arb.store import ArbStore


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _raw(interval: str, start: int) -> dict:
    return {
        "conditionId": f"0x{interval}",
        "question": f"Bitcoin Up or Down - {interval}",
        "description": "Resolves via Chainlink at the opening price.",
        "endDate": _iso(start + (300 if interval == "5m" else 900)),
        "negRisk": False,
        "tickSize": "0.01",
        "_event_slug": f"btc-updown-{interval}-{start}",
        "_window_start_ts": start,
        "tokens": [
            {"token_id": f"up-{interval}", "outcome": "Up", "price": "0.5"},
            {"token_id": f"down-{interval}", "outcome": "Down", "price": "0.5"},
        ],
    }


class FakePM:
    def __init__(self, end_ts: int) -> None:
        self.end_ts = end_ts
        self.books = {
            "up-5m": {"bids": [{"price": "0.39", "size": "100"}],
                      "asks": [{"price": "0.40", "size": "100"}]},
            "down-15m": {"bids": [{"price": "0.54", "size": "100"}],
                         "asks": [{"price": "0.55", "size": "100"}]},
        }

    async def get_asset_updown_markets(
        self, asset, *, slug_interval, window_sec, limit, start_ts=None
    ):
        return [_raw(slug_interval, start_ts)]

    async def get_tick_size(self, token_id):
        return "0.01"

    async def get_neg_risk(self, token_id):
        return False

    async def get_order_book(self, token_id):
        return self.books.get(token_id)

    async def get_market_resolution(self, condition_id, slug=None):
        if condition_id == "0x5m":
            return {"resolved": True, "up_wins": True, "outcome": "Up"}
        return {"resolved": True, "up_wins": False, "outcome": "Down"}

    async def close(self):
        pass


class FakeRef:
    async def open_at(self, asset, ts):
        return 69_500.0 if ts == self.end_ts - 300 else 69_600.0

    async def price_now(self, asset):
        return 69_550.0

    def __init__(self, end_ts: int) -> None:
        self.end_ts = end_ts


@pytest.mark.asyncio
async def test_paper_round_trip_and_settlement(tmp_path):
    end_ts = int(time.time()) + 100
    cfg = ArbSettings(
        PAPER_TRADING=True,
        MAX_NOTIONAL_PER_PAIR=1_000.0,
        MAX_SHARES_PER_PAIR=100.0,
        MIN_SHARES_PER_PAIR=5.0,
        MIN_EDGE_USD=0.0,
        MIN_EDGE_PCT=0.0,
        FEE_BUFFER_PCT=0.0,
        FEE_BUFFER_USD=0.0,
        MAX_OPEN_NOTIONAL=10_000.0,
        MAX_OPEN_POSITIONS=10,
        MAX_DAILY_LOSS_USD=1_000.0,
        UNWIND_RETRIES=1,
        UNWIND_RETRY_DELAY_S=0.0,
        COOLDOWN_S=0.0,
        MAX_QUOTE_AGE_S=10.0,
        STRIKE_SOURCE="binance",
    )
    store = ArbStore(tmp_path / "arb.db")
    pm = FakePM(end_ts)
    ref = FakeRef(end_ts)
    discovery = ArbDiscovery(pm, ref, store, cfg)
    pair = await discovery.pair("BTC", end_ts, now=end_ts - 100)
    assert pair is not None and pair.ready

    books = BookService(pm, cfg)
    detector = ArbDetector(cfg)
    pairs_tokens = [
        pair.m5.token_for(pair.direction_5m),
        pair.m15.token_for(pair.direction_15m),
    ]
    snap = await books.fetch_books(pairs_tokens)
    opp = detector.detect_cross(pair, snap, now=end_ts - 100)
    assert opp is not None
    assert opp.shares == pytest.approx(100.0)

    executor = PairExecutor(cfg, store, books, PaperBroker(cfg), detector)
    result = await executor.execute(opp)
    assert result.ok is True
    assert result.matched_shares == pytest.approx(100.0)
    assert len(store.open_positions()) == 1

    # Both buy fills (and their taker fees) must be persisted individually.
    fills = store.recent_fills(10)
    assert len(fills) == 2
    assert all(f["side"] == "BUY" and f["fee"] > 0 for f in fills)
    assert all(f["asset"] == "BTC" and f["kind"] == "cross" for f in fills)
    position = store.recent_positions(1)[0]
    assert position["fees"] == pytest.approx(sum(f["fee"] for f in fills))
    assert store.fees_total() == pytest.approx(sum(f["fee"] for f in fills))
    assert store.stats()["fees_total"] == pytest.approx(sum(f["fee"] for f in fills))

    watcher = SettlementWatcher(pm, store, cfg)
    settled = await watcher.poll_once(now=end_ts + 10)
    assert settled == 1
    assert store.open_positions() == []
    stats = store.stats()
    assert stats["settled_positions"] == 1
    assert stats["realized_pnl"] > 100.0  # both legs won
    settled_position = store.recent_positions(1)[0]
    assert settled_position["pnl"] == pytest.approx(
        200.0 - settled_position["buy_cost"] - settled_position["fees"]
    )
    store.close()
