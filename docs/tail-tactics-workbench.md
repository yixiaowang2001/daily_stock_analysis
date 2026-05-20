# 尾盘战术台（Tail Tactics Workbench）

本页说明 Web 端「战术台」功能的数据含义、与 Agent 的衔接方式及使用边界。完整产品方法论见 [A 股尾盘选股智能体设计](tail-picking-agent-design.md)。

## 定位

- 面向 **A 股短线/情绪类** 研究流程：你在 **T 日尾盘** 用自有规则筛出小批量候选（通常不超过 10 只），在战术台登记 **策略版本**、**实验** 与 **对话/参数快照**；可选用内置 Agent（问股同款工具链）对候选做 **逐票评分** 或 **次日复盘**。
- **不构成投资建议**。输出仅供研究、记录与复盘；交易决策须由你自行判断并承担风险。

## 双层策略

战术台中的“策略版本”必须明确区分两层：

- **第一层：尾盘选股策略**。面向用户和同花顺动态选股，用于生成候选池。当前规则来自图片中的一句话选股：主板，涨幅 3%-7%，近 20 日有涨停，量比 > 1，换手率 5%-10%，总市值 50 亿-200 亿，股价运行于当日分时均价线上且强于大盘，个股创当日新高且回踩分时均线不破。
- **第二层：Agent 评估 / 预测策略**。面向模型，对第一层候选逐票评分、执行风险闸门、给出 `reject` / `watch` / `candidate` / `priority`、T+1 开盘预测和复盘建议。

复盘时不要把两层混在一起：如果候选进入合理但模型高估了开盘溢价，这是第二层预测权重问题；只有当候选池长期混入不该出现的票，才修改第一层同花顺筛选条件。

## 访问入口

- 浏览器打开 Web UI 后，侧栏 **「战术台」** 进入路由 `/tail-tactics`。
- 与问股、回测等共用同一 FastAPI 服务；需开启 Agent 相关 LLM 配置后，评分/复盘流式对话才会成功。

## 数据模型（SQLite）

| 实体 | 说明 |
|------|------|
| `tail_strategy_version` | 策略版本：`version_label`、`title`、Markdown 正文 `body_markdown`、可选 `parent_version_id` 便于追溯 |
| `tail_experiment` | 单次实验：`trade_date`（T 日）、关联策略版本、粘贴原文 `pasted_raw`、`symbols_json`（有序，≤10）、`param_snapshot_json`（风控模式、持有/验证周期、硬排除条件、评分权重、当日市场上下文、`data_cutoff_time` 等对话快照）、`ranking_session_id` / `ranking_output`（兼容旧字段名，当前保存评分输出）、`review_note_markdown`、`case_summary`、`status`（如 `draft` / `ranked` / `closed`）；历史库中可能仍存在 `tags_json` 列，当前产品不再读写 |
| `tail_morning_metric` | 次日早盘指标：按 `(experiment_id, symbol)` 唯一；`surge_pct_prev_close_930_1000` 为冲高幅度（%），`source` 区分 `akshare_5m_em_0930_1000`（东方财富 5 分钟 K 窗口）、`daily_high_t1_fallback:*`（分钟缺失时日线最高回退）、`manual` / `skipped_non_cn` 等 |
| `tail_candidate_fact_snapshot` | 候选池事实包快照：记录每次评分 compose 实际交给 Agent 的 `candidate_facts`、`kind`、`generated_at` 与数据新鲜度摘要；用于复盘“当时模型看到了什么证据”，而不是存储评分结论 |

## REST API 概要

前缀：`/api/v1/tail-tactics`

