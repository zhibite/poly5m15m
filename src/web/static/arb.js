/* Shared dashboard helpers + renderers for the 5m/15m arbitrage bot. */
(function () {
  const $ = (id) => document.getElementById(id);

  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));

  const num = (v, d = 4) =>
    (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(d);

  const money = (v) =>
    (v === null || v === undefined || isNaN(v)) ? '—'
      : (v >= 0 ? '+' : '') + Number(v).toFixed(4);

  const ts = (s) => s ? new Date(s * 1000).toLocaleTimeString() : '—';

  const KIND = {
    cross: '两腿跨市场',
    complete: '四腿完整组合',
    same: '同市场组合'
  };
  const KIND_CLASS = { cross: 'watching', complete: 'placed', same: 'idle' };
  const SIDE = { BUY: '买入', SELL: '卖出' };
  const ROLE = { TAKER: '吃单', MAKER: '挂单' };
  const STATUS = {
    open: '持仓中', settled: '已结算', error: '异常', pending: '待处理'
  };
  const MODE = { paper: '纸面', live: '实盘' };
  const DIR = { UP: '涨', DOWN: '跌' };
  const REASON = {
    strike_missing: '缺少行权价',
    strike_gap_too_small: '行权价间距过小',
    resolution_source_mismatch: '结算源不一致',
    end_time_mismatch: '结束时间不一致',
    strike_settling: '行权价确认中',
    too_late: '临近结算',
    not_ready: '未就绪'
  };

  const map = (v, d) => d[v] || v || '—';
  const direction = (v) => (v || '').split('/').map((x) => DIR[x] || x).join('/') || '—';

  function pill(text, cls) {
    return '<span class="pill ' + (cls || '') + '">' + esc(text) + '</span>';
  }

  function empty(colspan, text) {
    return '<tr><td colspan="' + colspan + '" class="no-data">' + esc(text) + '</td></tr>';
  }

  function row(cols) {
    return '<tr>' + cols.map((c) => '<td>' + c + '</td>').join('') + '</tr>';
  }

  function showAlert(message) {
    const box = $('alert-box');
    if (!box) return;
    box.innerHTML = message
      ? '<div class="alert warn">' + esc(message) + '</div>'
      : '';
  }

  async function fetchStatus() {
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      if (r.status === 401) {
        window.location.href = '/login';
        return null;
      }
      const s = await r.json();
      if (s.error) {
        showAlert(s.error);
        return null;
      }
      showAlert('');
      return s;
    } catch (e) {
      showAlert('看板数据获取失败：' + e);
      return null;
    }
  }

  function renderHeader(s) {
    const h = s.health || {};
    const mode = $('mode');
    if (mode) {
      mode.textContent = map(s.mode, MODE);
      mode.className = 'pill ' + (s.mode === 'live' ? 'sell' : 'watching');
    }
    const ks = $('ks');
    if (ks) {
      const stopped = !!s.killswitch?.active;
      ks.textContent = stopped ? '已停止' : '运行中';
      ks.className = 'pill ' + (stopped ? 'sell' : 'filled');
    }
    const updated = $('updated');
    if (updated) updated.textContent = '更新于 ' + new Date().toLocaleTimeString();
    const dot = $('live-dot');
    if (dot) dot.className = 'live-dot ' + (h.ws_connected ? '' : 'offline');
    const label = $('live-label');
    if (label) label.textContent = h.ws_connected ? 'WS 已连接' : 'WS 未连接';
    const clock = $('clock');
    if (clock) clock.textContent = new Date().toLocaleTimeString();
    const footer = $('health');
    if (footer) {
      footer.textContent =
        `WS：${h.ws_connected ? '已连接' : '未连接'} · ` +
        `最近发现：${h.last_discovery_ago ?? '—'} 秒前 · ` +
        `最近错误：${h.last_error || '无'}`;
    }
  }

  function renderStats(s) {
    const stats = s.stats || {};
    const value = (id, text, cls) => {
      const el = $(id);
      if (!el) return;
      el.textContent = text;
      el.className = 'stat-value' + (cls ? ' ' + cls : '');
    };
    const text = (id, value) => {
      const el = $(id);
      if (el) el.textContent = value;
    };
    value('pnl-today', money(stats.realized_today),
      (stats.realized_today || 0) >= 0 ? 'green' : 'red');
    value('open-pos', stats.open_positions ?? '—');
    value('opps', stats.opportunities ?? '—');
    value('fees-total', num(stats.fees_total, 4));
    text('open-cost', '持仓成本 ' + num(stats.open_cost, 4));
    text('fees-today', '今日手续费 ' + num(stats.fees_today, 4));
    text('fee-rate', '费率 ' + (s.fee_rate ?? '—'));
  }

  function renderPairs(s) {
    const body = $('pairs');
    if (!body) return;
    const pairs = s.pairs || [];
    body.innerHTML = pairs.length ? pairs.map((p) => {
      const modes = [];
      if (p.ready) modes.push('两腿');
      if (p.complete_ready) modes.push('四腿');
      const modeCell = modes.length
        ? pill(modes.join('+'), 'filled')
        : pill(REASON[p.reason] || '未就绪', 'pending');
      return row([
        esc(p.asset), esc(p.end_utc), num(p.strike_5m, 2), num(p.strike_15m, 2),
        num(p.gap_bps, 1), esc(direction(p.direction)), num(p.ask_sum, 3),
        modeCell, num(p.seconds_left, 0)
      ]);
    }).join('') : empty(9, '暂无配对');
  }

  function renderPositions(s) {
    const body = $('positions');
    if (!body) return;
    const positions = s.positions || [];
    body.innerHTML = positions.length ? positions.map((p) => {
      const status = p.status === 'open'
        ? pill('持仓中', 'watching')
        : (p.status === 'settled'
          ? pill('已结算', 'filled')
          : pill(STATUS[p.status] || '未知', 'idle'));
      const pnl = (p.pnl === null || p.pnl === undefined)
        ? '—'
        : `<span class="${p.pnl >= 0 ? 'green' : 'red'}">${money(p.pnl)}</span>`;
      return row([
        esc(p.id), esc(p.asset), pill(map(p.kind, KIND), KIND_CLASS[p.kind] || 'idle'),
        num(p.shares, 2), num(p.buy_cost, 4), num(p.sell_proceeds, 4),
        num(p.fees, 4), status, pnl
      ]);
    }).join('') : empty(9, '暂无仓位');
  }

  function renderFills(s) {
    const body = $('fills');
    if (!body) return;
    const fills = s.fills || [];
    body.innerHTML = fills.length ? fills.map((f) => {
      const side = pill(map(f.side, SIDE), f.side === 'BUY' ? 'buy' : 'sell');
      return row([
        ts(f.ts), esc(f.asset), esc(map(f.kind, KIND)), side,
        num(f.price, 4), num(f.size, 2), num(f.fee, 4),
        esc(map(f.role, ROLE)), esc(f.position_id), esc(f.order_id || '—')
      ]);
    }).join('') : empty(10, '暂无成交');
  }

  function renderOpportunities(s) {
    const body = $('opportunities');
    if (!body) return;
    const opps = s.opportunities || [];
    body.innerHTML = opps.length ? opps.map((o) => row([
      ts(o.ts), esc(o.asset), pill(map(o.kind, KIND), KIND_CLASS[o.kind] || 'idle'),
      num(o.shares, 2), num(o.gross_cost, 4), num(o.fees, 4),
      `<span class="${o.floor_profit >= 0 ? 'green' : 'red'}">${money(o.floor_profit)}</span>`,
      num(o.edge_pct * 100, 2) + '%', o.executed ? '是' : '否'
    ])).join('') : empty(9, '暂无机会');
  }

  function start(renderers) {
    async function tick() {
      const s = await fetchStatus();
      if (!s) return;
      renderHeader(s);
      (renderers || []).forEach((fn) => {
        try { fn(s); } catch (e) { console.error(e); }
      });
    }
    tick();
    setInterval(tick, 1000);
  }

  window.ARB = {
    start,
    renderStats,
    renderPairs,
    renderPositions,
    renderFills,
    renderOpportunities,
    helpers: { $, esc, num, money, ts, map, direction }
  };
})();
