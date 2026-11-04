# Polymarket 5m/15m BTC/ETH 无风险套利策略 v1.0

> **状态**: 已落到本仓库代码（Paper 默认，实盘需双开关）
> **范围**: BTC / ETH，5 分钟与 15 分钟 UP/DOWN 市场
> **策略类型**: 仅做 payoff 无风险套利，不做方向性下注
> **入口**: `python -m src.main`
> **替代**: 原 1h martingale 主逻辑已从生产入口移除

---

## 0. 结论摘要

本方案只做三类**支付结构上确定**的套利：

1. **跨市场两腿 covered arbitrage（主策略）**
   同一个 endTime 的 5m 与 15m 市场，strike 不同。在低 strike 市场买 UP、高 strike 市场买 DOWN。
   无论结算价落在哪里，每份配对至少收回 **$1**；结算价落在两个 strike 之间时收回 **$2**。
   当两腿可执行成本 + taker 手续费 < $1 时，这是**支付意义上的无风险套利**。

2. **四腿跨市场 complete-set（最稳，strike 无关）**
   同时买入 5m UP、5m DOWN、15m UP、15m DOWN。任意结算价下每个市场恰好一边赢，
   四条腿每份组合恰好收回 **$2**。它不需要 strike，也不需要 strike 间距，
   只需要两个市场共享 endTime 且用同一个结算价。四条腿手续费更高，但门槛不被
   overlap 概率抬高，是三套里最稳健的无风险变体。

3. **同市场 complete-set（辅策略）**
   同一市场的 UP ask + DOWN ask + 两腿手续费 < $1 时，同时买入 UP 和 DOWN。
   结算时恰好一边赢，每份收回 $1。可以持有到结算，不依赖链上 merge。

**不采用**原文档 §4 的“5m vs 15m 概率偏差”策略：两个市场的窗口起点和 strike 都不同，
`P_15 = 1-(1-P_5)^3` 的独立事件假设不成立。

**已实现的关键风控**：结算源不一致拒绝交易、strike 间距过小拒绝交易、报价过期拒绝交易、
双腿成交不匹配立即 unwind、结算时校验支付下限 invariant。

**频率预期**：跨市场两腿的公平价格之和通常 ≥ 1，无风险条件（成本+费用 < 1）需要整对价格
跌到支付下限以下，属于**过期报价/交叉盘口**型机会，出现频率可能很低。四腿 complete-set
的公平价恰好是 2，不需要额外低于 overlap，因此门槛更稳定，但四条腿的手续费也翻倍。
Paper 阶段先量化三套策略的机会频率和深度；如果长期为零，应停止，而不是放宽阈值把它变成
方向性下注（见 §3.4 / §3.5）。

---

## 1. 对原文档的修正

原文档 `docs/polymarket-5min-15min-arbitrage.md` 有三处会直接影响实盘的错误，另外漏掉了一个频率事实。

### 1.1 §2.4 的结论错了：这不是“概率优势套利”

原文档在 §2.2 写对了“无论结算价在哪个区间，都能从胜出的一方获得 $1.00”，
但 §2.4 推导时把“赢的一边每份赔 $1”错写成了“赔你付的价格”，于是得出“不是无风险的”。

正确推导（A = 5m strike，B = 15m strike，S = 共同结算价，A < B）：

| 结算价 S | 5m UP | 15m DOWN | 配对支付 |
|---|---|---|---|
| S < A | 输 | 赢 | **$1** |
| A < S < B | 赢 | 赢 | **$2** |
| S > B | 赢 | 输 | **$1** |

支付下限恒为 $1。只有当 strike 顺序判断错、或两个市场不用同一个结算价时，才会出现两边都输。
因此这是**支付结构上的无风险套利**，真正的风险来自执行和模型假设，而不是结算价方向。

### 1.2 手续费不是 0.5%

Polymarket 官方 taker 费用公式（本项目沿用）：

```
fee = shares × fee_rate × p × (1 - p)
```

加密类别 `fee_rate = 0.07`，maker 为 0。p=0.5 时单腿费用是 `0.07 × 0.25 = 1.75 美分/份`，
**两腿约 3.5 美分/份**，不是 0.5%。这意味着：

