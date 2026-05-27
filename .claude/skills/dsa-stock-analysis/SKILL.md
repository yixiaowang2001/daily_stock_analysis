---
name: dsa-stock-analysis
description: "Use for DSA-backed stock or position analysis for one symbol, a holding, cost basis, or a small user-provided list of non-strategy symbols when the user wants per-symbol short/mid/long views, support/resistance/breakdown levels, buy/hold/reduce framing, refreshed facts, or time-aware recall of prior DSA/Agent analysis notes. Default to DSA data-only collection plus independent Agent judgment and persist concrete Agent notes when appropriate. Do not use for candidate-pool strategy experiments, multi-symbol ranking, tail-session scoring, T+1/open forecasts tied to a strategy, momentum selection, or saved strategy reviews; use dsa-candidate-lab for those, including single-symbol tail prompts."
---

# DSA Stock Analysis

## Overview

Use this skill to answer stock or position questions with DSA as the data collection core and the Agent as the judgment core. By default, collect DSA facts first, then reason independently. Always follow repository root `AGENTS.md`; treat outputs as research and risk framing, not investment instructions.

Keep this skill narrow. It is the per-symbol fact, judgment, and time-aware note engine. It may analyze a small list one stock at a time, but it is not the owner of candidate-pool strategies, ranking experiments, or strategy-version reviews.

## Repository And Discovery

This skill is available globally through `~/.codex/skills/dsa-stock-analysis`, which is expected to point to the repository skill folder. When the current working directory is not the DSA repository, run bundled scripts through the global path. The scripts locate the repository from `DSA_REPO_ROOT`, their own symlink target, or the current directory, then change into the repository before loading DSA config so relative `DATABASE_PATH` still writes to the DSA database.

If the repository cannot be found, ask the user for the DSA path before collecting or persisting data.

## Boundary With Candidate Lab

Use `dsa-stock-analysis` when the user asks:

- "这只股怎么看"
- "成本 31.038，有必要跑吗"
- "要不要买/卖/持有/减仓"
- "帮我分析 000021"
- "帮我分别看一下 000021、AAPL、HK00700"
- "这只股票的趋势、支撑、压力、风险是什么"
- "昨天你给这个票的跌破位是多少"

Use `dsa-candidate-lab` instead when the request includes:

- Candidate-pool ranking, scoring, strategy comparison, or "从这些里选一个/排序/打分".
- 尾盘选股, 尾盘实验, 尾盘战术台, T+1 forecast, next-day review, or 14:40/14:55 cutoff logic, even if only one symbol is present.
- Momentum selection, daily/weekly strategy evaluation, strategy versions, saved experiments, review metrics, or backtest-style learning.

If a candidate-lab workflow needs per-symbol facts, this skill's collector and evidence style may be reused, but candidate scoring, ranking, persistence, and final output contracts remain owned by `dsa-candidate-lab`.

Hard routing rule: a prompt like "尾盘选出来600783，评估一下" is not ordinary single-stock analysis. Hand it to `dsa-candidate-lab` as a one-candidate tail-session experiment so prediction, facts, morning metrics, and review are persisted.

## Core Posture

- Prefer facts first, judgment second. Do not invent prices, news, support levels, earnings, capital flow, or DSA report content.
- State the data cutoff, data source, and missing-data gaps before giving trade framing.
- When the user provides cost basis or position details, calculate unrealized P/L and anchor the answer around risk control, not only trend direction.
- Give conditional plans: keep/reduce/exit thresholds, invalidation levels, and what would change the view. Avoid absolute commands like "must buy" or "must sell".
- For live-market questions, refresh data through DSA first. If DSA cannot fetch current facts, use reputable external sources or browser/web fallback and clearly label them.
- Keep DSA conclusions advisory. If a DSA report exists, read it as one evidence source, then independently check whether its conclusion follows from the facts.

## Mode Selection

1. Data-only mode is the default. Use it for ordinary questions like "帮我分析", "要不要跑/减仓/持有", cost-basis questions, small non-ranking symbol lists, and any request where the user wants the Agent's judgment. Collect DSA facts, then reason independently.
2. Report-first mode: use when the user explicitly asks to "跑 DSA 报告", "先拿报告再判断", compare with DSA's recommendation, or preserve the full DSA report in history.
3. Hybrid mode: use when a fresh DSA report already exists. Load the latest report plus refreshed quote/K-line facts, then explain whether the report is still valid.
4. Recall mode: use when the user asks for a previously saved price view, support/resistance/breakdown level, or "你之前怎么看". Load latest reports/Agent notes first, state their `created_at`, `analysis_date`, and `data_cutoff`, then decide whether a refresh is needed.

## Data-Only Workflow

Run the bundled collector from anywhere:

```bash
python ~/.codex/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 --days 120 --save-db --include-news --include-latest-report
```

For multiple ordinary symbols, pass them together and analyze each independently:

