# DSA Agent Skill 接入说明

本文说明 daily_stock_analysis（DSA）仓库与股票相关 Agent Skill 的关系，帮助在不改变业务功能的前提下，把“这个 repo 该接哪个 skill”说清楚。

## 结论

这几个股票相关 skill 都和本仓库有关，但分工不同：

| Skill | 所属层级 | 适用场景 | 与本仓库的关系 |
| --- | --- | --- | --- |
| `dsa-stock-analysis` | 仓库级 skill，可软链接为用户级 / 全局 skill | 单只股票、单个持仓、小批量非策略股票逐票分析、成本价下的持有/减仓/止损判断、历史点位回看 | 复用 DSA 的行情、K 线、筹码、资金流、基本面、历史报告与搜索模块，输出事实包后由 Agent 独立判断；可将 Agent 时点研究笔记保存到 `analysis_history` |
| `dsa-watchlist-daily-review` | 仓库级 skill，可软链接为用户级 / 全局 skill | 收盘后关注列表复盘、每日定时自动化、10-20 只股票按短线/中线/长线分别排序，并输出建仓、目标、止损区间 | 复用 `dsa-stock-analysis` 的事实采集与笔记保存脚本，并补充 A 股 1 分钟证据；Agent 负责横向排序、价位框架、风险扣分与自动化交付 |
| `dsa-candidate-lab` | 用户级 / 全局 skill | 多候选池评分、策略实验、版本对比、复盘学习；包含尾盘、未来日/周动量等 profile | 以本仓库的尾盘战术台 API、存储、Web 页面和候选事实包作为当前主要落地面 |
| `tail-picking-agent` | 仓库级兼容 skill | 用户明确提到尾盘选股智能体、尾盘实验、14:40 评分、T+1 复盘 | 兼容入口；新逻辑应映射到 `dsa-candidate-lab` 的 `tail-session-t1` profile |
| 尾盘战术台 Codex 自动复盘 | Codex Desktop recurring automation，不是 skill | 每个 A 股工作日 11:30 复盘上一交易日已保存的尾盘实验 | 由 `scripts/run_tail_tactics_codex_review.py` 生成复盘上下文、补早盘指标并写回复盘结果；由 `scripts/install_tail_tactics_codex_automation.py` 注册自动化 |
| Agent 回测操盘自动化 | Codex Desktop recurring automation，不是 skill | 默认短/中/长三个隔离操盘手，并支持后续新增 active profile；在 `0940看盘`、`1030看盘`、`1120看盘`、`1335看盘`、`1440看盘` 决策，并在 16:05 `收盘复盘` 记录净值、自评和可选策略迭代 | 由本仓库的 `agent-backtest` API、Web 独立页 `/agent-backtest`、`scripts/run_agent_backtest_cycle.py` 和 `scripts/install_agent_backtest_codex_automations.py` 承载；Codex 是决策者，repo 内保存上下文、决策、订单、成交、净值和前向策略版本 |
| `daily-stock-analysis` / openclaw Skill | 外部集成 skill | openclaw 或其他外部 Agent 通过 HTTP 调用 DSA REST API | 依赖已运行的 DSA API 服务，不是仓库协作规则真源 |
| 根目录 `SKILL.md` | 产品 / 外部集成说明 | 通过 Python 入口理解 DSA 的股票分析能力 | 不是仓库 AI 协作治理真源；治理规则看 `AGENTS.md` |

仓库内 AI 协作规则的唯一真源仍是 `AGENTS.md`。仓库级 skill 真源放在 `.claude/skills/`。

## 如何选择

用户只问一只股票、一个持仓，或给出少量股票并要求逐只分析而不是排名时，用 `dsa-stock-analysis`。

典型请求：

```text
用 DSA 看一下 000021
成本 31.038，这只要不要跑？
帮我分析 AAPL 的支撑、压力和风险
分别看一下 000021、AAPL、HK00700 的短中长线和跌破位
昨天你给 000021 的跌破位是多少
```

用户给出关注列表并要求每天收盘后复盘、横向排序、短中长线分别排名、建仓/目标/止损价位时，用 `dsa-watchlist-daily-review`。

