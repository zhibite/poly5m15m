"""Polymarket multi-asset 1h Up/Down — pure GTD Martingale Bot.

One process trades BTC, ETH, BNB, XRP, SOL, DOGE and HYPE independently.
Each asset gets its own StrategyEngine / Position / 2-4-8-16 ladder; the
CLOB client, OrderManager, UserWS, market WS and dashboard are shared.

Entry point. Coordinates:
  1. WindowManager — one hourly boundary for all seven markets
  2. MarketWS      — shared CLOB market data WS (mid/last trade)
  3. MarketService — Gamma REST market discovery
  4. StrategyEngine (×7) — pure price martingale, no signal gates
  5. OrderManager  — serialised order executor shared by every asset
  6. TierWatcher   — one per in-flight GTD, passive lifecycle observer
  7. HeartbeatLoop — independent keepalive

Usage:
  pip install -e .              # install dependencies
  cp .env.example .env          # fill in credentials
  python -m src.main            # start bot

PAPER_TRADING selects local simulation vs live CLOB orders; the mode is
locked per round at T1 and can be changed from the dashboard.

H-NEW-2026-07-11 refactor:
  - BUY/TP GTDs post at the **strategy price exactly** (no orderbook snap).
  - Trigger lines use `PRICE_PRE_MARGIN` to pre-arm GTDs early:
      BUY : mid <= strategy_price * (1 + margin)  → post at strategy_price
      SELL: mid >= strategy_price * (1 - margin)  → post at strategy_price
  - Each GTD is monitored by a TierWatcher (token-balance polling) that
    cancels the GTD once enough shares have been filled, when the GTD
    expires, or when the next tier's zone has been crossed.
  - The orderbook (WS bba + REST /book fallback) is no longer consulted
    on the BUY/SELL paths — only the midpoint is used for triggering.
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from typing import Optional

from src.config import binance, general, log_config, strategy
from src.clob_l1_auth import create_or_derive_clob_api_creds, ClobL1Error
from src.execution.clob_client import CLOBClient
from src.execution.gtd_expiry_watcher import GtdExpiryWatcher
from src.execution.heartbeat import HeartbeatLoop
from src.execution.mode_router import ModeRouter, RouterGroup
from src.execution.order_manager import OrderManager
from src.execution.paper_broker import PaperBroker
from src.execution.tier_watcher import TierWatcher, WatcherResult
from src.feeds.binance_futures import MacroSignals
from src.feeds.binance_spot import BinanceCache, BinanceFeed, BinanceSpotService, RingBuffer
from src.infra.logging import configure_logging, get_logger
from src.market.market_service import MarketService
from src.market.pm_client import PolymarketClient
from src.market.window_manager import WindowManager
from src.market.market_ws import MarketWS, PriceCache
from src.market.signal_market import Signal5mFeed
from src.market.user_ws import UserWS
from src.persistence.db import DB, init_db
from src.strategy.engine import (
    Action,
    ActionCancelTier,
    ActionOpenT1,
    ActionOpenTN,
    ActionStopLoss,
    ActionTakeProfit,
    StrategyEngine,
)
from src.strategy import event_calendar
from src.strategy.killswitch import KillSwitch
from src.strategy.loop import DecisionLoop
from src.strategy.signals import SignalEngine

log = get_logger(__name__)


def _watcher_role(result: object) -> str | None:
    """Maker/taker role carried by a WatcherResult.

    Prefers the tri-state `trader_side` (TAKER / MAKER / None) the
    TierWatcher derives from the order's trades; falls back to the older
    `is_taker` bool so partially-updated callers keep working.
    """
    role = getattr(result, "trader_side", None)
    if role in ("TAKER", "MAKER"):
        return role
    return "TAKER" if getattr(result, "is_taker", False) else None


# ---------------------------------------------------------------------------
# Web dashboard server
# ---------------------------------------------------------------------------

class _DashboardServer:
    """uvicorn.Server subclass that keeps the bot's signal handlers.

    Imported lazily (inside `_start_dashboard`) so the bot still runs if
    FastAPI/uvicorn are not installed. The default `uvicorn.Server.serve()`
    replaces SIGINT/SIGTERM handlers; the bot registers its own via
    `loop.add_signal_handler` and relies on them for a graceful round
    shutdown (cancel open GTDs), so signal capture must be disabled.
    """

    @staticmethod
    def build():  # -> uvicorn.Server
        import contextlib

        import uvicorn

        class _Server(uvicorn.Server):
            def capture_signals(self):  # type: ignore[override]
                return contextlib.nullcontext()

        cfg = strategy()
        return _Server(
            uvicorn.Config(
                _web_app(),
                host=cfg.WEB_HOST,
                port=int(cfg.WEB_PORT),
                log_level=cfg.WEB_LOG_LEVEL,
                access_log=False,
                lifespan="off",
            )
        )


def _web_app():
    from src.web.app import app, set_state

    # Bind the LIVE state object. The bot runs as `python -m src.main`, so
    # `import src.main` inside the web package would re-execute the module
    # under a different name and hand the routes an empty AppState.
    set_state(STATE)
    return app


def _start_dashboard() -> "asyncio.Task | None":
    """Start the FastAPI dashboard on the bot's event loop.

    Returns the server task, or None when disabled / failed to start.
    Failures are logged and never abort the trading process.
    """
    cfg = strategy()
    if not cfg.WEB_ENABLED:
        log.info("web.disabled", note="WEB_ENABLED=0")
        return None
    try:
        server = _DashboardServer.build()
        task = asyncio.create_task(server.serve(), name="web-dashboard")

        def _on_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                log.error("web.server_exited", error=str(exc))

        task.add_done_callback(_on_done)
        log.info(
            "web.started",
            host=cfg.WEB_HOST,
            port=int(cfg.WEB_PORT),
            auth=bool(cfg.WEB_PASSWORD),
        )
        if not cfg.WEB_PASSWORD:
            log.warning(
                "web.auth_disabled",
                note="set WEB_PASSWORD (and optionally WEB_USERNAME) to "
                     "require HTTP Basic auth on the dashboard",
            )
        return task
    except Exception as exc:  # noqa: BLE001
        log.warning("web.start_failed", error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Window-open BTC baseline plumbing
# ---------------------------------------------------------------------------

def _on_window_baseline(
    asset: str, price: float, source: str, round_start_ts: int = 0
) -> None:
    """WindowManager -> GateKeeper bridge.

    Receives the BTC spot captured at each window's open (K-line backtrack),
    and applies it as the gate baseline. Source tag identifies the call site
    so logs can attribute each baseline update.

    Sync callback (WindowManager.tick() is async but this fires from inside
    the synchronous portion after await fetch_at_window_open). Mutation is
    idempotent: set_binance_baseline overwrites the existing baseline every
    time it fires, so we add a small guard so we don't reset on every tick
    of the same window — only the first applied value per round sticks.
    """
    if asset != "BTC":
        return
    eng = STATE.engine
    if not eng:
        return
    # `round_start_ts` comes from the market the price belongs to; fall back
    # to STATE for older callers. Using STATE.round_start_ts alone was wrong:
    # WindowManager.tick() runs BEFORE `_on_new_round`, so at boot it was
    # still 0 (production log 2026-10-30 showed `round_start_ts=0`) and the
    # dedupe key then mismatched the real round, re-applying the same
    # baseline on the next discovery.
    target_round_ts = round_start_ts or STATE.round_start_ts
    # Idempotency: only apply once per round. Without this, every
    # window_discovery within the same 15-min window would reset the
    # baseline to the same K-line value and re-flush logs.
    if getattr(eng, "_last_applied_baseline_round_ts", None) == target_round_ts:
        return
    eng.set_binance_baseline(price)
    eng._last_applied_baseline_round_ts = target_round_ts
    log.info(
        "gate.baseline_applied",
        asset=asset,
        baseline=price,
        source=source,
        round_start_ts=target_round_ts,
        btc_now=STATE.binance_cache.last_price,
    )


# ---------------------------------------------------------------------------
# Shared mutable state
# ---------------------------------------------------------------------------

class AppState:
    """Bundles all shared state for the application."""

    def __init__(self) -> None:
        self.round_start_ts: int = 0
        # Emergency stop. Loaded from disk so a restart/redeploy does not
        # silently resume trading after the operator pressed stop.
        self.killswitch = KillSwitch.load()
        # ── Multi-asset universe (2026-11-03) ─────────────────────────────
        # Every asset gets its own StrategyEngine / Position / router /
        # paper broker; the CLOB client, OrderManager, UserWS, market WS,
        # price cache and dashboard are shared.
        self.assets: list[str] = []
        self.engines: dict[str, "StrategyEngine"] = {}
        self.markets: dict[str, object | None] = {}
        self.market_tokens: dict[str, set[str]] = {}
        self.condition_ids: set[str] = set()
        self.live_routers: dict[str, "ActionRouter"] = {}
        self.mode_routers: dict[str, object] = {}
        self.paper_brokers: dict[str, PaperBroker] = {}
        self.decision_loops: dict[str, DecisionLoop] = {}
        self.gtd_expiries: dict[str, GtdExpiryWatcher] = {}
        # External-signal layer is no longer wired (pure martingale); these
        # attributes remain for older tests/dashboard code paths.
        self.event_calendar = None
        self.macro_signals: MacroSignals | None = None
        self.signal_5m: Signal5mFeed | None = None
        self.signals: SignalEngine | None = None
        self.binance_cache = BinanceCache()
        self.price_cache = PriceCache()
        self.ring = RingBuffer(retention_s=binance().BUFFER_RETENTION_S)
        self.db_conn = init_db()
        self.db = DB(self.db_conn)
        self.current_market = None   # MarketInfo | None
        self.engine: StrategyEngine | None = None
        self.binance_feed: BinanceFeed | None = None
        self.market_ws: MarketWS | None = None
        self.user_ws: UserWS | None = None
        self.order_manager: OrderManager | None = None
        self.paper_broker: PaperBroker | None = None
        self.heartbeat: HeartbeatLoop | None = None
        self.gtd_expiry: GtdExpiryWatcher | None = None
        self.decision_loop: DecisionLoop | None = None
        self.clob: CLOBClient | None = None  # reference for fill_callback wiring
        # H-NEW-2026-07-11: ActionRouter reference (set in __init__ of
        # ActionRouter). _on_new_round uses it to stop all TierWatchers
        # before the next round starts so stale watcher callbacks cannot
        # mutate the new round's position state.
        self.router = None
        self.pm_client: PolymarketClient | None = None
        self.market_svc: MarketService | None = None
        self.wm: WindowManager | None = None  # window lifecycle manager
        self.binance_spot: BinanceSpotService | None = None  # BTC spot via REST
        self._shutdown = asyncio.Event()
        # B-3 v2 fix: hold strong references to fire-and-forget background
        # tasks (currently the TP runner spawned by ActionRouter) so they
        # cannot be garbage-collected before they finish. Without this, a
        # CPython GC pass happening between `asyncio.create_task(...)` and
        # the task's first `await` can drop the task entirely — silently
        # aborting the TP sell path even though the GTD BUY is live on the
        # CLOB. Mirrors the same pattern used in `GtdExpiryWatcher._bg_tasks`.
        self._bg_tasks: set[asyncio.Task[None]] = set()

    # ── Multi-asset routing helpers ──────────────────────────────────────

    def engine_for_order(self, server_order_id: str):
        """Resolve the engine that owns a server order id."""
        om = getattr(self, "order_manager", None)
        tracked = om.by_server_id(server_order_id) if om else None
        if tracked is None:
            return None
        return self.engines.get(str(tracked.asset).upper())

    def engine_for_token(self, token_id: str):
        """Resolve the engine that owns an outcome token id."""
        for asset, tokens in self.market_tokens.items():
            if token_id in tokens:
                return self.engines.get(asset)
        return None

    def watcher_for_order(self, server_order_id: str):
        """Find the per-asset TierWatcher tracking a server order id."""
        for router in self.live_routers.values():
            watcher = router.watcher_for_server_order(server_order_id)
            if watcher is not None:
                return watcher
        return None


STATE = AppState()


# ---------------------------------------------------------------------------
# Action router — bridges StrategyEngine -> OrderManager + Position state
# ---------------------------------------------------------------------------


# A freshly bought conditional-token position can legitimately read 0 from
# the CLOB balance endpoint for a few seconds while settlement catches up.
# Never treat that as "the wallet is empty" inside this window — production
# 2026-10-31 14:00 UTC: a 1u taker T1 buy (2.857 shares) was followed ~0.3 s
# later by a taker TP that read balance 0, and the reconcile wiped a real
# position out of the engine.
_POSITION_SETTLE_GRACE_S = 10.0


async def _maybe_realize_closed(token_id: str, clob, pos) -> None:
    """If the CLOB reports zero token balance for `token_id`, instruct
    `Position.realize_position_closed()` to drop the phantom inventory.

    `clob` is the CLOBClient itself. The old signature took the engine and
    read `eng._state.clob` — but StrategyEngine has no `_state` attribute
    (the router owns it), so every call site was gated on
    `hasattr(eng, "_state")` and the helper NEVER RAN. Production
    2026-10-30 15:21: after the TP sold the whole position the engine still
    reported a 20.83-share phantom and kept retrying doomed sells.

    H-NEW-2026-07-11 (audit v6-12 production revisit): the bot used to
    re-fire trigger_tp into a hot retry loop when the user's wallet had
    been drained by a manual UI-side SELL, because (a) the UserWS still
    delivered `size=0` fill events and (b) `_check_take_profit` had no
    backoff. This helper is the authoritative reconciliation path: it
    asks the CLOB "how many of this token do I actually hold?" and, if
    the answer is zero, resets the position state so the next STATUS
    line is honest and trigger_tp stops firing.

    Best-effort: a transient REST failure does NOT clear state — we
    only act on a definitive balance==0.

    A BUY fill settles into the wallet with a lag (1-3s+, longer when the
    chain is busy), so a zero balance read right after a buy is not
    evidence of a drained position. Skip the reconcile while the newest
    tier fill is younger than `_POSITION_SETTLE_GRACE_S`; the next TP/stop
    retry will re-check after the backoff.
    """
    last_fill_ts = max(
        (float(getattr(t, "last_fill_ts", 0.0) or 0.0) for t in pos.tiers),
        default=0.0,
    )
    fill_age = time.time() - last_fill_ts if last_fill_ts > 0 else None
    if fill_age is not None and fill_age < _POSITION_SETTLE_GRACE_S:
        log.warning(
            "router.realize_check.settle_grace",
            token=token_id[:16],
            in_memory_qty=pos.total_qty,
            fill_age_s=round(fill_age, 2),
            grace_s=_POSITION_SETTLE_GRACE_S,
            note="newest BUY fill is still settling; keeping position state",
        )
        return

    try:
        bal = await clob.get_token_balance(token_id)
    except Exception as exc:
        log.debug(
            "router.realize_check.failed",
            token=token_id[:16], error=str(exc),
            note="REST balance check errored; keeping in-memory position as-is",
        )
        return

    if bal is None:
        # The balance API failed. It is NOT evidence that the inventory is
        # gone — never wipe the position on an error.
        log.warning(
            "router.realize_check.unavailable",
            token=token_id[:16],
            note="balance query unavailable; keeping in-memory position as-is",
        )
        return

    # balance is in shares; epsilon-comparison with the in-memory qty.
    if bal <= 0.5 and pos.total_qty > 0.5:
        # Confirm with a second read before destroying state. A single
        # zero can be a transient CLOB cache/settlement artefact right
        # after a fill; two consecutive zeros 0.5 s apart are credible
        # evidence that the wallet really is empty.
        await asyncio.sleep(0.5)
        try:
            bal2 = await clob.get_token_balance(token_id)
        except Exception:
            bal2 = None
        if bal2 is None or bal2 > 0.5:
            log.warning(
                "router.realize_check.transient_zero",
                token=token_id[:16],
                first_read=bal,
                second_read=bal2,
                in_memory_qty=pos.total_qty,
                note="zero balance was not confirmed on re-read; keeping "
                     "position state",
            )
            return
        log.warning(
            "router.realize_check.drained",
            token=token_id[:16],
            clob_balance_shares=bal2,
            in_memory_qty=pos.total_qty,
            note="CLOB balance is 0 (confirmed twice) but engine still "
                 "thinks we hold tokens; calling realize_position_closed()",
        )
        pos.realize_position_closed(
            source="router.realize_check",
            note=(
                "REST get_token_balance returned 0 on two consecutive reads "
                "during TP-rejection reconciliation; user-side UI sell (or "
                "upstream bot crash consuming the inventory) is the most "
                "likely cause"
            ),
        )
    elif bal + 0.5 < pos.total_qty:
        # Partial drain — log it but DON'T auto-clear, since we may still
        # be able to sell the residual via TP. The bot will retry at the
        # next backoff elapse.
        log.warning(
            "router.realize_check.partial_drain",
            token=token_id[:16],
            clob_balance_shares=bal,
            in_memory_qty=pos.total_qty,
            note="CLOB balance < in-memory qty but > 0; leaving position "
                 "intact, backoff will retry sell",
        )


class ActionRouter:
    """Converts StrategyEngine Actions into OrderManager calls and wires fill events.

    H-NEW-2026-07-11 refactor:
      - BUY actions post the GTD at the engine's strategy price (no orderbook
        snap). OrderManager.place_buy_by_strategy_price handles this.
      - TP actions post a single GTD SELL at the engine's tp_target. No more
        FAK fallback — the TierWatcher cancels the residual if mid never
        reaches tp_target.
      - For every GTD placed, a TierWatcher is spawned. The watcher polls
        the CLOB token balance and decides when to cancel the GTD (filled,
        expired, or crossed-into-next-tier).
    """

    def __init__(
        self,
        state: AppState,
        asset: str = "BTC",
        engine: "StrategyEngine | None" = None,
    ) -> None:
        self._state = state
        self.asset = str(asset or "BTC").upper()
        # Per-asset router: this engine is the only one whose position state
        # these callbacks may touch. The shared AppState.engines map is used
        # only for cross-asset fill attribution.
        self._engine = engine
        # Strong refs to the spawned TierWatchers per (round, tier). Lets us
        # cancel them on round-end without losing the references.
        self._watchers: dict[tuple[int, int], TierWatcher] = {}
        self._tp_watchers: dict[int, TierWatcher] = {}
        # H-NEW-2026-07-11-3 (audit, prod 14:30 round): reverse-lookup
        # so the user_ws on_order UPDATE callback can route size_matched
        # into the matching TierWatcher without the callback having to
        # know the (round, tier) tuple. Built on every spawn, dropped on
        # every terminal fire.
        self._watcher_by_server_id: dict[str, TierWatcher] = {}
        # Bare-construction compatibility for tests: only claim the single
        # state.router slot when no RouterGroup has been installed yet.
        if getattr(state, "router", None) is None:
            state.router = self

    def _eng(self):
        """The engine this router serves (falls back to state.engine for
        legacy single-asset tests that construct the router first)."""
        return self._engine or getattr(self._state, "engine", None)

    # ------------------------------------------------------------------
    # TierWatcher lifecycle helpers
    # ------------------------------------------------------------------

    def watcher_for_server_order(self, server_order_id: str) -> Optional["TierWatcher"]:
        """Return the active watcher tracking this server_order_id, if any.

        H-NEW-2026-07-11-3: used by the user_ws on_order UPDATE callback
        to push `size_matched` into the matching TierWatcher so it can
        short-circuit the REST poll loop on the next tick. Returns None
        for orders that are not ours or for watchers that have already
        reached a terminal state.
        """
        if not server_order_id:
            return None
        w = self._watcher_by_server_id.get(server_order_id)
        if w is None:
            return None
        if w.result is not None:
            # Already terminal — drop the index entry.
            self._watcher_by_server_id.pop(server_order_id, None)
            return None
        return w

    def _spawn_tier_watcher(
        self,
        round_start_ts: int,
        tier: int,
        server_order_id: str,
        token_id: str,
        strategy_price: float,
        size_expected: float,
        expires_at: float,
    ) -> TierWatcher:
        """Spawn a TierWatcher for a BUY tier's GTD and remember it.

        H-NEW-2026-07-15 tick-driven refactor: this watcher is now passive.
        The previous `cross_threshold` parameter (which used to drive a
        mid-cross → cancel-and-emit-CROSSED path) has been removed: the
        strategy tick (`_reconcile_inflight_buys`) is the sole place that
        decides when to cancel a BUY GTD. The watcher just observes.

        H-NEW-2026-07-11 (B-NEW-4): guard against accidental double-spawn
        by short-circuiting if a watcher for the same (round, tier) is
        already running. Otherwise (e.g. an `_evaluate` tick fires while
        the previous spawn's place_buy await is still in flight) we would
        post a second GTD on the book and double-spend the tier's budget.
        """
        existing = self._watchers.get((round_start_ts, tier))
        if existing is not None and not existing.result:
            if existing._server_order_id == server_order_id:
                # Same order, double spawn — harmless no-op.
                log.warning(
                    "router.duplicate_tier_watcher_rejected",
                    round=round_start_ts,
                    tier=tier,
                    server_id=server_order_id[:16],
                    active_server_id=(existing._server_order_id or "")[:16],
                )
                return existing
            # A NEW order for the same tier (the previous GTD was cancelled
            # by the strategy tick and the tier re-armed on a price rebound).
            # Supersede the stale watcher: refusing the spawn left the new
            # order unmonitored, and the old watcher would later apply its
            # result to the rebound tier.
            old_sid = existing._server_order_id or ""
            log.warning(
                "router.stale_tier_watcher_superseded",
                round=round_start_ts,
                tier=tier,
                old_server_id=old_sid[:16],
                new_server_id=server_order_id[:16],
            )
            if old_sid:
                self._watcher_by_server_id.pop(old_sid, None)
            stop_task = asyncio.create_task(existing.stop())
            self._state._bg_tasks.add(stop_task)
            stop_task.add_done_callback(self._state._bg_tasks.discard)

        cfg = strategy()
        eng = self._eng()

        async def on_done(result: WatcherResult) -> None:
            eng = self._eng()
            if not eng or not eng._round:
                return
            tier_snap = eng._round.position.tiers[tier - 1]
            # Stale-watcher guard: if this tier has since been re-armed with
            # a NEW server order, a terminal result from the old order must
            # not mutate the current tier (it would mark the new order
            # filled/expired based on the old one's outcome).
            if (
                tier_snap.server_order_id
                and result.server_order_id
                and tier_snap.server_order_id != result.server_order_id
            ):
                log.warning(
                    "router.stale_watcher_result_ignored",
                    tier=tier,
                    stale_server_id=result.server_order_id[:16],
                    current_server_id=tier_snap.server_order_id[:16],
                    outcome=result.outcome.value,
                )
                return

            # H-NEW-2026-07-12 (audit R10, double-count fix): when the watcher
            # fast-fills on spawn (or reaches FILLED via balance-poll before
            # the WS TRADE event arrives), it must NOT bypass OrderManager's
            # mirror bookkeeping. Otherwise the WS TRADE event that lands
            # 50-500 ms later finds `tracked.size_matched=0` and pushes a
            # second `record_fill(tier, 0.33, 5.26)` after the watcher
            # already set `tier.size_filled=5.26` via
            # `apply_tier_watcher_result`'s missed-fill branch — the
            # symptom is exactly the prod-1783855800 case:
            #   cumulative_size: 0 → 5.26 (watcher_reconciled_missed_fills
            #                          fill_px=0.38)
            #                   → 10.52 (fill_recorded fill_price=0.33)
            # i.e. a 5.26-share BUY ended up at total_qty=10.52.
            #
            # The fix: advance OrderManager's tracked.size_matched BEFORE
            # letting apply_tier_watcher_result commit. OrderManager's
            # monotonic-max guard (line 1315 of order_manager.py) means
            # the WS TRADE event that follows sees prev=5.26 → delta=0
            # and skips its own on_order_filled call. apply_tier_watcher_
            # result's missed-fill branch (round_state.py line 691) ALSO
            # short-circuits because `tier.size_filled == filled_qty`.
            #
            # We deliberately do NOT route through `clob._fire_fill_callback`
            # here — that path's FillDeduper is per-site (each fill_callback
            # install owns its own bookmark), so it doesn't see the WS TRADE
            # site's observations, and using it would just push another
            # record_fill on top of apply_tier_watcher_result's accumulation.
            om = self._state.order_manager
            if (
                result.outcome.value in ("filled", "over_filled")
                and om is not None
                and result.server_order_id
            ):
                # Defence-in-depth dedup: WS TRADE may have already
                # arrived (the watcher's spawn-time fast-fill races with
                # the WS feed on the same tick). Mirror `_make_fill_callback`:
                # only act on the delta above `tracked.size_matched`.
                # Without this guard a late watcher event after a WS-first
                # path would re-push the full `filled_qty` and the
                # `record_fill` accumulator would double-count
                # (size_filled 5.26 → 10.52).
                tracked = om.by_server_id(result.server_order_id)
                prev_size_matched = (
                    tracked.size_matched if tracked else 0.0
                )
                effective_delta = (
                    float(result.filled_qty) - prev_size_matched
                )
                if effective_delta <= 1e-9:
                    # WS path already credited this fill (or even more
                    # if the WS feed saw a later partial than the watcher's
                    # final observation). Mirror `_make_fill_callback`'s
                    # dedup contract: skip the engine call entirely to
                    # avoid double-counting. The fill ROW may still be
                    # missing its taker role / real execution price if
                    # the WS UPDATE booked it first — backfill those.
                    reconcile_px = (
                        float(result.avg_fill_price)
                        if result.avg_fill_price and result.avg_fill_price > 0
                        else None
                    )
                    reconcile_role = _watcher_role(result)
                    if reconcile_px is None or reconcile_role is None:
                        # Same race as the branch below: the watcher's
                        # spawn-time fill-state read can precede the
                        # trade ledger, so ask once more before the
                        # row is left fee-free forever.
                        clob = getattr(self._state, "clob", None)
                        if clob is not None:
                            try:
                                fill_state = await clob.get_order_fill_state(
                                    result.server_order_id
                                )
                                if fill_state:
                                    px = float(
                                        fill_state.get("avg_price", 0) or 0
                                    )
                                    if px > 0:
                                        reconcile_px = px
                                    fetched_role = fill_state.get(
                                        "trader_side"
                                    )
                                    if fetched_role in ("TAKER", "MAKER"):
                                        reconcile_role = fetched_role
                                    elif fill_state.get("is_taker"):
                                        reconcile_role = "TAKER"
                            except Exception as exc:  # noqa: BLE001
                                log.debug(
                                    "router.on_done.terms_refetch_failed",
                                    server_id=result.server_order_id[:16],
                                    error=str(exc),
                                )
                    om.reconcile_fill_terms(
                        result.server_order_id,
                        cumulative=float(result.filled_qty),
                        fill_price=reconcile_px,
                        trader_side=reconcile_role,
                    )
                    # The watcher reached FILLED/OVER_FILLED, but the
                    # cumulative was already mirrored by the WS UPDATE,
                    # so the usual post-apply promotion never ran. Without
                    # this the tracked order stays "live" forever and the
                    # 5-second GTD invariant reports a phantom order.
                    # mark_filled is a no-op unless the cumulative really
                    # reaches the order's expected size.
                    om.mark_filled(result.server_order_id)
                else:
                    forward_price = result.avg_fill_price
                    if not forward_price or forward_price <= 0:
                        # Re-fetch the CLOB fill state — the original
                        # `_read_fill_state` call inside the watcher's
                        # fast-fill path frequently returns avg_price=0.0
                        # because the fill_state endpoint's response
                        # doesn't always carry the field; ~50 ms later
                        # (when the matched ledger has caught up) the
                        # same endpoint often does. This second REST
                        # call is the difference between avg_cost=0.38
                        # (strategy-price fallback) and avg_cost=0.33
                        # (real maker-fill avg).
                        clob = getattr(self._state, "clob", None)
                        if clob is not None:
                            try:
                                fill_state = await clob.get_order_fill_state(
                                    result.server_order_id
                                )
                                if fill_state:
                                    fetched = float(
                                        fill_state.get("avg_price", 0.0) or 0.0
                                    )
                                    if fetched > 0:
                                        forward_price = fetched
                                    # The spawn-time read may have run
                                    # before the trade ledger existed —
                                    # learn the role from this one so
                                    # the taker fee is booked on insert.
                                    fetched_role = fill_state.get(
                                        "trader_side"
                                    )
                                    if fetched_role in ("TAKER", "MAKER"):
                                        result.trader_side = fetched_role
                                    if fill_state.get("is_taker"):
                                        result.is_taker = True
                            except Exception as exc:  # noqa: BLE001
                                log.debug(
                                    "router.on_done.avg_price_refetch_failed",
                                    server_id=result.server_order_id[:16],
                                    error=str(exc),
                                )
                    if not forward_price or forward_price <= 0:
                        forward_price = result.strategy_price

                    # Step 1: monotonic-mirror OrderManager to the
                    # watcher's authoritative cumulative (5.26). This
                    # also closes the WS-vs-watcher race by aligning
                    # tracked.size_matched with what the watcher just
                    # observed; any subsequent WS TRADE for the same
                    # order will see this value and dedup out via
                    # `_make_fill_callback`'s monotonic guard.
                    om.apply_fill_event(
                        result.server_order_id,
                        size_matched=float(result.filled_qty),
                        fill_price=float(forward_price),
                        # Taker fills pay the Polymarket fee; makers do not.
                        trader_side=_watcher_role(result),
                    )
                    # Promote status too: the fast-fill path can be the ONLY
                    # observation of a fully matched order (the WS TRADE and
                    # balance-poll paths may both miss it). Leaving it as
                    # "live" made the 5-second GTD invariant report a
                    # phantom live order for the rest of the round.
                    om.mark_filled(result.server_order_id)
                    # Step 2: feed engine position state with the DELTA
                    # above whatever the WS path already credited
                    # (effective_delta, NOT the watcher's full
                    # filled_qty). This is what
                    # `apply_tier_watcher_result`'s missed-fill branch
                    # would have done for us, but emitting the delta
                    # through `on_order_filled` instead keeps the
                    # engine-side accounting (`record_fill`'s
                    # `size_filled += size`, `cost_filled += price*size`)
                    # authoritative on a single accumulator. After this
                    # step, tier.size_filled is exactly
                    # watcher.filled_qty.
                    eng.on_order_filled(
                        token_id,
                        result.server_order_id,
                        fill_price=float(forward_price),
                        fill_size=float(effective_delta),
                    )

            # H-NEW-2026-07-11: forward dedup_key + real avg_fill_price so
            # apply_tier_watcher_result can dedup terminal states across
            # multiple fan-in paths and price its missed-fill fix-up
            # against the CLOB-reported average (not the strategy price).
            eng._round.position.apply_tier_watcher_result(
                tier_snap,
                result.outcome.value,
                result.filled_qty,
                dedup_key=result.dedup_key,
                avg_fill_price=result.avg_fill_price,
            )

        def is_active() -> bool:
            eng = self._eng()
            return bool(eng and eng._round and eng._round.round_start_ts == round_start_ts)

        # H-NEW-2026-07-12: continuous ground-truth reconciliation.
        #
        # Wire a partial-fill callback so that even if the WS TRADE channel
        # is dropping events (or `size=0` quirks swallow a row), the
        # TierWatcher's per-poll CLOB read still keeps the position view in
        # step with reality. The callback routes through the SAME
        # `om.apply_fill_event` + `eng.on_order_filled` path that
        # `_make_fill_callback` uses for the polling side of the WS/Polling
        # double-fire, so any dedup state (FillDeduper, OrderManager's
        # monotonic tracked order) applies uniformly.
        #
        # Why this is the right patch:
        #   * WS TRADE remains the primary low-latency signal.
        #   * Polling used to fire only ONE reconciliation at terminal state
        #     (apply_tier_watcher_result). In a fast cascade that 99.5%
        #     wait can be ~1.5 s during which the position view is stale.
        #     That's exactly the window where a TP or next-tier decision
        #     would be made on wrong data.
        #   * Now: every poll that observes a fresh size_matched increment
        #     pushes the AUTHORITATIVE CLOB cumulative through
        #     `on_order_filled`. The dedup contract is monotonic on
        #     tracked.size_matched, so if WS got there first the polling
        #     delta is silently skipped.
        #
        # We do NOT wire this for SELL (TP) watchers — SELLs are FAK, the
        # partial-fill window is sub-second, and `record_tp_fill` already
        # handles fill-or-no-fill semantics without continuous reconcile.
        async def on_partial_fill(
            cumulative_size_matched: float,
            fill_price: float,
            hint_result: "WatcherResult | None",
        ) -> None:
            eng = self._eng()
            om = self._state.order_manager
            if not eng or not om or cumulative_size_matched <= 0:
                return
            if cumulative_size_matched <= 1e-9:
                return
            # Ignore a stale watcher's polling if the tier was re-armed with
            # a different order in the meantime.
            tier_snap = eng._round.position.tiers[tier - 1]
            if (
                tier_snap.server_order_id
                and tier_snap.server_order_id != server_order_id
            ):
                return
            tracked = om.by_server_id(server_order_id)
            prev_size_matched = tracked.size_matched if tracked else 0.0
            # The TierWatcher passed the CLOB's authoritative cumulative.
            # Derive the per-event delta from OrderManager's monotonic
            # tracked state. This matches the WS-pathing's contract: when
            # WS UPDATE arrives first, the polling delta collapses to 0
            # and the engine call is skipped.
            effective_delta = cumulative_size_matched - prev_size_matched
            # The watcher passes 0.0 when this poll had no average price
            # (the trade ledger has not caught up). That is NOT an
            # execution price — never let it overwrite the fill row.
            price_known = fill_price is not None and fill_price > 0
            if effective_delta <= 1e-9:
                # Already booked (usually by the WS UPDATE, which has no
                # role and only the limit price). This poll carries the
                # CLOB average execution price and the maker/taker role
                # derived from the order's trades — use them to complete
                # the fill row's fee accounting.
                om.reconcile_fill_terms(
                    server_order_id,
                    cumulative=cumulative_size_matched,
                    fill_price=fill_price if price_known else None,
                    trader_side=_watcher_role(hint_result),
                )
                return
            om.apply_fill_event(
                server_order_id,
                size_matched=cumulative_size_matched,
                fill_price=fill_price if price_known else None,
                trader_side=_watcher_role(hint_result),
            )
            om.mark_filled(server_order_id)
            engine_px = (
                float(fill_price)
                if price_known
                else (
                    tracked.price
                    if tracked
                    else getattr(hint_result, "strategy_price", 0.0)
                )
            )
            eng.on_order_filled(
                token_id, server_order_id, engine_px, effective_delta
            )
            log.info(
                "router.polling_resync",
                token=token_id[:16],
                order_id=server_order_id[:16],
                cumulative=cumulative_size_matched,
                delta_size=effective_delta,
                fill_price=engine_px,
                price_observed=price_known,
                note="polling catch-up (WS TRADE channel missed/late)",
            )

        watcher = TierWatcher(
            clob=self._state.clob,
            server_order_id=server_order_id,
            token_id=token_id,
            side="BUY",
            strategy_price=strategy_price,
            expected_size=size_expected,
            expires_at=expires_at,
            poll_s=cfg.TIER_WATCHER_POLL_S,
            on_complete=on_done,
            on_partial_fill=on_partial_fill,
            is_still_active=is_active,
        )
        task = watcher.start()
        # Keep a strong ref so the task cannot be GC'd before its first await.
        self._state._bg_tasks.add(task)
        task.add_done_callback(self._state._bg_tasks.discard)
        self._watchers[(round_start_ts, tier)] = watcher
        # H-NEW-2026-07-11-3: register in reverse-lookup so on_order UPDATE
        # can route size_matched into this watcher.
        self._watcher_by_server_id[server_order_id] = watcher
        # Mark the tier as "being watched" so the engine's mutex holds.
        if eng and eng._round:
            tier_snap = eng._round.position.tiers[tier - 1]
            tier_snap.watching = True
        return watcher

    def _mirror_tp_terminal_fill(self, result: "WatcherResult") -> None:
        """Mirror a terminated TP SELL's cumulative fill into OrderManager.

        The TP watcher's on_done only updated the Position — the tracked
        order stayed at status='live' / size_matched=0, so the dashboard
        kept showing the TP as an open order and the fills table never got
        the SELL row (production 2026-10-30 15:21: the TP sold the whole
        35.19-share position but the UI showed it hanging).

        `apply_fill_event` is monotonic and idempotent against the WS /
        polling-resync paths; `mark_filled` promotes the status once the
        cumulative reaches the expected size.
        """
        om = getattr(self._state, "order_manager", None)
        sid = getattr(result, "server_order_id", "") or ""
        filled = float(getattr(result, "filled_qty", 0.0) or 0.0)
        if om is None or not sid or filled <= 0:
            return
        fill_px = getattr(result, "avg_fill_price", 0.0) or 0.0
        om.apply_fill_event(
            sid,
            size_matched=filled,
            # Only a real observed average may overwrite a fill row's
            # price; the strategy limit is just the insert-time fallback
            # (apply_fill_event uses the tracked order's limit itself).
            fill_price=float(fill_px) if fill_px > 0 else None,
            trader_side=_watcher_role(result),
        )
        om.mark_filled(sid)

    def _spawn_tp_watcher(
        self,
        round_start_ts: int,
        server_order_id: str,
        token_id: str,
        tp_price: float,
        size: float,
        expires_at: float,
    ) -> TierWatcher:
        """Spawn a TierWatcher for a TP SELL GTD."""
        cfg = strategy()

        async def on_done(result: WatcherResult) -> None:
            eng = self._eng()
            if not eng or not eng._round:
                return
            pos = eng._round.position
            sid = result.server_order_id
            # Fee/price accuracy: the spawn-time fast-fill read can race
            # the trade ledger, leaving avg_fill_price=0 and no taker
            # flag. One extra REST read here is the last chance to learn
            # them before the fill row is persisted; without it the SELL
            # exits stay fee-free in the dashboard.
            if not result.is_taker or (result.avg_fill_price or 0.0) <= 0:
                clob = getattr(self._state, "clob", None)
                if clob is not None:
                    try:
                        fill_state = await clob.get_order_fill_state(sid)
                        if fill_state:
                            px = float(fill_state.get("avg_price", 0) or 0)
                            if px > 0:
                                result.avg_fill_price = px
                            fetched_role = fill_state.get("trader_side")
                            if fetched_role in ("TAKER", "MAKER"):
                                result.trader_side = fetched_role
                            if fill_state.get("is_taker"):
                                result.is_taker = True
                    except Exception as exc:  # noqa: BLE001
                        log.debug(
                            "router.tp_done.terms_refetch_failed",
                            server_id=sid[:16],
                            error=str(exc),
                        )
            # Mirror the terminal cumulative into OrderManager FIRST: this
            # promotes the order to `filled` and persists the SELL fill row
            # (the engine-only update below left the dashboard showing a
            # live TP order after it had already sold).
            self._mirror_tp_terminal_fill(result)
            # `filled_qty` is the watcher's AUTHORITATIVE per-order cumulative
            # matched size. Apply only the part not already booked by the WS
            # TRADE path / an earlier partial-resync callback.
            try:
                already = pos.tp_size_sold(sid)
                if already is None:
                    # Snapshot already closed — the full amount was booked.
                    delta = 0.0
                else:
                    delta = max(0.0, float(result.filled_qty) - already)
                if delta > 1e-9:
                    fill_px = (
                        result.avg_fill_price
                        if result.avg_fill_price and result.avg_fill_price > 0
                        else result.strategy_price
                    )
                    # record_tp_fill reduces _total_qty / _total_cost and
                    # released `_tp_inflight` on every fill.
                    pos.record_tp_fill(sid, fill_px, delta)
                else:
                    pos._tp_inflight = False
            finally:
                # Terminal watcher state: release the TP slot so the next
                # tick can re-arm against the residual (if any). Without
                # this the snapshot leaks and `has_open_tp_orders` blocks
                # every future TP for the rest of the round.
                pos.close_tp_order(sid)

        async def on_partial_fill(
            cumulative_size_matched: float,
            fill_price: float,
            hint_result: "WatcherResult | None",
        ) -> None:
            """Ground-truth resync for partial TP sells.

            Without this, a partial SELL that the WS TRADE channel missed
            stayed invisible until the watcher's terminal state (up to the
            full GTD lifetime). The delta contract mirrors the BUY path:
            derive the increment from OrderManager's monotonic
            `size_matched`, then let the engine book it.
            """
            eng = self._eng()
            om = self._state.order_manager
            if not eng or not om or cumulative_size_matched <= 0:
                return
            tracked = om.by_server_id(server_order_id)
            prev_matched = tracked.size_matched if tracked else 0.0
            effective_delta = cumulative_size_matched - prev_matched
            # 0.0 = the watcher did not observe an average this poll.
            price_known = fill_price is not None and fill_price > 0
            if effective_delta <= 1e-9:
                # Same as the BUY side: complete the terms of a fill row
                # first booked by the WS UPDATE / WS TRADE without a role.
                om.reconcile_fill_terms(
                    server_order_id,
                    cumulative=cumulative_size_matched,
                    fill_price=fill_price if price_known else None,
                    trader_side=_watcher_role(hint_result),
                )
                return
            om.apply_fill_event(
                server_order_id,
                size_matched=cumulative_size_matched,
                fill_price=fill_price if price_known else None,
                trader_side=_watcher_role(hint_result),
            )
            om.mark_filled(server_order_id)
            engine_px = (
                float(fill_price)
                if price_known
                else (
                    tracked.price
                    if tracked
                    else getattr(hint_result, "strategy_price", 0.0)
                )
            )
            # No BUY tier matches a SELL id, so on_order_filled falls
            # through to Position.record_tp_fill.
            eng.on_order_filled(
                token_id, server_order_id, engine_px, effective_delta
            )
            log.info(
                "router.tp_polling_resync",
                token=token_id[:16],
                order_id=server_order_id[:16],
                cumulative=cumulative_size_matched,
                delta_size=effective_delta,
                fill_price=engine_px,
                price_observed=price_known,
            )

        def is_active() -> bool:
            eng = self._eng()
            return bool(eng and eng._round and eng._round.round_start_ts == round_start_ts)

        watcher = TierWatcher(
            clob=self._state.clob,
            server_order_id=server_order_id,
            token_id=token_id,
            side="SELL",
            strategy_price=tp_price,
            expected_size=size,
            expires_at=expires_at,
            poll_s=cfg.TIER_WATCHER_POLL_S,
            on_complete=on_done,
            on_partial_fill=on_partial_fill,
            is_still_active=is_active,
        )
        task = watcher.start()
        self._state._bg_tasks.add(task)
        task.add_done_callback(self._state._bg_tasks.discard)
        self._tp_watchers[round_start_ts] = watcher
        # H-NEW-2026-07-11-3: same reverse-lookup registration as BUY.
        self._watcher_by_server_id[server_order_id] = watcher
        return watcher

    async def _final_reconcile_before_close(self, server_order_id: str) -> None:
        """Account for fills up to the moment an order is cancelled.

        A cancel can race a fill. Before the router frees the tier/TP slot,
        read the order's authoritative `size_matched` and push any delta
        that WS + watcher polling have not already booked. This is what
        guarantees the in-memory position matches reality before a new
        order is allowed for the same tier.
        """
        om = self._state.order_manager
        eng = self._eng()
        clob = self._state.clob
        if not om or not eng or not eng._round or not clob or not server_order_id:
            return
        tracked = om.by_server_id(server_order_id)
        if tracked is None:
            return
        fill_px = tracked.price
        # True only when `fill_px` came from the trade-derived fill state.
        # The tracked LIMIT is fine as an insert-time fallback but must not
        # overwrite an already-booked row's real execution price.
        fill_px_authoritative = False
        rest_cumulative = 0.0
        try:
            fill_state = await clob.get_order_fill_state(server_order_id)
        except Exception:
            fill_state = None
        if fill_state:
            try:
                rest_cumulative = float(fill_state.get("size_matched", 0) or 0)
            except (TypeError, ValueError):
                rest_cumulative = 0.0
            try:
                avg_px = float(fill_state.get("avg_price", 0) or 0)
                if avg_px > 0:
                    fill_px = avg_px
                    fill_px_authoritative = True
            except (TypeError, ValueError):
                pass
        # Re-read `prev` AFTER the await. The watcher's terminal on_done (or
        # a WS fill) may have booked the same cumulative while this REST call
        # was in flight; the old code captured `prev` before the await and
        # then credited the engine with the full delta a second time.
        # Production 2026-10-30 15:17: T3 filled 20.83 exactly as the strategy
        # cancelled the tier — the watcher booked the fill, then this path
        # added it again and the position read 56.02 while the wallet held
        # 35.19. OrderManager's monotonic max kept ITS value correct, which is
        # why only the engine drifted.
        prev = tracked.size_matched or 0.0
        cumulative = max(prev, rest_cumulative)
        delta = cumulative - prev
        role = (fill_state or {}).get("trader_side")
        if role not in ("TAKER", "MAKER"):
            role = "TAKER" if (fill_state or {}).get("is_taker") else None
        if delta <= 1e-9:
            # Nothing new to book, but this is the last authoritative look
            # at the order before the slot is released — fill in any
            # role/price terms a racing WS UPDATE left incomplete.
            om.reconcile_fill_terms(
                server_order_id,
                cumulative=cumulative,
                fill_price=fill_px if fill_px_authoritative else None,
                trader_side=role,
            )
            return
        om.apply_fill_event(
            server_order_id,
            size_matched=cumulative,
            fill_price=fill_px if fill_px_authoritative else None,
            trader_side=role,
        )
        # Promote the tracked order to `filled` once the cumulative reaches
        # its expected size. Without this, an order whose last fill is
        # discovered here (e.g. a cancel racing a fill, or the watcher's
        # spawn-time fast-fill) keeps status='live' forever, which makes the
        # GTD invariant report a phantom live order every 5 s (production
        # 2026-10-30 15:00 round: T1 fully filled but status stayed live).
        om.mark_filled(server_order_id)
        if tracked.side == "SELL":
            pos = eng._round.position
            if (
                hasattr(pos, "tp_size_sold")
                and pos.tp_size_sold(server_order_id) is not None
            ):
                pos.record_tp_fill(server_order_id, fill_px, delta)
        else:
            eng.on_order_filled(
                tracked.token_id, server_order_id, fill_px, delta
            )
        log.info(
            "router.final_reconcile",
            side=tracked.side,
            order_id=server_order_id[:16],
            prev_matched=prev,
            cumulative=cumulative,
            delta_size=delta,
            fill_price=fill_px,
        )

    async def stop_all_watchers(self) -> None:
        """Cancel every active watcher (called on shutdown / round-end)."""
        for w in list(self._watchers.values()):
            await w.stop()
        self._watchers.clear()
        for w in list(self._tp_watchers.values()):
            await w.stop()
        self._tp_watchers.clear()
        # H-NEW-2026-07-11-3: clear reverse-lookup too.
        self._watcher_by_server_id.clear()

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    async def __call__(self, action: Action) -> None:
        eng = self._eng()
        om = self._state.order_manager
        if not eng or not om:
            return

        ks = getattr(self._state, "killswitch", None)
        if ks is not None and getattr(ks, "active", False):
            # Emergency stop engaged after the engine emitted this action.
            # OrderManager refuses the placement too; drop it here so no
            # watcher is spawned for an order that will never exist.
            log.warning(
                "router.blocked_by_killswitch",
                action=type(action).__name__,
                reason=getattr(ks, "reason", ""),
            )
            # Release the stop-loss in-flight latch: the operator's stop
            # suppresses auto-sells, but a later "恢复运行" must be able
            # to re-arm the stop instead of deadlocking on this action.
            if isinstance(action, ActionStopLoss) and eng._round is not None:
                eng._stop_loss_inflight = False
                eng._stop_loss_retry_after_ts = 0.0
            return

        tick_size = eng._round.tick_size if eng._round else 0.01
        round_ts = self._state.round_start_ts

        if isinstance(action, ActionOpenT1):
            # T1 is a GTD BUY at the strategy price, with the same
            # lifecycle as T2-T4. The engine requires notional / price
            # >= 5 shares; a marketable limit crosses immediately as a
            # taker, otherwise the order rests on the book until filled
            # or the GTD expires.
            order = await om.place_buy_by_strategy_price(
                token_id=action.token,
                strategy_price=action.price,
                notional=action.notional,
                round_start_ts=round_ts,
                tier_name="T1",
                tick_size=tick_size,
                asset=self.asset,
            )
            if order:
                expires = order.expires_at
                eng.bind_server_order_id(
                    tier=1,
                    server_order_id=order.server_order_id or "",
                    token_id=action.token,
                    expires_at=expires,
                    expected_price=order.price,
                    placed_size=order.placed_size if order.placed_size > 0 else None,
                )
                self._state.clob.fill_callback = self._make_fill_callback()
                size_expected = (
                    order.placed_size
                    if order.placed_size > 0
                    else (action.notional / action.price)
                )
                self._spawn_tier_watcher(
                    round_start_ts=round_ts,
                    tier=1,
                    server_order_id=order.server_order_id or "",
                    token_id=action.token,
                    strategy_price=order.price,
                    size_expected=size_expected,
                    expires_at=expires,
                )
                log.info(
                    "router.t1_placed",
                    side=action.side,
                    token=action.token[:16],
                    strategy_price=action.price,
                    size=order.size,
                    server_id=order.server_order_id or "none",
                    watcher=True,
                )
            else:
                reject_reason = (
                    getattr(om, "last_buy_reject_reason", "")
                    or "om.place_buy_by_strategy_price returned None"
                )
                eng._round.position.entry_side = None
                eng._round.position.entry_token = None
                log.error(
                    "router.t1_failed",
                    side=action.side,
                    token=action.token[:16],
                    strategy_price=action.price,
                    notional=action.notional,
                    reason=reject_reason,
                )
                # Backoff before the next T1 attempt (2 s) so a rejected
                # POST is not retried on every 250 ms tick. An empty
                # wallet is not fixed by retrying — back off much longer.
                eng._round.position.t1_retry_after_ts = time.time() + (
                    15.0 if reject_reason == "insufficient_balance"
                    else 2.0
                )

        elif isinstance(action, ActionOpenTN):
            order = await om.place_buy_by_strategy_price(
                token_id=action.token,
                strategy_price=action.price,
                notional=action.notional,
                round_start_ts=round_ts,
                tier_name=f"T{action.tier}",
                tick_size=tick_size,
                asset=self.asset,
            )
            if order:
                expires = order.expires_at
                eng.bind_server_order_id(
                    tier=action.tier,
                    server_order_id=order.server_order_id or "",
                    token_id=action.token,
                    expires_at=expires,
                    expected_price=order.price,
                    placed_size=order.placed_size if order.placed_size > 0 else None,
                )
                self._state.clob.fill_callback = self._make_fill_callback()
                size_expected = order.placed_size if order.placed_size > 0 else (action.notional / action.price)
                # H-NEW-2026-07-15: tick-driven refactor. The watcher is
                # passive and no longer receives a cross_threshold. The
                # strategy tick (`_reconcile_inflight_buys`) owns the
                # decision to cancel a BUY GTD once mid drops into the
                # next-tier zone. The watcher just observes lifecycle.
                self._spawn_tier_watcher(
                    round_start_ts=round_ts,
                    tier=action.tier,
                    server_order_id=order.server_order_id or "",
                    token_id=action.token,
                    strategy_price=order.price,
                    size_expected=size_expected,
                    expires_at=expires,
                )
                log.info(
                    "router.tN_placed",
                    tier=action.tier,
                    token=action.token[:16],
                    strategy_price=action.price,
                    size=order.size,
                    server_id=order.server_order_id or "none",
                    watcher=True,
                )
            else:
                reject_reason = (
                    getattr(om, "last_buy_reject_reason", "")
                    or "om.place_buy_by_strategy_price returned None"
                )
                log.error(
                    "router.tN_failed",
                    tier=action.tier,
                    token=action.token[:16],
                    strategy_price=action.price,
                    notional=action.notional,
                    reason=reject_reason,
                )
                tier_snap = getattr(eng._round.position, f"tier{action.tier}", None)
                if tier_snap is not None:
                    tier_snap.placed = False
                    tier_snap.watching = False
                    tier_snap.server_order_id = None
                    tier_snap.expires_at = 0.0
                    # Backoff before retrying this tier (2 s; 15 s when the
                    # wallet is empty — retrying cannot fix that).
                    tier_snap.retry_after_ts = time.time() + (
                        15.0 if reject_reason == "insufficient_balance" else 2.0
                    )

        elif isinstance(action, ActionTakeProfit):
            # H-NEW-2026-07-11: a single GTD SELL at tp_target, monitored
            # by a TierWatcher. No FAK fallback — when the GTD expires
            # un-filled, the watcher reports EXPIRED and we clear the
            # mutex so the next tick can re-arm if mid is still above
            # the pre-arm fire line.
            #
            # H-NEW-2026-07-11 (audit v6-12 production revisit): on
            # POST-failure we MUST tell the engine to backoff, otherwise
            # the next 0.25s `_evaluate` tick re-fires trigger_tp and we
            # re-POST the same unfulfillable order. Production saw 11
            # retries in <3s. We also REST-check the token balance: if
            # the on-chain inventory is actually 0 (e.g. user sold via
            # UI while we still believed in 5 tokens), we tell the
            # Position to forget the phantom inventory.
            async def _tp_runner() -> None:
                try:
                    if not eng._round:
                        return
                    pos = eng._round.position
                    if getattr(action, "taker", False):
                        # Compact (<5 share) position: a GTD SELL would be
                        # rejected. Exit with a FAK market sell; the fill
                        # is synchronous, so register the TP snapshot and
                        # apply it directly (no TierWatcher).
                        order = await om.place_market_sell(
                            token_id=action.token,
                            size=action.qty,
                            reference_price=action.price,
                            round_start_ts=round_ts,
                            tier_name="TP",
                            tick_size=tick_size,
                            asset=self.asset,
                        )
                        if not order:
                            pos._tp_inflight = False
                            log.warning(
                                "router.tp_taker_failed",
                                token=action.token[:16],
                                reference_price=action.price,
                                qty=action.qty,
                                reason="om.place_market_sell returned None",
                            )
                            if hasattr(eng, "notify_tp_post_error"):
                                try:
                                    eng.notify_tp_post_error(
                                        reason="place_market_sell returned None"
                                    )
                                except Exception:
                                    pass
                            clob = getattr(self._state, "clob", None)
                            if clob is not None:
                                try:
                                    await _maybe_realize_closed(
                                        action.token, clob, pos
                                    )
                                except Exception:
                                    pass
                            return
                        sid = order.server_order_id or ""
                        sold_qty = (
                            order.placed_size if order.placed_size > 0
                            else order.size
                        )
                        pos.register_tp(
                            server_order_id=sid,
                            token_id=action.token,
                            target_qty=sold_qty,
                        )
                        pos.record_tp_fill(sid, order.price, sold_qty)
                        if pos.total_qty <= 1e-9:
                            pos.realize_position_closed(
                                source="router.tp_taker",
                                note="taker TP closed the position",
                            )
                        eng.notify_tp_recovered()
                        log.info(
                            "router.tp_taker_filled",
                            token=action.token[:16],
                            mid=action.price,
                            qty=action.qty,
                            shares=sold_qty,
                            avg_price=order.price,
                            server_id=(sid or "none")[:16],
                        )
                        return
                    order = await om.place_gtd_sell_at_price(
                        token_id=action.token,
                        size=action.qty,
                        price=action.price,
                        round_start_ts=round_ts,
                        tier_name="TP",
                        tick_size=tick_size,
                        asset=self.asset,
                    )
                    if order and not pos._tp_inflight:
                        # The engine abandoned the TP while the POST was in
                        # flight. Cancel the fresh GTD; if the cancel fails,
                        # fall through and track it so a late fill is still
                        # attributed.
                        cancelled = await om.cancel(
                            order.client_order_id,
                            reason="tp_inflight_cleared",
                        )
                        if cancelled:
                            log.info(
                                "router.tp_runner.cancelled_after_post",
                                token=action.token[:16],
                                server_id=(order.server_order_id or "")[:16],
                            )
                            return
                        log.warning(
                            "router.tp_runner.cancel_failed_tracking",
                            token=action.token[:16],
                            server_id=(order.server_order_id or "")[:16],
                            note="engine abandoned TP but CLOB cancel failed; "
                                 "tracking the order so fills are not lost",
                        )
                    if not order:
                        pos._tp_inflight = False
                        log.warning(
                            "router.tp_failed",
                            token=action.token[:16],
                            strategy_price=action.price,
                            qty=action.qty,
                            reason="om.place_gtd_sell_at_price returned None",
                        )
                        # Engage engine backoff + reconcile inventory.
                        if hasattr(eng, "notify_tp_post_error"):
                            try:
                                eng.notify_tp_post_error(
                                    reason="place_gtd_sell_at_price returned None"
                                )
                            except Exception:
                                pass
                        clob = getattr(self._state, "clob", None)
                        if clob is not None:
                            try:
                                await _maybe_realize_closed(
                                    action.token, clob, pos
                                )
                            except Exception:
                                pass
                        return
                    eng.notify_tp_recovered()
                    # Sell qty actually posted: OrderManager caps the size to
                    # the wallet's token balance when the in-memory position
                    # drifted above it, so track THAT number (not
                    # action.qty) or the watcher would wait for shares that
                    # never existed.
                    sold_qty = (
                        order.placed_size if order.placed_size > 0 else order.size
                    )
                    pos.register_tp(
                        server_order_id=order.server_order_id or "",
                        token_id=action.token,
                        target_qty=sold_qty,
                    )
                    self._spawn_tp_watcher(
                        round_start_ts=round_ts,
                        server_order_id=order.server_order_id or "",
                        token_id=action.token,
                        tp_price=order.price,
                        size=sold_qty,
                        expires_at=order.expires_at,
                    )
                    log.info(
                        "router.tp_placed",
                        token=action.token[:16],
                        strategy_price=action.price,
                        qty=action.qty,
                        server_id=order.server_order_id or "none",
                        watcher=True,
                    )
                except Exception as exc:
                    log.exception("router.tp_runner_crashed", error=str(exc))
                    # Defensive release of _tp_inflight. Even if
                    # notify_tp_post_error / realize check paths below
                    # fail (e.g. test stubs that don't implement
                    # notify_tp_post_error), the mutex MUST come down so
                    # subsequent ticks don't re-fire trigger_tp on a
                    # permanently-failed attempt.
                    if eng._round:
                        eng._round.position._tp_inflight = False
                    # H-NEW-2026-07-11 (audit v6-12 production revisit):
                    # engage the backoff gate so trigger_tp is suppressed
                    # for `_tp_backoff_s` seconds (default 60s). Skip
                    # silently on stubs (real production engines always
                    # have this method).
                    if hasattr(eng, "notify_tp_post_error"):
                        try:
                            eng.notify_tp_post_error(
                                reason=f"tp_runner exception: {exc!r}"
                            )
                        except Exception:
                            log.debug(
                                "router.tp_runner.notify_failed",
                                note="notify_tp_post_error raised; "
                                     "continuing with mutex already cleared",
                            )
                    clob = getattr(self._state, "clob", None)
                    if eng._round and clob is not None:
                        try:
                            await _maybe_realize_closed(
                                action.token, clob, eng._round.position
                            )
                        except Exception:
                            log.debug(
                                "router.tp_runner.realize_failed",
                                note="_maybe_realize_closed raised; "
                                     "in-memory state may be stale until "
                                     "next reconcile",
                            )

            task = asyncio.create_task(_tp_runner())
            self._state._bg_tasks.add(task)
            task.add_done_callback(self._state._bg_tasks.discard)

        elif isinstance(action, ActionStopLoss):
            # Taker exit after T4 fills. Cancel resting BUY/TP orders
            # first: a live TP GTD reserves the very shares we need to
            # sell, and a resting BUY GTD could fill into the exit.
            async def _stop_runner() -> None:
                try:
                    if (
                        not eng._round
                        or eng._round.round_start_ts != action.round_start_ts
                    ):
                        log.warning(
                            "router.stop_loss.stale_round",
                            action_round=action.round_start_ts,
                            current_round=getattr(
                                eng._round, "round_start_ts", None
                            ),
                        )
                        eng._stop_loss_inflight = False
                        return
                    pos = eng._round.position
                    try:
                        try:
                            cancelled = await om.cancel_market(
                                action.round_start_ts, asset=self.asset
                            )
                        except TypeError:
                            # Legacy test double / older manager.
                            cancelled = await om.cancel_market(
                                action.round_start_ts
                            )
                        if cancelled:
                            log.warning(
                                "router.stop_loss.cancelled_orders",
                                stage=action.stage,
                                count=len(cancelled),
                            )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "router.stop_loss.cancel_failed",
                            stage=action.stage,
                            error=str(exc),
                            note="continuing; the sell may fail while the "
                                 "GTD still reserves shares",
                        )
                    order = await om.place_market_sell(
                        token_id=action.token,
                        size=action.qty,
                        reference_price=action.price,
                        round_start_ts=action.round_start_ts,
                        tier_name=f"SL{action.stage}",
                        tick_size=tick_size,
                        asset=self.asset,
                    )
                    if not order:
                        log.error(
                            "router.stop_loss_failed",
                            stage=action.stage,
                            reason=action.reason,
                            qty=action.qty,
                        )
                        eng.notify_stop_error(
                            action.stage,
                            reason="place_market_sell returned None",
                        )
                        # If the wallet is actually empty (manual UI sale,
                        # stale in-memory inventory), clear the phantom
                        # position instead of retrying forever.
                        clob = getattr(self._state, "clob", None)
                        if clob is not None:
                            try:
                                await _maybe_realize_closed(
                                    action.token, clob, pos
                                )
                            except Exception:  # noqa: BLE001
                                pass
                        return
                    sid = order.server_order_id or ""
                    sold = (
                        order.placed_size if order.placed_size > 0
                        else order.size
                    )
                    pos.register_tp(
                        server_order_id=sid,
                        token_id=action.token,
                        target_qty=sold,
                    )
                    pos.record_tp_fill(sid, order.price, sold)
                    if pos.total_qty <= 1e-9:
                        pos.realize_position_closed(
                            source="router.stop_loss",
                            note=f"stop-loss stage {action.stage} closed "
                                 "the position",
                        )
                    eng.notify_stop_filled(action.stage, sold)
                    log.warning(
                        "router.stop_loss_filled",
                        stage=action.stage,
                        reason=action.reason,
                        mid=action.price,
                        sold_qty=sold,
                        avg_price=order.price,
                        remaining_qty=pos.total_qty,
                        server_id=sid[:16],
                    )
                except Exception as exc:  # noqa: BLE001
                    log.exception(
                        "router.stop_runner_crashed", error=str(exc)
                    )
                    try:
                        eng.notify_stop_error(
                            action.stage,
                            reason=f"runner exception: {exc!r}",
                        )
                    except Exception:  # noqa: BLE001
                        pass

            task = asyncio.create_task(_stop_runner())
            self._state._bg_tasks.add(task)
            task.add_done_callback(self._state._bg_tasks.discard)

        elif isinstance(action, ActionCancelTier):
            # H-NEW-2026-07-15 tick-driven refactor: the engine emits
            # ActionCancelTier when its `_reconcile_inflight_buys` step
            # decides a BUY GTD is no longer wanted (mid dropped into
            # the next-tier zone, or mid rose into the TP fire zone).
            # This is the SOLE BUY-side cancel path under the new model;
            # the TierWatcher is passive and does not cancel on
            # fill-ratio or mid-cross.
            #
            # H-NEW-2026-07-15 (audit follow-up — TP mid-retreated): the
            # engine can ALSO emit ActionCancelTier with `tier=-1` as a
            # sentinel meaning "this is a TP SELL GTD, cancel it because
            # mid has retreated below the fire line". We dispatch to a
            # separate code path that doesn't touch the BUY-tier
            # TierSnapshot (BUY tiers don't own TP GTDs) and instead
            # releases `pos._tp_inflight` so the next tick can re-arm.
            #
            # H-NEW-2026-07-15 (audit follow-up): optimistic release.
            # Previously `placed` stayed True until the CLOB round-trip
            # finished, opening a window in which mid could oscillate
            # back above the next-tier fire line and have the engine
            # re-fire `trigger_tN` for the same tier. Now we drop
            # `placed` BEFORE awaiting the cancel so subsequent ticks
            # cannot re-trigger downstream of a pending cancel. We
            # always clear `cancel_pending` (success or failure) so a
            # future legitimate cancel — e.g. an external force-close
            # — is not lost behind a stale flag.
            #
            # ── TP path (tier=-1 sentinel) ─────────────────────────────
            if action.tier < 0:
                if eng._round is None:
                    log.warning(
                        "router.cancel_tp.no_round",
                        server_id=action.server_order_id[:16],
                        reason=action.reason,
                    )
                    return
                pos = eng._round.position
                # Release the mutex optimistically. The engine already
                # cleared _tp_inflight in the mid-retreated branch; this
                # is idempotent.
                if pos._tp_inflight:
                    pos._tp_inflight = False
                ok = await om.cancel_by_server_id(
                    action.server_order_id, reason=action.reason
                )
                if ok:
                    pos.tp_cancel_retry_after_ts = 0.0
                    # Book any fill that raced the cancel BEFORE closing the
                    # snapshot, then stop the watcher (no more polling).
                    await self._final_reconcile_before_close(
                        action.server_order_id
                    )
                    watcher = self._watcher_by_server_id.pop(
                        action.server_order_id, None
                    )
                    if watcher is not None:
                        try:
                            await watcher.stop()
                        except Exception:
                            pass
                    # Cancellation confirmed: the TP slot is free. (If the
                    # cancel failed, keep the snapshot so a late fill is
                    # still attributed and no second TP is posted.)
                    pos.close_tp_order(action.server_order_id)
                    log.info(
                        "router.cancel_tp.done",
                        server_id=action.server_order_id[:16],
                        reason=action.reason,
                    )
                else:
                    # Back off before retrying the cancel; the snapshot is
                    # kept so `has_open_tp_orders` still blocks a second TP.
                    pos.tp_cancel_retry_after_ts = time.time() + 2.0
                    log.warning(
                        "router.cancel_tp.failed",
                        server_id=action.server_order_id[:16],
                        reason=action.reason,
                        note="mutex already released; GTD will sit on CLOB "
                             "until natural expiry or the next reconcile",
                    )
                return

            # ── BUY-tier path (tier in 1..4) ───────────────────────────
            tier_idx = action.tier - 1
            tier_snap = (
                eng._round.position.tiers[tier_idx]
                if eng._round and 0 <= tier_idx < len(eng._round.position.tiers)
                else None
            )
            if tier_snap is None:
                log.warning(
                    "router.cancel_tier.invalid",
                    tier=action.tier,
                    server_id=action.server_order_id[:16] if action.server_order_id else "",
                    reason="tier index out of range",
                )
                return
            # Optimistic release: drop the placed flag now so a mid
            # rebound on a subsequent tick doesn't try to re-trigger
            # an averaging tier whose GTD is on its way out.
            tier_snap.placed = False
            tier_snap.watching = False
            tier_snap.expires_at = 0.0
            ok = await om.cancel_by_server_id(action.server_order_id, reason=action.reason)
            # Always clear the cancel-pending latch, success or fail —
            # the dispatch attempt is final from the engine's view
            # (the engine checks `cancel_pending` to single-flight).
            tier_snap.cancel_pending = False
            if ok:
                tier_snap.cancel_retry_after_ts = 0.0
                # Book fills that raced the cancel, then stop the watcher.
                # Only after this final reconcile is the tier slot truly
                # free; a later re-arm therefore sizes against the correct
                # remaining budget.
                await self._final_reconcile_before_close(
                    action.server_order_id
                )
                watcher = self._watcher_by_server_id.pop(
                    action.server_order_id, None
                )
                if watcher is not None:
                    try:
                        await watcher.stop()
                    except Exception:
                        pass
                # We keep server_order_id so a late-arriving WS TRADE
                # event can still attribute to this tier. The
                # optimistic release already removed the placed flag,
                # so has_inflight_tier() no longer counts this slot.
                log.info(
                    "router.cancel_tier.done",
                    tier=action.tier,
                    server_id=action.server_order_id[:16],
                    reason=action.reason,
                    size_filled=tier_snap.size_filled,
                    size_expected=tier_snap.size_expected,
                )
            else:
                # The cancel was NOT confirmed. If the order is still live
                # on the CLOB, the slot is NOT free: restore placed/watching
                # so `total_notional_committed` keeps reserving this tier's
                # notional and the engine cannot add another order on top of
                # it. (If the order already reached a terminal state, leave
                # the optimistic release in place.)
                tracked = om.by_server_id(action.server_order_id)
                still_live = (
                    tracked is not None
                    and tracked.status in ("placed", "live")
                )
                if still_live:
                    tier_snap.placed = True
                    tier_snap.watching = True
                    tier_snap.expires_at = tracked.expires_at
                    # Do not re-issue the cancel on every 250 ms tick.
                    tier_snap.cancel_retry_after_ts = time.time() + 2.0
                log.warning(
                    "router.cancel_tier.failed",
                    tier=action.tier,
                    server_id=action.server_order_id[:16],
                    reason=action.reason,
                    size_filled=tier_snap.size_filled,
                    size_expected=tier_snap.size_expected,
                    still_live=still_live,
                )

    def _make_fill_callback(self):
        """Create a fill callback that routes to engine (used by CLOBClient polling).

        CLOBClient now uses a FillDeduper to translate cumulative observations
        into per-event deltas. The callback receives `(token_id, order_id,
        fill_price, fill_delta_size, fill_total_size)`:

          * `fill_delta_size`  — strict increment since the last fire (B-3 fix;
            the engine must treat this as a delta, not a cumulative).
          * `fill_total_size`  — cumulative matched size; used by OrderManager
            to keep its tracked-order state in monotonic lock-step with the
            server. Always > 0 once deduper has accepted the observation.

        The engine end (on_order_filled -> record_fill) already does
        `size_filled += size`, which under delta semantics gives an exact sum,
        regardless of how many times WS / polling / FAK re-observe the order.
        """
        om = self._state.order_manager

        def on_fill(
            token_id: str,
            order_id: str,
            fill_price: float,
            fill_delta_size: float,
            fill_total_size: float,
            trader_side: str | None = None,
        ):
            # All per-asset routers install the same dispatcher: resolve the
            # engine from the order's asset so the fill always lands on the
            # market that owns it.
            resolver = getattr(self._state, "engine_for_order", None)
            eng = resolver(order_id) if callable(resolver) else self._eng()
            if not eng or not om:
                return
            if fill_total_size <= 0:
                return

            # H-NEW-2026-07-12 (audit): defence-in-depth against the
            # WS-TRADE / polling double-fire race. FillDeduper's bookmark
            # only sees fills that *this polling path* observed; the WS
            # TRADE path advances `om.by_server_id(order_id).size_matched`
            # directly without going through FillDeduper. Concretely:
            #
            #   T+0     GTD SELL 推送，FillDeduper.bookmark=0
            #   T+50ms  WS TRADE event → main.py on_maker_fill
            #           → om.apply_fill_event(size_matched=10)
            #           FillDeduper.bookmark 仍是 0 ← 不一致
            #   T+1500  polling 看到 size_matched=10
            #           → FillDeduper.submit(10) → delta=10-0=10
            #           → 推给 engine 又减 _total_qty 一次
            #
            # Monotonic-max against OrderManager trims that delta down to
            # "only what *this* fill_callback is contributing on top of
            # what the WS already told us". If WS got there first, the
            # polling delta is 0 and we skip the engine call entirely.
            tracked = om.by_server_id(order_id)
            prev_size_matched = tracked.size_matched if tracked else 0.0
            effective_delta = fill_total_size - prev_size_matched
            if effective_delta <= 1e-9:
                # WS path already advanced OrderManager past this observation,
                # OR the FillDeduper itself rejected this as a duplicate.
                # Mirror the cumulative into OrderManager (safe — monotonic
                # max) and skip the engine call.
                if effective_delta < -1e-9:
                    log.warning(
                        "router.fill.out_of_order",
                        order_id=order_id[:16],
                        fill_total_size=fill_total_size,
                        prev_size_matched=prev_size_matched,
                        fill_delta_size=fill_delta_size,
                        note="polling observed an OLDER size_matched than WS "
                             "already pushed — likely out-of-order delivery; "
                             "clamping to skip",
                    )
                    om.reconcile_fill_terms(
                        order_id,
                        cumulative=fill_total_size,
                        fill_price=fill_price,
                        trader_side=trader_side,
                    )
                else:
                    # Same cumulative as OrderManager already booked. The
                    # earlier booking may have been the WS UPDATE (limit
                    # price, no role) — this observation carries the real
                    # execution price and, for FAK sells, TAKER.
                    om.reconcile_fill_terms(
                        order_id,
                        cumulative=fill_total_size,
                        fill_price=fill_price,
                        trader_side=trader_side,
                    )
                return
            fill_delta_size = effective_delta

            # 1) Mirror the cumulative into OrderManager FIRST (monotonic),
            #    then promote status. mark_filled compares size_matched to
            #    the expected size, so it must run after apply_fill_event.
            om.apply_fill_event(
                order_id,
                size_matched=fill_total_size,
                fill_price=fill_price,
                trader_side=trader_side,
            )
            om.mark_filled(order_id)

            # 2) Feed the engine position state with the DELTA. record_fill
            #    does `size_filled += size` which under delta semantics is
            #    exactly correct — no double counting (B-3).
            eng.on_order_filled(
                token_id, order_id, fill_price, fill_delta_size
            )

            log.info(
                "router.fill",
                token=token_id[:16],
                order_id=order_id[:16],
                fill_price=fill_price,
                fill_delta_size=fill_delta_size,
                fill_total_size=fill_total_size,
                trader_side=trader_side,
            )
        return on_fill


# ---------------------------------------------------------------------------
# Round lifecycle
# ---------------------------------------------------------------------------

async def _on_binance_connect() -> None:
    log.debug("binance_feed.connected")


def _on_market_discovered(asset: str, mkt) -> None:
    """Called by WindowManager as soon as market info (token IDs) is resolved.

    Runs once per asset discovery (boundary, retry and late-binding). The
    per-asset token sets are merged into the shared MarketWS / UserWS
    subscriptions so all seven hourly markets are streamed concurrently.
    """
    state = STATE
    asset = str(asset or "BTC").upper()

    prev_mkt = state.markets.get(asset)
    prev_cid = getattr(prev_mkt, "condition_id", "") if prev_mkt else ""
    prev_up = getattr(prev_mkt, "token_up_id", "") if prev_mkt else ""
    is_transition = bool(prev_cid and prev_cid != mkt.condition_id)

    # Clear the previous window's tokens for THIS asset only.
    old_tokens = set(state.market_tokens.get(asset, set()))
    new_tokens = {
        t for t in (mkt.token_up_id, mkt.token_down_id) if t
    }
    for old_token in old_tokens - new_tokens:
        state.price_cache.clear_token(old_token)
    state.market_tokens[asset] = new_tokens
    state.markets[asset] = mkt

    primary = state.assets[0] if state.assets else "BTC"
    if asset == primary:
        state.current_market = mkt

    # Late discovery: `_on_new_round` may have run before this market was
    # listed (Gamma lag at the boundary). Refresh the round metadata now
    # that the tokens are known; the history row itself is written lazily
    # on the first fill.
    om = getattr(state, "order_manager", None)
    register_round_meta = getattr(om, "set_round_meta", None)
    round_ts = mkt.round_start_ts or state.round_start_ts
    if callable(register_round_meta) and round_ts:
        register_round_meta(
            round_start_ts=round_ts,
            asset=asset,
            slug=mkt.slug or None,
            condition_id=mkt.condition_id or None,
            up_token=mkt.token_up_id or None,
            down_token=mkt.token_down_id or None,
            tick_size=mkt.tick_size,
        )
    paper_brokers = getattr(state, "paper_brokers", None) or {}
    broker = paper_brokers.get(asset) or getattr(state, "paper_broker", None)
    if broker is not None and round_ts:
        try:
            broker.set_round_meta(round_ts, mkt)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "paper.round_meta_failed", asset=asset, error=str(exc)
            )

    # Market WS: union of every asset's current tokens. set_active_tokens
    # replaces the subscription set, so send the full union each time.
    if state.market_ws:
        try:
            all_tokens: set[str] = set()
            for tokens in state.market_tokens.values():
                all_tokens.update(tokens)
            if all_tokens:
                state.market_ws.set_active_tokens(all_tokens)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "market.subscribe_failed",
                asset=asset,
                cid=mkt.condition_id[:16],
                error=str(exc),
                note="engine still receives the market; MarketWS will "
                     "re-subscribe on its next reconnect",
            )

    # User WS: Polymarket only pushes order/trade events for markets named
    # in the subscribe frame. Send every current asset's condition id.
    if state.user_ws:
        current_cids = {
            m.condition_id
            for m in state.markets.values()
            if m is not None and getattr(m, "condition_id", "")
        }
        state.condition_ids = set(current_cids)
        try:
            state.user_ws.subscribe_markets(sorted(current_cids))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "user_ws.subscribe_markets_failed",
                asset=asset,
                cid=mkt.condition_id[:16],
                error=str(exc),
            )

    # Late discovery: bind the tokens to the already-created RoundState.
    engines = getattr(state, "engines", None) or {}
    eng = engines.get(asset) or getattr(state, "engine", None)
    if eng is not None and eng._round is not None:
        try:
            eng.attach_market(mkt)
        except Exception:
            log.exception(
                "market.attach_market_failed",
                asset=asset,
                cid=mkt.condition_id[:16],
            )
        ctx = state.wm.get(asset) if state.wm else None
        if (
            ctx is not None
            and ctx.skip_until_next_window
            and eng._round is not None
            and not eng._round.skip_reason
        ):
            eng._round.skip_reason = "window_precheck"
            log.warning(
                "round.skip.late_market",
                asset=asset,
                round_start_ts=eng._round.round_start_ts,
                reason="window_precheck",
            )

    if is_transition:
        log.info(
            "market.transition",
            asset=asset,
            slug=mkt.slug or "",
            cid=mkt.condition_id[:16],
            up=mkt.token_up_id[:16],
            down=mkt.token_down_id[:16],
            round_start_ts=mkt.round_start_ts,
            window_start=mkt.window_start.isoformat(),
            window_end=mkt.window_end.isoformat(),
            prev_cid=prev_cid[:16],
            prev_up=(prev_up[:16] if prev_up else "none"),
        )
    else:
        log.info(
            "market.discovered",
            asset=asset,
            slug=mkt.slug or "",
            cid=mkt.condition_id[:16],
            up=mkt.token_up_id[:16],
            down=mkt.token_down_id[:16],
        )


async def _on_new_round(round_start_ts: int) -> None:
    """Called by WindowManager on every hourly boundary.

    Every asset shares the same boundary, so this is a two-phase sweep:
    close the previous round for all seven engines, then seed the new
    round for each asset from its own WindowContext market.
    """
    state = STATE

    # ── End the previous round (all assets) ─────────────────────────────
    if state.round_start_ts > 0:
        prev = state.round_start_ts
        router = getattr(state, "router", None)
        if router is not None:
            try:
                await router.stop_all_watchers()
            except Exception as exc:
                log.warning("router.stop_all_watchers.error", error=str(exc))
        for asset, eng in list(state.engines.items()):
            try:
                await eng.on_round_end()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "round.end_engine_failed", asset=asset, error=str(exc)
                )
        for asset, broker in list(state.paper_brokers.items()):
            try:
                await broker.cancel_round(prev)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "round.end_paper_failed", asset=asset, error=str(exc)
                )
        if state.order_manager:
            # asset=None: cancel every market that shares the boundary.
            await state.order_manager.cancel_market(prev)
            try:
                await state.order_manager.reconcile()
            except Exception as exc:
                log.warning("round.reconcile_failed", error=str(exc))
            purged = await state.order_manager.purge_round(prev)
            if purged:
                log.info(
                    "round.purged_prev_state",
                    prev_round_start=prev, purged=purged,
                )
        log.info(
            "round.transition",
            prev_round_start=prev, new_round_start=round_start_ts,
        )

    state.round_start_ts = round_start_ts

    # ── Seed the new round for every asset ──────────────────────────────
    # A stale market from the previous round is rejected; the engine starts
    # without tokens and is late-bound by `_on_market_discovered`.
    om = getattr(state, "order_manager", None)
    register_round_meta = getattr(om, "set_round_meta", None)
    for asset in state.assets:
        ctx = state.wm.get(asset) if state.wm else None
        mkt = ctx.market if ctx else None
        if (
            mkt is not None
            and mkt.round_start_ts
            and mkt.round_start_ts != round_start_ts
        ):
            log.warning(
                "round.stale_market_rejected",
                asset=asset,
                round_start_ts=round_start_ts,
                stale_market_round_ts=mkt.round_start_ts,
                stale_slug=mkt.slug or "",
            )
            mkt = None
        state.markets[asset] = mkt

        eng = state.engines.get(asset)
        if eng is not None:
            try:
                await eng.on_new_round(round_start_ts, mkt)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "round.start_engine_failed",
                    asset=asset,
                    error=str(exc),
                )
            if (
                ctx is not None
                and ctx.skip_until_next_window
                and eng._round is not None
            ):
                eng._round.skip_reason = "window_precheck"
                log.warning(
                    "round.skip",
                    asset=asset,
                    round_start_ts=round_start_ts,
                    reason="window_precheck",
                    note="WindowManager flagged this window as unsafe "
                         "(late boot)",
                )
        broker = state.paper_brokers.get(asset)
        if broker is not None:
            try:
                broker.begin_round(round_start_ts, mkt)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "paper.begin_round_failed", asset=asset, error=str(exc)
                )

        if callable(register_round_meta):
            register_round_meta(
                round_start_ts=round_start_ts,
                asset=asset,
                slug=mkt.slug if mkt else None,
                condition_id=mkt.condition_id if mkt else None,
                up_token=mkt.up_token if mkt else None,
                down_token=mkt.down_token if mkt else None,
                tick_size=mkt.tick_size if mkt else 0.01,
            )
        if mkt is None:
            # Drop the expired window's tokens from the shared cache and
            # subscription set; the retry/late discovery will add the new
            # market's tokens.
            for token in state.market_tokens.get(asset, set()):
                state.price_cache.clear_token(token)
            state.market_tokens[asset] = set()
            log.warning(
                "round.market_not_found",
                asset=asset,
                round_start_ts=round_start_ts,
                note="waiting for WindowManager retry discovery; engine "
                     "starts without tokens and is late-bound by "
                     "_on_market_discovered",
            )

    primary = state.assets[0] if state.assets else "BTC"
    state.current_market = state.markets.get(primary)

    # Re-publish the union of live subscriptions (drops expired tokens even
    # when one asset's discovery failed).
    if state.market_ws:
        try:
            all_tokens: set[str] = set()
            for tokens in state.market_tokens.values():
                all_tokens.update(tokens)
            state.market_ws.set_active_tokens(all_tokens)
        except Exception as exc:  # noqa: BLE001
            log.warning("market.resubscribe_failed", error=str(exc))
    if state.user_ws:
        current_cids = {
            m.condition_id
            for m in state.markets.values()
            if m is not None and getattr(m, "condition_id", "")
        }
        state.condition_ids = set(current_cids)
        try:
            state.user_ws.subscribe_markets(sorted(current_cids))
        except Exception as exc:  # noqa: BLE001
            log.warning("user_ws.resubscribe_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Round settlement
# ---------------------------------------------------------------------------
#
# A losing position is never sold — it simply expires worthless. Without
# recording the market outcome the dashboard cannot tell a settled loss
# apart from a still-open position, so those losses were missing from the
# PnL / win-rate totals (operator report 2026-10-31). Gamma exposes the
# resolved outcome; poll it for every ended round until it answers.
#
# Resolution is usually available seconds after the window closes, but
# Gamma can lag or briefly 5xx. Attempts are capped per round so a market
# that never resolves (e.g. voided) does not get polled forever.

_RESOLUTION_POLL_S = 30.0
_RESOLUTION_GRACE_S = 15.0
_RESOLUTION_LOOKBACK_S = 30 * 86400
_RESOLUTION_MAX_ATTEMPTS = 40
_RESOLUTION_BATCH = 50


async def _resolve_rounds_once(
    state: AppState, attempts: dict[tuple[int, str], int]
) -> int:
    """Resolve ended rounds whose outcome is not yet in the DB.

    Returns the number of rounds resolved this pass. `attempts` is owned
    by the caller so the cap survives across polls.
    """
    pm = getattr(state, "pm_client", None)
    db = getattr(state, "db", None)
    if pm is None or db is None:
        return 0

    period = int(general().ROUND_PERIOD_S)
    before_ts = int(time.time() - period - _RESOLUTION_GRACE_S)
    try:
        rows = db.unresolved_rounds(
            before_ts=before_ts,
            lookback_s=_RESOLUTION_LOOKBACK_S,
            limit=_RESOLUTION_BATCH,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("round.resolve.query_failed", error=str(exc))
        return 0

    resolved_count = 0
    for row in rows:
        if len(row) >= 6:
            round_ts, cid, slug, up_token, down_token, asset = row[:6]
            legacy_key = False
        else:
            # Legacy single-asset DB / test double.
            round_ts, cid, slug, up_token, down_token = row[:5]
            asset = "BTC"
            legacy_key = True
        # Both token ids are required to attribute the held side. Rows
        # written by very old builds may lack them; they cannot be
        # settled from Gamma's UP/DOWN outcome alone.
        if not up_token or not down_token:
            continue
        key = (
            int(round_ts)
            if legacy_key
            else (int(round_ts), str(asset or "BTC").upper())
        )
        tries = attempts.get(key, 0)
        if tries >= _RESOLUTION_MAX_ATTEMPTS:
            continue
        attempts[key] = tries + 1
        try:
            res = await asyncio.wait_for(
                pm.get_market_resolution(cid, slug=slug or None),
                timeout=10.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "round.resolve.failed",
                asset=asset,
                round_start_ts=round_ts, cid=(cid or "")[:12], error=str(exc),
            )
            continue
        if not res or not res.get("resolved"):
            continue
        up_won = bool(res.get("up_wins"))
        try:
            try:
                db.set_round_resolution(
                    round_ts, up_won, asset=str(asset or "BTC").upper()
                )
            except TypeError:
                # Legacy test double / older DB method.
                db.set_round_resolution(round_ts, up_won)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "round.resolve.persist_failed",
                asset=asset,
                round_start_ts=round_ts, error=str(exc),
            )
            continue
        attempts.pop(key, None)
        resolved_count += 1
        log.info(
            "round.resolved",
            asset=asset,
            round_start_ts=round_ts,
            slug=slug or "",
            up_won=up_won,
            note="settlement outcome recorded for PnL accounting",
        )

    # Bound the attempts map on a long-lived process.
    if len(attempts) > 500:
        cutoff = int(time.time()) - _RESOLUTION_LOOKBACK_S
        stale = [
            k for k in attempts
            if (k[0] if isinstance(k, tuple) else k) < cutoff
        ]
        for key in stale:
            attempts.pop(key, None)
    return resolved_count


async def _resolution_loop() -> None:
    """Poll Gamma for the outcome of ended rounds (settlement accounting)."""
    attempts: dict[tuple[int, str], int] = {}
    while True:
        try:
            await _resolve_rounds_once(STATE, attempts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("round.resolve.loop_error", error=str(exc))
        await asyncio.sleep(_RESOLUTION_POLL_S)


# ---------------------------------------------------------------------------
# REST midpoint poller (stable prices for strategy decisions)
# ---------------------------------------------------------------------------

async def _paper_tick_loop(state: AppState) -> None:
    """Drive every paper broker's resting GTD fill/expiry simulation."""
    while True:
        for asset, broker in list(state.paper_brokers.items()):
            try:
                broker.tick()
            except Exception:  # noqa: BLE001
                log.exception("paper.tick_failed", asset=asset)
        await asyncio.sleep(0.1)


