# Polymarket 5分钟/15分钟 UP/DOWN 套利策略详解

> **更正说明（2026-11-04）**：本文档是最初的研究稿，其中 §2.4 的“不是无风险”推导、
> 0.5% 手续费假设和配对频率描述均已被更正。实现与最终方案见
> [docs/polymarket-5m-15m-arb-strategy.md](polymarket-5m-15m-arb-strategy.md)。
> 保留本文档仅作研究记录。

> **版本**: v1.0
> **日期**: 2026-06-10
> **状态**: 策略研究阶段
> **依赖**: Polymarket CLOB API V2 + Gamma API

---

## 一、市场结构核心概念

### 1.1 UP/DOWN 市场的运作机制

在深入套利逻辑之前，必须先理解 UP/DOWN 市场的精确运作方式。

```
创建市场时，Polymarket 会设置两个关键参数：

1. 起始价（Start Price）
   - 锚定创建瞬间 BTC 的实时价格
   - 例如：$69,500.00

2. Beat Price（边界价格）
   - 用于最终结算判定的 BTC 价格阈值
   - 注意：Beat Price ≠ 起始价

结算判定规则：
  结算价（Chainlink 喂价） > Beat Price  →  UP 赢
  结算价（Chainlink 喂价） < Beat Price  →  DOWN 赢
  结算价 = Beat Price                  →  平局
```

### 1.2 市场命名规则（Slug Pattern）

Polymarket 的 BTC/ETH UP/DOWN 市场使用固定格式的 URL slug：

```
5分钟市场:  btc-updown-5m-{时间戳}
15分钟市场: btc-updown-15m-{时间戳}

时间戳 = 该周期开始的 Unix 秒数（UTC）

举例：
  当前时间: 2026-06-10 20:15:00 UTC
  5分钟周期: 每5分钟一个市场 → 时间戳 = 1771168800（示例）
  15分钟周期: 每15分钟一个市场 → 时间戳 = 1771168500（示例）

  URL: https://polymarket.com/event/btc-updown-5m-1771168800
       https://polymarket.com/event/btc-updown-15m-1771168500
```

### 1.3 5分钟 vs 15分钟市场的关系

这是套利的核心基础：

| 维度 | 5分钟市场 | 15分钟市场 |
|------|---------|----------|
| 周期长度 | 5 分钟 | 15 分钟 |
| 创建频率 | 每5分钟一个 | 每15分钟一个 |
| 起始价锚定 | 创建时的即时价格 | 创建时的即时价格 |
| 结算数据源 | Chainlink BTC/USD 喂价 | Chainlink BTC/USD 喂价 |
| **Beat Price** | 独立设定 | 独立设定 |
| CLOB Token ID | 独立 | 独立 |

```
关键洞察：

虽然两个市场的周期不同，但存在一个特殊的时间窗口——
当 5分钟市场的结束时间 == 15分钟市场的结束时间时：
  两个市场会使用同一个 Chainlink 结算价来判定胜负

这就是 Beat Price 套利的核心前提
```

### 1.4 时间对齐机制详解

```
时间轴示意（UTC）：

12:00 ─── 5m market #1 starts  (ends 12:05)
12:00 ─── 15m market #1 starts (ends 12:15)
12:05 ─── 5m market #2 starts  (ends 12:10)
12:05 ─── 15m market #1 still running... (ends 12:15)  ← 12:05 这个时间点
12:10 ─── 5m market #3 starts  (ends 12:15)
12:10 ─── 15m market #1 still running...               ← 12:10 这个时间点
12:15 ─── 5m market #4 starts  (ends 12:20)
12:15 ─── 15m market #1 RESOLVES (endTime = 12:15)
12:15 ─── 15m market #2 starts  (ends 12:30)

发现：5m #3 和 15m #1 共享同一个 endTime（12:15）
      它们用同一个 Chainlink 结算价来判定结果
```

---

## 二、策略一：Beat Price 价差套利（核心策略）

### 2.1 核心逻辑

当 5分钟市场 和 15分钟市场 共享同一个 endTime 时：

- 它们使用**同一个 Chainlink 结算价**
- 但它们的 **Beat Price 是独立设定的**
- Beat Price 的差异 = 无风险套利空间

