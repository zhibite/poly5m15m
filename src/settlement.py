"""Settlement watcher and payoff attribution.

Every open position stores the two legs (interval, side, bought/sold
sizes, condition id). Once Gamma reports both markets resolved, the
watcher pays each held share whose side won and books the final PnL:

    pnl = payout + unwind_proceeds - buy_cost - fees

It also asserts the strategy's core invariant:

  * cross-market: payout >= matched shares (at least one leg wins for
    every matched pair), and
  * same-market: payout == held shares (exactly one of UP/DOWN wins).

A violation is a loud, persistent error: it means the strike ordering or
the shared-settlement assumption was wrong.
"""
from __future__ import annotations

import asyncio
import json
import time

from src.config import ArbSettings
from src.store import ArbStore
from src.infra.logging import get_logger
from src.market.pm_client import PolymarketClient

log = get_logger(__name__)

_EPS = 1e-6


class SettlementWatcher:
    def __init__(
        self, pm: PolymarketClient, store: ArbStore, cfg: ArbSettings
    ) -> None:
        self._pm = pm
        self._store = store
        self._cfg = cfg
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._last_warn: dict[int, float] = {}

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("arb.settlement.poll_crashed", error=str(exc))
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=max(1.0, self._cfg.SETTLEMENT_POLL_S)
                )
            except asyncio.TimeoutError:
                pass

    async def poll_once(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        settled = 0
        for pos in self._store.open_positions():
            end_ts = int(pos.get("end_ts") or 0)
            if now < end_ts + 1.0:
                continue
            try:
                settled += await self._settle_one(pos, now)
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "arb.settlement.position_failed",
                    position_id=pos.get("id"),
                    error=str(exc),
                )
        return settled

    async def _settle_one(self, pos: dict, now: float) -> int:
        position_id = int(pos["id"])
        detail = _load_detail(pos.get("detail_json"))
        if detail.get("leg_risk"):
            log.error(
                "arb.settlement.leg_risk_position",
                position_id=position_id,
                kind=pos.get("kind"),
                matched=pos.get("shares"),
                note="entry had an unmatched leg or a failed unwind; this "
                     "position was NOT payoff-risk-free",
            )
        legs = detail.get("legs") or []
        if not legs:
            log.error(
                "arb.settlement.missing_legs",
                position_id=position_id,
                note="position has no leg metadata; cannot attribute payoff",
            )
            return 0

        resolutions: dict[str, dict] = {}
        for leg in legs:
            condition_id = str(leg.get("condition_id") or "")
            if not condition_id:
                log.warning(
                    "arb.settlement.missing_condition",
                    position_id=position_id,
                    leg=leg,
                )
                return 0
            resolution = await self._resolve(condition_id, str(leg.get("slug") or ""))
            if not resolution or not resolution.get("resolved"):
                self._warn_waiting(position_id, pos, now)
                return 0
            resolutions[condition_id] = resolution

        payout = 0.0
        total_held = 0.0
        per_leg: list[dict] = []
        for leg in legs:
            condition_id = str(leg.get("condition_id") or "")
            resolution = resolutions[condition_id]
            up_wins = bool(resolution.get("up_wins"))
            side = str(leg.get("side") or "")
            bought = float(leg.get("bought") or 0.0)
            sold = float(leg.get("sold") or 0.0)
            held = max(0.0, bought - sold)
            total_held += held
            won = (side == "UP" and up_wins) or (side == "DOWN" and not up_wins)
            leg_payout = held if won else 0.0
            payout += leg_payout
            per_leg.append(
                {
                    "interval": leg.get("interval"),
                    "side": side,
                    "held": held,
                    "up_wins": up_wins,
                    "won": won,
                    "payout": leg_payout,
                }
            )

        buy_cost = float(pos.get("buy_cost") or 0.0)
        sell_proceeds = float(pos.get("sell_proceeds") or 0.0)
        fees = float(pos.get("fees") or 0.0)
        pnl = payout + sell_proceeds - buy_cost - fees

        kind = str(pos.get("kind") or "")
        matched = float(pos.get("shares") or 0.0)
        if kind == "cross":
            # At least one of the two directional legs wins per matched set.
            invariant_ok = payout + _EPS >= matched
        elif kind == "complete":
            # One UP and one DOWN win per market: exactly 2 per matched set.
            invariant_ok = payout + _EPS >= 2.0 * matched
        else:
            # Same-market complete-set: exactly one side wins per share.
            invariant_ok = abs(payout - total_held) <= _EPS

        if not invariant_ok:
            log.error(
                "arb.settlement.invariant_violation",
                position_id=position_id,
                kind=kind,
                payout=payout,
                matched=matched,
                total_held=total_held,
                strikes=(detail.get("strike_5m"), detail.get("strike_15m")),
                gap_bps=detail.get("gap_bps"),
                legs=per_leg,
                note="strike ordering or shared-settlement assumption failed",
            )

        up_won_5m = _up_wins_for(per_leg, "5m")
        up_won_15m = _up_wins_for(per_leg, "15m")
        self._store.settle_position(
            position_id,
            end_ts=int(pos.get("end_ts") or 0),
            up_won_5m=up_won_5m,
            up_won_15m=up_won_15m,
            payout=payout,
            pnl=pnl,
            invariant_ok=invariant_ok,
            detail={
                "legs": per_leg,
                "resolutions": {
                    cid: {
                        "resolved": r.get("resolved"),
                        "up_wins": r.get("up_wins"),
                        "outcome": r.get("outcome"),
                    }
                    for cid, r in resolutions.items()
                },
                "payout": payout,
                "total_held": total_held,
                "invariant_ok": invariant_ok,
            },
        )
        log.info(
            "arb.settlement.done",
            position_id=position_id,
            kind=kind,
            payout=payout,
            pnl=pnl,
            invariant_ok=invariant_ok,
            legs=per_leg,
        )
        return 1

    async def _resolve(self, condition_id: str, slug: str) -> dict | None:
        now = time.time()
        cached = self._cache.get(condition_id)
        if cached is not None and now - cached[0] < max(2.0, self._cfg.SETTLEMENT_POLL_S):
            return cached[1]
        try:
            resolution = await self._pm.get_market_resolution(condition_id, slug)
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "arb.settlement.resolve_failed",
                condition_id=condition_id[:12],
                error=str(exc),
            )
            resolution = None
        self._cache[condition_id] = (now, resolution)
        return resolution

    def _warn_waiting(self, position_id: int, pos: dict, now: float) -> None:
        end_ts = int(pos.get("end_ts") or 0)
        if now - end_ts < self._cfg.SETTLEMENT_MAX_WAIT_S:
            return
        last = self._last_warn.get(position_id, 0.0)
        if now - last < 60.0:
            return
        self._last_warn[position_id] = now
        log.warning(
            "arb.settlement.still_waiting",
            position_id=position_id,
            seconds_since_end=now - end_ts,
            note="Gamma has not reported a terminal outcome yet",
        )


def _load_detail(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _up_wins_for(per_leg: list[dict], interval: str) -> bool | None:
    for leg in per_leg:
        if leg.get("interval") == interval:
            value = leg.get("up_wins")
            return bool(value) if value is not None else None
    return None
