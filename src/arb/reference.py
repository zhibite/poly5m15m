"""Reference-price strikes for 5m/15m UP/DOWN windows.

A window's strike ("beat price") is the reference price at the window's
start. The market description is parsed first when it carries an explicit
number; otherwise this service reads the open of the Binance 1-second
kline at the exact window start (falling back to the 1-minute open).

Caveat: Polymarket may settle these markets on a different feed
(Chainlink). The detector therefore requires a minimum strike gap before
trading a cross-market pair; the gap absorbs small feed differences.
"""
from __future__ import annotations

import time

import httpx

from src.arb.config import ArbSettings
from src.infra.logging import get_logger

log = get_logger(__name__)

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"

_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT",
    "XRP": "XRPUSDT",
    "SOL": "SOLUSDT",
    "DOGE": "DOGEUSDT",
    "HYPE": "HYPEUSDT",
}


def symbol_for(asset: str) -> str:
    return _SYMBOLS.get(asset.upper(), f"{asset.upper()}USDT")


class ReferencePriceService:
    """Cached Binance open/spot prices for assets and window starts."""

    def __init__(self, cfg: ArbSettings) -> None:
        self._cfg = cfg
        self._http: httpx.AsyncClient | None = None
        self._open_cache: dict[tuple[str, int], float] = {}
        self._spot_cache: dict[str, tuple[float, float]] = {}

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(6.0, connect=3.0)
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def _klines(
        self, symbol: str, interval: str, start_ms: int, end_ms: int, limit: int
    ) -> list[list]:
        try:
            client = await self._client()
            resp = await client.get(
                BINANCE_KLINES,
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": limit,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "arb.ref.klines_failed",
                symbol=symbol,
                interval=interval,
                start_ms=start_ms,
                error=str(exc),
            )
            return []

    async def open_at(self, asset: str, ts: int) -> float | None:
        """Reference price at the exact second ``ts`` (UTC epoch seconds)."""
        key = (asset.upper(), int(ts))
        if key in self._open_cache:
            return self._open_cache[key]

        symbol = symbol_for(asset)
        start_ms = int(ts) * 1000
        # 1s klines give the exact window open; fall back to the 1m open.
        for interval, span_ms, limit in (
            ("1s", 3_000, 3),
            ("1m", 120_000, 2),
        ):
            rows = await self._klines(
                symbol, interval, start_ms, start_ms + span_ms, limit
            )
            for row in rows:
                try:
                    open_ms = int(row[0])
                    if open_ms != start_ms:
                        continue
                    price = float(row[1])
                except (IndexError, TypeError, ValueError):
                    continue
                if price > 0:
                    self._open_cache[key] = price
                    return price
        log.warning(
            "arb.ref.open_missing",
            asset=asset,
            ts=ts,
            note="no Binance kline open at this window start",
        )
        return None

    async def price_now(self, asset: str) -> float | None:
        """Current spot with a 1-second in-process cache."""
        asset = asset.upper()
        now = time.time()
        cached = self._spot_cache.get(asset)
        if cached and now - cached[1] < 1.0:
            return cached[0]
        try:
            client = await self._client()
            resp = await client.get(
                BINANCE_TICKER, params={"symbol": symbol_for(asset)}
            )
            resp.raise_for_status()
            price = float((resp.json() or {}).get("price", 0.0))
            if price > 0:
                self._spot_cache[asset] = (price, now)
                return price
        except Exception as exc:  # noqa: BLE001
            log.debug("arb.ref.spot_failed", asset=asset, error=str(exc))
        return cached[0] if cached else None
