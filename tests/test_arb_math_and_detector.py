"""Payoff, fee, pairing and detector tests for the 5m/15m arbitrage bot."""
from __future__ import annotations

import time

import pytest

from src.config import ArbSettings
from src.detector import ArbDetector
from src.discovery import (
    parse_resolution_source,
    parse_strike,
    sources_compatible,
    window_starts,
)
from src.fees import taker_fee, walk_asks
from src.models import ArbMarket, ArbPair, Book, Level, Opportunity
from src.models import Side


def _market(
    interval: str,
    start_ts: int,
    token_up: str,
    token_down: str,
    *,
    source: str = "chainlink",
    condition_id: str = "",
) -> ArbMarket:
    end_ts = start_ts + (300 if interval == "5m" else 900)
    return ArbMarket(
        asset="BTC",
        interval=interval,
        slug=f"btc-updown-{interval}-{start_ts}",
        condition_id=condition_id or f"cid-{interval}-{start_ts}",
        token_up=token_up,
        token_down=token_down,
        start_ts=start_ts,
        end_ts=end_ts,
        resolution_source=source,
    )


def _pair(end_ts: int, strike_5m: float, strike_15m: float) -> ArbPair:
    m5 = _market("5m", end_ts - 300, "up5", "down5")
    m15 = _market("15m", end_ts - 900, "up15", "down15")
    if strike_5m < strike_15m:
        direction_5m, direction_15m = Side.UP, Side.DOWN
    else:
        direction_5m, direction_15m = Side.DOWN, Side.UP
    gap = abs(strike_5m - strike_15m) / strike_15m * 10_000
    return ArbPair(
        asset="BTC",
        end_ts=end_ts,
        m5=m5,
        m15=m15,
        strike_5m=strike_5m,
        strike_15m=strike_15m,
        direction_5m=direction_5m,
        direction_15m=direction_15m,
        gap_bps=gap,
        resolution_ok=True,
    )


def _book(token: str, asks: list[tuple[float, float]], bids: list[tuple[float, float]], age: float = 0.0) -> Book:
    return Book(
        token_id=token,
        bids=[Level(p, s) for p, s in bids],
        asks=[Level(p, s) for p, s in asks],
        ts=time.time() - age,
    )


# ── payoff math ─────────────────────────────────────────────────────────

def _payout(direction_5m: Side, direction_15m: Side, a: float, b: float, s: float) -> float:
    up5 = s > a
    up15 = s > b
    won5 = (direction_5m == Side.UP and up5) or (direction_5m == Side.DOWN and not up5)
    won15 = (direction_15m == Side.UP and up15) or (direction_15m == Side.DOWN and not up15)
    return float(won5) + float(won15)


def test_cross_payoff_floor_when_5m_strike_is_lower():
    a, b = 69_500.0, 69_600.0
    direction_5m, direction_15m = Side.UP, Side.DOWN
    for s in (69_000.0, 69_500.0, 69_550.0, 69_600.0, 69_650.0, 70_000.0):
        payout = _payout(direction_5m, direction_15m, a, b, s)
        assert payout >= 1.0, f"floor broken at S={s}"
    assert _payout(direction_5m, direction_15m, a, b, 69_550.0) == 2.0


def test_cross_payoff_floor_when_15m_strike_is_lower():
    a, b = 69_600.0, 69_500.0
    direction_5m, direction_15m = Side.DOWN, Side.UP
    for s in (69_000.0, 69_500.0, 69_550.0, 69_600.0, 69_650.0, 70_000.0):
        assert _payout(direction_5m, direction_15m, a, b, s) >= 1.0
    assert _payout(direction_5m, direction_15m, a, b, 69_550.0) == 2.0


def test_wrong_orientation_has_a_zero_zone():
    """A sanity check that the direction rule matters."""
    a, b = 69_500.0, 69_600.0
    # Deliberately wrong: UP on the higher strike, DOWN on the lower.
    assert _payout(Side.DOWN, Side.UP, a, b, 69_550.0) == 0.0


# ── fees ───────────────────────────────────────────────────────────────

def test_taker_fee_official_formula():
    assert taker_fee(100, 0.5, 0.07) == pytest.approx(1.75)
    assert taker_fee(100, 0.0, 0.07) == 0.0
    assert taker_fee(100, 1.0, 0.07) == 0.0
    assert taker_fee(100, 0.4, 0.07) == pytest.approx(1.68)


def test_walk_asks_respects_depth_and_price_cap():
    asks = [Level(0.40, 5), Level(0.45, 5), Level(0.60, 100)]
    walk = walk_asks(asks, max_shares=8, max_price=0.55)
    assert walk.shares == pytest.approx(8)
    assert walk.notional == pytest.approx(2.0 + 1.35)
    assert walk.worst_price == pytest.approx(0.45)