典型请求：

```text
每天 16:00 帮我复盘关注列表并按短线/中线/长线排序
这里有 20 只观察票，收盘后拉分钟数据和历史数据，给我建仓区间和目标位
把 000021、600519、HK00700、AAPL 做日终关注列表排序
```

用户给出多个候选并要求排名、打分、实验、复盘、策略版本或学习闭环时，用 `dsa-candidate-lab`。

典型请求：

```text
今天尾盘候选 000001、000021、600519，帮我打分
按 14:40 截点保存尾盘实验并预测 T+1 开盘
复盘昨天的候选池，看看策略要不要调权重
```

用户显式说 `tail-picking-agent` 或“尾盘选股智能体”时，走 `tail-picking-agent` 兼容入口，但实际工作流按 `dsa-candidate-lab` 的 `tail-session-t1` profile 执行。

用户希望“每天上午盘收盘后自动复盘昨天尾盘候选”时，用尾盘战术台 Codex 自动复盘。它不是新 skill；Codex 定时任务会在 11:30 读取上一交易日 `tail_experiment`，拉取/保存早盘指标，生成 review 上下文，再由 Codex 写回复盘。

用户要比较“短线 / 中线 / 长线”以及后续新增操盘手的纸面交易收益、希望操盘手每天固定看盘并自主买卖时，用 Agent 回测操盘自动化。它不是仓库 skill，也不是 repo 内置 Agent：Codex Desktop automation 会定时唤起 Codex，由 Codex 读取每个 active profile 的隔离上下文并写回结构化决策；每次看盘允许 `observe` / `hold`，不要求必须交易。页面入口是 `/agent-backtest`，安装和运行细节见 `docs/agent-backtest-workbench.md`。

外部 Agent 只想通过部署好的 DSA 服务触发分析时，用 openclaw / HTTP Skill，参考 `docs/openclaw-skill-integration.md`。

## 接入方式

### 1. 仓库级 skill

仓库级 skill 已放在：

```text
.claude/skills/dsa-stock-analysis/
.claude/skills/dsa-watchlist-daily-review/
.claude/skills/tail-picking-agent/
```

如果当前 Agent 运行环境不会自动发现仓库级 skill，可以把它们软链接到用户级 skill 目录：

```bash
mkdir -p ~/.codex/skills
ln -s "$PWD/.claude/skills/dsa-stock-analysis" ~/.codex/skills/dsa-stock-analysis
ln -s "$PWD/.claude/skills/dsa-watchlist-daily-review" ~/.codex/skills/dsa-watchlist-daily-review
ln -s "$PWD/.claude/skills/tail-picking-agent" ~/.codex/skills/tail-picking-agent
```

如果同名目录已存在，先确认它是否是旧版本，避免覆盖用户级 skill。

`dsa-stock-analysis` 的脚本支持从任意工作目录运行，会按 `DSA_REPO_ROOT`、软链接目标、当前目录和默认仓库路径查找 DSA 根目录，并在加载配置前切回仓库根目录，避免相对 `DATABASE_PATH` 写到错误位置。常用入口：

```bash
python ~/.codex/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 AAPL --days 120 --save-db --include-latest-report
python ~/.codex/skills/dsa-stock-analysis/scripts/save_stock_analysis_note.py note.json
```

其中 `save_stock_analysis_note.py` 用于把 Agent 生成的短线/中线/长线观点、支撑位、压力位、跌破位、建仓/持仓计划和数据截点保存成 `analysis_history.report_type=agent_note`，供后续按日期回看。短线点位默认只代表当时数据截点，后续对话读取时必须说明 `analysis_date` / `data_cutoff`，过了下一交易窗口应视为参考而非当前结论。

`dsa-watchlist-daily-review` 的常用自动化采集入口：

```bash
python ~/.codex/skills/dsa-watchlist-daily-review/scripts/collect_watchlist_context.py 000021 600519 --days 160 --save-db --include-news --include-latest-report
python ~/.codex/skills/dsa-watchlist-daily-review/scripts/collect_watchlist_context.py --watchlist-file watchlist.txt --trade-date 2026-05-25 --cutoff-time 15:00
```

