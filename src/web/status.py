"""Read-only status/history builders for the web dashboard.

Two data sources:

  * live bot state (`src.main.STATE`) — current round, gate drift, position,
    tier state and open orders. Read in-process, no locking needed: the
    FastAPI server runs on the bot's event loop and these builders are
    synchronous, so they can never interleave with a mutation.
  * SQLite (`polymarket.db`) — rounds / orders / fills history, mirrored by
    `OrderManager` (`db=` argument).

Nothing here mutates bot state.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from src.config import strategy as _strategy


# Share quantities below this are CLOB size-rounding dust, not inventory.
# The exchange can match slightly less than the requested size (production
# 2026-10-31 03:00: TP asked for 29.6049, matched 29.6 → 0.004925 left
# behind). The engine itself closes a position at < 0.01 shares
# (`engine._check_take_profit`), so the history must use the same cutoff —
# otherwise a fully-exited round keeps showing 未平仓 forever.
_DUST_QTY = 1e-2

# Window length assumed when attributing a settlement to a UTC day.
# The bot now trades the 1-hour series for every asset.
_ROUND_PERIOD_S = 3600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(ts: float | int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _tier_status(t: Any) -> str:
    if getattr(t, "is_complete", False):
        return "filled"
    if getattr(t, "watching", False):
        return "watching"
    if getattr(t, "placed", False):
        return "placed"
    return "idle"


def _tier_payload(t: Any) -> dict[str, Any]:
    return {
        "tier": int(getattr(t, "tier", 0)),
        "notional": float(getattr(t, "notional", 0.0) or 0.0),
        "status": _tier_status(t),
        "placed": bool(getattr(t, "placed", False)),
        "watching": bool(getattr(t, "watching", False)),
        "filled": bool(getattr(t, "is_complete", False)),
        "size_filled": float(getattr(t, "size_filled", 0.0) or 0.0),
        "size_expected": float(getattr(t, "size_expected", 0.0) or 0.0),
        "avg_fill_price": getattr(t, "avg_fill_price", None),
        "server_order_id": getattr(t, "server_order_id", None),
    }


# ---------------------------------------------------------------------------
# Live status
# ---------------------------------------------------------------------------

def _asset_status(
    state: Any,
    asset: str,
    *,
    period_s: int,
    remain_s: float,
    elapsed_s: float,
    phase: str,
) -> dict[str, Any]:
    """Per-asset live snapshot (position, tiers, orders, prices)."""
    s = _strategy()
    engines = getattr(state, "engines", None) or {}
    eng = engines.get(asset) or getattr(state, "engine", None)
    rs = getattr(eng, "_round", None) if eng else None
    wm = getattr(state, "wm", None)
    ctx = wm.get(asset) if wm else None
    mkt = (getattr(state, "markets", None) or {}).get(asset) or (
        getattr(ctx, "market", None) if ctx else None
    )
    pc = state.price_cache

    up_token = (getattr(mkt, "up_token", "") if mkt else "") or ""
    down_token = (getattr(mkt, "down_token", "") if mkt else "") or ""
    up_mid = float(pc.get_midpoint(up_token)) if up_token else 0.0
    down_mid = float(pc.get_midpoint(down_token)) if down_token else 0.0

    pos = getattr(rs, "position", None) if rs else None
    position = None
    if pos is not None:
        qty = float(pos.total_qty)
        if qty < _DUST_QTY:
            qty = 0.0
        cost = float(pos.total_cost) if qty > 0 else 0.0
        avg = float(pos.avg_cost) if qty > 0 else 0.0
        held_mid = 0.0
        if pos.entry_side == "UP":
            held_mid = up_mid
        elif pos.entry_side == "DOWN":
            held_mid = down_mid
        upnl = (held_mid - avg) * qty if qty > 0 and held_mid > 0 else 0.0
        tiers = [_tier_payload(t) for t in getattr(pos, "tiers", [])]
        if qty <= 0:
            for payload in tiers:
                payload.update(
                    status="idle",
                    placed=False,
                    watching=False,
                    filled=False,
                    size_filled=0.0,
                    size_expected=0.0,
                    avg_fill_price=None,
                    server_order_id=None,
                )
        position = {
            "side": pos.entry_side,
            "token": pos.entry_token,
            "qty": qty,
            "avg_cost": avg,
            "cost": cost,
            "mid": held_mid,
            "upnl_usd": upnl,
            "upnl_pct": (upnl / cost) if cost > 0 else 0.0,
            "tp_target": pos.tp_target(),
            "tiers": tiers,
        }

    orders: list[dict[str, Any]] = []
    om = getattr(state, "order_manager", None)
    if om is not None and rs is not None:
        try:
            for o in om.open_orders_for_round(
                rs.round_start_ts, asset=asset
            ):
                orders.append({
                    "client_order_id": o.client_order_id,
                    "server_order_id": o.server_order_id,
                    "side": o.side,
                    "tier": o.tier_name,
                    "price": o.price,
                    "size": o.placed_size or o.size,
                    "size_matched": o.size_matched,
                    "status": o.status,
                    "expires_at": o.expires_at,
                    "age_s": max(0.0, time.time() - o.placed_at),
                })
        except Exception:
            orders = []

    configured_mode = (
        "paper" if bool(getattr(s, "PAPER_TRADING", True)) else "live"
    )
    round_mode = str(getattr(rs, "mode", "") or "") if rs else ""
    return {
        "asset": asset,
        "round_start_ts": getattr(rs, "round_start_ts", None),
        "slug": getattr(rs, "slug", None) or getattr(mkt, "slug", None),
        "condition_id": getattr(mkt, "condition_id", None),
        "skip_reason": getattr(rs, "skip_reason", None),
        "market_ok": mkt is not None,
        "trading_mode": {
            "configured": configured_mode,
            "round": round_mode or None,
            "effective": round_mode or configured_mode,
            "locked": bool(round_mode),
        },
        "window": {
            "period_s": period_s,
            "remain_s": remain_s,
            "elapsed_s": elapsed_s,
            "phase": phase,
            "start": _iso(
                getattr(mkt, "window_start", None)
                and mkt.window_start.timestamp()
            ),
            "end": _iso(
                getattr(mkt, "window_end", None)
                and mkt.window_end.timestamp()
            ),
        },
        "prices": {
            "up_token": up_token,
            "down_token": down_token,
            "up_mid": up_mid,
            "down_mid": down_mid,
            "entry_price_cap": float(s.ENTRY_PRICE_CAP),
        },
        "position": position,
        "orders": orders,
        "config": {
            "t1_entry_window_s": float(s.T1_ENTRY_WINDOW_S),
            "tier_notionals": list(cfg_tier_notionals()),
            "tier_drop_pcts": [
                None,
                float(s.TIER2_DROP_PCT),
                float(s.TIER3_DROP_PCT),
                float(s.TIER4_DROP_PCT),
            ],
        },
    }


def cfg_tier_notionals() -> tuple[float, float, float, float]:
    s = _strategy()
    return (
        float(s.TIER1_NOTIONAL),
        float(s.TIER2_NOTIONAL),
        float(s.TIER3_NOTIONAL),
        float(s.TIER4_NOTIONAL),
    )


def build_status(state: Any) -> dict[str, Any]:
    """Snapshot of every asset, plus primary-asset aliases for old clients."""
    s = _strategy()
    wm = getattr(state, "wm", None)
    period_s = int(wm.window_period_s()) if wm else int(
        getattr(s, "MARKET_WINDOW_SEC", 3600) or 3600
    )
    try:
        remain_s = max(
            0.0, min(float(period_s), float(wm.seconds_until_next()))
        )
    except Exception:
        remain_s = 0.0
    elapsed_s = max(0.0, period_s - remain_s)
    if remain_s < 1.0:
        phase = "END"
    elif elapsed_s <= s.T1_ENTRY_WINDOW_S:
        phase = "IN_T1"
    else:
        phase = "PAST_T1"

    assets = list(getattr(state, "assets", []) or ["BTC"])
    statuses = [
        _asset_status(
            state,
            asset,
            period_s=period_s,
            remain_s=remain_s,
            elapsed_s=elapsed_s,
            phase=phase,
        )
        for asset in assets
    ]
    primary = statuses[0] if statuses else {}
    ks = getattr(state, "killswitch", None)

    out: dict[str, Any] = dict(primary)
    out.update({
        "server_time": int(time.time()),
        "primary": primary.get("asset", "BTC"),
        "assets": statuses,
        "killswitch": {
            "active": bool(getattr(ks, "active", False)),
            "since": getattr(ks, "since", 0.0) or None,
            "reason": getattr(ks, "reason", "") or None,
        },
        "window": primary.get("window", {
            "period_s": period_s,
            "remain_s": remain_s,
            "elapsed_s": elapsed_s,
            "phase": phase,
        }),
        "trading_mode": primary.get("trading_mode", {
            "configured": "paper" if bool(getattr(s, "PAPER_TRADING", True)) else "live",
            "round": None,
            "effective": "paper" if bool(getattr(s, "PAPER_TRADING", True)) else "live",
            "locked": False,
        }),
    })
    # Compatibility field for older dashboard clients: the pure-martingale
    # deployment has no BTC drift gate.
    out.setdefault("gate", {
        "state": "pending",
        "baseline": None,
        "drift": None,
        "limit": float(getattr(s, "BASELINE_DRIFT_LIMIT", 0.0)),
        "binance": 0.0,
    })
    return out


# ---------------------------------------------------------------------------
# History (SQLite)
# ---------------------------------------------------------------------------

_ROUNDS_SQL = """
SELECT r.round_start_ts,
       COALESCE(r.asset, 'BTC') AS asset,
       COALESCE(r.mode, 'live') AS mode,
       r.slug,
       r.started_at,
       r.finished_at,
       COALESCE(r.resolved, 0) AS resolved,
       r.up_won,
       r.resolved_at,
       r.up_token,
       r.down_token,
       COALESCE(SUM(CASE WHEN f.side = 'BUY'  THEN f.price * f.size END), 0.0) AS buy_cost,
       COALESCE(SUM(CASE WHEN f.side = 'SELL' THEN f.price * f.size END), 0.0) AS sell_proceeds,
       COUNT(f.fill_id) AS fill_count,
       (SELECT COUNT(*) FROM orders o
         WHERE o.round_start_ts = r.round_start_ts
           AND COALESCE(o.asset, 'BTC') = COALESCE(r.asset, 'BTC')) AS order_count,
       (SELECT COUNT(*) FROM orders o3
         WHERE o3.round_start_ts = r.round_start_ts
           AND COALESCE(o3.asset, 'BTC') = COALESCE(r.asset, 'BTC')
           AND o3.status = 'cancelled') AS cancel_count
