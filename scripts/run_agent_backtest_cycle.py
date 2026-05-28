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
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_provider.base import canonical_stock_code  # noqa: E402
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
ACTIONABLE_ACTIONS = {"buy", "sell"}
REALTIME_QUOTE_TIMEOUT_SECONDS = 8
RESEARCH_HISTORY_DAYS = 260
RECENT_DAILY_BAR_DAYS = 20
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
            symbol: self._symbol_facts(symbol=symbol, live_data=live_data)
            for symbol in research_symbols
        }

        generated: List[Dict[str, Any]] = []
        cutoff = parse_dt_for_trade_date(trade_date, observation_time)
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
                "output_contract": self._output_contract(phase),
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

    def _symbol_facts(self, *, symbol: str, live_data: bool) -> Dict[str, Any]:
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
        realtime: Dict[str, Any]
        if live_data:
            realtime = self._fetch_realtime_quote(symbol)
        else:
            realtime = {"skipped": True, "reason": "live_data_disabled"}
        stock_name = self._resolve_stock_name(symbol, realtime)
        recent_daily = daily[-RECENT_DAILY_BAR_DAYS:]
        market_data = {
            "daily_bars_window": f"latest_{RECENT_DAILY_BAR_DAYS}_of_{RESEARCH_HISTORY_DAYS}_requested",
            "daily_bars_available": len(daily),
            "realtime_quote": realtime,
        }
        technical_context = self._build_technical_context(daily=daily, realtime=realtime)
        fundamental_context = self._fetch_fundamental_context(symbol=symbol, live_data=live_data)
        information_context = self._fetch_information_context(
            symbol=symbol,
            stock_name=stock_name,
            live_data=live_data,
        )
        sentiment_context = self._build_sentiment_context(
            realtime=realtime,
            information_context=information_context,
            live_data=live_data,
        )
        return {
            "schema_version": "agent_backtest_symbol_facts_v2",
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
    def _build_technical_context(*, daily: List[Dict[str, Any]], realtime: Dict[str, Any]) -> Dict[str, Any]:
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
            "source": "stock_daily_cache",
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
    def _fetch_information_context(*, symbol: str, stock_name: str, live_data: bool) -> Dict[str, Any]:
        if not live_data:
            return {"status": "skipped", "reason": "live_data_disabled"}
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
                realtime = (payload or {}).get("realtime_quote", {})
                if isinstance(realtime, dict) and realtime.get("error"):
                    statuses[layer_name] = "partial"
                elif isinstance(realtime, dict) and realtime.get("skipped"):
                    statuses[layer_name] = "partial"
                else:
                    statuses[layer_name] = "ok"
                continue
            if not isinstance(payload, dict):
                statuses[layer_name] = "failed"
                continue
            statuses[layer_name] = str(payload.get("status") or ("ok" if not payload.get("error") else "failed"))
        return {
            "layer_status": statuses,
            "failed_layers": [name for name, status in statuses.items() if status == "failed"],
            "skipped_layers": [name for name, status in statuses.items() if status == "skipped"],
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
    def _fetch_realtime_quote(symbol: str) -> Dict[str, Any]:
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
            completed = subprocess.run(
                [sys.executable, "-c", child_code, str(ROOT), symbol],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=REALTIME_QUOTE_TIMEOUT_SECONDS,
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
                "error": f"Realtime quote fetch timed out after {REALTIME_QUOTE_TIMEOUT_SECONDS}s",
                "source": "realtime_quote",
                "retriable": True,
            }
        except Exception as exc:
            return {"error": str(exc), "source": "realtime_quote", "retriable": True}

    @staticmethod
    def _output_contract(phase: str) -> Dict[str, Any]:
        if phase == "close":
            return RunnerContextBuilder._close_review_contract()
        return RunnerContextBuilder._decision_contract()

    @staticmethod
    def _decision_contract() -> Dict[str, Any]:
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
    def _close_review_contract() -> Dict[str, Any]:
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
            "- no_trade_allowed: true",
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


def command_prepare(args: argparse.Namespace) -> Dict[str, Any]:
    service = AgentBacktestService()
    run = service.create_run(
        name=args.name,
        symbols=parse_symbols(args.symbols),
        start_date=parse_date(args.start_date),
        end_date=parse_date(args.end_date),
        initial_cash_per_agent=args.initial_cash_per_agent,
        max_observations_per_day=args.max_observations_per_day,
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
    observation_time = args.observation_time or PHASE_DEFAULT_TIMES[args.phase]
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
    prepare.add_argument("--symbols", required=True, help="Comma-separated A-share symbols")
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
