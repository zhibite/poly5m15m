"""The status payload must expose live UP/DOWN prices for the dashboard."""
from __future__ import annotations

import json
import time

import pytest

from src.books import BBO
from src.config import ArbSettings
from src.engine import ArbEngine
from src.models import ArbMarket, ArbPair, Side
from src.store import ArbStore


class FakeBooks:
    ws_connected = True

    def bbo(self, tokens):
        now = time.time()
        return {
            token: BBO(bid=0.40, ask=0.42, ts=now, bid_ts=now, ask_ts=now)
            for token in tokens
        }

    def last_price(self, token):
        return 0.41


def _market(asset: str, interval: str, start: int) -> ArbMarket:
    end = start + (300 if interval == "5m" else 900)
    return ArbMarket(
        asset=asset,
        interval=interval,
        slug=f"{asset.lower()}-updown-{interval}-{start}",
        condition_id=f"{asset}-{interval}",
        token_up=f"{asset}-{interval}-up",
        token_down=f"{asset}-{interval}-down",
        start_ts=start,
        end_ts=end,
    )


def test_status_includes_up_down_market_prices(tmp_path):
    now = time.time()
    start5 = int(now // 300) * 300
    start15 = int(now // 900) * 900
    cfg = ArbSettings(
        PAPER_TRADING=True,
        STATUS_PATH=str(tmp_path / "status.json"),
    )
    store = ArbStore(tmp_path / "arb.db")
    engine = ArbEngine(
        cfg, None, None, store, FakeBooks(), None, None, None, None
    )
    engine._current = {
        ("BTC", "5m", start5): _market("BTC", "5m", start5),
        ("BTC", "15m", start15): _market("BTC", "15m", start15),
        ("ETH", "5m", start5): _market("ETH", "5m", start5),
    }
    engine._last_discovery = now

    engine._write_status()

    payload = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    markets = payload["markets"]
    assert len(markets) == 3
    assert markets[0]["asset"] == "BTC"
    assert markets[0]["interval"] == "5m"
    assert markets[0]["up"]["bid"] == pytest.approx(0.40)
    assert markets[0]["up"]["ask"] == pytest.approx(0.42)
    assert markets[0]["up"]["mid"] == pytest.approx(0.41)
    assert markets[0]["up"]["last"] == pytest.approx(0.41)
    assert markets[0]["up"]["age"] is not None
    assert markets[0]["down"]["mid"] == pytest.approx(0.41)
    assert markets[0]["start_utc"] and markets[0]["end_utc"]
    store.close()


def test_status_pair_signal_thresholds(tmp_path):
    now = time.time()
    end = int(now // 300) * 300 + 300
    m5 = _market("BTC", "5m", end - 300)
    m15 = _market("BTC", "15m", end - 900)
    pair = ArbPair(
        asset="BTC",
        end_ts=end,
        m5=m5,
        m15=m15,
        strike_5m=100.0,
        strike_15m=101.0,
        direction_5m=Side.UP,
        direction_15m=Side.DOWN,
        gap_bps=100.0,
        resolution_ok=True,
    )
    cfg = ArbSettings(
        PAPER_TRADING=True,
        STATUS_PATH=str(tmp_path / "status.json"),
    )
    store = ArbStore(tmp_path / "arb.db")
    engine = ArbEngine(
        cfg, None, None, store, FakeBooks(), None, None, None, None
    )
    engine._pairs = {("BTC", end): pair}

    engine._write_status()

    payload = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    item = payload["pairs"][0]
    assert item["side_5m"] == "UP"
    assert item["side_15m"] == "DOWN"
    # FakeBooks returns ask=0.42 for every token.
    assert item["ask_5m"] == pytest.approx(0.42)
    assert item["ask_15m"] == pytest.approx(0.42)
    assert item["ask_sum"] == pytest.approx(0.84)
    assert item["required_2leg"] == pytest.approx(0.960486, rel=1e-4)
    assert item["gap_2leg"] == pytest.approx(-0.120486, rel=1e-4)
    assert item["complete_ask_sum"] == pytest.approx(1.68)
    assert item["complete_required"] == pytest.approx(1.922971, rel=1e-4)
    assert item["complete_gap"] == pytest.approx(-0.242971, rel=1e-4)
    store.close()
