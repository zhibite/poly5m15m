"""Discovery/pairing tests with synthetic Gamma payloads."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config import ArbSettings
from src.discovery import ArbDiscovery
from src.store import ArbStore
from src.models import Side

END_TS = 1_781_131_500
START_5 = END_TS - 300
START_15 = END_TS - 900


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _raw(interval: str, start: int, description: str) -> dict:
    return {
        "conditionId": f"0x{interval}",
        "question": f"Bitcoin Up or Down - {interval}",
        "description": description,
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
    def __init__(self, description_5m: str, description_15m: str) -> None:
        self.description_5m = description_5m
        self.description_15m = description_15m

    async def get_asset_updown_markets(
        self, asset, *, slug_interval, window_sec, limit, start_ts=None
    ):
        if slug_interval == "5m":
            return [_raw("5m", start_ts, self.description_5m)]
        return [_raw("15m", start_ts, self.description_15m)]

    async def get_tick_size(self, token_id):
        return "0.01"

    async def get_neg_risk(self, token_id):
        return False

    async def close(self):
        pass


class FakeRef:
    def __init__(self, p5: float, p15: float, now: float | None = None) -> None:
        self.p5 = p5
        self.p15 = p15
        self.now = now if now is not None else (p5 + p15) / 2

    async def open_at(self, asset, ts):
        return self.p5 if ts == START_5 else self.p15

    async def price_now(self, asset):
        return self.now


def _discovery(tmp_path, pm, ref, **cfg_overrides) -> ArbDiscovery:
    cfg = ArbSettings(**cfg_overrides)
    store = ArbStore(tmp_path / "arb.db")
    return ArbDiscovery(pm, ref, store, cfg)


@pytest.mark.asyncio
async def test_pair_builds_correct_direction_and_strikes(tmp_path):
    desc = "Resolves via Chainlink at the opening price at the start."
    discovery = _discovery(
        tmp_path, FakePM(desc, desc), FakeRef(69_500.0, 69_600.0)
    )
    pair = await discovery.pair("BTC", END_TS, now=END_TS - 100)
    assert pair is not None
    assert pair.ready is True
    assert pair.strike_5m == pytest.approx(69_500.0)
    assert pair.strike_15m == pytest.approx(69_600.0)
    assert pair.direction_5m == Side.UP
    assert pair.direction_15m == Side.DOWN
    assert pair.gap_bps == pytest.approx(14.3678, rel=1e-3)
    discovery._store.close()


@pytest.mark.asyncio
async def test_pair_rejects_known_resolution_mismatch(tmp_path):
    discovery = _discovery(
        tmp_path,
        FakePM("Resolves via Binance BTCUSDT.", "Resolves via Chainlink BTC/USD."),
        FakeRef(69_500.0, 69_600.0),
    )
    pair = await discovery.pair("BTC", END_TS, now=END_TS - 100)
    assert pair is not None
    assert pair.ready is False
    assert pair.reason == "resolution_source_mismatch"
    discovery._store.close()


@pytest.mark.asyncio
async def test_pair_rejects_strikes_that_are_too_close(tmp_path):
    desc = "Resolves via Chainlink at the opening price."
    discovery = _discovery(
        tmp_path,
        FakePM(desc, desc),
        FakeRef(69_500.0, 69_502.0),  # ~0.29 bps apart
        MIN_STRIKE_GAP_BPS=5.0,
    )
    pair = await discovery.pair("BTC", END_TS, now=END_TS - 100)
    assert pair is not None
    assert pair.ready is False
    assert pair.reason == "strike_gap_too_small"
    discovery._store.close()