```
Beat Price 的含义：

15m Beat Price = $69,600
5m Beat Price  = $69,550

这意味着：
  - 15m 市场：BTC 结算价 > $69,600 才算 UP 赢
  - 5m 市场：  BTC 结算价 > $69,550 才算 UP 赢

由于两个市场用同一个结算价，我们可以分析所有可能的结果：

如果结算价 = $69,575（介于两个 Beat Price 之间）：
  → 5m 市场: UP 赢（因为 $69,575 > $69,550）
  → 15m 市场: DOWN 赢（因为 $69,575 < $69,600）
  → 结果必然是 一胜一负

如果结算价 = $69,500（低于两个 Beat Price）：
  → 两个市场都是 DOWN 赢

如果结算价 = $69,700（高于两个 Beat Price）：
  → 两个市场都是 UP 赢
```

### 2.2 套利操作详解

#### Case A: `beatPrice_15m > beatPrice_5m`

```
场景设定：

  5m 市场 Beat Price:  $69,550
  15m 市场 Beat Price: $69,600

  当前 BTC 价格: $69,500

套利逻辑：

  如果 BTC 结算价在 $69,550 ~ $69,600 之间：
    → 5m 市场 UP 赢（> $69,550）
    → 15m 市场 DOWN 赢（< $69,600）
    → 你同时持有 UP(5m) + DOWN(15m) = 一胜一负

  问题：这样不是互相抵消了吗？
  答案：不会！因为价格不同！

  实际上：
    买入 5m UP → 花费 price_up_5m × 份额
    买入 15m DOWN → 花费 price_down_15m × 份额
    总成本 = price_up_5m + price_down_15m

    无论结算价在哪个区间，你都能从"胜出"的那一方获得 $1.00

    但关键在于：
      当 beatPrice_15m > beatPrice_5m 时
      市场定价应该反映这个差异
      如果定价错误，就会产生套利机会
```

#### Case B: `beatPrice_5m > beatPrice_15m`

```
场景设定：

  5m 市场 Beat Price:  $69,600
  15m 市场 Beat Price: $69,550

  当前 BTC 价格: $69,500

套利操作：

  买入 15m UP (因为它的 Beat Price 更低，更容易赢)
  买入 5m DOWN (因为它的 Beat Price 更高，更难赢)

  条件：
    如果 5m Beat Price > 15m Beat Price
    且 price_15m_UP + price_5m_DOWN < ARB_THRESHOLD

  执行套利
```

### 2.3 数学建模

```
定义：

  BP_5m  = 5m 市场的 Beat Price
  BP_15m = 15m 市场的 Beat Price
  S      = Chainlink 结算价（两个市场共用）

结果判定：

  5m 市场结果:  UP if S > BP_5m, else DOWN
  15m 市场结果: UP if S > BP_15m, else DOWN

设 BP_5m < BP_15m（Case A 的情况）

三种结算场景：

  场景1: S > BP_15m (高于两者)
    → 5m: UP 赢
    → 15m: UP 赢
    → 你的持仓: 5m_UP + 15m_DOWN = 15m_DOWN 输 ❌

  场景2: S < BP_5m (低于两者)
    → 5m: DOWN 赢
    → 15m: DOWN 赢
    → 你的持仓: 5m_UP + 15m_DOWN = 5m_UP 输 ❌

  场景3: BP_5m < S < BP_15m (介于两者之间) ← 这是套利区间
    → 5m: UP 赢（因为 S > BP_5m）
    → 15m: DOWN 赢（因为 S < BP_15m）
    → 你的持仓: 5m_UP 赢 ✅ + 15m_DOWN 赢 ✅ = 两边都赢！

等等...这个结论有问题，让我重新推导...
```

### 2.4 套利的正确逻辑

