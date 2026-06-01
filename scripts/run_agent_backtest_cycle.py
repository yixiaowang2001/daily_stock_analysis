#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex runner for multi-agent paper-trading backtests.

This script deliberately does not call the repo's built-in Agent pipeline. It
creates bounded decision contexts for Codex to read, then provides a structured
write-back path for Codex-produced decisions.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_provider.base import DataFetcherManager, canonical_stock_code  # noqa: E402
from src.config import setup_env  # noqa: E402
from src.repositories.stock_repo import StockRepository  # noqa: E402
from src.services.agent_backtest_service import AgentBacktestError, AgentBacktestService  # noqa: E402
from src.services.portfolio_service import PortfolioService  # noqa: E402


PHASE_DEFAULT_TIMES = {
    "verify": "09:40:00",
    "morning": "09:40:00",
    "late_morning": "10:30:00",
    "pre_noon": "11:20:00",
    "midday": "13:35:00",
    "tail": "14:40:00",
    "close": "16:05:00",
}
MARKET_PHASE_DEFAULT_TIMES = {
    "cn": PHASE_DEFAULT_TIMES,
    "us": {
        "verify": "09:40:00",
        "morning": "09:40:00",
        "late_morning": "10:30:00",
        "pre_noon": "12:00:00",
        "midday": "14:30:00",
        "tail": "20:30:00",
        "close": "16:20:00",
    },
}
ACTIONABLE_ACTIONS = {"buy", "sell"}
REALTIME_QUOTE_TIMEOUT_SECONDS = 8
RESEARCH_HISTORY_DAYS = 260
RECENT_DAILY_BAR_DAYS = 20
DEFAULT_US_INTRADAY_DATA_SOURCE_PRIORITY = "massive,twelvedata,ibkr"
US_INTRADAY_SOURCE_LABELS = {
    "massive": "massive_intraday_1m",
    "polygon": "massive_intraday_1m",
    "polygonio": "massive_intraday_1m",
    "twelvedata": "twelvedata_intraday_1m",
    "twelve_data": "twelvedata_intraday_1m",
    "twelve": "twelvedata_intraday_1m",
    "ibkr": "ibkr_intraday_1m",
    "interactive_brokers": "ibkr_intraday_1m",
    "interactivebrokers": "ibkr_intraday_1m",
}
FACT_LAYER_NAMES = [
    "market_data",
    "technical_context",
    "fundamental_context",
    "information_context",
    "sentiment_context",
    "long_horizon_context",
]
FUNDAMENTAL_FALLBACK_BLOCKS = (
    "valuation",
    "growth",
    "earnings",
    "institution",
    "capital_flow",
    "dragon_tiger",
    "boards",
)
UNAVAILABLE_FACT_STATUSES = {"failed", "skipped", "no_data", "not_supported", "partial"}
LONG_HORIZON_RISK_KEYWORDS = (
    "异常波动",
    "澄清",
    "风险",
    "减持",
    "处罚",
    "诉讼",
    "立案",
    "问询",
    "监管",
    "退市",
    "跌停",
    "大宗交易",
)


def parse_symbols(value: str) -> List[str]:
    items = [item.strip() for item in (value or "").replace("，", ",").split(",")]
    symbols: List[str] = []
    for item in items:
        if not item:
            continue
        code = canonical_stock_code(item)
        if code not in symbols:
            symbols.append(code)
    return symbols


def parse_date(value: Optional[str], *, default: Optional[date] = None) -> Optional[date]:
    if not value:
        return default
    return date.fromisoformat(value)


def parse_dt_for_trade_date(trade_date: date, intraday_time: str) -> datetime:
    text = (intraday_time or "").strip()
    if "T" in text or " " in text:
        return datetime.fromisoformat(text)
    parts = text.split(":")
    if len(parts) < 2:
        raise ValueError("time must be HH:MM or HH:MM:SS")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) > 2 else 0
    return datetime(trade_date.year, trade_date.month, trade_date.day, hour, minute, second)


def default_phase_time(phase: str, market: str) -> str:
    market_key = (market or "cn").strip().lower()
    return MARKET_PHASE_DEFAULT_TIMES.get(market_key, PHASE_DEFAULT_TIMES).get(
        phase,
        PHASE_DEFAULT_TIMES[phase],
    )


def safe_json_loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


