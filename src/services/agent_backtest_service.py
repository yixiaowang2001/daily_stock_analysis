# -*- coding: utf-8 -*-
"""Multi-agent paper-trading backtest service."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import and_, select

from data_provider.base import canonical_stock_code
from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name
from src.repositories.agent_backtest_repo import AgentBacktestRepository
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_service import PortfolioService
from src.storage import (
    AgentBacktestDailyNav,
    AgentBacktestDecision,
    AgentBacktestFill,
    AgentBacktestObservation,
    AgentBacktestOrder,
    AgentBacktestPolicyVersion,
    AgentBacktestProfile,
    AgentBacktestRun,
    DatabaseManager,
    PortfolioAccount,
    PortfolioCashLedger,
    PortfolioTrade,
)


DEFAULT_AGENT_PROFILES: List[Dict[str, str]] = [
    {
        "profile_key": "short",
        "display_name": "短线操盘手",
        "style_profile": "short",
        "policy_version_label": "v1.0",
        "policy_markdown": (
            "风格边界：偏短线，关注 1-5 个交易日内的量价、分时强弱、板块热度与风险闸门。"
            "具体选股和交易策略允许基于复盘向前迭代，但不得突破 A 股交易规则或读取其他操盘手上下文。"
        ),
    },
    {
        "profile_key": "medium",
        "display_name": "中线操盘手",
        "style_profile": "medium",
        "policy_version_label": "v1.0",
        "policy_markdown": (
            "风格边界：偏中线，关注 5-30 个交易日内的趋势结构、资金承接、行业轮动和业绩/公告催化。"
            "具体策略可复盘调整，调整只对未来决策生效。"
        ),
    },
    {
        "profile_key": "long",
        "display_name": "长线操盘手",
        "style_profile": "long",
        "policy_version_label": "v1.0",
        "policy_markdown": (
            "风格边界：偏长线，关注基本面、估值、行业景气和周线/月线结构，默认降低换手频率。"
            "使用同一实验股票池，不单独扩展长线池；优先读取 long_horizon_context、fundamental_context "
            "和长周期技术结构。可以观察盘中信息，但不应被短时噪音驱动；当基本估值、长周期结构和信息风险闸门满足时，"
            "允许先用最小交易单位做试仓，并把缺失的业绩/成长/机构/资金流证据写入风险说明。"
        ),
    },
]

DEFAULT_US_AGENT_PROFILES: List[Dict[str, str]] = [
    {
        "profile_key": "short",
        "display_name": "短线操盘手",
        "style_profile": "short",
        "policy_version_label": "v1.0",
        "policy_markdown": (
            "风格边界：偏短线，关注盘前/盘中/盘后/隔夜流动性、事件催化、量价强弱和现金账户风险闸门。"
            "允许做 1-5 个美股交易日内的机会，但必须遵守 IBKR 现金账户风控：只使用 settled cash，"
            "卖出资金按 T+1 美股交易日释放，且每 5 个美股交易日最多 1 次日内回转。"
        ),
    },
    {
        "profile_key": "medium",
        "display_name": "中线操盘手",
        "style_profile": "medium",
        "policy_version_label": "v1.0",
        "policy_markdown": (
            "风格边界：偏中线，关注 5-30 个美股交易日的趋势、财报/指引、行业轮动、ETF/板块相对强弱和宏观利率风险。"
            "默认降低换手，避免把夜盘噪音当成主趋势。所有买入必须满足 settled cash 约束。"
        ),
    },
    {
        "profile_key": "long",
        "display_name": "长线操盘手",
        "style_profile": "long",
        "policy_version_label": "v1.0",
        "policy_markdown": (
            "风格边界：偏长线，关注公司基本面、估值、财报质量、行业竞争格局和周线/月线结构。"
            "可以观察 24 小时交易信息，但交易频率应低，优先避免现金冻结和信息过度反应。"
        ),
    },
]

DEFAULT_RULE_CONFIG: Dict[str, Any] = {
    "market": "cn",
    "lot_size": 100,
    "t_plus_one": True,
    "commission_rate": 0.0003,
    "min_commission": 5.0,
    "stamp_tax_rate": 0.0005,
    "transfer_fee_rate": 0.00001,
    "price_tick": 0.01,
}

DEFAULT_US_RULE_CONFIG: Dict[str, Any] = {
    "market": "us",
    "account_type": "cash",
    "base_currency": "USD",
    "lot_size": 1,
    "allow_fractional_shares": False,
    "cash_settlement": "T+1",
    "settlement_business_days": 1,
    "settlement_timezone": "America/New_York",
    "settlement_refresh_time": "20:00",
    "extended_hours_enabled": True,
    "overnight_trading_enabled": True,
    "overnight_trade_date_note": (
        "IBKR overnight orders are modeled conservatively by trade_date; "
        "cash from sells becomes usable on the next US business day."
    ),
    "day_trade_limit_per_rolling_window": 1,
    "day_trade_window_business_days": 5,
    "commission_rate": 0.0,
    "min_commission": 0.0,
    "stamp_tax_rate": 0.0,
    "transfer_fee_rate": 0.0,
    "sell_sec_fee_rate": 0.0,
    "sell_taf_fee_per_share": 0.0,
    "price_tick": 0.01,
    "data_source_priority": [
        "longbridge",
        "massive",
        "twelvedata",
        "finnhub",
        "alpha_vantage",
        "yfinance",
        "stooq",
    ],
    "information_policy": {
        "codex_research_first": True,
        "provider_search_fallback_enabled": False,
        "provider_search_priority": ["brave", "tavily", "serpapi", "searxng", "bocha", "minimax"],
    },
}

SUPPORTED_MARKETS = {"cn", "us"}
MARKET_BASE_CURRENCY = {"cn": "CNY", "us": "USD"}
DEFAULT_RULE_VERSION = {"cn": "cn_a_v1", "us": "us_cash_ibkr_v1"}
US_SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
VALID_SIDES = {"buy", "sell"}
VALID_ORDER_TYPES = {"limit", "market"}
MAX_OBSERVATIONS_PER_DAY = 5
EPS = 1e-8


class AgentBacktestError(ValueError):
    """Raised when an agent backtest request violates the experiment contract."""


class AgentBacktestService:
    """Create and maintain isolated short/medium/long paper-trading runs."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()
        self.repo = AgentBacktestRepository(self.db)
        self.portfolio_service = PortfolioService(repo=PortfolioRepository(self.db))

    def create_run(
        self,
        *,
        name: str,
        symbols: Iterable[str],
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        initial_cash_per_agent: float = 20000.0,
        max_observations_per_day: int = MAX_OBSERVATIONS_PER_DAY,
        rule_version: Optional[str] = None,
        market: str = "cn",
        config: Optional[Dict[str, Any]] = None,
        profiles: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        name_norm = (name or "").strip()
        if not name_norm:
            raise AgentBacktestError("name is required")
        market_norm = self._normalize_market(market)
        symbols_norm = self._normalize_symbols(symbols, market=market_norm)
        if not symbols_norm:
            raise AgentBacktestError("symbols is required")
        if end_date is not None and start_date is not None and end_date < start_date:
            raise AgentBacktestError("end_date cannot be before start_date")
        if initial_cash_per_agent <= 0:
            raise AgentBacktestError("initial_cash_per_agent must be > 0")
        if max_observations_per_day < 1 or max_observations_per_day > MAX_OBSERVATIONS_PER_DAY:
            raise AgentBacktestError(
                f"max_observations_per_day must be between 1 and {MAX_OBSERVATIONS_PER_DAY}"
            )

        rule_config = self._merged_rule_config(config, market=market_norm)
        profile_inputs = profiles or self._default_profiles(market_norm)
        if not profile_inputs:
            raise AgentBacktestError("profiles is required")

        cash_date = start_date or date.today()
        base_currency = str(rule_config.get("base_currency") or MARKET_BASE_CURRENCY[market_norm]).upper()
        rule_version_norm = (rule_version or DEFAULT_RULE_VERSION[market_norm]).strip() or DEFAULT_RULE_VERSION[market_norm]
        with self.db.session_scope() as session:
            run = AgentBacktestRun(
                name=name_norm,
                status="draft",
                market=market_norm,
                symbols_json=self._json_dumps(symbols_norm),
                start_date=start_date,
                end_date=end_date,
                initial_cash_per_agent=float(initial_cash_per_agent),
                max_observations_per_day=int(max_observations_per_day),
                rule_version=rule_version_norm,
                config_json=self._json_dumps(rule_config),
            )
            session.add(run)
            session.flush()

            seen_keys: set[str] = set()
            for profile_input in profile_inputs:
                profile_key = self._normalize_profile_key(profile_input.get("profile_key"))
                if profile_key in seen_keys:
                    raise AgentBacktestError(f"duplicate profile_key: {profile_key}")
                seen_keys.add(profile_key)

                display_name = (profile_input.get("display_name") or profile_key).strip()
                style_profile = (profile_input.get("style_profile") or profile_key).strip()
                version_label = (profile_input.get("policy_version_label") or "v1.0").strip()
                policy_markdown = (profile_input.get("policy_markdown") or "").strip()
                if not policy_markdown:
                    raise AgentBacktestError(f"policy_markdown is required for {profile_key}")

                account = PortfolioAccount(
                    owner_id=f"agent_backtest_run:{run.id}:{profile_key}",
                    name=f"{name_norm} / {display_name}",
                    broker="agent-backtest",
                    market=market_norm,
                    base_currency=base_currency,
                    is_active=True,
                )
                session.add(account)
                session.flush()

                session.add(
                    PortfolioCashLedger(
                        account_id=account.id,
                        event_date=cash_date,
                        direction="in",
                        amount=float(initial_cash_per_agent),
                        currency=base_currency,
                        note=f"agent_backtest_run:{run.id} initial cash",
                    )
                )

                profile = AgentBacktestProfile(
                    run_id=run.id,
                    account_id=account.id,
                    profile_key=profile_key,
                    display_name=display_name,
                    style_profile=style_profile,
                    policy_version_label=version_label,
                    policy_markdown=policy_markdown,
                    context_namespace=f"agent_backtest:{run.id}:{profile_key}",
                    status="active",
                )
                session.add(profile)
                session.flush()

                session.add(
                    AgentBacktestPolicyVersion(
                        run_id=run.id,
                        profile_id=profile.id,
                        version_label=version_label,
                        body_markdown=policy_markdown,
                        effective_from=start_date,
                        change_reason="initial profile policy",
                        status="active",
                    )
                )

            run_id = int(run.id)

        return self.get_run(run_id)

    def list_runs(
        self,
        *,
        status: Optional[str] = None,
        market: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        market_norm = self._normalize_market(market) if market else None
        rows = self.repo.list_runs(
            status=(status or "").strip() or None,
            market=market_norm,
            limit=max(1, min(int(limit), 200)),
        )
        return {"items": [self._run_row_to_dict(row) for row in rows], "total": len(rows)}

    def get_run(self, run_id: int) -> Dict[str, Any]:
        with self.db.get_session() as session:
            run = session.execute(
                select(AgentBacktestRun).where(AgentBacktestRun.id == run_id).limit(1)
            ).scalar_one_or_none()
            if run is None:
                raise AgentBacktestError(f"agent backtest run not found: {run_id}")
            profiles = session.execute(
                select(AgentBacktestProfile)
                .where(AgentBacktestProfile.run_id == run_id)
                .order_by(AgentBacktestProfile.id.asc())
            ).scalars().all()
            return self._run_row_to_dict(run, profiles=list(profiles), session=session)

    def update_run_settings(
        self,
        *,
        run_id: int,
        symbols: Optional[Iterable[str]] = None,
        max_observations_per_day: Optional[int] = None,
    ) -> Dict[str, Any]:
        if max_observations_per_day is not None and (
            int(max_observations_per_day) < 1
            or int(max_observations_per_day) > MAX_OBSERVATIONS_PER_DAY
        ):
            raise AgentBacktestError(
                f"max_observations_per_day must be between 1 and {MAX_OBSERVATIONS_PER_DAY}"
            )

        with self.db.session_scope() as session:
            run = self._require_run_in_session(session, run_id)
            if symbols is not None:
                symbols_norm = self._normalize_symbols(symbols, market=run.market)
                if not symbols_norm:
                    raise AgentBacktestError("symbols is required")
                run.symbols_json = self._json_dumps(symbols_norm)
            if max_observations_per_day is not None:
                run.max_observations_per_day = int(max_observations_per_day)
            run.updated_at = datetime.now()

        return self.get_run(run_id)

    def add_profile(
        self,
        *,
        run_id: int,
        profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        profile_key = self._normalize_profile_key(profile.get("profile_key"))
        display_name = (profile.get("display_name") or profile_key).strip()
        style_profile = (profile.get("style_profile") or profile_key).strip()
        version_label = (profile.get("policy_version_label") or "v1.0").strip()
        policy_markdown = (profile.get("policy_markdown") or "").strip()
        if not policy_markdown:
            raise AgentBacktestError(f"policy_markdown is required for {profile_key}")

        joined_date = date.today()
        with self.db.session_scope() as session:
            run = self._require_run_in_session(session, run_id)
            existing = self.repo.get_profile(run_id=run_id, profile_key=profile_key, session=session)
            if existing is not None:
                raise AgentBacktestError(f"duplicate profile_key: {profile_key}")

            account = PortfolioAccount(
                owner_id=f"agent_backtest_run:{run.id}:{profile_key}",
                name=f"{run.name} / {display_name}",
                broker="agent-backtest",
                market=run.market,
                base_currency=self._base_currency_for_run(run),
                is_active=True,
            )
            session.add(account)
            session.flush()

            session.add(
                PortfolioCashLedger(
                    account_id=account.id,
                    event_date=joined_date,
                    direction="in",
                    amount=float(run.initial_cash_per_agent),
                    currency=self._base_currency_for_run(run),
                    note=f"agent_backtest_run:{run.id} profile {profile_key} initial cash",
                )
            )

            row = AgentBacktestProfile(
                run_id=run.id,
                account_id=account.id,
                profile_key=profile_key,
                display_name=display_name,
                style_profile=style_profile,
                policy_version_label=version_label,
                policy_markdown=policy_markdown,
                context_namespace=f"agent_backtest:{run.id}:{profile_key}",
                status="active",
            )
            session.add(row)
            session.flush()

            session.add(
                AgentBacktestPolicyVersion(
                    run_id=run.id,
                    profile_id=row.id,
                    version_label=version_label,
                    body_markdown=policy_markdown,
                    effective_from=joined_date,
                    change_reason="added profile",
                    status="active",
                )
            )
            run.updated_at = datetime.now()
            session.flush()
            return self._profile_row_to_dict(row, session=session)

    def deactivate_profile(self, *, run_id: int, profile_key: str) -> Dict[str, Any]:
        with self.db.session_scope() as session:
            run = self._require_run_in_session(session, run_id)
            profile = self.repo.get_profile(
                run_id=run_id,
                profile_key=self._normalize_profile_key(profile_key),
                session=session,
            )
            if profile is None:
                raise AgentBacktestError(f"profile not found: {profile_key}")
            profile.status = "inactive"
            profile.updated_at = datetime.now()
            account = session.execute(
                select(PortfolioAccount).where(PortfolioAccount.id == profile.account_id).limit(1)
            ).scalar_one_or_none()
            if account is not None:
                account.is_active = False
            run.updated_at = datetime.now()

        return self.get_run(run_id)

    def record_observation(
        self,
        *,
        run_id: int,
        profile_key: str,
        trade_date: date,
        observation_time: str,
        data_cutoff_at: datetime,
        symbols: Optional[Iterable[str]] = None,
        evidence: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        observation_time_norm = self._normalize_intraday_time(observation_time)
        with self.db.session_scope() as session:
            run = self._require_run_in_session(session, run_id)
            profile = self._require_profile_in_session(session, run_id, profile_key)
            used = self.repo.count_observations(profile_id=profile.id, trade_date=trade_date, session=session)
            if used >= int(run.max_observations_per_day or 0):
                raise AgentBacktestError(
                    f"{profile.profile_key} exceeded max_observations_per_day={run.max_observations_per_day}"
                )
            symbols_payload = (
                self._normalize_symbols(symbols, market=run.market)
                if symbols is not None
                else self._json_loads(run.symbols_json, [])
            )
            row = AgentBacktestObservation(
                run_id=run.id,
                profile_id=profile.id,
                trade_date=trade_date,
                observation_time=observation_time_norm,
                data_cutoff_at=data_cutoff_at,
                sequence_no=used + 1,
                symbols_json=self._json_dumps(symbols_payload),
                evidence_json=self._json_dumps(evidence or {}),
                summary=(summary or "").strip() or None,
            )
            session.add(row)
            session.flush()
            payload = self._observation_row_to_dict(row)
        return payload

    def record_decision(
        self,
        *,
        run_id: int,
        profile_key: str,
        trade_date: date,
        decision_time: datetime,
        action: str,
        observation_id: Optional[int] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        quantity: Optional[float] = None,
        order_type: Optional[str] = None,
        limit_price: Optional[float] = None,
        confidence: Optional[float] = None,
        rationale: Optional[str] = None,
        risk_notes: Optional[str] = None,
        policy_version_label: Optional[str] = None,
        raw_output: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        action_norm = (action or "").strip().lower()
        if not action_norm:
            raise AgentBacktestError("action is required")
        side_norm = self._normalize_optional_side(side)
        if confidence is not None and not (0 <= float(confidence) <= 1):
            raise AgentBacktestError("confidence must be between 0 and 1")
        if quantity is not None and quantity <= 0:
            raise AgentBacktestError("quantity must be > 0")
        if limit_price is not None and limit_price <= 0:
            raise AgentBacktestError("limit_price must be > 0")

        with self.db.session_scope() as session:
            run = self._require_run_in_session(session, run_id)
            profile = self._require_profile_in_session(session, run_id, profile_key)
            symbol_norm = self._normalize_optional_symbol(symbol, market=run.market)
            self._validate_decision_symbol_scope(
                run=run,
                profile=profile,
                symbol=symbol_norm,
                action=action_norm,
                side=side_norm,
                trade_date=trade_date,
            )
            if observation_id is not None:
                observation = self.repo.get_observation(run_id=run_id, observation_id=observation_id, session=session)
                if observation is None or observation.profile_id != profile.id:
                    raise AgentBacktestError(f"observation not found for profile: {observation_id}")
            row = AgentBacktestDecision(
                run_id=run_id,
                profile_id=profile.id,
                observation_id=observation_id,
                trade_date=trade_date,
                decision_time=decision_time,
                action=action_norm,
                symbol=symbol_norm,
                side=side_norm,
                quantity=float(quantity) if quantity is not None else None,
                order_type=(order_type or "").strip().lower() or None,
                limit_price=float(limit_price) if limit_price is not None else None,
                confidence=float(confidence) if confidence is not None else None,
                rationale=(rationale or "").strip() or None,
                risk_notes=(risk_notes or "").strip() or None,
                policy_version_label=(policy_version_label or profile.policy_version_label or "v1.0").strip(),
                raw_output=self._json_dumps(raw_output or {}),
            )
            session.add(row)
            session.flush()
            payload = self._decision_row_to_dict(row)
        return payload

    def create_order(
        self,
        *,
        run_id: int,
        profile_key: str,
        symbol: str,
        side: str,
        requested_quantity: float,
        order_type: str,
        submitted_at: datetime,
        effective_at: datetime,
        decision_id: Optional[int] = None,
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        side_norm = self._normalize_side(side)
        order_type_norm = (order_type or "limit").strip().lower()
        if order_type_norm not in VALID_ORDER_TYPES:
            raise AgentBacktestError("order_type must be limit or market")
        if effective_at < submitted_at:
            raise AgentBacktestError("effective_at cannot be before submitted_at")
        if order_type_norm == "limit" and (limit_price is None or limit_price <= 0):
            raise AgentBacktestError("limit_price is required for limit orders")
        if limit_price is not None and limit_price <= 0:
            raise AgentBacktestError("limit_price must be > 0")

        with self.db.session_scope() as session:
            run = self._require_run_in_session(session, run_id)
            profile = self._require_profile_in_session(session, run_id, profile_key)
            symbol_norm = self._normalize_required_symbol(symbol, market=run.market)
            self._validate_order_quantity(requested_quantity, rule_config=self._json_loads(run.config_json, {}))
            self._validate_order_symbol_scope(
                run=run,
                profile=profile,
                symbol=symbol_norm,
                side=side_norm,
                trade_date=effective_at.date(),
            )
            if decision_id is not None:
                decision = self.repo.get_decision(run_id=run_id, decision_id=decision_id, session=session)
                if decision is None or decision.profile_id != profile.id:
                    raise AgentBacktestError(f"decision not found for profile: {decision_id}")
            row = AgentBacktestOrder(
                run_id=run_id,
                profile_id=profile.id,
                decision_id=decision_id,
                symbol=symbol_norm,
                side=side_norm,
                order_type=order_type_norm,
                requested_quantity=float(requested_quantity),
                limit_price=float(limit_price) if limit_price is not None else None,
                submitted_at=submitted_at,
                effective_at=effective_at,
                status="pending",
            )
            session.add(row)
            session.flush()
            payload = self._order_row_to_dict(row)
        return payload

    def record_fill(
        self,
        *,
        run_id: int,
        order_id: int,
        quantity: float,
        price: float,
        filled_at: datetime,
        fee: Optional[float] = None,
        tax: Optional[float] = None,
        source: str = "manual",
    ) -> Dict[str, Any]:
        if price <= 0:
            raise AgentBacktestError("price must be > 0")
        trade_date = filled_at.date()

        with self.db.get_session() as session:
            run = session.execute(
                select(AgentBacktestRun).where(AgentBacktestRun.id == run_id).limit(1)
            ).scalar_one_or_none()
            if run is None:
                raise AgentBacktestError(f"agent backtest run not found: {run_id}")
            order = self.repo.get_order(run_id=run_id, order_id=order_id, session=session)
            if order is None:
                raise AgentBacktestError(f"order not found: {order_id}")
            profile = self.repo.get_profile(run_id=run_id, profile_id=order.profile_id, session=session)
            if profile is None:
                raise AgentBacktestError(f"profile not found for order: {order_id}")
            rule_config = self._json_loads(run.config_json, {})
            self._validate_order_quantity(quantity, rule_config=rule_config)
            filled_qty = self.repo.filled_quantity_for_order(order_id=order_id, session=session)
            account_id = int(profile.account_id)
            order_snapshot = self._order_row_to_dict(order)

        if filled_qty + float(quantity) > float(order_snapshot["requested_quantity"]) + EPS:
            raise AgentBacktestError("fill quantity exceeds remaining order quantity")

        fee_value, tax_value = self._resolve_trade_costs(
            side=order_snapshot["side"],
            quantity=float(quantity),
            price=float(price),
            fee=fee,
            tax=tax,
            rule_config=rule_config,
        )
        self._validate_fill_against_market_rules(
            account_id=account_id,
            side=order_snapshot["side"],
            symbol=order_snapshot["symbol"],
            quantity=float(quantity),
            price=float(price),
            fee=fee_value,
            tax=tax_value,
            trade_date=trade_date,
            market=str(rule_config.get("market") or "cn"),
            rule_config=rule_config,
        )

        with self.db.session_scope() as session:
            order = self.repo.get_order(run_id=run_id, order_id=order_id, session=session)
            if order is None:
                raise AgentBacktestError(f"order not found: {order_id}")
            profile = self.repo.get_profile(run_id=run_id, profile_id=order.profile_id, session=session)
            if profile is None:
                raise AgentBacktestError(f"profile not found for order: {order_id}")
            filled_qty_now = self.repo.filled_quantity_for_order(order_id=order_id, session=session)
            if filled_qty_now + float(quantity) > float(order.requested_quantity) + EPS:
                raise AgentBacktestError("fill quantity exceeds remaining order quantity")

            fill = AgentBacktestFill(
                run_id=run_id,
                profile_id=profile.id,
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                quantity=float(quantity),
                price=float(price),
                fee=fee_value,
                tax=tax_value,
                filled_at=filled_at,
                source=(source or "manual").strip() or "manual",
            )
            session.add(fill)
            session.flush()

            trade = PortfolioTrade(
                account_id=profile.account_id,
                trade_uid=f"agentbt:{order.id}:{fill.id}",
                symbol=order.symbol,
                market=str(rule_config.get("market") or "cn"),
                currency=self._base_currency_from_config(rule_config),
                trade_date=trade_date,
                side=order.side,
                quantity=float(quantity),
                price=float(price),
                fee=fee_value,
                tax=tax_value,
                note=f"agent_backtest_run:{run_id} order:{order.id}",
                dedup_hash=f"agentbt:{order.id}:{fill.id}",
            )
            session.add(trade)
            session.flush()
            fill.portfolio_trade_id = trade.id

            total_filled = filled_qty_now + float(quantity)
            order.status = "filled" if math.isclose(total_filled, float(order.requested_quantity)) else "partially_filled"
            order.updated_at = datetime.now()
            payload = self._fill_row_to_dict(fill)
            payload["portfolio_trade_id"] = int(trade.id)
        return payload

    def evolve_policy(
        self,
        *,
        run_id: int,
        profile_key: str,
        version_label: str,
        body_markdown: str,
        effective_from: Optional[date] = None,
        change_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        label = (version_label or "").strip()
        body = (body_markdown or "").strip()
        if not label:
            raise AgentBacktestError("version_label is required")
        if not body:
            raise AgentBacktestError("body_markdown is required")

        with self.db.session_scope() as session:
            self._require_run_in_session(session, run_id)
            profile = self._require_profile_in_session(session, run_id, profile_key)
            parent = self.repo.latest_policy(profile_id=profile.id, session=session)
            row = AgentBacktestPolicyVersion(
                run_id=run_id,
                profile_id=profile.id,
                version_label=label,
                body_markdown=body,
                parent_policy_id=parent.id if parent is not None else None,
                effective_from=effective_from,
                change_reason=(change_reason or "").strip() or None,
                status="active",
            )
            profile.policy_version_label = label
            profile.policy_markdown = body
            profile.updated_at = datetime.now()
            session.add(row)
            session.flush()
            payload = self._policy_row_to_dict(row)
        return payload

    def list_policies(self, *, run_id: int, profile_key: str) -> Dict[str, Any]:
        with self.db.session_scope() as session:
            self._require_run_in_session(session, run_id)
            profile = self._require_profile_in_session(session, run_id, profile_key)
            rows = self.repo.list_policies(profile_id=profile.id, session=session)
            items = [self._policy_row_to_dict(row) for row in rows]
        return {"items": items, "total": len(items)}

    def record_daily_nav(
        self,
        *,
        run_id: int,
        trade_date: date,
        price_overrides: Optional[Dict[str, float]] = None,
        price_override_sources: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        with self.db.get_session() as session:
            run = session.execute(
                select(AgentBacktestRun).where(AgentBacktestRun.id == run_id).limit(1)
            ).scalar_one_or_none()
            if run is None:
                raise AgentBacktestError(f"agent backtest run not found: {run_id}")
            profiles = self.repo.list_profiles(run_id=run_id, session=session)
            profile_payloads = [
                self._profile_row_to_dict(profile, session=session)
                for profile in profiles
                if profile.status == "active"
            ]

        items: List[Dict[str, Any]] = []
        for profile in profile_payloads:
            snapshot = self.portfolio_service.get_portfolio_snapshot(
                account_id=profile["account_id"],
                as_of=trade_date,
                cost_method="fifo",
                price_overrides=price_overrides,
                price_override_sources=price_override_sources,
            )
            account_snapshot = (snapshot.get("accounts") or [{}])[0]
            items.append(
                {
                    "profile_id": profile["id"],
                    "account_id": profile["account_id"],
                    "profile_key": profile["profile_key"],
                    "cash": float(account_snapshot.get("total_cash") or 0.0),
                    "market_value": float(account_snapshot.get("total_market_value") or 0.0),
                    "total_equity": float(account_snapshot.get("total_equity") or 0.0),
                    "realized_pnl": float(account_snapshot.get("realized_pnl") or 0.0),
                    "unrealized_pnl": float(account_snapshot.get("unrealized_pnl") or 0.0),
                    "valuation_stale": bool(account_snapshot.get("valuation_stale")),
                    "payload": account_snapshot,
                }
            )

        with self.db.session_scope() as session:
            rows: List[Dict[str, Any]] = []
            for item in items:
                existing = session.execute(
                    select(AgentBacktestDailyNav)
                    .where(
                        and_(
                            AgentBacktestDailyNav.profile_id == item["profile_id"],
                            AgentBacktestDailyNav.trade_date == trade_date,
                        )
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if existing is None:
                    existing = AgentBacktestDailyNav(
                        run_id=run_id,
                        profile_id=item["profile_id"],
                        trade_date=trade_date,
                    )
                    session.add(existing)
                existing.cash = item["cash"]
                existing.market_value = item["market_value"]
                existing.total_equity = item["total_equity"]
                existing.realized_pnl = item["realized_pnl"]
                existing.unrealized_pnl = item["unrealized_pnl"]
                existing.payload_json = self._json_dumps(item["payload"])
                existing.updated_at = datetime.now()
                session.flush()
                rows.append(self._nav_row_to_dict(existing))

        return {"run_id": run_id, "trade_date": trade_date.isoformat(), "items": rows}

    def list_events(
        self,
        *,
        run_id: int,
        profile_key: Optional[str] = None,
        limit: int = 200,
        include_evidence: bool = True,
        include_raw_output: bool = True,
    ) -> Dict[str, Any]:
        profile_id: Optional[int] = None
        if profile_key:
            with self.db.get_session() as session:
                profile = self.repo.get_profile(run_id=run_id, profile_key=profile_key, session=session)
                if profile is None:
                    raise AgentBacktestError(f"profile not found: {profile_key}")
                profile_id = int(profile.id)

        observations, decisions, orders, fills, navs = self.repo.list_events(
            run_id=run_id,
            profile_id=profile_id,
            limit=max(1, min(int(limit), 500)),
            include_evidence=include_evidence,
            include_raw_output=include_raw_output,
        )
        return {
            "observations": [
                self._observation_row_to_dict(row, include_evidence=include_evidence)
                for row in observations
            ],
            "decisions": [
                self._decision_row_to_dict(row, include_raw_output=include_raw_output)
                for row in decisions
            ],
            "orders": [self._order_row_to_dict(row) for row in orders],
            "fills": [self._fill_row_to_dict(row) for row in fills],
            "daily_nav": [self._nav_row_to_dict(row) for row in navs],
        }

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _default_profiles(market: str) -> List[Dict[str, str]]:
        return DEFAULT_US_AGENT_PROFILES if market == "us" else DEFAULT_AGENT_PROFILES

    @staticmethod
    def _normalize_market(market: str) -> str:
        market_norm = (market or "cn").strip().lower()
        if market_norm not in SUPPORTED_MARKETS:
            raise AgentBacktestError(f"market must be one of {sorted(SUPPORTED_MARKETS)}")
        return market_norm

    @staticmethod
    def _normalize_symbols(symbols: Iterable[str], *, market: str = "cn") -> List[str]:
        result: List[str] = []
        for raw in symbols:
            raw_text = str(raw or "").strip()
            if not raw_text:
                continue
            code = AgentBacktestService._normalize_required_symbol(raw_text, market=market)
            if not code:
                continue
            if code not in result:
                result.append(code)
        return result

    @staticmethod
    def _normalize_profile_key(value: Any) -> str:
        key = str(value or "").strip().lower()
        if not key:
            raise AgentBacktestError("profile_key is required")
        if not key.replace("_", "").replace("-", "").isalnum():
            raise AgentBacktestError(f"invalid profile_key: {key}")
        return key

    @staticmethod
    def _normalize_intraday_time(value: str) -> str:
        text = (value or "").strip()
        parts = text.split(":")
        if len(parts) < 2:
            raise AgentBacktestError("observation_time must be HH:MM or HH:MM:SS")
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            second = int(parts[2]) if len(parts) > 2 else 0
        except ValueError as exc:
            raise AgentBacktestError("invalid observation_time") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            raise AgentBacktestError("invalid observation_time")
        return f"{hour:02d}:{minute:02d}:{second:02d}"

    @staticmethod
    def _normalize_optional_symbol(symbol: Optional[str], *, market: str = "cn") -> Optional[str]:
        if symbol is None or not str(symbol).strip():
            return None
        return AgentBacktestService._normalize_required_symbol(symbol, market=market)

    @staticmethod
    def _normalize_required_symbol(symbol: str, *, market: str = "cn") -> str:
        code = canonical_stock_code(str(symbol or "").strip())
        market_norm = AgentBacktestService._normalize_market(market)
        if market_norm == "cn":
            if not (code and code.isdigit() and len(code) == 6):
                raise AgentBacktestError(f"only 6-digit A-share symbols are supported: {symbol}")
            return code
        if not (code and US_SYMBOL_PATTERN.match(code)):
            raise AgentBacktestError(f"only US stock tickers are supported for US runs: {symbol}")
        return code

    @staticmethod
    def _normalize_side(side: str) -> str:
        side_norm = (side or "").strip().lower()
        if side_norm not in VALID_SIDES:
            raise AgentBacktestError("side must be buy or sell")
        return side_norm

    @staticmethod
    def _normalize_optional_side(side: Optional[str]) -> Optional[str]:
        if side is None or not str(side).strip():
            return None
        return AgentBacktestService._normalize_side(side)

    @staticmethod
    def _validate_order_quantity(quantity: float, *, rule_config: Dict[str, Any]) -> None:
        if quantity <= 0:
            raise AgentBacktestError("quantity must be > 0")
        market = str(rule_config.get("market") or "cn").lower()
        if market == "us" and not bool(rule_config.get("allow_fractional_shares", False)):
            if abs(float(quantity) - round(float(quantity))) > EPS:
                raise AgentBacktestError("US cash-account simulation requires whole-share quantity")
            return
        lot_size = float(rule_config.get("lot_size") or DEFAULT_RULE_CONFIG["lot_size"])
        if lot_size > 1 and abs(float(quantity) % lot_size) > EPS:
            raise AgentBacktestError(f"order quantity must be a multiple of {int(lot_size)}")

    def _validate_decision_symbol_scope(
        self,
        *,
        run: AgentBacktestRun,
        profile: AgentBacktestProfile,
        symbol: Optional[str],
        action: str,
        side: Optional[str],
        trade_date: date,
    ) -> None:
        if symbol is None:
            return
        if symbol in self._json_loads(run.symbols_json, []):
            return
        effective_side = side or (action if action in VALID_SIDES else None)
        if effective_side == "buy":
            raise AgentBacktestError(f"decision symbol is outside run symbols and cannot be bought: {symbol}")
        if not self._has_open_position(account_id=int(profile.account_id), symbol=symbol, as_of=trade_date):
            raise AgentBacktestError(f"decision symbol is outside run symbols and not currently held: {symbol}")

    def _validate_order_symbol_scope(
        self,
        *,
        run: AgentBacktestRun,
        profile: AgentBacktestProfile,
        symbol: str,
        side: str,
        trade_date: date,
    ) -> None:
        if symbol in self._json_loads(run.symbols_json, []):
            return
        if side == "buy":
            raise AgentBacktestError(f"order symbol is outside run symbols and cannot be bought: {symbol}")
        if not self._has_open_position(account_id=int(profile.account_id), symbol=symbol, as_of=trade_date):
            raise AgentBacktestError(f"order symbol is outside run symbols and not currently held: {symbol}")

    def _has_open_position(self, *, account_id: int, symbol: str, as_of: date) -> bool:
        snapshot = self.portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of,
            cost_method="fifo",
        )
        account = (snapshot.get("accounts") or [{}])[0]
        for position in account.get("positions") or []:
            if position.get("symbol") == symbol and float(position.get("quantity") or 0.0) > EPS:
                return True
        return False

    def _validate_fill_against_market_rules(
        self,
        *,
        account_id: int,
        side: str,
        symbol: str,
        quantity: float,
        price: float,
        fee: float,
        tax: float,
        trade_date: date,
        market: str,
        rule_config: Dict[str, Any],
    ) -> None:
        if market == "us":
            self._validate_fill_against_us_cash_rules(
                account_id=account_id,
                side=side,
                symbol=symbol,
                quantity=quantity,
                price=price,
                fee=fee,
                tax=tax,
                trade_date=trade_date,
                rule_config=rule_config,
            )
            return
        self._validate_fill_against_a_share_rules(
            account_id=account_id,
            side=side,
            symbol=symbol,
            quantity=quantity,
            price=price,
            fee=fee,
            tax=tax,
            trade_date=trade_date,
        )

    def _validate_fill_against_a_share_rules(
        self,
        *,
        account_id: int,
        side: str,
        symbol: str,
        quantity: float,
        price: float,
        fee: float,
        tax: float,
        trade_date: date,
    ) -> None:
        amount = quantity * price + fee + tax
        if side == "buy":
            snapshot = self.portfolio_service.get_portfolio_snapshot(
                account_id=account_id,
                as_of=trade_date,
                cost_method="fifo",
            )
            account = (snapshot.get("accounts") or [{}])[0]
            cash = float(account.get("total_cash") or 0.0)
            if cash + EPS < amount:
                raise AgentBacktestError("insufficient cash for fill")
            return

        previous_snapshot = self.portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=trade_date - timedelta(days=1),
            cost_method="fifo",
        )
        account = (previous_snapshot.get("accounts") or [{}])[0]
        available = 0.0
        for pos in account.get("positions") or []:
            if pos.get("symbol") == symbol:
                available = float(pos.get("quantity") or 0.0)
                break
        if available + EPS < quantity:
            raise AgentBacktestError("A-share T+1 rule: sell quantity exceeds previous-day available shares")

    def _validate_fill_against_us_cash_rules(
        self,
        *,
        account_id: int,
        side: str,
        symbol: str,
        quantity: float,
        price: float,
        fee: float,
        tax: float,
        trade_date: date,
        rule_config: Dict[str, Any],
    ) -> None:
        if side == "buy":
            required_cash = quantity * price + fee + tax
            settled_cash = self._available_us_settled_cash(account_id=account_id, as_of=trade_date)
            if settled_cash + EPS < required_cash:
                raise AgentBacktestError(
                    "US cash-account T+1 settlement rule: insufficient settled cash "
                    f"for fill (settled_cash={round(settled_cash, 6)}, required={round(required_cash, 6)})"
                )
            return

        snapshot = self.portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=trade_date,
            cost_method="fifo",
        )
        account = (snapshot.get("accounts") or [{}])[0]
        available = 0.0
        for pos in account.get("positions") or []:
            if pos.get("symbol") == symbol:
                available = float(pos.get("quantity") or 0.0)
                break
        if available + EPS < quantity:
            raise AgentBacktestError("US cash-account rule: sell quantity exceeds current available shares")

        if self._would_create_us_day_trade(
            account_id=account_id,
            symbol=symbol,
            trade_date=trade_date,
        ):
            limit = int(rule_config.get("day_trade_limit_per_rolling_window", 1) or 1)
            window_days = int(rule_config.get("day_trade_window_business_days", 5) or 5)
            used = self._count_us_day_trades(
                account_id=account_id,
                through=trade_date,
                window_business_days=window_days,
            )
            if used >= limit:
                raise AgentBacktestError(
                    "US cash-account day-trade guard: weekly day-trade allowance already used "
                    f"(limit={limit}, used={used}, window_business_days={window_days})"
                )

    def _available_us_settled_cash(self, *, account_id: int, as_of: date) -> float:
        with self.db.get_session() as session:
            ledger_rows = session.execute(
                select(PortfolioCashLedger)
                .where(
                    and_(
                        PortfolioCashLedger.account_id == account_id,
                        PortfolioCashLedger.event_date <= as_of,
                    )
                )
                .order_by(PortfolioCashLedger.event_date.asc(), PortfolioCashLedger.id.asc())
            ).scalars().all()
            trade_rows = session.execute(
                select(PortfolioTrade)
                .where(
                    and_(
                        PortfolioTrade.account_id == account_id,
                        PortfolioTrade.trade_date <= as_of,
                    )
                )
                .order_by(PortfolioTrade.trade_date.asc(), PortfolioTrade.id.asc())
            ).scalars().all()

        cash = 0.0
        for row in ledger_rows:
            amount = float(row.amount or 0.0)
            direction = (row.direction or "").strip().lower()
            if direction == "in":
                cash += amount
            elif direction == "out":
                cash -= amount

        for row in trade_rows:
            gross = float(row.quantity or 0.0) * float(row.price or 0.0)
            fee = float(row.fee or 0.0)
            tax = float(row.tax or 0.0)
            side = (row.side or "").strip().lower()
            if side == "buy":
                cash -= gross + fee + tax
            elif side == "sell" and self._us_settlement_date(row.trade_date) <= as_of:
                cash += gross - fee - tax
        return cash

    def _would_create_us_day_trade(self, *, account_id: int, symbol: str, trade_date: date) -> bool:
        with self.db.get_session() as session:
            buy_exists = session.execute(
                select(PortfolioTrade.id)
                .where(
                    and_(
                        PortfolioTrade.account_id == account_id,
                        PortfolioTrade.symbol == symbol,
                        PortfolioTrade.trade_date == trade_date,
                        PortfolioTrade.side == "buy",
                    )
                )
                .limit(1)
            ).scalar_one_or_none()
        return buy_exists is not None

    def _count_us_day_trades(
        self,
        *,
        account_id: int,
        through: date,
        window_business_days: int,
    ) -> int:
        start = self._subtract_us_business_days(through, max(0, window_business_days - 1))
        with self.db.get_session() as session:
            rows = session.execute(
                select(PortfolioTrade.symbol, PortfolioTrade.trade_date, PortfolioTrade.side)
                .where(
                    and_(
                        PortfolioTrade.account_id == account_id,
                        PortfolioTrade.trade_date >= start,
                        PortfolioTrade.trade_date <= through,
                    )
                )
            ).all()
        sides_by_key: Dict[tuple[str, date], set[str]] = {}
        for symbol, trade_dt, side in rows:
            key = (str(symbol), trade_dt)
            sides_by_key.setdefault(key, set()).add(str(side).lower())
        return sum(1 for sides in sides_by_key.values() if {"buy", "sell"}.issubset(sides))

    @staticmethod
    def _us_settlement_date(trade_date: date) -> date:
        current = trade_date
        added = 0
        while added < 1:
            current += timedelta(days=1)
            if current.weekday() < 5:
                added += 1
        return current

    @staticmethod
    def _subtract_us_business_days(value: date, days: int) -> date:
        current = value
        remaining = days
        while remaining > 0:
            current -= timedelta(days=1)
            if current.weekday() < 5:
                remaining -= 1
        return current

    @staticmethod
    def _merged_rule_config(config: Optional[Dict[str, Any]], *, market: str = "cn") -> Dict[str, Any]:
        market_norm = AgentBacktestService._normalize_market(market)
        merged = deepcopy(DEFAULT_US_RULE_CONFIG if market_norm == "us" else DEFAULT_RULE_CONFIG)
        if config:
            merged.update(config)
        merged["market"] = market_norm
        merged["base_currency"] = str(
            merged.get("base_currency") or MARKET_BASE_CURRENCY[market_norm]
        ).upper()
        return merged

    @staticmethod
    def _base_currency_from_config(rule_config: Dict[str, Any]) -> str:
        market = str(rule_config.get("market") or "cn").lower()
        return str(rule_config.get("base_currency") or MARKET_BASE_CURRENCY.get(market, "CNY")).upper()

    def _base_currency_for_run(self, run: AgentBacktestRun) -> str:
        return self._base_currency_from_config(self._json_loads(run.config_json, {}))

    @staticmethod
    def _resolve_trade_costs(
        *,
        side: str,
        quantity: float,
        price: float,
        fee: Optional[float],
        tax: Optional[float],
        rule_config: Dict[str, Any],
    ) -> tuple[float, float]:
        amount = float(quantity) * float(price)
        if fee is None:
            commission_rate = float(rule_config.get("commission_rate", DEFAULT_RULE_CONFIG["commission_rate"]))
            min_commission = float(rule_config.get("min_commission", DEFAULT_RULE_CONFIG["min_commission"]))
            transfer_rate = float(rule_config.get("transfer_fee_rate", DEFAULT_RULE_CONFIG["transfer_fee_rate"]))
            fee_value = max(amount * commission_rate, min_commission) + amount * transfer_rate
        else:
            fee_value = float(fee)
        if tax is None:
            tax_rate = float(rule_config.get("stamp_tax_rate", DEFAULT_RULE_CONFIG["stamp_tax_rate"]))
            tax_value = amount * tax_rate if side == "sell" else 0.0
        else:
            tax_value = float(tax)
        if side == "sell":
            fee_value += amount * float(rule_config.get("sell_sec_fee_rate", 0.0) or 0.0)
            fee_value += float(quantity) * float(rule_config.get("sell_taf_fee_per_share", 0.0) or 0.0)
        if fee_value < 0 or tax_value < 0:
            raise AgentBacktestError("fee and tax must be >= 0")
        return (round(fee_value, 6), round(tax_value, 6))

    def _require_run_in_session(self, session: Any, run_id: int) -> AgentBacktestRun:
        run = session.execute(
            select(AgentBacktestRun).where(AgentBacktestRun.id == run_id).limit(1)
        ).scalar_one_or_none()
        if run is None:
            raise AgentBacktestError(f"agent backtest run not found: {run_id}")
        return run

    def _require_profile_in_session(self, session: Any, run_id: int, profile_key: str) -> AgentBacktestProfile:
        key = self._normalize_profile_key(profile_key)
        profile = self.repo.get_profile(run_id=run_id, profile_key=key, session=session)
        if profile is None:
            raise AgentBacktestError(f"profile not found: {key}")
        if profile.status != "active":
            raise AgentBacktestError(f"profile inactive: {key}")
        return profile

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------
    def _run_row_to_dict(
        self,
        row: AgentBacktestRun,
        *,
        profiles: Optional[List[AgentBacktestProfile]] = None,
        session: Optional[Any] = None,
    ) -> Dict[str, Any]:
        symbols = self._json_loads(row.symbols_json, [])
        config = self._json_loads(row.config_json, {})
        configured_symbol_names = config.get("symbol_names") if isinstance(config, dict) else None
        payload = {
            "id": int(row.id),
            "name": row.name,
            "status": row.status,
            "market": row.market,
            "symbols": symbols,
            "symbol_names": self._resolve_symbol_names(symbols, overrides=configured_symbol_names),
            "start_date": row.start_date.isoformat() if row.start_date else None,
            "end_date": row.end_date.isoformat() if row.end_date else None,
            "initial_cash_per_agent": float(row.initial_cash_per_agent or 0.0),
            "max_observations_per_day": int(row.max_observations_per_day or 0),
            "rule_version": row.rule_version,
            "config": config,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        if profiles is not None:
            payload["profiles"] = [
                self._profile_row_to_dict(profile, session=session)
                for profile in profiles
                if profile.status == "active"
            ]
        return payload

    @staticmethod
    def _resolve_symbol_names(
        symbols: Iterable[str],
        *,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        names: Dict[str, str] = {}
        override_map = {
            str(key or "").strip().upper(): str(value or "").strip()
            for key, value in (overrides or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }
        for raw_symbol in symbols:
            symbol = str(raw_symbol or "").strip().upper()
            if not symbol:
                continue
            lookup_code = canonical_stock_code(symbol)
            name = (
                override_map.get(symbol)
                or override_map.get(lookup_code)
                or STOCK_NAME_MAP.get(lookup_code)
                or get_index_stock_name(lookup_code)
            )
            names[symbol] = str(name).strip() if is_meaningful_stock_name(name, lookup_code) else ""
        return names

    def _profile_row_to_dict(self, row: AgentBacktestProfile, *, session: Optional[Any] = None) -> Dict[str, Any]:
        latest_policy = self.repo.latest_policy(profile_id=row.id, session=session) if session is not None else None
        return {
            "id": int(row.id),
            "run_id": int(row.run_id),
            "account_id": int(row.account_id),
            "profile_key": row.profile_key,
            "display_name": row.display_name,
            "style_profile": row.style_profile,
            "policy_version_label": row.policy_version_label,
            "policy_markdown": row.policy_markdown,
            "latest_policy_id": int(latest_policy.id) if latest_policy is not None else None,
            "context_namespace": row.context_namespace,
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _policy_row_to_dict(row: AgentBacktestPolicyVersion) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "run_id": int(row.run_id),
            "profile_id": int(row.profile_id),
            "version_label": row.version_label,
            "body_markdown": row.body_markdown,
            "parent_policy_id": int(row.parent_policy_id) if row.parent_policy_id else None,
            "effective_from": row.effective_from.isoformat() if row.effective_from else None,
            "change_reason": row.change_reason,
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _observation_row_to_dict(
        row: AgentBacktestObservation,
        *,
        include_evidence: bool = True,
    ) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "run_id": int(row.run_id),
            "profile_id": int(row.profile_id),
            "trade_date": row.trade_date.isoformat() if row.trade_date else None,
            "observation_time": row.observation_time,
            "data_cutoff_at": row.data_cutoff_at.isoformat() if row.data_cutoff_at else None,
            "sequence_no": int(row.sequence_no),
            "symbols": AgentBacktestService._json_loads(row.symbols_json, []),
            "evidence": AgentBacktestService._json_loads(row.evidence_json, {}) if include_evidence else {},
            "summary": row.summary,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _decision_row_to_dict(
        row: AgentBacktestDecision,
        *,
        include_raw_output: bool = True,
    ) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "run_id": int(row.run_id),
            "profile_id": int(row.profile_id),
            "observation_id": int(row.observation_id) if row.observation_id else None,
            "trade_date": row.trade_date.isoformat() if row.trade_date else None,
            "decision_time": row.decision_time.isoformat() if row.decision_time else None,
            "action": row.action,
            "symbol": row.symbol,
            "side": row.side,
            "quantity": float(row.quantity) if row.quantity is not None else None,
            "order_type": row.order_type,
            "limit_price": float(row.limit_price) if row.limit_price is not None else None,
            "confidence": float(row.confidence) if row.confidence is not None else None,
            "rationale": row.rationale,
            "risk_notes": row.risk_notes,
            "policy_version_label": row.policy_version_label,
            "raw_output": AgentBacktestService._json_loads(row.raw_output, {}) if include_raw_output else {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _order_row_to_dict(row: AgentBacktestOrder) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "run_id": int(row.run_id),
            "profile_id": int(row.profile_id),
            "decision_id": int(row.decision_id) if row.decision_id else None,
            "symbol": row.symbol,
            "side": row.side,
            "order_type": row.order_type,
            "requested_quantity": float(row.requested_quantity),
            "limit_price": float(row.limit_price) if row.limit_price is not None else None,
            "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
            "effective_at": row.effective_at.isoformat() if row.effective_at else None,
            "status": row.status,
            "reject_reason": row.reject_reason,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _fill_row_to_dict(row: AgentBacktestFill) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "run_id": int(row.run_id),
            "profile_id": int(row.profile_id),
            "order_id": int(row.order_id),
            "portfolio_trade_id": int(row.portfolio_trade_id) if row.portfolio_trade_id else None,
            "symbol": row.symbol,
            "side": row.side,
            "quantity": float(row.quantity),
            "price": float(row.price),
            "fee": float(row.fee),
            "tax": float(row.tax),
            "filled_at": row.filled_at.isoformat() if row.filled_at else None,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _nav_row_to_dict(row: AgentBacktestDailyNav) -> Dict[str, Any]:
        payload = AgentBacktestService._json_loads(row.payload_json, {})
        data = {
            "id": int(row.id),
            "run_id": int(row.run_id),
            "profile_id": int(row.profile_id),
            "trade_date": row.trade_date.isoformat() if row.trade_date else None,
            "cash": float(row.cash),
            "market_value": float(row.market_value),
            "total_equity": float(row.total_equity),
            "realized_pnl": float(row.realized_pnl),
            "unrealized_pnl": float(row.unrealized_pnl),
            "payload": payload,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        data["valuation_stale"] = bool(payload.get("valuation_stale"))
        return data

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _json_loads(value: Optional[str], default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except Exception:
            return default