```
重新梳理：

如果 BP_5m < BP_15m，套利方向是：
  买入 5m UP + 买入 15m DOWN

结算结果分析：

  场景1: S > BP_15m
    → 5m: UP 赢 ✅
    → 15m: DOWN 输 ❌
    → 净值: 1 × (5m_UP价格) + 0 × (15m_DOWN价格) = 5m_UP_cost
    → 如果 5m_UP_cost < 1，有收益

  场景2: S < BP_5m
    → 5m: UP 输 ❌
    → 15m: DOWN 赢 ✅
    → 净值: 0 + 1 × (15m_DOWN_cost) = 15m_DOWN_cost
    → 如果 15m_DOWN_cost < 1，有收益

  场景3: BP_5m < S < BP_15m ← 这是确定性盈利区间
    → 5m: UP 赢 ✅
    → 15m: DOWN 赢 ✅
    → 净值: 1 + 1 = $2.00（持有2份，各赢$1）
    → 成本: price_5m_UP + price_15m_DOWN
    → 利润 = 2 - (price_5m_UP + price_15m_DOWN)

结论：这个套利不是"无风险"的
它依赖于 S 落在 [BP_5m, BP_15m] 区间内的概率

但关键洞察是：
  当 beatPrice_15m > beatPrice_5m 时
  BP_15m 是"更高的门槛"，15m DOWN 更容易赢
  BP_5m 是"更低的门槛"，5m UP 更容易赢

  市场的定价应该反映这个概率差异
  如果定价出现偏差（price_5m_UP + price_15m_DOWN < 某个阈值）
  就产生了套利机会
```

### 2.5 套利条件公式

```
触发条件：

  price_5m_UP + price_15m_DOWN < ARB_THRESHOLD (例如 0.90)

  ARB_THRESHOLD 越低，套利利润越高，风险越小

预期收益：

  P(场景3) × 利润 - P(场景1) × 亏损 - P(场景2) × 亏损

  其中 P(场景3) = (BP_15m - BP_5m) / BTC_5min波动范围

  波动范围越大，场景3概率越低
  Beat Price 差距越大，场景3概率越高
```

### 2.6 Bot 实现要点（参考 `horsedev77/polymarket_5minute_15minute-arbitrage-bot`）

```python
# 伪代码逻辑
def check_beat_price_arbitrage():
    # 1. 获取当前和下一个 5m 市场的 endTime
    markets_5m = get_active_markets("btc-updown-5m")
    markets_15m = get_active_markets("btc-updown-15m")

    # 2. 找到 endTime 相同的 5m 和 15m 市场
    for m5 in markets_5m:
        for m15 in markets_15m:
            if m5.endTime == m15.endTime:
                # 找到了！检查 beat price 差异
                if m15.beatPrice > m5.beatPrice:
                    # Case A: 买入 5m UP + 15m DOWN
                    total_cost = m5.up_price + m15.down_price
                    if total_cost < ARB_THRESHOLD:
                        execute_arb(m5, m15, direction="up5_down15")
                elif m5.beatPrice > m15.beatPrice:
                    # Case B: 买入 15m UP + 5m DOWN
                    total_cost = m15.up_price + m5.down_price
                    if total_cost < ARB_THRESHOLD:
                        execute_arb(m5, m15, direction="up15_down5")
```

### 2.7 关键风险

```
风险1: Beat Price 信息获取延迟
- Beat Price 可能不在公开 API 中直接返回
- 需要解析市场详情或通过其他渠道获取
- 如果获取有误，整个套利逻辑就失效

风险2: 结算价不在预期区间
- 如果 BTC 结算价不在 [BP_5m, BP_15m] 区间内
- 有一边的头寸会亏损
- 这不是无风险套利，而是概率优势套利

风险3: 执行延迟
- 检测到机会 → 下单执行 的延迟可能让机会消失
- 高频 bot 会在你之前吃掉这个机会

风险4: 价格滑点
- 市场深度不足时，大单会滑点
- 实际买入价格可能比看到的更差
```

---

## 三、策略二：YES+NO Spread 套利（市场内套利）

### 3.1 核心逻辑

这是更简单、更常见的套利方式：

```
在同一个市场中：
  YES 价格 + NO 价格 应该 = $1.00

由于市场买卖价差（spread）等因素：
  YES 卖价（ask）+ NO 卖价（ask） 可能 < $1.00

套利方法：
  在同一市场同时买入 YES 和 NO
  当两者价格之和 < $1.00 时
  买入两边必然有一边赢 $1.00
  成本 < $1.00 → 利润 = $1.00 - 成本
```

### 3.2 具体操作