class RunnerContextBuilder:
    """Build per-profile Codex decision contexts with profile isolation."""

    def __init__(
        self,
        *,
        service: AgentBacktestService,
        portfolio_service: PortfolioService,
        stock_repo: StockRepository,
    ) -> None:
        self.service = service
        self.portfolio_service = portfolio_service
        self.stock_repo = stock_repo
        self._data_fetcher: Optional[DataFetcherManager] = None

    def build_cycle(
        self,
        *,
        run_id: int,
        phase: str,
        trade_date: date,
        observation_time: str,
        context_dir: Path,
        live_data: bool = False,
        record_observation: bool = True,
    ) -> Dict[str, Any]:
        run = self.service.get_run(run_id)
        context_root = context_dir / f"run_{run_id}" / trade_date.isoformat() / phase
        context_root.mkdir(parents=True, exist_ok=True)

        close_prices: Dict[str, float] = {}
        close_price_sources: Dict[str, str] = {}
        close_price_gaps: Dict[str, Any] = {}
        if phase == "close" and live_data:
            held_symbols = self._held_symbols_for_close(run=run, trade_date=trade_date)
            close_prices, close_price_sources, close_price_gaps = self._collect_realtime_price_overrides(
                held_symbols
            )

        if phase == "close":
            nav = self.service.record_daily_nav(
                run_id=run_id,
                trade_date=trade_date,
                price_overrides=close_prices,
                price_override_sources=close_price_sources,
            )
        else:
            nav = None
        close_nav_by_profile_id = {
            int(item["profile_id"]): item
            for item in (nav or {}).get("items", [])
            if item.get("profile_id") is not None
        }

        profiles = run.get("profiles") or []
        profile_snapshots: Dict[str, Dict[str, Any]] = {}
        profile_held_symbols: Dict[str, List[str]] = {}
        all_held_symbols: List[str] = []
        snapshot_price_overrides = close_prices if phase == "close" else None
        snapshot_price_override_sources = close_price_sources if phase == "close" else None
        for profile in profiles:
            profile_key = profile["profile_key"]
            snapshot = self.portfolio_service.get_portfolio_snapshot(
                account_id=int(profile["account_id"]),
                as_of=trade_date,
                cost_method="fifo",
                price_overrides=snapshot_price_overrides,
                price_override_sources=snapshot_price_override_sources,
            )
            profile_snapshots[profile_key] = snapshot
            account = (snapshot.get("accounts") or [{}])[0]
            held_symbols = self._held_symbols_from_account(account)
            profile_held_symbols[profile_key] = held_symbols
            all_held_symbols = self._merge_symbols(all_held_symbols, held_symbols)

        watchlist_symbols = run.get("symbols") or []
        research_symbols = self._merge_symbols(watchlist_symbols, all_held_symbols)
        cutoff = parse_dt_for_trade_date(trade_date, observation_time)
        shared_symbol_scope = {
            "watchlist_symbols": watchlist_symbols,
            "portfolio_held_symbols": all_held_symbols,
            "research_symbols": research_symbols,
            "buy_allowed_symbols": watchlist_symbols,
            "shared_fact_policy": (
                "All active profiles receive the same symbol_facts for every research symbol. "
                "Profile style decides which layers to emphasize; data collection does not privilege short, "
                "medium, or long horizons."
            ),
            "profile_isolation_note": (
                "portfolio_held_symbols is a de-identified symbol universe derived from experiment portfolios; "
                "owner profile, position size, and other profiles' decisions remain hidden."
            ),
        }
        shared_symbol_facts = {
            symbol: self._symbol_facts(
                symbol=symbol,
                live_data=live_data,
                run=run,
                trade_date=trade_date,
                data_cutoff_at=cutoff,
            )
            for symbol in research_symbols
        }

        generated: List[Dict[str, Any]] = []
        public_run = {key: value for key, value in run.items() if key != "profiles"}
        for profile in profiles:
            profile_key = profile["profile_key"]
            evidence = self._collect_evidence(
                run=run,
                profile=profile,
                portfolio_snapshot=profile_snapshots.get(profile_key, {}),
                own_held_symbols=profile_held_symbols.get(profile_key, []),
                shared_symbol_scope=shared_symbol_scope,
                shared_symbol_facts=shared_symbol_facts,
                close_daily_nav=close_nav_by_profile_id.get(int(profile["id"])),
                close_price_overrides=close_prices,
                close_price_gaps=close_price_gaps,
                trade_date=trade_date,
                phase=phase,
                data_cutoff_at=cutoff,
                live_data=live_data,
            )
            observation = None
            if record_observation and phase != "close":
                observed_symbols = (evidence.get("symbol_scope") or {}).get("research_symbols") or run.get("symbols") or []
                observation = self.service.record_observation(
                    run_id=run_id,
                    profile_key=profile_key,
                    trade_date=trade_date,
                    observation_time=observation_time,
                    data_cutoff_at=cutoff,
                    symbols=observed_symbols,
                    evidence=evidence,
                    summary=f"{phase} cycle context generated for Codex decision.",
                )

            context_payload = {
                "run": public_run,
                "profile": profile,
                "phase": phase,
                "trade_date": trade_date.isoformat(),
                "observation_time": observation_time,
                "data_cutoff_at": cutoff.isoformat(),
                "observation": observation,
                "evidence": evidence,
                "output_contract": self._output_contract(phase, run),
            }
            stem = f"{profile_key}_context"
            json_path = context_root / f"{stem}.json"
            md_path = context_root / f"{stem}.md"
            json_path.write_text(
                json.dumps(context_payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            md_path.write_text(self._render_markdown_context(context_payload), encoding="utf-8")
            generated.append(
                {
                    "profile_key": profile_key,
                    "observation_id": observation.get("id") if observation else None,
                    "context_json": str(json_path),
                    "context_markdown": str(md_path),
                }
            )

        return {
            "run_id": run_id,
            "phase": phase,
            "trade_date": trade_date.isoformat(),
            "observation_time": observation_time,
            "context_dir": str(context_root),
            "live_data": live_data,
            "record_observation": record_observation,
            "generated": generated,
            "daily_nav": nav,
            "close_price_overrides": close_prices,
            "close_price_gaps": close_price_gaps,
            "next_step": (
                "Codex should read each context_markdown independently and call apply-decision for each profile."
                if phase != "close"
                else "Close phase recorded daily NAV; review daily_nav for report."
            ),
        }

    def _collect_evidence(
        self,
        *,
        run: Dict[str, Any],
        profile: Dict[str, Any],
        portfolio_snapshot: Dict[str, Any],
        own_held_symbols: List[str],
        shared_symbol_scope: Dict[str, Any],
        shared_symbol_facts: Dict[str, Any],
        close_daily_nav: Optional[Dict[str, Any]],
        close_price_overrides: Dict[str, float],
        close_price_gaps: Dict[str, Any],
        trade_date: date,
        phase: str,
        data_cutoff_at: datetime,
        live_data: bool,
    ) -> Dict[str, Any]:
        own_events = self.service.list_events(
            run_id=int(run["id"]),
            profile_key=profile["profile_key"],
            limit=20,
        )
        own_events = self._compact_recent_events(own_events)
        watchlist_symbols = shared_symbol_scope.get("watchlist_symbols") or []
        research_symbols = shared_symbol_scope.get("research_symbols") or []
        exit_only_symbols = [symbol for symbol in own_held_symbols if symbol not in watchlist_symbols]
        research_only_symbols = [
            symbol
            for symbol in research_symbols
            if symbol not in watchlist_symbols and symbol not in own_held_symbols
        ]
        symbol_scope = {
            **shared_symbol_scope,
            # Backward-compatible alias: this profile's own holdings.
            "held_symbols": own_held_symbols,
            "own_held_symbols": own_held_symbols,
            "exit_only_symbols": exit_only_symbols,
            "research_only_symbols": research_only_symbols,
            "note": (
                "Research covers watchlist symbols plus all symbols held anywhere in the experiment, with "
                "identical symbol_facts for every profile. This profile may buy/add only buy_allowed_symbols. "
                "Its own exit_only_symbols may be held, reduced, or sold; research_only_symbols are visible "
                "for market awareness but are not buy candidates unless they return to the watchlist."
            ),
        }
        payload = {
            "source": "codex_runner",
            "phase": phase,
            "data_cutoff_at": data_cutoff_at.isoformat(),
            "symbol_scope": symbol_scope,
            "visibility": {
                "profile_scope": profile["profile_key"],
                "other_profiles_hidden": True,
                "decision_source": "codex_scheduled",
            },
            "portfolio_snapshot": portfolio_snapshot,
            "own_recent_events": own_events,
            "symbol_facts": shared_symbol_facts,
            "profile_decision_guidance": self._profile_decision_guidance(
                profile=profile,
                run=run,
            ),
            "codex_research_policy": self._codex_research_policy(
                live_data=live_data,
                data_cutoff_at=data_cutoff_at,
            ),
            "data_quality": {
                "live_data_enabled": live_data,
                "requested_fact_layers": FACT_LAYER_NAMES,
                "note": (
                    "Symbol fact collection is profile-neutral and fail-open. External realtime, fundamental, "
                    "information, and sentiment layers may be skipped or failed explicitly; all profiles still "
                    "receive the same layer keys for every research symbol."
                ),
            },
        }
        if run.get("market") == "us":
            try:
                settled_cash = self.service._available_us_settled_cash(  # noqa: SLF001 - runner owns this experiment contract
                    account_id=int(profile["account_id"]),
                    as_of=trade_date,
                )
            except Exception as exc:
                settled_cash = None
                payload.setdefault("data_quality", {}).setdefault("warnings", []).append(
                    f"settled_cash unavailable: {exc}"
                )
            payload["us_cash_account_state"] = {
                "settled_cash": settled_cash,
                "base_currency": (run.get("config") or {}).get("base_currency") or "USD",
                "sell_proceeds_settlement": "T+1 US business day",
                "day_trade_limit_per_rolling_window": (run.get("config") or {}).get(
                    "day_trade_limit_per_rolling_window",
                    1,
                ),
                "day_trade_window_business_days": (run.get("config") or {}).get(
                    "day_trade_window_business_days",
                    5,
                ),
                "extended_hours_enabled": bool((run.get("config") or {}).get("extended_hours_enabled", True)),
                "overnight_trading_enabled": bool((run.get("config") or {}).get("overnight_trading_enabled", True)),
            }
        if phase == "close":
            payload["close_daily_nav"] = close_daily_nav
            payload["close_price_overrides"] = close_price_overrides
            payload["close_price_gaps"] = close_price_gaps
            payload["close_valuation_note"] = (
                "The close portfolio_snapshot and close_daily_nav use the same price overrides. "
                "If close_price_gaps is non-empty or close_daily_nav.valuation_stale is true, treat NAV and PnL "
                "ranking as tentative."
            )
        return payload

    @staticmethod
    def _compact_recent_events(events: Dict[str, Any]) -> Dict[str, Any]:
        """Keep history useful without embedding prior full evidence snapshots."""
        if not isinstance(events, dict):
            return {}
        return {
            "observations": [
                RunnerContextBuilder._compact_observation_event(item)
                for item in (events.get("observations") or [])
            ],
            "decisions": [
                RunnerContextBuilder._compact_decision_event(item)
                for item in (events.get("decisions") or [])
            ],
            "orders": events.get("orders") or [],
            "fills": events.get("fills") or [],
            "daily_nav": [
                RunnerContextBuilder._compact_nav_event(item)
                for item in (events.get("daily_nav") or [])
            ],
            "history_payload_note": (
                "Prior observation evidence is summarized here to avoid recursively embedding full "
                "historical context snapshots."
            ),
        }

    @staticmethod
    def _compact_observation_event(item: Dict[str, Any]) -> Dict[str, Any]:
        evidence = item.get("evidence") if isinstance(item, dict) else {}
        scope = evidence.get("symbol_scope") if isinstance(evidence, dict) else {}
        if not isinstance(scope, dict):
            scope = {}
        facts = evidence.get("symbol_facts") if isinstance(evidence, dict) else {}
        if not isinstance(facts, dict):
            facts = {}
        return {
            "id": item.get("id"),
            "trade_date": item.get("trade_date"),
            "observation_time": item.get("observation_time"),
            "data_cutoff_at": item.get("data_cutoff_at"),
            "sequence_no": item.get("sequence_no"),
            "symbols": item.get("symbols") or [],
            "summary": item.get("summary"),
            "evidence_summary": {
                "phase": evidence.get("phase") if isinstance(evidence, dict) else None,
                "own_held_symbols": scope.get("own_held_symbols") or scope.get("held_symbols") or [],
                "buy_allowed_symbols": scope.get("buy_allowed_symbols") or [],
                "exit_only_symbols": scope.get("exit_only_symbols") or [],
                "research_symbols_count": len(scope.get("research_symbols") or []),
                "symbol_facts_count": len(facts or {}),
            },
            "created_at": item.get("created_at"),
        }

    @staticmethod
    def _compact_decision_event(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": item.get("id"),
            "observation_id": item.get("observation_id"),
            "trade_date": item.get("trade_date"),
            "decision_time": item.get("decision_time"),
            "action": item.get("action"),
            "symbol": item.get("symbol"),
            "side": item.get("side"),
            "quantity": item.get("quantity"),
            "order_type": item.get("order_type"),
            "limit_price": item.get("limit_price"),
            "confidence": item.get("confidence"),
            "rationale": item.get("rationale"),
            "risk_notes": item.get("risk_notes"),
            "policy_version_label": item.get("policy_version_label"),
            "created_at": item.get("created_at"),
        }

    @staticmethod
    def _compact_nav_event(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": item.get("id"),
            "trade_date": item.get("trade_date"),
            "cash": item.get("cash"),
            "market_value": item.get("market_value"),
            "total_equity": item.get("total_equity"),
            "realized_pnl": item.get("realized_pnl"),
            "unrealized_pnl": item.get("unrealized_pnl"),
            "valuation_stale": item.get("valuation_stale"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }

    @staticmethod
    def _profile_decision_guidance(
        *,
        profile: Dict[str, Any],
        run: Dict[str, Any],
    ) -> Dict[str, Any]:
        style = str(profile.get("style_profile") or profile.get("profile_key") or "").strip().lower()
        market = str((run or {}).get("market") or "cn").strip().lower()
        guidance: Dict[str, Any] = {
            "same_stock_pool": True,
            "profile_style": style,
            "symbol_fact_policy": (
                "All profiles receive the same symbol_facts for the same research_symbols. "
                "Use the profile policy to decide which evidence layers matter most."
            ),
            "no_trade_required": True,
        }
        if style == "long":
            guidance.update(
                {
                    "primary_layers": [
                        "long_horizon_context",
                        "fundamental_context",
                        "technical_context",
                        "information_context",
                    ],
                    "same_pool_note": (
                        "The long profile does not get a separate stock pool. It should evaluate the shared "
                        "watchlist through long-horizon gates instead of importing other candidates."
                    ),
                    "pilot_entry_rule": (
                        "Observe/hold is valid. For A-share runs, a 100-share pilot buy is allowed only when "
                        "the symbol is in evidence.symbol_scope.buy_allowed_symbols, "
                        "long_horizon_context.pilot_entry_gate.status is candidate, cash/lot checks pass, "
                        "and no material pre-cutoff information risk is unresolved."
                        if market == "cn"
                        else "Observe/hold is valid. A small whole-share pilot buy is allowed only when "
                        "the symbol is in evidence.symbol_scope.buy_allowed_symbols, "
                        "long_horizon_context.pilot_entry_gate.status is candidate, settled-cash checks pass, "
                        "and no material pre-cutoff information risk is unresolved."
                    ),
                    "data_gap_handling": (
                        "Do not require every provider fundamental block to be perfect before considering a "
                        "pilot entry; require at least basic valuation/market-cap evidence, record missing "
                        "earnings/growth/institution/capital-flow blocks in risk_notes, and use Codex external "
                        "fallback only under codex_research_policy."
                    ),
                    "position_sizing_note": (
                        "Treat intraday tail-session buys as pilot entries only. Full long allocation should "
                        "wait for after-close or weekly confirmation unless policy is explicitly evolved."
                    ),
                }
            )
        return guidance

    def _symbol_facts(
        self,
        *,
        symbol: str,
        live_data: bool,
        run: Optional[Dict[str, Any]] = None,
        trade_date: Optional[date] = None,
        data_cutoff_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        latest_rows = self.stock_repo.get_latest(symbol, days=RESEARCH_HISTORY_DAYS)
        daily = [
            {
                "date": row.date.isoformat() if row.date else None,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "amount": row.amount,
                "pct_chg": row.pct_chg,
            }
            for row in reversed(latest_rows)
        ]
        daily_source = "stock_daily_cache"
        daily_fetch_fallback: Optional[Dict[str, Any]] = None
        latest_daily_date = daily[-1].get("date") if daily else None
        daily_stale_for_trade_date = self._daily_stale_for_trade_date(
            latest_daily_date=latest_daily_date,
            trade_date=trade_date,
        )
        if live_data and (len(daily) < RECENT_DAILY_BAR_DAYS or daily_stale_for_trade_date):
            fetched_daily, daily_fetch_fallback = self._fetch_daily_rows(
                symbol=symbol,
                days=RESEARCH_HISTORY_DAYS,
                target_date=trade_date,
            )
            if fetched_daily:
                daily = fetched_daily
                daily_source = str(daily_fetch_fallback.get("source") or "market_data_fallback")
                latest_daily_date = daily[-1].get("date") if daily else None
                daily_stale_for_trade_date = self._daily_stale_for_trade_date(
                    latest_daily_date=latest_daily_date,
                    trade_date=trade_date,
                )
        intraday_cutoff = self._fetch_intraday_cutoff(
            symbol=symbol,
            live_data=live_data,
            run=run,
            trade_date=trade_date,
            data_cutoff_at=data_cutoff_at,
        )
        point_in_time_quote = self._quote_from_intraday_cutoff(
            intraday_cutoff=intraday_cutoff,
            daily=daily,
            trade_date=trade_date,
        )
        realtime: Dict[str, Any]
        if point_in_time_quote is not None:
            realtime = point_in_time_quote
        elif live_data:
            realtime = self._fetch_realtime_quote(
                symbol,
                skip_ibkr=self._should_skip_ibkr_realtime(intraday_cutoff),
            )
        else:
            realtime = {"skipped": True, "reason": "live_data_disabled"}
        official_latest_daily_date = latest_daily_date
        official_daily_stale_for_trade_date = daily_stale_for_trade_date
        provisional_daily_bar = None
        intraday_daily_bar = self._intraday_daily_bar(
            intraday_cutoff=intraday_cutoff,
            trade_date=trade_date,
        )
        if (
            live_data
            and trade_date is not None
            and intraday_daily_bar is not None
            and (
                daily_stale_for_trade_date
                or self._should_use_intraday_trade_date_bar(run=run, data_cutoff_at=data_cutoff_at)
            )
        ):
            trade_date_text = trade_date.isoformat()
            daily = [row for row in daily if row.get("date") != trade_date_text]
            daily.append(intraday_daily_bar)
            latest_daily_date = trade_date_text
            daily_stale_for_trade_date = False
            provisional_daily_bar = intraday_daily_bar
        elif live_data and daily_stale_for_trade_date and trade_date is not None:
            provisional_daily_bar = self._realtime_daily_bar(realtime=realtime, trade_date=trade_date)
            if provisional_daily_bar is not None:
                trade_date_text = trade_date.isoformat()
                daily = [row for row in daily if row.get("date") != trade_date_text]
                daily.append(provisional_daily_bar)
                latest_daily_date = trade_date_text
                daily_stale_for_trade_date = False
        stock_name = self._resolve_stock_name(symbol, realtime)
        recent_daily = daily[-RECENT_DAILY_BAR_DAYS:]
        market_data = {
            "daily_bars_window": f"latest_{RECENT_DAILY_BAR_DAYS}_of_{RESEARCH_HISTORY_DAYS}_requested",
            "daily_bars_available": len(daily),
            "daily_bars_latest_date": latest_daily_date,
            "daily_bars_stale_for_trade_date": daily_stale_for_trade_date,
            "daily_bars_official_latest_date": official_latest_daily_date,
            "daily_bars_official_stale_for_trade_date": official_daily_stale_for_trade_date,
            "daily_bars_trade_date_provisional": provisional_daily_bar is not None,
            "daily_bars_source": daily_source,
            "intraday_cutoff": intraday_cutoff,
            "realtime_quote": realtime,
            "realtime_quote_semantics": (
                "point_in_time_intraday_cutoff"
                if point_in_time_quote is not None
                else "live_snapshot_or_fallback"
            ),
        }
        if provisional_daily_bar is not None:
            market_data["daily_bars_trade_date_provisional_source"] = (
                str(provisional_daily_bar.get("source") or f"realtime_quote:{realtime.get('source') or 'unknown'}")
            )
        if daily_fetch_fallback is not None:
            market_data["daily_fetch_fallback"] = daily_fetch_fallback
        technical_source = daily_source
        if provisional_daily_bar is not None and provisional_daily_bar.get("partial_intraday"):
            technical_source = f"{daily_source}+{provisional_daily_bar.get('source') or 'intraday_1m_cutoff'}"
        technical_context = self._build_technical_context(daily=daily, realtime=realtime, source=technical_source)
        fundamental_context = self._fetch_fundamental_context(symbol=symbol, live_data=live_data)
        fundamental_context = self._merge_realtime_valuation_into_fundamental_context(
            fundamental_context=fundamental_context,
            realtime=realtime,
        )
        information_context = self._fetch_information_context(
            symbol=symbol,
            stock_name=stock_name,
            live_data=live_data,
            run=run,
        )
        codex_research_fallback = self._build_codex_research_fallback(
            symbol=symbol,
            stock_name=stock_name,
            information_context=information_context,
            fundamental_context=fundamental_context,
            live_data=live_data,
        )
        sentiment_context = self._build_sentiment_context(
            realtime=realtime,
            information_context=information_context,
            live_data=live_data,
        )
        long_horizon_context = self._build_long_horizon_context(
            symbol=symbol,
            stock_name=stock_name,
            daily=daily,
            market_data=market_data,
            technical_context=technical_context,
            fundamental_context=fundamental_context,
            information_context=information_context,
            sentiment_context=sentiment_context,
        )
        return {
            "schema_version": "agent_backtest_symbol_facts_v3",
            "symbol": symbol,
            "name": stock_name,
            "data_layers": FACT_LAYER_NAMES,
            "market_data": market_data,
            # Compatibility fields for existing contexts and ad-hoc scripts.
            "daily_bars": recent_daily,
            "realtime_quote": realtime,
            "technical_context": technical_context,
            "fundamental_context": fundamental_context,
            "information_context": information_context,
            "codex_research_fallback": codex_research_fallback,
            "sentiment_context": sentiment_context,
            "long_horizon_context": long_horizon_context,
            "data_quality": self._fact_data_quality(
                {
                    "market_data": market_data,
                    "technical_context": technical_context,
                    "fundamental_context": fundamental_context,
                    "information_context": information_context,
                    "sentiment_context": sentiment_context,
                    "long_horizon_context": long_horizon_context,
                }
            ),
        }

    @staticmethod
    def _daily_stale_for_trade_date(*, latest_daily_date: Any, trade_date: Optional[date]) -> bool:
        if trade_date is None or not latest_daily_date:
            return False
        try:
            return date.fromisoformat(str(latest_daily_date)) < trade_date
        except ValueError:
            return True

    @staticmethod
    def _realtime_daily_bar(*, realtime: Dict[str, Any], trade_date: date) -> Optional[Dict[str, Any]]:
        price = RunnerContextBuilder._quote_price(realtime)
        if price is None:
            return None
        open_price = (
            RunnerContextBuilder._safe_float(realtime.get("open_price"))
            or RunnerContextBuilder._safe_float(realtime.get("open"))
            or price
        )
        high = RunnerContextBuilder._safe_float(realtime.get("high")) or max(open_price, price)
        low = RunnerContextBuilder._safe_float(realtime.get("low")) or min(open_price, price)
        volume = RunnerContextBuilder._safe_float(realtime.get("volume"))
        amount = volume * price if volume is not None else None
        return {
            "date": trade_date.isoformat(),
            "open": open_price,
            "high": high,
            "low": low,
            "close": price,
            "volume": volume,
            "amount": amount,
            "pct_chg": RunnerContextBuilder._safe_float(realtime.get("change_pct")),
            "provisional": True,
            "source": f"realtime_quote:{realtime.get('source') or 'unknown'}",
        }

    @staticmethod
    def _should_use_intraday_trade_date_bar(
        *,
        run: Optional[Dict[str, Any]],
        data_cutoff_at: Optional[datetime],
    ) -> bool:
        if (run or {}).get("market") != "us" or data_cutoff_at is None:
            return False
        return data_cutoff_at.time() < datetime.strptime("16:00:00", "%H:%M:%S").time()

    def _fetch_intraday_cutoff(
        self,
        *,
        symbol: str,
        live_data: bool,
        run: Optional[Dict[str, Any]],
        trade_date: Optional[date],
        data_cutoff_at: Optional[datetime],
    ) -> Dict[str, Any]:
        if not live_data:
            return {"status": "skipped", "reason": "live_data_disabled"}
        if (run or {}).get("market") != "us":
            return {"status": "skipped", "reason": "non_us_market"}
        if trade_date is None or data_cutoff_at is None:
            return {"status": "skipped", "reason": "cutoff_missing"}
        errors: List[Dict[str, str]] = []
        for source_key, source_label, fetcher in self._us_intraday_fetchers():
            try:
                if (
                    source_key != "ibkr"
                    and hasattr(fetcher, "_is_available")
                    and not fetcher._is_available()
                ):
                    errors.append({"source": source_label, "error": "provider_not_configured_or_unavailable"})
                    continue
                bars = fetcher.get_intraday_bars_until(symbol, data_cutoff_at)
                payload = self._build_intraday_cutoff_payload(
                    symbol=symbol,
                    bars=bars,
                    trade_date=trade_date,
                    data_cutoff_at=data_cutoff_at,
                    source=source_label,
                )
                if payload.get("status") == "ok":
                    return payload
                errors.append({"source": source_label, "error": str(payload.get("missing_reason") or "no_data")})
            except Exception as exc:
                errors.append({"source": source_label, "error": str(exc)[:300]})

        error_text = "; ".join(
            f"{item.get('source')}: {item.get('error')}"
            for item in errors[:4]
        )
        return {
            "status": "failed",
            "source": "us_intraday_1m_fallback",
            "attempted_sources": [item.get("source") for item in errors],
            "errors": errors,
            "error": error_text[:300] if error_text else "no intraday provider attempted",
            "cutoff_at": data_cutoff_at.isoformat() if data_cutoff_at else None,
        }

    @staticmethod
    def _us_intraday_source_keys() -> List[str]:
        raw = os.getenv("US_INTRADAY_DATA_SOURCE_PRIORITY") or DEFAULT_US_INTRADAY_DATA_SOURCE_PRIORITY
        keys: List[str] = []
        seen = set()
        for token in raw.split(","):
            key = token.strip().lower().replace("-", "_").replace(" ", "_")
            if not key or key not in US_INTRADAY_SOURCE_LABELS:
                continue
            canonical = "massive" if key in {"polygon", "polygonio"} else key
            canonical = "twelvedata" if canonical in {"twelve_data", "twelve"} else canonical
            canonical = "ibkr" if canonical in {"interactive_brokers", "interactivebrokers"} else canonical
            if canonical in seen:
                continue
            keys.append(canonical)
            seen.add(canonical)
        return keys or ["massive", "twelvedata", "ibkr"]

    @staticmethod
    def _us_intraday_fetchers() -> List[tuple[str, str, Any]]:
        from data_provider.us_market_fetchers import IbkrFetcher, MassiveFetcher, TwelveDataFetcher

        fetcher_by_key = {
            "massive": MassiveFetcher,
            "twelvedata": TwelveDataFetcher,
            "ibkr": IbkrFetcher,
        }
        fetchers = []
        for source_key in RunnerContextBuilder._us_intraday_source_keys():
            fetcher_cls = fetcher_by_key.get(source_key)
            if fetcher_cls is None:
                continue
            fetchers.append((source_key, US_INTRADAY_SOURCE_LABELS[source_key], fetcher_cls()))
        return fetchers

    @staticmethod
    def _build_intraday_cutoff_payload(
        *,
        symbol: str,
        bars: List[Dict[str, Any]],
        trade_date: date,
        data_cutoff_at: datetime,
        source: str = "ibkr_intraday_1m",
    ) -> Dict[str, Any]:
        cutoff_text = data_cutoff_at.isoformat(sep=" ")
        scoped = [
            bar
            for bar in bars
            if str(bar.get("date") or "") == trade_date.isoformat()
            and str(bar.get("timestamp") or "") <= cutoff_text
        ]
        if not scoped:
            return {
                "status": "no_data",
                "source": source,
                "symbol": symbol,
                "trade_date": trade_date.isoformat(),
                "cutoff_at": data_cutoff_at.isoformat(),
                "missing_reason": "no_minute_rows_at_or_before_cutoff",
            }
        scoped.sort(key=lambda item: str(item.get("timestamp") or ""))
        last = scoped[-1]
        last_bar_lag_minutes = RunnerContextBuilder._intraday_bar_lag_minutes(
            last_bar_at=last.get("timestamp"),
            cutoff_at=data_cutoff_at,
        )
        if last_bar_lag_minutes is not None and last_bar_lag_minutes > 90:
            return {
                "status": "no_data",
                "source": source,
                "symbol": symbol,
                "trade_date": trade_date.isoformat(),
                "cutoff_at": data_cutoff_at.isoformat(),
                "last_bar_at": last.get("timestamp"),
                "last_bar_lag_minutes": last_bar_lag_minutes,
                "missing_reason": "last_minute_bar_too_stale_for_cutoff",
            }
        highs = [RunnerContextBuilder._safe_float(row.get("high")) for row in scoped]
        highs = [value for value in highs if value is not None]
        lows = [RunnerContextBuilder._safe_float(row.get("low")) for row in scoped]
        lows = [value for value in lows if value is not None]
        volumes = [RunnerContextBuilder._safe_float(row.get("volume")) for row in scoped]
        volumes = [value for value in volumes if value is not None]
        amounts = [RunnerContextBuilder._safe_float(row.get("amount")) for row in scoped]
        amounts = [value for value in amounts if value is not None]
        recent_5m_change_pct = None
        if len(scoped) >= 6:
            anchor = RunnerContextBuilder._safe_float(scoped[-6].get("close"))
            current = RunnerContextBuilder._safe_float(last.get("close"))
            if anchor and current:
                recent_5m_change_pct = round((current - anchor) / anchor * 100, 2)
        return {
            "status": "ok",
            "source": source,
            "symbol": symbol,
            "trade_date": trade_date.isoformat(),
            "cutoff_at": data_cutoff_at.isoformat(),
            "source_scope": (
                "1-minute historical bars filtered to trade_date and capped at data_cutoff_at; "
                "post-cutoff bars are excluded."
            ),
            "bar_count": len(scoped),
            "first_bar_at": scoped[0].get("timestamp"),
            "last_bar_at": last.get("timestamp"),
            "last_bar_lag_minutes": last_bar_lag_minutes,
            "window_open": RunnerContextBuilder._safe_float(scoped[0].get("open")),
            "window_high": max(highs) if highs else None,
            "window_low": min(lows) if lows else None,
            "window_close": RunnerContextBuilder._safe_float(last.get("close")),
            "window_volume": sum(volumes) if volumes else None,
            "window_amount": sum(amounts) if amounts else None,
            "recent_5m_change_pct": recent_5m_change_pct,
            "last_bar": last,
            "recent_bars": scoped[-5:],
        }

    @staticmethod
    def _intraday_bar_lag_minutes(*, last_bar_at: Any, cutoff_at: datetime) -> Optional[float]:
        try:
            last_dt = datetime.fromisoformat(str(last_bar_at))
        except (TypeError, ValueError):
            return None
        return max(0.0, round((cutoff_at - last_dt).total_seconds() / 60, 2))

    @staticmethod
    def _previous_close_for_trade_date(
        *,
        daily: List[Dict[str, Any]],
        trade_date: Optional[date],
    ) -> Optional[float]:
        if trade_date is None:
            return None
        trade_date_text = trade_date.isoformat()
        previous_rows = [row for row in daily if str(row.get("date") or "") < trade_date_text]
        if not previous_rows:
            return None
        return RunnerContextBuilder._safe_float(previous_rows[-1].get("close"))

    @staticmethod
    def _quote_from_intraday_cutoff(
        *,
        intraday_cutoff: Dict[str, Any],
        daily: List[Dict[str, Any]],
        trade_date: Optional[date],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(intraday_cutoff, dict) or intraday_cutoff.get("status") != "ok":
            return None
        price = RunnerContextBuilder._safe_float(intraday_cutoff.get("window_close"))
        if price is None or price <= 0:
            return None
        prev_close = RunnerContextBuilder._previous_close_for_trade_date(
            daily=daily,
            trade_date=trade_date,
        )
        change_amount = None
        change_pct = None
        if prev_close and prev_close > 0:
            change_amount = round(price - prev_close, 4)
            change_pct = round((price - prev_close) / prev_close * 100, 2)
        return {
            "code": intraday_cutoff.get("symbol"),
            "source": intraday_cutoff.get("source") or "intraday_1m",
            "price": price,
            "change_pct": change_pct,
            "change_amount": change_amount,
            "volume": intraday_cutoff.get("window_volume"),
            "amount": intraday_cutoff.get("window_amount"),
            "open_price": intraday_cutoff.get("window_open"),
            "high": intraday_cutoff.get("window_high"),
            "low": intraday_cutoff.get("window_low"),
            "pre_close": prev_close,
            "timestamp": intraday_cutoff.get("last_bar_at"),
            "cutoff_at": intraday_cutoff.get("cutoff_at"),
            "semantics": "point_in_time_intraday_cutoff",
        }

    @staticmethod
    def _intraday_daily_bar(
        *,
        intraday_cutoff: Dict[str, Any],
        trade_date: Optional[date],
    ) -> Optional[Dict[str, Any]]:
        if (
            trade_date is None
            or not isinstance(intraday_cutoff, dict)
            or intraday_cutoff.get("status") != "ok"
        ):
            return None
        close = RunnerContextBuilder._safe_float(intraday_cutoff.get("window_close"))
        if close is None or close <= 0:
            return None
        return {
            "date": trade_date.isoformat(),
            "open": RunnerContextBuilder._safe_float(intraday_cutoff.get("window_open")),
            "high": RunnerContextBuilder._safe_float(intraday_cutoff.get("window_high")),
            "low": RunnerContextBuilder._safe_float(intraday_cutoff.get("window_low")),
            "close": close,
            "volume": RunnerContextBuilder._safe_float(intraday_cutoff.get("window_volume")),
            "amount": RunnerContextBuilder._safe_float(intraday_cutoff.get("window_amount")),
            "pct_chg": None,
            "partial_intraday": True,
            "source": f"{intraday_cutoff.get('source') or 'intraday_1m'}_cutoff",
            "cutoff_at": intraday_cutoff.get("cutoff_at"),
        }

    def _fetch_daily_rows(
        self,
        *,
        symbol: str,
        days: int,
        target_date: Optional[date] = None,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        try:
            if self._data_fetcher is None:
                self._data_fetcher = DataFetcherManager()
            df, source = self._data_fetcher.get_daily_data(
                symbol,
                days=days,
                end_date=target_date.isoformat() if target_date else None,
            )
            if df is None or df.empty:
                return [], {"status": "empty", "source": source or "unknown"}

            saved_rows = self.stock_repo.save_dataframe(df, symbol, str(source or "market_data_fallback"))
            if "date" in df.columns:
                df = df.sort_values("date")

            rows: List[Dict[str, Any]] = []
            for index, row in df.iterrows():
                raw_date = row.get("date") if hasattr(row, "get") else None
                if raw_date is None:
                    raw_date = index
                if hasattr(raw_date, "date"):
                    raw_date = raw_date.date()
                rows.append(
                    {
                        "date": raw_date.isoformat() if hasattr(raw_date, "isoformat") else str(raw_date),
                        "open": self._safe_float(row.get("open")),
                        "high": self._safe_float(row.get("high")),
                        "low": self._safe_float(row.get("low")),
                        "close": self._safe_float(row.get("close")),
                        "volume": self._safe_float(row.get("volume")),
                        "amount": self._safe_float(row.get("amount")),
                        "pct_chg": self._safe_float(row.get("pct_chg")),
                    }
                )

            return rows, {
                "status": "ok",
                "source": str(source or "unknown"),
                "rows": len(rows),
                "saved_rows": saved_rows,
            }
        except Exception as exc:
            return [], {"status": "failed", "error": str(exc)[:300]}

    @staticmethod
    def _build_technical_context(
        *,
        daily: List[Dict[str, Any]],
        realtime: Dict[str, Any],
        source: str = "stock_daily_cache",
    ) -> Dict[str, Any]:
        closes = [RunnerContextBuilder._safe_float(row.get("close")) for row in daily]
        closes = [value for value in closes if value is not None and value > 0]
        volumes = [RunnerContextBuilder._safe_float(row.get("volume")) for row in daily]
        volumes = [value for value in volumes if value is not None and value >= 0]
        current_price = RunnerContextBuilder._quote_price(realtime) or (closes[-1] if closes else None)
        latest_daily_date = daily[-1].get("date") if daily else None

        ma_periods = [5, 10, 20, 60, 120, 250]
        moving_averages: Dict[str, Any] = {}
        for period in ma_periods:
            if len(closes) < period:
                moving_averages[f"ma{period}"] = None
                continue
            ma_value = sum(closes[-period:]) / period
            bias_pct = None
            price_above = None
            if current_price is not None and ma_value > 0:
                bias_pct = round((current_price - ma_value) / ma_value * 100, 2)
                price_above = current_price > ma_value
            moving_averages[f"ma{period}"] = {
                "value": round(ma_value, 4),
                "bias_pct": bias_pct,
                "price_above": price_above,
            }

        returns: Dict[str, Optional[float]] = {}
        for days in (1, 5, 20, 60, 120):
            returns[f"return_{days}d_pct"] = RunnerContextBuilder._return_pct(closes, days)

        avg_volume_5d = RunnerContextBuilder._avg_tail(volumes, 5)
        avg_volume_20d = RunnerContextBuilder._avg_tail(volumes, 20)
        latest_volume = volumes[-1] if volumes else None
        volume_ratio_vs_20d = None
        if latest_volume is not None and avg_volume_20d and avg_volume_20d > 0:
            volume_ratio_vs_20d = round(latest_volume / avg_volume_20d, 2)

        return {
            "status": "ok" if closes else "no_data",
            "source": source,
            "latest_daily_date": latest_daily_date,
            "bars_available": len(daily),
            "current_price": current_price,
            "moving_averages": moving_averages,
            "returns": returns,
            "volume": {
                "latest_volume": latest_volume,
                "avg_volume_5d": round(avg_volume_5d, 0) if avg_volume_5d is not None else None,
                "avg_volume_20d": round(avg_volume_20d, 0) if avg_volume_20d is not None else None,
                "volume_ratio_vs_20d": volume_ratio_vs_20d,
                "realtime_volume_ratio": realtime.get("volume_ratio") if isinstance(realtime, dict) else None,
                "realtime_turnover_rate": realtime.get("turnover_rate") if isinstance(realtime, dict) else None,
            },
        }

    @staticmethod
    def _build_long_horizon_context(
        *,
        symbol: str,
        stock_name: str,
        daily: List[Dict[str, Any]],
        market_data: Dict[str, Any],
        technical_context: Dict[str, Any],
        fundamental_context: Dict[str, Any],
        information_context: Dict[str, Any],
        sentiment_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        realtime = market_data.get("realtime_quote") if isinstance(market_data, dict) else {}
        current_price = (
            RunnerContextBuilder._safe_float((technical_context or {}).get("current_price"))
            or RunnerContextBuilder._quote_price(realtime if isinstance(realtime, dict) else {})
        )
        closes = [RunnerContextBuilder._safe_float(row.get("close")) for row in daily]
        closes = [value for value in closes if value is not None and value > 0]
        moving_averages = (technical_context or {}).get("moving_averages")
        if not isinstance(moving_averages, dict):
            moving_averages = {}
        returns = (technical_context or {}).get("returns")
        if not isinstance(returns, dict):
            returns = {}

        technical_structure = {
            "current_price": current_price,
            "latest_daily_date": (technical_context or {}).get("latest_daily_date"),
            "bars_available": (technical_context or {}).get("bars_available") or len(daily),
            "moving_average_alignment": {
                key: moving_averages.get(key)
                for key in ("ma60", "ma120", "ma250")
                if key in moving_averages
            },
            "all_moving_averages": moving_averages,
            "returns": {
                key: returns.get(key)
                for key in ("return_20d_pct", "return_60d_pct", "return_120d_pct")
                if key in returns
            },
            "price_windows": {
                f"{days}d": RunnerContextBuilder._price_window_context(
                    daily=daily,
                    days=days,
                    current_price=current_price,
                )
                for days in (20, 60, 120, 250)
            },
            "volume": (technical_context or {}).get("volume") or {},
        }
        valuation_snapshot = RunnerContextBuilder._valuation_snapshot(
            fundamental_context=fundamental_context,
            realtime=realtime if isinstance(realtime, dict) else {},
        )
        fundamental_availability = RunnerContextBuilder._fundamental_availability(
            fundamental_context=fundamental_context,
            valuation_snapshot=valuation_snapshot,
        )
        information_risk_flags = RunnerContextBuilder._information_risk_flags(information_context)
        pilot_entry_gate = RunnerContextBuilder._long_pilot_entry_gate(
            technical_structure=technical_structure,
            valuation_snapshot=valuation_snapshot,
            fundamental_availability=fundamental_availability,
            information_risk_flags=information_risk_flags,
            sentiment_context=sentiment_context,
        )

        status = "ok"
        if not closes and current_price is None:
            status = "no_data"
        elif (
            fundamental_availability["critical_gaps"]
            or information_risk_flags
            or pilot_entry_gate["status"] in {"blocked", "watch"}
        ):
            status = "partial"

        return {
            "status": status,
            "source": "derived_from_shared_symbol_facts",
            "symbol": symbol,
            "name": stock_name,
            "horizon": "long",
            "technical_structure": technical_structure,
            "valuation_snapshot": valuation_snapshot,
            "fundamental_availability": fundamental_availability,
            "information_risk_flags": information_risk_flags,
            "pilot_entry_gate": pilot_entry_gate,
            "interpretation_note": (
                "This is a derived long-profile checklist over the same shared symbol_facts. "
                "It does not change the watchlist and is not an automatic trading signal."
            ),
        }

    @staticmethod
    def _price_window_context(
        *,
        daily: List[Dict[str, Any]],
        days: int,
        current_price: Optional[float],
    ) -> Dict[str, Any]:
        rows = daily[-days:] if daily else []
        highs = [RunnerContextBuilder._safe_float(row.get("high")) for row in rows]
        lows = [RunnerContextBuilder._safe_float(row.get("low")) for row in rows]
        highs = [value for value in highs if value is not None and value > 0]
        lows = [value for value in lows if value is not None and value > 0]
        window_high = max(highs) if highs else None
        window_low = min(lows) if lows else None
        drawdown_from_high_pct = None
        upside_from_low_pct = None
        if current_price is not None and window_high:
            drawdown_from_high_pct = round((current_price - window_high) / window_high * 100, 2)
        if current_price is not None and window_low:
            upside_from_low_pct = round((current_price - window_low) / window_low * 100, 2)
        return {
            "bars": len(rows),
            "high": window_high,
            "low": window_low,
            "drawdown_from_high_pct": drawdown_from_high_pct,
            "upside_from_low_pct": upside_from_low_pct,
        }

    @staticmethod
    def _valuation_snapshot(
        *,
        fundamental_context: Dict[str, Any],
        realtime: Dict[str, Any],
    ) -> Dict[str, Any]:
        nested = RunnerContextBuilder._nested_fundamental_context(fundamental_context)
        valuation = nested.get("valuation") if isinstance(nested, dict) else {}
        valuation_data = valuation.get("data") if isinstance(valuation, dict) else {}
        if not isinstance(valuation_data, dict):
            valuation_data = {}

        pe_ratio = RunnerContextBuilder._first_float(
            (fundamental_context or {}).get("pe_ratio"),
            valuation_data.get("pe_ratio"),
            (realtime or {}).get("pe_ratio"),
        )
        pb_ratio = RunnerContextBuilder._first_float(
            (fundamental_context or {}).get("pb_ratio"),
            valuation_data.get("pb_ratio"),
            (realtime or {}).get("pb_ratio"),
        )
        total_mv = RunnerContextBuilder._first_float(
            (fundamental_context or {}).get("total_mv"),
            valuation_data.get("total_mv"),
            (realtime or {}).get("total_mv"),
        )
        circ_mv = RunnerContextBuilder._first_float(
            (fundamental_context or {}).get("circ_mv"),
            valuation_data.get("circ_mv"),
            (realtime or {}).get("circ_mv"),
        )
        source = "missing"
        if any(value is not None for value in (pe_ratio, pb_ratio, total_mv, circ_mv)):
            source = "fundamental_context.valuation_or_realtime_quote"
        flags: List[str] = []
        if pe_ratio is not None and pe_ratio > 100:
            flags.append("pe_ratio_above_100")
        if pb_ratio is not None and pb_ratio > 10:
            flags.append("pb_ratio_above_10")
        return {
            "pe_ratio": pe_ratio,
            "pb_ratio": pb_ratio,
            "total_mv": total_mv,
            "circ_mv": circ_mv,
            "source": source,
            "has_basic_valuation": any(value is not None for value in (pe_ratio, pb_ratio, total_mv, circ_mv)),
            "valuation_flags": flags,
        }

    @staticmethod
    def _fundamental_availability(
        *,
        fundamental_context: Dict[str, Any],
        valuation_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        nested = RunnerContextBuilder._nested_fundamental_context(fundamental_context)
        coverage = nested.get("coverage") if isinstance(nested, dict) else {}
        if not isinstance(coverage, dict):
            coverage = {}

        block_status: Dict[str, str] = {}
        available_blocks: List[str] = []
        unavailable_blocks: List[str] = []
        for block in FUNDAMENTAL_FALLBACK_BLOCKS:
            payload = nested.get(block) if isinstance(nested, dict) else {}
            payload_status = payload.get("status") if isinstance(payload, dict) else None
            status = str(coverage.get(block) or payload_status or "unknown").strip().lower()
            block_status[block] = status
            payload_data = payload.get("data") if isinstance(payload, dict) else None
            has_data = bool(payload_data) if isinstance(payload_data, (dict, list)) else payload_data is not None
            if status in UNAVAILABLE_FACT_STATUSES or status == "unknown":
                unavailable_blocks.append(block)
            elif has_data or status in {"ok", "available", "success"}:
                available_blocks.append(block)
            else:
                unavailable_blocks.append(block)

        has_basic_valuation = bool((valuation_snapshot or {}).get("has_basic_valuation"))
        critical_gaps: List[str] = []
        if not has_basic_valuation:
            critical_gaps.append("basic_valuation_missing")
        if block_status.get("valuation") in {"failed", "not_supported", "no_data"} and not has_basic_valuation:
            critical_gaps.append(f"valuation_{block_status.get('valuation')}")

        return {
            "status": str((nested or {}).get("status") or (fundamental_context or {}).get("status") or "unknown"),
            "coverage": block_status,
            "available_blocks": available_blocks,
            "unavailable_blocks": unavailable_blocks,
            "has_basic_valuation": has_basic_valuation,
            "has_earnings": "earnings" in available_blocks,
            "has_growth": "growth" in available_blocks,
            "has_institution": "institution" in available_blocks,
            "has_capital_flow": "capital_flow" in available_blocks,
            "critical_gaps": RunnerContextBuilder._dedupe_keep_order(critical_gaps),
            "data_gap_note": (
                "Missing earnings/growth/institution/capital_flow blocks reduce confidence, but do not by "
                "themselves forbid a 100-share pilot entry when valuation and long structure are acceptable."
            ),
        }

    @staticmethod
    def _information_risk_flags(information_context: Dict[str, Any]) -> List[Dict[str, str]]:
        if not isinstance(information_context, dict):
            return []
        raw_results = information_context.get("results") or information_context.get("items") or []
        if not isinstance(raw_results, list):
            return []
        flags: List[Dict[str, str]] = []
        for item in raw_results[:10]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("headline") or "").strip()
            snippet = str(item.get("snippet") or item.get("summary") or item.get("content") or "").strip()
            text = f"{title} {snippet}"
            for keyword in LONG_HORIZON_RISK_KEYWORDS:
                if keyword in text:
                    flags.append(
                        {
                            "keyword": keyword,
                            "title": title[:120],
                        }
                    )
                    break
        return flags[:5]

    @staticmethod
    def _long_pilot_entry_gate(
        *,
        technical_structure: Dict[str, Any],
        valuation_snapshot: Dict[str, Any],
        fundamental_availability: Dict[str, Any],
        information_risk_flags: List[Dict[str, str]],
        sentiment_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        blockers: List[str] = []
        supports: List[str] = []
        warnings: List[str] = []

        current_price = RunnerContextBuilder._safe_float(technical_structure.get("current_price"))
        if current_price is None:
            blockers.append("current_price_missing")

        ma = technical_structure.get("moving_average_alignment") or {}
        ma60 = ma.get("ma60") if isinstance(ma, dict) else None
        ma120 = ma.get("ma120") if isinstance(ma, dict) else None
        ma250 = ma.get("ma250") if isinstance(ma, dict) else None
        if isinstance(ma60, dict) and ma60.get("price_above") is True:
            supports.append("price_above_ma60")
        elif isinstance(ma60, dict) and ma60.get("price_above") is False:
            blockers.append("price_below_ma60")
        else:
            blockers.append("ma60_missing")
        if isinstance(ma120, dict) and ma120.get("price_above") is True:
            supports.append("price_above_ma120")
        elif isinstance(ma120, dict) and ma120.get("price_above") is False:
            warnings.append("price_below_ma120")
        if isinstance(ma250, dict) and ma250.get("price_above") is True:
            supports.append("price_above_ma250")

        returns = technical_structure.get("returns") or {}
        return_60d = RunnerContextBuilder._safe_float(returns.get("return_60d_pct"))
        return_120d = RunnerContextBuilder._safe_float(returns.get("return_120d_pct"))
        if return_60d is not None:
            if return_60d >= 0:
                supports.append("return_60d_non_negative")
            elif return_60d < -20:
                blockers.append("return_60d_below_minus_20")
        if return_120d is not None and return_120d >= 0:
            supports.append("return_120d_non_negative")

        ma20 = (technical_structure.get("moving_average_alignment") or {}).get("ma20")
        if not isinstance(ma20, dict):
            full_ma = technical_structure.get("all_moving_averages") or {}
            ma20 = full_ma.get("ma20") if isinstance(full_ma, dict) else None
        ma20_bias = RunnerContextBuilder._safe_float(ma20.get("bias_pct")) if isinstance(ma20, dict) else None
        if ma20_bias is not None and ma20_bias > 35:
            blockers.append("ma20_bias_above_35")

        return_20d = RunnerContextBuilder._safe_float(returns.get("return_20d_pct"))
        if return_20d is not None and return_20d > 60:
            blockers.append("return_20d_above_60")

        volume = technical_structure.get("volume") or {}
        turnover = RunnerContextBuilder._safe_float(volume.get("realtime_turnover_rate"))
        if turnover is not None and turnover > 15:
            blockers.append("turnover_rate_above_15")

        if valuation_snapshot.get("has_basic_valuation"):
            supports.append("basic_valuation_available")
        else:
            blockers.append("basic_valuation_missing")
        for flag in valuation_snapshot.get("valuation_flags") or []:
            warnings.append(str(flag))

        for gap in fundamental_availability.get("critical_gaps") or []:
            blockers.append(str(gap))
        for block in ("earnings", "growth", "institution", "capital_flow"):
            if block in (fundamental_availability.get("unavailable_blocks") or []):
                warnings.append(f"{block}_unavailable")

        if information_risk_flags:
            blockers.append("pre_cutoff_information_risk_flag")

        if isinstance(sentiment_context, dict):
            risk_level = str(sentiment_context.get("risk_level") or "").strip().lower()
            if risk_level in {"high", "elevated"}:
                warnings.append(f"sentiment_risk_{risk_level}")

        blockers = RunnerContextBuilder._dedupe_keep_order(blockers)
        supports = RunnerContextBuilder._dedupe_keep_order(supports)
        warnings = RunnerContextBuilder._dedupe_keep_order(warnings)
        status = "blocked" if blockers else ("candidate" if len(supports) >= 3 else "watch")
        return {
            "status": status,
            "supports": supports,
            "blocking_reasons": blockers,
            "warnings": warnings,
            "pilot_size_rule": "A-share pilot entries must still be positive multiples of 100 shares and pass cash checks.",
            "decision_note": (
                "candidate permits consideration of a small pilot buy; blocked/watch means observe or hold unless "
                "the policy is explicitly evolved with stronger evidence."
            ),
        }

    @staticmethod
    def _return_pct(values: List[float], days: int) -> Optional[float]:
        if len(values) <= days:
            return None
        base = values[-days - 1]
        current = values[-1]
        if base <= 0:
            return None
        return round((current - base) / base * 100, 2)

    @staticmethod
    def _avg_tail(values: List[float], days: int) -> Optional[float]:
        if not values:
            return None
        tail = values[-days:]
        if not tail:
            return None
        return sum(tail) / len(tail)

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed

    @staticmethod
    def _first_float(*values: Any) -> Optional[float]:
        for value in values:
            parsed = RunnerContextBuilder._safe_float(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _fetch_fundamental_context(*, symbol: str, live_data: bool) -> Dict[str, Any]:
        if not live_data:
            return {"status": "skipped", "reason": "live_data_disabled"}
        try:
            from src.agent.tools.data_tools import _handle_get_stock_info

            return _handle_get_stock_info(symbol)
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}

    @staticmethod
    def _merge_realtime_valuation_into_fundamental_context(
        *,
        fundamental_context: Dict[str, Any],
        realtime: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(fundamental_context, dict) or not isinstance(realtime, dict):
            return fundamental_context
        valuation_fields = {
            "pe_ratio": RunnerContextBuilder._safe_float(realtime.get("pe_ratio")),
            "pb_ratio": RunnerContextBuilder._safe_float(realtime.get("pb_ratio")),
            "total_mv": RunnerContextBuilder._safe_float(realtime.get("total_mv")),
            "circ_mv": RunnerContextBuilder._safe_float(realtime.get("circ_mv")),
        }
        valuation_fields = {key: value for key, value in valuation_fields.items() if value is not None}
        if not valuation_fields:
            return fundamental_context

        patched = dict(fundamental_context)
        for key, value in valuation_fields.items():
            patched.setdefault(key, value)

        nested = patched.get("fundamental_context")
        if not isinstance(nested, dict):
            return patched
        nested = dict(nested)
        valuation_block = nested.get("valuation")
        if not isinstance(valuation_block, dict):
            valuation_block = {"status": "partial", "data": {}}
        else:
            valuation_block = dict(valuation_block)
        valuation_data = valuation_block.get("data")
        if not isinstance(valuation_data, dict):
            valuation_data = {}
        else:
            valuation_data = dict(valuation_data)
        changed = False
        for key, value in valuation_fields.items():
            if valuation_data.get(key) is None:
                valuation_data[key] = value
                changed = True
        if changed:
            valuation_block["data"] = valuation_data
            if str(valuation_block.get("status") or "").lower() in UNAVAILABLE_FACT_STATUSES:
                valuation_block["status"] = "partial"
            if str(nested.get("status") or "").lower() in {"not_supported", "failed", "no_data"}:
                nested["status"] = "partial"
            source_chain = list(valuation_block.get("source_chain") or [])
            source_chain.append(
                {
                    "provider": f"realtime_quote:{realtime.get('source') or 'unknown'}",
                    "result": "valuation_fallback",
                    "duration_ms": 0,
                }
            )
            valuation_block["source_chain"] = source_chain
            nested["valuation"] = valuation_block
            coverage = nested.get("coverage")
            if isinstance(coverage, dict):
                coverage = dict(coverage)
                if str(coverage.get("valuation") or "").lower() in UNAVAILABLE_FACT_STATUSES:
                    coverage["valuation"] = "partial"
                nested["coverage"] = coverage
            patched["fundamental_context"] = nested
        return patched

    @staticmethod
    def _codex_research_policy(*, live_data: bool, data_cutoff_at: datetime) -> Dict[str, Any]:
        return {
            "enabled": bool(live_data),
            "data_cutoff_at": data_cutoff_at.isoformat(),
            "when_to_use": (
                "Use Codex supplemental web/search research when a symbol's "
                "codex_research_fallback.status is recommended, or when a trade decision materially depends "
                "on fresh information not covered by information_context."
            ),
            "triage_rule": (
                "For broad watchlists, research held symbols, likely buy/sell candidates, and large movers first; "
                "record unresolved lower-impact gaps instead of doing unfocused broad searches."
            ),
            "cutoff_rule": "Use only public information available at or before data_cutoff_at.",
            "source_priority": [
                "exchange/company announcements",
                "official investor-relations disclosures",
                "reputable financial media",
                "market data/news pages with timestamps",
            ],
            "output_expectation": (
                "If supplemental research affects the decision, cite source title, source name, "
                "published date/time when available, and URL in rationale/risk_notes and the final summary. "
                "If Codex search tooling is unavailable, record the information gap explicitly."
            ),
        }

    @staticmethod
    def _information_context_gap_reason(information_context: Dict[str, Any]) -> Optional[str]:
        if not isinstance(information_context, dict):
            return "information_context_not_dict"
        if information_context.get("error"):
            return "information_context_error"
        if information_context.get("success") is False:
            return "information_context_failed"

        status = str(information_context.get("status") or "").strip().lower()
        if status in {"failed", "skipped", "no_data"}:
            return f"information_context_{status}"

        results_count = information_context.get("results_count")
        if results_count is not None:
            try:
                if int(results_count) <= 0:
                    return "information_context_empty"
            except (TypeError, ValueError):
                return "information_context_invalid_results_count"

        if information_context.get("success") is True and not information_context.get("results"):
            return "information_context_empty"
        return None

    @staticmethod
    def _codex_research_queries(
        *,
        symbol: str,
        stock_name: str,
        gap_reasons: Optional[List[str]] = None,
    ) -> List[str]:
        display_name = (stock_name or symbol or "").strip()
        code = (symbol or "").strip()
        is_us_symbol = bool(code.isupper() and not code.isdigit())
        gap_text = " ".join(gap_reasons or [])
        if is_us_symbol:
            display = display_name if display_name and display_name != code else code
            queries = [
                f"{display} {code} latest stock news earnings guidance SEC filing",
                f"{display} {code} premarket after hours overnight trading news",
                f"{display} {code} analyst rating sector ETF peer performance risk",
            ]
            if "fundamental" in gap_text:
                queries.extend(
                    [
                        f"{display} {code} SEC 10-Q 8-K earnings investor relations",
                        f"{display} {code} valuation revenue margin cash flow guidance",
                    ]
                )
            if "capital_flow" in gap_text:
                queries.append(f"{display} {code} institutional ownership fund flow short interest")
            return RunnerContextBuilder._dedupe_keep_order(queries)[:6]

        if display_name == code:
            queries = [
                f"{code} 最新消息 公告",
                f"{code} 减持 处罚 诉讼 风险",
                f"{code} 业绩预告 财报 机构调研",
            ]
        else:
            queries = [
                f"{display_name} {code} 最新消息 公告",
                f"{display_name} {code} 减持 处罚 诉讼 风险",
                f"{display_name} {code} 业绩预告 财报 机构调研",
            ]
        if "fundamental" in gap_text:
            queries.append(f"{display_name} {code} 财报 营收 净利润 现金流 ROE 估值")
        if "valuation" in gap_text:
            queries.append(f"{display_name} {code} PE PB 市值 估值 东方财富")
        if "capital_flow" in gap_text:
            queries.append(f"{display_name} {code} 资金流向 主力净流入 龙虎榜")
        return RunnerContextBuilder._dedupe_keep_order(queries)[:6]

    @staticmethod
    def _dedupe_keep_order(items: List[str]) -> List[str]:
        seen = set()
        deduped: List[str] = []
        for item in items:
            normalized = str(item or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    @staticmethod
    def _nested_fundamental_context(fundamental_context: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(fundamental_context, dict):
            return {}
        nested = fundamental_context.get("fundamental_context")
        if isinstance(nested, dict):
            return nested
        return fundamental_context

    @staticmethod
    def _fundamental_context_gap_reasons(fundamental_context: Dict[str, Any]) -> List[str]:
        if not isinstance(fundamental_context, dict):
            return ["fundamental_context_invalid"]
        nested = RunnerContextBuilder._nested_fundamental_context(fundamental_context)
        if not nested:
            return ["fundamental_context_empty"]

        reasons: List[str] = []
        status = str(nested.get("status") or fundamental_context.get("status") or "").strip().lower()
        if status in UNAVAILABLE_FACT_STATUSES:
            reasons.append(f"fundamental_context_{status}")

        coverage = nested.get("coverage")
        if isinstance(coverage, dict):
            for block in FUNDAMENTAL_FALLBACK_BLOCKS:
                block_status = str(coverage.get(block) or "").strip().lower()
                if block_status in UNAVAILABLE_FACT_STATUSES:
                    reasons.append(f"fundamental_{block}_{block_status}")

        for block in FUNDAMENTAL_FALLBACK_BLOCKS:
            block_payload = nested.get(block)
            if not isinstance(block_payload, dict):
                continue
            block_status = str(block_payload.get("status") or "").strip().lower()
            if block_status in UNAVAILABLE_FACT_STATUSES:
                reasons.append(f"fundamental_{block}_{block_status}")

        return RunnerContextBuilder._dedupe_keep_order(reasons)

    @staticmethod
    def _provider_gap_layers(
        *,
        information_gap: Optional[str],
        fundamental_gaps: List[str],
    ) -> Dict[str, List[str]]:
        layers: Dict[str, List[str]] = {}
        if information_gap:
            layers["information_context"] = [information_gap]
        if fundamental_gaps:
            layers["fundamental_context"] = fundamental_gaps
        return layers

    @staticmethod
    def _build_codex_research_fallback(
        *,
        symbol: str,
        stock_name: str,
        information_context: Dict[str, Any],
        fundamental_context: Dict[str, Any],
        live_data: bool,
    ) -> Dict[str, Any]:
        if not live_data:
            return {
                "status": "disabled",
                "reason": "live_data_disabled",
                "gap_reasons": [],
                "provider_gap_layers": {},
                "queries": [],
            }

        information_gap = RunnerContextBuilder._information_context_gap_reason(information_context)
        fundamental_gaps = RunnerContextBuilder._fundamental_context_gap_reasons(fundamental_context)
        gap_reasons = RunnerContextBuilder._dedupe_keep_order(
            ([information_gap] if information_gap else []) + fundamental_gaps
        )
        provider_gap_layers = RunnerContextBuilder._provider_gap_layers(
            information_gap=information_gap,
            fundamental_gaps=fundamental_gaps,
        )
        return {
            "status": "recommended" if gap_reasons else "optional",
            "source": "Codex 外部兜底",
            "reason": gap_reasons[0] if gap_reasons else "provider_context_available",
            "gap_reasons": gap_reasons,
            "provider_gap_layers": provider_gap_layers,
            "scope": "supplemental_public_evidence_not_provider_replacement",
            "queries": RunnerContextBuilder._codex_research_queries(
                symbol=symbol,
                stock_name=stock_name,
                gap_reasons=gap_reasons,
            ),
            "can_supplement": [
                "exchange/company announcements and financial reports",
                "timestamped reputable media about catalysts and risks",
                "public valuation, ownership, fund-flow, and dragon-tiger-board pages when timestamped",
            ],
            "cannot_replace": [
                "DSA normalized quotes, K-lines, or intraday point-in-time bars",
                "proprietary or unavailable provider-only capital-flow datasets",
                "unlabeled facts inside fundamental_context or information_context",
            ],
            "instructions": [
                "Use Codex available web/search/browser tools only as a supplemental evidence layer.",
                "Label any used fact as Codex 外部兜底 and keep it separate from DSA provider facts.",
                "Respect evidence.data_cutoff_at and do not use later information for intraday decisions.",
                "Prefer official announcements, financial reports, and timestamped reputable financial media.",
                "Cite title/source/date/url when supplemental research is used.",
                "If only post-cutoff sources are found, record them as ignored_post_cutoff and do not use them for the decision.",
            ],
        }

    @staticmethod
    def _fetch_information_context(
        *,
        symbol: str,
        stock_name: str,
        live_data: bool,
        run: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not live_data:
            return {"status": "skipped", "reason": "live_data_disabled"}
        config = (run or {}).get("config") or {}
        information_policy = config.get("information_policy") if isinstance(config, dict) else {}
        if (
            (run or {}).get("market") == "us"
            and isinstance(information_policy, dict)
            and information_policy.get("codex_research_first") is True
            and not information_policy.get("provider_search_fallback_enabled")
        ):
            return {
                "status": "skipped",
                "reason": "codex_research_first",
                "success": False,
                "query": f"{stock_name or symbol} {symbol} stock latest news earnings filings",
                "provider_search_priority": information_policy.get("provider_search_priority") or [],
            }
        try:
            from src.agent.tools.search_tools import _handle_search_stock_news

            result = _handle_search_stock_news(symbol, stock_name)
            if isinstance(result, dict):
                return result
            return {"status": "failed", "error": "search returned non-dict payload"}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}

    @staticmethod
    def _build_sentiment_context(
        *,
        realtime: Dict[str, Any],
        information_context: Dict[str, Any],
        live_data: bool,
    ) -> Dict[str, Any]:
        if not live_data:
            return {"status": "skipped", "reason": "live_data_disabled"}
        change_pct = realtime.get("change_pct") if isinstance(realtime, dict) else None
        volume_ratio = realtime.get("volume_ratio") if isinstance(realtime, dict) else None
        news_count = information_context.get("results_count") if isinstance(information_context, dict) else None
        if change_pct is None and volume_ratio is None and news_count is None:
            return {"status": "no_data", "source": "derived_realtime_and_news"}
        mood = "neutral"
        try:
            change = float(change_pct) if change_pct is not None else 0.0
            vol_ratio = float(volume_ratio) if volume_ratio is not None else 1.0
            if change >= 5 or (change >= 2 and vol_ratio >= 1.5):
                mood = "positive"
            elif change <= -5 or (change <= -2 and vol_ratio >= 1.5):
                mood = "negative"
        except (TypeError, ValueError):
            pass
        return {
            "status": "derived",
            "source": "realtime_quote_and_news_count",
            "mood": mood,
            "intraday_change_pct": change_pct,
            "volume_ratio": volume_ratio,
            "news_results_count": news_count,
        }

    @staticmethod
    def _fact_data_quality(layers: Dict[str, Any]) -> Dict[str, Any]:
        statuses: Dict[str, str] = {}
        for layer_name, payload in layers.items():
            if layer_name == "market_data":
                market_payload = payload if isinstance(payload, dict) else {}
                realtime = market_payload.get("realtime_quote", {})
                intraday_cutoff = market_payload.get("intraday_cutoff", {})
                intraday_cutoff_ok = isinstance(intraday_cutoff, dict) and intraday_cutoff.get("status") == "ok"
                daily_count = RunnerContextBuilder._safe_float(market_payload.get("daily_bars_available"))
                if daily_count is not None and daily_count < RECENT_DAILY_BAR_DAYS:
                    statuses[layer_name] = "partial"
                elif market_payload.get("daily_bars_trade_date_provisional") is True and not intraday_cutoff_ok:
                    statuses[layer_name] = "partial"
                elif market_payload.get("daily_bars_stale_for_trade_date") is True:
                    statuses[layer_name] = "partial"
                elif (
                    isinstance(intraday_cutoff, dict)
                    and intraday_cutoff.get("status") in {"failed", "no_data"}
                ):
                    statuses[layer_name] = "partial"
                elif isinstance(realtime, dict) and realtime.get("error"):
                    statuses[layer_name] = "partial"
                elif isinstance(realtime, dict) and realtime.get("skipped"):
                    statuses[layer_name] = "partial"
                else:
                    statuses[layer_name] = "ok"
                continue
            if not isinstance(payload, dict):
                statuses[layer_name] = "failed"
                continue
            nested_status = None
            if layer_name == "fundamental_context":
                nested = payload.get("fundamental_context")
                if isinstance(nested, dict):
                    nested_status = nested.get("status")
            statuses[layer_name] = str(
                payload.get("status")
                or nested_status
                or ("ok" if not payload.get("error") else "failed")
            )
        unavailable_statuses = {"failed", "skipped", "no_data", "not_supported", "partial"}
        return {
            "layer_status": statuses,
            "failed_layers": [name for name, status in statuses.items() if status == "failed"],
            "skipped_layers": [name for name, status in statuses.items() if status == "skipped"],
            "unavailable_layers": [
                name for name, status in statuses.items() if status in unavailable_statuses
            ],
        }

    @staticmethod
    def _resolve_stock_name(symbol: str, realtime: Dict[str, Any]) -> str:
        if isinstance(realtime, dict):
            name = str(realtime.get("name") or "").strip()
            if name:
                return name
        try:
            from src.data.stock_index_loader import get_index_stock_name

            name = get_index_stock_name(symbol)
            if name:
                return name
        except Exception:
            pass
        return symbol

    def _collect_realtime_price_overrides(
        self,
        symbols: List[str],
    ) -> tuple[Dict[str, float], Dict[str, str], Dict[str, Any]]:
        prices: Dict[str, float] = {}
        sources: Dict[str, str] = {}
        gaps: Dict[str, Any] = {}
        for symbol in symbols:
            quote = self._fetch_realtime_quote(symbol)
            price = self._quote_price(quote)
            if price is None:
                gaps[symbol] = quote
                continue
            prices[symbol] = price
            source = quote.get("source") if isinstance(quote, dict) else None
            sources[symbol] = f"realtime_quote:{source or 'unknown'}"
        return prices, sources, gaps

    def _held_symbols_for_close(self, *, run: Dict[str, Any], trade_date: date) -> List[str]:
        symbols: List[str] = []
        for profile in run.get("profiles") or []:
            snapshot = self.portfolio_service.get_portfolio_snapshot(
                account_id=int(profile["account_id"]),
                as_of=trade_date,
                cost_method="fifo",
            )
            account = (snapshot.get("accounts") or [{}])[0]
            for symbol in self._held_symbols_from_account(account):
                if symbol not in symbols:
                    symbols.append(symbol)
        return symbols

    @staticmethod
    def _held_symbols_from_account(account: Dict[str, Any]) -> List[str]:
        symbols: List[str] = []
        for position in account.get("positions") or []:
            symbol = str(position.get("symbol") or "").strip()
            quantity = float(position.get("quantity") or 0.0)
            if symbol and quantity > 0 and symbol not in symbols:
                symbols.append(symbol)
        return symbols

    @staticmethod
    def _merge_symbols(*groups: List[str]) -> List[str]:
        symbols: List[str] = []
        for group in groups:
            for raw_symbol in group:
                symbol = str(raw_symbol or "").strip()
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
        return symbols

    @staticmethod
    def _quote_price(quote: Dict[str, Any]) -> Optional[float]:
        if not isinstance(quote, dict):
            return None
        for key in ("price", "current_price", "last_price", "close"):
            value = quote.get(key)
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
        return None

    @staticmethod
    def _realtime_quote_timeout_seconds() -> int:
        raw_value = os.getenv("IBKR_TIMEOUT_SECONDS") or str(REALTIME_QUOTE_TIMEOUT_SECONDS)
        try:
            timeout = int(float(raw_value))
        except (TypeError, ValueError):
            return REALTIME_QUOTE_TIMEOUT_SECONDS
        return max(1, timeout)

    @staticmethod
    @staticmethod
    def _should_skip_ibkr_realtime(intraday_cutoff: Dict[str, Any]) -> bool:
        if not isinstance(intraday_cutoff, dict):
            return False
        if intraday_cutoff.get("status") != "failed":
            return False
        text = " ".join(
            str(intraday_cutoff.get(key) or "")
            for key in ("error", "source")
        ).lower()
        return "ibkr" in text and ("timed out" in text or "temporarily disabled" in text)

    @staticmethod
    def _us_realtime_priority_without_ibkr() -> str:
        raw = (
            os.getenv("US_REALTIME_DATA_SOURCE_PRIORITY")
            or os.getenv("US_MARKET_DATA_SOURCE_PRIORITY")
            or "longbridge,twelvedata,finnhub,alpha_vantage,massive,yfinance"
        )
        aliases = {"ibkr", "interactive_brokers", "interactivebrokers"}
        tokens = [
            token.strip()
            for token in raw.split(",")
            if token.strip() and token.strip().lower().replace("-", "_").replace(" ", "_") not in aliases
        ]
        return ",".join(tokens) or "longbridge,twelvedata,finnhub,alpha_vantage,massive,yfinance"

    @staticmethod
    def _fetch_realtime_quote(symbol: str, *, skip_ibkr: bool = False) -> Dict[str, Any]:
        child_code = r"""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
symbol = sys.argv[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from src.config import setup_env
from data_provider.base import DataFetcherManager

setup_env()
quote = DataFetcherManager().get_realtime_quote(symbol)
if quote is None:
    payload = {"error": "No realtime quote available", "source": "realtime_quote", "retriable": True}
else:
    payload = quote.to_dict() if hasattr(quote, "to_dict") else dict(quote)
print(json.dumps(payload, ensure_ascii=False))
"""
        try:
            timeout_seconds = RunnerContextBuilder._realtime_quote_timeout_seconds()
            child_env = os.environ.copy()
            if skip_ibkr:
                child_env["US_REALTIME_DATA_SOURCE_PRIORITY"] = (
                    RunnerContextBuilder._us_realtime_priority_without_ibkr()
                )
            completed = subprocess.run(
                [sys.executable, "-c", child_code, str(ROOT), symbol],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env=child_env,
                timeout=timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                return {
                    "error": (completed.stderr or completed.stdout or "realtime quote subprocess failed").strip(),
                    "source": "realtime_quote",
                    "retriable": True,
                }
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                return {"error": "No realtime quote output", "source": "realtime_quote", "retriable": True}
            return json.loads(lines[-1])
        except subprocess.TimeoutExpired:
            return {
                "error": (
                    "Realtime quote fetch timed out after "
                    f"{RunnerContextBuilder._realtime_quote_timeout_seconds()}s"
                ),
                "source": "realtime_quote",
                "retriable": True,
            }
        except Exception as exc:
            return {"error": str(exc), "source": "realtime_quote", "retriable": True}

    @staticmethod
    def _output_contract(phase: str, run: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if phase == "close":
            return RunnerContextBuilder._close_review_contract(run)
        return RunnerContextBuilder._decision_contract((run or {}).get("market") or "cn")

    @staticmethod
    def _decision_contract(market: str = "cn") -> Dict[str, Any]:
        if market == "us":
            return {
                "required_json_fields": [
                    "action",
                    "rationale",
                    "risk_notes",
                    "confidence",
                ],
                "action_values": ["observe", "hold", "buy", "sell", "cancel"],
                "buy_sell_required_fields": [
                    "symbol",
                    "side",
                    "quantity",
                    "order_type",
                    "limit_price",
                    "submitted_at",
                    "effective_at",
                ],
                "us_cash_account_constraints": [
                    "No trade is required for a phase; observe/hold is valid when the profile's strategy calls for waiting.",
                    "Buy/add only symbols listed in evidence.symbol_scope.buy_allowed_symbols.",
                    "Symbols in evidence.symbol_scope.exit_only_symbols may be researched, held, reduced, or sold, but not bought/added.",
                    "Quantity must be positive whole shares unless run.config.allow_fractional_shares is true.",
                    "Buy orders may use settled cash only; same-day or unsettled sell proceeds are unavailable until T+1 US business day.",
                    "At most one same-symbol buy-then-sell day trade is allowed per rolling 5 US business days.",
                    "Extended-hours and overnight decisions must use limit orders and explicitly mention session/liquidity risk.",
                    "Use only evidence at or before data_cutoff_at.",
                ],
            }
        return {
            "required_json_fields": [
                "action",
                "rationale",
                "risk_notes",
                "confidence",
            ],
            "action_values": ["observe", "hold", "buy", "sell", "cancel"],
            "buy_sell_required_fields": [
                "symbol",
                "side",
                "quantity",
                "order_type",
                "limit_price",
                "submitted_at",
                "effective_at",
            ],
            "a_share_constraints": [
                "No trade is required for a phase; observe/hold is valid when the profile's strategy calls for waiting.",
                "Buy/add only symbols listed in evidence.symbol_scope.buy_allowed_symbols.",
                "Symbols in evidence.symbol_scope.exit_only_symbols may be researched, held, reduced, or sold, but not bought/added.",
                "Quantity must be a positive multiple of 100.",
                "No same-day sell for shares bought today.",
                "Use only evidence at or before data_cutoff_at.",
            ],
        }

    @staticmethod
    def _close_review_contract(run: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        market = (run or {}).get("market") or "cn"
        market_constraint = (
            "For US cash-account runs, review settled-cash usage, unsettled proceeds, extended-hours/overnight liquidity, and day-trade allowance usage."
            if market == "us"
            else "For A-share runs, review lot-size, T+1 share availability, and A-share session discipline."
        )
        return {
            "required_json_fields": [
                "profile_key",
                "self_review",
                "policy_update",
            ],
            "self_review_required_fields": [
                "trade_date",
                "what_worked",
                "what_failed_or_was_missed",
                "risk_discipline",
                "data_quality_notes",
                "next_session_focus",
            ],
            "policy_update": {
                "required": False,
                "when_to_use": (
                    "Only propose a forward-only policy update when the close review finds a durable lesson. "
                    "Do not overfit one noisy day."
                ),
                "fields_when_present": [
                    "version_label",
                    "body_markdown",
                    "effective_from",
                    "change_reason",
                ],
                "persist_command": (
                    "python scripts/run_agent_backtest_cycle.py evolve-policy --run-id <run_id> "
                    "--profile-key <profile_key> --policy-file <policy_json_file>"
                ),
            },
            "close_constraints": [
                "Do not create buy/sell/hold trading decisions or orders during close review.",
                "Keep profile contexts isolated and review only this profile's events and holdings.",
                "Policy updates are forward-only and must preserve the profile's style boundary.",
                "If valuation_stale is true, treat NAV ranking and PnL conclusions as tentative.",
                market_constraint,
            ],
        }

    @staticmethod
    def _render_markdown_context(payload: Dict[str, Any]) -> str:
        run = payload["run"]
        profile = payload["profile"]
        observation = payload.get("observation") or {}
        evidence = payload["evidence"]
        scope = evidence.get("symbol_scope") or {}
        lines = [
            "# Codex Agent Backtest Decision Context",
            "",
            "You are the only decision maker for this isolated profile. Do not use or infer other profiles' decisions.",
            "",
            "## Run",
            f"- run_id: {run['id']}",
            f"- market: {run.get('market')}",
            f"- symbols: {', '.join(run.get('symbols') or [])}",
            f"- rule_version: {run.get('rule_version')}",
            f"- max_observations_per_day: {run.get('max_observations_per_day')}",
            "",
            "## Symbol Scope",
            f"- watchlist_symbols: {', '.join(scope.get('watchlist_symbols') or []) or '(none)'}",
            f"- held_symbols: {', '.join(scope.get('held_symbols') or []) or '(none)'}",
            f"- research_symbols: {', '.join(scope.get('research_symbols') or []) or '(none)'}",
            f"- buy_allowed_symbols: {', '.join(scope.get('buy_allowed_symbols') or []) or '(none)'}",
            f"- exit_only_symbols: {', '.join(scope.get('exit_only_symbols') or []) or '(none)'}",
            "- no_trade_required: true",
            "",
            "## Profile",
            f"- profile_key: {profile['profile_key']}",
            f"- display_name: {profile['display_name']}",
            f"- style_profile: {profile['style_profile']}",
            f"- policy_version_label: {profile['policy_version_label']}",
            f"- context_namespace: {profile['context_namespace']}",
            "",
            "## Current Policy",
            profile.get("policy_markdown") or "(empty)",
            "",
            "## Timing",
            f"- phase: {payload['phase']}",
            f"- trade_date: {payload['trade_date']}",
            f"- observation_time: {payload['observation_time']}",
            f"- data_cutoff_at: {payload['data_cutoff_at']}",
            f"- observation_id: {observation.get('id')}",
            "",
            "## Codex Supplemental Research",
            RunnerContextBuilder._render_codex_research_notes(evidence),
            "",
            "## Evidence JSON",
            "```json",
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            (
                "## Required Close Self-Review JSON"
                if payload["phase"] == "close"
                else "## Required Decision JSON"
            ),
            (
                "Return one JSON object for self-review. Do not create trading decisions or orders in close phase."
                if payload["phase"] == "close"
                else "Return one JSON object. For observe/hold, omit order fields. For buy/sell, include all order fields."
            ),
            "```json",
            json.dumps(payload["output_contract"], ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_codex_research_notes(evidence: Dict[str, Any]) -> str:
        policy = evidence.get("codex_research_policy") or {}
        if not policy.get("enabled"):
            return "- Disabled because live_data is false."

        facts = evidence.get("symbol_facts") or {}
        recommended: List[tuple[str, str, str, List[str]]] = []
        optional_count = 0
        for symbol, fact in facts.items():
            if not isinstance(fact, dict):
                continue
            fallback = fact.get("codex_research_fallback") or {}
            status = fallback.get("status")
            if status == "recommended":
                recommended.append(
                    (
                        str(symbol),
                        str(fact.get("name") or symbol),
                        str(fallback.get("reason") or "information_context_gap"),
                        [str(q) for q in (fallback.get("queries") or [])],
                    )
                )
            elif status == "optional":
                optional_count += 1

        if not recommended:
            return (
                "- No required supplemental research. Use Codex search only if the decision depends on "
                "fresh information beyond information_context."
            )

        lines = [
            (
                "- Use Codex web/search tools as labeled external evidence for these gaps before deciding. "
                "Triage held symbols, likely buy/sell candidates, and large movers first; respect data_cutoff_at "
                "and cite title/date/url when used."
            )
        ]
        for symbol, name, reason, queries in recommended:
            lines.append(f"- {symbol} {name}: {reason}")
            for query in queries[:3]:
                lines.append(f"  - {query}")
        if optional_count:
            lines.append(f"- {optional_count} other symbols have optional supplemental queries.")
        return "\n".join(lines)


def command_prepare(args: argparse.Namespace) -> Dict[str, Any]:
    service = AgentBacktestService()
    run = service.create_run(
        name=args.name,
        symbols=parse_symbols(args.symbols),
        start_date=parse_date(args.start_date),
        end_date=parse_date(args.end_date),
        initial_cash_per_agent=args.initial_cash_per_agent,
        max_observations_per_day=args.max_observations_per_day,
        market=args.market,
    )
    return {
        "created": True,
        "run": run,
        "next_step": f"Run: python scripts/run_agent_backtest_cycle.py cycle --run-id {run['id']} --phase verify",
    }


def command_cycle(args: argparse.Namespace) -> Dict[str, Any]:
    service = AgentBacktestService()
    builder = RunnerContextBuilder(
        service=service,
        portfolio_service=PortfolioService(),
        stock_repo=StockRepository(),
    )
    trade_date = parse_date(args.trade_date, default=date.today())
    if trade_date is None:
        raise ValueError("trade_date is required")
    run = service.get_run(args.run_id)
    observation_time = args.observation_time or default_phase_time(args.phase, str(run.get("market") or "cn"))
    return builder.build_cycle(
        run_id=args.run_id,
        phase=args.phase,
        trade_date=trade_date,
        observation_time=observation_time,
        context_dir=Path(args.context_dir),
        live_data=args.live_data,
        record_observation=not args.no_record_observation,
    )


def command_apply_decision(args: argparse.Namespace) -> Dict[str, Any]:
    service = AgentBacktestService()
    decision = load_decision_payload(args)
    run = service.get_run(args.run_id)
    profile = next((p for p in run.get("profiles") or [] if p["profile_key"] == args.profile_key), None)
    if profile is None:
        raise AgentBacktestError(f"profile not found: {args.profile_key}")

    trade_date = parse_date(decision.get("trade_date") or args.trade_date, default=date.today())
    if trade_date is None:
        raise AgentBacktestError("trade_date is required")
    decision_time = parse_decision_datetime(decision.get("decision_time") or args.decision_time, trade_date)
    action = str(decision.get("action") or "observe").strip().lower()
    symbol = decision.get("symbol")
    side = decision.get("side")
    quantity = decision.get("quantity")
    order_type = decision.get("order_type")
    limit_price = decision.get("limit_price")
    observation_id = decision.get("observation_id") or args.observation_id

    decision_row = service.record_decision(
        run_id=args.run_id,
        profile_key=args.profile_key,
        observation_id=observation_id,
        trade_date=trade_date,
        decision_time=decision_time,
        action=action,
        symbol=symbol,
        side=side,
        quantity=float(quantity) if quantity is not None else None,
        order_type=order_type,
        limit_price=float(limit_price) if limit_price is not None else None,
        confidence=float(decision["confidence"]) if decision.get("confidence") is not None else None,
        rationale=decision.get("rationale"),
        risk_notes=decision.get("risk_notes"),
        policy_version_label=decision.get("policy_version_label") or profile.get("policy_version_label"),
        raw_output=decision,
    )
    order_row = None
    if action in ACTIONABLE_ACTIONS:
        submitted_at = parse_decision_datetime(decision.get("submitted_at"), trade_date)
        effective_at = parse_decision_datetime(decision.get("effective_at"), trade_date)
        order_row = service.create_order(
            run_id=args.run_id,
            profile_key=args.profile_key,
            decision_id=decision_row["id"],
            symbol=str(symbol or ""),
            side=str(side or action),
            requested_quantity=float(quantity),
            order_type=str(order_type or "limit"),
            limit_price=float(limit_price) if limit_price is not None else None,
            submitted_at=submitted_at,
            effective_at=effective_at,
        )
    return {
        "run_id": args.run_id,
        "profile_key": args.profile_key,
        "decision": decision_row,
        "order": order_row,
    }


def command_daily_nav(args: argparse.Namespace) -> Dict[str, Any]:
    trade_date = parse_date(args.trade_date, default=date.today())
    if trade_date is None:
        raise ValueError("trade_date is required")
    return AgentBacktestService().record_daily_nav(run_id=args.run_id, trade_date=trade_date)


def command_evolve_policy(args: argparse.Namespace) -> Dict[str, Any]:
    service = AgentBacktestService()
    payload = load_policy_payload(args)
    effective_from = parse_date(payload.get("effective_from") or args.effective_from)
    policy = service.evolve_policy(
        run_id=args.run_id,
        profile_key=args.profile_key,
        version_label=payload.get("version_label") or args.version_label,
        body_markdown=payload.get("body_markdown") or read_optional_text_file(args.body_file),
        effective_from=effective_from,
        change_reason=payload.get("change_reason") or args.change_reason,
    )
    return {"run_id": args.run_id, "profile_key": args.profile_key, "policy": policy}


def command_fill_order(args: argparse.Namespace) -> Dict[str, Any]:
    trade_date = parse_date(args.trade_date, default=date.today())
    if trade_date is None:
        raise ValueError("trade_date is required")
    filled_at = parse_decision_datetime(args.filled_at, trade_date)
    fill = AgentBacktestService().record_fill(
        run_id=args.run_id,
        order_id=args.order_id,
        quantity=args.quantity,
        price=args.price,
        filled_at=filled_at,
        fee=args.fee,
        tax=args.tax,
        source=args.source,
    )
    return {"run_id": args.run_id, "order_id": args.order_id, "fill": fill}


def load_decision_payload(args: argparse.Namespace) -> Dict[str, Any]:
    if args.decision_json:
        return json.loads(args.decision_json)
    if args.decision_file:
        if args.decision_file == "-":
            return json.loads(sys.stdin.read())
        return json.loads(Path(args.decision_file).read_text(encoding="utf-8"))
    raise AgentBacktestError("decision_json or decision_file is required")


def load_policy_payload(args: argparse.Namespace) -> Dict[str, Any]:
    if args.policy_json:
        return json.loads(args.policy_json)
    if args.policy_file:
        if args.policy_file == "-":
            return json.loads(sys.stdin.read())
        return json.loads(Path(args.policy_file).read_text(encoding="utf-8"))
    return {}


def read_optional_text_file(path: Optional[str]) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8")


def parse_decision_datetime(value: Optional[str], trade_date: date) -> datetime:
    if value:
        return parse_dt_for_trade_date(trade_date, value)
    now = datetime.now()
    if now.date() == trade_date:
        return now.replace(microsecond=0)
    return datetime(trade_date.year, trade_date.month, trade_date.day, 14, 40)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex runner for agent backtest cycles")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Create a run with isolated short/medium/long profiles")
    prepare.add_argument("--name", required=True)
    prepare.add_argument("--market", choices=["cn", "us"], default="cn")
    prepare.add_argument("--symbols", required=True, help="Comma-separated symbols for the selected market")
    prepare.add_argument("--start-date")
    prepare.add_argument("--end-date")
    prepare.add_argument("--initial-cash-per-agent", type=float, default=20000.0)
    prepare.add_argument("--max-observations-per-day", type=int, default=5)
    prepare.set_defaults(func=command_prepare)

    cycle = sub.add_parser("cycle", help="Generate isolated Codex decision contexts for one phase")
    cycle.add_argument("--run-id", type=int, required=True)
    cycle.add_argument("--phase", choices=sorted(PHASE_DEFAULT_TIMES), required=True)
    cycle.add_argument("--trade-date")
    cycle.add_argument("--observation-time")
    cycle.add_argument("--context-dir", default=str(ROOT / ".claude" / "reviews" / "agent_backtest"))
    cycle.add_argument("--live-data", action="store_true", help="Fetch live realtime quotes fail-open")
    cycle.add_argument("--no-record-observation", action="store_true")
    cycle.set_defaults(func=command_cycle)

    apply_decision = sub.add_parser("apply-decision", help="Persist one Codex-produced decision and optional order")
    apply_decision.add_argument("--run-id", type=int, required=True)
    apply_decision.add_argument("--profile-key", required=True)
    apply_decision.add_argument("--trade-date")
    apply_decision.add_argument("--decision-time")
    apply_decision.add_argument("--observation-id", type=int)
    apply_decision.add_argument("--decision-json")
    apply_decision.add_argument("--decision-file")
    apply_decision.set_defaults(func=command_apply_decision)

    daily_nav = sub.add_parser("daily-nav", help="Record daily NAV snapshots")
    daily_nav.add_argument("--run-id", type=int, required=True)
    daily_nav.add_argument("--trade-date")
    daily_nav.set_defaults(func=command_daily_nav)

    evolve_policy = sub.add_parser("evolve-policy", help="Append a forward-only policy version")
    evolve_policy.add_argument("--run-id", type=int, required=True)
    evolve_policy.add_argument("--profile-key", required=True)
    evolve_policy.add_argument("--version-label")
    evolve_policy.add_argument("--body-file")
    evolve_policy.add_argument("--effective-from")
    evolve_policy.add_argument("--change-reason")
    evolve_policy.add_argument("--policy-json")
    evolve_policy.add_argument("--policy-file")
    evolve_policy.set_defaults(func=command_evolve_policy)

    fill_order = sub.add_parser("fill-order", help="Record a simulated fill for an order")
    fill_order.add_argument("--run-id", type=int, required=True)
    fill_order.add_argument("--order-id", type=int, required=True)
    fill_order.add_argument("--quantity", type=float, required=True)
    fill_order.add_argument("--price", type=float, required=True)
    fill_order.add_argument("--trade-date")
    fill_order.add_argument("--filled-at")
    fill_order.add_argument("--fee", type=float)
    fill_order.add_argument("--tax", type=float)
    fill_order.add_argument("--source", default="manual")
    fill_order.set_defaults(func=command_fill_order)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    setup_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.func(args)
    except Exception as exc:
        print_json({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return 1
    print_json({"ok": True, **payload})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
