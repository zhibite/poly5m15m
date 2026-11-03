"""
pm_client.py — Polymarket Gamma + CLOB v2 public API client.
Handles market discovery, midpoint prices, fee rates, and resolution queries.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from src.infra.logging import get_logger

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - ancient runtime
    ZoneInfo = None  # type: ignore[assignment]

log = get_logger(__name__)

# Hourly Up/Down markets use an Eastern-Time dated slug, e.g.
#   bitcoin-up-or-down-november-3-2026-6am-et
# These are the aliases Polymarket has used for each asset. We probe every
# alias and keep the legacy `{asset}-updown-1h-<epoch>` form as a fallback.
_HOURLY_SLUG_NAMES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "btc"),
    "ETH": ("ethereum", "eth"),
    "BNB": ("bnb", "binance-coin", "binancecoin"),
    "XRP": ("xrp", "ripple"),
    "SOL": ("solana", "sol"),
    "DOGE": ("dogecoin", "doge"),
    "HYPE": ("hyperliquid", "hype"),
}

_ET: object
try:
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo unavailable")
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - no tzdata installed
    # Fixed EST is correct in winter but one hour off during EDT. The
    # `tzdata` package is a project dependency, so this is only a safety
    # net for minimal images without a tz database.
    _ET = timezone(timedelta(hours=-5))
    log.warning(
        "pm_client.tzdata_missing",
        note="America/New_York unavailable; falling back to fixed UTC-5. "
             "Install the tzdata package for correct DST handling.",
    )


def _et_hour_candidates(
    asset: str, now_ts: float, window_sec: int, hours: tuple[int, ...]
) -> list[tuple[str, int]]:
    """Build dated ET hourly slugs for the current/previous/next hour.

    Returns `(slug, window_start_ts)` pairs, newest-relevant first. The
    window start is the UTC epoch of the ET hour boundary, which is what
    `MarketService` uses to build `window_start`/`window_end`.
    """
    names = _HOURLY_SLUG_NAMES.get(asset.upper(), (asset.lower(),))
    et_now = datetime.fromtimestamp(now_ts, timezone.utc).astimezone(_ET)
    base_et = et_now.replace(minute=0, second=0, microsecond=0)
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for delta_h in hours:
        dt = base_et + timedelta(hours=delta_h)
        window_start_ts = int(dt.timestamp())
        month = dt.strftime("%B").lower()
        day = dt.day
        year = dt.year
        hour12 = dt.hour % 12 or 12
        ampm = "am" if dt.hour < 12 else "pm"
        # Polymarket shipped both with and without the year; probe both.
        for year_part in (f"-{year}", ""):
            for name in names:
                slug = (
                    f"{name}-up-or-down-{month}-{day}{year_part}"
                    f"-{hour12}{ampm}-et"
                )
                if slug in seen:
                    continue
                seen.add(slug)
                out.append((slug, window_start_ts))
    return out


def _market_is_live(market: dict) -> bool:
    """Reject closed/archived rows and zombie slugs from another year."""
    if _as_bool(market.get("closed")) or _as_bool(market.get("archived")):
        return False
    if market.get("active") is not None and not _as_bool(market.get("active")):
        return False
    end = _parse_end_dt(
        str(market.get("endDate") or market.get("end_date") or "")
    )
    if end is not None and end.timestamp() <= time.time():
        return False
    return True


def _parse_end_dt(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _as_bool(value: object) -> bool:
    """Parse a Gamma boolean that may arrive as bool, number or string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "resolved")


