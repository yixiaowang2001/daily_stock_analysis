# Repository Claude Skills

本目录存放仓库级协作 skills，属于版本库资产。

- 规则真源：仓库根目录 `AGENTS.md`
- 兼容入口：根目录 `CLAUDE.md`（应为指向 `AGENTS.md` 的软链接）
- 本目录中的 skill 需要与 `AGENTS.md` 保持一致
- `.claude/reviews/` 属于本地分析产物，不作为规则真源
- 股票相关 skill 的接入关系见 `docs/agent-skill-integration.md`

## 当前仓库级 skills

| Skill | 用途 |
| --- | --- |
| `analyze-issue` | 分析 GitHub Issue，生成仓库内评估产物 |
| `analyze-pr` | 审查 PR 必要性、验证证据、实现风险与合入判断 |
| `fix-issue` | 按 issue 修复流程读取上下文、实施改动并验证 |
| `dsa-stock-analysis` | 通过 DSA 收集股票事实包，逐票输出短中长线、关键点位与持仓/买卖/风险判断，并可保存时点研究笔记 |
| `dsa-watchlist-daily-review` | 收盘后对用户关注列表拉取 DSA 事实与分钟证据，按短线/中线/长线分别排序并输出建仓、目标、止损区间 |
| `dsa-codex-information-fallback` | 在 DSA 资讯/搜索 provider 缺口下，用 Codex 外部公开来源补齐公告、新闻、事件与情绪证据，并强制标注截点与来源 |
| `tail-picking-agent` | 尾盘选股兼容入口，实际映射到 `dsa-candidate-lab` 的尾盘 profile |

## 统一信息兜底口径

股票相关 skill 默认 DSA provider 优先。若 Bocha、SearXNG、Tavily、Brave、SerpAPI、MiniMax、Anspire、Iwencai 等新闻搜索接口失败或过滤后无结果，Codex 可以用自身联网/浏览器信息获取能力补齐公开新闻、公告、行业与公司信息。若行情、K 线、分钟证据或公开基本面缺失，也可以用 Codex 外部来源做补充核验；系统化资讯兜底按 `dsa-codex-information-fallback` 的来源优先级与输出契约执行。

所有外部补充都必须标注为 `Codex 外部兜底`，说明来源、获取时间、数据截点和仍未验证的缺口；不能把外部兜底事实伪装成 DSA provider 正常返回，也不能默认写入 DSA 数据库。

## 相关但不是 skill 的自动化

「操盘」三操盘手纸面交易实验由 Codex Desktop recurring automation 驱动，不放在 `.claude/skills/` 下。仓库内代码入口是 `scripts/run_agent_backtest_cycle.py`，自动化安装入口是 `scripts/install_agent_backtest_codex_automations.py`，Web 独立页面是 `/agent-backtest`。收盘净值任务必须检查持仓 `valuation_date` / `valuation_source` / `valuation_stale`，避免当日日线缺失时把前一交易日收盘价当作今日净值。详细边界见 `docs/agent-skill-integration.md` 与 `docs/agent-backtest-workbench.md`。

尾盘战术台 11:30 T+1 复盘也由 Codex Desktop recurring automation 驱动，不放在 `.claude/skills/` 下。仓库内代码入口是 `scripts/run_tail_tactics_codex_review.py`，自动化安装入口是 `scripts/install_tail_tactics_codex_automation.py`。第一层同花顺筛选调整只记录为待用户确认建议；第二层 Agent 评分/预测校准沉淀到 `.claude/reviews/tail_tactics/layer2_calibration.md` 并注入后续评分。

如果未来需要兼容其他 agent 目录（如 `.agents/skills/` 或 `.github/skills/`），应先明确单一真源，再通过脚本或镜像同步，而不是手工长期维护多份同义内容。
