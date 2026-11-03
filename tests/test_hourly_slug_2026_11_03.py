"""Hourly Up/Down market discovery (2026-11-03).

Polymarket's 1-hour markets use a dated Eastern-Time slug:
    bitcoin-up-or-down-november-3-2026-6am-et
    ethereum-up-or-down-november-3-2026-6am-et
    solana-up-or-down-november-3-2026-6am-et

These tests pin the slug generator (including DST) and the discovery path.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.market.pm_client import (
    PolymarketClient,
    _et_hour_candidates,
    _market_is_live,
)


def _ts(y: int, mo: int, d: int, h: int, mi: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


def test_et_hour_candidates_use_est_and_edt() -> None:
    # November 3 2026 11:39 UTC = 6:39 AM EST (UTC-5).
    nov = _ts(2026, 11, 3, 11, 39)
    slugs = dict(_et_hour_candidates("BTC", nov, 3600, (0,)))
    assert slugs["bitcoin-up-or-down-november-3-2026-6am-et"] == _ts(
        2026, 11, 3, 11
    )
    assert "bitcoin-up-or-down-november-3-6am-et" in slugs

    # July 15 2026 16:39 UTC = 12:39 PM EDT (UTC-4).
    jul = _ts(2026, 7, 15, 16, 39)
    slugs = dict(_et_hour_candidates("BTC", jul, 3600, (0,)))
    assert slugs["bitcoin-up-or-down-july-15-2026-12pm-et"] == _ts(
        2026, 7, 15, 16
    )


def test_et_hour_candidates_cover_asset_name_aliases() -> None:
    now = _ts(2026, 11, 3, 11, 39)
    expected = {
        "ETH": "ethereum-up-or-down-november-3-2026-6am-et",
        "SOL": "solana-up-or-down-november-3-2026-6am-et",
        "DOGE": "dogecoin-up-or-down-november-3-2026-6am-et",
        "XRP": "xrp-up-or-down-november-3-2026-6am-et",
        "BNB": "bnb-up-or-down-november-3-2026-6am-et",
        "HYPE": "hyperliquid-up-or-down-november-3-2026-6am-et",
    }
    for asset, slug in expected.items():
        got = {s for s, _ in _et_hour_candidates(asset, now, 3600, (0,))}
        assert slug in got, f"{asset}: {sorted(got)[:6]}"


def test_market_is_live_rejects_closed_and_expired() -> None:
    now = datetime.now(timezone.utc)
    future = (now.timestamp() + 3600)
    past = (now.timestamp() - 1)
    assert _market_is_live({
        "active": True,
        "closed": False,
        "endDate": datetime.fromtimestamp(
            future, timezone.utc
        ).isoformat(),
    })
    assert not _market_is_live({
        "active": True,
        "closed": True,
        "endDate": datetime.fromtimestamp(
            future, timezone.utc
        ).isoformat(),
    })
    assert not _market_is_live({
        "active": True,
        "endDate": datetime.fromtimestamp(
            past, timezone.utc
        ).isoformat(),
    })


class _FakeResponse:
    status_code = 200

    def json(self):
        return [{
            "slug": "bitcoin-up-or-down-november-3-2026-6am-et",
            "closed": False,
            "markets": [{
                "conditionId": "0xabc",
                "active": True,
                "closed": False,
                "endDate": "2026-11-03T12:00:00Z",
                "clobTokenIds": '["UPTOKEN", "DOWNTOKEN"]',
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["0.55", "0.45"]',
            }],
        }]


class _FakeHTTP:
    def __init__(self) -> None:
        self.slugs: list[str] = []

    async def get(self, url, params=None, timeout=None):  # noqa: ANN001
        self.slugs.append((params or {}).get("slug", ""))
        return _FakeResponse()


@pytest.mark.asyncio
async def test_hourly_discovery_uses_dated_slug(monkeypatch) -> None:
    import src.market.pm_client as pm_mod

    monkeypatch.setattr(
        pm_mod.time, "time", lambda: _ts(2026, 11, 3, 11, 39)
    )
    client = PolymarketClient(gamma_api="https://gamma.test")
    fake = _FakeHTTP()
    async def _fake_client():
        return fake
    client._client = _fake_client  # type: ignore[method-assign]

    markets = await client.get_asset_updown_markets(
        "BTC", slug_interval="1h", window_sec=3600
    )

    assert fake.slugs[0] == "bitcoin-up-or-down-november-3-2026-6am-et"
    assert len(markets) == 1
    m = markets[0]
    assert m["_event_slug"] == "bitcoin-up-or-down-november-3-2026-6am-et"
    assert m["_window_start_ts"] == _ts(2026, 11, 3, 11)
    assert [t["token_id"] for t in m["tokens"]] == ["UPTOKEN", "DOWNTOKEN"]
    assert m["_up_price"] == pytest.approx(0.55)
    assert m["_down_price"] == pytest.approx(0.45)