# ── parsing / pairing ──────────────────────────────────────────────────

def test_parse_resolution_source():
    assert parse_resolution_source("Settled by Chainlink BTC/USD") == "chainlink"
    assert parse_resolution_source("Binance BTCUSDT price") == "binance"
    assert parse_resolution_source("will resolve on the official feed") == "unknown"


def test_parse_strike_prefers_number_near_reference():
    text = "This market resolves Up if BTC is above $69,500.00 at the end."
    assert parse_strike(text, ref_price=69_480) == pytest.approx(69_500)
    # A number far from the reference is not a strike for this asset.
    assert parse_strike("volume $2,500,000", ref_price=69_480) is None
    assert parse_strike("opening price at the start of the period") is None


def test_sources_compatible():
    cfg = ArbSettings(PAPER_TRADING=False, ALLOW_UNKNOWN_RESOLUTION_SOURCE=False)
    assert sources_compatible("chainlink", "chainlink", cfg) is True
    assert sources_compatible("chainlink", "binance", cfg) is False
    assert sources_compatible("chainlink", "unknown", cfg) is False
    paper = ArbSettings(PAPER_TRADING=True, ALLOW_UNKNOWN_RESOLUTION_SOURCE=True)
    assert sources_compatible("chainlink", "unknown", paper) is True


def test_window_starts_pair_last_5m_of_15m():
    now = 1_781_131_200  # a 5m boundary, 10 minutes into a 15m window
    start5, start15, end15 = window_starts(now)
    assert start5 == now
    assert start15 == 1_781_130_600
    assert end15 == 1_781_131_500
    assert end15 - start5 == 300


# ── detector ───────────────────────────────────────────────────────────

def _cfg(**overrides) -> ArbSettings:
    base = dict(
        MAX_NOTIONAL_PER_PAIR=1_000.0,
        MAX_SHARES_PER_PAIR=100.0,
        MIN_SHARES_PER_PAIR=5.0,
        MIN_EDGE_USD=0.02,
        MIN_EDGE_PCT=0.001,
        SAME_MARKET_MIN_EDGE_USD=0.02,
        SAME_MARKET_MIN_EDGE_PCT=0.001,
        FEE_RATE=0.07,
        FEE_BUFFER_PCT=0.0,
        FEE_BUFFER_USD=0.0,
        MIN_STRIKE_GAP_BPS=1.0,
        MAX_QUOTE_AGE_S=1.5,
    )
    base.update(overrides)
    return ArbSettings(**base)


def test_detector_finds_cross_when_cost_plus_fee_below_one():
    end_ts = int(time.time()) + 100
    pair = _pair(end_ts, 69_500.0, 69_600.0)
    detector = ArbDetector(_cfg())
    books = {
        "up5": _book("up5", [(0.40, 100)], [(0.39, 100)]),
        "down15": _book("down15", [(0.55, 100)], [(0.54, 100)]),
    }
    opp = detector.detect_cross(pair, books)
    assert opp is not None
    assert opp.kind.value == "cross"
    assert opp.shares == pytest.approx(100)
    assert opp.gross_cost == pytest.approx(95.0)
    assert opp.floor_profit > 0


def test_detector_rejects_cross_when_fee_eats_the_edge():
    end_ts = int(time.time()) + 100
    pair = _pair(end_ts, 69_500.0, 69_600.0)
    detector = ArbDetector(_cfg())
    # 0.50 + 0.49 = 0.99 < 1, but the two 50-cent legs pay ~3.5 cents of
    # taker fees, so the trade is NOT risk-free.
    books = {
        "up5": _book("up5", [(0.50, 100)], [(0.49, 100)]),
        "down15": _book("down15", [(0.49, 100)], [(0.48, 100)]),
    }
    assert detector.detect_cross(pair, books) is None


def test_detector_rejects_stale_and_crossed_books():
    end_ts = int(time.time()) + 100
    pair = _pair(end_ts, 69_500.0, 69_600.0)
    detector = ArbDetector(_cfg())
    stale = {
        "up5": _book("up5", [(0.40, 100)], [(0.39, 100)], age=5.0),
        "down15": _book("down15", [(0.55, 100)], [(0.54, 100)], age=5.0),
    }
    assert detector.detect_cross(pair, stale) is None
    crossed = {
        "up5": _book("up5", [(0.40, 100)], [(0.45, 100)]),
        "down15": _book("down15", [(0.55, 100)], [(0.54, 100)]),
    }
    assert detector.detect_cross(pair, crossed) is None


