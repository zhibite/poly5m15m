"""Pure-martingale engine has no GateKeeper (2026-11-03).

Regression for the 12:00 UTC window switch: `_check_t1_entry` logged
`drift=self._gate.state(bc_now).drift` unconditionally, so the first
side whose mid crossed ENTRY_PRICE_CAP raised AttributeError instead of
emitting ActionOpenT1.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from src.feeds.binance_spot import BinanceCache
from src.market.market_ws import PriceCache
from src.market.models import MarketInfo
from src.strategy.engine import ActionOpenT1, StrategyEngine
from src.strategy.round_state import RoundState


@pytest.mark.asyncio
async def test_t1_triggers_without_gatekeeper() -> None:
    pc = PriceCache()
    pc.midpoint["UPTOKEN"] = 0.30
    pc.midpoint["DOWNTOKEN"] = 0.30
    actions: list = []

    async def handler(action) -> None:  # noqa: ANN001
        actions.append(action)

    engine = StrategyEngine(BinanceCache(), pc, handler, signals=None)
    market = MarketInfo(
        asset="BTC",
        condition_id="0xdeadbeef",
        token_up_id="UPTOKEN",
        token_down_id="DOWNTOKEN",
        window_start=datetime.now(timezone.utc) - timedelta(seconds=10),
        window_end=datetime.now(timezone.utc) + timedelta(seconds=3590),
        round_start_ts=int(time.time()) - 10,
    )
    engine._round = RoundState(round_start_ts=market.round_start_ts, market=market)
    engine._gate = None

    await engine._check_t1_entry(0.0)

    assert len(actions) == 1
    assert isinstance(actions[0], ActionOpenT1)
    assert actions[0].side == "UP"
    assert engine._round.position.entry_token == "UPTOKEN"