- `GET/POST /strategy-versions` — 列表与新建
- `GET /strategy-versions/{id}` — 详情
- `DELETE /strategy-versions/{id}` — 删除策略版本（若仍有实验 `strategy_version_id` 引用则 **409**；删除前会将其他版本的 `parent_version_id` 指向该 id 的记录置空）
- `GET /strategy-versions/{a}/diff/{b}` — 正文 unified diff（文本）
- `GET/POST /experiments` — 列表与创建
- `GET/PATCH /experiments/{id}` — 详情与部分更新
- `DELETE /experiments/{id}` — 删除实验（级联删除早盘指标；并清理 `tail_exp_{id}` 及该实验记录的 `ranking_session_id` 对应的问股会话消息）
- `GET /experiments/{id}/candidate-facts` — 返回 DB-first 候选池事实包：`selection_cutoff`、T 日日线、前一交易日日线、近期日线、衍生量价特征、已保存的 T+1 早盘指标、数据缺口与新鲜度说明；在 T 日尾盘评分窗口或 T+1 开盘前，会额外按 AkShare Eastmoney 历史分钟、AkShare 分钟缓存、efinance 历史分钟顺序尝试补充 1 分钟 K，并将成功字段或失败原因写入 `selection_cutoff_evidence`；分钟字段同时包含价格与量能，如 `last_volume`、`window_volume`、`last_5m_volume_share`、`last_5m_vs_previous_30m_volume_ratio`；该接口只提供证据层，不直接给出评分结论
- `GET /experiments/{id}/candidate-fact-snapshots` — 查看已持久化的候选池事实包快照（可选 `kind=rank` 与 `limit`）；评分 compose 会自动创建一条兼容旧枚举的 `rank` 快照，并在 `candidate_facts.snapshot_id` 返回其 id
- `GET /experiments/{id}/compose?kind=rank|review` — 组装调用 Agent 所需的 `session_id`、`message`、`context`；`kind=rank` 为兼容旧 API 名称，当前语义是逐票评分（前端再 POST 到 `/api/v1/agent/chat/stream`）
- `GET/PUT /experiments/{id}/morning-metrics` — 查询与批量 upsert 早盘指标
- `POST /experiments/{id}/morning-metrics/auto-fetch` — 按行情自动计算并写入早盘指标（可选 JSON：`{"morning_trade_date": "YYYY-MM-DD"}` 覆盖 T+1 日；默认取 T 之后首个 A 股交易日，见 `src/core/trading_calendar.next_cn_trading_day_after`）

## 与 Agent 的衔接

1. 调用 `GET .../compose?kind=rank`（或 `review`）拿到 `message` 与 `context`（内含 `tail_ranking_bundle` 或 `tail_review_bundle`，其中 `tail_ranking_bundle` 为兼容旧命名）。评分 compose 会自动附带 `candidate_facts`，并先持久化一条 `tail_candidate_fact_snapshot`；也可单独调用 `GET .../candidate-facts` 查看当前事实包。
2. 使用 **`POST /api/v1/agent/chat/stream`**，请求体携带上述字段，并增加 HTTP 头 **`X-DSA-Tail-Ranking: 1`**。
3. 在该头存在时，服务端 **固定使用单智能体 `AgentExecutor`**（忽略全局 `AGENT_ARCH=multi`），避免多智能体流水线按单票重复跑全栈。
4. `AgentExecutor.chat` 会将 bundle 格式化为前置说明，再进入工具循环；评分任务要求模型优先使用 `candidate_facts` 作为证据层，同时独立判断数据缺口、信号冲突与风险闸门，而不是把 DSA 缓存直接当最终结论。
5. Agent 输出和复盘建议必须说明其判断属于第一层候选池生成，还是第二层评分/预测；`watch` / `candidate` / `priority` 不应被反向写成同花顺筛选条件。
6. `/api/v1/agent/chat` 与 `/api/v1/agent/chat/stream` 会在回答成功后做后端归档：显式 `tail_ranking_bundle` / `tail_review_bundle` 会写回对应实验；普通问股聊天如果同时出现尾盘语义与 A 股代码，会自动创建或更新一条 `workflow_goal=conversation_auto_tail_archive` 的尾盘实验，保存候选池、`ranking_output` 与来源会话。
7. 最终回答优先展示面向用户阅读的中文报告，再在末尾附可解析的 `tail_score_result` JSON 结构（详见 compose 中的用户消息说明）。

### 报告阅读版

评分报告默认先输出中文阅读版，避免直接展示大段 JSON。建议结构：

- `结论卡`：代码、评分、动作级别、置信度、一句话判断。
- `关键证据`：涨幅、量能、分时承接、相对大盘/板块、催化与数据口径。
- `主要风险`：风险闸门、数据缺口、题材或盘口风险。
- `次日开盘预判`：方向、涨跌幅区间、价格区间、关键价位。
- `明日观察/失效条件`：开盘后需要验证和触发降级的条件。

JSON 仍需保留在报告末尾作为机器解析附录，供战术台保存、复盘统计和后续自动化使用。

### 评分 JSON 契约

`tail_score_result` 的每个元素至少包含：

- `code`：股票代码。
- `score`：0-100 数字，表示该票独立评分。
- `action_level`：`reject` / `watch` / `candidate` / `priority`；`priority` 仅代表研究优先级最高，不是交易建议。
- `confidence`：`low` / `medium` / `high`。
- `factor_scores`：对象，建议包含 `market`、`sector`、`price_volume`、`liquidity`、`catalyst`、`risk_gate`。
- `risk_flags`：风险点数组。
- `data_quality_flags`：数据缺口、口径冲突或低置信度原因数组。
- `next_day_watch`：次日观察点数组。
- `next_open_forecast`：T+1 开盘方向与区间预测对象，至少包含 `direction`、`expected_open_pct_range`、`expected_open_price_range`、`base_price`、`confidence`、`reasoning`、`key_levels`。
- `invalidation_conditions`：失效条件数组。
- `one_line_reason`：一句话理由。

