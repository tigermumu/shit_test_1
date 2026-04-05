# PDA‑Zenith（Polymarket‑Deribit Arbitrage Zenith）当前实现逻辑说明

本文档描述当前仓库 `polymarket_deribit/` 目录下的“盘口监控 + N‑Solver + Shadow Tracker + Dashboard”完整闭环逻辑，用于你后续把同一套思路喂给 Trae/Cursor/Windsurf 做进一步扩展（实盘执行、更多标的、多组合等）。

---

## 0. 目标与原则

当前阶段定位为“数据收集与验证”，核心目标不是过滤信号，而是：

- 用真实盘口（含深度、点差）评估“是否存在可行对冲张数 N 区间”
- 生成影子交易（Shadow Trades）并持续追踪其 Mark/Realizable ROI 路径
- 将所有记录以 CSV 导出，便于你做回归分析与压力测试

关键原则：

- 盘口相关计算默认只用 L1（第一档）做触发判断（避免流动性幻影）
- 价值评估分两种口径：
  - Mark（研究用：mid/mark 估值）
  - Realizable（执行用：bid/ask 可实现估值）

---

## 1. 系统拓扑

- Polymarket Gamma API：用于通过 event slug 找到对应的 market/token
- Polymarket CLOB API：用于拉取订单簿（`/book`）
- Deribit Public API：用于拉取期权订单簿（`/api/v2/public/get_order_book`，币本位，含 `index_price` 与 greeks）

本服务提供：

- `/`：订单簿展示（含 USD 深度与累计深度）
- `/dashboard`：Zenith 监控面板（N‑Solver 结果、Shadow ROI 曲线、Shadow 记录表、CSV 下载）
- `/api/v1/*`：JSON 接口（订单簿、collector 状态、数据导出）

入口代码：

