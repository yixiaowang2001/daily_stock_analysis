---
name: tail-picking-agent
description: "Legacy compatibility alias for explicit DSA tail-session candidate-pool work: 尾盘选股智能体, 尾盘实验, 尾盘战术台, 14:40 scoring, T+1 open forecasts, and next-day reviews. Prefer dsa-candidate-lab for new strategy-lab work; this skill maps tail requests to its tail-session-t1 profile."
---

# Tail Picking Agent

This skill is now a compatibility entry for DSA tail-session work. New candidate-pool strategy logic belongs in `dsa-candidate-lab`; when this skill triggers, handle the request as `dsa-candidate-lab` profile `tail-session-t1`.

If available, load the global candidate-lab skill:

```text
~/.codex/skills/dsa-candidate-lab/SKILL.md
```

Then follow its repository, persistence, scoring, and review workflow.

## Tail Profile Mapping

- `strategy_profile`: `tail-session-t1`
- Output root: `tail_score_result`
- Default cutoff: `14:40` Asia/Shanghai
- Default timing windows: `14:30 初筛`, `14:40 评分截点`, `14:55 收盘前风控复核`
- Default holding/review horizon: T+1 open / next-morning review
- Candidate limit: prefer 1-10 symbols

Layer 1 is the user's Tonghuashun tail-session screening strategy. Current canonical sentence:

```text
主板，涨幅 3%-7%，近 20 日有涨停，量比 > 1，换手率 5%-10%，总市值 50 亿-200 亿，股价运行于当日分时均价线上且强于大盘，个股创当日新高且回踩分时均线不破.
```

Layer 2 is Agent evaluation: risk gate, score, `action_level`, T+1 open forecast, review, and strategy-learning notes. Do not turn Layer 2 labels such as `watch`, `candidate`, or `priority` into Tonghuashun screening conditions.

## Repository

Default DSA repository path is the current repository root containing `AGENTS.md` and `main.py`. If the current working directory is not the DSA repository, use `DSA_REPO_ROOT` when provided or ask the user for the path before persisting data.

When working inside the DSA repository, follow root `AGENTS.md`.

## Tail Workflow

1. Use the user's stated trade date, otherwise current Asia/Shanghai date.
2. Require at least one real candidate symbol.
3. Capture `param_snapshot` with profile, cutoff, timing windows, risk mode, Layer 1 rules, Layer 2 weights, hard exclusions, and user market context.
4. Prefer the tail tactics REST API if FastAPI is running:
   - `/api/v1/tail-tactics/strategy-versions`
   - `/api/v1/tail-tactics/experiments`
   - `/api/v1/tail-tactics/experiments/{id}/compose?kind=rank|review`
   - `/api/v1/tail-tactics/morning-metrics`
5. If no server is running and persistence is needed, use existing DSA Python storage/API objects rather than inventing a parallel format.
6. Score only from data observable at or before `data_cutoff_time`. Full-day T bars are reference-only and must be labeled if used.
7. Put a readable Chinese report first, then append machine-readable `tail_score_result` JSON.
8. For reviews, fetch or accept morning metrics first, then save review notes and case summary when persistence is supported.

## Output Contract

Each `tail_score_result` item should include:

- `code`
- `score`
- `action_level`: `reject`, `watch`, `candidate`, or `priority`
- `confidence`: `low`, `medium`, or `high`
- `factor_scores`
- `risk_flags`
- `data_quality_flags`
- `next_day_watch`
- `next_open_forecast`
- `invalidation_conditions`
- `one_line_reason`

## Validation

For docs/skill-only changes, run:

```bash
python scripts/check_ai_assets.py
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .claude/skills/tail-picking-agent
```

For code changes touching tail tactics backend/frontend, follow `AGENTS.md` and prefer:

```bash
python -m pytest tests/test_tail_tactics_api.py -q
cd apps/dsa-web && npm run lint && npm run build
```