```bash
python ~/.codex/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 AAPL HK00700 --days 120 --save-db --include-news --include-latest-report
```

Use fewer switches when speed matters:

```bash
python ~/.codex/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 --days 90 --save-db
```

The collector reuses DSA modules:

- `DataFetcherManager` for quote, K-line, chip distribution, basic fundamentals, and capital flow.
- `StockTrendAnalyzer` for MA/MACD/RSI/volume trend facts.
- `src.storage` for local daily bars and latest saved DSA reports.
- `SearchService` for optional news when search providers are configured.

If `IWENCAI_API_KEY` is configured, DSA appends Iwencai as the last realtime quote/search fallback. Treat any `quote.source == "iwencai"` or `news.provider == "Iwencai"` as quota-limited fallback evidence from 同花顺问财, not as the primary data source. Do not hide the fallback source in the answer; mention it in `未验证` or the data source/cutoff line when it appears. Iwencai does not replace daily K-line data, so a missing `daily` block remains a data gap.

After collection, analyze the returned JSON yourself. Use DSA's `trend.buy_signal`, `signal_score`, chip structure, valuation/earnings blocks, capital flow, and recent bars as evidence, not as final orders.

For recall-only questions, use a shorter collection that emphasizes saved notes:

```bash
python ~/.codex/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 --days 30 --no-save-db --include-latest-report --report-days 14 --report-limit 5
```

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

## Persistence Workflow

When a real analysis produces concrete short-term levels, support/resistance, breakdown or stop-loss levels, entry framing, or a position action plan, persist one `agent_note` record per symbol unless the user explicitly says not to save. Do not persist pure brainstorming, invalid symbols, or recall-only answers that do not refresh the view.

Save through the bundled script:

```bash
python ~/.codex/skills/dsa-stock-analysis/scripts/save_stock_analysis_note.py note.json
```

Use this JSON shape:

```json
{
  "code": "000021",
  "name": "深科技",
  "analysis_date": "2026-05-22",
  "data_cutoff": "2026-05-22T15:00:00+08:00",
  "valid_until": "next_trading_session_or_refresh",
  "current_price": 12.34,
  "change_pct": 1.2,
  "sentiment_score": 56,
  "trend_prediction": "震荡偏强",
  "operation_advice": "观望/回踩试仓",
  "time_horizon": {
    "short": "1-3 个交易日观点",
    "medium": "1-4 周观点",
    "long": "1 个季度以上观点"
  },
  "factors": {
    "technical": "技术面证据",
    "fundamental": "基本面证据",
    "sentiment": "情绪/消息面证据"
  },
  "levels": {
    "support": 12.1,
    "resistance": 13.2,
    "breakdown": 11.8,
    "ideal_buy": 12.0,
    "secondary_buy": 11.8,
    "stop_loss": 11.7,
    "take_profit": 13.5
  },
  "action_plan": {
    "no_position": "无仓观察/试仓条件",
    "has_position": "有仓持有/减仓/退出条件",
    "watch_points": ["放量站上压力", "跌破失效位"]
  },
  "analysis_summary": "供历史列表展示的一句话或短段落",
  "risk_warning": "最关键风险",
  "source_snapshot": {
    "collector_generated_at": "DSA facts generated_at",
    "daily_end_date": "latest K-line date",
    "quote_time": "quote timestamp if available"
  },
  "agent_note": "最终给用户的中文研究笔记正文"
}
```

After saving, mention the returned `record_id` or `query_id`. For future recall, prefer the latest same-code `agent_note` whose `analysis_date` and `data_cutoff` match the user's requested date. Treat short-term levels as stale after the next trading session unless refreshed; older notes can be used only as a reference to what the plan was at that time.

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
- `有效期`: analysis date, data cutoff, and whether prior saved notes are stale.
- `短线`: 1-3 trading day view with support, resistance, breakdown/stop-loss, and trigger conditions.
- `中线`: 1-4 week view with trend/sector/fundamental checks.
- `长线`: quarter-plus view focused on business quality, valuation, and structural risk.
- `三面验证`: technical, sentiment/news, and fundamental evidence.
- `操作框架`: no-position and has-position scenarios for watch/entry/hold/reduce/exit.
- `已保存`: record id/query id when persistence succeeds, or why it was not saved.
- `风险`: what can break the view.
- `未验证`: missing data or paths not run.

End with a short research-risk disclaimer. Do not ask the user to commit, push, tag, or publish anything while using this skill.

## Validation

For skill-only changes, run:

```bash
python scripts/check_ai_assets.py
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .claude/skills/dsa-stock-analysis
python -m py_compile .claude/skills/dsa-stock-analysis/scripts/collect_stock_context.py .claude/skills/dsa-stock-analysis/scripts/save_stock_analysis_note.py
python ~/.codex/skills/dsa-stock-analysis/scripts/collect_stock_context.py 000021 --days 30 --no-save-db
```