该入口先复用 `dsa-stock-analysis` 的事实采集，再对 A 股补充截至收盘附近的 1 分钟证据；它只生成证据包，不直接替 Agent 做结论排序。

### 1.1 问财兜底数据源

如配置 `IWENCAI_API_KEY`，DSA 会把同花顺问财 OpenAPI 作为低优先级兜底接入：

- 实时行情：`iwencai` 会追加到 `REALTIME_SOURCE_PRIORITY` 末尾，仅在腾讯/新浪/东财/Tushare/长桥等常规源失败后调用。
- 新闻搜索：`Iwencai` provider 会追加到常规搜索 provider 之后，仅在前置搜索失败或过滤后无可用结果时尝试。
- 调用保护：实时行情和搜索共用 `IWENCAI_DAILY_CALL_LIMIT`（默认 100 次/天），本地计数写入 `IWENCAI_USAGE_PATH`。
- 边界：问财兜底不替代日线 K 线、分钟 K 线或筹码分布；这些缺失时仍需在 `未验证` 中说明。

Agent 使用 `dsa-stock-analysis` 或 `dsa-watchlist-daily-review` 时，如果事实包出现 `quote.source == "iwencai"` 或 `news.provider == "Iwencai"`，最终回答必须标注该证据来自同花顺问财兜底，不能写成主数据源正常返回。

### 2. `dsa-candidate-lab`

`dsa-candidate-lab` 当前是用户级 / 全局 skill，默认路径：

```text
~/.codex/skills/dsa-candidate-lab/
```

它不直接替代本仓库代码，而是调用或参考本仓库的候选池实验能力。当前尾盘相关落地点包括：

- `api/v1/endpoints/tail_tactics.py`
- `api/v1/schemas/tail_tactics.py`
- `src/services/tail_tactics_compose.py`
- `src/services/tail_candidate_facts.py`
- `src/services/tail_conversation_archive.py`
- `src/storage.py`
- `apps/dsa-web/src/pages/TailTacticsPage.tsx`
- `docs/tail-tactics-workbench.md`
- `docs/tail-picking-agent-design.md`

后续如果把 `dsa-candidate-lab` 也纳入版本库，应先决定它是否迁入 `.claude/skills/`，再同步更新 `AGENTS.md`、`.claude/skills/README.md` 和 `scripts/check_ai_assets.py`。

### 3. 外部 HTTP Skill

外部 Agent 不需要读取仓库 skill 文件，只需要运行 DSA API：

```bash
python main.py --serve-only
```

然后把外部 skill 的 `DSA_BASE_URL` 指向服务地址，例如：

```text
http://localhost:8000
```

详见 `docs/openclaw-skill-integration.md`。

### 4. Agent 回测操盘自动化

操盘自动化使用 Codex Desktop recurring automation，不通过 `.claude/skills/` 注册。clone 到新电脑后，先创建回测实验，再安装自动化：

```bash
python scripts/run_agent_backtest_cycle.py prepare \
  --name "A股三周期纸面交易实验" \
  --symbols "002975,002222,603083,603267,002156,600481,002463,603660,000636,603678,603601,603881,002371" \
  --initial-cash-per-agent 20000 \
  --max-observations-per-day 5

python scripts/install_agent_backtest_codex_automations.py --run-id <prepare 输出的 run.id>
```

自动化会注册六个本地任务：`0940看盘`、`1030看盘`、`1120看盘`、`1335看盘`、`1440看盘` 和 `收盘复盘`。前五个任务为所有 active profile 生成隔离上下文，Codex 分别读取并调用 `apply-decision`；收盘复盘写入 `daily-nav`，逐个 profile 生成自评 markdown，并在有稳定复盘结论时用 `evolve-policy` 写入新的前向策略版本。收盘复盘应使用 `--live-data` 让持仓按当日可靠价格估值。runner 会把已移出股票池但仍持有的标的标记为 `exit_only_symbols`，让操盘手继续研究、持有、减仓或卖出，但不作为重新买入候选。收盘复盘必须检查持仓 payload 的 `valuation_date`、`valuation_source` 和 `valuation_stale`；若当日日线或实时价缺失，不能把该 NAV 当作最终排名。 如果 Codex 自动化页面没有立即显示，重启 Codex Desktop。

