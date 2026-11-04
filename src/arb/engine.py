"""Arbitrage orchestration loop.

One process handles BTC and ETH. Every discovery cycle it resolves:

  * the currently active 5m and 15m markets (same-market arb),
  * the 5m/15m pair that shares the next 15m boundary (cross-market arb).

The trading loop is deliberately trigger-driven: it watches the CLOB
WebSocket best bid/ask, and only pulls fresh REST books when the two
executable asks could sum below $1. This keeps the public API load low
while still sizing every order from a depth-aware snapshot.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from src.arb.books import BookService
from src.arb.config import ArbSettings
from src.arb.detector import ArbDetector
from src.arb.discovery import ArbDiscovery, window_starts
from src.arb.executor import PairExecutor
from src.arb.models import ArbMarket, ArbPair, Opportunity
from src.arb.reference import ReferencePriceService
from src.arb.settlement import SettlementWatcher
from src.arb.status import atomic_write_json
from src.arb.store import ArbStore
from src.infra.logging import get_logger
from src.market.pm_client import PolymarketClient

log = get_logger(__name__)

# Skip reasons that are normal steady-state outcomes, not incidents.
_SILENT_SKIPS = {
    "already_executing",
    "cooldown",
    "position_already_open",
    "killswitch",
    "failure_cooldown",
    "max_open_positions",
    "max_open_notional",
    "daily_loss_limit",
    "revalidation_failed",
}


class ArbEngine:
    def __init__(
        self,
        cfg: ArbSettings,
        pm: PolymarketClient,
        ref: ReferencePriceService,
        store: ArbStore,
        books: BookService,
        discovery: ArbDiscovery,
        detector: ArbDetector,
        executor: PairExecutor,
        settlement: SettlementWatcher,
        killswitch=None,
    ) -> None:
        self._cfg = cfg
        self._pm = pm
        self._ref = ref
        self._store = store
        self._books = books
        self._discovery = discovery
        self._detector = detector
        self._executor = executor
        self._settlement = settlement
        self._killswitch = killswitch

        self._pairs: dict[tuple[str, int], ArbPair] = {}
        self._current: dict[tuple[str, str, int], ArbMarket] = {}
        self._pair_state: dict[tuple[str, int], str] = {}
        self._rest_scan_at: dict[tuple, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._last_discovery = 0.0
        self._last_error = ""
        self._last_summary = 0.0
        self._last_execution: dict | None = None
        self._stopped = False

    # ── lifecycle ───────────────────────────────────────────────────────
    async def run(self) -> None:
        await self.start()
        await self._stop.wait()
        await self.stop()

    async def start(self) -> None:
        self._stopped = False
        await self._books.start()
        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="arb-discovery"),
            asyncio.create_task(self._trading_loop(), name="arb-trading"),
            asyncio.create_task(self._status_loop(), name="arb-status"),
            asyncio.create_task(
                self._settlement.run(self._stop), name="arb-settlement"
            ),
        ]
        log.info(
            "arb.engine.started",
            mode="paper" if self._cfg.PAPER_TRADING else "live",
            assets=self._cfg.assets,
            fee_rate=self._cfg.FEE_RATE,
            min_strike_gap_bps=self._cfg.MIN_STRIKE_GAP_BPS,
        )

    def request_stop(self) -> None:
        self._stop.set()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        tasks = self._tasks
        self._tasks = []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self._books.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("arb.engine.ws_stop_failed", error=str(exc))
        try:
            await self._pm.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._ref.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("arb.engine.stopped")

    # ── discovery ───────────────────────────────────────────────────────
    async def _discovery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._discover_all()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"discovery: {exc}"
                log.exception("arb.discovery.crashed", error=str(exc))
            await self._sleep(self._cfg.DISCOVERY_S)

    async def _discover_all(self) -> None:
        now = time.time()
        for asset in self._cfg.assets:
            current = await self._discovery.current_markets(asset, now)
            for market in current:
                self._current[(asset, market.interval, market.start_ts)] = market

            _, _, end15 = window_starts(now)
            pair = await self._discovery.pair(asset, end15, now)
            if pair is not None:
                self._pairs[(asset, end15)] = pair

        # Drop windows that can no longer be traded.
        for key in list(self._current):
            market = self._current[key]
            if market.end_ts < now - 120:
                self._current.pop(key, None)
        for key in list(self._pairs):
            if key[1] < now - 120:
                self._pairs.pop(key, None)
                self._pair_state.pop(key, None)

        tokens: set[str] = set()
        for market in self._current.values():
            tokens.add(market.token_up)
            tokens.add(market.token_down)
        for pair in self._pairs.values():
            for market in (pair.m5, pair.m15):
                tokens.add(market.token_up)
                tokens.add(market.token_down)
        if tokens:
            self._books.subscribe(tokens)

        self._last_discovery = now
        self._log_pair_transitions()

    def _log_pair_transitions(self) -> None:
        for key, pair in self._pairs.items():
            state = "ready" if pair.ready else (pair.reason or "not_ready")
            if self._pair_state.get(key) == state:
                continue
            self._pair_state[key] = state
            log.info(
                "arb.pair.state",
                asset=pair.asset,
                end_ts=pair.end_ts,
                state=state,
                strike_5m=pair.strike_5m,
                strike_15m=pair.strike_15m,
                gap_bps=round(pair.gap_bps, 2) if pair.gap_bps else None,
                complete_ready=pair.markets_ready,
                direction=(
                    f"{pair.direction_5m.value}/{pair.direction_15m.value}"
                    if pair.direction_5m and pair.direction_15m
                    else None
                ),
            )

    # ── trading ─────────────────────────────────────────────────────────
    async def _trading_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._sync_killswitch():
                    await self._sleep(self._cfg.TICK_S)
                    continue
                await self._scan_and_execute()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"trading: {exc}"
                log.exception("arb.trading.crashed", error=str(exc))
            await self._sleep(self._cfg.TICK_S)

    async def _scan_and_execute(self) -> None:
        now = time.time()
        candidates: list[Opportunity] = []

        for pair in list(self._pairs.values()):
            if now > pair.end_ts - self._cfg.STOP_OPEN_BEFORE_END_S:
                continue
            if pair.ready and now >= pair.m5.start_ts + self._cfg.STRIKE_SETTLE_S:
                token5 = pair.m5.token_for(pair.direction_5m)  # type: ignore[arg-type]
                token15 = pair.m15.token_for(pair.direction_15m)  # type: ignore[arg-type]
                opp = await self._detect_cross_pair(pair, token5, token15, now)
                if opp is not None:
                    candidates.append(opp)
            if pair.markets_ready and now >= pair.m5.start_ts:
                opp = await self._detect_complete_pair(pair, now)
                if opp is not None:
                    candidates.append(opp)

        for market in list(self._current.values()):
            if now > market.end_ts - self._cfg.STOP_OPEN_BEFORE_END_S:
                continue
            if market.end_ts - now < self._cfg.MIN_SECONDS_TO_END_S:
                continue
            opp = await self._detect_same_market(market, now)
            if opp is not None:
                candidates.append(opp)

        if not candidates:
            return
        best = max(candidates, key=lambda item: item.floor_profit)
        result = await self._executor.execute(best)
        if result.ok:
            self._last_execution = {
                "ts": time.time(),
                "asset": best.asset,
                "kind": best.kind.value,
                "matched": result.matched_shares,
                "floor_profit": best.floor_profit,
                "position_id": result.position_id,
            }
            log.info(
                "arb.execution",
                asset=best.asset,
                kind=best.kind.value,
                matched=result.matched_shares,
                floor_profit=round(best.floor_profit, 4),
                position_id=result.position_id,
            )
        elif result.error not in _SILENT_SKIPS and result.error:
            log.warning(
                "arb.execution_failed",
                asset=best.asset,
                kind=best.kind.value,
                reason=result.error,
            )

    async def _detect_cross_pair(
        self, pair: ArbPair, token5: str, token15: str, now: float
    ) -> Opportunity | None:
        bbos = self._books.bbo([token5, token15])
        b5, b15 = bbos.get(token5), bbos.get(token15)
        prices_ok = bool(
            b5 and b15 and b5.ask and b15.ask and b5.ask > 0 and b15.ask > 0
        )
        fresh = bool(
            prices_ok
            and b5.ask_age(now) <= self._cfg.MAX_QUOTE_AGE_S
            and b15.ask_age(now) <= self._cfg.MAX_QUOTE_AGE_S
        )
        if prices_ok and fresh and (b5.ask + b15.ask) > 1.0:
            return None
        key = ("cross", pair.asset, pair.end_ts)
        if not fresh and not self._rest_scan_allowed(key, now):
            return None
        books = await self._books.fetch_books([token5, token15], max_age=0.0)
        return self._detector.detect_cross(pair, books, now)

    async def _detect_complete_pair(
        self, pair: ArbPair, now: float
    ) -> Opportunity | None:
        tokens = [
            pair.m5.token_up,
            pair.m5.token_down,
            pair.m15.token_up,
            pair.m15.token_down,
        ]
        bbos = self._books.bbo(tokens)
        quotes = [bbos.get(token) for token in tokens]
        prices_ok = all(
            quote is not None and quote.ask is not None and quote.ask > 0
            for quote in quotes
        )
        fresh = bool(
            prices_ok
            and all(
                quote.ask_age(now) <= self._cfg.MAX_QUOTE_AGE_S  # type: ignore[union-attr]
                for quote in quotes
            )
        )
        if prices_ok and fresh:
            ask_sum = sum(quote.ask for quote in quotes)  # type: ignore[misc]
            if ask_sum > 2.0:
                return None
        key = ("complete", pair.asset, pair.end_ts)
        if not fresh and not self._rest_scan_allowed(key, now):
            return None
        books = await self._books.fetch_books(tokens, max_age=0.0)
        return self._detector.detect_complete(pair, books, now)

    async def _detect_same_market(
        self, market: ArbMarket, now: float
    ) -> Opportunity | None:
        bbos = self._books.bbo([market.token_up, market.token_down])
        b_up, b_down = bbos.get(market.token_up), bbos.get(market.token_down)
        prices_ok = bool(
            b_up
            and b_down
            and b_up.ask
            and b_down.ask
            and b_up.ask > 0
            and b_down.ask > 0
        )
        fresh = bool(
            prices_ok
            and b_up.ask_age(now) <= self._cfg.MAX_QUOTE_AGE_S
            and b_down.ask_age(now) <= self._cfg.MAX_QUOTE_AGE_S
        )
        if prices_ok and fresh and (b_up.ask + b_down.ask) > 1.0:
            return None
        key = ("same", market.asset, market.interval, market.start_ts)
        if not fresh and not self._rest_scan_allowed(key, now):
            return None
        books = await self._books.fetch_books(
            [market.token_up, market.token_down], max_age=0.0
        )
        return self._detector.detect_same(market, books, now)

    def _rest_scan_allowed(self, key: tuple, now: float) -> bool:
        last = self._rest_scan_at.get(key, 0.0)
        if now - last < 1.0:
            return False
        self._rest_scan_at[key] = now
        return True

    # ── status ──────────────────────────────────────────────────────────
    async def _status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._write_status()
                now = time.time()
                if now - self._last_summary >= 30.0:
                    self._last_summary = now
                    self._log_summary(now)
            except Exception as exc:  # noqa: BLE001
                log.warning("arb.status.failed", error=str(exc))
            await self._sleep(self._cfg.STATUS_S)

    def _write_status(self) -> None:
        now = time.time()
        pairs = []
        for pair in sorted(self._pairs.values(), key=lambda p: p.end_ts):
            ask_5 = ask_15 = ask_sum = None
            if pair.direction_5m and pair.direction_15m:
                token5 = pair.m5.token_for(pair.direction_5m)
                token15 = pair.m15.token_for(pair.direction_15m)
                bbo = self._books.bbo([token5, token15])
                b5, b15 = bbo.get(token5), bbo.get(token15)
                ask_5 = b5.ask if b5 else None
                ask_15 = b15.ask if b15 else None
                if ask_5 and ask_15:
                    ask_sum = round(ask_5 + ask_15, 4)
            pairs.append(
                {
                    "asset": pair.asset,
                    "end_ts": pair.end_ts,
                    "end_utc": datetime.fromtimestamp(
                        pair.end_ts, tz=timezone.utc
                    ).strftime("%H:%M:%S"),
                    "strike_5m": pair.strike_5m,
                    "strike_15m": pair.strike_15m,
                    "gap_bps": pair.gap_bps,
                    "direction": (
                        f"{pair.direction_5m.value}/{pair.direction_15m.value}"
                        if pair.direction_5m and pair.direction_15m
                        else None
                    ),
                    "ask_5m": ask_5,
                    "ask_15m": ask_15,
                    "ask_sum": ask_sum,
                    "ready": pair.ready,
                    "complete_ready": pair.markets_ready,
                    "reason": pair.reason,
                    "seconds_left": round(pair.seconds_left(now), 1),
                }
            )

        opportunities = []
        for row in self._store.recent_opportunities(20):
            opportunities.append(
                {
                    "id": row.get("id"),
                    "ts": (row.get("ts_ms") or 0) / 1000.0,
                    "asset": row.get("asset"),
                    "kind": row.get("kind"),
                    "shares": row.get("shares"),
                    "gross_cost": row.get("gross_cost"),
                    "fees": row.get("fees"),
                    "floor_profit": row.get("floor_profit"),
                    "edge_pct": row.get("edge_pct"),
                    "executed": bool(row.get("executed")),
                }
            )

        positions = []
        for row in self._store.recent_positions(20):
            positions.append(
                {
                    "id": row.get("id"),
                    "asset": row.get("asset"),
                    "kind": row.get("kind"),
                    "shares": row.get("shares"),
                    "buy_cost": row.get("buy_cost"),
                    "sell_proceeds": row.get("sell_proceeds"),
                    "fees": row.get("fees"),
                    "status": row.get("status"),
                    "pnl": row.get("pnl"),
                }
            )

        fills = []
        for row in self._store.recent_fills(30):
            fills.append(
                {
                    "id": row.get("id"),
                    "ts": (row.get("ts_ms") or 0) / 1000.0,
                    "asset": row.get("asset"),
                    "kind": row.get("kind"),
                    "side": row.get("side"),
                    "price": row.get("price"),
                    "size": row.get("size"),
                    "fee": row.get("fee"),
                    "role": row.get("role"),
                    "position_id": row.get("position_id"),
                    "order_id": (row.get("order_id") or "")[:16],
                    "note": row.get("note"),
                }
            )

        payload = {
            "ts": now,
            "mode": "paper" if self._cfg.PAPER_TRADING else "live",
            "assets": self._cfg.assets,
            "fee_rate": self._cfg.FEE_RATE,
            "killswitch": (
                self._killswitch.snapshot()
                if self._killswitch is not None
                else {"active": False}
            ),
            "pairs": pairs,
            "positions": positions,
            "fills": fills,
            "opportunities": opportunities,
            "stats": self._store.stats(),
            "last_execution": self._last_execution,
            "health": {
                "ws_connected": self._books_connected(),
                "last_discovery_ago": (
                    round(now - self._last_discovery, 1)
                    if self._last_discovery
                    else None
                ),
                "last_error": self._last_error or None,
            },
        }
        atomic_write_json(self._cfg.status_path, payload)

    def _books_connected(self) -> bool:
        return bool(getattr(self._books, "ws_connected", False))

    def _log_summary(self, now: float) -> None:
        stats = self._store.stats()
        ready = sum(1 for pair in self._pairs.values() if pair.ready)
        log.info(
            "arb.summary",
            assets=self._cfg.assets,
            pairs=len(self._pairs),
            ready_pairs=ready,
            open_positions=stats["open_positions"],
            open_cost=round(stats["open_cost"], 4),
            realized_today=round(stats["realized_today"], 4),
            opportunities=stats["opportunities"],
            last_error=self._last_error or None,
        )

    # ── helpers ─────────────────────────────────────────────────────────
    def _sync_killswitch(self) -> bool:
        """Reload the on-disk kill switch so a file write stops the bot live.

        ``KillSwitch.load`` only runs at startup. Without this poll, the
        operator could write ``data/arb_emergency_stop.json`` and the
        already-running process would keep trading. The file is deleted to
        release, matching ``KillSwitch.release()``.
        """
        ks = self._killswitch
        if ks is None:
            return False
        path = getattr(ks, "path", None)
        if path is None:
            return bool(getattr(ks, "active", False))
        try:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                active = bool(raw.get("active"))
                reason = str(raw.get("reason") or "manual")
                since = float(raw.get("since") or 0.0)
            else:
                active, reason, since = False, "", 0.0
        except Exception as exc:  # noqa: BLE001
            # A corrupt control file must not wedge the bot; keep the last
            # known state and surface the problem.
            log.warning("arb.killswitch.read_failed", error=str(exc))
            return bool(getattr(ks, "active", False))
        if active != bool(getattr(ks, "active", False)):
            ks.active = active
            ks.reason = reason if active else ""
            ks.since = (since or time.time()) if active else 0.0
            log.warning(
                "arb.killswitch.changed",
                active=active,
                reason=ks.reason or None,
                path=str(path),
            )
        return bool(ks.active)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.01, seconds))
        except asyncio.TimeoutError:
            pass
