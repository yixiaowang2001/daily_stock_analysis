# Agent 回测实验台

Agent 回测实验台用于把多个风格不同的操盘手放进同一组 A 股关注列表中做纸面交易实验。实验默认创建“短线 / 中线 / 长线”三个操盘手，也可以后续追加新的隔离操盘手，例如更激进的短线交易员。它与现有 `/backtest` 的差异是：现有回测评价历史分析建议；Agent 回测实验台记录操盘手在某个时间点看到了什么、做了什么决定、什么时候加入实验、订单何时生效、最终如何成交和落账。

## 设计边界

- **风格固定，策略可演进**：短线、中线、长线只是默认风格边界；后续新增 profile 也使用同一套版本化策略机制。具体交易策略用 `policy_version` 保存，可通过收盘复盘追加新版本。新版本只影响未来决策，不改写历史。
- **上下文隔离**：每个 profile 拥有独立 `context_namespace`、独立组合账户和独立策略版本；任意操盘手都不能读取其他 profile 的决策和持仓。
- **加入时间可追踪**：profile 的 `created_at` 表示该操盘手加入实验的时间；后续新增操盘手从自己的加入时间和初始资金开始记录，不补写过去的观察和决策。
- **A 股规则优先**：当前 MVP 支持 6 位 A 股代码、100 股整数手、T+1 卖出约束、买入现金校验、手续费/印花税估算和组合账本落账。
- **时间戳留痕**：观察记录包含 `data_cutoff_at`；决策记录包含 `decision_time`；订单包含 `submitted_at` 和 `effective_at`，用于表达“Agent 需要思考，成交价格可能已经变化”的情况。
- **不内置硬策略**：默认 profile 只描述短/中/长风格和风险边界，不写死指标组合、买卖公式或持仓周期。
- **关注池与持仓解耦**：实验股票池表示允许新开仓/加仓的关注范围；看盘研究全集覆盖关注池和所有 active profile 当前持仓标的，并给每个 profile 注入同一份多维 `symbol_facts`。某个 profile 已持有但后来被移出股票池的标的会标记为该 profile 的 `exit_only_symbols`，可继续研究、持有、减仓或卖出；其他 profile 持有但当前 profile 未持有且不在关注池的标的只作为 `research_only_symbols` 提供市场观察。

## 数据表

| 表 | 说明 |
| --- | --- |
| `agent_backtest_runs` | 一次实验根记录：股票池、起止日期、每个操盘手初始资金、每日观察次数、A 股规则版本和成本参数 |
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
| `/runs` | `POST` | 创建实验，默认生成短线、中线、长线三个 profile |
| `/runs` | `GET` | 查询实验列表 |
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
| `/runs/{run_id}/events` | `GET` | 查询观察、决策、订单、成交和净值记录 |

## Web 界面

Web 前端入口：`/agent-backtest`，页面名称为“操盘”。该页面不挂在 DSA 主导航中，作为独立的多操盘手对比视图使用。

页面默认加载最新实验，展示：

- 曲线区：用每日 16:00 后生成的 `daily-nav` 快照展示所有 active 操盘手，可在累计收益率和账户总权益资金曲线之间切换。
- 操盘手卡片：展示当前收益、当前权益、持仓数量、当前策略版本、加入时间和最新决策；点击卡片会打开居中详情弹窗，蒙版下默认展示最新策略正文，可点击策略卡片右上角版本号只读切换历史策略版本，以横向表格展示当前持仓、建仓均价、当前价、浮动涨跌和估值状态，并在最新决策下方列出历史决策。详情弹窗左右侧可切换上一位 / 下一位操盘手，也支持键盘方向键切换，便于连续浏览所有 active profile。所有决策动作（持有 / 买入 / 卖出）用带颜色的加粗标签呈现。新加入且尚无净值快照的 profile 会先以初始资金展示。
- 实验概览：只读展示观察股票池、股票名称、规则、状态和每个操盘手每日允许复盘决策次数；股票池和次数调整通过后台/API 写入。
- 事件流：观察、决策、订单、成交和净值快照，支持按操盘手过滤；长文点击文本本身即可展开/收起。

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

每轮看盘会先为研究全集生成同一份 `symbol_facts`，再注入每个 profile 的隔离上下文。`symbol_facts` 固定保留行情/日线兼容字段，并按同一结构提供 `market_data`、`technical_context`、`fundamental_context`、`information_context` 和 `sentiment_context`。短线、中线、长线的差异只体现在策略解释和取舍上：例如短线可以重点看技术和情绪，长线可以重点看基本面和长周期结构，但数据采集层不会因为 profile 风格不同而少给某一类证据。外部增强链路不可用时以 `skipped` / `failed` 标记，保持 fail-open。

### Codex CLI 备用方案

如果 Codex Desktop automation 不可用，本地 macOS 仍可以把脚本交给 `launchd` 或其他定时器调用。当前仓库提供：

- `scripts/run_agent_backtest_codex_schedule.sh`：根据当前北京时间推断 `morning` / `late_morning` / `pre_noon` / `midday` / `tail` / `close`，再调用 `codex exec`。

检查配置和 dry-run：

```bash
scripts/run_agent_backtest_codex_schedule.sh --phase morning --dry-run
```

日志会写入 `.claude/reviews/agent_backtest/run_<id>/logs/`。如果要换实验，通过 `AGENT_BACKTEST_RUN_ID` 环境变量指定；如果仓库不在当前脚本所在 repo，通过 `AGENT_BACKTEST_REPO_ROOT` 指定。

## 后续演进

- 接入分钟线回放器，在 `effective_at` 之后自动撮合限价/市价订单。
- 接入 Agent runner，让短/中/长 profile 根据独立上下文自动生成观察计划、决策和复盘。
- 增加 Web 页面展示今日观察次数、持仓、订单、未成交原因和策略版本演进。
- 增加规则版本表，按交易日期加载沪深北不同板块的涨跌停、ST、新股和停牌规则。