```
BTC 5分钟 UP/DOWN 市场：

  YES 最新卖价: $0.62
  NO 最新卖价:  $0.36
  YES + NO = $0.98 < $1.00

套利步骤：

  1. 买入 100 份 YES @ $0.62 → 花费 $62.00
  2. 买入 100 份 NO  @ $0.36 → 花费 $36.00
  3. 总成本: $98.00

  到期结算（无论哪种结果）：
    如果 UP 赢:  YES = $1.00 × 100 = $100
                NO  = $0.00
                总收入: $100
                利润: $2.00 (ROI 2.04%)

    如果 DOWN 赢: YES = $0.00
                NO  = $1.00 × 100 = $100
                总收入: $100
                利润: $2.00 (ROI 2.04%)

  确定性盈利！只要 YES + NO < $1.00
```

### 3.3 市场为什么会给出 YES + NO < $1.00 的价格？

```
原因1: AMM 机制的买卖价差
  AMM 的 YES 卖价 = $0.62
  AMM 的 NO 卖价 = $0.36
  这两个"卖价"是 AMM 愿意卖给你的价格
  它们之和通常接近但略低于 $1.00

原因2: 流动性提供者（LP）不完美对冲
  LP 向池子注入 YES 和 NO 两种流动性
  如果两边流动性不平衡，AMM 价格会偏离 $1.00

原因3: 短期价格冲击
  大量交易者涌入买某一方向
  AMM 调整价格，但调整不是瞬时的
  短暂出现 YES + NO < 1 的窗口

原因4: 临近结算的流动性枯竭
  接近 endTime 时，很多 LP 撤出流动性
  剩余的流动性提供者报价更宽
  YES + NO 可能短暂 < 1
```

### 3.4 Bot 实现要点

```python
# 伪代码 - YES+NO Spread 套利
def check_spread_arbitrage(token_id_yes, token_id_no):
    # 获取订单簿
    book_yes = get_clob_book(token_id_yes)
    book_no  = get_clob_book(token_id_no)

    # 最佳卖价（AMM 愿意卖给你的价格）
    yes_ask = book_yes.best_ask  # 你买入 YES 的价格
    no_ask  = book_no.best_ask   # 你买入 NO 的价格

    total_cost = yes_ask + no_ask

    if total_cost < ARB_THRESHOLD:
        # 套利机会！
        shares = calculate_shares(total_capital, yes_ask, no_ask)

        # 执行：同时买入 YES 和 NO
        place_order(token_id_yes, "BUY", yes_ask, shares)
        place_order(token_id_no,  "BUY", no_ask,  shares)

        # 等待结算
        # 注意：结算后需要 merge（合并 YES+NO = $1）来兑换
```

### 3.5 Merge（合并）机制

```
关键：买入 YES + NO 后，你不会自动获得 $1.00

你需要执行 "merge" 操作来合并 YES + NO = $1.00

合并方式：
  - 手动：在 Polymarket 界面点击合并
  - 自动：通过 API 调用 merge_max 函数

合并时机：
  - 市场结算后，YES/NO 结果确定
  - 获胜方持有 1.00，失败方为 0.00
  - 合并后可以兑换 $1.00

Bot 示例（参考 rvenandowsley/bot）：
  MERGE_INTERVAL_MINUTES: 每 N 分钟检查并合并一次
  STOP_ARBITRAGE_BEFORE_END_MINUTES: 结算前 N 分钟停止套利
```

### 3.6 关键风险

```
风险1: 两单不同步执行
  - 如果 YES 成交但 NO 没成交
  - 你只持有 YES，暴露单边风险
  - 需要 FOK（Fill-Or-Kill）订单类型保证同时成交

风险2: 结算前合并限制
  - 市场结算前，YES + NO 可能不能合并
  - 你需要持有到结算才能合并

风险3: 机会转瞬即逝
  - YES + NO < 1 的窗口通常只有 2-3 秒
  - 高频 bot 会在你之前吃掉机会
  - 2026年平均套利窗口: 2.7 秒
  - 73% 的套利利润被 sub-100ms 的 bot 拿走

风险4: 手续费侵蚀
  - Polymarket 收取 0.5% 交易费
  - Polygon 网络 gas 费
  - 如果 YES + NO = 0.98，扣完 fee 后利润极薄
```

---

## 四、策略三：概率偏差套利（5m vs 15m 隐含概率）