### 5. 尾盘战术台 Codex 自动复盘

尾盘 T+1 复盘自动化也使用 Codex Desktop recurring automation，不通过 `.claude/skills/` 注册。安装：

```bash
python scripts/install_tail_tactics_codex_automation.py
```

它会注册 `dsa-tail-tactics-midday-review`，按北京时间每个工作日 `11:30` 运行。任务会先调用：

```bash
python scripts/run_tail_tactics_codex_review.py prepare-review
```

默认目标是上一 A 股交易日最新一条“已评分、未复盘”的尾盘实验。runner 会自动拉取并保存 9:30-10:00 早盘冲高指标，生成 `.claude/reviews/tail_tactics/<trade_date>/exp_<id>_review_context.md` 供 Codex 阅读。Codex 生成复盘后调用：

```bash
python scripts/run_tail_tactics_codex_review.py apply-review --experiment-id <id> --review-file <review_md_file>
```

写回 `review_note_markdown`、`case_summary` 并关闭实验。若没有符合条件的实验或当天非 A 股交易日，任务只输出跳过原因。

双层迭代规则：

- 第一层同花顺筛选逻辑是用户策略，Codex 只能把调整建议写入 `layer1_change_requests` / `.claude/reviews/tail_tactics/layer1_change_requests.md`，等待用户确认。
- 第二层 Agent 评分与预测逻辑由 Codex 自我迭代；复盘里的 `layer2_calibration_notes` 会追加到 `.claude/reviews/tail_tactics/layer2_calibration.md`，后续尾盘评分 compose 会自动读取并注入第二层上下文。
- 第二层校准记忆不得反向改写第一层同花顺筛选条件。

## 关键边界

- `dsa-stock-analysis` 不负责候选池排名、策略版本、尾盘实验或复盘持久化。
- `dsa-stock-analysis` 可以接少量股票并逐只输出，但不能把它变成横向评分、候选池排名或策略实验。
- `dsa-watchlist-daily-review` 负责用户已有关注列表的日终横向排序，不负责生成候选池筛选策略、尾盘 T+1 预测实验或策略复盘学习。
- `dsa-candidate-lab` 可以复用单票事实包，但候选池评分、排名、输出契约与复盘学习由它负责。
- `tail-picking-agent` 只保留兼容入口；新增跨策略逻辑不要继续塞进这个 alias。
- 尾盘战术台 Codex 自动复盘只复盘已经保存到 `tail_experiment` 的尾盘实验，不自动生成候选池，也不替代 14:40 尾盘评分。
- Agent 回测操盘自动化不负责生成关注列表，也不复用其他 profile 的决策；每个操盘手只能读取自己的 `*_context.md`，按 A 股交易约束写回观察、决策、订单、成交和净值。
- `.agents/skills/` 如需存在，应视为 `.claude/skills/` 的本地镜像或适配目录，不作为手工维护的第二真源。
- 所有 skill 输出都属于研究和风险框架，不是投资指令或自动交易建议。

## 验证

修改仓库级 skill 或 AI 协作资产后，执行：

```bash
python scripts/check_ai_assets.py
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .claude/skills/dsa-watchlist-daily-review
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .claude/skills/dsa-stock-analysis
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .claude/skills/tail-picking-agent
python -m py_compile .claude/skills/dsa-watchlist-daily-review/scripts/collect_watchlist_context.py
python -m py_compile .claude/skills/dsa-stock-analysis/scripts/collect_stock_context.py .claude/skills/dsa-stock-analysis/scripts/save_stock_analysis_note.py
python -m py_compile scripts/install_agent_backtest_codex_automations.py scripts/run_agent_backtest_cycle.py
```

如修改 `dsa-stock-analysis` 的采集脚本，再补充：

```bash
python .claude/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 --days 30 --no-save-db
```

如修改尾盘战术台后端或前端，按 `AGENTS.md` 的改动面验证矩阵执行对应测试与构建。
