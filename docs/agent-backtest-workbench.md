# Agent 回测实验台

Agent 回测实验台用于把多个风格不同的操盘手放进同一组关注列表中做纸面交易实验。实验按市场隔离运行：A 股 run 默认创建 A 股短线 / 中线 / 长线三个操盘手，美股 run 默认创建美股现金账户短线 / 中线 / 长线三个操盘手。它与现有 `/backtest` 的差异是：现有回测评价历史分析建议；Agent 回测实验台记录操盘手在某个时间点看到了什么、做了什么决定、什么时候加入实验、订单何时生效、最终如何成交和落账。

## 设计边界

- **风格固定，策略可演进**：短线、中线、长线只是默认风格边界；后续新增 profile 也使用同一套版本化策略机制。具体交易策略用 `policy_version` 保存，可通过收盘复盘追加新版本。新版本只影响未来决策，不改写历史。
- **上下文隔离**：每个 profile 拥有独立 `context_namespace`、独立组合账户和独立策略版本；任意操盘手都不能读取其他 profile 的决策和持仓。
- **加入时间可追踪**：profile 的 `created_at` 表示该操盘手加入实验的时间；后续新增操盘手从自己的加入时间和初始资金开始记录，不补写过去的观察和决策。
- **市场规则隔离**：A 股 run 只接受 6 位 A 股代码，执行 100 股整数手、T+1 卖出约束、买入现金校验、手续费/印花税估算和 CNY 账本落账；美股 run 只接受美股 ticker，执行 USD 现金账户、整股、settled cash、卖出资金 T+1 美股工作日可用、滚动 5 个美股工作日最多 1 次日内回转的硬约束。
- **时间戳留痕**：观察记录包含 `data_cutoff_at`；决策记录包含 `decision_time`；订单包含 `submitted_at` 和 `effective_at`，用于表达“Agent 需要思考，成交价格可能已经变化”的情况。
- **不内置硬策略**：默认 profile 只描述短/中/长风格和风险边界，不写死指标组合、买卖公式或持仓周期。
- **关注池与持仓解耦**：实验股票池表示允许新开仓/加仓的关注范围；看盘研究全集覆盖关注池和所有 active profile 当前持仓标的，并给每个 profile 注入同一份多维 `symbol_facts`。某个 profile 已持有但后来被移出股票池的标的会标记为该 profile 的 `exit_only_symbols`，可继续研究、持有、减仓或卖出；其他 profile 持有但当前 profile 未持有且不在关注池的标的只作为 `research_only_symbols` 提供市场观察。

## 数据表

| 表 | 说明 |
| --- | --- |
| `agent_backtest_runs` | 一次实验根记录：市场、股票池、起止日期、每个操盘手初始资金、每日观察次数、规则版本和成本参数 |
| `agent_backtest_profiles` | 实验中的操盘手 profile：默认 `short` / `medium` / `long`，可追加如 `aggressive_short` 的 active profile；记录组合账户、上下文命名空间、当前策略版本和加入时间 |
| `agent_backtest_policy_versions` | 策略版本历史：版本号、正文、父版本、启用日期和调整原因 |
| `agent_backtest_observations` | 每次看盘记录：交易日、观察时间、数据截止时间、证据快照和摘要 |
| `agent_backtest_decisions` | 每次决策记录：动作、理由、风险、置信度、可选订单意图和当时策略版本 |
| `agent_backtest_orders` | 模拟订单：方向、数量、限价/市价、提交时间、生效时间、状态 |
| `agent_backtest_fills` | 模拟成交：成交价、数量、费用、税费，并关联写入 `portfolio_trades` |
| `agent_backtest_daily_nav` | 每日净值快照：现金、市值、总权益、已实现/未实现盈亏和持仓 payload |

每个 profile 创建时会自动生成一个 `portfolio_accounts` 隔离账户，并写入初始现金流水。成交记录会同步写入该账户的 `portfolio_trades`，因此可以复用现有持仓快照与风险服务。

## API