- 同市场 `UP ask + DOWN ask < 1` 并不够，实际要 `< 1 - 两腿费用`；
- 在 50 美分附近，可执行 ask 之和通常要低于 **0.965** 才有正的下限利润；
- 跨市场机会虽然在支付上更“厚”（fair value = 1 + P(A<S<B)），但两腿费用同样是硬门槛。

### 1.3 可配对窗口每 15 分钟只有一次

5m 市场每 300 秒开始，15m 每 900 秒开始。只有满足

```
start_5m = end_15m - 300
```

的那个 5m 窗口与 15m 窗口共享 endTime，也就是每个 15 分钟周期的**最后 5 分钟**。
BTC + ETH 合计每小时只有 **8 个配对窗口**，不是任意 5m 都能配。

### 1.4 原文档漏掉的：结算源必须一致

原文档假设两个市场都用 Chainlink 同一价格结算。如果实际结算源不同（例如一个 Binance、
一个 Chainlink），跨市场套利的支付下限不成立。代码里做了 fail-closed 校验：

- 两个市场描述都能识别出结算源且不一致 → 拒绝；
- 两个市场 Gamma `endDate` 相差超过 2 秒 → 拒绝（可能不是同一个结算 print）；
- 结算源未知时，Paper 模式允许（用于收集数据），Live 模式默认拒绝；
- 每个已结算的跨市场仓位都会校验 `payout >= matched_shares`，违反会打 `arb.settlement.invariant_violation`。

---

## 2. 市场结构与配对

### 2.1 时间边界

```
15m 窗口:  12:00 ─────────────────────── 12:15
5m 窗口:                              12:10 ─── 12:15   ← 唯一可配对
5m 窗口:  12:00 ─ 12:05, 12:05 ─ 12:10              ← 不能与 12:15 的 15m 配对
```

配对条件用实际 endTime 判断，不靠时钟猜测：

```python
end_15 = (now // 900 + 1) * 900
start_15 = end_15 - 900
start_5 = end_15 - 300
```

### 2.2 Strike（beat price）

当前实现按以下优先级确定每个窗口的 strike：

1. 市场 `question` / `description` 里能解析出明确价格 → 用该价格（会与参考价做 5% sanity check）；
2. 否则用窗口开始时刻的 Binance 1s K 线 open，取不到则回退 1m open；
3. 两个 strike 都拿不到 → `reason=strike_missing`，不交易。

> **待实盘验证**：如果 Polymarket 的 up/down 市场描述里没有明确 strike，而结算用的是
> Chainlink，则 Binance open 只是近似。代码用 `MIN_STRIKE_GAP_BPS` 过滤 strike 过近的配对，
> 并用结算 invariant 兜底；上线前应至少对比 100 个已结算窗口的 Binance open 与最终结果。

### 2.3 方向规则

```
A = strike_5m, B = strike_15m

A < B  → 买 5m UP + 15m DOWN
A > B  → 买 5m DOWN + 15m UP
```

即：**永远在低 strike 市场买 UP，在高 strike 市场买 DOWN。**

---

## 3. 策略 A：跨市场 covered arbitrage

### 3.1 成本与利润

设两腿各买 `Q` 份，成交均价 `p5`、`p15`，taker 费率 `r`：

```
gross_cost = Q × (p5 + p15)
fees       = Q × r × [p5(1-p5) + p15(1-p15)]
floor_pnl  = Q × 1 - gross_cost - fees
```

`floor_pnl > 0` 即支付下限为正。代码还加了 `FEE_BUFFER_PCT` 和 `FEE_BUFFER_USD`，
并要求 `floor_pnl >= max(MIN_EDGE_USD, MIN_EDGE_PCT × gross_cost)`。

结算价落在两个 strike 之间时，额外多收一份 $1/份：

```
overlap_bonus = Q × 1
EV = floor_pnl + Q × P(A < S < B)
```

本策略只要求 `floor_pnl > 0`，不依赖 overlap 概率模型。

### 3.2 数值例子

A = 69,500，B = 69,600，A < B，买 5m UP + 15m DOWN，Q = 100：

