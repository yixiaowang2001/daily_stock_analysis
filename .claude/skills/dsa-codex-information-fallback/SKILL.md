---
name: dsa-codex-information-fallback
description: Use when DSA stock workflows, Agent backtest, candidate-lab, watchlist review, or single-stock analysis have skipped, failed, stale, empty, or insufficient information_context/news/search evidence and Codex must collect public information as an external fallback under a data_cutoff_at. Collect SEC/exchange filings, company investor-relations/newsroom items, reputable timestamped media, and optional sentiment evidence; label facts as Codex 外部兜底 and keep them separate from DSA provider facts.
---

# DSA Codex Information Fallback

## Overview

Use this skill to turn Codex web/browser research into a controlled supplemental evidence layer for DSA. It is for public information gaps only: disclosures, company events, news, sector context, and sentiment signals.

Follow repository `AGENTS.md`. Treat all outputs as research evidence and risk framing, not investment instructions or automated trading authorization.

## Required Inputs

Before researching, identify these fields from the DSA context or the user:

- `symbol`, `stock_name`, and `market`.
- `data_cutoff_at` or the task's effective decision timestamp.
- The gap reason, such as `information_context_skipped`, provider timeout, empty results, stale result, or high-impact decision requiring confirmation.
- Workflow type: Agent backtest, candidate-lab, watchlist review, or single-stock analysis.
- Whether output is a historical masked replay; if yes, avoid exact later prices and separate post-cutoff observations.

## Source Priority

Use DSA provider facts first. Codex research begins only when provider facts are unavailable, insufficient, or need high-impact confirmation.

1. Official/regulatory sources:
   SEC EDGAR submissions and XBRL APIs for US issuers; exchange announcements, halt/status pages, and official issuer filings for the relevant market.
2. Company-controlled primary sources:
   investor-relations pages, earnings releases, event transcripts/webcasts, newsroom press releases, and official blogs.
3. Timestamped reputable financial media:
   Reuters/AP/CNBC/WSJ/Bloomberg/MarketWatch/Nasdaq/Yahoo Finance-style headline pages when the source page includes a usable timestamp. Prefer direct article/source pages over search snippets.
4. DSA search providers as discovery:
   The repository `SearchService` is provider-order dependent: Anspire is inserted at the front when configured, then Bocha, Tavily, Brave, SerpAPI, MiniMax, SearXNG, and Iwencai is appended as the tail fallback. Treat these results as discovery unless the returned source page is itself reliable and timestamped.
5. Social/sentiment sources:
   Reddit, X, Stocktwits, forums, and analyst chatter are low-priority sentiment flags only. Do not use them as factual confirmation of filings, earnings, orders, guidance, or legal/regulatory events.

## Cutoff Rules

- Include only information that was public at or before `data_cutoff_at`.
- Convert timestamps to the market timezone or UTC before comparing.
- If a source has only a date and that date equals the cutoff date, mark it `ambiguous_timestamp` unless the page proves it was available before the cutoff.
- For historical replay, post-cutoff sources may be used only in a separate after-the-fact review section. They must not affect the simulated decision.
- Do not use later prices, next-day realized moves, or post-cutoff news to explain a pre-cutoff buy/sell decision.

## Workflow

1. Read the DSA context first.
   Inspect `information_context`, `codex_research_fallback`, `sentiment_context`, `data_cutoff_at`, and any `data_quality_flags`. If provider evidence is already adequate, do not duplicate research.

2. Build targeted queries.
   Use repository-provided `codex_research_fallback.queries` when present. For US symbols, add official-source queries such as `<ticker> SEC 8-K 10-Q`, `<company> investor relations earnings release`, and `<ticker> guidance press release`. For A-share/HK symbols, prefer exchange/company announcement and official media queries before generic news queries.

3. Collect a small evidence set.
   For one symbol, usually collect 3-5 usable sources. For 10+ symbols, triage first: held symbols, large intraday movers, symbols with provider gaps, and decisions that would otherwise become buy/sell. Record unresolved symbols as gaps instead of doing broad unfocused searches.

4. Normalize evidence.
   Capture `title`, `source_name`, `source_type`, `url`, `published_at`, `retrieved_at`, `cutoff_status`, `summary`, `decision_relevance`, `reliability`, and `remaining_gap`.

5. Separate facts from interpretation.
   Facts are what the source says. Interpretation is how it affects catalyst, risk, sentiment, or confidence. Keep both short, cited, and scoped to the cutoff.

6. Write the fallback result.
   In Agent backtest, include it in the decision rationale, `risk_notes`, final summary, or local review artifact. Do not write it into `quote`, `daily`, `information_context`, or provider-normalized DB rows unless an existing DSA workflow explicitly supports a labeled evidence snapshot.

## Output Contract

Use this shape when structured output is useful:

```json
{
  "status": "collected",
  "source": "Codex 外部兜底",
  "market": "us",
  "symbol": "AAPL",
  "data_cutoff_at": "2026-05-29T09:40:00-04:00",
  "retrieved_at": "2026-05-31T13:20:00+08:00",
  "provider_gap_reason": "information_context_skipped",
  "evidence": [
    {
      "source_type": "official_filing",
      "title": "Current report filing",
      "source_name": "SEC EDGAR",
      "url": "https://data.sec.gov/submissions/CIK0000320193.json",
      "published_at": "2026-05-29",
      "cutoff_status": "after_cutoff",
      "summary": "Filing exists but was not available before the simulated cutoff.",
      "decision_relevance": "ignored_for_pre_cutoff_decision",
      "reliability": "high",
      "remaining_gap": null
    }
  ],
  "decision_relevance": {
    "catalyst": [],
    "risk": [],
    "sentiment": [],
    "ignored_post_cutoff": ["SEC filing after simulated cutoff"]
  },
  "data_quality_flags": ["Codex 外部兜底", "post_cutoff_source_excluded"]
}
```

`status` should be one of `collected`, `partial`, `no_usable_sources`, or `unavailable`.

## Agent Backtest Notes

- For masked replay, summarize price-sensitive context as percent moves, volume/range buckets, or qualitative states. Do not reveal exact future prices.
- If a same-day official filing appears after a morning/midday cutoff, record it as `ignored_post_cutoff`; it can explain later review but not the original decision.
- Treat DSA search-provider snippets as leads. Confirm important claims on primary or reputable timestamped pages before using them to change an order decision.
- If Codex browsing/search is unavailable, say so and leave the gap in `data_quality_flags`; do not invent news, sentiment, guidance, or regulatory facts.

## Quick Interpretation Guide

- SEC/company filings: high reliability for disclosure existence and filed financial facts; weaker for immediate market interpretation.
- Company IR/newsroom: high reliability for company statements, earnings releases, product/events, and guidance; still issuer-biased.
- Reputable media: useful for market interpretation, analyst framing, legal/regulatory context, and cross-company sector impact; verify timestamps.
- Search APIs: useful for recall and discovery; not sufficient by themselves when a trade decision depends on the claim.
- Social sentiment: useful only as a low-confidence sentiment or crowd-attention signal.