### 4.1 核心逻辑

利用 5分钟市场 和 15分钟市场 之间隐含概率的差异：

```
5分钟市场隐含概率：
  BTC 未来5分钟上涨的概率

15分钟市场隐含概率：
  BTC 未来15分钟上涨的概率

直觉上：
  P(5min上涨) 应该 ≈ P(15min上涨) ^ (1/3)
  因为15分钟包含3个5分钟

但实际上：
  市场定价可能不符合这个关系
  这就产生了套利机会
```

### 4.2 具体场景

```
场景1：5m 市场定价过高

  BTC 当前: $69,500
  BTC 5m UP:  YES = $0.78  (隐含 78% 上涨概率)
  BTC 15m UP: YES = $0.65  (隐含 65% 上涨概率)

  问题：15m 市场只比 5m 多10分钟
        但隐含概率从 78% 降到 65%
        这个降幅合理吗？

  分析：
    如果 BTC 在 5min 内上涨概率是 78%
    那么在 15min 内上涨的概率应该更高，而不是更低
    因为15min 有更多时间让 BTC 涨

  结论：15m 市场被低估了（相对于 5m）

  策略：
    买入 15m UP ($0.65) — 被低估
    可选: 卖出/做空 5m UP ($0.78) — 被高估
    等待价格回归均衡

场景2：方向背离

  BTC 5m UP:  YES = $0.55（几乎中性）
  BTC 15m UP: YES = $0.80（高度看涨）

  这意味着：
    市场认为短期（5min）方向不明
    但长期（15min）非常确定会涨

  策略：
    如果你同意这个判断：
      买入 15m UP（赔率更好）
    如果你不同意：
      买入 5m UP 做空（均值回归）
```

### 4.3 数学模型

```
定义：

  P_5  = 5m 市场中 UP 的隐含概率（= YES 价格）
  P_15 = 15m 市场中 UP 的隐含概率（= YES 价格）

理论关系：

  P_15 ≈ 1 - (1 - P_5)^3

  推导：
    15min 不涨 = 3个连续的 5min 都不涨
    P(15min 不涨) = P(5min 不涨)^3 = (1-P_5)^3
    P(15min 涨) = 1 - (1-P_5)^3

举例：
  如果 P_5 = 0.70（5min 70% 上涨）
  那么 P_15 应该 ≈ 1 - (0.30)^3 = 1 - 0.027 = 0.973

  但如果市场给 P_15 = 0.85：
    市场定价: 0.85
    理论定价: 0.973
    差异:     0.123 ← 显著低估！

套利操作：
  买入被低估的 15m UP ($0.85)
  等待 P_15 回归到 0.97
  利润 = 0.12/份
```

### 4.4 关键风险

```
风险1: 理论关系假设失效
  - P_15 ≈ 1-(1-P_5)^3 假设每个5min是独立事件
  - 实际上价格有动量/趋势，5min 之间不独立
  - 连续3个5min都涨的概率远高于独立假设下的概率

风险2: 时间窗口不匹配
  - 5m 市场和 15m 市场的起始时间可能不同
  - 它们不是同一个起点
  - 直接比较 P_5 和 P_15 可能有误导

风险3: 执行延迟
  - 概率偏差通常很小（1-5%）
  - 扣除 gas 和 fee 后利润更薄
  - 需要快速执行才能抓住机会
```

---

## 五、综合套利框架

### 5.1 三种策略的对比

| 维度 | Beat Price 套利 | YES+NO Spread 套利 | 概率偏差套利 |
|------|---------------|------------------|-------------|
| **本质** | 边界价格差异 | 市场内买卖价差 | 跨市场概率差异 |
| **难度** | 高 | 低 | 中 |
| **利润** | 中等 | 低 | 中 |
| **风险** | 中等 | 低 | 中高 |
| **前提条件** | endTime 相同 | YES+NO < 1 | P_5 vs P_15 偏差 |
| **Bot 成熟度** | 有开源实现 | 有开源实现 | 较少 |
| **适用市场** | 5m + 15m 同时存在 | 所有市场 | 5m + 15m 同时存在 |

### 5.2 推荐的套利流水线

