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

  function renderMarkets(s) {
    const body = $('markets');
    if (!body) return;
    const markets = s.markets || [];
    if (!markets.length) {
      body.innerHTML = '<tr><td colspan="5" class="no-data">暂无实时行情</td></tr>';
      return;
    }
    const price = (v) => (v === null || v === undefined) ? '—' : Number(v).toFixed(4);
    const quote = (q) => {
      q = q || {};
      const mid = (q.bid && q.ask) ? (q.bid + q.ask) / 2 : q.mid;
      return {
        mid: price(mid),
        bid: price(q.bid),
        ask: price(q.ask),
        last: price(q.last),
        age: (q.age === null || q.age === undefined) ? '—' : Number(q.age).toFixed(1),
      };
    };
    body.innerHTML = markets.map((m) => {
      const up = quote(m.up);
      const down = quote(m.down);
      return `<tr>
        <td><b>${esc(m.asset)}</b> <span class="muted">${esc(m.interval)}</span></td>
        <td class="muted">${esc(m.start_utc)}–${esc(m.end_utc)}</td>
        <td>
          <div class="market-mid up">${up.mid}</div>
          <div class="muted market-quote">买 ${up.bid} / 卖 ${up.ask} · 最新 ${up.last}</div>
        </td>
        <td>
          <div class="market-mid down">${down.mid}</div>
          <div class="muted market-quote">买 ${down.bid} / 卖 ${down.ask} · 最新 ${down.last}</div>
        </td>
        <td class="muted">ask ${up.age}s / ${down.age}s</td>
      </tr>`;
    }).join('');
  }

  const sideText = (v) => v === 'UP' ? '涨' : v === 'DOWN' ? '跌' : '—';
  const fmtPrice = (v) => (v === null || v === undefined || isNaN(v))
    ? '—'
    : Number(v).toLocaleString(undefined, {
        minimumFractionDigits: 2, maximumFractionDigits: 2
      });

  function meter(label, sum, required, fee) {
    if (sum === null || sum === undefined || required === null || required === undefined) {
      return `<div class="pair-meter">
        <div class="meter-label"><span>${esc(label)}</span><span class="muted">行情不足</span></div>
      </div>`;
    }
    const good = sum <= required;
    const gap = sum - required;
    const lo = required - 0.05;
    const hi = required + 0.05;
    const pct = Math.max(0, Math.min(100, ((sum - lo) / (hi - lo)) * 100));
    const feeText = (fee === null || fee === undefined)
      ? ''
      : `手续费≈${(fee * 100).toFixed(1)}¢/份`;
    return `<div class="pair-meter">
      <div class="meter-label">
        <span>${esc(label)} <span class="muted">${esc(feeText)}</span></span>
        <b class="${good ? 'green' : 'red'}">${num(sum, 4)}</b>
      </div>
      <div class="meter-track">
        <div class="meter-fill ${good ? 'good' : ''}" style="width:${pct}%"></div>
        <div class="meter-threshold"></div>
      </div>
      <div class="meter-foot">
        <span>触发线 ≤ ${num(required, 4)}</span>
        <span class="${good ? 'green' : 'red'}">${good
          ? '已低于触发线 ' + num(Math.abs(gap), 4)
          : '还差 ' + num(gap, 4)}</span>
      </div>
    </div>`;
  }

  function renderPairs(s) {
    const box = $('pairs');
    if (!box) return;
    const pairs = s.pairs || [];
    if (!pairs.length) {
      box.innerHTML = '<div class="no-data">暂无配对窗口（每 15 分钟最后 5 分钟出现）</div>';
      return;
    }
    box.innerHTML = pairs.map((p) => {
      const badges = [];
      if (p.ready) badges.push(pill('两腿就绪', 'filled'));
      if (p.complete_ready) badges.push(pill('四腿就绪', 'watching'));
      if (!badges.length) badges.push(pill(REASON[p.reason] || '未就绪', 'pending'));

      const side5 = sideText(p.side_5m);
      const side15 = sideText(p.side_15m);
      const direction = (p.side_5m && p.side_15m)
        ? `买 <b>5m ${side5}</b> + <b>15m ${side15}</b>`
        : '<span class="muted">方向待定（缺少 strike）</span>';

      const legs = [];
      if (p.ask_5m !== null && p.ask_5m !== undefined) {
        legs.push(`<span class="pill ${p.side_5m === 'UP' ? 'buy' : 'sell'}">5m ${side5} · ask ${num(p.ask_5m, 3)}</span>`);
      }
      if (p.ask_15m !== null && p.ask_15m !== undefined) {
        legs.push(`<span class="pill ${p.side_15m === 'UP' ? 'buy' : 'sell'}">15m ${side15} · ask ${num(p.ask_15m, 3)}</span>`);
      }

      return `<div class="pair-card">
        <div class="pair-top">
          <span class="pair-title">${esc(p.asset)} · 5m × 15m</span>
          <span>${badges.join(' ')}</span>
        </div>
        <div class="pair-direction">${direction}</div>
        <div class="pair-meta">
          <span>结束 <b>${esc(p.end_utc)}</b></span>
          <span>剩余 <b>${num(p.seconds_left, 0)}s</b></span>
          <span>strike 间距 <b>${num(p.gap_bps, 1)} bps</b></span>
        </div>
        ${meter('两腿卖一价和', p.ask_sum, p.required_2leg, p.fee_2leg)}
        ${p.complete_ask_sum !== null && p.complete_ask_sum !== undefined
          ? meter('四腿完整组合价和', p.complete_ask_sum, p.complete_required, p.complete_fee)
          : ''}
        <div class="pair-strikes">
          <span>5m 行权价 <b>${fmtPrice(p.strike_5m)}</b></span>
          <span>15m 行权价 <b>${fmtPrice(p.strike_15m)}</b></span>
        </div>
        <div class="pair-legs">${legs.join('') || '<span class="muted">暂无方向腿报价</span>'}</div>
      </div>`;
    }).join('');
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
    renderMarkets,
    renderPairs,
    renderPositions,
    renderFills,
    renderOpportunities,
    helpers: { $, esc, num, money, ts, map, direction }
  };
})();