async def _midpoint_poll_loop() -> None:
    """Poll /midpoint for every asset every 2s to keep the cache fresh.

    The strategy still decides solely from its 250 ms DecisionLoop tick;
    this loop only refreshes the shared PriceCache. Seven assets × two
    tokens are fetched concurrently — the signal-layer order-book depth
    calls were removed with the pure-martingale refactor.
    """
    last_condition_ids: dict[str, str] = {}
    while True:
        await asyncio.sleep(2.0)
        s = STATE
        if not s.pm_client:
            continue

        targets: list[tuple[str, object]] = []
        for asset in s.assets:
            ctx = s.wm.get(asset) if s.wm else None
            mkt = ctx.market if ctx else s.markets.get(asset)
            if (
                mkt is None
                or not getattr(mkt, "token_up_id", "")
                or not getattr(mkt, "token_down_id", "")
            ):
                continue
            targets.append((asset, mkt))

        if not targets:
            continue

        async def _mid(token_id: str) -> float:
            try:
                return float(await s.pm_client.get_midpoint(token_id))
            except Exception:  # noqa: BLE001
                return 0.0

        # Fetch all 14 midpoints concurrently so one slow endpoint cannot
        # serialise the other six assets' price refresh.
        async def _pair(mkt) -> tuple[float, float]:
            return await asyncio.gather(
                _mid(mkt.token_up_id), _mid(mkt.token_down_id)
            )

        results = await asyncio.gather(
            *(_pair(mkt) for _asset, mkt in targets)
        )
        for (asset, mkt), (up_mid, down_mid) in zip(targets, results):
            up_token = mkt.token_up_id
            down_token = mkt.token_down_id
            cid = mkt.condition_id or ""
            prev_cid = last_condition_ids.get(asset, "")
            if cid != prev_cid and prev_cid:
                log.info(
                    "midpoint.window_changed",
                    asset=asset,
                    prev_condition_id=prev_cid[:16],
                    new_condition_id=cid[:16],
                    up_token=up_token[:16],
                    down_token=down_token[:16],
                    round_start_ts=mkt.round_start_ts,
                )
            last_condition_ids[asset] = cid

            if up_mid > 0:
                s.price_cache.midpoint[up_token] = up_mid
            if down_mid > 0:
                s.price_cache.midpoint[down_token] = down_mid