```
Phase 1: 市场发现
  │
  ├─ 通过 Gamma API 获取当前活跃的 5m 和 15m 市场
  │   GET https://gamma-api.polymarket.com/markets?active=true
  │   过滤 slug 包含 "updown-5m" 和 "updown-15m"
  │
  ├─ 获取每个市场的详细信息：
  │   - endTime (时间戳)
  │   - beatPrice (边界价格)
  │   - clobTokenIds (YES Token ID + NO Token ID)
  │
  └─ 匹配 endTime 相同的 5m + 15m 市场对

Phase 2: 价格监控
  │
  ├─ 通过 CLOB API 获取实时订单簿
  │   WebSocket: wss://ws-subscriptions-clob.polymarket.com/ws/market
  │   监控 YES 和 NO 的 best bid / best ask
  │
  └─ 实时计算：
      - YES_ask + NO_ask (Spread 检查)
      - price_5m_UP + price_15m_DOWN (Beat Price 检查)
      - P_5 vs P_15 (概率偏差检查)

Phase 3: 套利执行
  │
  ├─ 满足 Spread 套利条件:
  │   YES_ask + NO_ask < 0.99
  │   → 同时买入 YES + NO (FOK 订单)
  │
  ├─ 满足 Beat Price 条件:
  │   price_5m_UP + price_15m_DOWN < ARB_THRESHOLD
  │   且 endTime 相同
  │   → 执行对应方向的套利
  │
  └─ 满足概率偏差条件:
      |P_5 - P_15_theoretical| > THRESHOLD
      → 买入被低估方向

Phase 4: 持仓管理
  │
  ├─ 监控市场结算状态
  ├─ 执行 merge（合并 YES+NO）
  └─ 记录 PnL，更新统计
```

### 5.3 关键参数配置

```
# 环境变量参考（基于开源 Bot）

# Gamma API
POLYMARKET_API_KEY=your_api_key

# CLOB API
POLYMARKET_PRIVATE_KEY=your_private_key
POLYMARKET_PROXY_ADDRESS=your_proxy_address

# 套利阈值
ARBITRAGE_EXECUTION_SPREAD=0.01  # 当 YES+NO < 0.99 时触发
ARB_THRESHOLD=0.90                 # Beat Price 套利阈值

# 订单参数
MAX_ORDER_SIZE_USDC=100            # 每笔最大仓位
SLIPPAGE=0,0.01                   # slippage 容限
GTD_EXPIRATION_SECS=300           # GTD 订单有效期
ARBITRAGE_ORDER_TYPE=GTD          # FOK/GTD/GTC

# 风险控制
RISK_MAX_EXPOSURE_USDC=500        # 最大敞口
MIN_PROFIT_THRESHOLD=0.001        # 最小利润阈值
STOP_ARBITRAGE_BEFORE_END_MINUTES=1  # 结算前1分钟停止

# Merge
MERGE_INTERVAL_MINUTES=5         # 每5分钟检查合并
```

---

## 六、技术实现路线

### 6.1 API 端点汇总

```
市场发现（Gamma API）：
  GET https://gamma-api.polymarket.com/markets
    ?active=true
    &marketTypes=["binary"]
    &tagId=... (crypto 相关标签)

  响应中查找 slug 格式：
    btc-updown-5m-{timestamp}
    btc-updown-15m-{timestamp}

订单簿（CLOB API）：
  GET https://clob.polymarket.com/book?token_id={clobTokenId}
  WebSocket: wss://ws-subscriptions-clob.polymarket.com/ws/market

市场 BBO（轻量）：
  GET https://clob.polymarket.com/best_bid_offer?token_id={token_id}

交易执行：
  POST https://clob.polymarket.com/orders
  （需要签名认证）

结算信息：
  GET https://gamma-api.polymarket.com/markets/{market_id}
  包含 end_time, question, outcomes 等字段
```

### 6.2 市场发现的精确方法

