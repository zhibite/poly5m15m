"""Pure arbitrage detection.

Cross-market payoff (A = 5m strike, B = 15m strike, S = settlement price):

    A < B -> buy 5m UP + 15m DOWN
      S <= A          : 15m DOWN wins            -> 1
      A < S < B       : 5m UP + 15m DOWN win     -> 2
      S >= B          : 5m UP wins               -> 1

    A > B -> buy 5m DOWN + 15m UP (mirror)

The floor is therefore exactly $1 per matched share. If the executable
cost including taker fees is below $1, the trade is risk-free in payoff
terms. The detector also checks the market's own asks for the same-market
UP+DOWN variant, whose payoff is exactly $1.

The detector never assumes the strikes: the pair carries the direction
chosen by ``ArbDiscovery`` from the two window-open prices.
"""
from __future__ import annotations

import time

from src.config import ArbSettings
from src.fees import taker_fee, walk_asks
from src.models import (
    ArbPair,
    ArbMarket,
    Book,
    Kind,
    LegPlan,
    Level,
    Opportunity,
)
from src.infra.logging import get_logger
from src.models import Side

log = get_logger(__name__)

# Never pay a full dollar for a leg; a 0.99 cap leaves a tick of room and
# keeps the arithmetic away from the degenerate p=1 fee case.
MAX_LEG_PRICE = 0.99