前缀：`/api/v1/agent-backtest`

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/runs` | `POST` | 创建实验，`market=cn` 或 `market=us`，默认生成对应市场的短线、中线、长线三个 profile |
| `/runs` | `GET` | 查询实验列表，可用 `market=cn/us` 过滤 |
| `/runs/{run_id}` | `GET` | 查看实验详情与 profile |
| `/runs/{run_id}` | `PATCH` | 更新实验股票池和每日允许复盘决策次数 |
| `/runs/{run_id}/profiles` | `POST` | 为实验新增一个隔离操盘手 profile，并创建独立组合账户与初始策略版本 |
| `/runs/{run_id}/profiles/{profile_key}` | `DELETE` | 将某个操盘手 profile 标记为 inactive；历史记录保留，后续自动化不再处理 |
| `/runs/{run_id}/observations` | `POST` | 记录一次受限看盘，受 `max_observations_per_day` 限制 |
| `/runs/{run_id}/decisions` | `POST` | 记录一次操盘手决策 |
| `/runs/{run_id}/orders` | `POST` | 创建模拟订单 |
| `/runs/{run_id}/orders/{order_id}/fills` | `POST` | 记录成交并同步写入组合账本 |
| `/runs/{run_id}/profiles/{profile_key}/policies` | `GET` | 只读列出某个操盘手的策略版本历史，用于前端切换浏览 |
| `/runs/{run_id}/profiles/{profile_key}/policies` | `POST` | 追加策略版本，更新该 profile 的当前策略 |
| `/runs/{run_id}/daily-nav` | `POST` | 抓取所有 profile 的每日净值快照 |
| `/runs/{run_id}/events` | `GET` | 查询观察、决策、订单、成交和净值记录；可用 `include_evidence=false` 和 `include_raw_output=false` 跳过大字段，适合操盘页面、净值曲线和事件流轻量加载 |

## Web 界面

Web 前端入口：`/agent-backtest`，页面名称为“操盘”。该页面不挂在 DSA 主导航中，作为独立的多操盘手对比视图使用；页头提供 A 股 / 美股切换，切换后只加载对应 `market` 的实验列表。

页面默认加载最新实验，展示：

- 曲线区：用每日 16:00 后生成的 `daily-nav` 快照展示所有 active 操盘手，可在累计收益率和账户总权益资金曲线之间切换。
- 操盘手卡片：展示风格缩略图、当日盈亏、当前收益、当前权益、持仓数量、当前策略版本、加入时间和最新决策；当日盈亏用最新净值快照与上一净值日快照相减计算。点击卡片会打开居中详情弹窗，蒙版下默认展示最新策略正文，可点击策略卡片右上角版本号只读切换历史策略版本，以横向表格展示当前持仓、建仓均价、当前价、浮动涨跌和估值状态，并在最新决策下方列出历史决策。详情弹窗左右侧可切换上一位 / 下一位操盘手，也支持键盘方向键切换，便于连续浏览所有 active profile。所有决策动作（持有 / 买入 / 卖出）用带颜色的加粗标签呈现。新加入且尚无净值快照的 profile 会先以初始资金展示。
- 实验概览：只读展示观察股票池、股票名称、规则、状态和每个操盘手每日允许复盘决策次数；股票池和次数调整通过后台/API 写入。
- 事件流：观察、决策、订单、成交和净值快照，支持按操盘手过滤；长文点击文本本身即可展开/收起。

操盘页面默认以轻量模式读取事件流，不拉取 observation 的完整 `evidence` 和 decision 的完整 `raw_output`。这些字段仍保存在数据库中，调试或审计需要完整上下文时，可以直接调用 `/runs/{run_id}/events` 并保持 `include_evidence=true`、`include_raw_output=true`。

该页面不直接驱动 Codex 决策或手工记录收盘净值；Codex 决策与净值快照仍由 runner / 定时任务写回，以保持上下文隔离。

## MVP 使用示例

### Codex runner

`scripts/run_agent_backtest_cycle.py` 是给 Codex 定时触发使用的入口。它不会调用 repo 内置 Agent，而是为 Codex 生成每个操盘手的隔离决策上下文，再用 `apply-decision` 写回 Codex 给出的结构化决策。

```bash
# 创建实验
python scripts/run_agent_backtest_cycle.py prepare \
  --name "A股三周期纸面交易实验" \
  --symbols "600519,000001" \
  --start-date 2026-01-02 \
  --initial-cash-per-agent 20000 \
  --max-observations-per-day 5
