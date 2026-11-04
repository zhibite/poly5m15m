# Polymarket 5m/15m 套利 Bot — 部署指南

> 本项目已从 1h martingale 切换为 **5m/15m BTC/ETH 无风险套利**（2026-11-04）。
> 策略与验证计划见 [docs/polymarket-5m-15m-arb-strategy.md](docs/polymarket-5m-15m-arb-strategy.md)。
> 旧 1h 策略代码、dashboard、backtest 已删除。

## 环境要求

- Python 3.11+
- Docker + Docker Compose（或 Coolify）
- Polymarket 账户 + pUSD 余额（仅实盘需要）
- CLOB L2 API Key 或 WALLET_PRIVATE_KEY（仅实盘需要）
- 能访问 Gamma API、CLOB REST/WS、Binance REST

Paper 模式不需要任何凭证，只需要网络。

---

## 一、凭证配置（仅 Live 需要）

### 方式 A：L2 API Key

在 Coolify 环境变量 / 宿主机环境变量 / 本地 `.env` 中设置：

```bash
API_KEY=xxxxx
API_SECRET=xxxxx
API_PASSPHRASE=xxxxx
```

### 方式 B：从私钥派生

```bash
WALLET_PRIVATE_KEY=0xYOUR_PRIVATE_KEY
SIGNATURE_TYPE=0              # 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE
FUNDER_ADDRESS=0x...          # SIG_TYPE=1/2 必填：真正持有 pUSD 的钱包
```

如果 L2 三件套不完整，程序会用 `WALLET_PRIVATE_KEY` 调
`create_or_derive_clob_api_creds` 派生。

### 安全提醒

- `.env.docker` **会提交到 git**，只放非敏感配置；密钥放 Coolify 面板或未提交的 `.env`。
- Dashboard 的 `ARB_WEB_PASSWORD` 也属于密钥。未设置时会回退到旧的 `WEB_PASSWORD`；
  公网部署必须设置其中之一。

---

## 二、模式（双开关）

```bash
# Paper（默认）
ARB_PAPER_TRADING=true
ARB_LIVE_ENABLED=false

# Live（两个都必须满足，否则自动回退 Paper）
ARB_PAPER_TRADING=false
ARB_LIVE_ENABLED=true
```

实盘还会在启动时检查：
- CLOB 客户端是否配置成功；
- pUSD balance 是否 > 0；
- allowance 是否 > 0。

任何一项失败都会记录 ERROR，但不会静默下真单。

---

## 三、本地运行

```bash
pip install -e .
cp .env.example .env      # 可选；Paper 可以不改
python -m src.main
```

Dashboard：<http://127.0.0.1:8031/>（未登录会跳转到 `/login`）
成交记录：<http://127.0.0.1:8031/trades>
运行配置：<http://127.0.0.1:8031/config>
状态文件：`data/arb_status.json`
数据库：`data/arb.db`
运行时配置覆盖：`data/runtime_config.json`

### 看板实时价格

首页顶部展示 BTC / ETH 当前 5m / 15m 市场的 **UP / DOWN 买一、卖一、中间价、最新成交**。
数据来自 CLOB 行情 WS；ask 超过 `MAX_QUOTE_AGE_S` 未更新时页面会显示报价年龄，
引擎会回退 REST 深度，不会用旧价格成交。

### 配置页

`/config`（登录后）可以修改：

- 风控 / 仓位：单对份数、单对成本、最大持仓、当日亏损等
- 手续费 / 边际：taker 费率、缓冲、最小保底利润
- 配对 / Strike：strike 来源、最小间距、报价年龄、结算前停止时间
- 执行：腿风险策略、unwind 重试、滑点、两腿/四腿开关
- 循环：tick、发现、结算轮询、状态写入间隔

保存后写入 `data/runtime_config.json`，对当前进程**立即生效**，重启后继续应用。
模式、资产、端口、DB/状态路径、MarketWS 等系统项显示为只读，需修改环境变量并重启。
配置页还提供 **停止开新仓 / 恢复运行**，等价于写入/删除 `data/arb_emergency_stop.json`。

### 登录与会话

- 登录页：`/login`，中文界面；账号 `ARB_WEB_USERNAME`（默认 `admin`），
  密码 `ARB_WEB_PASSWORD`（或旧的 `WEB_PASSWORD`）。