class ArbDetector:
    def __init__(self, cfg: ArbSettings) -> None:
        self._cfg = cfg

    # ── cross-market (the core strategy) ────────────────────────────────
    def detect_cross(
        self,
        pair: ArbPair,
        books: dict[str, Book],
        now: float | None = None,
    ) -> Opportunity | None:
        now = time.time() if now is None else now
        if not self._cfg.ALLOW_CROSS_MARKET or not pair.ready:
            return None
        if pair.direction_5m is None or pair.direction_15m is None:
            return None
        if now > pair.end_ts - self._cfg.STOP_OPEN_BEFORE_END_S:
            return None
        if pair.seconds_left(now) < self._cfg.MIN_SECONDS_TO_END_S:
            return None

        book5 = books.get(pair.m5.token_for(pair.direction_5m))
        book15 = books.get(pair.m15.token_for(pair.direction_15m))
        if not self._books_ok(now, book5, book15):
            return None

        walk5 = walk_asks(
            book5.asks,
            max_shares=self._cfg.MAX_SHARES_PER_PAIR,
            max_price=MAX_LEG_PRICE,
        )
        walk15 = walk_asks(
            book15.asks,
            max_shares=self._cfg.MAX_SHARES_PER_PAIR,
            max_price=MAX_LEG_PRICE,
        )
        shares = min(walk5.shares, walk15.shares, self._cfg.MAX_SHARES_PER_PAIR)
        if shares < self._cfg.MIN_SHARES_PER_PAIR:
            return None
        shares = self._fit_notional(
            shares, [book5.asks, book15.asks], MAX_LEG_PRICE
        )
        if shares < self._cfg.MIN_SHARES_PER_PAIR:
            return None

        w5 = walk_asks(book5.asks, max_shares=shares, max_price=MAX_LEG_PRICE)
        w15 = walk_asks(book15.asks, max_shares=shares, max_price=MAX_LEG_PRICE)
        if w5.shares + 1e-9 < shares or w15.shares + 1e-9 < shares:
            return None

        gross = w5.notional + w15.notional
        if gross <= 0 or gross > self._cfg.MAX_NOTIONAL_PER_PAIR:
            return None
        fee5 = taker_fee(shares, w5.avg_price, self._cfg.FEE_RATE)
        fee15 = taker_fee(shares, w15.avg_price, self._cfg.FEE_RATE)
        fees = fee5 + fee15
        fees_buffered = fees * (1.0 + self._cfg.FEE_BUFFER_PCT) + self._cfg.FEE_BUFFER_USD
        floor_profit = shares - gross - fees_buffered
        edge_pct = floor_profit / gross if gross > 0 else 0.0
        if floor_profit < self._cfg.MIN_EDGE_USD:
            return None
        if edge_pct < self._cfg.MIN_EDGE_PCT:
            return None

        legs = [
            LegPlan(
                token_id=book5.token_id,
                side=pair.direction_5m,
                interval="5m",
                shares=shares,
                avg_price=w5.avg_price,
                worst_price=w5.worst_price,
                notional=w5.notional,
                fee=fee5,
                book_ts=book5.ts,
            ),
            LegPlan(
                token_id=book15.token_id,
                side=pair.direction_15m,
                interval="15m",
                shares=shares,
                avg_price=w15.avg_price,
                worst_price=w15.worst_price,
                notional=w15.notional,
                fee=fee15,
                book_ts=book15.ts,
            ),
        ]
        return Opportunity(
            kind=Kind.CROSS,
            asset=pair.asset,
            end_ts=pair.end_ts,
            ts=now,
            legs=legs,
            shares=shares,
            gross_cost=gross,
            fees=fees_buffered,
            floor_profit=floor_profit,
            edge_pct=edge_pct,
            pair=pair,
            detail={
                "strike_5m": pair.strike_5m,
                "strike_15m": pair.strike_15m,
                "gap_bps": pair.gap_bps,
                "direction_5m": pair.direction_5m.value,
                "direction_15m": pair.direction_15m.value,
                "seconds_left": pair.seconds_left(now),
                "fee_rate": self._cfg.FEE_RATE,
                "fees_raw": fees,
                "max_leg_price": MAX_LEG_PRICE,
            },
        )

    # ── same-market complete-set ────────────────────────────────────────
    def detect_same(
        self,
        market: ArbMarket,
        books: dict[str, Book],
        now: float | None = None,
    ) -> Opportunity | None:
        now = time.time() if now is None else now
        if not self._cfg.ALLOW_SAME_MARKET:
            return None
        if now > market.end_ts - self._cfg.STOP_OPEN_BEFORE_END_S:
            return None
        if market.end_ts - now < self._cfg.MIN_SECONDS_TO_END_S:
            return None

        book_up = books.get(market.token_up)
        book_down = books.get(market.token_down)
        if not self._books_ok(now, book_up, book_down):
            return None

        walk_up = walk_asks(
            book_up.asks,
            max_shares=self._cfg.MAX_SHARES_PER_PAIR,
            max_price=MAX_LEG_PRICE,
        )
        walk_down = walk_asks(
            book_down.asks,
            max_shares=self._cfg.MAX_SHARES_PER_PAIR,
            max_price=MAX_LEG_PRICE,
        )
        shares = min(walk_up.shares, walk_down.shares, self._cfg.MAX_SHARES_PER_PAIR)
        if shares < self._cfg.MIN_SHARES_PER_PAIR:
            return None
        shares = self._fit_notional(
            shares, [book_up.asks, book_down.asks], MAX_LEG_PRICE
        )
        if shares < self._cfg.MIN_SHARES_PER_PAIR:
            return None

        wu = walk_asks(book_up.asks, max_shares=shares, max_price=MAX_LEG_PRICE)
        wd = walk_asks(book_down.asks, max_shares=shares, max_price=MAX_LEG_PRICE)
        if wu.shares + 1e-9 < shares or wd.shares + 1e-9 < shares:
            return None

        gross = wu.notional + wd.notional
        if gross <= 0 or gross > self._cfg.MAX_NOTIONAL_PER_PAIR:
            return None
        fee_up = taker_fee(shares, wu.avg_price, self._cfg.FEE_RATE)
        fee_down = taker_fee(shares, wd.avg_price, self._cfg.FEE_RATE)
        fees = fee_up + fee_down
        fees_buffered = fees * (1.0 + self._cfg.FEE_BUFFER_PCT) + self._cfg.FEE_BUFFER_USD
        floor_profit = shares - gross - fees_buffered
        edge_pct = floor_profit / gross if gross > 0 else 0.0
        if floor_profit < self._cfg.SAME_MARKET_MIN_EDGE_USD:
            return None
        if edge_pct < self._cfg.SAME_MARKET_MIN_EDGE_PCT:
            return None

        legs = [
            LegPlan(
                token_id=market.token_up,
                side=Side.UP,
                interval=market.interval,
                shares=shares,
                avg_price=wu.avg_price,
                worst_price=wu.worst_price,
                notional=wu.notional,
                fee=fee_up,
                book_ts=book_up.ts,
            ),
            LegPlan(
                token_id=market.token_down,
                side=Side.DOWN,
                interval=market.interval,
                shares=shares,
                avg_price=wd.avg_price,
                worst_price=wd.worst_price,
                notional=wd.notional,
                fee=fee_down,
                book_ts=book_down.ts,
            ),
        ]
        return Opportunity(
            kind=Kind.SAME,
            asset=market.asset,
            end_ts=market.end_ts,
            ts=now,
            legs=legs,
            shares=shares,
            gross_cost=gross,
            fees=fees_buffered,
            floor_profit=floor_profit,
            edge_pct=edge_pct,
            market=market,
            detail={
                "interval": market.interval,
                "seconds_left": market.end_ts - now,
                "fee_rate": self._cfg.FEE_RATE,
                "fees_raw": fees,
                "max_leg_price": MAX_LEG_PRICE,
            },
        )

    # ── four-leg complete set (strike-independent) ──────────────────────
    def detect_complete(
        self,
        pair: ArbPair,
        books: dict[str, Book],
        now: float | None = None,
    ) -> Opportunity | None:
        """Buy 5m UP+DOWN and 15m UP+DOWN.

        Whatever the settlement price, exactly one leg of each market wins,
        so the four legs pay exactly $2 per matched set. This variant does
        not need the strikes or their ordering — only the shared end time
        and settlement source. It is the most robust of the three modes.
        """
        now = time.time() if now is None else now
        if not self._cfg.ALLOW_COMPLETE_SET or not pair.markets_ready:
            return None
        if now < pair.m5.start_ts:
            return None
        if now > pair.end_ts - self._cfg.STOP_OPEN_BEFORE_END_S:
            return None
        if pair.end_ts - now < self._cfg.MIN_SECONDS_TO_END_S:
            return None

        legs_spec = [
            (pair.m5.token_up, Side.UP, "5m"),
            (pair.m5.token_down, Side.DOWN, "5m"),
            (pair.m15.token_up, Side.UP, "15m"),
            (pair.m15.token_down, Side.DOWN, "15m"),
        ]
        book_list = [books.get(token) for token, _, _ in legs_spec]
        if not self._books_ok(now, *book_list):
            return None

        walks = [
            walk_asks(book.asks, self._cfg.MAX_SHARES_PER_PAIR, MAX_LEG_PRICE)
            for book in book_list
        ]
        shares = min(
            [walk.shares for walk in walks] + [self._cfg.MAX_SHARES_PER_PAIR]
        )
        if shares < self._cfg.MIN_SHARES_PER_PAIR:
            return None
        shares = self._fit_notional(
            shares, [book.asks for book in book_list], MAX_LEG_PRICE
        )
        if shares < self._cfg.MIN_SHARES_PER_PAIR:
            return None

        walks = [
            walk_asks(book.asks, shares, MAX_LEG_PRICE)
            for book in book_list
        ]
        if any(walk.shares + 1e-9 < shares for walk in walks):
            return None

        gross = sum(walk.notional for walk in walks)
        if gross <= 0 or gross > self._cfg.MAX_NOTIONAL_PER_PAIR:
            return None
        fees = sum(
            taker_fee(shares, walk.avg_price, self._cfg.FEE_RATE)
            for walk in walks
        )
        fees_buffered = fees * (1.0 + self._cfg.FEE_BUFFER_PCT) + self._cfg.FEE_BUFFER_USD
        floor_profit = 2.0 * shares - gross - fees_buffered
        edge_pct = floor_profit / gross if gross > 0 else 0.0
        if floor_profit < self._cfg.MIN_EDGE_USD:
            return None
        if edge_pct < self._cfg.MIN_EDGE_PCT:
            return None

        legs = [
            LegPlan(
                token_id=token,
                side=side,
                interval=interval,
                shares=shares,
                avg_price=walk.avg_price,
                worst_price=walk.worst_price,
                notional=walk.notional,
                fee=taker_fee(shares, walk.avg_price, self._cfg.FEE_RATE),
                book_ts=book.ts,
            )
            for (token, side, interval), walk, book in zip(
                legs_spec, walks, book_list
            )
        ]
        return Opportunity(
            kind=Kind.COMPLETE,
            asset=pair.asset,
            end_ts=pair.end_ts,
            ts=now,
            legs=legs,
            shares=shares,
            gross_cost=gross,
            fees=fees_buffered,
            floor_profit=floor_profit,
            edge_pct=edge_pct,
            pair=pair,
            detail={
                "payoff_per_set": 2.0,
                "strike_5m": pair.strike_5m,
                "strike_15m": pair.strike_15m,
                "gap_bps": pair.gap_bps,
                "seconds_left": pair.seconds_left(now),
                "fee_rate": self._cfg.FEE_RATE,
                "fees_raw": fees,
                "max_leg_price": MAX_LEG_PRICE,
            },
        )

    # ── revalidation before sending orders ──────────────────────────────
    def redetect(
        self, opp: Opportunity, books: dict[str, Book]
    ) -> Opportunity | None:
        now = time.time()
        if opp.kind == Kind.CROSS and opp.pair is not None:
            return self.detect_cross(opp.pair, books, now)
        if opp.kind == Kind.SAME and opp.market is not None:
            return self.detect_same(opp.market, books, now)
        if opp.kind == Kind.COMPLETE and opp.pair is not None:
            return self.detect_complete(opp.pair, books, now)
        return None

    # ── helpers ─────────────────────────────────────────────────────────
    def _fit_notional(
        self,
        shares: float,
        ladders: list[list[Level]],
        max_price: float,
    ) -> float:
        """Shrink ``shares`` until the walked cost fits the per-pair cap.

        The cap is a dollar budget, not a reason to skip an otherwise
        valid opportunity. The old code rejected ``gross > cap`` outright,
        which silently disabled trading at the default $20 cap whenever
        the leg prices were near 50 cents.
        """
        cap = float(self._cfg.MAX_NOTIONAL_PER_PAIR)
        if cap <= 0:
            return 0.0
        current = float(shares)
        for _ in range(8):
            walks = [
                walk_asks(asks, current, max_price) for asks in ladders
            ]
            available = min(walk.shares for walk in walks)
            if available + 1e-9 < current:
                current = available
                if current < self._cfg.MIN_SHARES_PER_PAIR:
                    return 0.0
                continue
            gross = sum(walk.notional for walk in walks)
            if gross <= cap + 1e-9:
                return current
            # The walk is monotonic in size, so a proportional shrink is
            # an upper bound on the next cost; 0.995 converges from above.
            current *= (cap / gross) * 0.995
            if current < self._cfg.MIN_SHARES_PER_PAIR:
                return 0.0
        return 0.0

    def _books_ok(self, now: float, *books: Book | None) -> bool:
        for book in books:
            if book is None or book.crossed:
                return False
            if book.best_ask is None or book.best_ask <= 0:
                return False
            if now - book.ts > self._cfg.MAX_QUOTE_AGE_S:
                return False
        return True
