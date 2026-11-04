"""Two-leg execution with explicit leg-risk handling.

Paper and live brokers expose the same tiny interface. A ``PaperBroker``
fills against the REST book snapshot; a ``LiveBroker`` posts FAK orders
through the existing ``CLOBClient``.

The executor sizes both legs from the same snapshot, fires them
concurrently, and then reconciles the filled sizes:

  * matched  = min(size_leg_a, size_leg_b)
  * excess   = size_leg - matched on the larger leg

The excess is sold immediately (``LEG_RISK_POLICY="unwind"``) or, if
configured, the shortfall is first bought at a bounded slippage
(``"complete"``). A position that cannot be unwound is still recorded and
settled truthfully; it is flagged as a leg-risk event because it is no
longer payoff-risk-free.
"""
from __future__ import annotations

import asyncio
import time
from typing import Protocol

from src.arb.books import BookService
from src.arb.config import ArbSettings
from src.arb.detector import ArbDetector
from src.arb.fees import taker_fee, walk_bids, walk_budget
from src.arb.models import (
    Book,
    ExecutionResult,
    Fill,
    Kind,
    LegPlan,
    Opportunity,
)
from src.arb.store import ArbStore
from src.execution.clob_client import CLOBClient
from src.infra.logging import get_logger

log = get_logger(__name__)

_EPS = 1e-9


class Broker(Protocol):
    async def buy(
        self,
        token_id: str,
        amount_usdc: float,
        limit_price: float,
        tick_size: float,
        book: Book | None,
    ) -> Fill | None: ...

    async def sell(
        self,
        token_id: str,
        shares: float,
        reference_price: float,
        tick_size: float,
        book: Book | None,
    ) -> Fill | None: ...


class PaperBroker:
    def __init__(self, cfg: ArbSettings) -> None:
        self._cfg = cfg

    async def buy(
        self,
        token_id: str,
        amount_usdc: float,
        limit_price: float,
        tick_size: float,
        book: Book | None,
    ) -> Fill | None:
        if book is None or not book.asks:
            return None
        walk = walk_budget(book.asks, amount_usdc, limit_price)
        if walk.shares <= _EPS:
            return None
        fee = taker_fee(walk.shares, walk.avg_price, self._cfg.FEE_RATE)
        return Fill(
            token_id=token_id,
            side="BUY",
            price=walk.avg_price,
            size=walk.shares,
            fee=fee,
            ts=time.time(),
            note="paper",
        )

    async def sell(
        self,
        token_id: str,
        shares: float,
        reference_price: float,
        tick_size: float,
        book: Book | None,
    ) -> Fill | None:
        if book is None or not book.bids:
            return None
        floor = reference_price * (1.0 - self._cfg.SELL_SLIPPAGE_PCT / 100.0)
        # Match the live FAK SELL's limit semantics: a bid below the
        # slippage floor is not crossed. The caller retries / carries the
        # excess instead of pretending a 1-cent bid filled.
        walk = walk_bids(book.bids, shares, min_price=max(0.0, floor))
        if walk.shares <= _EPS:
            return None
        fee = taker_fee(walk.shares, walk.avg_price, self._cfg.FEE_RATE)
        return Fill(
            token_id=token_id,
            side="SELL",
            price=walk.avg_price,
            size=walk.shares,
            fee=fee,
            ts=time.time(),
            note="paper_unwind",
        )


