"""Market discovery and 5m/15m pairing.

Only one out of every three 5-minute windows shares its end timestamp
with a 15-minute window: the one that starts at ``end_15 - 300``. The
pairing is computed from exact timestamps, not from the current clock, so
a late process start cannot silently pair the wrong windows.

The module also parses the resolution source and, when present, an
explicit strike from the market description. If no strike is published,
the reference-price service supplies the window-open price.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from src.config import ArbSettings
from src.models import ArbMarket, ArbPair
from src.reference import ReferencePriceService
from src.store import ArbStore
from src.infra.logging import get_logger
from src.models import Side
from src.market.pm_client import PolymarketClient

log = get_logger(__name__)

INTERVAL_SEC = {"5m": 300, "15m": 900}

_SOURCE_PATTERNS = (
    ("chainlink", "chainlink"),
    ("binance", "binance"),
    ("coinbase", "coinbase"),
    ("kraken", "kraken"),
    ("pyth", "pyth"),
)


def window_starts(now: float) -> tuple[int, int, int]:
    """(current 5m start, current 15m start, next 15m end)."""
    now = float(now)
    return (
        int(now // 300) * 300,
        int(now // 900) * 900,
        (int(now // 900) + 1) * 900,
    )


def parse_resolution_source(text: str) -> str:
    low = (text or "").lower()
    for needle, label in _SOURCE_PATTERNS:
        if needle in low:
            return label
    return "unknown"


_NUM_RE = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)(?![\d.])")
_STRIKE_HINT_RE = re.compile(
    r"(above|below|over|under|strike|beat|opening\s+price|open\s+price|"
    r"greater than|less than|at or above|at or below)",
    re.IGNORECASE,
)


def parse_strike(text: str, ref_price: float | None = None) -> float | None:
    """Best-effort extraction of an explicit strike from market text.

    Up/Down windows usually describe the strike as "the opening price at
    the start of the period" without a number, in which case this returns
    None and the reference service is used. When numbers are present, the
    one closest to the current reference price wins, so unrelated numbers
    (volume, dates, fees) are ignored.
    """
    text = text or ""
    if not text:
        return None
    candidates: list[float] = []
    # Prefer numbers that sit next to a strike hint.
    def _plausible_number(source: str, match: re.Match) -> bool:
        prefix = source[max(0, match.start() - 2) : match.start()]
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            return False
        # Crypto strikes are either >= 100 (BTC/ETH) or explicitly
        # money-formatted (DOGE-style "$0.20"). This rejects the dates and
        # clock times that appear in every up/down question ("June 10",
        # "7:20PM") without needing the reference price.
        return value >= 100 or "$" in prefix

    for match in _STRIKE_HINT_RE.finditer(text):
        tail = text[match.end() : match.end() + 80]
        for num in _NUM_RE.finditer(tail):
            if _plausible_number(tail, num):
                candidates.append(float(num.group(1).replace(",", "")))
                break
    if not candidates:
        for num in _NUM_RE.finditer(text):
            if _plausible_number(text, num):
                candidates.append(float(num.group(1).replace(",", "")))
    if not candidates:
        return None
    if ref_price and ref_price > 0:
        # Keep only plausible same-asset prices, then take the closest.
        plausible = [
            c for c in candidates if abs(c - ref_price) / ref_price <= 0.20
        ]
        if plausible:
            return min(plausible, key=lambda c: abs(c - ref_price))
        return None
    return candidates[0]


def sources_compatible(
    source_5m: str, source_15m: str, cfg: ArbSettings
) -> bool:
    """True when both legs may share a settlement price.

    A known mismatch is always rejected. An unknown source is rejected in
    live mode unless the operator explicitly allows it.
    """
    a = (source_5m or "unknown").lower()
    b = (source_15m or "unknown").lower()
    if a != "unknown" and b != "unknown":
        return a == b
    if cfg.PAPER_TRADING or cfg.ALLOW_UNKNOWN_RESOLUTION_SOURCE:
        return True
    return not cfg.REQUIRE_SAME_RESOLUTION_SOURCE


class ArbDiscovery:
    def __init__(
        self,
        pm: PolymarketClient,
        ref: ReferencePriceService,
        store: ArbStore,
        cfg: ArbSettings,
    ) -> None:
        self._pm = pm
        self._ref = ref
        self._store = store
        self._cfg = cfg
        self._cache: dict[tuple[str, str, int], tuple[float, ArbMarket | None]] = {}
        self._token_meta: dict[str, tuple[float, float, bool]] = {}

    # ── public API ──────────────────────────────────────────────────────
    async def market(
        self,
        asset: str,
        interval: str,
        start_ts: int,
        *,
        force: bool = False,
    ) -> ArbMarket | None:
        key = (asset.upper(), interval, int(start_ts))
        now = time.time()
        cached = self._cache.get(key)
        if cached is not None and not force:
            # A negative result (market not listed yet) must be retried on
            # the next discovery cycle, not cached for a full minute.
            ttl = (
                self._cfg.MARKET_META_REFRESH_S
                if cached[1] is not None
                else max(2.0, self._cfg.DISCOVERY_S)
            )
            if now - cached[0] < ttl:
                return cached[1]

        sec = INTERVAL_SEC[interval]
        try:
            rows = await self._pm.get_asset_updown_markets(
                asset,
                slug_interval=interval,
                window_sec=sec,
                limit=1,
                start_ts=int(start_ts),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "arb.discovery.market_failed",
                asset=asset,
                interval=interval,
                start_ts=start_ts,
                error=str(exc),
            )
            rows = []
        market = None
        if rows:
            try:
                market = await self._to_market(asset, interval, start_ts, sec, rows[0])
                self._store.upsert_market(market)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "arb.discovery.parse_failed",
                    asset=asset,
                    interval=interval,
                    start_ts=start_ts,
                    error=str(exc),
                )
                market = None
        self._cache[key] = (now, market)
        return market

    async def current_markets(self, asset: str, now: float | None = None) -> list[ArbMarket]:
        """The currently active 5m and 15m markets (for same-market arb)."""
        now = time.time() if now is None else now
        start5, start15, _ = window_starts(now)
        out: list[ArbMarket] = []
        for interval, start in (("5m", start5), ("15m", start15)):
            market = await self.market(asset, interval, start)
            if market is not None:
                out.append(market)
        return out

    async def pair(self, asset: str, end_ts: int, now: float | None = None) -> ArbPair | None:
        """Discover and evaluate the 5m/15m pair that ends at ``end_ts``."""
        now = time.time() if now is None else now
        end_ts = int(end_ts)
        start15 = end_ts - 900
        start5 = end_ts - 300

        m15 = await self.market(asset, "15m", start15)
        if m15 is None:
            return None
        # The paired 5m market may not be listed until shortly before its
        # own open. Do not treat that as an error; the loop retries.
        if now < start5 - 30:
            return None
        m5 = await self.market(asset, "5m", start5)
        if m5 is None:
            return None

        strike15 = await self._strike_for(m15, start15)
        strike5 = await self._strike_for(m5, start5)

        resolution_ok = sources_compatible(
            m15.resolution_source, m5.resolution_source, self._cfg
        )
        gap_bps = None
        direction5 = direction15 = None
        if strike5 and strike15 and strike15 > 0:
            gap_bps = abs(strike5 - strike15) / strike15 * 10_000.0
            # Lower strike buys UP, higher strike buys DOWN. With this
            # orientation the pair pays $1 in every settlement state and
            # $2 when S lands between the strikes.
            if strike5 < strike15:
                direction5, direction15 = Side.UP, Side.DOWN
            else:
                direction5, direction15 = Side.DOWN, Side.UP

        end_time_ok = abs(int(m5.end_ts) - int(m15.end_ts)) <= 2
        reason = ""
        if not end_time_ok:
            # The payoff floor requires the exact same settlement print.
            # A multi-second skew means the two markets may resolve on
            # different prints even if the slugs look paired.
            reason = "end_time_mismatch"
        elif strike5 is None or strike15 is None:
            reason = "strike_missing"
        elif not resolution_ok:
            reason = "resolution_source_mismatch"
        elif gap_bps is not None and gap_bps < self._cfg.MIN_STRIKE_GAP_BPS:
            reason = "strike_gap_too_small"
        elif now < start5 + self._cfg.STRIKE_SETTLE_S:
            reason = "strike_settling"
        elif now > end_ts - self._cfg.STOP_OPEN_BEFORE_END_S:
            reason = "too_late"

        pair = ArbPair(
            asset=asset.upper(),
            end_ts=end_ts,
            m5=m5,
            m15=m15,
            strike_5m=strike5,
            strike_15m=strike15,
            direction_5m=direction5,
            direction_15m=direction15,
            gap_bps=gap_bps,
            resolution_ok=resolution_ok,
            end_time_ok=end_time_ok,
            reason=reason,
        )
        self._store.upsert_pair(pair)
        return pair

    # ── internals ───────────────────────────────────────────────────────
    async def _strike_for(self, market: ArbMarket, start_ts: int) -> float | None:
        source_mode = (self._cfg.STRIKE_SOURCE or "auto").lower()
        # The window-open reference is both the fallback strike and the
        # sanity anchor for a number parsed out of the description.
        open_ref = await self._ref.open_at(market.asset, start_ts)
        if market.strike is not None and source_mode in ("description", "auto"):
            anchor = open_ref
            if anchor is None or anchor <= 0:
                anchor = await self._ref.price_now(market.asset)
            if anchor is None or anchor <= 0:
                return market.strike
            if abs(market.strike - anchor) / anchor <= 0.05:
                market.strike_source = "description"
                self._store.upsert_market(market)
                return market.strike
            log.warning(
                "arb.discovery.strike_outlier",
                asset=market.asset,
                interval=market.interval,
                strike=market.strike,
                ref=anchor,
                note="description strike ignored; using window-open price",
            )
        if source_mode in ("binance", "auto"):
            if open_ref and open_ref > 0:
                market.strike = open_ref
                market.strike_source = "binance_open"
                self._store.upsert_market(market)
                return open_ref
        # "description" is strict: when the text has no usable strike, do
        # not silently substitute the Binance open.
        return market.strike

    async def _to_market(
        self,
        asset: str,
        interval: str,
        start_ts: int,
        window_sec: int,
        raw: dict,
    ) -> ArbMarket:
        tokens = raw.get("tokens") or []
        token_up = token_down = ""
        for token in tokens:
            label = str(token.get("outcome") or "").strip().lower()
            if label in ("up", "yes"):
                token_up = str(token.get("token_id") or "")
            elif label in ("down", "no"):
                token_down = str(token.get("token_id") or "")
        if not token_up or not token_down:
            raise ValueError(f"missing UP/DOWN token ids in {raw.get('_event_slug')}")

        text = " ".join(
            str(raw.get(key) or "")
            for key in ("question", "description", "title")
        ).strip()
        source = parse_resolution_source(text)
        tick_size, neg_risk = await self._token_meta_for(token_up, raw)

        end_ts = self._parse_end_ts(raw.get("endDate"), start_ts + window_sec)
        parsed_strike = parse_strike(text)
        return ArbMarket(
            asset=asset.upper(),
            interval=interval,
            slug=str(raw.get("_event_slug") or ""),
            condition_id=str(raw.get("conditionId") or ""),
            token_up=token_up,
            token_down=token_down,
            start_ts=int(start_ts),
            end_ts=int(end_ts),
            tick_size=tick_size,
            neg_risk=neg_risk,
            description=text,
            resolution_source=source,
            strike=parsed_strike,
            strike_source="description" if parsed_strike else "",
            raw=raw,
        )

    async def _token_meta_for(self, token_id: str, raw: dict) -> tuple[float, bool]:
        now = time.time()
        cached = self._token_meta.get(token_id)
        if cached and now - cached[0] < 300.0:
            return cached[1], cached[2]

        tick = 0.01
        try:
            tick = float(raw.get("tickSize") or 0.0) or 0.01
        except (TypeError, ValueError):
            tick = 0.01
        if tick <= 0:
            try:
                fetched = float(await self._pm.get_tick_size(token_id))
                if fetched > 0:
                    tick = fetched
            except Exception:  # noqa: BLE001
                pass

        neg_risk = raw.get("negRisk")
        if neg_risk is None:
            try:
                neg_risk = await self._pm.get_neg_risk(token_id)
            except Exception:  # noqa: BLE001
                neg_risk = False
        self._token_meta[token_id] = (now, tick, bool(neg_risk))
        return tick, bool(neg_risk)

    @staticmethod
    def _parse_end_ts(raw: object, fallback: int) -> int:
        if isinstance(raw, (int, float)) and raw > 0:
            value = float(raw)
            return int(value / 1000) if value > 10_000_000_000 else int(value)
        text = str(raw or "").strip()
        if text:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                pass
        return int(fallback)
