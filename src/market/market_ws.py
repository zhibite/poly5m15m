"""Polymarket CLOB Market WebSocket feed.

Subscribes to:  wss://ws-subscriptions-clob.polymarket.com/ws/market

Provides:
  - PriceCache: last_trade_price / midpoint per token_id
  - Tick size change notifications

H-NEW-2026-07-11: orderbook parsing (best_bid / best_ask / book events) has
been removed. The strategy now trades off REST midpoint + WS last_trade_price
only — see StrategyEngine / ActionRouter docs.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable

import websockets
# `websockets.State` is only exported at the top level from v14 onward;
# `websockets.protocol.State` is stable across 11–15.
from websockets.protocol import State as WsState

from src.infra.logging import get_logger

log = get_logger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class PriceCache:
    """Per-token price snapshot, updated by MarketWS and REST polling."""

    last_trade_price: dict[str, float] = field(default_factory=dict)
    tick_size: dict[str, float] = field(default_factory=dict)
    midpoint: dict[str, float] = field(default_factory=dict)  # REST /midpoint poller
    # Wall-clock timestamps for the two price sources. Settlement at the
    # window boundary needs the freshest UP/DOWN price, not a value from
    # an arbitrary earlier point in the round.
    last_trade_ts: dict[str, float] = field(default_factory=dict)
    midpoint_ts: dict[str, float] = field(default_factory=dict)

    def last(self, token: str) -> float:
        return self.last_trade_price.get(token, 0.0)

    def mid(self, token: str) -> float:
        return self.midpoint.get(token, 0.0)

    def get_midpoint(self, token: str) -> float:
        """REST midpoint price — more stable than last_trade_price for strategy decisions."""
        return self.midpoint.get(token, 0.0)

    def latest(self, token: str) -> tuple[float, float, str]:
        """Freshest known price for a token: `(price, timestamp, source)`.

        Prefers whichever of the WS last-trade price and the REST
        midpoint was updated most recently. Returns `(0, 0, "")` when
        neither source has produced a price.
        """
        candidates: list[tuple[float, float, str]] = []
        trade_px = self.last_trade_price.get(token, 0.0)
        trade_ts = self.last_trade_ts.get(token, 0.0)
        if trade_px > 0:
            candidates.append((trade_ts, trade_px, "last_trade"))
        mid_px = self.midpoint.get(token, 0.0)
        mid_ts = self.midpoint_ts.get(token, 0.0)
        if mid_px > 0:
            candidates.append((mid_ts, mid_px, "midpoint"))
        if not candidates:
            return 0.0, 0.0, ""
        ts, px, src = max(candidates, key=lambda row: row[0])
        return float(px), float(ts), src

    def clear_token(self, token: str) -> None:
        """Clear cached data for a specific token (e.g., on window change)."""
        self.last_trade_price.pop(token, None)
        self.tick_size.pop(token, None)
        self.midpoint.pop(token, None)
        self.last_trade_ts.pop(token, None)
        self.midpoint_ts.pop(token, None)


MarketWSConnected = Callable[[], None]
MarketWSDisconnected = Callable[[], None]


class MarketWS:
    """Async WebSocket consumer for CLOB market data."""

    def __init__(
        self,
        cache: PriceCache,
        on_connect: MarketWSConnected | None = None,
        on_disconnect: MarketWSDisconnected | None = None,
    ) -> None:
        self._cache = cache
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        # H-V6-10: dump the first N raw WS messages so we can see what
        # Polymarket is actually sending. Once we know the real shape, the
        # diagnostic can be turned off. The 10x log-flood cap is to keep
        # the firehose manageable in production.
        self._raw_dump_remaining: int = 5
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._url = MARKET_WS_URL
        self._pending_tokens: set[str] = set()
        self._ws: websockets.WebSocketClientProtocol | None = None
        # 2026-11-01 multi-market: tokens from expired windows cannot be
        # unsubscribed from an open Polymarket market channel. `set_active_tokens`
        # prunes them locally, filters their events, and forces one
        # reconnect after enough have accumulated so the upstream
        # subscription frame shrinks back to the active 28 tokens.
        self._pruned_since_connect = 0
        self._force_reconnect_pending = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe_tokens(self, token_ids: list[str]) -> None:
        """Queue token subscriptions and send immediately if WS is connected."""
        new_tokens = [tid for tid in token_ids if tid not in self._pending_tokens]
        for tid in new_tokens:
            self._pending_tokens.add(tid)
        if new_tokens and self._ws is not None and self._ws.state == WsState.OPEN:
            # WS is already connected — send subscription right away
            asyncio.create_task(self._subscribe(self._ws, new_tokens))

    def set_active_tokens(self, token_ids: set[str]) -> None:
        """Replace the active subscription set (multi-market window rotation).

        Tokens removed here stop updating the cache immediately. The
        upstream channel keeps streaming them until the next reconnect,
        so after `_RECONNECT_AFTER_PRUNED` removals we cycle the socket
        once to resubscribe with just the active set.
        """
        active = {t for t in token_ids if t}
        removed = self._pending_tokens - active
        self._pending_tokens = active
        self._pruned_since_connect += len(removed)
        if (
            removed
            and self._pruned_since_connect >= self._RECONNECT_AFTER_PRUNED
            and self._ws is not None
            and self._ws.state == WsState.OPEN
            and not self._force_reconnect_pending
        ):
            self._force_reconnect_pending = True
            asyncio.create_task(self._cycle_socket())

    @property
    def subscribed_tokens(self) -> frozenset[str]:
        return frozenset(self._pending_tokens)

    _RECONNECT_AFTER_PRUNED = 128

    async def _cycle_socket(self) -> None:
        """Close the current socket; `_run` reconnects with active tokens."""
        ws = self._ws
        try:
            if ws is not None:
                await ws.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._force_reconnect_pending = False
            self._pruned_since_connect = 0

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        log.info("market_ws.started", url=self._url)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("market_ws.stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        delay = 1.0
        max_delay = 30.0

        while self._running:
            try:
                async with websockets.connect(self._url, ping_interval=10) as ws:
                    self._ws = ws
                    delay = 1.0
                    log.debug("market_ws.connected")

                    if self._pending_tokens:
                        await self._subscribe(ws, list(self._pending_tokens))

                    if self._on_connect:
                        asyncio.create_task(self._on_connect())

                    async for raw in ws:
                        if not self._running:
                            break
                        await self._process(raw)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("market_ws.error", error=str(exc), retry_in_s=delay)
                if self._on_disconnect:
                    self._on_disconnect()
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)
            finally:
                self._ws = None

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol, tokens: list[str]) -> None:
        payload = {
            "type": "market",
            "assets_ids": tokens,
            "custom_feature_enabled": True,
        }
        await ws.send(json.dumps(payload))
        log.info("market_ws.subscribed", tokens=[t[:16] for t in tokens])

    def _set_last_trade(self, token: str, price: object) -> None:
        if not token:
            return
        try:
            px = float(price)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        self._cache.last_trade_price[token] = px
        self._cache.last_trade_ts[token] = time.time()

    async def _process(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Skip non-dict messages (some WS messages may be arrays)
        if not isinstance(msg, dict):
            return

        # H-V6-10 diagnostic: dump the first few raw messages so we can
        # confirm the real Polymarket wire shape. Production symptom
        # 2026-07-11 01:30 round 01:30 — STATUS shows bids=0.0000 for
        # every token every tick, no `best_bid_ask` event was ever
        # parsed; we need the raw JSON to verify what to feed the parser.
        if self._raw_dump_remaining > 0:
            self._raw_dump_remaining -= 1
            log.info(
                "market_ws.raw_sample",
                keys=sorted(msg.keys()),
                asset_id=msg.get("asset_id"),
                event_type=msg.get("event_type"),
                payload=str(msg)[:400],
            )

        # ── Polymarket Market Channel payload schema (relevant subset) ────────
        #   price_change:          { event_type, market,
        #                            price_changes: [{ asset_id, price, side,
        #                            size, ... }], timestamp }
        #   last_trade_price:      { event_type, asset_id, price, side, size,
        #                            fee_rate_bps, timestamp }
        #
        # orderbook events (best_bid_ask / book / bba dict) are NOT parsed —
        # the strategy trades off REST midpoint + WS last_trade_price only.
        event_type = msg.get("event_type") or msg.get("type") or ""

        # Extract token id — last_trade_price uses top-level `asset_id`;
        # price_change puts it inside each price_changes[].
        token = (
            msg.get("asset_id")
            or msg.get("token")
            or ""
        )
        market_field = msg.get("market")
        if not token and isinstance(market_field, dict):
            token = market_field.get("asset_id") or market_field.get("assetID") or ""

        # ── dedicated last_trade_price event ─────────────────────────────────
        # Schema: { event_type: "last_trade_price", asset_id, price, ... }
        # The price is a TOP-LEVEL `price` field. The old parser only looked
        # for a `last_trade_price` field, which this event never carries, so
        # every last-trade event was dropped.
        if event_type == "last_trade_price":
            price_raw = msg.get("price")
            if (
                price_raw not in (None, "")
                and token
                and (not self._pending_tokens or token in self._pending_tokens)
            ):
                self._set_last_trade(token, price_raw)

        # ── best_bid / best_ask ──────────────────────────────────────────────
        # Orderbook (best_bid / best_ask / book) events are intentionally
        # NOT parsed here. The strategy trades off REST midpoint +
        # WS last_trade_price only — see StrategyEngine / ActionRouter.

        # price_change → per-asset updates (each carries its own best_bid/ask)
        pcs = msg.get("price_changes", [])
        if isinstance(pcs, list):
            for pc in pcs:
                if not isinstance(pc, dict):
                    continue
                pc_token = pc.get("asset_id") or token
                if not pc_token:
                    continue
                if self._pending_tokens and pc_token not in self._pending_tokens:
                    continue
                # Bump last_trade_price from price_changes too (these are
                # trade executions, the price is the actual fill).
                pc_price = pc.get("price")
                if pc_price is not None and pc_price != "":
                    self._set_last_trade(pc_token, pc_price)


        # last_trade_price (top-level field on dedicated event_type)
        if self._pending_tokens and token and token not in self._pending_tokens:
            return
        ltp = msg.get("last_trade_price")
        if isinstance(ltp, list) and len(ltp) >= 1:
            self._set_last_trade(token, ltp[0])
        elif isinstance(ltp, dict):
            price = ltp.get("price") or ltp.get("last_trade_price")
            if price not in (None, ""):
                self._set_last_trade(token, price)
        elif isinstance(ltp, str) and ltp and token:
            # top-level last_trade_price event: { event_type, asset_id,
            # price, side, size, fee_rate_bps, timestamp }
            self._set_last_trade(token, ltp)