- [main.py](file:///f:/workshop/polymarket/polymarket_deribit/pda/main.py)

---

## 2. 数据源与“统一单位”规则

### 2.1 Polymarket

- Event 定位：Gamma `GET /events?slug=<event_slug>`
- Token 定位：从 event 下的 market 里解析 `clobTokenIds`，取 Yes tokenId
- Orderbook：CLOB `GET /book?token_id=<yes_token_id>`

单位：

- price：USDC/Share（0~1）
- size：Share 数量
- 单档 USD 深度：`price * size`

实现位置：

- Event/Token 解析：[polymarket.py](file:///f:/workshop/polymarket/polymarket_deribit/pda/clients/polymarket.py)

### 2.2 Deribit（币本位期权）

- Instrument 生成：`BTC-10APR26-70000-P` 这类格式
- Orderbook：优先走 `https://www.deribit.com/api/v2/public/get_order_book?depth=<n>&instrument_name=<instrument>`
- 取用字段：
  - `bids/asks`（价格单位 BTC）
  - `index_price`（USD/BTC）
  - `mark_price`（BTC）
  - `greeks.delta/theta`（用于概率代理与 theta 观察）

单位换算：

- Deribit 单档 premium（USD/contract）：`ask_btc * index_price`
- Deribit 单档 USD 深度：`premium_usd * contracts`

实现位置：

- [deribit.py](file:///f:/workshop/polymarket/polymarket_deribit/pda/clients/deribit.py)

---

## 3. 订单簿深度展示（/ 与 /api/v1/orderbooks）

### 3.1 排序规则

- bids：按 price 降序（最高价为 best bid）
- asks：按 price 升序（最低价为 best ask）

### 3.2 USD 深度与累计深度

对每一档 level 增加：

- `notional_native = price * size`
- `notional_usd`：
  - Polymarket：`notional_native`
  - Deribit：`notional_native * index_price`
- `cum_notional_usd`：从最优价向下逐档累加

实现位置：

- [orderbook.py:add_usd_depth](file:///f:/workshop/polymarket/polymarket_deribit/pda/orderbook.py#L93-L111)

---

## 4. PDA‑Zenith 核心：Liquidity Aligner + N‑Solver + Shadow Tracker

这一段是当前版本的“策略内核”（不下单、只做影子记录）。

入口位置：

- [main.py:_collector_loop](file:///f:/workshop/polymarket/polymarket_deribit/pda/main.py)

### 4.1 Liquidity Aligner（只取第一档，避免流动性幻影）

从实时订单簿取 L1：

- Polymarket：
  - `poly_l1_ask`：Yes 的第一档 ask（price、size）
  - `poly_l1_ask_depth_usd = ask_price * ask_size`
- Deribit：
  - `deribit_l1_ask`：Put 的第一档 ask（price_btc、contracts）
  - `deribit_l1_ask_premium_usd = ask_btc * index_price`
  - `deribit_l1_ask_depth_usd = premium_usd * contracts`

有效可成交容量：

`effective_usd = min(max_capital_usd, poly_l1_ask_depth_usd, deribit_l1_ask_depth_usd)`

### 4.2 Safety Filters（过滤逻辑）

当前实现的过滤逻辑（与设计文档一致）：

- 点差过大：任一市场 `spread_pct > 5%` → 不 executable
- 流动性过小：`effective_usd < 100` → 不 executable

其中：

- `spread_pct = (best_ask - best_bid) / mid`

### 4.3 N‑Solver（实时解 N 区间）

目标：在当前盘口下寻找一个对冲张数 N，使组合在你指定的两个场景下都满足收益约束。

当前实现将“成交规模”统一为 `budget_usd = effective_usd`，并按以下方式建模：

- 购买 Polymarket Yes：
  - shares = `budget_usd / P_poly_ask`
  - 成本（USD）≈ `budget_usd`
  - 上涨场景（到期 S≥K）收益：`shares - budget_usd`
- 购买 Deribit Put（以 premium 计价）：
  - 每张期权 premium（USD）≈ `P_deri_ask_premium_usd`
  - 成本（USD）≈ `N * premium_usd`
  - 下跌保护（到期 S = K - ΔS）简化收益：`N * ΔS - N * premium_usd`

在该简化模型下，代码里对 N 区间的求解与解释如下（与 [solve_n_range](file:///f:/workshop/polymarket/polymarket_deribit/pda/orderbook.py#L201-L287) 完全一致）。

记：

- `p = P_poly_ask`（Polymarket Yes 的 L1 ask 价格，0~1）
- `q = P_deri_ask_premium_usd`（Deribit Put 的 L1 ask premium，已换算成 USD/contract）
- `w = budget_usd`（本轮有效可成交规模 `effective_usd`）
- `ds = ΔS`（保护深度，默认 500 USD）
- `t = target_profit_pct`（目标利润率，例如 0.03）
- `fee_pct = fees_pct_total`（总手续费/滑点的比例化近似，按 `(w + N*q)` 计提）
- `m = max_deadzone_loss_pct`（允许的“最惨点”亏损占比上限，默认 15%）

并定义在“建议 N 下”的简化到期 PnL：

- `shares = w / p`
- `Fees(N) = fee_pct * (w + N*q)`
- `Cost_total(N) = w + N*q + Fees(N)`
- 上涨场景（S≥K）：`PnL_up(N) = (shares - w) - N*q - Fees(N)`
- 下跌场景（S<K）：`PnL_down(S, N) = N*(K - S) - (w + N*q) - Fees(N)`

代码的约束是：两种场景都要满足 `PnL > t * Cost_total`，由此推得区间：

- `N_min = w*(1+t) / ( ds - q*(1+t) )`
  - 需要 `ds - q*(1+t) > 0`，否则直接判定不可行（reason=`premium_too_high_for_protection`）
- `N_max = w*( 1/p - 1 - t ) / ( q*(1+t) )`
  - 需要 `1/p - 1 - t > 0`，否则直接判定不可行（reason=`poly_upside_not_enough`）
- 若 Deribit L1 的合约数量上限为 `C1`，则 `N_max = min(N_max, C1)`（流动性约束）
- 额外约束（避免“死区最惨点”过深）：在代码里用 `S=K` 的亏损占比上限 `m` 对 `N_max` 再做一次裁剪
- 若 `N_min >= N_max`，判定不可行（reason=`no_feasible_n_range`）

在可行区间存在时，代码会给一个推荐值：

- `N_star = (w/p) / ds`（等价于 `shares / ds`）
- `suggested_n = clamp(N_star, N_min, N_max)`

并在 `suggested_n` 上做“全路径”压力测试（从 `K-2000` 到 `K+1000`，步长 50）：

- 生成 `PnL(S)` 序列并取最小值：`worst_case_profit_usd = min_S PnL(S)`
- `worst_case_roi_pct = worst_case_profit_usd / Cost_total(suggested_n) * 100`
- 自动估算盈亏平衡点：
  - `lower_breakeven`：`PnL(S)=0` 的下方根（若存在）
  - `upper_breakeven`：`PnL(S)=0` 的上方根（若存在；在“买 Yes + 买 Put”的简化模型中可能不存在）
- 计算“死区”：
  - `max_loss_usd = |min_S PnL(S)|`
  - `deadzone_width_usd`：包含 `S=K` 的那段连续负收益区间宽度
- 期望值（研究用）：
  - `expected_profit_usd = p_above*PnL_up(suggested_n) + (1-p_above)*PnL_down(K-2000, suggested_n)`
  - `expected_roi_pct = expected_profit_usd / Cost_total(suggested_n) * 100`
  - 其中 `p_above` 来自 `Prob(Above) ≈ 1 + delta_put`（若不在 0~1 则期望值不计算）
- 可执行判定（Zenith 升级版）：
  - 当前价格（用 Deribit `index_price` 代理）落在盈利区间，或 `deadzone_width_usd <= 400` 才标记 `is_executable=true`

实现位置：

- [orderbook.py:solve_n_range](file:///f:/workshop/polymarket/polymarket_deribit/pda/orderbook.py#L201-L287)

重要说明：

- 这里的到期收益模型是“研究阶段简化版”，没有引入：
  - Deribit 真正的到期 payoff `max(K - S_T, 0)`（而是用 ΔS 截断保护深度）
  - Polymarket 方向切换（买 Yes / 卖 Yes / 买 No）的多路径
  - 手续费、maker/taker、资金费率等
- 你可以把该求解器当作“是否存在正区间”的快速筛选器，后续可替换为更精确的到期积分模型。

### 4.4 Shadow Tracker（影子交易）

逻辑：

- 当某个时刻 `is_executable=true`，系统创建一笔 shadow trade（不会真实下单），记录：
  - `trade_id`
  - entry 时刻的 `budget_usd`、`poly_ask_entry`、`deribit_ask_premium_usd`
  - `n_min/n_max/suggested_n`
  - `worst_case_roi_pct/expected_roi_pct`
- 每轮轮询会对所有 shadow trades 进行更新估值并写入记录行：
  - Mark 估值：
    - Polymarket 用 `mid`
    - Deribit 用 `mark_price`（若缺失则用 `mid`）
  - Realizable 估值：
    - Polymarket 用 `best_bid`
    - Deribit 用 `best_bid`（再乘 `index_price` 折 USD）
  - 输出字段：
    - `mark_value_usd/mark_pnl_usd/mark_roi_pct`
    - `realizable_value_usd/realizable_pnl_usd/realizable_roi_pct`

记录上限：

- `PDA_MAX_SHADOW_TRADES`（默认 50），超过后会移除最早的 trade。

---

## 5. Dashboard 视图（/dashboard）

Dashboard 分为三块：

### 5.1 KPI（当前盘口扫描结果）

显示当前扫描 snapshot：

- 是否可执行：`is_executable`
- L1 ask 深度、effective_usd
- N 区间与推荐值：`n_min/n_max/suggested_n`
- 最坏/期望 ROI：`worst_case_roi_pct/expected_roi_pct`
- gap、deribit greeks、shadow 数量等

### 5.2 Shadow ROI 曲线

- 曲线：`mark_roi_pct`（研究用）
- 0% 与 10% 参考线（可视化阈值）

### 5.3 Shadow 记录表 + 导出

- 表格展示 shadow 记录（每条 trade 的 ROI 路径）
- 导出按钮：`/api/v1/collector/export.csv`
  - 返回 UTF‑8 CSV（Excel 可直接打开）

---

## 6. API 一览

### 6.1 Orderbook

- `GET /api/v1/orderbooks`
  - 参数：`date_iso/strike/currency/option_type/depth/poly_event_slug`
  - 返回：两边订单簿 + USD 深度 + 累计深度 + 汇总

### 6.2 Collector / Shadow

- `GET /api/v1/collector/config`
  - 返回：collector 配置 + 当前扫描 snapshot + last_error
- `GET /api/v1/collector/latest`
  - 返回：最新一行记录 + 记录数量
- `GET /api/v1/collector/rows?limit=200`
  - 返回：最近 N 行记录
- `GET /api/v1/collector/export.csv`
  - 下载全部已记录数据（CSV）

---

## 7. 环境变量（Railway 可直接录入）

基础（必需/常用）：

```json
{
  "PDA_COLLECTOR_ENABLED": "true",
  "PDA_POLY_EVENT_SLUG": "bitcoin-above-on-april-10",
  "PDA_DEFAULT_DATE": "2026-04-10",
  "PDA_DEFAULT_STRIKE": "70000",
  "PDA_DEFAULT_OPTION_TYPE": "P",
  "PDA_COLLECTOR_POLL_S": "15",
  "PDA_COLLECTOR_DEPTH": "15",
  "PDA_COLLECTOR_MAX_ROWS": "3000"
}
```

Zenith 求解/过滤参数：

```json
{
  "PDA_MAX_CAPITAL_USD": "1000",
  "PDA_DELTA_S_USD": "500",
  "PDA_TARGET_PROFIT_PCT": "0.03",
  "PDA_MAX_SHADOW_TRADES": "50"
}
```

数据导出（可选）：

- `PDA_COLLECTOR_WRITE_CSV=false`（Railway 文件系统不保证长期持久；更推荐用导出接口抓取）

---

## 8. 部署（Railway）

本目录自带 Railway 配置：

- [railway.json](file:///f:/workshop/polymarket/polymarket_deribit/railway.json)

启动命令：

`uvicorn pda.main:app --host 0.0.0.0 --port $PORT`

部署后入口：

- `/`：订单簿
- `/dashboard`：Zenith 面板

---

## 9. 当前实现的已知限制（下一步迭代方向）

- N‑Solver 采用研究用简化到期模型：
  - Deribit payoff 以 ΔS 截断保护深度近似，不是完整 `max(K-S,0)`
- 未计入手续费与成交滑点的结构化影响：
  - 目前只做点差过滤与 L1 深度约束
- Shadow Trades 的持久化：
  - 内存 + 导出 CSV；在 Railway 上更建议后续接 Redis/SQLite/对象存储做长期留存
- 时间对齐（待做）：
  - 先不作为 executable 过滤条件，但需要持续记录与观察
  - 后续可加入过滤：Polymarket endDate 与 Deribit 交割时间偏差 `> 2h` 则放弃
  - 交割时间建议从 Deribit instrument 元数据精确获取，而不是固定假设
