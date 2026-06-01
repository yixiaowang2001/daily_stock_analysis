# -*- coding: utf-8 -*-
"""Repository helpers for multi-agent paper-trading backtests."""

from __future__ import annotations

from datetime import date
from typing import Any, List, Optional, Tuple

from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import defer

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
)


class AgentBacktestRepository:
    """DB access layer for agent backtest experiments."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def get_run(self, run_id: int) -> Optional[AgentBacktestRun]:
        with self.db.get_session() as session:
            return session.execute(
                select(AgentBacktestRun).where(AgentBacktestRun.id == run_id).limit(1)
            ).scalar_one_or_none()

    def list_runs(
        self,
        *,
        status: Optional[str] = None,
        market: Optional[str] = None,
        limit: int = 50,
    ) -> List[AgentBacktestRun]:
        with self.db.get_session() as session:
            query = select(AgentBacktestRun)
            if status:
                query = query.where(AgentBacktestRun.status == status)
            if market:
                query = query.where(AgentBacktestRun.market == market)
            rows = session.execute(
                query.order_by(desc(AgentBacktestRun.created_at), desc(AgentBacktestRun.id)).limit(limit)
            ).scalars().all()
            return list(rows)

    def get_profile(
        self,
        *,
        run_id: int,
        profile_key: Optional[str] = None,
        profile_id: Optional[int] = None,
        session: Optional[Any] = None,
    ) -> Optional[AgentBacktestProfile]:
        own_session = session is None
        session = session or self.db.get_session()
        try:
            conditions = [AgentBacktestProfile.run_id == run_id]
            if profile_id is not None:
                conditions.append(AgentBacktestProfile.id == profile_id)
            if profile_key is not None:
                conditions.append(AgentBacktestProfile.profile_key == profile_key)
            return session.execute(
                select(AgentBacktestProfile).where(and_(*conditions)).limit(1)
            ).scalar_one_or_none()
        finally:
            if own_session:
                session.close()

    def list_profiles(self, *, run_id: int, session: Optional[Any] = None) -> List[AgentBacktestProfile]:
        own_session = session is None
        session = session or self.db.get_session()
        try:
            rows = session.execute(
                select(AgentBacktestProfile)
                .where(AgentBacktestProfile.run_id == run_id)
                .order_by(AgentBacktestProfile.id.asc())
            ).scalars().all()
            return list(rows)
        finally:
            if own_session:
                session.close()

    def latest_policy(
        self,
        *,
        profile_id: int,
        session: Optional[Any] = None,
    ) -> Optional[AgentBacktestPolicyVersion]:
        own_session = session is None
        session = session or self.db.get_session()
        try:
            return session.execute(
                select(AgentBacktestPolicyVersion)
                .where(AgentBacktestPolicyVersion.profile_id == profile_id)
                .order_by(desc(AgentBacktestPolicyVersion.created_at), desc(AgentBacktestPolicyVersion.id))
                .limit(1)
            ).scalar_one_or_none()
        finally:
            if own_session:
                session.close()

    def list_policies(
        self,
        *,
        profile_id: int,
        session: Optional[Any] = None,
    ) -> List[AgentBacktestPolicyVersion]:
        own_session = session is None
        session = session or self.db.get_session()
        try:
            rows = session.execute(
                select(AgentBacktestPolicyVersion)
                .where(AgentBacktestPolicyVersion.profile_id == profile_id)
                .order_by(desc(AgentBacktestPolicyVersion.created_at), desc(AgentBacktestPolicyVersion.id))
            ).scalars().all()
            return list(rows)
        finally:
            if own_session:
                session.close()

    def count_observations(
        self,
        *,
        profile_id: int,
        trade_date: date,
        session: Optional[Any] = None,
    ) -> int:
        own_session = session is None
        session = session or self.db.get_session()
        try:
            count = session.execute(
                select(func.count(AgentBacktestObservation.id)).where(
                    and_(
                        AgentBacktestObservation.profile_id == profile_id,
                        AgentBacktestObservation.trade_date == trade_date,
                    )
                )
            ).scalar()
            return int(count or 0)
        finally:
            if own_session:
                session.close()

    def get_observation(
        self,
        *,
        run_id: int,
        observation_id: int,
        session: Optional[Any] = None,
    ) -> Optional[AgentBacktestObservation]:
        own_session = session is None
        session = session or self.db.get_session()
        try:
            return session.execute(
                select(AgentBacktestObservation).where(
                    and_(
                        AgentBacktestObservation.run_id == run_id,
                        AgentBacktestObservation.id == observation_id,
                    )
                ).limit(1)
            ).scalar_one_or_none()
        finally:
            if own_session:
                session.close()

    def get_decision(
        self,
        *,
        run_id: int,
        decision_id: int,
        session: Optional[Any] = None,
    ) -> Optional[AgentBacktestDecision]:
        own_session = session is None
        session = session or self.db.get_session()
        try:
            return session.execute(
                select(AgentBacktestDecision).where(
                    and_(
                        AgentBacktestDecision.run_id == run_id,
                        AgentBacktestDecision.id == decision_id,
                    )
                ).limit(1)
            ).scalar_one_or_none()
        finally:
            if own_session:
                session.close()

    def get_order(
        self,
        *,
        run_id: int,
        order_id: int,
        session: Optional[Any] = None,
    ) -> Optional[AgentBacktestOrder]:
        own_session = session is None
        session = session or self.db.get_session()
        try:
            return session.execute(
                select(AgentBacktestOrder).where(
                    and_(
                        AgentBacktestOrder.run_id == run_id,
                        AgentBacktestOrder.id == order_id,
                    )
                ).limit(1)
            ).scalar_one_or_none()
        finally:
            if own_session:
                session.close()

    def filled_quantity_for_order(
        self,
        *,
        order_id: int,
        session: Optional[Any] = None,
    ) -> float:
        own_session = session is None
        session = session or self.db.get_session()
        try:
            qty = session.execute(
                select(func.coalesce(func.sum(AgentBacktestFill.quantity), 0.0)).where(
                    AgentBacktestFill.order_id == order_id
                )
            ).scalar()
            return float(qty or 0.0)
        finally:
            if own_session:
                session.close()

    def list_events(
        self,
        *,
        run_id: int,
        profile_id: Optional[int] = None,
        limit: int = 200,
        include_evidence: bool = True,
        include_raw_output: bool = True,
    ) -> Tuple[
        List[AgentBacktestObservation],
        List[AgentBacktestDecision],
        List[AgentBacktestOrder],
        List[AgentBacktestFill],
        List[AgentBacktestDailyNav],
    ]:
        with self.db.get_session() as session:
            conditions = [AgentBacktestObservation.run_id == run_id]
            if profile_id is not None:
                conditions.append(AgentBacktestObservation.profile_id == profile_id)
            observation_query = select(AgentBacktestObservation)
            if not include_evidence:
                observation_query = observation_query.options(defer(AgentBacktestObservation.evidence_json))
            observations = session.execute(
                observation_query
                .where(and_(*conditions))
                .order_by(desc(AgentBacktestObservation.trade_date), desc(AgentBacktestObservation.id))
                .limit(limit)
            ).scalars().all()

            conditions = [AgentBacktestDecision.run_id == run_id]
            if profile_id is not None:
                conditions.append(AgentBacktestDecision.profile_id == profile_id)
            decision_query = select(AgentBacktestDecision)
            if not include_raw_output:
                decision_query = decision_query.options(defer(AgentBacktestDecision.raw_output))
            decisions = session.execute(
                decision_query
                .where(and_(*conditions))
                .order_by(desc(AgentBacktestDecision.decision_time), desc(AgentBacktestDecision.id))
                .limit(limit)
            ).scalars().all()

            conditions = [AgentBacktestOrder.run_id == run_id]
            if profile_id is not None:
                conditions.append(AgentBacktestOrder.profile_id == profile_id)
            orders = session.execute(
                select(AgentBacktestOrder)
                .where(and_(*conditions))
                .order_by(desc(AgentBacktestOrder.submitted_at), desc(AgentBacktestOrder.id))
                .limit(limit)
            ).scalars().all()

            conditions = [AgentBacktestFill.run_id == run_id]
            if profile_id is not None:
                conditions.append(AgentBacktestFill.profile_id == profile_id)
            fills = session.execute(
                select(AgentBacktestFill)
                .where(and_(*conditions))
                .order_by(desc(AgentBacktestFill.filled_at), desc(AgentBacktestFill.id))
                .limit(limit)
            ).scalars().all()

            conditions = [AgentBacktestDailyNav.run_id == run_id]
            if profile_id is not None:
                conditions.append(AgentBacktestDailyNav.profile_id == profile_id)
            navs = session.execute(
                select(AgentBacktestDailyNav)
                .where(and_(*conditions))
                .order_by(desc(AgentBacktestDailyNav.trade_date), desc(AgentBacktestDailyNav.id))
                .limit(limit)
            ).scalars().all()

            return (list(observations), list(decisions), list(orders), list(fills), list(navs))
