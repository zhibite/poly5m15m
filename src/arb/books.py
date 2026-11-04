"""Order-book access for the arbitrage engine.

Two sources are used deliberately:

  * the CLOB market WebSocket (via ``PriceCache``) for continuous,
    low-latency best-bid/ask monitoring — this is only a *trigger*;
  * the REST ``/orderbook`` endpoint for a fresh, depth-aware snapshot
    immediately before an order is sized or sent.

A quote that is older than ``MAX_QUOTE_AGE_S`` is never traded. Crossing
or empty books are rejected by the detector.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from src.arb.config import ArbSettings
from src.arb.models import Book, Level
from src.infra.logging import get_logger
from src.market.market_ws import MarketWS, PriceCache
from src.market.pm_client import PolymarketClient

log = get_logger(__name__)


@dataclass(frozen=True)
class BBO:
    bid: float | None
    ask: float | None
    ts: float
    bid_ts: float = 0.0
    ask_ts: float = 0.0

    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.ts)

    def ask_age(self, now: float | None = None) -> float:
        """Freshness of the ask side specifically (the pre-screen's input)."""
        if self.ask_ts <= 0:
            return float("inf")
        return max(0.0, (time.time() if now is None else now) - self.ask_ts)

    def bid_age(self, now: float | None = None) -> float:
        if self.bid_ts <= 0:
            return float("inf")
        return max(0.0, (time.time() if now is None else now) - self.bid_ts)


class BookService:
    def __init__(
        self,
        pm: PolymarketClient,
        cfg: ArbSettings,
        cache: PriceCache | None = None,
        market_ws: MarketWS | None = None,
    ) -> None:
        self._pm = pm
        self._cfg = cfg
        self._cache = cache
        self._ws = market_ws
        self._rest_cache: dict[str, tuple[float, Book]] = {}
        self._subscribed: set[str] = set()

    # ── subscriptions ───────────────────────────────────────────────────
    def subscribe(self, tokens: set[str]) -> None:
        active = {t for t in tokens if t}
        removed = self._subscribed - active
        self._subscribed = active
        if self._ws is not None:
            try:
                self._ws.set_active_tokens(active)
            except Exception as exc:  # noqa: BLE001
                log.warning("arb.books.subscribe_failed", error=str(exc))
        if removed:
            self.clear_tokens(removed)

    def clear_tokens(self, tokens: set[str]) -> None:
        """Drop expired tokens from both the WS cache and the REST cache."""
        for token in tokens:
            self._rest_cache.pop(token, None)
            if self._cache is not None:
                self._cache.clear_token(token)

    @property
    def ws_connected(self) -> bool:
        return bool(self._ws is not None and getattr(self._ws, "connected", False))

    async def start(self) -> None:
        if self._ws is not None:
            await self._ws.start()

    async def stop(self) -> None:
        if self._ws is not None:
            await self._ws.stop()

    # ── cheap monitor ───────────────────────────────────────────────────
    def bbo(self, tokens: list[str]) -> dict[str, BBO]:
        out: dict[str, BBO] = {}
        if self._cache is None:
            return out
        for token in tokens:
            out[token] = BBO(
                bid=self._cache.best_bid.get(token),
                ask=self._cache.best_ask.get(token),
                ts=float(self._cache.bbo_ts.get(token, 0.0)),
                bid_ts=float(self._cache.bid_ts.get(token, 0.0)),
                ask_ts=float(self._cache.ask_ts.get(token, 0.0)),
            )
        return out

    def cache_age(self, token: str) -> float:
        if self._cache is None:
            return float("inf")
        ts = float(self._cache.bbo_ts.get(token, 0.0))
        return float("inf") if ts <= 0 else max(0.0, time.time() - ts)

    # ── REST snapshots ──────────────────────────────────────────────────
    async def fetch_books(
        self, tokens: list[str], *, max_age: float = 0.0
    ) -> dict[str, Book]:
        """Fetch books concurrently, reusing entries newer than ``max_age``."""
        now = time.time()
        out: dict[str, Book] = {}
        need: list[str] = []
        for token in tokens:
            if not token:
                continue
            hit = self._rest_cache.get(token)
            if hit is not None and max_age > 0 and now - hit[0] <= max_age:
                out[token] = hit[1]
            else:
                need.append(token)
        if need:
            results = await asyncio.gather(
                *(self._fetch_one(token) for token in need),
                return_exceptions=True,
            )
            for token, result in zip(need, results):
                if isinstance(result, Book):
                    out[token] = result
                elif isinstance(result, Exception):
                    log.debug(
                        "arb.books.fetch_failed", token=token[:16], error=str(result)
                    )
        return out

    async def _fetch_one(self, token: str) -> Book | None:
        raw = await self._pm.get_order_book(token)
        if not isinstance(raw, dict):
            return None
        bids = _parse_levels(
            raw.get("bids") or raw.get("buys") or [], reverse=True,
            limit=self._cfg.MAX_BOOK_LEVELS,
        )
        asks = _parse_levels(
            raw.get("asks") or raw.get("sells") or [], reverse=False,
            limit=self._cfg.MAX_BOOK_LEVELS,
        )
        book = Book(
            token_id=token,
            bids=bids,
            asks=asks,
            ts=time.time(),
            source="rest",
        )
        self._rest_cache[token] = (book.ts, book)
        return book


def _parse_levels(rows: object, *, reverse: bool, limit: int) -> list[Level]:
    if not isinstance(rows, list):
        return []
    levels: list[Level] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("price", 0) or 0)
            size = float(row.get("size", 0) or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or price >= 1 or size <= 0:
            continue
        levels.append(Level(price=price, size=size))
    # Best price first: asks ascending, bids descending.
    levels.sort(key=lambda lv: lv.price, reverse=reverse)
    return levels[: max(1, int(limit))]