def _parse_market_resolution(data: dict | None) -> dict | None:
    """Parse one Gamma market object into a resolution verdict.

    Returns None when `data` is not a dict. Gamma has exposed resolution
    through slightly different fields over time; accept any of: explicit
    `resolved`, UMA status resolved, or a closed market whose two outcome
    prices are terminal (exactly 0 and 1). A voided market's ["0","0"] is
    NOT a resolution.
    """
    if not isinstance(data, dict):
        return None

    raw = data.get("outcomePrices") or ""
    try:
        prices = json.loads(raw) if raw else []
    except Exception:
        prices = []
    try:
        nums = [float(p) for p in prices]
    except (TypeError, ValueError):
        nums = []

    terminal = len(nums) >= 2 and sorted(nums[:2]) == [0.0, 1.0]
    uma = str(data.get("umaResolutionStatus") or "").strip().lower()
    resolved = _as_bool(data.get("resolved")) or (
        terminal and (_as_bool(data.get("closed")) or uma == "resolved")
    )
    if not resolved:
        return {"resolved": False, "up_wins": None, "outcome": ""}

    # `outcomePrices` is ordered like `outcomes`. The 15-minute series
    # lists Up first, but do not rely on it: find the UP label when
    # present so a reordered market cannot book the winner as a loser.
    up_idx = 0
    raw_outcomes = data.get("outcomes")
    try:
        labels = json.loads(raw_outcomes) if raw_outcomes else []
    except Exception:
        labels = []
    if isinstance(labels, list) and len(labels) == len(nums):
        for i, label in enumerate(labels):
            if str(label).strip().lower() in ("up", "yes"):
                up_idx = i
                break

    up_price = nums[up_idx] if nums else 0.0
    up_wins = up_price >= 0.5
    outcome_text = (data.get("outcome") or "").strip()
    if outcome_text.lower() in ("up", "yes"):
        up_wins = True
    elif outcome_text.lower() in ("down", "no"):
        up_wins = False
    return {"resolved": True, "up_wins": up_wins, "outcome": outcome_text}


@dataclass(frozen=True)
class ClientSettings:
    gamma_api: str
    polymarket_host: str