| 项目 | 数值 |
|---|---:|
| 5m UP ask | 0.40 |
| 15m DOWN ask | 0.55 |
| gross cost | $95.00 |
| 5m 腿手续费 `100×0.07×0.40×0.60` | $1.68 |
| 15m 腿手续费 `100×0.07×0.55×0.45` | $1.7325 |
| 总成本 | $98.41 |
| 保底支付 | $100.00 |
| **保底利润** | **$1.59（1.6%）** |
| 若 A < S < B，总收入 | $200.00 |

### 3.3 机会来源

两个市场是独立订单簿。fair value 满足：

```
fair(p5_up) + fair(p15_down) = 1 + P(A < S < B) ≥ 1
```

所以理论上两腿 ask 之和通常 ≥ 1。真正出现 `< 1 - fees` 的情况来自：

- 其中一个订单簿的报价过期，没有跟上标的移动；
- 两个市场的做市商/流动性提供者各自定价，短时偏离；
- 临近结算时流动性变薄，价差变宽。

**这是延迟和报价陈旧型机会，不是长期稳定存在的 edge。** 执行速度、报价新鲜度和手续费模型
比“预测方向”重要得多。

### 3.4 频率现实：这是“地板价以下”的错价，不是小偏差

必须把这一点说清楚：

```
fair(5m UP) + fair(15m DOWN) = 1 + P(A < S < B) ≥ 1
```

也就是说，两条腿的**公平价格之和通常大于 1**（overlap 越大，越大于 1）。
要触发无风险套利，需要：

```
ask_5m + ask_15m + fees < 1
```

这不是“比公平价便宜 1–2%”，而是**整对价格跌到支付下限以下**。正常做市下这几乎不会发生；
只有其中一个订单簿明显过期、或者两个市场的做市商短暂失去同步时才会出现。

同市场 complete-set 也一样：`ask_up + ask_down` 通常 ≥ 1（买卖价差），
扣掉约 3.5 美分/份的 taker 费后，需要 `ask_up + ask_down < 0.965` 才无风险。
这是一个**交叉盘口/过期报价**事件，不是稳定收益。

因此：

- Paper 阶段的首要目标不是“赚多少”，而是**测量这种错价到底多久出现一次、深度多大**；
- 如果 1–2 周 Paper 一个机会都没有，正确结论是**这个策略不适合作为独立 bot**；
- 不要为了让策略“跑起来”而把 `MIN_EDGE_USD` 调到 0 或允许 `cost > 1` 的“正 EV”交易——
  那会把无风险套利变成方向性下注。

### 3.5 四腿 complete-set（strike 无关，推荐作为主力补充）

除了“低 strike UP + 高 strike DOWN”两腿，还可以同时买四条腿：

```
5m UP + 5m DOWN + 15m UP + 15m DOWN
```

任意结算价下，每个市场恰好一边赢（UP 或 DOWN），所以：

```
5m 这条组合收回 $1
15m 这条组合收回 $1
四腿每份组合收回 $2
```

它**不依赖 strike 顺序、不依赖 strike 间距、甚至不需要知道 strike**，只需要：

1. 两个市场共享 endTime；
2. 两个市场用同一个结算价；
3. 四条腿都成交。

成本：

```
gross = Q × (ask_5up + ask_5down + ask_15up + ask_15down)
fees  = Q × r × Σ p_i(1-p_i)          # 四条腿
floor = 2Q - gross - fees
```

与两腿策略对比：

| | 两腿（低 strike UP + 高 strike DOWN） | 四腿 complete-set |
|---|---|---|
| 保底支付 | $1/份 | $2/份 |
| 公平价值 | 1 + P(A<S<B) ≥ 1 | 2（不含费用） |
| 触发门槛 | 两腿 ask 之和 < 1 - fees，**还要额外低于 overlap** | 四腿 ask 之和 < 2 - fees，**不受 overlap 影响** |
| 依赖 strike | 是（顺序必须正确） | 否 |
| 手续费 | 2 条腿 | 4 条腿（约 2 倍） |
| 腿风险 | 2 条腿 | 4 条腿 |

数值例子（Q=100，四腿各 0.48）：

```
gross = 4 × 0.48 × 100 = 192.00
fees  = 100 × 0.07 × 4 × 0.48 × 0.52 ≈ 6.99
floor = 200 - 192 - 6.99 = 1.01
```

四腿各 0.50 时：gross=200，fees≈7，floor=-7，不做。

