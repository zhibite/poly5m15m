"""Data structures shared by the arbitrage modules.

Everything here is deliberately plain: the engine, detector, executor and
store exchange these objects, and the tests construct them directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from src.market.models import Side


class Kind(str, Enum):
    CROSS = "cross"        # 5m UP/DOWN + 15m DOWN/UP, same end time
    SAME = "same"          # UP + DOWN of one market
    COMPLETE = "complete"  # all four legs across both markets, pays 2/set


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass
class Book:
    """Top-N executable book snapshot."""

    token_id: str
    bids: list[Level]
    asks: list[Level]
    ts: float
    source: str = "rest"

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def crossed(self) -> bool:
        bid, ask = self.best_bid, self.best_ask
        return bool(bid and ask and bid >= ask)


@dataclass
class ArbMarket:
    """One binary UP/DOWN market (5m or 15m)."""

    asset: str
    interval: str            # "5m" | "15m"
    slug: str
    condition_id: str
    token_up: str
    token_down: str
    start_ts: int
    end_ts: int
    tick_size: float = 0.01
    neg_risk: bool = False
    description: str = ""
    resolution_source: str = "unknown"
    strike: float | None = None
    strike_source: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def window_sec(self) -> int:
        return 300 if self.interval == "5m" else 900

    @property
    def end_dt(self) -> datetime:
        return datetime.fromtimestamp(self.end_ts, tz=timezone.utc)

    def token_for(self, side: Side) -> str:
        return self.token_up if side == Side.UP else self.token_down


@dataclass
class ArbPair:
    """A 5m and a 15m market that settle on the same end timestamp."""

    asset: str
    end_ts: int
    m5: ArbMarket
    m15: ArbMarket
    strike_5m: float | None
    strike_15m: float | None
    direction_5m: Side | None
    direction_15m: Side | None
    gap_bps: float | None
    resolution_ok: bool
    end_time_ok: bool = True
    reason: str = ""

    @property
    def markets_ready(self) -> bool:
        """Both markets may share a settlement print (strikes not needed).

        The four-leg complete-set strategy only needs this, so a missing
        or too-close strike must not block it.
        """
        return self.end_time_ok and self.resolution_ok

    @property
    def ready(self) -> bool:
        """The two-leg direction strategy needs strikes and a gap too."""
        return (
            self.markets_ready
            and self.reason == ""
            and self.strike_5m is not None
            and self.strike_15m is not None
            and self.direction_5m is not None
            and self.direction_15m is not None
            and self.gap_bps is not None
        )

    def seconds_left(self, now: float | None = None) -> float:
        import time

        return float(self.end_ts) - (time.time() if now is None else now)


@dataclass(frozen=True)
class LegPlan:
    """One side of an opportunity, sized from a book snapshot."""

    token_id: str
    side: Side
    interval: str
    shares: float
    avg_price: float
    worst_price: float
    notional: float
    fee: float
    book_ts: float


@dataclass
class Opportunity:
    kind: Kind
    asset: str
    end_ts: int
    ts: float
    legs: list[LegPlan]
    shares: float
    gross_cost: float
    fees: float
    floor_profit: float
    edge_pct: float
    pair: ArbPair | None = None
    market: ArbMarket | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Fill:
    token_id: str
    side: str                 # "BUY" | "SELL"
    price: float
    size: float
    fee: float
    order_id: str = ""
    role: str = "TAKER"
    ts: float = 0.0
    note: str = ""

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class ExecutionResult:
    ok: bool
    position_id: int | None
    matched_shares: float
    buy_cost: float
    sell_proceeds: float
    fees: float
    fills: list[Fill] = field(default_factory=list)
    error: str = ""
