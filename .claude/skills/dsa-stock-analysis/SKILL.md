---
name: dsa-stock-analysis
description: "Use for DSA-backed single-stock or position analysis: one symbol, cost basis, holding size, buy/sell/hold/reduce framing, or refreshed facts for an individual security. Default to DSA data-only collection plus independent Agent judgment. Do not use for candidate-pool strategy experiments, multi-symbol ranking, tail-session scoring, momentum selection, or saved strategy reviews; use dsa-candidate-lab for those."
---

# DSA Stock Analysis

## Overview

Use this skill to answer single-stock or position questions with DSA as the data collection core and the Agent as the judgment core. By default, collect DSA facts first, then reason independently. Always follow repository root `AGENTS.md`; treat outputs as research and risk framing, not investment instructions.

Keep this skill narrow. It is the fact and judgment engine for one security at a time, not the owner of candidate-pool strategies, ranking experiments, or strategy-version reviews.

## Boundary With Candidate Lab

Use `dsa-stock-analysis` when the user asks:

- "这只股怎么看"
- "成本 31.038，有必要跑吗"
- "要不要买/卖/持有/减仓"
- "帮我分析 000021"
- "这只股票的趋势、支撑、压力、风险是什么"

Use `dsa-candidate-lab` instead when the request includes:

- Multiple candidate symbols, candidate-pool ranking, or strategy comparison.
- 尾盘选股, 尾盘实验, 尾盘战术台, T+1 forecast, next-day review, or 14:40/14:55 cutoff logic.
- Momentum selection, daily/weekly strategy evaluation, strategy versions, saved experiments, review metrics, or backtest-style learning.

If a candidate-lab workflow needs per-symbol facts, this skill's collector and evidence style may be reused, but candidate scoring, ranking, persistence, and final output contracts remain owned by `dsa-candidate-lab`.

## Core Posture

- Prefer facts first, judgment second. Do not invent prices, news, support levels, earnings, capital flow, or DSA report content.
- State the data cutoff, data source, and missing-data gaps before giving trade framing.
- When the user provides cost basis or position details, calculate unrealized P/L and anchor the answer around risk control, not only trend direction.
- Give conditional plans: keep/reduce/exit thresholds, invalidation levels, and what would change the view. Avoid absolute commands like "must buy" or "must sell".
- For live-market questions, refresh data through DSA first. If DSA cannot fetch current facts, use reputable external sources or browser/web fallback and clearly label them.
- Keep DSA conclusions advisory. If a DSA report exists, read it as one evidence source, then independently check whether its conclusion follows from the facts.

## Mode Selection

1. Data-only mode is the default. Use it for ordinary single-stock questions like "帮我分析", "要不要跑/减仓/持有", cost-basis questions, and any request where the user wants the Agent's judgment. Collect DSA facts, then reason independently.
2. Report-first mode: use when the user explicitly asks to "跑 DSA 报告", "先拿报告再判断", compare with DSA's recommendation, or preserve the full DSA report in history.
3. Hybrid mode: use when a fresh DSA report already exists. Load the latest report plus refreshed quote/K-line facts, then explain whether the report is still valid.

## Data-Only Workflow

Run the bundled collector from the repository root:

```bash
python .claude/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 --days 120 --save-db --include-news --include-latest-report
```

Use fewer switches when speed matters:

```bash
python .claude/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 --days 90 --save-db
```

The collector reuses DSA modules:

- `DataFetcherManager` for quote, K-line, chip distribution, basic fundamentals, and capital flow.
- `StockTrendAnalyzer` for MA/MACD/RSI/volume trend facts.
- `src.storage` for local daily bars and latest saved DSA reports.
- `SearchService` for optional news when search providers are configured.

After collection, analyze the returned JSON yourself. Use DSA's `trend.buy_signal`, `signal_score`, chip structure, valuation/earnings blocks, capital flow, and recent bars as evidence, not as final orders.

## Report-First Workflow

When the user wants a DSA report first, run the app's normal pipeline with notifications disabled:

```bash
python main.py --stocks 000021 --no-market-review --no-notify --force-run
```

Then collect the latest context with `--include-latest-report`. If the report is stale versus refreshed quote/K-line data, say so and prioritize newer facts.

For data refresh without DSA LLM analysis:

```bash
python main.py --stocks 000021 --dry-run --no-market-review --no-notify --force-run
```

## Cost-Basis Answer Shape

For questions like "成本 31.038，有必要跑吗", include:

- Current price and unrealized P/L versus cost.
- Whether the question is about stop-loss, stop-profit, or trend invalidation.
- Key levels from recent lows/highs, moving averages, and DSA trend/chip facts.
- A conservative action framework, for example: "先锁一部分利润", "跌破 X 减仓", "跌破 Y 退出短线逻辑", "站回 Z 才恢复强势".
- Main risks: data freshness, high turnover/volume anomaly, valuation pressure, earnings/cash-flow weakness, news catalyst uncertainty, or broad-market/sector drag.

## Output Discipline

Answer in Chinese by default when the user writes Chinese. Keep the structure compact:

- `结论`: direct but conditional.
- `关键数据`: price, P/L, trend, volume, support/resistance, DSA source freshness.
- `操作框架`: scenario levels for hold/reduce/exit.
- `风险`: what can break the view.
- `未验证`: missing data or paths not run.

End with a short research-risk disclaimer. Do not ask the user to commit, push, tag, or publish anything while using this skill.

## Validation

For skill-only changes, run:

```bash
python scripts/check_ai_assets.py
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .claude/skills/dsa-stock-analysis
python .claude/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 --days 30 --no-save-db
```