**结论**：四腿 complete-set 不依赖 strike 模型，是三者中最稳的无风险变体；
代价是四条腿的成交/手续费/腿风险。代码里是 `Kind.COMPLETE` / `detect_complete`，
和两腿策略并行检测，取 floor_profit 最大的那个执行。

---

## 4. 策略 B：同市场 complete-set

### 4.1 条件

```
gross_cost = Q × (ask_up + ask_down)
fees       = Q × r × [ask_up(1-ask_up) + ask_down(1-ask_down)]
floor_pnl  = Q - gross_cost - fees > 0
```

结算时恰好一边赢，每份收回 $1。**不需要 merge**：5m/15m 市场最多持有几分钟到十几分钟，
到期自动结算。后续如果要做资金效率优化，再加 CTF `mergePositions`。

### 4.2 手续费现实

`ask_up + ask_down = 0.95`、p≈0.5 时，两腿费用约 3.5 美分/份，保底利润约 1.5 美分/份。
`ask_up + ask_down = 0.99` 时，费用 3.5 美分，净亏约 2.5 美分/份。

所以原文档“YES+NO < 1 就无风险盈利”的说法只有在**零手续费**下成立；实盘必须按上面的公式算。

---

## 5. 不采用的策略

### 5.1 概率偏差（原文档 §4）

原文档假设：

```
P_15 ≈ 1 - (1 - P_5)^3
```

问题：

1. 5m 与 15m 窗口的**起点不同**，strike 不同，比较的不是同一个事件；
2. 5 分钟收益不独立，有动量/波动聚集；
3. 即使公式成立，买入“被低估”的一边也是方向性下注，不是套利。

首版不实现。若以后要研究，只能作为单独的 research module，且必须用历史数据做样本外验证。

### 5.2 正 EV overlap（成本 > 1 但 < 1 + P(overlap)）

数学上可以建模，但需要可信的短周期波动率模型和结算价分布。首版不做，避免把“有正期望”
误当成“无风险”。配置里保留 `ALLOW_CROSS_MARKET` 等开关，未来可扩展。

---

## 6. 执行与风控

### 6.1 两腿执行

1. 用 WS best bid/ask 做触发，只有两腿 ask 之和可能低于 $1 时才拉 REST `/orderbook` 深度；
2. 用同一份快照给两腿定 size、限价和预期成本；
3. 两腿并发 FAK BUY（`amount` 按 `shares × worst_price` 上限提交）；
4. 成交后取 `matched = min(size_leg_a, size_leg_b)`；
5. 多出来的一腿按 `LEG_RISK_POLICY` 处理：
   - `unwind`（默认）：立即按 bid 卖出 excess；
   - `complete`：先尝试在失败腿补足 shortfall，再 unwind 剩余；
6. 任何无法 unwind 的 naked excess 都会标 `leg_risk=true`，并如实持有到结算，
   同时在日志/状态里报警——这时仓位已经不再是严格无风险。

### 6.2 硬性风控

| 风控 | 默认值 | 作用 |
|---|---:|---|
| `MIN_STRIKE_GAP_BPS` | 5 bps | strike 太近时方向可能翻转，拒绝 |
| `MAX_QUOTE_AGE_S` | 1.5 s | 过期盘口不交易 |
| `STOP_OPEN_BEFORE_END_S` | 20 s | 结算前停止开新仓 |
| `MAX_NOTIONAL_PER_PAIR` | $20 | 单对最大成本 |
| `MAX_OPEN_NOTIONAL` | $80 | 总在险成本 |
| `MAX_OPEN_POSITIONS` | 4 | 最大同时持仓数；同一 (asset, endTime) 只允许一个仓位 |
| `MAX_DAILY_LOSS_USD` | $50 | 当日已实现亏损达到后停止开仓 |
| `MAX_CONSECUTIVE_FAILURES` | 5 | 连续执行失败触发冷却 |
| `REQUIRE_SAME_RESOLUTION_SOURCE` | true | 结算源已知且不一致时拒绝 |
| `ALLOW_UNKNOWN_RESOLUTION_SOURCE` | false | Paper 始终允许未知；Live 默认 fail-closed |

### 6.3 资金占用与 sizing