- 登录成功后写入 HttpOnly / SameSite=Lax 会话 Cookie；会话保存在进程内存，
  `ARB_WEB_SESSION_TTL_S`（默认 604800 秒 = 7 天）后过期；重启/重新部署后需要重新登录。
- 连续 10 次登录失败（5 分钟窗口）会暂时返回 429。
- 未设置密码时看板公开，`/login` 会提示先配置 `ARB_WEB_PASSWORD`。
- 看板、成交记录、登录页均为中文；`/healthz` 和 `/api/health` 不需要登录。

---

## 四、Docker / Coolify

```bash
docker compose up -d --build
```

- `docker-compose.yml` 暴露 **8031**（旧 1h 看板是 8025）。
- `data/` 挂载为 `polymarket_data` volume；`arb.db` / 状态文件 / kill switch 都在里面。
- `config.toml [ARB]` 提供默认值；`.env.docker` / 进程环境变量覆盖。
- 容器名/服务名仍保留 `polymarket-gtd`（兼容现有部署），端口已改为 8031。

### 健康检查

```bash
curl -fsS http://127.0.0.1:8031/healthz
# 兼容旧配置：
curl -fsS http://127.0.0.1:8031/api/health
```

- `/healthz` 和 `/api/health` 不需要认证；
- `/`、`/trades`、`/api/status` 受 `ARB_WEB_PASSWORD` 保护；
- **Coolify 健康检查请用 `/healthz` 或 `/api/health`**。不要用 `/`：未登录时 `/` 会 302 到
  `/login`，Coolify 可能把容器判成 unhealthy。

---

## 五、运行时停止（Kill Switch）

引擎每 250ms 读取一次控制文件，运行中写入立即生效：

```bash
# 停止开新仓（已有仓位仍正常结算）
printf '{"active": true, "reason": "manual"}' > data/arb_emergency_stop.json

# 恢复
rm data/arb_emergency_stop.json
```

控制文件损坏时保留上一次状态（fail safe）。该状态跨重启保留：重启后仍是停止状态，
直到删除文件。

---

## 六、实盘上线清单

1. 先跑 Paper 至少 1–2 周，确认 `arb.settlement.invariant_violation` 为 0；
2. 确认同一 endTime 的 5m/15m 市场使用同一个结算源；
3. 确认 strike（beat price）与窗口 open 一致，或只启用四腿 complete-set
   （`ARB_ALLOW_COMPLETE_SET=true`，不依赖 strike）；
4. 用极小实盘单确认实际 taker fee 与 `0.07 × p × (1-p)` 一致；
5. 小仓位实盘：

```bash
ARB_PAPER_TRADING=false
ARB_LIVE_ENABLED=true
ARB_MAX_NOTIONAL_PER_PAIR=5
ARB_MAX_OPEN_NOTIONAL=20
ARB_MAX_SHARES_PER_PAIR=20
ARB_ALLOW_UNKNOWN_RESOLUTION_SOURCE=false
```

6. 确认 `arb.live.collateral` 日志里 balance/allowance 正常。

---

## 七、常见问题

| 现象 | 处理 |
|---|---|
| Dashboard 打不开 | 检查 `ARB_WEB_PORT=8031` 是否映射；容器内是否启动（日志 `arb.web.started`） |
| 没有 pair | 5m/15m 只在每个 15 分钟窗口最后 5 分钟配对；查看 `arb.pair.state` 的 reason |
| `strike_missing` | Binance kline 未取到；检查网络；或启用四腿 complete-set |
| `resolution_source_mismatch` | 两个市场描述里的结算源不同；不要交易该 pair |
| 一直是 Paper | `PAPER_TRADING` / `LIVE_ENABLED` 双开关或凭证未配置 |
| 没有机会 | 正常：这是过期报价/交叉盘口型套利，先看 dashboard 的 Ask sum |

---

## 八、命令速查

```bash
python -m src.main                    # Paper
python -m pytest -q                   # 测试
curl http://127.0.0.1:8031/api/status # 状态 JSON（需 Basic Auth）
sqlite3 data/arb.db 'select * from arb_positions order by id desc limit 10;'
```