def test_detector_finds_same_market_complete_set():
    end_ts = int(time.time()) + 100
    market = _market("5m", end_ts - 300, "up", "down")
    detector = ArbDetector(_cfg())
    books = {
        "up": _book("up", [(0.45, 100)], [(0.44, 100)]),
        "down": _book("down", [(0.50, 100)], [(0.49, 100)]),
    }
    opp = detector.detect_same(market, books)
    assert opp is not None
    assert opp.kind.value == "same"
    assert opp.shares == pytest.approx(100)
    assert opp.floor_profit > 0


def test_detector_rejects_same_market_when_sum_is_one():
    end_ts = int(time.time()) + 100
    market = _market("5m", end_ts - 300, "up", "down")
    detector = ArbDetector(_cfg())
    books = {
        "up": _book("up", [(0.50, 100)], [(0.49, 100)]),
        "down": _book("down", [(0.50, 100)], [(0.49, 100)]),
    }
    assert detector.detect_same(market, books) is None


def test_detector_finds_four_leg_complete_set():
    end_ts = int(time.time()) + 100
    pair = _pair(end_ts, 69_500.0, 69_600.0)
    detector = ArbDetector(_cfg())
    books = {
        "up5": _book("up5", [(0.48, 100)], [(0.47, 100)]),
        "down5": _book("down5", [(0.48, 100)], [(0.47, 100)]),
        "up15": _book("up15", [(0.48, 100)], [(0.47, 100)]),
        "down15": _book("down15", [(0.48, 100)], [(0.47, 100)]),
    }
    opp = detector.detect_complete(pair, books)
    assert opp is not None
    assert opp.kind.value == "complete"
    assert len(opp.legs) == 4
    assert opp.shares == pytest.approx(100)
    assert opp.gross_cost == pytest.approx(192.0)
    assert opp.floor_profit > 0


def test_complete_set_does_not_need_strikes():
    """The four-leg payoff is strike-independent; a strike outage must not
    block it as long as both markets share an end time and a source."""
    end_ts = int(time.time()) + 100
    base = _pair(end_ts, 69_500.0, 69_600.0)
    pair = ArbPair(
        asset=base.asset,
        end_ts=base.end_ts,
        m5=base.m5,
        m15=base.m15,
        strike_5m=None,
        strike_15m=None,
        direction_5m=None,
        direction_15m=None,
        gap_bps=None,
        resolution_ok=True,
        reason="strike_missing",
    )
    detector = ArbDetector(_cfg())
    books = {
        "up5": _book("up5", [(0.48, 100)], [(0.47, 100)]),
        "down5": _book("down5", [(0.48, 100)], [(0.47, 100)]),
        "up15": _book("up15", [(0.48, 100)], [(0.47, 100)]),
        "down15": _book("down15", [(0.48, 100)], [(0.47, 100)]),
    }
    assert detector.detect_complete(pair, books) is not None


def test_detector_rejects_complete_set_when_sum_is_two():
    end_ts = int(time.time()) + 100
    pair = _pair(end_ts, 69_500.0, 69_600.0)
    detector = ArbDetector(_cfg())
    books = {
        token: _book(token, [(0.50, 100)], [(0.49, 100)])
        for token in ("up5", "down5", "up15", "down15")
    }
    assert detector.detect_complete(pair, books) is None


def test_cross_sizes_down_to_the_notional_cap():
    """A $20 cap must shrink the size, not reject a valid opportunity."""
    end_ts = int(time.time()) + 100
    pair = _pair(end_ts, 69_500.0, 69_600.0)
    detector = ArbDetector(_cfg(MAX_NOTIONAL_PER_PAIR=20.0))
    books = {
        "up5": _book("up5", [(0.40, 100)], [(0.39, 100)]),
        "down15": _book("down15", [(0.55, 100)], [(0.54, 100)]),
    }
    opp = detector.detect_cross(pair, books)
    assert opp is not None
    assert opp.gross_cost <= 20.0 + 1e-6
    assert 5 <= opp.shares < 30


def test_complete_set_sizes_down_to_the_notional_cap():
    end_ts = int(time.time()) + 100
    pair = _pair(end_ts, 69_500.0, 69_600.0)
    detector = ArbDetector(_cfg(MAX_NOTIONAL_PER_PAIR=20.0))
    books = {
        token: _book(token, [(0.48, 100)], [(0.47, 100)])
        for token in ("up5", "down5", "up15", "down15")
    }
    opp = detector.detect_complete(pair, books)
    assert opp is not None
    assert opp.gross_cost <= 20.0 + 1e-6
    assert 5 <= opp.shares < 15
