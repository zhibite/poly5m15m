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

Dashboard：<http://127.0.0.1:8031/>
状态文件：`data/arb_status.json`
数据库：`data/arb.db`

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
```

`/healthz` 不需要认证；`/` 和 `/api/status` 受 `ARB_WEB_PASSWORD` 保护。

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
