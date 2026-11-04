"""Fee and book-walking helpers.

The official Polymarket taker fee (crypto category) is

    fee = shares × fee_rate × p × (1 - p)

where ``p`` is the execution price. Makers pay zero. The formula is
maximised at p=0.5, which is exactly where most 5m/15m UP/DOWN trades
happen — a pair of 50-cent legs costs about 3.5 cents per share, not the
0.5% quoted in some research notes.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.arb.models import Level


def taker_fee(shares: float, price: float, rate: float) -> float:
    """Fee in collateral (USDC) for ``shares`` at ``price``."""
    s = max(0.0, float(shares))
    p = min(1.0, max(0.0, float(price)))
    return s * max(0.0, float(rate)) * p * (1.0 - p)


@dataclass(frozen=True)
class Walk:
    shares: float
    notional: float
    worst_price: float

    @property
    def avg_price(self) -> float:
        return self.notional / self.shares if self.shares > 0 else 0.0

    @property
    def complete(self) -> bool:
        return self.shares > 0


def walk_asks(
    asks: list[Level], max_shares: float, max_price: float
) -> Walk:
    """Walk the ask ladder, buying at most ``max_shares`` up to ``max_price``."""
    remaining = max(0.0, float(max_shares))
    shares = 0.0
    notional = 0.0
    worst = 0.0
    for level in sorted(asks, key=lambda lv: lv.price):
        if level.price <= 0 or level.size <= 0:
            continue
        if level.price > max_price + 1e-12:
            break
        take = min(remaining, level.size)
        if take <= 0:
            continue
        shares += take
        notional += take * level.price
        worst = max(worst, level.price)
        remaining -= take
        if remaining <= 1e-9:
            break
    return Walk(shares=shares, notional=notional, worst_price=worst)


def walk_bids(
    bids: list[Level], max_shares: float, min_price: float = 0.0
) -> Walk:
    """Walk the bid ladder, selling at most ``max_shares`` down to ``min_price``."""
    remaining = max(0.0, float(max_shares))
    shares = 0.0
    notional = 0.0
    worst = 0.0
    for level in sorted(bids, key=lambda lv: lv.price, reverse=True):
        if level.price <= 0 or level.size <= 0:
            continue
        if level.price < min_price - 1e-12:
            break
        take = min(remaining, level.size)
        if take <= 0:
            continue
        shares += take
        notional += take * level.price
        worst = level.price if worst <= 0 else min(worst, level.price)
        remaining -= take
        if remaining <= 1e-9:
            break
    return Walk(shares=shares, notional=notional, worst_price=worst)


def walk_budget(
    asks: list[Level], budget: float, max_price: float
) -> Walk:
    """Walk the ask ladder spending at most ``budget`` USDC.

    This mirrors a CLOB FAK BUY, whose ``amount`` is denominated in USDC:
    better prices can therefore return *more* shares than planned. The
    executor matches the two legs afterwards.
    """
    remaining = max(0.0, float(budget))
    shares = 0.0
    notional = 0.0
    worst = 0.0
    for level in sorted(asks, key=lambda lv: lv.price):
        if level.price <= 0 or level.size <= 0:
            continue
        if level.price > max_price + 1e-12:
            break
        affordable = remaining / level.price
        take = min(level.size, affordable)
        if take <= 0:
            continue
        shares += take
        notional += take * level.price
        worst = max(worst, level.price)
        remaining -= take * level.price
        if remaining <= 1e-9:
            break
    return Walk(shares=shares, notional=notional, worst_price=worst)
