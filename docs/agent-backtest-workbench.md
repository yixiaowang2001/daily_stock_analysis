# Agent 回测实验台

Agent 回测实验台用于把“短线 / 中线 / 长线”三个操盘手放进同一组 A 股关注列表中做纸面交易实验。它与现有 `/backtest` 的差异是：现有回测评价历史分析建议；Agent 回测实验台记录操盘手在某个时间点看到了什么、做了什么决定、订单何时生效、最终如何成交和落账。

## 设计边界

- **风格固定，策略可演进**：短线、中线、长线只是风格边界；具体交易策略用 `policy_version` 版本化保存，可通过复盘追加新版本。新版本只影响未来决策，不改写历史。
- **上下文隔离**：每个 profile 拥有独立 `context_namespace`、独立组合账户和独立策略版本；短线、中线、长线互不读取彼此决策和持仓。
- **A 股规则优先**：当前 MVP 支持 6 位 A 股代码、100 股整数手、T+1 卖出约束、买入现金校验、手续费/印花税估算和组合账本落账。
- **时间戳留痕**：观察记录包含 `data_cutoff_at`；决策记录包含 `decision_time`；订单包含 `submitted_at` 和 `effective_at`，用于表达“Agent 需要思考，成交价格可能已经变化”的情况。
- **不内置硬策略**：默认 profile 只描述短/中/长风格和风险边界，不写死指标组合、买卖公式或持仓周期。

## 数据表

| 表 | 说明 |
| --- | --- |
| `agent_backtest_runs` | 一次实验根记录：股票池、起止日期、每个操盘手初始资金、每日观察次数、A 股规则版本和成本参数 |
| `agent_backtest_profiles` | 实验中的操盘手 profile：`short` / `medium` / `long`、组合账户、上下文命名空间、当前策略版本 |
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
| `/runs/{run_id}/observations` | `POST` | 记录一次受限看盘，受 `max_observations_per_day` 限制 |
| `/runs/{run_id}/decisions` | `POST` | 记录一次操盘手决策 |
| `/runs/{run_id}/orders` | `POST` | 创建模拟订单 |
| `/runs/{run_id}/orders/{order_id}/fills` | `POST` | 记录成交并同步写入组合账本 |
| `/runs/{run_id}/profiles/{profile_key}/policies` | `POST` | 追加策略版本，更新该 profile 的当前策略 |
| `/runs/{run_id}/daily-nav` | `POST` | 抓取所有 profile 的每日净值快照 |
| `/runs/{run_id}/events` | `GET` | 查询观察、决策、订单、成交和净值记录 |

## Web 界面

Web 前端入口：`/agent-backtest`，页面名称为“操盘”。该页面不挂在 DSA 主导航中，作为独立的三操盘手对比视图使用。

页面默认加载最新实验，展示：

- 收益曲线：用每日 16:00 后生成的 `daily-nav` 快照计算短线 / 中线 / 长线三个操盘手的累计收益率。
- 三个操盘手的当前收益、当前权益、持仓数量、当前策略版本和最新决策。
- 运行信息：股票池规模、A 股规则版本、每日观察次数和实验状态。
- 事件流：观察、决策、订单、成交和净值快照，支持按操盘手过滤。
- 手动刷新与手动记录指定日期收盘净值；该操作只写入 `daily-nav` 快照用于收益曲线，不触发买卖决策。

该页面只展示和触发 `daily-nav`，不直接驱动 Codex 决策；Codex 决策仍由 runner / 定时任务写回，以保持上下文隔离。

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
  --max-observations-per-day 3
```

```bash
# 为短/中/长分别生成 Codex 决策上下文，并记录一次观察
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
# 日终生成三个 profile 的净值快照
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
  --max-observations-per-day 3

python scripts/install_agent_backtest_codex_automations.py --run-id <prepare 输出的 run.id>
```

已安装自动化按 4 个独立任务拆分：

| 自动化 ID | 时间 | 阶段 | 行为 |
| --- | --- | --- | --- |
| `dsa-agent-backtest-morning` | 09:40 | `morning` | 生成短/中/长隔离上下文，Codex 分别写回决策 |
| `dsa-agent-backtest-midday` | 13:30 | `midday` | 生成短/中/长隔离上下文，Codex 分别写回决策 |
| `dsa-agent-backtest-tail` | 14:40 | `tail` | 生成短/中/长隔离上下文，Codex 分别写回决策 |
| `dsa-agent-backtest-close` | 16:05 | `close` | 只记录收盘净值快照，不触发买卖决策 |

这些任务默认工作目录为本仓库，默认实验为 `run_id=1`。复盘类任务会调用 `cycle --live-data` 生成上下文，再由 Codex 按 profile 隔离读取并调用 `apply-decision` 写回；收盘任务只调用 `close` 阶段记录 `daily-nav`。

### Codex CLI 备用方案

如果 Codex Desktop automation 不可用，本地 macOS 仍可以把脚本交给 `launchd` 或其他定时器调用。当前仓库提供：

- `scripts/run_agent_backtest_codex_schedule.sh`：根据当前北京时间推断 `morning` / `midday` / `tail` / `close`，再调用 `codex exec`。

检查配置和 dry-run：

```bash
scripts/run_agent_backtest_codex_schedule.sh --phase morning --dry-run
```

日志会写入 `.claude/reviews/agent_backtest/run_<id>/logs/`。如果要换实验，通过 `AGENT_BACKTEST_RUN_ID` 环境变量指定；如果仓库不在当前脚本所在 repo，通过 `AGENT_BACKTEST_REPO_ROOT` 指定。

## 后续演进

- 接入分钟线回放器，在 `effective_at` 之后自动撮合限价/市价订单。
- 接入 Agent runner，让短/中/长 profile 根据独立上下文自动生成观察计划、决策和复盘。
- 增加 Web 页面展示资金曲线、今日观察次数、持仓、订单、未成交原因和策略版本演进。
- 增加规则版本表，按交易日期加载沪深北不同板块的涨跌停、ST、新股和停牌规则。
