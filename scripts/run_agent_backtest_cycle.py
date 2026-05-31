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
]


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
            live_data=live_data,
        )
        sentiment_context = self._build_sentiment_context(
            realtime=realtime,
            information_context=information_context,
            live_data=live_data,
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
            "data_quality": self._fact_data_quality(
                {
                    "market_data": market_data,
                    "technical_context": technical_context,
                    "fundamental_context": fundamental_context,
                    "information_context": information_context,
                    "sentiment_context": sentiment_context,
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
    def _fetch_fundamental_context(*, symbol: str, live_data: bool) -> Dict[str, Any]:
        if not live_data:
            return {"status": "skipped", "reason": "live_data_disabled"}
        try:
            from src.agent.tools.data_tools import _handle_get_stock_info

            return _handle_get_stock_info(symbol)
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}

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
    def _codex_research_queries(*, symbol: str, stock_name: str) -> List[str]:
        display_name = (stock_name or symbol or "").strip()
        code = (symbol or "").strip()
        is_us_symbol = bool(code.isupper() and not code.isdigit())
        if is_us_symbol:
            display = display_name if display_name and display_name != code else code
            return [
                f"{display} {code} latest stock news earnings guidance SEC filing",
                f"{display} {code} premarket after hours overnight trading news",
                f"{display} {code} analyst rating sector ETF peer performance risk",
            ]
        if display_name == code:
            return [
                f"{code} 最新消息 公告",
                f"{code} 减持 处罚 诉讼 风险",
                f"{code} 业绩预告 财报 机构调研",
            ]
        return [
            f"{display_name} {code} 最新消息 公告",
            f"{display_name} {code} 减持 处罚 诉讼 风险",
            f"{display_name} {code} 业绩预告 财报 机构调研",
        ]

    @staticmethod
    def _build_codex_research_fallback(
        *,
        symbol: str,
        stock_name: str,
        information_context: Dict[str, Any],
        live_data: bool,
    ) -> Dict[str, Any]:
        if not live_data:
            return {
                "status": "disabled",
                "reason": "live_data_disabled",
                "queries": [],
            }

        gap_reason = RunnerContextBuilder._information_context_gap_reason(information_context)
        return {
            "status": "recommended" if gap_reason else "optional",
            "reason": gap_reason or "information_context_available",
            "queries": RunnerContextBuilder._codex_research_queries(
                symbol=symbol,
                stock_name=stock_name,
            ),
            "instructions": [
                "Use Codex available web/search/browser tools only as a supplemental evidence layer.",
                "Respect evidence.data_cutoff_at and do not use later information for intraday decisions.",
                "Prefer official announcements and timestamped reputable financial media.",
                "Cite title/source/date/url when supplemental research is used.",
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
                "- Use Codex web/search tools for these information gaps before deciding. "
                "Respect data_cutoff_at and cite title/date/url when used."
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
