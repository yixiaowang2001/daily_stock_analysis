# DSA Agent Skill 接入说明

本文说明 daily_stock_analysis（DSA）仓库与股票相关 Agent Skill 的关系，帮助在不改变业务功能的前提下，把“这个 repo 该接哪个 skill”说清楚。

## 结论

这几个股票相关 skill 都和本仓库有关，但分工不同：

| Skill | 所属层级 | 适用场景 | 与本仓库的关系 |
| --- | --- | --- | --- |
| `dsa-stock-analysis` | 仓库级 skill | 单只股票、单个持仓、成本价下的持有/减仓/止损判断 | 复用 DSA 的行情、K 线、筹码、资金流、基本面、历史报告与搜索模块，输出事实包后由 Agent 独立判断 |
| `dsa-candidate-lab` | 用户级 / 全局 skill | 多候选池评分、策略实验、版本对比、复盘学习；包含尾盘、未来日/周动量等 profile | 以本仓库的尾盘战术台 API、存储、Web 页面和候选事实包作为当前主要落地面 |
| `tail-picking-agent` | 仓库级兼容 skill | 用户明确提到尾盘选股智能体、尾盘实验、14:40 评分、T+1 复盘 | 兼容入口；新逻辑应映射到 `dsa-candidate-lab` 的 `tail-session-t1` profile |
| `daily-stock-analysis` / openclaw Skill | 外部集成 skill | openclaw 或其他外部 Agent 通过 HTTP 调用 DSA REST API | 依赖已运行的 DSA API 服务，不是仓库协作规则真源 |
| 根目录 `SKILL.md` | 产品 / 外部集成说明 | 通过 Python 入口理解 DSA 的股票分析能力 | 不是仓库 AI 协作治理真源；治理规则看 `AGENTS.md` |

仓库内 AI 协作规则的唯一真源仍是 `AGENTS.md`。仓库级 skill 真源放在 `.claude/skills/`。

## 如何选择

用户只问一只股票或一个持仓时，用 `dsa-stock-analysis`。

典型请求：

```text
用 DSA 看一下 000021
成本 31.038，这只要不要跑？
帮我分析 AAPL 的支撑、压力和风险
```

用户给出多个候选、要求排名、实验、复盘、策略版本或学习闭环时，用 `dsa-candidate-lab`。

典型请求：

```text
今天尾盘候选 000001、000021、600519，帮我打分
按 14:40 截点保存尾盘实验并预测 T+1 开盘
复盘昨天的候选池，看看策略要不要调权重
```

用户显式说 `tail-picking-agent` 或“尾盘选股智能体”时，走 `tail-picking-agent` 兼容入口，但实际工作流按 `dsa-candidate-lab` 的 `tail-session-t1` profile 执行。

外部 Agent 只想通过部署好的 DSA 服务触发分析时，用 openclaw / HTTP Skill，参考 `docs/openclaw-skill-integration.md`。

## 接入方式

### 1. 仓库级 skill

仓库级 skill 已放在：

```text
.claude/skills/dsa-stock-analysis/
.claude/skills/tail-picking-agent/
```

如果当前 Agent 运行环境不会自动发现仓库级 skill，可以把它们软链接到用户级 skill 目录：

```bash
mkdir -p ~/.codex/skills
ln -s "$PWD/.claude/skills/dsa-stock-analysis" ~/.codex/skills/dsa-stock-analysis
ln -s "$PWD/.claude/skills/tail-picking-agent" ~/.codex/skills/tail-picking-agent
```

如果同名目录已存在，先确认它是否是旧版本，避免覆盖用户级 skill。

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

## 关键边界

- `dsa-stock-analysis` 不负责候选池排名、策略版本、尾盘实验或复盘持久化。
- `dsa-candidate-lab` 可以复用单票事实包，但候选池评分、排名、输出契约与复盘学习由它负责。
- `tail-picking-agent` 只保留兼容入口；新增跨策略逻辑不要继续塞进这个 alias。
- `.agents/skills/` 如需存在，应视为 `.claude/skills/` 的本地镜像或适配目录，不作为手工维护的第二真源。
- 所有 skill 输出都属于研究和风险框架，不是投资指令或自动交易建议。

## 验证

修改仓库级 skill 或 AI 协作资产后，执行：

```bash
python scripts/check_ai_assets.py
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .claude/skills/dsa-stock-analysis
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .claude/skills/tail-picking-agent
```

如修改 `dsa-stock-analysis` 的采集脚本，再补充：

```bash
python .claude/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 --days 30 --no-save-db
```

如修改尾盘战术台后端或前端，按 `AGENTS.md` 的改动面验证矩阵执行对应测试与构建。