class LiveBroker:
    def __init__(self, cfg: ArbSettings, clob: CLOBClient) -> None:
        self._cfg = cfg
        self._clob = clob

    async def buy(
        self,
        token_id: str,
        amount_usdc: float,
        limit_price: float,
        tick_size: float,
        book: Book | None,
    ) -> Fill | None:
        result = await self._clob.place_fak_buy(
            token_id=token_id,
            amount=amount_usdc,
            price=limit_price,
            tick_size=tick_size,
        )
        if not result.success or result.tokens <= 0:
            return None
        fee = taker_fee(result.tokens, result.fill_price, self._cfg.FEE_RATE)
        return Fill(
            token_id=token_id,
            side="BUY",
            price=result.fill_price,
            size=result.tokens,
            fee=fee,
            order_id=result.order_id,
            ts=time.time(),
            note="live",
        )

    async def sell(
        self,
        token_id: str,
        shares: float,
        reference_price: float,
        tick_size: float,
        book: Book | None,
    ) -> Fill | None:
        result = await self._clob.place_fak_sell_marketable(
            token_id=token_id,
            size=shares,
            reference_price=reference_price,
            tick_size=tick_size,
            slippage_pct=self._cfg.SELL_SLIPPAGE_PCT,
        )
        if not result.success or result.tokens <= 0:
            return None
        fee = taker_fee(result.tokens, result.fill_price, self._cfg.FEE_RATE)
        return Fill(
            token_id=token_id,
            side="SELL",
            price=result.fill_price,
            size=result.tokens,
            fee=fee,
            order_id=result.order_id,
            ts=time.time(),
            note="live_unwind",
        )