```

```bash
# 创建美股现金账户实验；symbol 会标准化为大写 ticker，默认 USD
python scripts/run_agent_backtest_cycle.py prepare \
  --market us \
  --name "美股三周期现金账户实验" \
  --symbols "AAPL,NVDA,TSLA" \
  --start-date 2026-01-05 \
  --initial-cash-per-agent 1000 \
  --max-observations-per-day 5
```

```bash
# 为所有 active profile 分别生成 Codex 决策上下文，并记录一次观察
python scripts/run_agent_backtest_cycle.py cycle \
  --run-id 1 \
  --phase verify \
  --trade-date 2026-01-02
```

`cycle` 会把上下文写到 `.claude/reviews/agent_backtest/run_<id>/<trade_date>/<phase>/`。Codex 应逐个读取 `*_context.md`，不要跨 profile 使用信息。

```bash
# Codex 产出结构化决策后写回；hold/observe 不会创建订单
python scripts/run_agent_backtest_cycle.py apply-decision \
  --run-id 1 \
  --profile-key short \
  --trade-date 2026-01-02 \
  --decision-json '{"action":"hold","confidence":0.6,"rationale":"验证阶段不交易","risk_notes":"等待真实行情"}'
```

```bash
# 如果买卖决策生成了订单，用实际或回放价格记录成交并落到账户
python scripts/run_agent_backtest_cycle.py fill-order \
  --run-id 1 \
  --order-id 1 \
  --quantity 100 \
  --price 10.00 \
  --trade-date 2026-01-02 \
  --filled-at 09:45:00
```

```bash
# 日终生成所有 active profile 的净值快照
python scripts/run_agent_backtest_cycle.py daily-nav \
  --run-id 1 \
  --trade-date 2026-01-02
```

### Codex 自动化

推荐用 Codex Desktop 的 recurring automation 触发操盘手复盘。仓库提供可移植安装脚本：

```bash
python scripts/install_agent_backtest_codex_automations.py --run-id 1
```

脚本会把自动化写入当前用户的 `~/.codex/automations/`，并把当前仓库路径写入自动化 `cwds`。如果迁移到新电脑，先 clone 本仓库并创建对应实验，再执行安装脚本即可；如需覆盖旧配置，追加 `--force`。

```bash
python scripts/run_agent_backtest_cycle.py prepare \
  --name "A股三周期纸面交易实验" \
  --symbols "002975,002222,603083,603267,002156,600481,002463,603660,000636,603678,603601,603881,002371" \
  --initial-cash-per-agent 20000 \
  --max-observations-per-day 5