默认参数：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `MAX_NOTIONAL_PER_PAIR` | $20 | 单个 pair 的**预期 gross 成本**上限（不含手续费） |
| `MAX_SHARES_PER_PAIR` | 100 | 单个 pair 的份数上限 |
| `MIN_SHARES_PER_PAIR` | 5 | 低于这个份数不做 |
| `MAX_OPEN_POSITIONS` | 4 | 同时最多 4 个仓位 |
| `MAX_OPEN_NOTIONAL` | $80 | 所有 open 仓位的 gross 成本上限 |
| `MAX_DAILY_LOSS_USD` | $50 | 当日已实现亏损达到后停止开仓 |

sizing 规则：

```
shares = min(两腿/四腿可成交深度, MAX_SHARES_PER_PAIR)
然后按 MAX_NOTIONAL_PER_PAIR 缩小，直到 gross <= $20
若缩小后 < MIN_SHARES_PER_PAIR(5) → 不做
```

**注意**：`MAX_NOTIONAL_PER_PAIR` 是“缩小下单量”的预算，不是“超过就跳过”。
旧实现会直接拒绝超过 $20 的机会；已修正。

按 p≈0.50 估算：

| 模式 | 每份组合成本 | $20 预算约可买 | 实际现金（含 taker fee） |
|---|---:|---:|---:|
| 两腿 CROSS | ~$1.00 | ~20 份 | ~$20.70 |
| 四腿 COMPLETE | ~$2.00 | ~10 份 | ~$20.70 |
| 同市场 SAME | ~$1.00 | ~20 份 | ~$20.70 |

- 手续费约 3.5%（p=0.5，两腿 3.5 美分/份；四腿 7 美分/份），是在 gross 之外额外占用的现金；
- 4 个满仓 position 约需 **$82–85**；实盘钱包建议至少放 **$100** 覆盖费用和滑点；
- 最小交易：两腿约 $5、四腿约 $10（5 份 × 每份成本）；
- Paper 模式不占用真实资金。

### 6.4 结算 invariant

每个仓位结算时：

- 跨市场两腿：`payout >= matched_shares`（每份配对至少一边赢）；
- 四腿 complete-set：`payout >= 2 × matched_shares`（每个市场恰好一边赢）；
- 同市场：`payout == held_shares`（恰好一边赢）。

违反时写 `arb.settlement.invariant_violation` 并落库 `invariant_ok=0`。
这是发现“strike 取错 / 结算源不同 / 方向规则错”的最后一道防线。

### 6.5 手续费记录与交易展示

记录链路：

| 层级 | 表/字段 | 内容 |
|---|---|---|
| 逐笔成交 | `arb_fills.fee` | 每笔 BUY/SELL 的 taker fee（公式计算） |
| 仓位聚合 | `arb_positions.fees` | 该仓位所有 fill 的 fee 之和 |
| 结算 | `arb_settlements.pnl` / `arb_positions.pnl` | `payout + sell_proceeds - buy_cost - fees`，已扣手续费 |
| 机会 | `arb_opportunities.fees` | **下单前估算**，含 10% + $0.01 buffer，dashboard 标为 "Est. fees" |

Dashboard 现在包含：

- **Recent fills**：逐笔成交（时间、asset、kind、side、price、size、fee、role、position_id、order_id）
- **Recent positions**：open + settled，显示聚合 buy_cost / sell_proceeds / fees / PnL
- **Fees paid**：`SUM(arb_fills.fee)`，open + settled 都计入
- **Recent opportunities**：机会成本、估算手续费、保底利润、是否执行

准确度边界：

- **Paper**：完全按公式模拟，记录完整；
- **Live**：按官方公式 `fee = shares × 0.07 × p × (1-p)` 记录，**不读取 CLOB 实际扣费字段**；
  建议用一笔极小实盘单核对钱包实际扣费与 `arb_fills.fee`；
- **Live POST 响应丢失**：如果 FAK 请求已在 CLOB 成交但响应没收到，当前可能漏记该笔成交；
  上实盘前应补启动时余额/持仓对账（见 §10 已知限制）；
- **Maker/GTD**：若以后加 maker 模式，fee 应为 0（可能有 rebate），当前所有记录都按 TAKER 处理。

### 6.6 价格订阅

