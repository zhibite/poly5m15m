"""Regression tests for `market_ws._process` against the real Polymarket
Market Channel payload schema.

The 5m/15m arbitrage engine uses the top of book as its trigger:
  * `best_bid_ask` -> scalar `best_bid` / `best_ask`
  * `book`         -> top-of-book + top-5 depth
  * `price_change` -> per-asset `best_bid` / `best_ask`

`last_trade_price` and REST midpoint remain separate fields; they are
not used by the arbitrage pre-screen. These tests pin the wire shapes and
the one-sided / zero-side update semantics (a missing side must not drop
the other; a zero side must not leave a stale quote behind).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.market.market_ws import MarketWS, PriceCache


def _make_ws() -> tuple[MarketWS, PriceCache]:
    cache = PriceCache()
    ws = MarketWS(cache=cache)
    return ws, cache


@pytest.mark.asyncio
async def test_best_bid_ask_event_updates_top_of_book() -> None:
    """The dedicated BBA event carries the scalar pair used as a trigger."""
    ws, cache = _make_ws()
    msg = {
        "event_type": "best_bid_ask",
        "market": "0xabc",
        "asset_id": "TOKEN_UP",
        "best_bid": "0.42",
        "best_ask": "0.55",
        "spread": "0.13",
        "timestamp": "1757908892351",
    }
    await ws._process(json.dumps(msg))

    assert cache.best_bid["TOKEN_UP"] == pytest.approx(0.42)
    assert cache.best_ask["TOKEN_UP"] == pytest.approx(0.55)
    assert cache.bbo_ts["TOKEN_UP"] > 0
    assert cache.bid_ts["TOKEN_UP"] > 0
    assert cache.ask_ts["TOKEN_UP"] > 0
    # This is an orderbook snapshot, not a trade.
    assert cache.last_trade_price.get("TOKEN_UP") is None
    assert cache.midpoint.get("TOKEN_UP") is None


@pytest.mark.asyncio
async def test_price_change_event_updates_last_trade_price_for_each_token() -> None:
    """`price_change.price_changes[]` carries per-asset trade executions.

    Each item has a `price` field that is the actual fill price — that
    goes into `last_trade_price`. Pre-fix the loop bailed after the
    first token; this pins the multi-token case.
    """
    ws, cache = _make_ws()
    msg = {
        "event_type": "price_change",
        "market": "0xabc",
        "price_changes": [
            {
                "asset_id": "TOKEN_UP",
                "price": "0.5",
                "size": "200",
                "side": "BUY",
                "hash": "0xdead",
                "best_bid": "0.5",
                "best_ask": "0.6",
            },
            {
                "asset_id": "TOKEN_DOWN",
                "price": "0.5",
                "size": "200",
                "side": "SELL",
                "hash": "0xbeef",
                "best_bid": "0.4",
                "best_ask": "0.5",
            },
        ],
        "timestamp": "1757908892351",
    }
    await ws._process(json.dumps(msg))

    # BOTH tokens must have last_trade_price set, not just the first.
    assert cache.last_trade_price["TOKEN_UP"] == pytest.approx(0.5)
    assert cache.last_trade_price["TOKEN_DOWN"] == pytest.approx(0.5)
    # Each price_changes row also refreshes that token's BBO.
    assert cache.best_bid["TOKEN_UP"] == pytest.approx(0.5)
    assert cache.best_ask["TOKEN_UP"] == pytest.approx(0.6)
    assert cache.best_bid["TOKEN_DOWN"] == pytest.approx(0.4)
    assert cache.best_ask["TOKEN_DOWN"] == pytest.approx(0.5)
    # midpoint is populated by the REST poller, NOT by WS events.
    assert cache.midpoint.get("TOKEN_UP") is None
    assert cache.midpoint.get("TOKEN_DOWN") is None


@pytest.mark.asyncio
async def test_book_event_updates_top_of_book_and_depth() -> None:
    """The book snapshot seeds the BBO and top-5 depth."""
    ws, cache = _make_ws()
    msg = {
        "event_type": "book",
        "asset_id": "TOKEN_UP",
        "market": "0xabc",
        "bids": [
            {"price": "0.50", "size": "200"},
            {"price": "0.49", "size": "100"},
        ],
        "asks": [
            {"price": "0.55", "size": "50"},
        ],
        "timestamp": "1757908892351",
        "hash": "0x...",
    }
    await ws._process(json.dumps(msg))

    assert cache.best_bid["TOKEN_UP"] == pytest.approx(0.50)
    assert cache.best_ask["TOKEN_UP"] == pytest.approx(0.55)
    assert cache.bid_depth["TOKEN_UP"] == pytest.approx(300)
    assert cache.ask_depth["TOKEN_UP"] == pytest.approx(50)
    assert cache.bbo_ts["TOKEN_UP"] > 0
    assert cache.bid_ts["TOKEN_UP"] > 0
    assert cache.ask_ts["TOKEN_UP"] > 0
    assert cache.last_trade_price.get("TOKEN_UP") is None
    assert cache.midpoint.get("TOKEN_UP") is None

    # A later full snapshot with an empty ask side must clear the old ask.
    await ws._process(json.dumps({
        "event_type": "book",
        "asset_id": "TOKEN_UP",
        "bids": [{"price": "0.50", "size": "200"}],
        "asks": [],
    }))
    assert cache.best_ask["TOKEN_UP"] == 0.0
    assert cache.ask_depth["TOKEN_UP"] == 0.0


@pytest.mark.asyncio
async def test_legacy_nested_best_bid_ask_is_ignored() -> None:
    """Defensive: if a server emits a legacy nested dict, we do nothing.

    H-NEW-2026-07-15: there is no `best_bid_ask` fallback path in
    `_process` anymore — the legacy nested form is silently ignored
    along with every other orderbook-shape variant. This test pins
    that defensive stance: an unrecognized orderbook shape must not
    clobber an existing `last_trade_price`.
    """
    ws, cache = _make_ws()
    # Pre-fill a known good last_trade_price from a previous trade event.
    cache.last_trade_price["TOKEN_UP"] = 0.99

    msg = {
        "event_type": "book",
        "asset_id": "TOKEN_UP",
        "bids": [{"price": "0.42", "size": "1"}],
        "asks": [{"price": "0.50", "size": "1"}],
        "best_bid_ask": {       # legacy / defensive path (now defunct)
            "asset_id": "TOKEN_UP",
            "bids": [{"price": "0.10", "size": "999"}],
            "asks": [{"price": "0.20", "size": "999"}],
        },
    }
    await ws._process(json.dumps(msg))

    # last_trade_price must NOT have been clobbered by the book event.
    assert cache.last_trade_price["TOKEN_UP"] == pytest.approx(0.99)
    # And nothing was written to midpoint either.
    assert cache.midpoint.get("TOKEN_UP") is None


@pytest.mark.asyncio
async def test_one_sided_bba_preserves_the_other_side() -> None:
    ws, cache = _make_ws()
    await ws._process(json.dumps({
        "event_type": "best_bid_ask",
        "asset_id": "TOKEN_UP",
        "best_bid": "0.42",
    }))
    assert cache.best_bid["TOKEN_UP"] == pytest.approx(0.42)
    assert "TOKEN_UP" not in cache.best_ask
    assert cache.bid_ts["TOKEN_UP"] > 0
    assert cache.ask_ts.get("TOKEN_UP", 0.0) == 0.0

    await ws._process(json.dumps({
        "event_type": "best_bid_ask",
        "asset_id": "TOKEN_UP",
        "best_ask": "0.55",
    }))
    assert cache.best_bid["TOKEN_UP"] == pytest.approx(0.42)
    assert cache.best_ask["TOKEN_UP"] == pytest.approx(0.55)
    assert cache.ask_ts["TOKEN_UP"] > 0


@pytest.mark.asyncio
async def test_zero_side_clears_the_old_quote() -> None:
    ws, cache = _make_ws()
    await ws._process(json.dumps({
        "event_type": "best_bid_ask",
        "asset_id": "TOKEN_UP",
        "best_bid": "0.42",
        "best_ask": "0.55",
    }))
    await ws._process(json.dumps({
        "event_type": "best_bid_ask",
        "asset_id": "TOKEN_UP",
        "best_bid": "0",
    }))
    # The emptied bid is 0, not a stale 0.42; the ask is untouched.
    assert cache.best_bid["TOKEN_UP"] == 0.0
    assert cache.best_ask["TOKEN_UP"] == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_batched_array_is_processed() -> None:
    ws, cache = _make_ws()
    batch = [
        {
            "event_type": "best_bid_ask",
            "asset_id": "TOKEN_UP",
            "best_bid": "0.42",
            "best_ask": "0.55",
        },
        {
            "event_type": "best_bid_ask",
            "asset_id": "TOKEN_DOWN",
            "best_bid": "0.43",
            "best_ask": "0.56",
        },
    ]
    await ws._process(json.dumps(batch))
    assert cache.best_bid["TOKEN_UP"] == pytest.approx(0.42)
    assert cache.best_ask["TOKEN_DOWN"] == pytest.approx(0.56)


@pytest.mark.asyncio
async def test_no_crash_on_malformed_message() -> None:
    ws, _ = _make_ws()
    # Garbage input must not kill the WS reader.
    await ws._process("not json at all")
    await ws._process(json.dumps([1, 2, 3]))  # array without dict events
    await ws._process(json.dumps({"event_type": "best_bid_ask"}))  # no asset_id
    # No assertion — just confirms the coroutine returned cleanly.