可以额外输出 `passed_candidates`，但只有 `candidate` / `priority` 能进入；当市场环境不适合尾盘参与、候选池只有一只但质量不好，或 14:40 前数据不足以支持判断时，应将 `action_level` 降为 `reject` 或 `watch`，并允许 `passed_candidates` 为空。

### 数据截止与泄漏边界

- 默认评分截止时间为 T 日 `14:40`，也可通过 `param_snapshot.data_cutoff_time` 覆盖。
- T 日收盘价、全天最高价、收盘成交额、14:40 后分时新高/回落等都属于评分时不可见信息，不能参与 `score`、`action_level` 或 `passed_candidates` 判断。
- `next_open_forecast` 是独立预测补充：盘后生成报告时可使用 T 日收盘信息估算 T+1 开盘方向与价格区间，但必须说明依据，且不得倒灌影响 14:40 评分。
- 当前候选事实包会在活跃尾盘评分窗口尝试补充 14:40 前 1 分钟 K，取数成功时 `selection_cutoff_evidence.available=true`，并包含 `last_bar_time`、`last_close`、`pre_cutoff_high`、`distance_to_pre_cutoff_high_pct`、`last_volume`、`window_volume`、`last_5m_volume_share`、`last_5m_vs_previous_30m_volume_ratio` 等字段；取数失败时会保留 `fallback_attempts` 与 `missing_reason`。无论成功与否，Agent 都只能使用受 `selection_cutoff` 约束的分钟/快照数据，不能用全日 K 线替代。评分时分钟价格与分钟量能需要共同进入 `price_volume` / `liquidity` 判断，避免只看价格位置。

## 推荐工作流

1. 在 **策略与案例** Tab 写入首版方法论（新建策略版本），后续迭代用可选父版本，并在同页 **策略版本列表** 中用 **Diff** 对比文本变化。
2. 在 **实验与评分** Tab 选择交易日、策略版本，粘贴候选池，并填写风控模式、验证周期、硬排除条件与当日市场上下文；页面会把这些内容保存为 `param_snapshot`。
3. 选中实验后 **发起评分**：流式结束后服务端会把结果写回兼容旧字段 `ranking_output` 并将 `status` 置为 `ranked`；页面仍会刷新实验状态。
4. 在 **实验与评分** 中对选中实验点击 **自动拉取并保存**（或仍用手动填写 +「手动保存」）；再 **生成复盘**（需已有早盘指标行）。
5. 编辑并保存 **复盘笔记** 与 **案例摘要**；如果要改策略，先判断改的是第一层同花顺选股条件还是第二层 Agent 评估/预测权重；在 **策略与案例** Tab 的 **案例库** 表格中 **查看**（悬浮窗）或 **删除** 实验（需确认）。亦可在实验详情弹窗中跳转至「实验与评分」继续操作。

## 已知限制（P0）

- 自动拉取依赖 **外网** 与 **AkShare / efinance / 项目日线数据源**；代理异常、停牌、历史过久导致分钟接口无数据时，尾盘评分事实包会保留 `fallback_attempts`，早盘指标会回退 **日线最高价**（非严格 10:00 窗口），响应 `notes` 与每条 `source` 会标明。
- 案例在 **策略与案例** 页以表格浏览；全文检索与更复杂筛选可后续扩展。

## 相关代码路径

- 前端页面：[`apps/dsa-web/src/pages/TailTacticsPage.tsx`](../../apps/dsa-web/src/pages/TailTacticsPage.tsx)
- 前端通用弹层：[`apps/dsa-web/src/components/common/ModalDialog.tsx`](../../apps/dsa-web/src/components/common/ModalDialog.tsx)
- API：[`api/v1/endpoints/tail_tactics.py`](../../api/v1/endpoints/tail_tactics.py)
- 存储：[`src/storage.py`](../../src/storage.py) 中 `TailStrategyVersion` / `TailExperiment` / `TailMorningMetric`
- 候选事实包：[`src/services/tail_candidate_facts.py`](../../src/services/tail_candidate_facts.py)
- 尾盘分钟线补充：[`src/services/tail_intraday_fetch.py`](../../src/services/tail_intraday_fetch.py)
- Agent 组装：[`src/services/tail_tactics_compose.py`](../../src/services/tail_tactics_compose.py)
- 对话归档：[`src/services/tail_conversation_archive.py`](../../src/services/tail_conversation_archive.py)
- 早盘自动拉取：[`src/services/tail_morning_fetch.py`](../../src/services/tail_morning_fetch.py)
- 单智能体工厂：[`src/agent/factory.py`](../../src/agent/factory.py) 中 `build_tail_chat_executor`