class PairExecutor:
    def __init__(
        self,
        cfg: ArbSettings,
        store: ArbStore,
        books: BookService,
        broker: Broker,
        detector: ArbDetector,
        killswitch=None,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._books = books
        self._broker = broker
        self._detector = detector
        self._killswitch = killswitch
        self._locks: dict[tuple[str, int, str], asyncio.Lock] = {}
        self._cooldown_until: dict[tuple[str, int, str], float] = {}
        self._consecutive_failures = 0
        self._failure_until = 0.0

    async def execute(self, opp: Opportunity) -> ExecutionResult:
        key = (opp.asset, int(opp.end_ts), opp.kind.value)
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return self._skip("already_executing")
        async with lock:
            return await self._execute_locked(key, opp)

    async def _execute_locked(
        self, key: tuple[str, int, str], opp: Opportunity
    ) -> ExecutionResult:
        now = time.time()
        if self._killswitch is not None and getattr(self._killswitch, "active", False):
            return self._skip("killswitch")
        if now < self._failure_until:
            return self._skip("failure_cooldown")
        if now < self._cooldown_until.get(key, 0.0):
            return self._skip("cooldown")
        if self._has_open(key):
            return self._skip("position_already_open")
        if self._open_count() >= self._cfg.MAX_OPEN_POSITIONS:
            return self._skip("max_open_positions")
        if self._store.realized_pnl_today() <= -abs(self._cfg.MAX_DAILY_LOSS_USD):
            return self._skip("daily_loss_limit")

        opp_id = self._store.record_opportunity(opp)
        tokens = [leg.token_id for leg in opp.legs]
        books = await self._books.fetch_books(tokens, max_age=0.0)
        fresh = self._detector.redetect(opp, books)
        if fresh is None:
            return self._skip("revalidation_failed")

        max_spend = sum(
            leg.shares * max(leg.worst_price, 0.01) for leg in fresh.legs
        )
        if self._open_cost() + max_spend > self._cfg.MAX_OPEN_NOTIONAL:
            return self._skip("max_open_notional")

        tick_by_token = self._ticks_for(fresh)
        amounts = [
            leg.shares * max(leg.worst_price, 0.01) for leg in fresh.legs
        ]
        results = await asyncio.gather(
            *(
                self._broker.buy(
                    leg.token_id,
                    amount,
                    max(leg.worst_price, 0.01),
                    tick_by_token[leg.token_id],
                    books.get(leg.token_id),
                )
                for leg, amount in zip(fresh.legs, amounts)
            ),
            return_exceptions=True,
        )
        fills: list[Fill] = []
        for result in results:
            if isinstance(result, Fill):
                fills.append(result)
            elif isinstance(result, Exception):
                log.warning("arb.executor.leg_exception", error=str(result))

        if not fills:
            self._note_failure("both_legs_failed")
            return ExecutionResult(
                ok=False,
                position_id=None,
                matched_shares=0.0,
                buy_cost=0.0,
                sell_proceeds=0.0,
                fees=0.0,
                fills=[],
                error="both_legs_failed",
            )

        self._store.mark_opportunity_executed(opp_id)
        self._cooldown_until[key] = time.time() + self._cfg.COOLDOWN_S
        return await self._reconcile(fresh, fills, tick_by_token, books, opp_id)

    # ── reconciliation ──────────────────────────────────────────────────
    async def _reconcile(
        self,
        opp: Opportunity,
        fills: list[Fill],
        ticks: dict[str, float],
        books: dict[str, Book],
        opp_id: int,
    ) -> ExecutionResult:
        buy_fills = [f for f in fills if f.side == "BUY"]
        size_by_token: dict[str, float] = {f.token_id: f.size for f in buy_fills}
        sizes = [size_by_token.get(leg.token_id, 0.0) for leg in opp.legs]
        matched = min(sizes) if sizes else 0.0

        # Optional completion: when the two FAK buys return different
        # sizes, buy the shortfall on the smaller leg up to a bounded
        # slippage. The default ("unwind") skips this and sells the excess.
        if (
            str(self._cfg.LEG_RISK_POLICY).lower() == "complete"
            and matched > _EPS
            and max(sizes) - matched > _EPS
        ):
            target = max(sizes)
            small_idx = sizes.index(min(sizes))
            small_leg = opp.legs[small_idx]
            extra = target - matched
            tick = ticks.get(small_leg.token_id, 0.01)
            limit = min(
                0.99,
                small_leg.worst_price
                + max(0, int(self._cfg.MAX_LEG_SLIPPAGE_TICKS)) * tick,
            )
            fresh_books = await self._books.fetch_books(
                [small_leg.token_id], max_age=0.0
            )
            extra_fill = await self._broker.buy(
                small_leg.token_id,
                extra * max(limit, 0.01),
                max(limit, 0.01),
                tick,
                fresh_books.get(small_leg.token_id),
            )
            if extra_fill is not None and extra_fill.size > _EPS:
                buy_fills.append(extra_fill)
                size_by_token[small_leg.token_id] = (
                    size_by_token.get(small_leg.token_id, 0.0)
                    + extra_fill.size
                )
                sizes = [size_by_token.get(leg.token_id, 0.0) for leg in opp.legs]
                matched = min(sizes)
                log.info(
                    "arb.executor.completed_leg",
                    token=small_leg.token_id[:16],
                    extra=extra_fill.size,
                    limit=limit,
                    matched=matched,
                )

        detail_legs = []
        for leg in opp.legs:
            bought = size_by_token.get(leg.token_id, 0.0)
            detail_legs.append(
                {
                    "token_id": leg.token_id,
                    "side": leg.side.value,
                    "interval": leg.interval,
                    "bought": bought,
                    "sold": 0.0,
                    "avg_price": leg.avg_price,
                    "tick_size": ticks.get(leg.token_id, 0.01),
                    "condition_id": self._condition_for(opp, leg.interval),
                    "slug": self._slug_for(opp, leg.interval),
                }
            )

        buy_cost = sum(f.notional for f in buy_fills)
        fees = sum(f.fee for f in buy_fills)
        sell_proceeds = 0.0
        all_fills: list[Fill] = list(buy_fills)
        leg_risk = matched <= _EPS

        # Sell excess above the matched size on every leg.
        for leg in opp.legs:
            bought = size_by_token.get(leg.token_id, 0.0)
            excess = bought - matched
            if excess <= _EPS:
                continue
            sell = await self._unwind(
                leg, excess, ticks.get(leg.token_id, 0.01), books.get(leg.token_id)
            )
            if sell is not None:
                all_fills.append(sell)
                sell_proceeds += sell.notional
                fees += sell.fee
                for dl in detail_legs:
                    if dl["token_id"] == leg.token_id:
                        dl["sold"] += sell.size
            else:
                leg_risk = True
                log.error(
                    "arb.executor.unwind_failed",
                    asset=opp.asset,
                    token=leg.token_id[:16],
                    excess=excess,
                    note="naked excess carried to settlement; no longer risk-free",
                )

        held = sum(max(0.0, dl["bought"] - dl["sold"]) for dl in detail_legs)
        if matched <= _EPS:
            # Nothing matched. Either both legs failed (handled earlier) or
            # one filled and was unwound. Book the round-trip PnL now.
            pnl = sell_proceeds - buy_cost - fees
            status = "settled" if held <= _EPS else "open"
            position_id = self._store.create_position(
                asset=opp.asset,
                end_ts=opp.end_ts,
                kind=opp.kind.value,
                shares=0.0,
                buy_cost=buy_cost,
                sell_proceeds=sell_proceeds,
                fees=fees,
                detail={
                    "opp_id": opp_id,
                    "legs": detail_legs,
                    "leg_risk": leg_risk,
                    "kind": opp.kind.value,
                },
                status=status,
            )
            for fill in all_fills:
                self._store.add_fill(position_id, fill)
            if status == "settled":
                self._store.update_position(
                    position_id,
                    status="settled",
                    pnl=pnl,
                    settled_ms=int(time.time() * 1000),
                )
            self._note_failure("zero_matched")
            return ExecutionResult(
                ok=False,
                position_id=position_id,
                matched_shares=0.0,
                buy_cost=buy_cost,
                sell_proceeds=sell_proceeds,
                fees=fees,
                fills=all_fills,
                error="zero_matched",
            )

        position_id = self._store.create_position(
            asset=opp.asset,
            end_ts=opp.end_ts,
            kind=opp.kind.value,
            shares=matched,
            buy_cost=buy_cost,
            sell_proceeds=sell_proceeds,
            fees=fees,
            detail={
                "opp_id": opp_id,
                "legs": detail_legs,
                "leg_risk": leg_risk,
                "kind": opp.kind.value,
                "strike_5m": opp.detail.get("strike_5m"),
                "strike_15m": opp.detail.get("strike_15m"),
                "gap_bps": opp.detail.get("gap_bps"),
                "floor_profit_planned": opp.floor_profit,
            },
            status="open" if held > _EPS else "settled",
        )
        for fill in all_fills:
            self._store.add_fill(position_id, fill)

        if held <= _EPS:
            pnl = sell_proceeds - buy_cost - fees
            self._store.update_position(
                position_id,
                status="settled",
                pnl=pnl,
                settled_ms=int(time.time() * 1000),
            )

        self._consecutive_failures = 0
        log.info(
            "arb.executor.position",
            asset=opp.asset,
            kind=opp.kind.value,
            end_ts=opp.end_ts,
            matched=matched,
            buy_cost=buy_cost,
            sell_proceeds=sell_proceeds,
            fees=fees,
            leg_risk=leg_risk,
            position_id=position_id,
        )
        return ExecutionResult(
            ok=True,
            position_id=position_id,
            matched_shares=matched,
            buy_cost=buy_cost,
            sell_proceeds=sell_proceeds,
            fees=fees,
            fills=all_fills,
            error="",
        )

    async def _unwind(
        self,
        leg: LegPlan,
        shares: float,
        tick_size: float,
        book: Book | None,
    ) -> Fill | None:
        remaining = float(shares)
        total_size = 0.0
        total_notional = 0.0
        total_fee = 0.0
        order_id = ""
        for attempt in range(max(1, int(self._cfg.UNWIND_RETRIES))):
            fresh_books = await self._books.fetch_books(
                [leg.token_id], max_age=0.0
            )
            fresh = fresh_books.get(leg.token_id) or book
            reference = fresh.best_bid if fresh and fresh.best_bid else leg.avg_price
            sell = await self._broker.sell(
                leg.token_id,
                remaining,
                reference,
                tick_size,
                fresh,
            )
            if sell is not None and sell.size > _EPS:
                total_size += sell.size
                total_notional += sell.notional
                total_fee += sell.fee
                order_id = sell.order_id or order_id
                remaining -= sell.size
                if remaining <= _EPS:
                    break
            log.warning(
                "arb.executor.unwind_retry",
                token=leg.token_id[:16],
                attempt=attempt + 1,
                remaining=remaining,
            )
            await asyncio.sleep(
                max(0.0, float(self._cfg.UNWIND_RETRY_DELAY_S)) * (attempt + 1)
            )
        if total_size <= _EPS:
            return None
        return Fill(
            token_id=leg.token_id,
            side="SELL",
            price=total_notional / total_size,
            size=total_size,
            fee=total_fee,
            order_id=order_id,
            ts=time.time(),
            note="unwind",
        )

    # ── helpers ─────────────────────────────────────────────────────────
    def _ticks_for(self, opp: Opportunity) -> dict[str, float]:
        ticks: dict[str, float] = {}
        if opp.kind in (Kind.CROSS, Kind.COMPLETE) and opp.pair is not None:
            ticks[opp.pair.m5.token_up] = opp.pair.m5.tick_size
            ticks[opp.pair.m5.token_down] = opp.pair.m5.tick_size
            ticks[opp.pair.m15.token_up] = opp.pair.m15.tick_size
            ticks[opp.pair.m15.token_down] = opp.pair.m15.tick_size
        elif opp.market is not None:
            ticks[opp.market.token_up] = opp.market.tick_size
            ticks[opp.market.token_down] = opp.market.tick_size
        for leg in opp.legs:
            ticks.setdefault(leg.token_id, 0.01)
        return ticks

    def _condition_for(self, opp: Opportunity, interval: str) -> str:
        if opp.kind in (Kind.CROSS, Kind.COMPLETE) and opp.pair is not None:
            market = opp.pair.m5 if interval == "5m" else opp.pair.m15
            return market.condition_id
        return opp.market.condition_id if opp.market else ""

    def _slug_for(self, opp: Opportunity, interval: str) -> str:
        if opp.kind in (Kind.CROSS, Kind.COMPLETE) and opp.pair is not None:
            market = opp.pair.m5 if interval == "5m" else opp.pair.m15
            return market.slug
        return opp.market.slug if opp.market else ""

    def _has_open(self, key: tuple[str, int, str]) -> bool:
        """One position per (asset, end time), regardless of strategy kind.

        The cross, complete-set and same-market variants on the same window
        are correlated; allowing two of them would silently double the
        intended exposure on one settlement print.
        """
        asset, end_ts, _kind = key
        for pos in self._store.open_positions():
            if (
                pos.get("asset") == asset
                and int(pos.get("end_ts") or 0) == end_ts
            ):
                return True
        return False

    def _open_count(self) -> int:
        return len(self._store.open_positions())

    def _open_cost(self) -> float:
        return sum(
            float(p.get("buy_cost") or 0) - float(p.get("sell_proceeds") or 0)
            for p in self._store.open_positions()
        )

    def _note_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        log.warning(
            "arb.executor.failure",
            reason=reason,
            consecutive=self._consecutive_failures,
        )
        if self._consecutive_failures >= max(1, self._cfg.MAX_CONSECUTIVE_FAILURES):
            self._failure_until = time.time() + self._cfg.FAILURE_COOLDOWN_S
            self._consecutive_failures = 0
            log.error(
                "arb.executor.circuit_breaker",
                cooldown_s=self._cfg.FAILURE_COOLDOWN_S,
            )

    @staticmethod
    def _skip(reason: str) -> ExecutionResult:
        log.debug("arb.executor.skip", reason=reason)
        return ExecutionResult(
            ok=False,
            position_id=None,
            matched_shares=0.0,
            buy_cost=0.0,
            sell_proceeds=0.0,
            fees=0.0,
            fills=[],
            error=reason,
        )
