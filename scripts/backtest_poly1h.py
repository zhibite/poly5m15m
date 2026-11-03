#!/usr/bin/env python3
"""Backtest the current 1h multi-asset martingale on Polymarket history.

The database is produced by ``/root/probe/fetch_poly1h_history.py``:

  rounds(asset, window_start, window_end, slug, up_won, kline_ok, coverage_ok, ...)
  prices(asset, window_start, side, time_ms, price, volume)

`prices` contains ~30 s points from future.news or 1-minute points from
the CLOB ``/prices-history`` endpoint.  The last point sits exactly at
``window_end`` and is the resolved 0/1 print; it is excluded from trading
decisions and settlement is taken from ``rounds.up_won``.

Strategy modelled (current production defaults):
  * T1: mid <= 0.40 within the first 600 s; GTD/taker at mid (paper broker
    semantics), 2 USD.
  * T2/T3/T4: trigger = T1 fill * (1 - 20%/40%/60%);
    fire line = trigger * (1 + PRICE_PRE_MARGIN); 4/8/16 USD.
    Marketable at arm -> taker at the observed mid; otherwise a resting
    GTD at the trigger, filled maker when a later point crosses it.
  * TP: max(5%, 20% - 3% * filled averaging tiers) above average cost.
  * T4 two-stage stop-loss with the current defaults.
  * Taker fee = C * 0.07 * p * (1-p); makers pay 0.

The model is deliberately mid-based: there is no order-book depth in the
history, so maker fills are assumed to happen whenever the price touches
the limit.  Taker exits are assumed at the observed mid (paper broker),
not at the live FAK limit minus slippage.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


ASSETS = ("BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "HYPE")


# ---------------------------------------------------------------------------
# Parameters (defaults mirror .env.docker / config.py on 2026-11-03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Params:
    entry_price_cap: float = 0.40
    price_pre_margin: float = 0.10

    t1_notional: float = 2.0
    t2_notional: float = 4.0
    t3_notional: float = 8.0
    t4_notional: float = 16.0
    drop2: float = 0.20
    drop3: float = 0.40
    drop4: float = 0.60

    t1_entry_window_s: float = 600.0
    gtd_lifetime_s: float = 300.0
    min_shares: float = 5.0

    tp_profit_pct: float = 0.20
    tp_decrement_pct: float = 0.03
    tp_min_pct: float = 0.05
    cancel_hysteresis_pct: float = 0.02

    taker_fee_rate: float = 0.07

    stop_loss_enabled: bool = True
    sl_t4_drop_pct: float = 0.45
    sl_confirm_s: float = 20.0
    sl_stage1_sell_pct: float = 0.60
    sl_stage2_extra_drop_pct: float = 0.30
    sl_time_exit_s: float = 120.0
    sl_floor: float = 0.05
    sl_final_exit_s: float = 60.0
    sl_final_mid: float = 0.50

    def notional(self, tier: int) -> float:
        return (
            self.t1_notional,
            self.t2_notional,
            self.t3_notional,
            self.t4_notional,
        )[tier - 1]

    def drop(self, tier: int) -> float | None:
        return (None, self.drop2, self.drop3, self.drop4)[tier - 1]

    def tp_pct(self, n_avg: int) -> float:
        return max(
            self.tp_min_pct,
            self.tp_profit_pct - n_avg * self.tp_decrement_pct,
        )


def taker_fee(shares: float, price: float, rate: float) -> float:
    """Official Polymarket taker fee: C * rate * p * (1 - p)."""
    if shares <= 0 or price <= 0 or rate <= 0:
        return 0.0
    p = min(max(float(price), 0.0), 1.0)
    return max(0.0, shares) * rate * p * (1.0 - p)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class RoundResult:
    asset: str
    window_start: int
    slug: str
    side: str | None = None
    entry_ts: float | None = None
    entry_price: float | None = None
    exit: str = "NO_ENTRY"
    pnl: float = 0.0
    fees: float = 0.0
    cost: float = 0.0
    proceeds: float = 0.0
    qty_left: float = 0.0
    highest_tier: int = 1
    t4_filled: bool = False
    n_fills: int = 0


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _merge_series(
    up: list[tuple[float, float]],
    down: list[tuple[float, float]],
) -> list[tuple[float, float, float]]:
    """Union of sample times with forward-filled UP/DOWN prices."""
    if not up or not down:
        return []
    times = sorted({t for t, _ in up} | {t for t, _ in down})
    iu = idn = 0
    last_up = last_down = None
    merged: list[tuple[float, float, float]] = []
    for t in times:
        while iu < len(up) and up[iu][0] <= t:
            last_up = up[iu][1]
            iu += 1
        while idn < len(down) and down[idn][0] <= t:
            last_down = down[idn][1]
            idn += 1
        if last_up is not None and last_down is not None:
            merged.append((t, last_up, last_down))
    return merged


def load_series(
    conn: sqlite3.Connection, asset: str, window_start: int, window_end: int
) -> list[tuple[float, float, float]] | None:
    rows = conn.execute(
        "SELECT side, time_ms, price FROM prices "
        "WHERE asset=? AND window_start=? AND time_ms < ? "
        "ORDER BY time_ms",
        (asset, int(window_start), int(window_end) * 1000),
    ).fetchall()
    up: list[tuple[float, float]] = []
    down: list[tuple[float, float]] = []
    for side, t_ms, price in rows:
        try:
            t = float(t_ms) / 1000.0
            p = float(price)
        except (TypeError, ValueError):
            continue
        if p <= 0 or p >= 1:
            continue
        if side == "UP":
            up.append((t, p))
        elif side == "DOWN":
            down.append((t, p))
    merged = _merge_series(up, down)
    return merged or None


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay(
    asset: str,
    window_start: int,
    window_end: int,
    slug: str,
    up_won: bool,
    points: list[tuple[float, float, float]],
    p: Params,
) -> RoundResult:
    result = RoundResult(asset=asset, window_start=window_start, slug=slug)
    if not points:
        return result

    ws = float(window_start)
    we = float(window_end)
    entry_idx: int | None = None
    side = ""
    t1_price = 0.0

    # ── T1: first side at/below the price cap in the entry window ──────
    for i, (t, up, down) in enumerate(points):
        if t - ws > p.t1_entry_window_s:
            break
        if up <= p.entry_price_cap:
            entry_idx, side, t1_price = i, "UP", up
            break
        if down <= p.entry_price_cap:
            entry_idx, side, t1_price = i, "DOWN", down
            break
    if entry_idx is None:
        return result

    t1_shares = p.t1_notional / p.entry_price_cap
    if t1_shares < p.min_shares - 1e-9:
        return result

    qty = t1_shares
    avg = t1_price
    bought_qty = t1_shares
    bought_cost = t1_shares * t1_price
    fees = taker_fee(t1_shares, t1_price, p.taker_fee_rate)
    proceeds = 0.0
    max_shares = qty

    result.side = side
    result.entry_ts = points[entry_idx][0]
    result.entry_price = t1_price
    result.n_fills = 1

    tier_filled = [True, False, False, False]  # T1..T4
    # A zero notional disables that tier (used by the A/B variants).
    for tier in (2, 3, 4):
        if p.notional(tier) <= 1e-9:
            tier_filled[tier - 1] = True
    n_avg = 0
    highest_tier = 1
    t4_filled = False
    t4_avg = 0.0
    stop_stage = 0
    stop_below_since: float | None = None
    resting: dict[str, Any] | None = None
    tp_armed = False

    def tp_target() -> float:
        return min(0.99, avg * (1.0 + p.tp_pct(n_avg)))

    def buy_more(
        tier: int, size_price: float, fill_price: float, role: str
    ) -> None:
        """Fill a tier.

        `size_price` is the order's limit/strategy price: the CLOB order
        is sized as `notional / size_price`.  `fill_price` is what the
        order actually traded at; for a marketable order this can be
        below the limit (price improvement), but the share count must
        still be based on the limit, not on the improved fill price.
        """
        nonlocal qty, avg, bought_qty, bought_cost, fees, n_avg
        nonlocal highest_tier, t4_filled, t4_avg, tp_armed, max_shares
        if size_price <= 0 or fill_price <= 0:
            return
        notional = p.notional(tier)
        shares = notional / size_price
        if shares <= 1e-9:
            return
        qty += shares
        bought_qty += shares
        bought_cost += shares * fill_price
        avg = bought_cost / bought_qty
        fee = (
            taker_fee(shares, fill_price, p.taker_fee_rate)
            if role == "TAKER" else 0.0
        )
        fees += fee
        n_avg += 1
        tier_filled[tier - 1] = True
        highest_tier = max(highest_tier, tier)
        result.n_fills += 1
        max_shares = max(max_shares, qty)
        if tier == 4:
            t4_filled = True
            t4_avg = fill_price
        tp_armed = False  # the TP target changed

    def sell(qty_sold: float, price: float, role: str) -> None:
        nonlocal qty, proceeds, fees
        qty_sold = min(qty_sold, qty)
        if qty_sold <= 1e-12:
            return
        proceeds += qty_sold * price
        if role == "TAKER":
            fees += taker_fee(qty_sold, price, p.taker_fee_rate)
        qty -= qty_sold

    # ── Walk the window after the T1 fill ──────────────────────────────
    for i in range(entry_idx + 1, len(points)):
        t, up, down = points[i]
        price = up if side == "UP" else down
        remaining = max(0.0, we - t)

        # 1) A resting next-tier GTD fills at its limit.
        if resting is not None:
            if price <= resting["trigger"]:
                buy_more(
                    resting["tier"], resting["trigger"],
                    resting["trigger"], "MAKER",
                )
                resting = None
            elif t >= resting["armed_at"] + p.gtd_lifetime_s:
                resting = None

        # 2) Arm/fill the next unfilled tier. A gap can fill several in one
        #    sample; loop until the next tier is not triggered or is resting.
        while True:
            next_tier = next(
                (tier for tier in (2, 3, 4) if not tier_filled[tier - 1]),
                None,
            )
            if next_tier is None or resting is not None:
                break
            drop = p.drop(next_tier)
            if drop is None:
                break
            trigger = t1_price * (1.0 - drop)
            fire = trigger * (1.0 + p.price_pre_margin)
            if trigger <= 0 or price > fire:
                break
            if price <= trigger:
                # Marketable at arm time: size on the trigger (the GTD
                # limit), fill at the observed mid (price improvement).
                buy_more(next_tier, trigger, price, "TAKER")
            else:
                resting = {
                    "tier": next_tier,
                    "trigger": trigger,
                    "armed_at": t,
                }
                break

        # 3) Take-profit GTD/order.
        target = tp_target()
        fire = target * (1.0 - p.price_pre_margin)
        cancel = fire * (1.0 - p.cancel_hysteresis_pct)
        if tp_armed and price < cancel:
            tp_armed = False
        if qty > 1e-12:
            if not tp_armed:
                if price >= fire:
                    tp_armed = True
                    if price >= target:
                        if qty < p.min_shares:
                            sell(qty, price, "TAKER")
                        else:
                            sell(qty, target, "TAKER")
                        result.exit = "TP"
                        break
            else:
                if price >= target:
                    if qty < p.min_shares:
                        sell(qty, price, "TAKER")
                    else:
                        sell(qty, target, "MAKER")
                    result.exit = "TP"
                    break

        # 4) Two-stage stop after T4.
        if p.stop_loss_enabled and t4_filled and qty > 1e-12:
            line = t4_avg * (1.0 - p.sl_t4_drop_pct)
            if stop_stage <= 0:
                if price <= line:
                    if stop_below_since is None:
                        stop_below_since = t
                    elif t - stop_below_since >= p.sl_confirm_s:
                        qty_sold = (
                            math.floor(qty * p.sl_stage1_sell_pct * 10000.0)
                            / 10000.0
                        )
                        sell(qty_sold, price, "TAKER")
                        stop_stage = 1
                        stop_below_since = None
                else:
                    stop_below_since = None
            else:
                stage2_line = line * (1.0 - p.sl_stage2_extra_drop_pct)
                if (
                    price <= stage2_line
                    or (
                        remaining <= p.sl_time_exit_s
                        and price <= p.sl_floor
                    )
                    or (
                        remaining <= p.sl_final_exit_s
                        and price < p.sl_final_mid
                    )
                ):
                    sell(qty, price, "TAKER")
                    result.exit = "SL"
                    break

    # ── Settlement ──────────────────────────────────────────────────────
    settlement = 0.0
    if qty > 1e-9:
        won = (side == "UP") == bool(up_won)
        settlement = qty * (1.0 if won else 0.0)
        result.exit = "SETTLE"

    result.pnl = proceeds + settlement - bought_cost - fees
    result.fees = fees
    result.cost = bought_cost
    result.proceeds = proceeds
    result.qty_left = qty
    result.highest_tier = highest_tier
    result.t4_filled = t4_filled
    return result


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _rounds(
    conn: sqlite3.Connection,
    assets: Iterable[str],
    start_ts: int | None,
    end_ts: int | None,
    max_rounds: int | None,
) -> list[tuple]:
    placeholders = ",".join("?" for _ in assets)
    sql = (
        "SELECT asset, window_start, window_end, COALESCE(slug, ''), up_won "
        "FROM rounds "
        f"WHERE asset IN ({placeholders}) "
        "AND kline_ok=1 AND coverage_ok=1 AND up_won IS NOT NULL "
        "AND window_end - window_start = 3600 "
    )
    args: list[Any] = list(assets)
    if start_ts is not None:
        sql += "AND window_start >= ? "
        args.append(int(start_ts))
    if end_ts is not None:
        sql += "AND window_start < ? "
        args.append(int(end_ts))
    sql += "ORDER BY window_start, asset"
    if max_rounds is not None:
        sql += " LIMIT ?"
        args.append(int(max_rounds))
    return list(conn.execute(sql, args))


def _month_key(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m")


def _detail_block(rs: list[RoundResult], p: Params) -> dict[str, Any]:
    """Detailed metrics for one asset / month / tier slice."""
    entries = [r for r in rs if r.exit != "NO_ENTRY"]
    if not entries:
        return {
            "windows": len(rs),
            "entries": 0,
            "no_entry": len(rs),
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "pnl": 0.0,
            "avg": 0.0,
            "profit_factor": None,
            "max_drawdown": 0.0,
            "fees": 0.0,
            "exits": {},
            "t4_entries": 0,
            "t4_pnl": 0.0,
            "t4_win_rate": None,
            "t1_3_pnl": 0.0,
        }
    wins = [r for r in entries if r.pnl > 1e-9]
    losses = [r for r in entries if r.pnl < -1e-9]
    gross_win = sum(r.pnl for r in wins)
    gross_loss = -sum(r.pnl for r in losses)
    cum = peak = max_dd = 0.0
    for r in sorted(rs, key=lambda x: (x.window_start, x.asset)):
        cum += r.pnl
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    exits: dict[str, int] = {}
    for r in entries:
        exits[r.exit] = exits.get(r.exit, 0) + 1
    t4 = [r for r in entries if r.highest_tier == 4]
    t4_wins = [r for r in t4 if r.pnl > 1e-9]
    return {
        "windows": len(rs),
        "entries": len(entries),
        "no_entry": len(rs) - len(entries),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(entries), 4),
        "pnl": round(sum(r.pnl for r in entries), 4),
        "avg": round(sum(r.pnl for r in entries) / len(entries), 4),
        "profit_factor": (
            round(gross_win / gross_loss, 4) if gross_loss > 1e-12 else None
        ),
        "max_drawdown": round(max_dd, 4),
        "fees": round(sum(r.fees for r in rs), 4),
        "exits": exits,
        "t4_entries": len(t4),
        "t4_pnl": round(sum(r.pnl for r in t4), 4),
        "t4_win_rate": (
            round(len(t4_wins) / len(t4), 4) if t4 else None
        ),
        "t1_3_pnl": round(
            sum(r.pnl for r in entries if r.highest_tier < 4), 4
        ),
    }


def _summary(results: list[RoundResult], p: Params) -> dict[str, Any]:
    entries = [r for r in results if r.exit != "NO_ENTRY"]
    no_entry = [r for r in results if r.exit == "NO_ENTRY"]
    wins = [r for r in entries if r.pnl > 1e-9]
    losses = [r for r in entries if r.pnl < -1e-9]
    gross_win = sum(r.pnl for r in wins)
    gross_loss = -sum(r.pnl for r in losses)
    total_pnl = sum(r.pnl for r in results)

    cum = peak = max_dd = 0.0
    for r in sorted(results, key=lambda x: (x.window_start, x.asset)):
        cum += r.pnl
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    exits: dict[str, int] = {}
    for r in entries:
        exits[r.exit] = exits.get(r.exit, 0) + 1

    by_asset: dict[str, dict[str, Any]] = {}
    for asset in sorted({r.asset for r in results}):
        rs = [r for r in results if r.asset == asset]
        by_asset[asset] = _summary_block(rs, p)

    by_highest: dict[str, dict[str, Any]] = {}
    for tier in (1, 2, 3, 4):
        rs = [r for r in entries if r.highest_tier == tier]
        by_highest[f"T{tier}"] = _summary_block(rs, p)

    by_side: dict[str, dict[str, Any]] = {}
    for side in ("UP", "DOWN"):
        rs = [r for r in entries if r.side == side]
        by_side[side] = _summary_block(rs, p)

    by_month: dict[str, dict[str, Any]] = {}
    for month in sorted({_month_key(r.window_start) for r in results}):
        rs = [r for r in results if _month_key(r.window_start) == month]
        by_month[month] = _detail_block(rs, p)

    period = None
    if results:
        period = {
            "start": min(r.window_start for r in results),
            "end": max(r.window_start for r in results) + 3600,
        }

    return {
        "params": dataclasses.asdict(p),
        "windows": len(results),
        "entries": len(entries),
        "no_entry": len(no_entry),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(entries)) if entries else None,
        "pnl_total": round(total_pnl, 4),
        "pnl_avg_per_window": (
            round(total_pnl / len(results), 4) if results else None
        ),
        "pnl_avg_per_entry": (
            round(total_pnl / len(entries), 4) if entries else None
        ),
        "gross_win": round(gross_win, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": (
            round(gross_win / gross_loss, 4) if gross_loss > 1e-12 else None
        ),
        "max_drawdown": round(max_dd, 4),
        "fees_total": round(sum(r.fees for r in results), 4),
        "exits": exits,
        "by_asset": by_asset,
        "by_highest_tier": by_highest,
        "by_side": by_side,
        "by_month": by_month,
        "period": period,
    }


def _summary_block(rs: list[RoundResult], p: Params) -> dict[str, Any]:
    entries = [r for r in rs if r.exit != "NO_ENTRY"]
    if not entries:
        return {
            "windows": len(rs),
            "entries": 0,
            "n": 0,
            "pnl": 0.0,
            "win_rate": None,
        }
    wins = [r for r in entries if r.pnl > 1e-9]
    losses = [r for r in entries if r.pnl < -1e-9]
    gross_win = sum(r.pnl for r in wins)
    gross_loss = -sum(r.pnl for r in losses)
    return {
        "windows": len(rs),
        "entries": len(entries),
        "n": len(entries),
        "wins": len(wins),
        "losses": len(losses),
        "pnl": round(sum(r.pnl for r in entries), 4),
        "avg": round(sum(r.pnl for r in entries) / len(entries), 4),
        "win_rate": round(len(wins) / len(entries), 4),
        "profit_factor": (
            round(gross_win / gross_loss, 4) if gross_loss > 1e-12 else None
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_ts(raw: str | None) -> int | None:
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.strptime(raw, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _print_summary(s: dict[str, Any]) -> None:
    print("\n=== overall ===")
    print(
        f"windows={s['windows']} entries={s['entries']} "
        f"no_entry={s['no_entry']} win_rate={s['win_rate']} "
        f"pnl={s['pnl_total']:+.2f} avg/entry={s['pnl_avg_per_entry']} "
        f"pf={s['profit_factor']} max_dd={s['max_drawdown']:+.2f} "
        f"fees={s['fees_total']:.2f}"
    )
    print("exits:", s["exits"])
    print("\n=== by asset ===")
    print(
        f"{'asset':6} {'win':>5} {'entry':>6} {'no_ent':>6} "
        f"{'win%':>7} {'pnl':>10} {'avg':>9} {'pf':>7}"
    )
    for asset, b in s["by_asset"].items():
        print(
            f"{asset:6} {b.get('wins', 0):5d} {b.get('entries', 0):6d} "
            f"{b.get('windows', 0) - b.get('entries', 0):6d} "
            f"{b.get('win_rate') if b.get('win_rate') is not None else 0:7.3f} "
            f"{b.get('pnl', 0.0):+10.2f} {b.get('avg', 0.0):+9.3f} "
            f"{b.get('profit_factor') if b.get('profit_factor') is not None else 0:7.2f}"
        )
    print("\n=== by highest tier ===")
    for tier, b in s["by_highest_tier"].items():
        if b.get("entries", 0):
            print(
                f"{tier}: n={b['entries']:5d} pnl={b['pnl']:+9.2f} "
                f"win_rate={b['win_rate']:.3f} avg={b['avg']:+.3f}"
            )
    print("\n=== by side ===")
    for side, b in s["by_side"].items():
        if b.get("entries", 0):
            print(
                f"{side}: n={b['entries']:5d} pnl={b['pnl']:+9.2f} "
                f"win_rate={b['win_rate']:.3f}"
            )
    months = s.get("by_month") or {}
    if months:
        print("\n=== by month ===")
        print(
            f"{'month':8} {'win':>5} {'entry':>6} {'no_ent':>6} "
            f"{'win%':>7} {'pnl':>10} {'avg':>8} {'pf':>6} "
            f"{'maxdd':>9} {'T4n':>5} {'T4pnl':>10} {'T4win%':>7}"
        )
        for month, b in months.items():
            wr = b["win_rate"]
            t4wr = b["t4_win_rate"]
            pf = b["profit_factor"]
            print(
                f"{month:8} {b['wins']:5d} {b['entries']:6d} "
                f"{b['no_entry']:6d} "
                f"{(wr if wr is not None else 0):7.3f} "
                f"{b['pnl']:+10.2f} {b['avg']:+8.3f} "
                f"{(pf if pf is not None else 0):6.2f} "
                f"{b['max_drawdown']:+9.2f} {b['t4_entries']:5d} "
                f"{b['t4_pnl']:+10.2f} "
                f"{(t4wr if t4wr is not None else 0):7.3f}"
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="SQLite history database")
    ap.add_argument("--assets", default=",".join(ASSETS))
    ap.add_argument("--start", default=None, help="ISO date or unix ts")
    ap.add_argument("--end", default=None, help="ISO date or unix ts (exclusive)")
    ap.add_argument("--max-rounds", type=int, default=None)
    ap.add_argument("--output", default=None, help="write the JSON report here")
    ap.add_argument(
        "--trades-csv", default=None,
        help="write one row per replayed window to this CSV",
    )
    ap.add_argument("--progress", type=int, default=0, help="progress every N rounds")
    ap.add_argument("--entry-price-cap", type=float, default=Params.entry_price_cap)
    ap.add_argument("--price-pre-margin", type=float, default=Params.price_pre_margin)
    ap.add_argument("--t1-notional", type=float, default=Params.t1_notional)
    ap.add_argument("--t2-notional", type=float, default=Params.t2_notional)
    ap.add_argument("--t3-notional", type=float, default=Params.t3_notional)
    ap.add_argument("--t4-notional", type=float, default=Params.t4_notional)
    ap.add_argument("--drop2", type=float, default=Params.drop2)
    ap.add_argument("--drop3", type=float, default=Params.drop3)
    ap.add_argument("--drop4", type=float, default=Params.drop4)
    ap.add_argument("--t1-entry-window-s", type=float, default=Params.t1_entry_window_s)
    ap.add_argument("--gtd-lifetime-s", type=float, default=Params.gtd_lifetime_s)
    ap.add_argument("--tp-profit-pct", type=float, default=Params.tp_profit_pct)
    ap.add_argument("--tp-decrement-pct", type=float, default=Params.tp_decrement_pct)
    ap.add_argument("--tp-min-pct", type=float, default=Params.tp_min_pct)
    ap.add_argument("--taker-fee-rate", type=float, default=Params.taker_fee_rate)
    ap.add_argument(
        "--no-stop-loss", action="store_true", help="disable the T4 stop-loss"
    )
    ap.add_argument("--sl-t4-drop-pct", type=float, default=Params.sl_t4_drop_pct)
    ap.add_argument("--sl-confirm-s", type=float, default=Params.sl_confirm_s)
    ap.add_argument("--sl-stage1-sell-pct", type=float, default=Params.sl_stage1_sell_pct)
    ap.add_argument(
        "--sl-stage2-extra-drop-pct", type=float,
        default=Params.sl_stage2_extra_drop_pct,
    )
    args = ap.parse_args(argv)

    params = dataclasses.replace(
        Params(),
        entry_price_cap=args.entry_price_cap,
        price_pre_margin=args.price_pre_margin,
        t1_notional=args.t1_notional,
        t2_notional=args.t2_notional,
        t3_notional=args.t3_notional,
        t4_notional=args.t4_notional,
        drop2=args.drop2,
        drop3=args.drop3,
        drop4=args.drop4,
        t1_entry_window_s=args.t1_entry_window_s,
        gtd_lifetime_s=args.gtd_lifetime_s,
        tp_profit_pct=args.tp_profit_pct,
        tp_decrement_pct=args.tp_decrement_pct,
        tp_min_pct=args.tp_min_pct,
        taker_fee_rate=args.taker_fee_rate,
        stop_loss_enabled=not args.no_stop_loss,
        sl_t4_drop_pct=args.sl_t4_drop_pct,
        sl_confirm_s=args.sl_confirm_s,
        sl_stage1_sell_pct=args.sl_stage1_sell_pct,
        sl_stage2_extra_drop_pct=args.sl_stage2_extra_drop_pct,
    )
    assets = tuple(
        a.strip().upper() for a in args.assets.split(",") if a.strip()
    )
    start_ts = _parse_ts(args.start)
    end_ts = _parse_ts(args.end)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rounds = _rounds(conn, assets, start_ts, end_ts, args.max_rounds)
    print(
        f"db={args.db} assets={assets} rounds={len(rounds)} "
        f"start={start_ts} end={end_ts}"
    )
    print("params:", json.dumps(dataclasses.asdict(params), sort_keys=True))

    results: list[RoundResult] = []
    skipped = 0
    t0 = time.time()
    for n, (asset, ws, we, slug, up_won) in enumerate(rounds, 1):
        points = load_series(conn, asset, int(ws), int(we))
        if not points:
            skipped += 1
            continue
        results.append(
            replay(
                asset, int(ws), int(we), slug or "",
                bool(up_won), points, params,
            )
        )
        if args.progress and n % args.progress == 0:
            elapsed = time.time() - t0
            print(
                f"  {n}/{len(rounds)} kept={len(results)} "
                f"skipped={skipped} {n/max(elapsed,1e-9):.1f}/s",
                flush=True,
            )
    conn.close()

    summary = _summary(results, params)
    summary["skipped_no_prices"] = skipped
    summary["elapsed_s"] = round(time.time() - t0, 2)
    _print_summary(summary)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.output}")
    if args.trades_csv:
        with open(args.trades_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "month", "asset", "window_start_utc", "slug", "side",
                "entry_utc", "entry_price", "exit", "pnl", "fees",
                "cost", "proceeds", "qty_left", "highest_tier",
                "t4_filled", "n_fills",
            ])
            for r in results:
                writer.writerow([
                    _month_key(r.window_start),
                    r.asset,
                    datetime.fromtimestamp(
                        r.window_start, tz=timezone.utc
                    ).isoformat(),
                    r.slug,
                    r.side or "",
                    (
                        datetime.fromtimestamp(
                            r.entry_ts, tz=timezone.utc
                        ).isoformat()
                        if r.entry_ts else ""
                    ),
                    "" if r.entry_price is None else round(r.entry_price, 6),
                    r.exit,
                    round(r.pnl, 6),
                    round(r.fees, 6),
                    round(r.cost, 6),
                    round(r.proceeds, 6),
                    round(r.qty_left, 6),
                    r.highest_tier,
                    int(r.t4_filled),
                    r.n_fills,
                ])
        print(f"wrote {args.trades_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
