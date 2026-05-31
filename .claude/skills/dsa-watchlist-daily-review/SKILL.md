---
name: dsa-watchlist-daily-review
description: "Use for DSA-backed after-close watchlist reviews and recurring automations when the user provides a watched stock list and wants daily minute-level/post-close evidence, historical DSA context, technical/news/fundamental analysis, short/mid/long rankings, entry/target/stop levels, and persisted Agent notes. Use for 15:00-16:30 Asia/Shanghai daily reviews of 10-20 symbols. Do not use for tail-session T+1 strategy experiments or candidate-pool learning; use dsa-candidate-lab or tail-picking-agent there."
---

# DSA Watchlist Daily Review

## Overview

Use this skill to run a repeatable after-close review for a user-provided watchlist. Follow repository root `AGENTS.md`. Treat DSA as the evidence collector and the Agent as the judgment/ranking layer; outputs are research and risk framing, not investment instructions.

This skill sits above `dsa-stock-analysis`: it may reuse that skill's collector and persistence script, but it owns the watchlist-level ranking tables and daily automation output.

## Automation Defaults

- Preferred schedule: trading days after close, usually `16:00` Asia/Shanghai for A-share-only lists; use `16:10` or later when Hong Kong symbols are included so post-close data has time to settle.
- If U.S. symbols are included, either mark them as stale during China afternoon runs or create a separate U.S. after-close automation.
- Keep the watchlist in the automation prompt or a user-maintained watchlist file. Do not hardcode stock lists, ports, model names, accounts, or local-only paths into code.
- If no watchlist is provided or discoverable, ask for the list instead of fabricating symbols.
- Default data cutoff for A-share intraday evidence is `15:00`; for analysis validity, state the generated time and the actual latest daily/minute timestamps.

Example automation prompt:

```text
Use $dsa-watchlist-daily-review for watchlist: 000021, 600519, hk00700, AAPL.
Run the after-close DSA review, rank short/mid/long opportunities separately, include entry/target/stop levels, save per-symbol Agent notes, and report missing data.
```

## Evidence Collection

From the DSA repository root, collect watchlist context:

```bash
python .claude/skills/dsa-watchlist-daily-review/scripts/collect_watchlist_context.py 000021 600519 --days 160 --save-db --include-news --include-latest-report
```

Useful options:

```bash
python .claude/skills/dsa-watchlist-daily-review/scripts/collect_watchlist_context.py --watchlist-file watchlist.txt --trade-date 2026-05-25 --cutoff-time 15:00 --days 160 --recent-bars 15 --save-db --include-news --include-latest-report
python .claude/skills/dsa-watchlist-daily-review/scripts/collect_watchlist_context.py 000021 600519 --no-intraday --no-save-db
```

The script wraps `.claude/skills/dsa-stock-analysis/scripts/collect_stock_context.py` for quote, daily K-line, trend, chip, fundamental, capital-flow, news, and latest saved reports. It then adds A-share 1-minute evidence capped at the cutoff using `src.services.tail_intraday_fetch.fetch_tail_intraday_cutoff_evidence`. Minute fetch is fail-open; missing minute data must be reported as a data gap, not hidden.

When `IWENCAI_API_KEY` is configured, the base collector can use 同花顺问财 as the final realtime quote/search fallback. Because all Iwencai skill calls share a small daily quota, treat `quote.source == "iwencai"` or `news.provider == "Iwencai"` as fallback evidence and surface it in the missing-data/source summary. Iwencai fallback does not replace missing daily K-line or minute evidence.

### Codex External Research Fallback

The base collector emits per-symbol `codex_research_fallback`. The watchlist collector also marks `minute_evidence_missing` when after-close minute evidence is unavailable. When those flags are present, Codex should use its own web/browser/finance research tools to supplement public facts needed for the review.

- News/search fallback covers Bocha, SearXNG, Tavily, Brave, SerpAPI, MiniMax, Anspire, and Iwencai failures or empty filtered results.
- Stock-data fallback may cover realtime/delayed quotes, recent daily or intraday price/volume context, company announcements, and public fundamentals.
- Fallback facts must be labeled as `Codex 外部兜底`, with source names/URLs where available, retrieval time, and the effective market cutoff.
- Do not let Codex fallback silently erase DSA data gaps: `post_close_intraday_evidence.available=false`, missing `daily`, or failed search providers still belong in the source/missing-data summary.
- For after-close reviews, keep the A-share `15:00` cutoff discipline. Later facts can be mentioned only as post-cutoff context, not as evidence that was observable at the cutoff.

For each symbol, inspect:

- `quote`, `daily.latest`, `daily.recent`, `trend`, `chip`, `fundamental_context`, `capital_flow_context`
- `news.results` when available
- `latest_reports` for stale or prior Agent/DSA views
- `post_close_intraday_evidence.fields` for A-share minute price/volume evidence such as last close, pre-cutoff high/low, last 5-minute return, and volume ratio
- `codex_research_fallback` to decide whether Codex should supplement missing news or stock facts before final ranking

## Judgment And Ranking

Analyze every symbol independently first, then rank across the watchlist. Do not let a weak list force a recommendation.

Use three horizons:

- Short: 1-3 trading days, focused on intraday close position, volume, recent support/resistance, catalyst freshness, and immediate invalidation.
- Medium: 1-4 weeks, focused on daily trend, moving-average structure, sector/market context, capital flow, and news/fundamental confirmation.
- Long: one quarter or longer, focused on business quality, earnings/valuation/fundamental context, structural sector position, and downside risk.