```python
import time
import requests

def generate_market_slug(asset: str, duration_minutes: int) -> str:
    """
    Polymarket 市场的 slug 通过时间戳确定
    时间戳 = 当前 UTC 时间向下取整到最近的 N 分钟
    """
    now = int(time.time())
    interval = duration_minutes * 60  # 秒
    timestamp = (now // interval) * interval
    return f"{asset}-updown-{duration_minutes}m-{timestamp}"

def get_market_info(asset: str, duration_minutes: int):
    slug = generate_market_slug(asset, duration_minutes)

    # 通过 Gamma API 获取市场详情
    response = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"slug": slug}
    )
    markets = response.json()

    for market in markets:
        if market["slug"] == slug:
            return {
                "market_id": market["id"],
                "slug": market["slug"],
                "question": market["question"],
                "end_time": market["end_time"],
                "clob_token_ids": market["clobTokenIds"],
                "start_time": market["start_time"],
            }
    return None

# 示例
btc_5m = get_market_info("btc", 5)
btc_15m = get_market_info("btc", 15)

print(f"5m endTime:  {btc_5m['end_time']}")
print(f"15m endTime: {btc_15m['end_time']}")
print(f"是否相同: {btc_5m['end_time'] == btc_15m['end_time']}")
```

### 6.3 实时价格监控

```python
import websockets
import json
import asyncio

async def monitor_price(token_id_yes, token_id_no):
    uri = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    async with websockets.connect(uri) as ws:
        # 订阅订单簿更新
        subscribe_msg = {
            "type": "subscribe",
            "channel": "book",
            "token_id": token_id_yes
        }
        await ws.send(json.dumps(subscribe_msg))

        async for message in ws:
            data = json.loads(message)

            if data["type"] == "book":
                best_bid = data["bids"][0]["price"]
                best_ask = data["asks"][0]["price"]
                # 存储价格，计算套利机会
                check_arbitrage(token_id_yes, best_ask, token_id_no, ...)
```

---

## 七、下一步行动计划

### 7.1 当前优先级

| 优先级 | 行动项 | 难度 | 说明 |
|-------|--------|------|------|
| **1** | 搭建市场发现模块 | 低 | 获取 5m/15m 市场 slug 和 token ID |
| **2** | 搭建价格监控模块 | 中 | WebSocket 实时获取订单簿 |
| **3** | 实现 Spread 套利（YES+NO < 1） | 低 | 最简单，收益稳定 |
| **4** | 验证 Beat Price 数据源 | 高 | 确认能否获取 beatPrice |
| **5** | 实现 Beat Price 套利 | 高 | 需要 endTime 匹配 |
| **6** | 模拟盘回测 | 中 | 收集历史数据验证策略 |
| **7** | 实盘小资金测试 | 中 | 逐步放大仓位 |

### 7.2 数据需求

```
需要收集的历史数据：

1. 5m 市场和 15m 市场的：
   - slug / market_id
   - end_time（必须相同才算有效套利对）
   - beat_price（边界价格）
   - start_price（起始价格）
   - YES_token_id / NO_token_id

2. 每个市场的实时价格数据：
   - YES_best_bid / YES_best_ask
   - NO_best_bid / NO_best_ask
   - 每隔 1 秒采样

3. 结算结果：
   - 最终结算价（Chainlink 喂价）
   - UP / DOWN 胜负结果
```

---

## 八、附录：开源 Bot 参考

| 项目 | 语言 | 策略 | 链接 |
|------|------|------|------|
| `horsedev77/polymarket_5minute_15minute-arbitrage-bot` | TypeScript | Beat Price 套利 | GitHub |
| `rvenandowsley/Polymarket-crypto-5min-arbitrage-bot` | Rust | YES+NO Spread | GitHub |
| `rvenandowsley/Polymarket-crypto-15min-arbitrage-bot` | Rust | YES+NO Spread | GitHub |
| `handiko/Polymarket-Market-Finder` | Python | 市场发现 | GitHub |

---

## 九、核心结论

1. **Beat Price 套利**（5m vs 15m 共享 endTime）是最核心的套利策略，但依赖 `beatPrice` 数据可得性，且不是严格无风险的（概率优势套利）

2. **YES+NO Spread 套利**是最简单可靠的策略，当 `YES_ask + NO_ask < 1` 时确定性盈利，但窗口极短（~2.7秒），需要 sub-100ms 执行

3. **概率偏差套利**逻辑清晰但假设脆弱（假设 5min 周期独立），实际表现取决于市场特性

4. **关键瓶颈**：执行速度。所有套利机会的窗口都在秒级，高频交易基础设施是关键门槛