class PolymarketClient:
    """
    v2 Gamma + CLOB public API client.
    No authentication required for all methods.
    """

    def __init__(
        self,
        *,
        gamma_api: Optional[str] = None,
        polymarket_host: Optional[str] = None,
    ):
        self.settings = ClientSettings(
            gamma_api=(gamma_api or os.getenv("GAMMA_API") or "https://gamma-api.polymarket.com"),
            polymarket_host=(polymarket_host or os.getenv("POLYMARKET_HOST") or "https://clob.polymarket.com"),
        )
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._http

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    # ── Market discovery ──────────────────────────────────────────────────────────

    DEFAULT_UPDOWN_INTERVAL_SLUG: str = "15m"
    DEFAULT_UPDOWN_WINDOW_SEC: int = 900

    async def get_asset_updown_markets(
        self,
        asset: str,
        *,
        slug_interval: str | None = None,
        window_sec: int | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """
        Fetch active up/down ladder markets for the given asset.

        5m / 15m / 4h:
            `{asset.lower()}-updown-{slug_interval}-{unix_slot}`

        1h:
            `{name}-up-or-down-{month}-{day}-{year}-{hour}{am|pm}-et`
            e.g. `bitcoin-up-or-down-november-3-2026-6am-et`.
            The year-less variant and the legacy
            `{asset.lower()}-updown-1h-{unix_slot}` form are also probed.
        """
        if slug_interval is None:
            slug_interval = self.DEFAULT_UPDOWN_INTERVAL_SLUG
        if window_sec is None:
            window_sec = self.DEFAULT_UPDOWN_WINDOW_SEC
        asset = asset.upper()
        c = await self._client()
        now = int(time.time())
        if window_sec <= 0:
            window_sec = self.DEFAULT_UPDOWN_WINDOW_SEC
        base = (now // window_sec) * window_sec
        interval = str(slug_interval).strip().lower()

        candidates: list[tuple[str, int]] = []
        legacy_prefix = f"{asset.lower()}-updown-{slug_interval}"
        if interval in ("1h", "60m", "hourly"):
            # Current ET hour first, then the legacy epoch slug, then the
            # adjacent hours as a boundary/lag fallback.
            candidates.extend(
                _et_hour_candidates(asset, now, window_sec, (0,))
            )
            candidates.append((f"{legacy_prefix}-{base}", base))
            candidates.extend(
                _et_hour_candidates(asset, now, window_sec, (-1, 1))
            )
            for ts in (base - window_sec, base + window_sec):
                candidates.append((f"{legacy_prefix}-{ts}", ts))
        else:
            for ts in (base, base - window_sec, base + window_sec):
                candidates.append((f"{legacy_prefix}-{ts}", ts))

        # Deduplicate candidate slugs while preserving priority order.
        ordered: list[tuple[str, int]] = []
        seen_slugs: set[str] = set()
        for slug, window_start_ts in candidates:
            if window_start_ts <= 0 or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            ordered.append((slug, window_start_ts))

        markets: list[dict] = []
        tried: list[str] = []
        for slug, window_start_ts in ordered:
            tried.append(slug)
            try:
                resp = await c.get(
                    f"{self.settings.gamma_api}/events",
                    params={"slug": slug},
                    timeout=8,
                )
                if resp.status_code != 200:
                    continue
                events = resp.json()
                if isinstance(events, dict):
                    events = [events] if events else []

                for ev in events:
                    ev_slug = str(ev.get("slug") or "").strip()
                    if not ev_slug:
                        continue
                    # Accept only the exact slug we asked for (or a row
                    # that adds a suffix), never another timestamp.
                    if ev_slug != slug and not ev_slug.startswith(slug + "-"):
                        continue
                    if _as_bool(ev.get("closed")) or _as_bool(ev.get("archived")):
                        continue
                    for m in (ev.get("markets") or []):
                        if not isinstance(m, dict):
                            continue
                        if not _market_is_live(m):
                            continue
                        raw_ids = m.get("clobTokenIds") or "[]"
                        if isinstance(raw_ids, list):
                            token_ids = raw_ids
                        else:
                            try:
                                token_ids = json.loads(raw_ids)
                            except Exception:
                                token_ids = []
                        if not isinstance(token_ids, list):
                            token_ids = []

                        raw_prices = m.get("outcomePrices") or '["0.5", "0.5"]'
                        if isinstance(raw_prices, list):
                            prices = raw_prices
                        else:
                            try:
                                prices = json.loads(raw_prices)
                            except Exception:
                                prices = ["0.5", "0.5"]
                        if not isinstance(prices, list):
                            prices = []

                        if len(token_ids) < 2:
                            log.warning(
                                "get_asset_updown_markets missing token ids",
                                asset=asset, slug=ev.get("slug", ""),
                                token_count=len(token_ids),
                            )
                            continue

                        # The previous code hard-coded outcome labels to
                        # "Up"/"Down" and then "validated" them against
                        # itself — a vacuous check. Read the real Gamma
                        # `outcomes` array and map token/price indices from
                        # it, so a server-side reorder cannot silently make
                        # the bot buy the wrong side.
                        raw_outcomes = m.get("outcomes")
                        if isinstance(raw_outcomes, str):
                            try:
                                outcome_list = json.loads(raw_outcomes)
                            except Exception:
                                outcome_list = []
                        elif isinstance(raw_outcomes, list):
                            outcome_list = raw_outcomes
                        else:
                            outcome_list = []

                        def _outcome_index(label: str) -> Optional[int]:
                            aliases = ("up", "yes") if label == "up" else ("down", "no")
                            for idx, outcome in enumerate(outcome_list):
                                if str(outcome).strip().lower() in aliases:
                                    return idx
                            return None

                        idx_up = _outcome_index("up")
                        idx_down = _outcome_index("down")
                        if idx_up is None or idx_down is None:
                            if outcome_list:
                                log.warning(
                                    "get_asset_updown_markets unexpected outcomes",
                                    asset=asset,
                                    slug=ev.get("slug", ""),
                                    outcomes=outcome_list,
                                )
                                continue
                            # Gamma omitted `outcomes` entirely: fall back to
                            # the historical positional order, but surface it.
                            log.warning(
                                "get_asset_updown_markets outcomes missing",
                                asset=asset,
                                slug=ev.get("slug", ""),
                                note="assuming clobTokenIds[0]=Up, [1]=Down",
                            )
                            idx_up, idx_down = 0, 1

                        def _price_at(idx: int) -> float:
                            if idx >= len(prices):
                                return 0.5
                            try:
                                return float(prices[idx])
                            except (TypeError, ValueError):
                                return 0.5

                        m["tokens"] = [
                            {
                                "token_id": token_ids[idx_up],
                                "outcome": "Up",
                                "price": str(_price_at(idx_up)),
                            },
                            {
                                "token_id": token_ids[idx_down],
                                "outcome": "Down",
                                "price": str(_price_at(idx_down)),
                            },
                        ]
                        m["_event_slug"] = ev_slug
                        m["_window_start_ts"] = window_start_ts
                        m["_up_price"] = _price_at(idx_up)
                        m["_down_price"] = _price_at(idx_down)
                        m["_neg_risk"] = False
                        markets.append(m)
                if markets:
                    log.info(
                        "get_asset_updown_markets matched",
                        asset=asset,
                        slug=ev_slug,
                        window_start_ts=window_start_ts,
                    )
                    break
            except Exception as e:
                log.debug("Slug query failed", slug=slug, error=str(e))

        if not markets:
            log.info(
                "get_asset_updown_markets no match",
                asset=asset,
                interval=slug_interval,
                window_sec=window_sec,
                tried=tried[:12],
            )
        return markets[:limit]

    async def get_markets_by_condition_id(self, condition_id: str) -> Optional[dict]:
        """Fetch a single market by its condition_id (CLOB market info)."""
        c = await self._client()
        try:
            resp = await c.get(
                f"{self.settings.polymarket_host}/markets/{condition_id}",
                timeout=8,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.debug("get_markets_by_condition_id failed", condition_id=condition_id[:12], error=str(e))
            return None

    # ── CLOB price data ─────────────────────────────────────────────────────────

    async def get_order_book(self, token_id: str) -> Optional[dict]:
        """
        Get order book (bids/asks) for a token from CLOB.
        Returns: {"bids": [{"price": "0.50", "size": "100"}, ...], "asks": [...]}
        """
        c = await self._client()
        try:
            resp = await c.get(
                f"{self.settings.polymarket_host}/orderbook",
                params={"token_id": token_id},
                timeout=3.0,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.debug("get_order_book failed", token_id=token_id[:12], error=str(e))
            return None

    async def get_midpoint(self, token_id: str) -> float:
        """Get current midpoint price (bid+ask)/2 for a token from CLOB."""
        c = await self._client()
        try:
            resp = await c.get(
                f"{self.settings.polymarket_host}/midpoint",
                params={"token_id": token_id},
                timeout=3.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("mid", 0.0))
        except Exception:
            return 0.0

    async def get_last_trade_prices_batch(
        self, token_ids: list[str]
    ) -> dict[str, float]:
        """
        Batch fetch last trade prices for multiple tokens.
        CLOB: GET /last-trades-prices?token_ids=id1,id2,...
        Returns: {"token_id": price}
        """
        if not token_ids:
            return {}
        c = await self._client()
        try:
            resp = await c.get(
                f"{self.settings.polymarket_host}/last-trades-prices",
                params={"token_ids": ",".join(token_ids)},
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json()
            result: dict[str, float] = {}
            # The endpoint has returned both shapes across deployments:
            #   {"<token_id>": "0.53", ...}  (dict)
            #   [{"token_id": "...", "price": "0.53"}, ...]  (list)
            # Support both; the previous duplicate definitions only handled
            # one shape each, and the surviving one assumed a dict.
            if isinstance(data, dict):
                for tid, price_val in data.items():
                    if tid not in token_ids:
                        continue
                    try:
                        result[tid] = float(price_val)
                    except (TypeError, ValueError):
                        result[tid] = 0.0
            elif isinstance(data, list):
                for entry in data:
                    if not isinstance(entry, dict):
                        continue
                    tid = entry.get("token_id") or entry.get("asset_id")
                    if not tid or tid not in token_ids:
                        continue
                    try:
                        result[tid] = float(entry.get("price", 0.0))
                    except (TypeError, ValueError):
                        result[tid] = 0.0
            for tid in token_ids:
                result.setdefault(tid, 0.0)
            return result
        except Exception:
            return {tid: 0.0 for tid in token_ids}

    async def get_tick_size(self, token_id: str) -> str:
        """Get minimum tick size for a token (v2 CLOB API)."""
        c = await self._client()
        resp = await c.get(
            f"{self.settings.polymarket_host}/tick-size",
            params={"token_id": token_id},
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("minimum_tick_size", "0.0001"))

    async def get_neg_risk(self, token_id: str) -> bool:
        """Check if token uses Neg Risk exchange."""
        c = await self._client()
        resp = await c.get(
            f"{self.settings.polymarket_host}/neg-risk",
            params={"token_id": token_id},
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return bool(data.get("neg_risk", False))

    async def get_clob_market_info(self, condition_id: str) -> Optional[dict]:
        """Fetch all CLOB market parameters in one call (v2)."""
        c = await self._client()
        try:
            resp = await c.get(
                f"{self.settings.polymarket_host}/markets/{condition_id}",
                timeout=8,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.debug("get_clob_market_info failed", condition_id=condition_id[:12], error=str(e))
            return None

    # ── Fee rate ────────────────────────────────────────────────────────────────

    async def get_fee_rate_bps(self, token_id: str) -> int:
        """Get taker fee rate in basis points for a token."""
        c = await self._client()
        try:
            resp = await c.get(
                f"{self.settings.polymarket_host}/fee-rate",
                params={"token_id": token_id},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            rate = int(data.get("base_fee") or data.get("fee_rate_bps") or -1)
            if rate < 0:
                raise ValueError(f"fee-rate response has no base_fee/fee_rate_bps: {data}")
            return rate
        except Exception as e:
            raise RuntimeError(f"get_fee_rate_bps({token_id[:12]}) failed: {e}") from e

    # ── Market resolution ───────────────────────────────────────────────────────

    async def _market_by_condition(
        self, c: httpx.AsyncClient, condition_id: str
    ) -> dict | None:
        """Gamma `/markets` lookup by condition_id (first/only match)."""
        resp = await c.get(
            f"{self.settings.gamma_api}/markets",
            params={"condition_ids": [condition_id]},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict):
            return data
        log.warning(
            "get_market_resolution unexpected response type",
            condition_id=condition_id[:12], got=type(data).__name__,
        )
        return None

    async def _market_by_event_slug(
        self, c: httpx.AsyncClient, slug: str, condition_id: str
    ) -> dict | None:
        """Find the market inside a Gamma event payload.

        Why this exists: for the recurring 15-minute crypto markets Gamma
        returns `[]` from `/markets?condition_ids=` once the market is
        resolved, even though `/events?slug=` still carries the final
        `closed` / `outcomePrices` / `umaResolutionStatus` fields
        (production 2026-10-31 13:15 UTC round). Without this fallback a
        losing round can never be settled.
        """
        resp = await c.get(
            f"{self.settings.gamma_api}/events",
            params={"slug": slug},
        )
        resp.raise_for_status()
        data = resp.json()
        events = data if isinstance(data, list) else [data]
        wanted_cid = str(condition_id or "").lower()
        by_slug: dict | None = None
        for ev in events:
            if not isinstance(ev, dict):
                continue
            for m in ev.get("markets") or []:
                if not isinstance(m, dict):
                    continue
                if wanted_cid and str(m.get("conditionId") or "").lower() == wanted_cid:
                    return m
                if m.get("slug") == slug:
                    by_slug = m
        return by_slug

    async def get_market_resolution(
        self, condition_id: str, slug: str | None = None
    ) -> Optional[dict]:
        """
        Query Gamma for market resolution result.

        `condition_id` is tried first via `/markets?condition_ids=`.
        `slug` (the event slug) enables the `/events?slug=` fallback used
        for resolved recurring markets, where the `/markets` filter can
        return an empty list.
        """
        c = await self._client()
        try:
            data = await self._market_by_condition(c, condition_id)
            parsed = _parse_market_resolution(data) or {
                "resolved": False, "up_wins": None, "outcome": "",
            }
            if not parsed.get("resolved") and slug:
                event_data = await self._market_by_event_slug(
                    c, slug, condition_id
                )
                event_parsed = _parse_market_resolution(event_data)
                if event_parsed and event_parsed.get("resolved"):
                    parsed = event_parsed
            return parsed
        except Exception as e:
            is_422 = getattr(e, "status_code", None) == 422
            level = "debug" if is_422 else "warning"
            getattr(log, level)("get_market_resolution failed", condition_id=condition_id[:12], error=str(e))
            return None


def parse_window_end(market: dict | None) -> Optional[datetime]:
    if not market:
        return None
    raw = market.get("endDate") or market.get("end_date") or ""
    return _parse_end_dt(raw)


async def safe_gather(*coros):
    return await asyncio.gather(*coros, return_exceptions=True)