Score each horizon from `0` to `100` with explicit risk penalties. A practical default:

- Short score: technical/momentum 35, minute-volume confirmation 25, risk-reward to nearby levels 20, sentiment/catalyst 10, market/sector context 10.
- Medium score: daily trend 30, structure and support quality 20, capital/news confirmation 20, fundamentals 20, risk controls 10.
- Long score: fundamentals/valuation 40, structural trend 20, sector quality 20, balance-sheet/earnings/news risk 20.

For prices and levels:

- Give a buy/observe zone only when the support, trend, and risk-reward evidence justify it.
- Derive horizon-specific zones instead of reusing one price band for every table:
  - Short entry zone: nearby support, intraday close position, next-session volume confirmation, and immediate invalidation.
  - Medium build zone: daily MA10/MA20, recent platform support, sector confirmation, and 1-4 week invalidation.
  - Long build zone: valuation/fundamental margin of safety, quarterly trend support, and staged allocation levels; if long-term quality is high but price is extended, say `等待深回撤/无法给出当前建仓位`.
- Give target prices as conditional resistance/target zones, not guaranteed outcomes.
- Always include invalidation or stop-loss levels. If data is insufficient, say `无法可靠给出`.
- For high-volatility or limit-up/limit-down names, prefer zones over single-point precision.

## Output Contract

Answer in Chinese by default when the user writes Chinese. Put the watchlist summary first:

- `数据截点`: generated time, trade date, latest daily date, minute cutoff, and missing-data summary.
- `Codex兜底`: which symbols required/used external fallback and which gaps remain unresolved.
- `短线排序`: rank, code/name, score, action, entry zone, target zone, stop/invalidation, one-line reason.
- `中线排序`: same columns, using medium-horizon judgment; `entry_zone` must be the 1-4 week build/add zone, not a copied short-term buy zone.
- `长线排序`: same columns, using long-horizon judgment; `entry_zone` must be the quarterly staged build zone or `等待深回撤/无法可靠给出`.
- `中长线建仓计划`: for the medium/long priority and candidate names, list `code/name`, `medium_build_zone`, `long_build_zone`, `staging_plan`, `target_or_recheck_zone`, `medium_invalidation`, `long_invalidation`, and the evidence gap that could change the plan.
- `重点观察`: 3-5 cross-watchlist watch points for the next session.
- `逐票简表`: concise per-symbol technical/sentiment/fundamental evidence and horizon views.
- `已保存`: per-symbol `record_id` / `query_id`, or why a note was not saved.
- `未验证`: missing providers, stale U.S./HK data, failed minute fetches, or tests not run.
- `风险`: data freshness, market regime, liquidity, news, earnings, and model/analysis uncertainty.
- When the user asks for `详细情况`, or when this skill runs in an automation, expand the answer beyond the top summary: include interface health, all three ranking tables, the `中长线建仓计划`, 3-5 key watch points, concise per-symbol details, saved note ids, unverified items, and risks.

Each ranking row should include:

```text
rank | code | name | score | action(reject/watch/candidate/priority) | entry_zone | target_zone | stop_or_invalidation | reason
```

## Persistence

When a real daily review produces concrete short/mid/long views or price levels, save one `agent_note` per symbol unless the user explicitly asks not to persist:

```bash
python .claude/skills/dsa-stock-analysis/scripts/save_stock_analysis_note.py note.json
```

Use the `dsa-stock-analysis` note shape and include watchlist-specific fields inside `source_snapshot`, for example:

```json
{
  "code": "000021",
  "name": "深科技",
  "analysis_date": "2026-05-25",
  "data_cutoff": "2026-05-25T15:00:00+08:00",
  "valid_until": "next_trading_session_or_refresh",
  "current_price": 12.34,
  "change_pct": 1.2,
  "sentiment_score": 62,
  "trend_prediction": "震荡偏强",
  "operation_advice": "回踩观察/轻仓试探",
  "time_horizon": {
    "short": "1-3 个交易日观点",
    "medium": "1-4 周观点",
    "long": "季度以上观点"
  },
  "factors": {
    "technical": "技术面证据",
    "sentiment": "新闻/情绪证据",
    "fundamental": "基本面证据"
  },
  "levels": {
    "ideal_buy": 12.0,
    "secondary_buy": 11.7,
    "support": 11.8,
    "resistance": 12.9,
    "breakdown": 11.5,
    "stop_loss": 11.5,
    "take_profit": 13.2
  },
  "action_plan": {
    "no_position": "无仓等待回踩或放量突破",
    "has_position": "有仓按跌破位控制风险",
    "watch_points": ["次日量能", "是否站稳压力位"]
  },
  "source_snapshot": {
    "source": "dsa-watchlist-daily-review",
    "short_rank": 3,
    "medium_rank": 5,
    "long_rank": 8,
    "collector_generated_at": "2026-05-25T08:20:00Z",
    "minute_source": "akshare_hist_min_em_1m",
    "codex_research_fallback": "needed/used/external sources if any"
  },
  "analysis_summary": "一句话摘要",
  "risk_warning": "最关键风险",
  "agent_note": "最终中文研究笔记正文"
}
```

If only a high-level watchlist scan was produced with no concrete levels, skip persistence and say why.

## Validation

For skill-only changes, run:

```bash
python scripts/check_ai_assets.py
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .claude/skills/dsa-watchlist-daily-review
python -m py_compile .claude/skills/dsa-watchlist-daily-review/scripts/collect_watchlist_context.py
python .claude/skills/dsa-watchlist-daily-review/scripts/collect_watchlist_context.py --help
```