python scripts/install_agent_backtest_codex_automations.py --run-id <prepare 输出的 run.id>
```

已安装自动化按 6 个独立任务拆分：5 次盘中看盘决策 + 1 次收盘复盘。每次看盘都会写回一条结构化决策，但 `observe` / `hold` 是正常结果；系统不会要求每个时点必须交易。16:05 收盘复盘不创建买卖决策，而是记录净值、逐个 profile 生成自评，并在有稳定复盘结论时通过 `evolve-policy` 写入前向策略版本。

| 自动化 ID | 名称 | 时间 | 阶段 | 行为 |
| --- | --- | --- | --- | --- |
| `dsa-agent-backtest-morning` | `0940看盘` | 09:40 | `morning` | 为所有 active profile 生成隔离上下文，Codex 分别写回决策 |
| `dsa-agent-backtest-late-morning` | `1030看盘` | 10:30 | `late_morning` | 为所有 active profile 生成隔离上下文，Codex 分别写回决策 |
| `dsa-agent-backtest-pre-noon` | `1120看盘` | 11:20 | `pre_noon` | 为所有 active profile 生成隔离上下文，Codex 分别写回决策 |
| `dsa-agent-backtest-midday` | `1335看盘` | 13:35 | `midday` | 为所有 active profile 生成隔离上下文，Codex 分别写回决策 |
| `dsa-agent-backtest-tail` | `1440看盘` | 14:40 | `tail` | 为所有 active profile 生成隔离上下文，Codex 分别写回决策 |
| `dsa-agent-backtest-close` | `收盘复盘` | 16:05 | `close` | 记录收盘净值、自评和可选策略迭代，不触发买卖决策 |

这些任务默认工作目录为本仓库，默认实验为 `run_id=1`。看盘类任务会调用 `cycle --live-data` 生成上下文，再由 Codex 按 profile 隔离读取并调用 `apply-decision` 写回；收盘复盘任务调用 `close` 阶段记录 `daily-nav`，读取每个 profile 的 close context，保存自评 markdown，并在确有可复用策略教训时调用 `evolve-policy` 追加策略版本。上下文中的 `symbol_scope` 会拆分 `watchlist_symbols`、`held_symbols`、`own_held_symbols`、`portfolio_held_symbols`、`buy_allowed_symbols`、`exit_only_symbols` 和 `research_only_symbols`：研究全集等于关注池加所有 active 操盘手账户持仓标的，但只暴露去归属的标的列表，不暴露其他 profile 的持仓数量、成本或决策。

美股现金账户 run 建议使用更少、更贴近交易时段的独立节奏：09:40 ET `morning` 开盘看盘、14:30 ET `midday` 下午看盘、16:20 ET `close` 收盘复盘、20:30 ET `tail` 盘外/隔夜看盘。若用北京时间调度，夏令时大致对应 21:40 当日以及次日 02:30、04:20、08:30，冬令时需顺延 1 小时；收盘复盘只做净值、自评和策略迭代，不创建买卖订单。

每轮看盘会先为研究全集生成同一份 `symbol_facts`，再注入每个 profile 的隔离上下文。`symbol_facts` 固定保留行情/日线兼容字段，并按同一结构提供 `market_data`、`technical_context`、`fundamental_context`、`information_context` 和 `sentiment_context`。从 `agent_backtest_symbol_facts_v3` 起，`symbol_facts` 还会提供 `codex_research_fallback`：当内置搜索 provider 失败、无结果或被跳过时标记为 `recommended`，并给出可直接用于 Codex 补搜的查询词。短线、中线、长线的差异只体现在策略解释和取舍上：例如短线可以重点看技术和情绪，长线可以重点看基本面和长周期结构，但数据采集层不会因为 profile 风格不同而少给某一类证据。外部增强链路不可用时以 `skipped` / `failed` 标记，保持 fail-open。

Codex Desktop 自动化和本地 CLI 备用调度都会读取 `codex_research_fallback`。若关注池或持仓标的的资讯层标记为 `recommended`，Codex 应使用可用的联网/搜索/browser 工具补搜公开信息，优先交易所/公司公告与有时间戳的可靠财经媒体，只使用 `data_cutoff_at` 之前的信息，并在决策理由、风险说明和本轮总结中记录来源标题、日期和 URL；如果 Codex 当前环境没有搜索工具，则把它作为数据缺口处理。

### 美股现金账户规则

美股 run 的默认规则版本为 `us_cash_ibkr_v1`，用于模拟 IBKR 现金账户的保守约束：

- 基础货币为 USD，默认不允许碎股，下单和成交数量必须为正整数股。
- 买入只能使用 settled cash；卖出成交产生的资金不会在当日重新可用，按 T+1 美股工作日释放。当前实现按美股工作日做周末跳过，尚未内置完整美国交易所假日日历。
- 日内回转按“同一 ticker 同一交易日先买后卖”计数，滚动 5 个美股工作日最多允许 1 次；用完后服务层会拒绝第二次同类成交。
- 盘前、盘后、隔夜交易允许进入决策上下文，但策略契约要求使用限价单并显式说明流动性、价差和 session 风险。IBKR 隔夜交易的具体可交易名单与权限仍以券商实际返回为准。

现金可用时间不要写死成固定北京时间。美股标准结算按美国交易日 T+1；在中国时区观察时，夏令时和券商刷新批处理会影响“几点看到资金可用”。上下文里会写明 settled cash 与 T+1 约束，最终以券商账户可交易现金为准。

### 美股数据源

美股行情和日线使用独立 fallback 顺序，不影响 A 股数据源。默认优先级可按用途拆分：

```env
US_DAILY_DATA_SOURCE_PRIORITY=ibkr,longbridge,massive,twelvedata,finnhub,alpha_vantage,yfinance
US_REALTIME_DATA_SOURCE_PRIORITY=ibkr,longbridge,twelvedata,finnhub,alpha_vantage,massive,yfinance
US_REALTIME_STOP_AFTER_BASIC_QUOTE=false
US_INTRADAY_DATA_SOURCE_PRIORITY=massive,twelvedata,ibkr
US_INTRADAY_CACHE_DIR=data/us_intraday_cache
US_INTRADAY_429_RETRY_SECONDS=15
```

`US_DAILY_DATA_SOURCE_PRIORITY` 和 `US_REALTIME_DATA_SOURCE_PRIORITY` 未设置时，会兼容读取旧的 `US_MARKET_DATA_SOURCE_PRIORITY`。`US_REALTIME_STOP_AFTER_BASIC_QUOTE=true` 时，美股实时 quote 在首个 provider 返回基础价格后即停止，不再为了补充 PE/PB/量比等字段继续调用后续 provider；适合“实时价用外部 API，分钟线和回放证据用 IBKR”的低请求量模式。`US_INTRADAY_DATA_SOURCE_PRIORITY` 独立控制 agent 回测的美股 1 分钟历史 K，默认先用 Massive/Polygon 兼容聚合 K，再用 Twelve Data，最后才尝试 IBKR。这样在 IBKR 账号尚未开通 Level 1 API 历史行情订阅时，`intraday_cutoff` 仍能获得可遮掩、可截取的分钟证据。`US_INTRADAY_CACHE_DIR` 会按 provider/symbol/trade_date 缓存整日或已返回的分钟 rows；同一天多个回放节点会先读缓存，并且只有缓存中 cutoff 前最后一根 bar 足够接近目标时间时才复用，避免复盘三节点时反复触发 provider 限流。外部分钟源返回 HTTP 429 时会按 `US_INTRADAY_429_RETRY_SECONDS` 做短退避重试。

可选 key 环境变量：

- `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID` / `IBKR_TIMEOUT_SECONDS` / `IBKR_FAILURE_COOLDOWN_SECONDS` / `IBKR_MARKET_DATA_TYPE` / `IBKR_INTRADAY_USE_RTH`：连接本机 IBKR Gateway/TWS 获取美股日线、IBKR 历史 1 分钟 K 和行情快照。IB Gateway live 默认端口通常是 `4001`，paper 默认通常是 `4002`；当前 fetcher 只调用历史行情与 market-data snapshot，不调用下单接口。若 Gateway 端口可连但行情请求超时或无回包，`IBKR_FAILURE_COOLDOWN_SECONDS` 会让当前进程短时间跳过 IBKR，快速降级到后续来源，避免整轮美股看盘逐标的重复等待超时。IBKR API 的历史 bar 通常需要对应 Level 1 市场数据订阅；未订阅时可用 delayed streaming tick 不代表历史 1 分钟 K 一定可用。`IBKR_INTRADAY_USE_RTH=false` 时分钟 K 包含盘前/盘后，适合美股盘外/隔夜节点；设为 `true` 时仅取常规交易时段。使用 live 账号看行情时，建议在 IBKR 网站保持只读权限，并在 Gateway 端只开放本机可信 IP。
- `LONGBRIDGE_APP_KEY` / `LONGBRIDGE_APP_SECRET` / `LONGBRIDGE_ACCESS_TOKEN`：长桥 OpenAPI，美股/港股行情与盘外数据能力取决于账户权限。
- `MASSIVE_API_KEY` 或 `POLYGON_API_KEY`：Massive/Polygon 兼容聚合 K 线与 snapshot 兜底；agent 回测会用其 1 分钟 aggregates 作为默认分钟证据，覆盖盘前、常规交易和盘后。
- `TWELVEDATA_API_KEY`：Twelve Data 日线/报价兜底；agent 回测可用其 `/time_series` 1 分钟数据补分钟证据，常规时段默认可用，盘前/盘后取决于套餐是否支持 `prepost=true`。
- `FINNHUB_API_KEY`：Finnhub 日线/报价兜底；也用于美股 `fundamental_context` 的公司资料和基础财务指标快照。
- `ALPHA_VANTAGE_API_KEY`：Alpha Vantage 日线/报价兜底；也用于美股 `fundamental_context` 的 company overview / valuation / growth 快照，免费额度很小，适合低频调用。
- `yfinance` 不需要 key，但属于非官方 Yahoo Finance 包装，适合开发和低频兜底，不应作为唯一生产来源。

美股 `fundamental_context` 当前采用 Alpha Vantage / Finnhub 的最新 provider snapshot，属于 `latest_provider_snapshot_not_point_in_time`；用于历史节点复盘时可以补充长线视角，但不能当作严格历史截点基本面数据库。行情与技术层仍以 `US_INTRADAY_DATA_SOURCE_PRIORITY` 返回的 cutoff 前分钟 K 做 point-in-time 遮掩。

美股基本面接口默认使用 `US_FUNDAMENTAL_STAGE_TIMEOUT_SECONDS=4.0`、`US_FUNDAMENTAL_FETCH_TIMEOUT_SECONDS=3.0`，因为这些远程 provider 通常比 A 股本地聚合慢；未配置时不影响 A 股 `FUNDAMENTAL_*` 的快失败预算。

美股资讯搜索按用户约束采用 Codex 自身联网/搜索优先；`information_policy.codex_research_first=true` 且 `provider_search_fallback_enabled=false` 时，runner 不会先消耗 Tavily / Brave / SerpAPI / Bocha 等 provider 配额，而是在 `codex_research_fallback` 中给出英文补搜 query。需要启用 provider 搜索时，可在 run config 中显式打开。

Codex 补搜不等同于 DSA provider 正常返回。若 `information_context` 被跳过、失败、为空或对买卖判断不够充分，按仓库级 skill `.claude/skills/dsa-codex-information-fallback/SKILL.md` 执行，并把结果标注为 `Codex 外部兜底`。推荐优先级是：SEC EDGAR / 交易所 / 监管披露；公司 Investor Relations、earnings release、newsroom 和 events；有时间戳的可靠财经媒体；DSA 搜索 provider 发现到的原始来源；最后才是社交/论坛情绪。所有来源都必须记录获取时间、发布时间、URL、数据截点和是否在 `data_cutoff_at` 之前可见；截点后的同日新闻只能进入事后复盘，不能反向参与当时决策。

### Codex CLI 备用方案

如果 Codex Desktop automation 不可用，本地 macOS 仍可以把脚本交给 `launchd` 或其他定时器调用。当前仓库提供：

- `scripts/run_agent_backtest_codex_schedule.sh`：根据当前调度时区推断 `morning` / `late_morning` / `pre_noon` / `midday` / `tail` / `close`，再调用 `codex exec`。

检查配置和 dry-run：

```bash
scripts/run_agent_backtest_codex_schedule.sh --phase morning --dry-run
```

日志会写入 `.claude/reviews/agent_backtest/run_<id>/logs/`。如果要换实验，通过 `AGENT_BACKTEST_RUN_ID` 环境变量指定；如果仓库不在当前脚本所在 repo，通过 `AGENT_BACKTEST_REPO_ROOT` 指定。美股备用调度可设置 `AGENT_BACKTEST_MARKET=us`，默认时区会切到 `America/New_York`；如需用北京时间运行，显式设置 `AGENT_BACKTEST_TIMEZONE=Asia/Shanghai`。

## 后续演进

- 接入分钟线回放器，在 `effective_at` 之后自动撮合限价/市价订单。
- 接入 Agent runner，让短/中/长 profile 根据独立上下文自动生成观察计划、决策和复盘。
- 增加 Web 页面展示今日观察次数、持仓、订单、未成交原因和策略版本演进。
- 增加规则版本表，按交易日期加载沪深北不同板块的涨跌停、ST、新股和停牌规则。