- **通道**：`wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **订阅帧**：`{"type":"market","assets_ids":[...],"custom_feature_enabled":true}`
- **订阅集合**：当前 5m/15m 市场 + 当前配对的两个市场，去重后一起订阅；市场轮换时移除的 token 会从 `PriceCache` 清除，避免无限增长和旧报价残留。
- **解析**：
  - `best_bid_ask` → `best_bid` / `best_ask`
  - `book` → top-of-book + top-5 depth（对 bids/asks 排序后取最优）
  - `price_change.price_changes[]` → 每个 token 的 `best_bid` / `best_ask`
- **分侧新鲜度**：`bid_ts` / `ask_ts` 分开记录。套利预筛只看 **ask 侧** 的 `ask_ts`；只有 bid 更新不会让旧 ask 看起来新鲜。超过 `MAX_QUOTE_AGE_S`（1.5s）未更新就回退 REST。
- **空边处理**：BBA 的 0、book 的空数组都会把该侧写成 0，而不是保留旧值；`price_change` 没带 BBA 时两边都置 0，触发 REST 快照。
- **REST 兜底**：WS 只做触发。真正下单前 `BookService.fetch_books` 一定会拉 `/orderbook` 深度，成交 sizing 和 `matched` 都基于 REST 快照；WS 漏一条消息最多导致一次机会错过，不会用旧价格成交。
- **重连**：断线后 1–30s 退避重连，重连时重新发送当前 `_pending_tokens`；断开期间引擎按 1s/市场 的限速走 REST。

---

## 7. 代码架构（本仓库）

```
src/main.py                 新入口：只启动套利 bot
src/config.py               ArbSettings (ARB_*) + CredentialsSettings
src/models.py               Side / ArbMarket / ArbPair / Book / Opportunity / Fill
src/fees.py                 taker fee 公式、ask/bid 深度走单
src/reference.py            Binance 窗口 open / 现价
src/discovery.py            市场发现、5m/15m 配对、strike/结算源解析
src/books.py                WS BBO + REST /orderbook 深度
src/detector.py             两腿跨市场 / 四腿 complete-set / 同市场机会检测（纯函数）
src/executor.py             PaperBroker / LiveBroker / 双腿成交与 unwind
src/settlement.py           Gamma 结算 + invariant
src/store.py                SQLite：markets/pairs/opportunities/positions/fills/settlements
src/engine.py               discovery / trading / status 三个循环
src/killswitch.py           文件式 kill switch
src/runtime_config.py       配置页可改参数 + data/runtime_config.json
src/status.py               原子写 data/arb_status.json
src/web/                    FastAPI + 中文看板/成交记录/登录/配置页
src/market/                 pm_client / market_ws
src/execution/              clob_client / clob_l1_auth
src/infra/                  structlog
```

看板首页会展示当前 BTC/ETH 5m/15m 市场的 UP/DOWN 买一/卖一/中间价/最新成交；
`/config` 支持运行时修改风控、手续费、配对、执行和循环参数，并持久化到
`data/runtime_config.json`。模式、资产、端口、路径等系统项只读，需重启生效。

复用底层：

- `src/market/pm_client.py`：Gamma/CLOB 公共接口（精确 `start_ts` 发现参数）；
- `src/market/market_ws.py`：CLOB 盘口 WS（`bbo_ts` / `bid_ts` / `ask_ts` / `connected`）；
- `src/execution/clob_client.py`：FAK BUY/SELL、余额、订单状态；
- `src/execution/clob_l1_auth.py`：L2 凭证派生；
- `src/killswitch.py`：独立 kill switch 文件；
- `src/web/`：登录页、中文看板、成交记录页。

> 旧 1h 策略、dashboard、backtest、旧 DB/feeds 已在 2026-11-04 的清理中物理删除；
> 仅保留新 bot 实际依赖的 `clob_client`、`pm_client`、`market_ws`、`killswitch`、
> `logging` 等底层模块。

### 7.1 数据流

```
Gamma /events?slug=... ──► Discovery ──► ArbPair (strikes, direction)
                                              │
CLOB WS best bid/ask ──► BookService.bbo ─────┤
                                              ▼
                                     Detector (fee-aware)
                                              │
                        ┌─────────────────────┴──────────────────┐
                        ▼                                        ▼
                 PaperBroker (REST book)                 LiveBroker (FAK)
                        └─────────────────────┬──────────────────┘
                                              ▼
                                      PairExecutor
                                   match / unwind / store
                                              │
                                      SettlementWatcher
                                   Gamma resolution + invariant