FROM rounds r
LEFT JOIN fills f ON f.round_start_ts = r.round_start_ts
                 AND COALESCE(f.asset, 'BTC') = COALESCE(r.asset, 'BTC')
WHERE (EXISTS (SELECT 1 FROM orders o2
                WHERE o2.round_start_ts = r.round_start_ts
                  AND COALESCE(o2.asset, 'BTC') = COALESCE(r.asset, 'BTC'))
    OR EXISTS (SELECT 1 FROM fills f2
                WHERE f2.round_start_ts = r.round_start_ts
                  AND COALESCE(f2.asset, 'BTC') = COALESCE(r.asset, 'BTC')))
  AND (? IS NULL OR COALESCE(r.mode, 'live') = ?)
  AND (? IS NULL OR COALESCE(r.asset, 'BTC') = ?)
GROUP BY r.round_start_ts, r.asset, r.mode
ORDER BY r.round_start_ts DESC, r.asset ASC
LIMIT ? OFFSET ?
"""


def _round_realized_pnl(
    conn: Any, wanted: set[int]
) -> tuple[
    dict[int, float], dict[int, float], dict[int, dict[str, list[float]]],
    dict[int, float], dict[int, int], dict[int, float], dict[int, float],
]:
    """Walk each round's fills in time order and return REALISED PnL.

    Uses the same average-cost accounting as the engine: BUY fills grow the
    position and its cost basis, SELL fills realise
    `(sell_price - avg_cost) * sold_qty` and reduce the basis pro-rata.

    Why this exists (operator 2026-10-31): the old round PnL was
    `sell_proceeds - buy_cost`, so a same-window re-entry after a TP made
    the round look like a loss — the fresh BUY's cost was subtracted even
    though that position had not been sold yet (first TP +0.5 then a 2u
    re-entry displayed -1.5). Realised PnL counts each SELL when it fills;
    an open re-entry does not touch it.

    Fees (Polymarket taker fee, stored per fill) are included: a BUY fee
    is added to the cost basis, a SELL fee is deducted from the proceeds.

    Returns dicts keyed by round_start_ts:
      realized, cost_of_sold, open_positions, pnl_today, sells_today,
      fees_total, buy_fees

    `open_positions[round][token_id] = [qty, cost_basis]` is what allows
    settlement accounting: shares left at expiry are paid 1 (win) or 0
    (loss) and never produce a SELL fill, so the caller needs the token
    and remaining basis to book that outcome.
    """
    realized: dict[int, float] = {}
    cost_sold: dict[int, float] = {}
    pnl_today: dict[int, float] = {}
    sells_today: dict[int, int] = {}
    fees_total: dict[int, float] = {}
    buy_fees: dict[int, float] = {}
    today_start_ms = int(
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp() * 1000
    )
    # round -> token_id -> [qty, cost basis]
    state: dict[int, dict[str, list[float]]] = {}
    for (
        round_ts, asset, token_id, side, price, size, ts_ms, fee
    ) in conn.execute(
        "SELECT round_start_ts, COALESCE(asset, 'BTC'), token_id, side, "
        "price, size, ts_ms, COALESCE(fee, 0) FROM fills "
        "ORDER BY round_start_ts ASC, ts_ms ASC, fill_id ASC"
    ):
        key = (int(round_ts), str(asset or "BTC").upper())
        if key not in wanted:
            continue
        try:
            px = float(price or 0.0)
            sz = float(size or 0.0)
            fee_f = float(fee or 0.0)
        except (TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0:
            continue
        tok = str(token_id or "")
        positions = state.setdefault(key, {})
        st = positions.setdefault(tok, [0.0, 0.0])
        fees_total[key] = fees_total.get(key, 0.0) + fee_f
        if str(side or "").upper() == "BUY":
            st[0] += sz
            st[1] += px * sz + fee_f
            buy_fees[key] = buy_fees.get(key, 0.0) + fee_f
            continue
        # SELL: realise only against shares actually held. Older rows can
        # carry a blank token_id on either side; fall back to the sole
        # token that still holds inventory.
        if st[0] <= 1e-12:
            held = [s for s in positions.values() if s[0] > 1e-12]
            if len(held) != 1:
                continue
            st = held[0]
        sold = min(sz, st[0])
        if sold <= 1e-12:
            continue
        avg = st[1] / st[0]
        fee_sold = fee_f * (sold / sz)
        proceeds = px * sold - fee_sold
        pnl = proceeds - avg * sold
        realized[key] = realized.get(key, 0.0) + pnl
        cost_sold[key] = cost_sold.get(key, 0.0) + avg * sold
        try:
            ts_ms_i = int(ts_ms or 0)
        except (TypeError, ValueError):
            ts_ms_i = 0
        if ts_ms_i >= today_start_ms:
            pnl_today[key] = pnl_today.get(key, 0.0) + pnl
            sells_today[key] = sells_today.get(key, 0) + 1
        st[0] -= sold
        st[1] -= avg * sold
        if st[0] <= 1e-9:
            st[0] = 0.0
            st[1] = 0.0
    return (
        realized, cost_sold, state, pnl_today, sells_today, fees_total,
        buy_fees,
    )


def rounds_with_pnl(
    conn: Any,
    limit: int = 100,
    mode: str | None = None,
    asset: str | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Rounds with realised PnL derived from fills.

    Realised PnL is computed per SELL fill against the round's running
    average cost, so each take-profit is booked the moment it fills — a
    same-window re-entry does not subtract its (still open) buy cost.

    Status:
      * `closed`  — at least one SELL and the position is flat
      * `holding` — realised some sells but still holds inventory
      * `open`    — holds inventory, no sells yet (pnl_usd is None)
      * `unfilled`— orders but no fills at all

    Rounds with neither orders nor fills are excluded entirely. Older
    builds wrote a `rounds` row at every window boundary, so windows where
    nothing was ever bought (e.g. every BUY rejected for insufficient
    balance) lingered in the history as phantom open positions.
    """
    wanted_asset = str(asset).upper() if asset else None
    rows = conn.execute(
        _ROUNDS_SQL,
        (
            mode, mode, wanted_asset, wanted_asset,
            int(limit), max(0, int(offset)),
        ),
    ).fetchall()
    wanted = {
        (int(row[0]), str(row[1] or "BTC").upper()) for row in rows
    }
    if wanted:
        (realized, cost_sold, open_positions, pnl_today, sells_today,
         fees_total, buy_fees) = _round_realized_pnl(conn, wanted)
    else:
        (realized, cost_sold, open_positions, pnl_today, sells_today,
         fees_total, buy_fees) = ({}, {}, {}, {}, {}, {}, {})
    today_start_ms = int(
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp() * 1000
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        (round_ts, row_asset, row_mode, slug, started_at, finished_at,
         resolved, up_won, resolved_at, up_token, down_token, buy_cost,
         sell_proceeds, fill_count, order_count, cancel_count) = row
        key = (int(round_ts), str(row_asset or "BTC").upper())
        resolved = bool(resolved)
        up_won_b = None if up_won is None else bool(up_won)
        buy_cost = float(buy_cost or 0.0)
        sell_proceeds = float(sell_proceeds or 0.0)
        fill_count = int(fill_count or 0)
        has_sells = sell_proceeds > 0

        # Remaining inventory, per token. Ignore CLOB size-rounding dust
        # (see `_DUST_QTY`): a round whose residual cannot even be sold is
        # flat.
        positions = open_positions.get(key, {})
        qty_open = sum(
            float(q) for q, _cost in positions.values() if q >= _DUST_QTY
        )

        # Settlement: shares left at expiry pay 1 (win) or 0 (loss) and
        # never produce a SELL fill. Book the outcome explicitly — without
        # this, a fully losing T1..T4 round stays "未平仓" forever and is
        # missing from the dashboard's PnL / win-rate stats.
        settle_pnl: float | None = None
        if resolved and up_won_b is not None and qty_open > 0:
            total = 0.0
            attributed = True
            for token, (q, cost) in positions.items():
                if q < _DUST_QTY:
                    continue
                if up_token and str(token) == str(up_token):
                    payoff = 1.0 if up_won_b else 0.0
                elif down_token and str(token) == str(down_token):
                    payoff = 0.0 if up_won_b else 1.0
                else:
                    # The held side cannot be mapped to UP/DOWN (legacy
                    # row without token ids). Leave the round open rather
                    # than book a wrong number.
                    attributed = False
                    break
                total += float(q) * payoff - float(cost)
            if attributed:
                settle_pnl = total

        if resolved and (qty_open <= 0 or settle_pnl is not None):
            round_status = "closed"
        elif qty_open > 0:
            round_status = "holding" if has_sells else "open"
        elif has_sells:
            round_status = "closed"
        elif fill_count > 0:
            round_status = "open"
        else:
            round_status = "unfilled"

        pnl = None
        if has_sells or settle_pnl is not None:
            pnl = float(realized.get(key, 0.0)) + (settle_pnl or 0.0)
        cost_sold_round = float(cost_sold.get(key, 0.0))
        # A settled round's return is measured against everything it
        # invested (notional + buy-side taker fees); a still-open round
        # keeps the realised-on-sold-shares denominator.
        pnl_denom = (
            buy_cost + float(buy_fees.get(key, 0.0))
            if settle_pnl is not None
            else cost_sold_round
        )

        # Attribute the settlement to the window it belongs to, not to the
        # moment Gamma answered: a round ending at 23:58 UTC must stay in
        # that day's PnL even if resolution lands after midnight (or the
        # bot was offline and backfilled the outcome the next day).
        settled_today = bool(
            settle_pnl is not None
            and (float(round_ts) + _ROUND_PERIOD_S) * 1000.0 >= today_start_ms
        )

        out.append({
            "round_start_ts": round_ts,
            "asset": key[1],
            "mode": str(row_mode or "live"),
            "slug": slug,
            "started_at": started_at,
            "finished_at": finished_at,
            "resolved": resolved,
            "up_won": up_won_b,
            "resolved_at": resolved_at,
            "buy_cost": buy_cost,
            "sell_proceeds": sell_proceeds,
            "fill_count": fill_count,
            "order_count": int(order_count or 0),
            "cancel_count": int(cancel_count or 0),
            # Taker fees recorded on this round's fills (USDC).
            "fee_total": float(fees_total.get(key, 0.0)),
            "status": round_status,
            "closed": round_status == "closed",
            "pnl_usd": pnl,
            "pnl_pct": (
                (pnl / pnl_denom)
                if (pnl is not None and pnl_denom > 0)
                else None
            ),
            # PnL from the position left to settlement (1/0 payout).
            "settlement_usd": (
                float(settle_pnl) if settle_pnl is not None else 0.0
            ),
            # Realised PnL booked TODAY (by sell fill timestamp), so the
            # dashboard reflects a TP immediately, not at the round's end.
            # Settlement PnL is included when the market resolved today.
            "pnl_today_usd": (
                float(pnl_today.get(key, 0.0)) if has_sells else 0.0
            ) + (float(settle_pnl) if settled_today else 0.0),
            "sells_today": int(sells_today.get(key, 0)),
            "settled_today": settled_today,
            "open_qty": 0.0 if settle_pnl is not None else qty_open,
        })
    return out


def compute_stats(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [r for r in rounds if r["status"] == "closed"]
    # "未平" includes rounds still holding inventory, whether or not they
    # already realised some sells (same-window re-entry).
    open_rounds = [r for r in rounds if r["status"] in ("open", "holding")]
    unfilled = [r for r in rounds if r["status"] == "unfilled"]
    # Win/loss is judged on REALISED PnL per round (a holding round with a
    # profitable TP already counts; a round settled at expiry counts too,
    # even when it never produced a SELL — e.g. the losing T4 case).
    traded = [r for r in rounds if r.get("pnl_usd") is not None]
    wins = [r for r in traded if (r["pnl_usd"] or 0.0) > 0]
    losses = [r for r in traded if (r["pnl_usd"] or 0.0) <= 0]
    total_pnl = sum(r["pnl_usd"] or 0.0 for r in rounds)
    gross_win = sum(r["pnl_usd"] for r in wins)
    gross_loss = abs(sum(r["pnl_usd"] for r in losses))
    today_pnl = sum(float(r.get("pnl_today_usd") or 0.0) for r in rounds)
    today_rounds = sum(
        1 for r in rounds
        if int(r.get("sells_today") or 0) > 0 or r.get("settled_today")
    )
    return {
        "closed_rounds": len(closed),
        "open_rounds": len(open_rounds),
        "unfilled_rounds": len(unfilled),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(traded)) if traded else None,
        "total_pnl_usd": total_pnl,
        "today_pnl_usd": today_pnl,
        "today_rounds": today_rounds,
        "avg_win_usd": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss_usd": (-gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
    }


_FILLS_SQL = """
SELECT f.fill_id,
       f.round_start_ts,
       COALESCE(f.asset, 'BTC') AS asset,
       COALESCE(f.mode, 'live') AS mode,
       f.side,
       f.price,
       f.size,
       f.ts_ms,
       COALESCE(o.tier_name, '') AS tier_name,
       COALESCE(r.slug, '') AS slug,
       COALESCE(o.price, 0.0) AS limit_price,
       COALESCE(f.trader_side, '') AS trader_side,
       COALESCE(f.fee, 0.0) AS fee,
       CASE
         WHEN r.up_token IS NOT NULL AND f.token_id = r.up_token THEN 'UP'
         WHEN r.down_token IS NOT NULL AND f.token_id = r.down_token THEN 'DOWN'
         ELSE ''
       END AS outcome
FROM fills f
LEFT JOIN orders o ON o.client_order_id = f.order_id
LEFT JOIN rounds r ON r.round_start_ts = f.round_start_ts
                  AND COALESCE(r.asset, 'BTC') = COALESCE(f.asset, 'BTC')
WHERE (? IS NULL OR COALESCE(f.mode, 'live') = ?)
  AND (? IS NULL OR COALESCE(f.asset, 'BTC') = ?)
ORDER BY f.ts_ms DESC, f.fill_id DESC
LIMIT ? OFFSET ?
"""


def _improvement_bps(side: str, price: float, limit_price: float) -> float | None:
    """Signed price improvement vs the posted limit, in basis points.

    Positive means the fill beat the limit: a BUY filled below its limit
    price, or a SELL above it. Maker fills land exactly on the limit
    (0 bps); crossing (taker) fills usually show positive improvement.
    """
    if price <= 0 or limit_price <= 0:
        return None
    side_u = str(side or "").upper()
    if side_u == "BUY":
        return (limit_price - price) / limit_price * 10000.0
    if side_u == "SELL":
        return (price - limit_price) / limit_price * 10000.0
    return None


def count_fills(
    conn: Any, mode: str | None = None, asset: str | None = None
) -> int:
    """Total number of fill rows (pagination denominator)."""
    wanted_asset = str(asset).upper() if asset else None
    row = conn.execute(
        "SELECT COUNT(*) FROM fills "
        "WHERE (? IS NULL OR COALESCE(mode, 'live') = ?) "
        "AND (? IS NULL OR COALESCE(asset, 'BTC') = ?)",
        (mode, mode, wanted_asset, wanted_asset),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def recent_fills(
    conn: Any,
    limit: int = 200,
    offset: int = 0,
    mode: str | None = None,
    asset: str | None = None,
) -> list[dict[str, Any]]:
    """Fill rows, newest first.

    `offset` powers the 成交明细 pager: the query is deterministically
    ordered by (ts_ms, fill_id) so consecutive pages neither overlap nor
    skip a row when several fills share a millisecond.
    """
    wanted_asset = str(asset).upper() if asset else None
    rows = conn.execute(
        _FILLS_SQL,
        (mode, mode, wanted_asset, wanted_asset, int(limit),
         max(0, int(offset))),
    ).fetchall()
    out = []
    for (fill_id, round_ts, row_asset, row_mode, side, price, size, ts_ms,
         tier_name, slug, limit_price, trader_side, fee, outcome) in rows:
        price = float(price or 0.0)
        size = float(size or 0.0)
        limit_price = float(limit_price or 0.0)
        fee = float(fee or 0.0)
        notional = price * size
        out.append({
            "fill_id": fill_id,
            "round_start_ts": round_ts,
            "asset": str(row_asset or "BTC").upper(),
            "mode": str(row_mode or "live"),
            "side": side,
            # UP/DOWN token the fill touched; '' for legacy rows whose
            # round predates token-id storage (cannot be mapped).
            "outcome": str(outcome or "").upper(),
            "price": price,
            "size": size,
            "notional": notional,
            "ts_ms": ts_ms,
            "tier": tier_name,
            "slug": slug,
            "limit_price": limit_price,
            "trader_side": str(trader_side or "").upper(),
            "fee": fee,
            "improvement_bps": _improvement_bps(side, price, limit_price),
        })
    return out


def execution_quality(conn: Any, limit: int = 500) -> dict[str, Any]:
    """Execution-quality stats for the dashboard.

    Covers the four levers we tune when adjusting the order logic:
      * maker vs taker mix (WS `trader_side`; REST-only observations are
        counted as unknown),
      * realised price improvement vs the posted limit (SELL premium and
        BUY discount, in bps),
      * cancel rate (cancelled orders / all orders),
      * average time from placement to first fill.
    """
    rows = conn.execute(
        """
        SELECT f.side, f.price, COALESCE(o.price, 0.0), f.trader_side
        FROM fills f
        LEFT JOIN orders o ON o.client_order_id = f.order_id
        ORDER BY f.ts_ms DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()

    maker = taker = unknown = 0
    buy_improvements: list[float] = []
    sell_improvements: list[float] = []
    for side, price, limit_price, trader_side in rows:
        role = str(trader_side or "").upper()
        if role == "MAKER":
            maker += 1
        elif role == "TAKER":
            taker += 1
        else:
            unknown += 1
        bps = _improvement_bps(side, float(price or 0.0), float(limit_price or 0.0))
        if bps is None:
            continue
        if str(side or "").upper() == "BUY":
            buy_improvements.append(bps)
        elif str(side or "").upper() == "SELL":
            sell_improvements.append(bps)

    order_row = conn.execute(
        "SELECT COUNT(*), "
        "COALESCE(SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END), 0) "
        "FROM orders"
    ).fetchone()
    total_orders = int(order_row[0] or 0) if order_row else 0
    cancelled_orders = int(order_row[1] or 0) if order_row else 0

    latency_row = conn.execute(
        """
        SELECT AVG((ff.first_ts - o.placed_at * 1000.0) / 1000.0)
        FROM orders o
        JOIN (
            SELECT order_id, MIN(ts_ms) AS first_ts
            FROM fills GROUP BY order_id
        ) ff ON ff.order_id = o.client_order_id
        WHERE o.placed_at > 0
          AND ff.first_ts >= o.placed_at * 1000.0
          AND ff.first_ts - o.placed_at * 1000.0 < 900000.0
        """
    ).fetchone()
    avg_fill_time = (
        float(latency_row[0])
        if latency_row and latency_row[0] is not None
        else None
    )

    known = maker + taker
    return {
        "maker_fills": maker,
        "taker_fills": taker,
        "unknown_fills": unknown,
        "maker_ratio": (maker / known) if known else None,
        "buy_improvement_bps": (
            sum(buy_improvements) / len(buy_improvements)
            if buy_improvements else None
        ),
        "sell_improvement_bps": (
            sum(sell_improvements) / len(sell_improvements)
            if sell_improvements else None
        ),
        "total_orders": total_orders,
        "cancelled_orders": cancelled_orders,
        "cancel_ratio": (cancelled_orders / total_orders) if total_orders else None,
        "avg_time_to_fill_s": avg_fill_time,
    }


def fills_for_round(
    conn: Any, round_start_ts: int, asset: str | None = None
) -> list[dict[str, Any]]:
    wanted_asset = str(asset).upper() if asset else None
    rows = conn.execute(
        """
        SELECT f.fill_id, COALESCE(f.asset, 'BTC'), f.side, f.price,
               f.size, f.ts_ms, COALESCE(o.tier_name, '') AS tier_name
        FROM fills f
        LEFT JOIN orders o ON o.client_order_id = f.order_id
        WHERE f.round_start_ts = ?
          AND (? IS NULL OR COALESCE(f.asset, 'BTC') = ?)
        ORDER BY f.ts_ms ASC
        """,
        (int(round_start_ts), wanted_asset, wanted_asset),
    ).fetchall()
    return [
        {
            "fill_id": r[0], "asset": r[1], "side": r[2],
            "price": float(r[3] or 0.0), "size": float(r[4] or 0.0),
            "ts_ms": r[5], "tier": r[6],
        }
        for r in rows
    ]