# ---------------------------------------------------------------------------
# Status printer
# ---------------------------------------------------------------------------


def _audit_gtd_invariants(
    s: AppState, asset: str = "BTC", eng: "StrategyEngine | None" = None
) -> None:
    """Audit the GTD invariants every STATUS tick (5 s), per asset.

    Emits ERROR (not just a debug line) when one is violated so an operator
    can act immediately:
      1. at most ONE live order per (round, tier)
      2. every live GTD has a TierWatcher attached
      3. a tier never spends more than its configured notional
      4. total committed exposure never exceeds the configured cap
    """
    eng = eng or getattr(s, "engines", {}).get(asset) or s.engine
    om = s.order_manager
    if not eng or not eng._round or not om:
        return
    pos = eng._round.position
    cap = strategy().total_max_notional

    committed = pos.total_notional_committed
    if committed > cap + 0.01:
        log.error(
            "invariant.round_over_committed",
            asset=asset,
            committed=round(committed, 4),
            cap=cap,
            note="round exposure exceeds total_max_notional",
        )

    # Live GTDs by tier — the authoritative source for "there is still an
    # order that needs a watcher". `tier.placed` is NOT that signal: it is
    # set on placement AND again on the first fill, and only cleared on
    # cancel/expire. So the old `placed and not watching` check flagged
    # every COMPLETED tier for the rest of the round (production
    # 2026-10-30: T1/T3 filled normally but warned every 5 s until the
    # window ended).
    live_by_tier: dict[str, list[str]] = {}
    try:
        live_orders = om.open_orders_for_round(
            s.round_start_ts, asset=asset
        )
    except TypeError:
        # Legacy single-asset test doubles / older managers take one arg.
        live_orders = om.open_orders_for_round(s.round_start_ts)
    for order in live_orders:
        live_by_tier.setdefault(order.tier_name, []).append(
            order.client_order_id
        )

    for tier in pos.tiers:
        if tier.cost_filled > tier.notional + 0.01:
            log.error(
                "invariant.tier_over_budget",
                asset=asset,
                tier=tier.tier,
                spent=round(tier.cost_filled, 4),
                notional=tier.notional,
                note="tier spent more than its configured notional",
            )
        live_ids = live_by_tier.get(f"T{tier.tier}")
        if live_ids and not tier.watching:
            log.warning(
                "invariant.tier_placed_without_watcher",
                asset=asset,
                tier=tier.tier,
                live_client_ids=live_ids[:4],
                note="a live GTD for this tier has no TierWatcher; terminal "
                     "fills may be missed unless a WS event arrives",
            )

    for tier_name, client_ids in live_by_tier.items():
        if len(client_ids) > 1:
            log.error(
                "invariant.duplicate_live_orders",
                asset=asset,
                tier=tier_name,
                count=len(client_ids),
                client_ids=client_ids[:4],
                note="more than one live GTD for the same tier/round",
            )