```

### 7.2 配置

优先级：构造参数 > 进程环境（`ARB_*`）> `.env` > `.env.docker` > `config.toml [ARB]` > 默认值。

常用：

```bash
ARB_PAPER_TRADING=true          # 默认纸面
ARB_LIVE_ENABLED=false          # 实盘必须显式打开
ARB_ASSETS=BTC,ETH
ARB_FEE_RATE=0.07
ARB_MIN_STRIKE_GAP_BPS=5
ARB_MAX_NOTIONAL_PER_PAIR=20
ARB_MAX_OPEN_NOTIONAL=80
ARB_MAX_QUOTE_AGE_S=1.5
ARB_MIN_EDGE_USD=0.02
ARB_ALLOW_COMPLETE_SET=true     # 四腿 complete-set（strike 无关）
ARB_WEB_PASSWORD=...            # 公网部署必须设置
```

实盘双开关：

```
PAPER_TRADING=false  AND  LIVE_ENABLED=true  → live
其它组合                                      → paper
```

这是为了防止“只改了一个 flag 就下真单”。

### 7.3 数据库

`data/arb.db`（WAL）：

| 表 | 内容 |
|---|---|
| `arb_markets` | 每个 5m/15m 市场的 slug、token、strike、结算源 |
| `arb_pairs` | 每个 (asset, end_ts) 的配对、strike、gap、方向、ready/reason |
| `arb_opportunities` | 检测到的机会（成本、费用、保底利润、是否执行） |
| `arb_positions` | 仓位（matched shares、buy cost、unwind proceeds、fees、状态、PnL） |
| `arb_fills` | 每笔 BUY/SELL 成交 |
| `arb_settlements` | 结算结果、两侧 up_wins、payout、invariant_ok |

---

## 8. 验证与上线计划

### Phase 0：只读/Paper 采集（建议 3–7 天）

- 启动 `python -m src.main`（默认 Paper）；
- 打开 `http://<host>:8031`，确认：
  - 每个 asset 每 15 分钟出现一个 pair；
  - `reason` 能解释未就绪原因（`strike_missing` / `strike_gap_too_small` / `too_late`）；
  - 市场描述里的结算源能被识别；
  - 机会数量和盘口深度是否足够。
- **不要**因为看到机会就开实盘；先确认结算 invariant。

### Phase 1：Paper 验证（建议 2 周）

验收标准：

1. 所有跨市场仓位 `invariant_ok=1`；
2. 结算在 endTime 后 5 分钟内完成的比例 > 95%；
3. paper 净 PnL（扣手续费）> 0，且机会频率与滑点符合预期；
4. 没有 `unwind_failed` / `leg_risk` 事件，或每个事件都有明确原因；
5. 最大在险成本从未超过 `MAX_OPEN_NOTIONAL`。

### Phase 2：小仓位实盘（建议 1 周）

```bash
ARB_PAPER_TRADING=false
ARB_LIVE_ENABLED=true
ARB_MAX_NOTIONAL_PER_PAIR=5
ARB_MAX_OPEN_NOTIONAL=20
ARB_MAX_SHARES_PER_PAIR=20
ARB_ALLOW_UNKNOWN_RESOLUTION_SOURCE=false
```

> $5 的 cap 对两腿约可买 5 份；四腿最小 5 份约需 $10，会被跳过。
> 如果 Phase 2 要同时验证四腿，把 `ARB_MAX_NOTIONAL_PER_PAIR` 设为 10，
> 或暂时 `ARB_ALLOW_COMPLETE_SET=false`。

对比 live 与 paper 的成交均价、手续费、unwind 滑点。任何 invariant 违反立即停机。

### Phase 3：放量

只有在 Phase 2 的所有 invariant 通过、且 live 滑点可控后才提高 `MAX_*`。

### 上线前必须人工确认的 4 件事

1. **结算源**：同一 endTime 的 5m/15m 市场描述是否都指向同一个价格源（Chainlink/Binance）；
2. **strike**：窗口 open 的 Binance 价格与最终结算规则是否一致；至少抽样 100 个已结算窗口；
3. **手续费**：用一笔极小实盘单确认实际 taker fee 与 `0.07 × p × (1-p)` 一致；
4. **凭证/allowance**：CLOB L2 凭证、funder 地址、pUSD allowance 正常。

