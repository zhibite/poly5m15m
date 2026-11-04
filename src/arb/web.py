"""Minimal dashboard for the arbitrage bot.

The page is served by the same process on ``ARB_WEB_PORT`` (8031). It is
read-only: the trading kill switch is a file on disk and is managed
separately, so a web compromise cannot place orders.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from src.arb.config import ArbSettings

_security = HTTPBasic(auto_error=False)


def _load(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {"error": "status file not written yet"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"status unreadable: {exc}"}


def create_app(status_path: Path, cfg: ArbSettings) -> FastAPI:
    app = FastAPI(title="Polymarket 5m/15m Arbitrage", docs_url=None, redoc_url=None)

    def auth(
        creds: HTTPBasicCredentials | None = Depends(_security),
    ) -> None:
        password = cfg.effective_web_password
        if not password:
            return
        expected_user = cfg.effective_web_username
        if creds is None:
            raise HTTPException(
                status_code=401,
                detail="authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        ok_user = secrets.compare_digest(creds.username or "", expected_user)
        ok_pass = secrets.compare_digest(creds.password or "", password)
        if not (ok_user and ok_pass):
            raise HTTPException(
                status_code=401,
                detail="invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/api/status", dependencies=[Depends(auth)])
    async def api_status() -> JSONResponse:
        return JSONResponse(_load(status_path))

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    async def index() -> HTMLResponse:
        return HTMLResponse(_PAGE)

    return app


_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Polymarket 5m/15m Arbitrage</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       background: #0b0f14; color: #d7e0ea; }
header { padding: 14px 18px; border-bottom: 1px solid #1d2833; display: flex;
         align-items: center; gap: 14px; flex-wrap: wrap; }
h1 { font-size: 15px; margin: 0; letter-spacing: .04em; color: #7fd1ff; }
.badge { padding: 2px 8px; border-radius: 999px; border: 1px solid #2b3a49; color: #9fb3c8; }
.badge.paper { border-color: #8a6d1f; color: #e8c15a; }
.badge.live { border-color: #8f2f2f; color: #ff7b72; }
.badge.ok { border-color: #1f6f4a; color: #56d364; }
.badge.bad { border-color: #8f2f2f; color: #ff7b72; }
main { padding: 16px 18px 40px; display: grid; gap: 16px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.card { background: #111821; border: 1px solid #1d2833; border-radius: 8px; padding: 10px 12px; }
.card .k { color: #7d8fa3; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
.card .v { font-size: 18px; margin-top: 4px; }
.pos { color: #56d364; } .neg { color: #ff7b72; }
section { background: #0f151d; border: 1px solid #1d2833; border-radius: 8px; overflow: hidden; }
section h2 { margin: 0; padding: 9px 12px; font-size: 12px; color: #9fb3c8;
             text-transform: uppercase; letter-spacing: .08em; border-bottom: 1px solid #1d2833; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #16202a; white-space: nowrap; }
th { color: #7d8fa3; font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }
tr:last-child td { border-bottom: none; }
.muted { color: #7d8fa3; }
footer { padding: 0 18px 24px; color: #7d8fa3; font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>POLYMARKET 5m/15m ARBITRAGE</h1>
  <span id="mode" class="badge">—</span>
  <span id="ks" class="badge">—</span>
  <span id="updated" class="muted">—</span>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="k">Realized today</div><div id="pnl-today" class="v">—</div></div>
    <div class="card"><div class="k">Open positions</div><div id="open-pos" class="v">—</div></div>
    <div class="card"><div class="k">Open cost</div><div id="open-cost" class="v">—</div></div>
    <div class="card"><div class="k">Opportunities</div><div id="opps" class="v">—</div></div>
    <div class="card"><div class="k">Fees paid</div><div id="fees-total" class="v">—</div></div>
    <div class="card"><div class="k">Fee rate</div><div id="fee-rate" class="v">—</div></div>
  </div>
  <section>
    <h2>Pairs</h2>
    <table><thead><tr><th>Asset</th><th>End</th><th>5m strike</th><th>15m strike</th>
    <th>Gap bps</th><th>Direction</th><th>Ask sum</th><th>Modes</th><th>Left s</th></tr></thead>
    <tbody id="pairs"><tr><td colspan="9" class="muted">waiting for discovery…</td></tr></tbody></table>
  </section>
  <section>
    <h2>Recent positions</h2>
    <table><thead><tr><th>ID</th><th>Asset</th><th>Kind</th><th>Shares</th>
    <th>Buy cost</th><th>Sell proceeds</th><th>Fees</th><th>Status</th><th>PnL</th></tr></thead>
    <tbody id="positions"><tr><td colspan="9" class="muted">none</td></tr></tbody></table>
  </section>
  <section>
    <h2>Recent fills</h2>
    <table><thead><tr><th>Time</th><th>Asset</th><th>Kind</th><th>Side</th>
    <th>Price</th><th>Size</th><th>Fee</th><th>Role</th><th>Pos</th><th>Order</th></tr></thead>
    <tbody id="fills"><tr><td colspan="10" class="muted">none</td></tr></tbody></table>
  </section>
  <section>
    <h2>Recent opportunities</h2>
    <table><thead><tr><th>Time</th><th>Asset</th><th>Kind</th><th>Shares</th>
    <th>Cost</th><th>Est. fees</th><th>Floor profit</th><th>Edge</th><th>Executed</th></tr></thead>
    <tbody id="opportunities"><tr><td colspan="9" class="muted">none</td></tr></tbody></table>
  </section>
</main>
<footer id="health">—</footer>
<script>
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = (v, d=4) => (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(d);
const money = (v) => (v === null || v === undefined || isNaN(v)) ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(4);
const ts = (s) => s ? new Date(s * 1000).toLocaleTimeString() : '—';

function row(cols) { return '<tr>' + cols.map(c => '<td>' + c + '</td>').join('') + '</tr>'; }

async function tick() {
  try {
    const r = await fetch('/api/status', {cache: 'no-store'});
    const s = await r.json();
    if (s.error) { $('health').textContent = s.error; return; }
    $('mode').textContent = s.mode || '—';
    $('mode').className = 'badge ' + (s.mode === 'live' ? 'live' : 'paper');
    $('ks').textContent = s.killswitch?.active ? 'STOPPED' : 'running';
    $('ks').className = 'badge ' + (s.killswitch?.active ? 'bad' : 'ok');
    $('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
    const stats = s.stats || {};
    $('pnl-today').textContent = money(stats.realized_today);
    $('pnl-today').className = 'v ' + ((stats.realized_today || 0) >= 0 ? 'pos' : 'neg');
    $('open-pos').textContent = stats.open_positions ?? '—';
    $('open-cost').textContent = num(stats.open_cost, 4);
    $('opps').textContent = stats.opportunities ?? '—';
    $('fees-total').textContent = num(stats.fees_total, 4);
    $('fee-rate').textContent = s.fee_rate ?? '—';

    const pairs = s.pairs || [];
    $('pairs').innerHTML = pairs.length ? pairs.map(p => {
      const modes = [];
      if (p.ready) modes.push('2-leg');
      if (p.complete_ready) modes.push('4-leg');
      const modeCell = modes.length
        ? '<span class="pos">' + modes.join('+') + '</span>'
        : esc(p.reason || 'no');
      return row([
        esc(p.asset), esc(p.end_utc), num(p.strike_5m, 2), num(p.strike_15m, 2),
        num(p.gap_bps, 1), esc(p.direction), num(p.ask_sum, 3),
        modeCell, num(p.seconds_left, 0)
      ]);
    }).join('') : '<tr><td colspan="9" class="muted">no pairs</td></tr>';

    const positions = s.positions || [];
    $('positions').innerHTML = positions.length ? positions.map(p => row([
      esc(p.id), esc(p.asset), esc(p.kind), num(p.shares, 2), num(p.buy_cost, 4),
      num(p.sell_proceeds, 4), num(p.fees, 4),
      p.status === 'open' ? '<span class="pos">open</span>' : esc(p.status),
      (p.pnl === null || p.pnl === undefined) ? '—'
        : `<span class="${p.pnl >= 0 ? 'pos' : 'neg'}">${money(p.pnl)}</span>`
    ])).join('') : '<tr><td colspan="9" class="muted">none</td></tr>';

    const fills = s.fills || [];
    $('fills').innerHTML = fills.length ? fills.map(f => row([
      ts(f.ts), esc(f.asset), esc(f.kind), esc(f.side), num(f.price, 4),
      num(f.size, 2), num(f.fee, 4), esc(f.role), esc(f.position_id),
      esc(f.order_id || '—')
    ])).join('') : '<tr><td colspan="10" class="muted">none</td></tr>';

    const opps = s.opportunities || [];
    $('opportunities').innerHTML = opps.length ? opps.map(o => row([
      ts(o.ts), esc(o.asset), esc(o.kind), num(o.shares, 2), num(o.gross_cost, 4),
      num(o.fees, 4), money(o.floor_profit), num(o.edge_pct * 100, 2) + '%',
      o.executed ? 'yes' : 'no'
    ])).join('') : '<tr><td colspan="9" class="muted">none</td></tr>';

    const h = s.health || {};
    $('health').textContent = `ws=${h.ws_connected ? 'up' : 'down'} · last discovery ${h.last_discovery_ago ?? '—'}s ago · ` +
      `last error: ${h.last_error || 'none'}`;
  } catch (e) {
    $('health').textContent = 'dashboard fetch failed: ' + e;
  }
}
tick(); setInterval(tick, 1000);
</script>
</body>
</html>
"""
