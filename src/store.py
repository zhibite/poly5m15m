"""SQLite persistence for the arbitrage bot.

A separate database file (``data/arb.db`` by default) keeps this strategy's
schema independent of the legacy 1h bot's ``polymarket.db``. WAL mode lets
the dashboard read while the engine writes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.models import ArbMarket, ArbPair, Fill, Opportunity
from src.infra.logging import get_logger

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS arb_markets (
  asset TEXT NOT NULL,
  interval TEXT NOT NULL,
  start_ts INTEGER NOT NULL,
  end_ts INTEGER NOT NULL,
  slug TEXT,
  condition_id TEXT,
  token_up TEXT,
  token_down TEXT,
  tick_size REAL DEFAULT 0.01,
  neg_risk INTEGER DEFAULT 0,
  resolution_source TEXT DEFAULT 'unknown',
  strike REAL,
  strike_source TEXT,
  description TEXT,
  raw_json TEXT,
  discovered_at TEXT,
  PRIMARY KEY (asset, interval, start_ts)
);

CREATE TABLE IF NOT EXISTS arb_pairs (
  asset TEXT NOT NULL,
  end_ts INTEGER NOT NULL,
  start_5m INTEGER,
  start_15m INTEGER,
  strike_5m REAL,
  strike_15m REAL,
  gap_bps REAL,
  direction_5m TEXT,
  direction_15m TEXT,
  resolution_ok INTEGER DEFAULT 0,
  ready INTEGER DEFAULT 0,
  reason TEXT,
  updated_at TEXT,
  PRIMARY KEY (asset, end_ts)
);

CREATE TABLE IF NOT EXISTS arb_opportunities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  asset TEXT NOT NULL,
  end_ts INTEGER,
  kind TEXT NOT NULL,
  direction_5m TEXT,
  direction_15m TEXT,
  shares REAL,
  gross_cost REAL,
  fees REAL,
  floor_profit REAL,
  edge_pct REAL,
  executed INTEGER DEFAULT 0,
  detail_json TEXT
);

CREATE TABLE IF NOT EXISTS arb_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opened_ms INTEGER NOT NULL,
  asset TEXT NOT NULL,
  end_ts INTEGER NOT NULL,
  kind TEXT NOT NULL,
  shares REAL NOT NULL,
  buy_cost REAL NOT NULL,
  sell_proceeds REAL DEFAULT 0,
  fees REAL DEFAULT 0,
  status TEXT DEFAULT 'open',
  pnl REAL,
  detail_json TEXT,
  settled_ms INTEGER
);

CREATE TABLE IF NOT EXISTS arb_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL,
  token_id TEXT,
  side TEXT,
  price REAL,
  size REAL,
  fee REAL,
  order_id TEXT,
  role TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS arb_settlements (
  position_id INTEGER PRIMARY KEY,
  end_ts INTEGER,
  up_won_5m INTEGER,
  up_won_15m INTEGER,
  payout REAL,
  pnl REAL,
  invariant_ok INTEGER,
  resolved_ms INTEGER,
  detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_arb_positions_status
  ON arb_positions(status, end_ts);
CREATE INDEX IF NOT EXISTS idx_arb_fills_position
  ON arb_fills(position_id);
CREATE INDEX IF NOT EXISTS idx_arb_opp_ts
  ON arb_opportunities(ts_ms);
"""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class ArbStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        log.info("arb.store.ready", path=str(self.path))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── markets / pairs ────────────────────────────────────────────────
    def upsert_market(self, m: ArbMarket) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO arb_markets (
                  asset, interval, start_ts, end_ts, slug, condition_id,
                  token_up, token_down, tick_size, neg_risk,
                  resolution_source, strike, strike_source, description,
                  raw_json, discovered_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(asset, interval, start_ts) DO UPDATE SET
                  end_ts=excluded.end_ts,
                  slug=excluded.slug,
                  condition_id=excluded.condition_id,
                  token_up=excluded.token_up,
                  token_down=excluded.token_down,
                  tick_size=excluded.tick_size,
                  neg_risk=excluded.neg_risk,
                  resolution_source=excluded.resolution_source,
                  strike=excluded.strike,
                  strike_source=excluded.strike_source,
                  description=excluded.description,
                  raw_json=excluded.raw_json,
                  discovered_at=excluded.discovered_at
                """,
                (
                    m.asset,
                    m.interval,
                    int(m.start_ts),
                    int(m.end_ts),
                    m.slug,
                    m.condition_id,
                    m.token_up,
                    m.token_down,
                    float(m.tick_size),
                    1 if m.neg_risk else 0,
                    m.resolution_source,
                    m.strike,
                    m.strike_source,
                    m.description,
                    _json(m.raw),
                    _iso_now(),
                ),
            )
            self._conn.commit()

    def upsert_pair(self, p: ArbPair) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO arb_pairs (
                  asset, end_ts, start_5m, start_15m, strike_5m,
                  strike_15m, gap_bps, direction_5m, direction_15m,
                  resolution_ok, ready, reason, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(asset, end_ts) DO UPDATE SET
                  start_5m=excluded.start_5m,
                  start_15m=excluded.start_15m,
                  strike_5m=excluded.strike_5m,
                  strike_15m=excluded.strike_15m,
                  gap_bps=excluded.gap_bps,
                  direction_5m=excluded.direction_5m,
                  direction_15m=excluded.direction_15m,
                  resolution_ok=excluded.resolution_ok,
                  ready=excluded.ready,
                  reason=excluded.reason,
                  updated_at=excluded.updated_at
                """,
                (
                    p.asset,
                    int(p.end_ts),
                    int(p.m5.start_ts),
                    int(p.m15.start_ts),
                    p.strike_5m,
                    p.strike_15m,
                    p.gap_bps,
                    p.direction_5m.value if p.direction_5m else None,
                    p.direction_15m.value if p.direction_15m else None,
                    1 if p.resolution_ok else 0,
                    1 if p.ready else 0,
                    p.reason,
                    _iso_now(),
                ),
            )
            self._conn.commit()

    # ── opportunities ──────────────────────────────────────────────────
    def record_opportunity(self, o: Opportunity) -> int:
        directions = {}
        for leg in o.legs:
            directions[leg.interval] = leg.side.value
        detail = dict(o.detail)
        detail["legs"] = [
            {
                "token_id": leg.token_id,
                "side": leg.side.value,
                "interval": leg.interval,
                "shares": leg.shares,
                "avg_price": leg.avg_price,
                "worst_price": leg.worst_price,
                "notional": leg.notional,
            }
            for leg in o.legs
        ]
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO arb_opportunities (
                  ts_ms, asset, end_ts, kind, direction_5m, direction_15m,
                  shares, gross_cost, fees, floor_profit, edge_pct,
                  executed, detail_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(o.ts * 1000),
                    o.asset,
                    int(o.end_ts),
                    o.kind.value,
                    directions.get("5m"),
                    directions.get("15m"),
                    o.shares,
                    o.gross_cost,
                    o.fees,
                    o.floor_profit,
                    o.edge_pct,
                    0,
                    _json(detail),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def mark_opportunity_executed(self, opp_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE arb_opportunities SET executed=1 WHERE id=?", (opp_id,)
            )
            self._conn.commit()

    def recent_opportunities(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM arb_opportunities ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── positions / fills ──────────────────────────────────────────────
    def create_position(
        self,
        *,
        asset: str,
        end_ts: int,
        kind: str,
        shares: float,
        buy_cost: float,
        sell_proceeds: float,
        fees: float,
        detail: dict,
        status: str = "open",
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO arb_positions (
                  opened_ms, asset, end_ts, kind, shares, buy_cost,
                  sell_proceeds, fees, status, detail_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(time.time() * 1000),
                    asset,
                    int(end_ts),
                    kind,
                    float(shares),
                    float(buy_cost),
                    float(sell_proceeds),
                    float(fees),
                    status,
                    _json(detail),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def add_fill(self, position_id: int, f: Fill) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO arb_fills (
                  position_id, ts_ms, token_id, side, price, size, fee,
                  order_id, role, note
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(position_id),
                    int((f.ts or time.time()) * 1000),
                    f.token_id,
                    f.side,
                    float(f.price),
                    float(f.size),
                    float(f.fee),
                    f.order_id,
                    f.role,
                    f.note,
                ),
            )
            self._conn.commit()

    def update_position(self, position_id: int, **fields: Any) -> None:
        allowed = {
            "shares",
            "buy_cost",
            "sell_proceeds",
            "fees",
            "status",
            "pnl",
            "detail_json",
            "settled_ms",
        }
        sets = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        values.append(int(position_id))
        with self._lock:
            self._conn.execute(
                f"UPDATE arb_positions SET {', '.join(sets)} WHERE id=?",
                values,
            )
            self._conn.commit()

    def open_positions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM arb_positions WHERE status='open' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_positions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM arb_positions ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def position_fills(self, position_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM arb_fills WHERE position_id=? ORDER BY id",
                (int(position_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_fills(self, limit: int = 50) -> list[dict]:
        """Individual fills with their position's asset/kind for the dashboard."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT f.id, f.position_id, f.ts_ms, f.token_id, f.side,
                       f.price, f.size, f.fee, f.order_id, f.role, f.note,
                       p.asset, p.kind, p.status AS position_status
                FROM arb_fills f
                LEFT JOIN arb_positions p ON p.id = f.position_id
                ORDER BY f.id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def settle_position(
        self,
        position_id: int,
        *,
        end_ts: int,
        up_won_5m: bool | None,
        up_won_15m: bool | None,
        payout: float,
        pnl: float,
        invariant_ok: bool,
        detail: dict | None = None,
    ) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO arb_settlements (
                  position_id, end_ts, up_won_5m, up_won_15m, payout, pnl,
                  invariant_ok, resolved_ms, detail_json
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(position_id) DO UPDATE SET
                  end_ts=excluded.end_ts,
                  up_won_5m=excluded.up_won_5m,
                  up_won_15m=excluded.up_won_15m,
                  payout=excluded.payout,
                  pnl=excluded.pnl,
                  invariant_ok=excluded.invariant_ok,
                  resolved_ms=excluded.resolved_ms,
                  detail_json=excluded.detail_json
                """,
                (
                    int(position_id),
                    int(end_ts),
                    None if up_won_5m is None else (1 if up_won_5m else 0),
                    None if up_won_15m is None else (1 if up_won_15m else 0),
                    float(payout),
                    float(pnl),
                    1 if invariant_ok else 0,
                    now_ms,
                    _json(detail or {}),
                ),
            )
            self._conn.execute(
                """
                UPDATE arb_positions
                SET status='settled', pnl=?, settled_ms=?
                WHERE id=?
                """,
                (float(pnl), now_ms, int(position_id)),
            )
            self._conn.commit()

    # ── reporting ──────────────────────────────────────────────────────
    def fees_total(self) -> float:
        """Sum of every recorded per-fill fee (open + settled positions)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(fee), 0) AS total FROM arb_fills"
            ).fetchone()
        return float(row["total"] if row else 0.0)

    def fees_today(self) -> float:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(start.timestamp() * 1000)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(fee), 0) AS total
                FROM arb_fills WHERE ts_ms >= ?
                """,
                (start_ms,),
            ).fetchone()
        return float(row["total"] if row else 0.0)

    def realized_pnl_today(self) -> float:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(start.timestamp() * 1000)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(pnl), 0) AS total
                FROM arb_positions
                WHERE status='settled' AND settled_ms >= ?
                """,
                (start_ms,),
            ).fetchone()
        return float(row["total"] if row else 0.0)

    def stats(self) -> dict:
        with self._lock:
            open_row = self._conn.execute(
                """
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(buy_cost - sell_proceeds), 0) AS cost
                FROM arb_positions WHERE status='open'
                """
            ).fetchone()
            settled_row = self._conn.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(pnl), 0) AS pnl,
                       COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins
                FROM arb_positions WHERE status='settled'
                """
            ).fetchone()
            opp_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM arb_opportunities"
            ).fetchone()
        return {
            "open_positions": int(open_row["n"] or 0),
            "open_cost": float(open_row["cost"] or 0.0),
            "settled_positions": int(settled_row["n"] or 0),
            "realized_pnl": float(settled_row["pnl"] or 0.0),
            "wins": int(settled_row["wins"] or 0),
            "opportunities": int(opp_row["n"] or 0),
            "realized_today": self.realized_pnl_today(),
            "fees_total": self.fees_total(),
            "fees_today": self.fees_today(),
        }