---

## 9. 运行

```bash
# 安装依赖
pip install -e .

# Paper（默认）
python -m src.main

# 实盘（确认 Phase 2 清单后再执行）
ARB_PAPER_TRADING=false ARB_LIVE_ENABLED=true python -m src.main
```

Dashboard：`http://127.0.0.1:8031/`（只读；`ARB_WEB_PASSWORD` 控制 Basic Auth）。
如果 `ARB_WEB_PASSWORD` 未设置，会回退到旧部署里的 `WEB_PASSWORD`，避免迁移后看板意外公开。
状态文件：`data/arb_status.json`。
数据库：`data/arb.db`。

**运行时停止（kill switch）**：引擎每 250ms 读取一次
`data/arb_emergency_stop.json`，所以文件写入对正在运行的进程立即生效：

```bash
# 停止开新仓（已有仓位仍会正常结算）
printf '{"active": true, "reason": "manual"}' > data/arb_emergency_stop.json

# 恢复
rm data/arb_emergency_stop.json
```

文件损坏时保留上一次状态（fail safe）；不会因为读文件失败而继续开仓。

---

## 10. 已知限制

1. **没有链上 merge**：同市场 complete-set 持有到结算，资金占用几分钟到十几分钟；
2. **strike 用 Binance 近似**：若结算实际用 Chainlink，靠 `MIN_STRIKE_GAP_BPS` 和结算 invariant 兜底；
3. **backtest 尚未接入订单簿**：历史回测只能用 kline 概率价格 + 假设价差，不能精确复现双腿成交；
4. **5m 盘口可能很薄**：executor 按深度 cap size，并 unwind excess；
5. **Live 启动对账未实现**：如果进程在两腿 FAK 成交后、写库前崩溃，可能留下未入库的 token。
   上实盘前应补一个启动时 CLOB 余额/持仓对账，把孤儿仓位登记或平掉；
6. **Void/平局风险**：若某个市场被作废（两个 outcomePrices 都是 0）或无法正常结算，
   四腿/同市场 complete-set 的“恰好一边赢”假设失效；settlement watcher 会保持仓位并告警；
7. **四腿执行风险**：complete-set 有 4 条腿，任意一条腿失败都会留下方向性残仓，
   由 `leg_risk` 标记并按真实结算记账；
8. **旧 1h 代码已删除**：包括旧策略、dashboard、backtest、旧 DB/feeds；如果以后要恢复，
   只能从 git 历史里找；
6. **网络/沙箱限制**：本仓库开发环境无法直连 Gamma/Binance，实时字段（尤其市场 description 里的结算规则）
   必须在部署环境用 Paper 模式先验证。

---

## 附录 A：为什么方向不能反

以 A < B 为例，如果错误地买 `5m DOWN + 15m UP`：

| 结算价 S | 5m DOWN | 15m UP | 配对支付 |
|---|---|---|---|
| S < A | 赢 | 输 | $1 |
| A < S < B | 输 | 输 | **$0** |
| S > B | 输 | 赢 | $1 |

中间区间两边都输。所以 `A < B → 5m UP + 15m DOWN` 不能写反；
`MIN_STRIKE_GAP_BPS` 和结算 invariant 就是防这个的。

## 附录 B：费率公式

```
taker_fee(shares, p, r) = shares × r × p × (1 - p)
```

- 费用在成交时收取，maker 为 0；
- p 越接近 0.5 费用越高；
- 本仓库 `ARB_FEE_RATE` 默认 0.07，与旧 1h bot 的 `TAKER_FEE_RATE` 一致。

## 附录 C：术语

| 术语 | 含义 |
|---|---|
| strike / beat price | 该窗口的边界价格；UP 赢的条件是结算价高于它 |
| covered arbitrage | 两边都持有互补方向，支付下限确定 |
| complete-set | 同一市场同时持有 UP + DOWN，支付恒为 $1/份 |
| leg risk | 两腿成交数量/价格不一致导致的单边暴露 |
| unwind | 把多出来的一腿按市价卖出 |
| invariant | 结算时校验支付下限是否成立 |