async def _status_loop() -> None:
    cfg = log_config()
    last_status_ts = 0.0
    while True:
        await asyncio.sleep(cfg.HEARTBEAT_S)
        s = STATE

        # Throttle status log to every 5s
        now = time.time()
        if now - last_status_ts < 5.0:
            continue
        last_status_ts = now

        wm = s.wm
        next_in = wm.seconds_until_next() if wm else 0
        period_s = wm.window_period_s() if wm else 3600
        remain_s = max(0, min(period_s, next_in))
        elapsed_s = max(0, period_s - remain_s)
        t1_entry_window_s = strategy().T1_ENTRY_WINDOW_S
        t1_status = "IN_T1" if elapsed_s <= t1_entry_window_s else "PAST_T1"
        if remain_s < 1.0:
            t1_status = "END"
        window_str = (
            f"window=[{t1_status}] remain={remain_s:.0f}s/{period_s:.0f}s"
        )

        ks = getattr(s, "killswitch", None)
        stop_str = " [STOPPED]" if (ks is not None and ks.active) else ""

        for asset in s.assets or ["BTC"]:
            eng = s.engines.get(asset)
            mkt = s.markets.get(asset)
            up_token = getattr(mkt, "token_up_id", "") if mkt else ""
            down_token = getattr(mkt, "token_down_id", "") if mkt else ""
            up_last = (
                s.price_cache.get_midpoint(up_token) if up_token else 0.0
            )
            down_last = (
                s.price_cache.get_midpoint(down_token) if down_token else 0.0
            )

            pos_str = ""
            mode_str = "-"
            if eng is not None and eng._round is not None:
                rs = eng._round
                mode_str = rs.mode or "pending"
                pos = rs.position
                tiers_str = ",".join(
                    f"T{t.tier}="
                    f"{'✓' if t.is_complete else ('⏳' if t.placed else '○')}"
                    for t in pos.tiers
                )
                held_mid = 0.0
                if pos.entry_side == "UP":
                    held_mid = up_last
                elif pos.entry_side == "DOWN":
                    held_mid = down_last
                has_pos = (
                    pos.avg_cost > 0 and pos.total_qty > 0 and held_mid > 0
                )
                if has_pos:
                    unreal_pnl = (
                        held_mid - pos.avg_cost
                    ) * pos.total_qty
                    unreal_pnl_pct = (
                        held_mid - pos.avg_cost
                    ) / pos.avg_cost
                    sign = (
                        "+" if unreal_pnl > 0
                        else ("-" if unreal_pnl < 0 else "")
                    )
                    upnl_str = (
                        f"${sign}{abs(unreal_pnl):.2f}"
                        f"({unreal_pnl_pct * 100:+.1f}%)"
                    )
                else:
                    upnl_str = "$0.00"
                tp_target = pos.tp_target()
                tp_str = (
                    f" tpTarget={tp_target:.4f}"
                    if tp_target is not None else ""
                )
                pos_str = (
                    f" | side={pos.entry_side or '-'}"
                    f" tiers=[{tiers_str}]"
                    f" qty={pos.total_qty:.2f}"
                    f" avg={pos.avg_cost:.4f}"
                    f" cost=${pos.total_cost:.2f}"
                    f" mid={held_mid:.4f}"
                    f" uPnL={upnl_str}"
                    f"{tp_str}"
                )
                _audit_gtd_invariants(s, asset, eng)

            market_str = (
                f"mkt={'ok' if mkt is not None else 'none'}"
            )
            if mkt is not None:
                market_str += f" round={getattr(mkt, 'round_start_ts', 0)}"
            log.info(
                f"STATUS[{asset}]{stop_str}  {window_str}  mode={mode_str}"
                f"  {market_str}  UP={up_last:.4f}  DOWN={down_last:.4f}"
                f"{pos_str}"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    configure_logging()
    s = STATE
    log.info("bot.starting", version="0.1.0")

    # Apply strategy overrides saved from the dashboard /config page before
    # any component captures config values. Stored on the /app/data volume so
    # they survive restarts and redeploys.
    try:
        from src.web.runtime_config import apply_persisted_overrides

        apply_persisted_overrides()
    except Exception as exc:  # noqa: BLE001
        log.warning("config.runtime_overrides_failed", error=str(exc))

    cfg_strategy = strategy()

    # ── CLOB credentials ─────────────────────────────────────────────────
    # Priority: env L2 creds > L1 private key derivation
    api_key = cfg_strategy.API_KEY
    api_secret = cfg_strategy.API_SECRET
    api_passphrase = cfg_strategy.API_PASSPHRASE

    if not (api_key and api_secret and api_passphrase):
        if cfg_strategy.WALLET_PRIVATE_KEY:
            try:
                creds = create_or_derive_clob_api_creds(
                    host=cfg_strategy.POLYMARKET_HOST,
                    private_key=cfg_strategy.WALLET_PRIVATE_KEY,
                    chain_id=137,
                )
                api_key = creds.api_key
                api_secret = creds.api_secret
                api_passphrase = creds.api_passphrase
                log.info(
                    "clob_l1_auth.derived",
                    api_key=api_key[:8],
                )
            except ClobL1Error as exc:
                log.error(
                    "clob_l1_auth.failed — trading disabled",
                    error=str(exc),
                )
        else:
            log.warning(
                "CLOB credentials missing: set API_KEY/API_SECRET/API_PASSPHRASE "
                "or WALLET_PRIVATE_KEY in .env"
            )

    # ── CLOB client ──────────────────────────────────────────────────────
    s.clob = CLOBClient(
        host=cfg_strategy.POLYMARKET_HOST,
        private_key=cfg_strategy.WALLET_PRIVATE_KEY,
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        signature_type=cfg_strategy.SIGNATURE_TYPE,
        funder_address=cfg_strategy.FUNDER_ADDRESS,
    )

    # ── Market / Window management ────────────────────────────────────────────
    # PolymarketClient: Gamma + CLOB public API (market discovery, prices, resolution)
    s.pm_client = PolymarketClient(
        gamma_api="https://gamma-api.polymarket.com",
        polymarket_host=cfg_strategy.POLYMARKET_HOST,
    )
    s.assets = cfg_strategy.trade_assets or ["BTC"]
    s.market_svc = MarketService(
        pm=s.pm_client,
        threshold=0.5,
        slug_interval=str(cfg_strategy.MARKET_SLUG_INTERVAL or "1h"),
        window_sec=int(cfg_strategy.MARKET_WINDOW_SEC or 3600),
    )
    # Pure price martingale: no Binance spot feed and no drift/baseline
    # gate. WindowManager still discovers every asset's hourly market and
    # fires the shared boundary callback.
    s.binance_spot = None
    s.wm = WindowManager(
        market_svc=s.market_svc,
        symbols=s.assets,
        on_new_round=_on_new_round,
        on_market_discovered=_on_market_discovered,
        binance_spot=None,
        entry_cfg=None,
        round_period_s=int(general().ROUND_PERIOD_S),
        on_window_baseline=None,
    )
    log.info(
        "bot.universe",
        assets=s.assets,
        slug_interval=str(cfg_strategy.MARKET_SLUG_INTERVAL or "1h"),
        window_s=int(cfg_strategy.MARKET_WINDOW_SEC or 3600),
    )

    # ── Execution layer ─────────────────────────────────────────────────
    s.order_manager = OrderManager(
        s.clob,
        price_cache=s.price_cache,
        pm_client=s.pm_client,
        db=s.db,
        killswitch=s.killswitch,
    )
    # H-NEW-2026-07-11 (audit v6-12 production revisit): if a previous
    # bot process crashed (or `_safe_cancel` lost its race against SDK
    # signature bugs), dangling GTDs on the CLOB still occupy the maker
    # allowance and block any subsequent SELL with `not enough
    # balance/allowance`. Force-cancel them BEFORE the first round
    # starts so an old stuck `0x3ede40a1f6c744`-style order cannot
    # wedge the account into "I still hold 5 tokens but cannot sell".
    try:
        await s.order_manager.startup_reconcile()
    except Exception as exc:
        log.warning(
            "main.startup_reconcile.failed",
            error=str(exc),
            note="continuing — partial reconciliation may leave a stale order on CLOB",
        )
    # H-NEW-1 fix: tell CLOBClient how to pre-register a server_order_id
    # the moment the post-response arrives. This closes the race where a
    # fill callback fires from inside place_gtd_buy / place_gtd_sell /
    # place_fak_sell BEFORE the awaited call returns to place_buy / etc.,
    # by which time the real TrackedOrder is not yet in OrderManager._orders.
    # See OrderManager.register_pending_server_id docstring.
    s.clob.register_pending = s.order_manager.register_pending_server_id
    s.heartbeat = HeartbeatLoop(s.clob)

    # ── Per-asset execution routers + Strategy engines ──────────────────
    # Both executors stay live for every asset so PAPER_TRADING can be
    # toggled from the config page without a restart. The mode is locked
    # per round at T1. All routers share one OrderManager / CLOB / wallet.
    for asset in s.assets:
        live_router = ActionRouter(s, asset=asset)
        paper_broker = PaperBroker(
            market=f"{asset}-1h",
            asset=asset,
            interval="1h",
            price_cache=s.price_cache,
            db=s.db,
            killswitch=s.killswitch,
            t1_entry_window_s=float(cfg_strategy.T1_ENTRY_WINDOW_S),
        )
        mode_router = ModeRouter(s, live_router, paper_broker, asset=asset)
        engine = StrategyEngine(
            s.binance_cache,
            s.price_cache,
            mode_router,
            killswitch=s.killswitch,
            signals=None,
            asset=asset,
        )
        paper_broker.engine = engine
        mode_router.engine = engine
        live_router._engine = engine

        s.live_routers[asset] = live_router
        s.mode_routers[asset] = mode_router
        s.paper_brokers[asset] = paper_broker
        s.engines[asset] = engine
        s.decision_loops[asset] = DecisionLoop(
            engine, tick_interval_s=0.25
        )
        s.gtd_expiries[asset] = GtdExpiryWatcher(
            order_manager=s.order_manager,
            round_state_provider=(
                lambda eng=engine: eng._round
            ),
            poll_interval_s=2.0,
        )

    # Backward-compatible aliases for the primary asset (and for the many
    # single-asset tests/helpers that still read state.engine).
    primary = s.assets[0]
    s.engine = s.engines[primary]
    s.paper_broker = s.paper_brokers[primary]
    s.decision_loop = s.decision_loops[primary]
    s.gtd_expiry = s.gtd_expiries[primary]
    s.router = RouterGroup(s.mode_routers)
    # Any per-asset router's callback dispatches by order asset, so one
    # installation is enough.
    s.clob.fill_callback = s.live_routers[primary]._make_fill_callback()
    log.info(
        "execution.mode",
        configured=("paper" if cfg_strategy.PAPER_TRADING else "live"),
        assets=s.assets,
        note="mode is applied to the next T1 of each asset; an open round "
             "keeps its entry mode",
    )
    log.info(
        "execution.universe",
        assets=s.assets,
        ladder=cfg_strategy.tier_notionals,
        drops=cfg_strategy.tier_drop_pcts,
        t1_entry_window_s=float(cfg_strategy.T1_ENTRY_WINDOW_S),
        signal_gates="disabled",
    )

    # ── Feeds ───────────────────────────────────────────────────────────
    # The pure martingale needs only the CLOB midpoints (REST poller +
    # shared market WS). No Binance spot feed or 5m signal market is used.
    s.binance_feed = None
    s.market_ws = MarketWS(
        cache=s.price_cache,
        on_connect=None,
        on_disconnect=None,
    )

    # ── User WebSocket (order events) ────────────────────────────────────
    # Use the RESOLVED credentials, not cfg_strategy.*: when the operator
    # supplies only WALLET_PRIVATE_KEY, the L2 creds are derived above into
    # the local variables and cfg_strategy.API_KEY stays empty. The old
    # check therefore disabled UserWS for every private-key-only setup —
    # exactly the configuration that needs the WS fill feed most.
    if api_key and api_secret and api_passphrase:
        s.user_ws = UserWS(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            on_maker_fill=_make_maker_fill_callback(s),
            on_order=_make_user_ws_order_callback(s),
            clob=s.clob,  # H-V6-12: REST fallback when WS size=0
        )
    else:
        log.warning(
            "bot.no_credentials",
            msg="L2 credentials unavailable — UserWS disabled, fill events "
                "will only arrive via TierWatcher REST polling"
        )
        s.user_ws = None

    # ── Start services ──────────────────────────────────────────────────
    await s.market_ws.start()
    await s.wm.start()
    for expiry in s.gtd_expiries.values():
        expiry.start()
    for loop in s.decision_loops.values():
        loop.start()

    if s.user_ws:
        await s.user_ws.start()

    if s.clob.is_configured():
        await s.heartbeat.start()
    else:
        log.error(
            "bot.no_clob_credentials",
            msg="CLOB not configured — cannot start heartbeat. "
                "Set API_KEY / API_SECRET / API_PASSPHRASE in .env",
        )

    # ── Web dashboard (FastAPI on the bot's event loop) ─────────────────
    web_task = _start_dashboard()

    # ── Historical backtest (background daemon thread) ─────────────────
    # Collects the last N days (cached, incremental) and replays the live
    # strategy plus a few variants. Runs off the event loop; the dashboard
    # at /backtest shows progress. A collection failure must never affect
    # trading, hence the broad try.
    try:
        from src.config import backtest as _backtest_cfg
        from src.backtest.runner import get_manager as _get_backtest_manager

        bt = _backtest_cfg()
        if bt.BACKTEST_ENABLED and bt.BACKTEST_COLLECT_ON_START:
            started = _get_backtest_manager().start(
                days=int(bt.BACKTEST_COLLECT_DAYS),
                refresh=False,
                variants=bool(bt.BACKTEST_AUTO_VARIANTS),
            )
            if started:
                log.info(
                    "backtest.job_started",
                    days=int(bt.BACKTEST_COLLECT_DAYS),
                    variants=bool(bt.BACKTEST_AUTO_VARIANTS),
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("backtest.job_start_failed", error=str(exc))

    # Status printer + REST midpoint poller + settlement resolver
    status_task = asyncio.create_task(_status_loop())
    midpoint_task = asyncio.create_task(_midpoint_poll_loop())
    resolution_task = asyncio.create_task(_resolution_loop())
    paper_task = asyncio.create_task(_paper_tick_loop(s))

    # ── Graceful shutdown ───────────────────────────────────────────────
    def _signal_handler() -> None:
        log.info("bot.shutdown_signal_received")
        s._shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, OSError):
            pass

    await s._shutdown.wait()

    # ── Cleanup ─────────────────────────────────────────────────────────
    log.info("bot.shutdown_initiated")
    status_task.cancel()
    midpoint_task.cancel()
    resolution_task.cancel()
    paper_task.cancel()
    # Stop the dashboard first: its routes read `db_conn`, which is closed
    # at the very end of shutdown.
    if web_task is not None:
        web_task.cancel()
        try:
            await web_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("web.stop_failed", error=str(exc))

    # B-3 v2 fix: drain in-flight TP runners / watchers before tearing down
    # dependencies (clob / order_manager). Watchers must be stopped first:
    # they normally live until the GTD expires (up to GTD_LIFETIME_S), so
    # an unbounded gather could hang shutdown for ~5 minutes.
    router = getattr(s, "router", None)
    if router is not None:
        try:
            await router.stop_all_watchers()
        except Exception as exc:
            log.warning("shutdown.stop_watchers_failed", error=str(exc))
    if STATE._bg_tasks:
        pending = [t for t in STATE._bg_tasks if not t.done()]
        if pending:
            _, still_running = await asyncio.wait(pending, timeout=10.0)
            for t in still_running:
                t.cancel()
            if still_running:
                await asyncio.gather(*still_running, return_exceptions=True)
        STATE._bg_tasks.clear()

    for expiry in s.gtd_expiries.values():
        await expiry.stop()
    for loop in s.decision_loops.values():
        await loop.stop()
    if s.wm:
        await s.wm.stop()
    if s.market_ws:
        await s.market_ws.stop()
    if s.user_ws:
        await s.user_ws.stop()
    if s.binance_feed:
        await s.binance_feed.stop()
    if s.heartbeat:
        await s.heartbeat.stop()
    if s.binance_spot:
        await s.binance_spot.close()
    if s.pm_client:
        await s.pm_client.close()
    s.db_conn.close()
    log.info("bot.shutdown_complete")


# ---------------------------------------------------------------------------
# User WS callbacks — route order/trade events to engine
# ---------------------------------------------------------------------------

def _make_maker_fill_callback(state: AppState):
    """
    Route each TRADE event fill to engine.

    H-NEW-2026-07-14 audit: prior shape assumed the user_ws callback
    delivered ONE callback per maker_orders[] entry. Polymarket's actual
    semantics is one TRADE event = one broadcast that may contain
    multiple maker_orders[] rows (liquidity from multiple makers can be
    aggregated into a single fill). We now receive ONE callback per
    TRADE event carrying `candidates=[(order_id, asset_id, matched_amount)]`
    — the union of taker_order_id and every maker_orders[].order_id (full
    hashes, never truncated). The router's job is to find the candidate
    whose order_id lives in our OrderManager and use IT for attribution.

    Backwards compatibility: older wiring that passes only `fill` is
    still supported by user_ws (it falls back to the 1-arg signature). In
    that case the first candidate wins as before; if it doesn't match
    our tracked orders the existing untracked-breadcrumb path handles it.

    Trade-id dedupe (H-NEW-2026-07-14): polymarket User Channel emits
    a TRADE event ONCE per status transition (MATCHED → MINED →
    CONFIRMED). Each emission carries the same `trade_id` and the same
    `size` (per-trade share count). Without dedupe here, every status
    upgrade would re-route `fill.size` into `record_fill` and inflate
    `_total_qty`. We track trades we've already fired for in a bounded
    LRU on the closure — trade_id is 36 chars and well-behaved.
    """
    om = state.order_manager
    _seen_trades: set[str] = set()
    _SEEN_TRADES_LIMIT = 1024
    # server_order_id -> sum of the per-trade volumes WE have seen. Used as
    # the trade-side cumulative so a WS ORDER-UPDATE that already mirrored
    # the same fill cannot be added a second time by the TRADE event.
    _trade_totals: dict[str, float] = {}
    _TRADE_TOTALS_LIMIT = 4096

    def on_maker_fill(fill, candidates=None) -> None:
        if not om:
            return

        # ------------------------------------------------------------------
        # Trade-id dedupe (MATCHED/MINED/CONFIRMED re-fires are common).
        # ------------------------------------------------------------------
        trade_id = getattr(fill, "trade_id", "") or ""
        if trade_id:
            if trade_id in _seen_trades:
                log.debug(
                    "router.maker_fill.trade_dedup",
                    trade_id=trade_id[:16], status=fill.status,
                    note="same trade id re-fired with new status — skip",
                )
                return
            _seen_trades.add(trade_id)
            # Bound the dedupe cache so it doesn't grow unbounded across
            # many rounds.
            if len(_seen_trades) > _SEEN_TRADES_LIMIT:
                # Drop the oldest half by recreating; trades don't recur.
                # (We don't need LRU semantics — recent trades we've
                # ignored already won't come back with new info.)
                _seen_trades.clear()
                # Re-add this trade so we don't re-fire within this batch.
                _seen_trades.add(trade_id)

        # ------------------------------------------------------------------
        # Candidate resolution
        # ------------------------------------------------------------------
        # Build the candidate list. If user_ws supplied the keyword arg we
        # use it; otherwise fall back to the legacy single-order_id flow.
        if candidates:
            # Each row: (order_id, asset_id, matched_amount, maker_price,
            # rest_filled_size). All FULL hashes; per-row values come
            # straight from maker_orders[] in user_ws (or were filled by
            # the per-candidate REST fallback when the WS payload
            # collapsed the maker row's amount — see user_ws docstring
            # H-NEW-2026-07-11-2). The rest_filled_size is informational;
            # router uses matched_amount (chosen[2]) as the authoritative
            # per-maker share count.
            candidate_pairs = [
                (oid, aid, float(amt or 0), float(mprice or 0), float(rest or 0))
                for (oid, aid, amt, mprice, rest) in candidates
                if oid
            ]
        else:
            candidate_pairs = [
                (fill.maker_order_id, fill.asset_id, fill.size, fill.price, 0.0)
            ]

        # Pick the first candidate that lives in our OrderManager.
        # om.by_server_id does a full-hash dict lookup — no prefix matching.
        # H-NEW-2026-07-11 (audit, prod 14:03 round): the top-level
        # `fill.price` and `fill.size` describe the TAKER side of the trade
        # (the counterparty who crossed the book). When our maker is one of
        # many makers that the taker matched, the top-level price/size is
        # NOT necessarily our fill price / our share count — the per-row
        # values in maker_orders[] are. We pick the candidate row that is
        # ours and use ITS (asset_id, size, price) for downstream routing.
        chosen: tuple[str, str, float, float, float] | None = None
        for cand in candidate_pairs:
            if om.by_server_id(cand[0]):
                chosen = cand
                break

        if chosen is None:
            # None of the order_ids in the trade are ours — it's purely a
            # counterparty trade (e.g. they traded against themselves) or
            # the user_ws attribution failed. Either way, no position
            # delta for us.
            log.info(
                "router.maker_fill.no_match",
                candidates=[oid[:16] for oid, _, _, _, _ in candidate_pairs],
                reason="none of the candidate order_ids match OrderManager",
            )
            return

        order_id = chosen[0]
        asset_id = chosen[1] or fill.asset_id

        # Resolve the tracked order BEFORE the price fallback reads
        # `tracked.price` (the previous ordering raised UnboundLocalError
        # whenever the maker row carried no price).
        tracked = om.by_server_id(order_id)
        if not tracked:
            # Defensive: chosen matched in the loop above but disappeared
            # before apply_fill_event runs (e.g. another callback purged
            # it). Keep the existing INFO breadcrumb.
            log.info(
                "router.maker_fill.untracked",
                order_id=order_id, asset_id=asset_id[:16],
                reason="no TrackedOrder matches server_order_id (race?)",
            )
            return

        # Price fallback chain for OUR maker's matched price:
        # 1. chosen[3]  — maker_orders[].price  (the row's own price)
        # 2. tracked.price — our limit order's placed price (always
        #                    present, since this order_id is in OM)
        # 3. fill.price  — top-level taker-side price (last resort; can
        #                  be on the wrong token side)
        fill_price = chosen[3] or tracked.price or fill.price

        # When both the WS maker row and the REST fallback are empty we
        # DO NOT fall back to fill.size — it is the taker side and
        # overcounts by (taker_total / our_maker).
        if not order_id or (chosen[2] <= 0 and chosen[4] <= 0):
            log.info(
                "router.maker_fill.skip",
                order_id=order_id, maker_amount=chosen[2],
                rest_filled_size=chosen[4],
                reason="authoritative per-maker amount is 0 (no WS amount, "
                       "no REST amount) — refusing to use taker-side "
                       "fill.size which would overcount",
            )
            return

        # Decide what *increment* to forward to the engine.
        #
        # chosen[2] semantics:
        #   * WS path       — maker_orders[].matched_amount for THIS trade,
        #                     i.e. a per-trade contribution → ADD it.
        #   * REST fallback — the order's CUMULATIVE size_matched; in that
        #                     case chosen[4] carries the per-trade delta
        #                     and takes precedence below.
        # The previous code subtracted `prev_cumulative` in both cases,
        # which silently dropped every partial fill after the first one
        # on a multi-trade GTD.
        #
        # ORDERING (2026-10-31 review): the WS ORDER-UPDATE and the WS
        # TRADE report the SAME fill through different channels. Adding
        # this trade's `inc` to OrderManager's UPDATE-mirrored cumulative
        # double-books whenever the UPDATE arrived first — e.g. UPDATE
        # mirrors 4 → prev=4, the TRADE then adds its 4 again → phantom
        # 8 shares, mark_filled promotes a half-filled order, and the
        # genuine second fill is capped away forever. Instead, keep our
        # OWN running total of the trade volumes we have seen and advance
        # the order to `max(OrderManager's cumulative, trade total)`.
        # That is order-independent; a trade the WS missed simply leaves
        # the max at OrderManager's (polling-derived) value.
        prev_cumulative = tracked.size_matched or 0.0
        rest_delta = chosen[4]
        prev_trade_total = _trade_totals.get(order_id, 0.0)
        if rest_delta > 0:
            # REST fallback candidate shape: (oid, asset, CUMULATIVE, price,
            # delta). `chosen[2]` is the order's cumulative size_matched and
            # `rest_delta` was computed against user_ws's OWN baseline — NOT
            # against OrderManager's cumulative. Adding the delta to our
            # cumulative double-counts whenever another feed already advanced
            # it. Production 2026-10-30: the TierWatcher polling resync moved
            # T2 3.34 → 9.36, then the trade event's REST delta (6.02, based
            # on user_ws's stale 3.34 baseline) was added again → 15.38; the
            # position read 20.38 while the wallet held 14.36 and the TP SELL
            # was rejected by the CLOB ("balance: 14360000, order amount:
            # 20380000"). Treat the REST value as the authoritative absolute
            # cumulative and never move backwards.
            trade_total = max(prev_trade_total, float(chosen[2]))
            new_cumulative = max(prev_cumulative, trade_total)
        else:
            inc = max(0.0, float(chosen[2]))
            trade_total = prev_trade_total + inc
            new_cumulative = max(prev_cumulative, trade_total)
            # Cap the cumulative at the order's expected size. This absorbs
            # duplicate MATCHED→MINED→CONFIRMED re-fires when the WS payload
            # lacks a trade_id.
            expected_total = (
                tracked.placed_size if tracked.placed_size > 0 else tracked.size
            )
            if expected_total > 0:
                new_cumulative = min(new_cumulative, expected_total)
        _trade_totals[order_id] = trade_total
        if len(_trade_totals) > _TRADE_TOTALS_LIMIT:
            _trade_totals.clear()
        fill_size_inc = new_cumulative - prev_cumulative
        if fill_size_inc <= 1e-9:
            # The cumulative was already booked (typically by the WS
            # ORDER UPDATE, which knows neither our role nor the actual
            # execution price). THIS event is the authoritative source
            # for both — backfill the fill row's terms (and taker fee)
            # before dropping the duplicate.
            om.reconcile_fill_terms(
                order_id,
                cumulative=prev_cumulative,
                # Only the row's OWN price counts. `fill_price` may have
                # fallen back to tracked.price (the LIMIT) or to the
                # top-level taker-side price; either would clobber a
                # real execution price that is already on the row.
                fill_price=fill_price if chosen[3] > 0 else None,
                trader_side=getattr(fill, "trader_side", None),
            )
            # The UPDATE mirror advanced OrderManager, so the usual
            # post-apply promotion is skipped. Promote here when the
            # cumulative really is complete (mark_filled re-checks it).
            om.mark_filled(order_id)
            log.debug(
                "router.maker_fill.no_new",
                order_id=order_id, prev=prev_cumulative,
                maker_amount=chosen[2], rest_delta=rest_delta,
            )
            return

        om.apply_fill_event(
            order_id,
            size_matched=new_cumulative,
            fill_price=fill_price,
            # MAKER/TAKER hint from the WS TRADE event, stored for the
            # dashboard execution-quality stats (Unknown for REST resyncs).
            trader_side=getattr(fill, "trader_side", None),
        )
        om.mark_filled(order_id)

        # Route to the engine that owns this order's asset. Engine treats
        # `fill_size` as the per-event delta after the B-3 deduper / Lock
        # refactor (see engine.on_order_filled docstring). `fill_size_inc`
        # was derived above from either the REST delta or the WS
        # maker-row amount (minus the previous cumulative).
        engines = getattr(state, "engines", None)
        if engines:
            eng = engines.get(str(tracked.asset).upper())
        else:
            # Legacy single-asset test stubs only expose state.engine.
            eng = getattr(state, "engine", None)
        if eng is None:
            log.error(
                "router.maker_fill.no_engine",
                asset=tracked.asset,
                order_id=order_id[:16],
                note="fill mirrored into OrderManager but no engine owns "
                     "this asset — position state was not advanced",
            )
            return
        eng.on_order_filled(asset_id, order_id, fill_price, fill_size_inc)

        log.info(
            "router.maker_fill",
            asset=tracked.asset,
            maker_order_id=order_id,    # full id — never truncate
            asset_id=asset_id[:16],
            chosen_maker_amount=chosen[2],
            chosen_maker_price=chosen[3],
            fill_price=fill_price,
            fill_size=fill_size_inc,
            fill_total_size=new_cumulative,
            trader_side=getattr(fill, "trader_side", None),
            status=fill.status,
        )

    return on_maker_fill


def _make_user_ws_order_callback(state: AppState):
    """
    Route CLOB order events to engine.

    Handles PLACEMENT (bind server order id), UPDATE (size_matched),
    and CANCELLATION events from the UserWS order channel.
    """
    def on_order(ev) -> None:  # ev: OrderEvent from user_ws.py
        # H-NEW-2026-07-11-3 (audit, prod 14:30 round): WSS User Channel
        # pushes order.UPDATE events with the AUTHORITATIVE cumulative
        # `size_matched` straight from the CLOB — this is a stronger
        # signal than waiting for a TRADE event and reverse-engineering
        # our per-maker share count from `maker_orders[].amount`. Driving
        # engine.on_order_filled from here (instead of only from the
        # TRADE path) eliminates the 1.5s TierWatcher polling lag and
        # closes the cross-tier mis-trigger window (T1 actually fills at
        # 14:31:15.620 but TierWatcher only learns at 14:31:16+ via REST
        # — T2 was already placed at 14:31:14).
        om = state.order_manager
        if not om:
            return

        order_id = ev.order_id
        evt_type = ev.event_type.upper()

        if evt_type == "CANCELLATION":
            # A cancellation can race the final fill event. Before closing
            # a TP snapshot, book any matched size OrderManager already
            # knows about but the engine has not credited — otherwise
            # close_tp_order() would drop that residual from the position
            # forever.
            tracked = om.by_server_id(order_id)
            eng = state.engines.get(
                str(tracked.asset).upper()
            ) if tracked else None
            pos = (
                eng._round.position
                if eng is not None and eng._round
                else None
            )
            if (
                pos is not None
                and tracked is not None
                and hasattr(pos, "tp_size_sold")
            ):
                already = pos.tp_size_sold(order_id)
                if already is not None:
                    delta = max(0.0, tracked.size_matched - already)
                    if delta > 1e-9:
                        pos.record_tp_fill(order_id, tracked.price, delta)
                        log.info(
                            "router.tp_cancel.booked_residual",
                            order_id=order_id[:16],
                            delta_size=delta,
                            fill_price=tracked.price,
                            note="booked matched shares observed before the "
                                 "cancellation closed the TP snapshot",
                        )
            if eng is not None:
                eng.on_order_cancelled(order_id, reason="user_ws")
            else:
                log.warning(
                    "router.order_cancel.no_engine",
                    order_id=order_id[:16],
                    note="cancellation observed but no engine owns this "
                         "asset; engine tier state was not cleared",
                )
            return

        if evt_type == "UPDATE":
            tracked = om.by_server_id(order_id)
            if not tracked:
                # Not ours — could be a stale UPDATE for an order we
                # cancelled earlier. Silently drop.
                return
            # H-NEW-2026-07-11-3 (audit, prod 14:30 round, A7 review):
            # WS UPDATE carries the AUTHORITATIVE cumulative size_matched
            # straight from the CLOB, but we MUST NOT call
            # `eng.on_order_filled(...)` from here. Why: the SAME fill
            # also surfaces as a TRADE event carrying the per-trade
            # `maker_orders[].matched_amount`, and that path IS where
            # position totals / cost_filled get advanced. Both paths
            # running on the same fill would double-count
            # (size_filled += delta twice, _total_qty += delta twice,
            # cost_filled += delta * price twice).
            #
            # The TRADE event path is the AUTHORITATIVE source for fill
            # accounting (it carries per-trade deltas and is deduped by
            # trade_id via _seen_trades in `_make_maker_fill_callback`).
            # The UPDATE event's job here is:
            #   1. Mirror the cumulative size_matched into TrackedOrder
            #      so other code paths (TierWatcher, status queries) see
            #      an up-to-date view of where the order currently stands.
            #   2. Push the cumulative into the TierWatcher so it can
            #      short-circuit the REST poll loop on its next tick.
            # That is exactly what `receive_update` does, so we don't
            # need any new state — just call it.
            new_size_matched = ev.size_matched
            prev_size_matched = tracked.size_matched or 0.0
            if new_size_matched <= prev_size_matched + 1e-9:
                # WS re-fire or out-of-order; nothing new.
                return
            # No execution price / trader role here: the UPDATE predates
            # the trade data, so the row is booked at the LIMIT with
            # fee=0 and completed later by the TRADE / watcher reconcile
            # (`reconcile_fill_terms`). Passing the limit as a price
            # would make that backfill treat it as authoritative.
            om.apply_fill_event(
                order_id,
                size_matched=new_size_matched,
                fill_price=None,
            )
            # H-NEW-2026-07-11-3: push cumulative into the watcher.
            # TierWatcher.receive_update() is monotonic — out-of-order
            # frames are dropped — and it wakes the watch loop early so
            # the residual GTD gets cancelled within ~1ms of the fill
            # (instead of waiting up to `_poll_s` seconds for the next
            # REST balance read).
            getter = getattr(state, "watcher_for_order", None)
            if callable(getter):
                watcher = getter(order_id)
            else:
                router = getattr(state, "router", None)
                watcher = (
                    router.watcher_for_server_order(order_id)
                    if router is not None else None
                )
            if watcher is not None:
                watcher.receive_update(new_size_matched)
            log.info(
                "router.order_update.mirror_only",
                order_id=order_id,        # full hash
                asset=tracked.asset,
                token=tracked.token_id[:16],
                tier=tracked.tier_name,
                size_matched=new_size_matched,
                prev_size_matched=prev_size_matched,
                note="WS UPDATE only mirrors cumulative to OM + watcher; "
                     "TRADE event path is the authoritative source for "
                     "position totals (avoid double-count).",
            )
            return

    return on_order


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
