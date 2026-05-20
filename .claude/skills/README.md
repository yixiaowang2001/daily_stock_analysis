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
| `dsa-stock-analysis` | 通过 DSA 收集单只股票事实包，再做持仓/买卖/风险判断 |
| `tail-picking-agent` | 尾盘选股兼容入口，实际映射到 `dsa-candidate-lab` 的尾盘 profile |

如果未来需要兼容其他 agent 目录（如 `.agents/skills/` 或 `.github/skills/`），应先明确单一真源，再通过脚本或镜像同步，而不是手工长期维护多份同义内容。
