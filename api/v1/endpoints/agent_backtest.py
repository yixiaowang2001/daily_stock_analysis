# -*- coding: utf-8 -*-
"""Agent backtest endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_database_manager
from api.v1.schemas.agent_backtest import (
    AgentBacktestDailyNavRequest,
    AgentBacktestDailyNavResponse,
    AgentBacktestDecisionCreateRequest,
    AgentBacktestDecisionItem,
    AgentBacktestEventsResponse,
    AgentBacktestFillCreateRequest,
    AgentBacktestFillItem,
    AgentBacktestObservationCreateRequest,
    AgentBacktestObservationItem,
    AgentBacktestOrderCreateRequest,
    AgentBacktestOrderItem,
    AgentBacktestProfileCreateRequest,
    AgentBacktestPolicyCreateRequest,
    AgentBacktestPolicyItem,
    AgentBacktestPolicyListResponse,
    AgentBacktestProfileItem,
    AgentBacktestRunCreateRequest,
    AgentBacktestRunItem,
    AgentBacktestRunListResponse,
    AgentBacktestRunUpdateRequest,
)
from api.v1.schemas.common import ErrorResponse
from src.services.agent_backtest_service import AgentBacktestError, AgentBacktestService
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter()


def _service(db_manager: DatabaseManager) -> AgentBacktestService:
    return AgentBacktestService(db_manager)


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": "validation_error", "message": str(exc)},
    )


def _not_found_or_bad_request(exc: AgentBacktestError) -> HTTPException:
    text = str(exc)
    if "not found" in text:
        return HTTPException(status_code=404, detail={"error": "not_found", "message": text})
    return _bad_request(exc)


def _internal_error(message: str, exc: Exception) -> HTTPException:
    logger.error("%s: %s", message, exc, exc_info=True)
    return HTTPException(
        status_code=500,
        detail={"error": "internal_error", "message": f"{message}: {str(exc)}"},
    )


@router.post(
    "/runs",
    response_model=AgentBacktestRunItem,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Create multi-agent paper-trading run",
)
def create_run(
    request: AgentBacktestRunCreateRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestRunItem:
    try:
        data = _service(db_manager).create_run(
            name=request.name,
            symbols=request.symbols,
            start_date=request.start_date,
            end_date=request.end_date,
            initial_cash_per_agent=request.initial_cash_per_agent,
            max_observations_per_day=request.max_observations_per_day,
            rule_version=request.rule_version,
            market=request.market,
            config=request.config,
            profiles=[p.dict() for p in request.profiles] if request.profiles is not None else None,
        )
        return AgentBacktestRunItem(**data)
    except AgentBacktestError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create agent backtest run failed", exc)


@router.get(
    "/runs",
    response_model=AgentBacktestRunListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="List agent backtest runs",
)
def list_runs(
    status: Optional[str] = Query(None, description="Optional run status"),
    market: Optional[str] = Query(None, description="Optional market namespace: cn or us"),
    limit: int = Query(50, ge=1, le=200),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestRunListResponse:
    try:
        data = _service(db_manager).list_runs(status=status, market=market, limit=limit)
        return AgentBacktestRunListResponse(
            total=int(data.get("total", 0)),
            items=[AgentBacktestRunItem(**item) for item in data.get("items", [])],
        )
    except AgentBacktestError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("List agent backtest runs failed", exc)


@router.get(
    "/runs/{run_id}",
    response_model=AgentBacktestRunItem,
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Get agent backtest run",
)
def get_run(
    run_id: int,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestRunItem:
    try:
        return AgentBacktestRunItem(**_service(db_manager).get_run(run_id))
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("Get agent backtest run failed", exc)


@router.patch(
    "/runs/{run_id}",
    response_model=AgentBacktestRunItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Update mutable agent backtest run settings",
)
def update_run(
    run_id: int,
    request: AgentBacktestRunUpdateRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestRunItem:
    try:
        data = _service(db_manager).update_run_settings(
            run_id=run_id,
            symbols=request.symbols,
            max_observations_per_day=request.max_observations_per_day,
        )
        return AgentBacktestRunItem(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("Update agent backtest run failed", exc)


@router.post(
    "/runs/{run_id}/profiles",
    response_model=AgentBacktestProfileItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Add one isolated agent profile to a run",
)
def add_profile(
    run_id: int,
    request: AgentBacktestProfileCreateRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestProfileItem:
    try:
        data = _service(db_manager).add_profile(
            run_id=run_id,
            profile=request.dict(),
        )
        return AgentBacktestProfileItem(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("Add agent backtest profile failed", exc)


@router.delete(
    "/runs/{run_id}/profiles/{profile_key}",
    response_model=AgentBacktestRunItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Deactivate one isolated agent profile in a run",
)
def deactivate_profile(
    run_id: int,
    profile_key: str,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestRunItem:
    try:
        data = _service(db_manager).deactivate_profile(run_id=run_id, profile_key=profile_key)
        return AgentBacktestRunItem(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("Deactivate agent backtest profile failed", exc)


@router.post(
    "/runs/{run_id}/observations",
    response_model=AgentBacktestObservationItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Record one bounded market observation",
)
def record_observation(
    run_id: int,
    request: AgentBacktestObservationCreateRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestObservationItem:
    try:
        data = _service(db_manager).record_observation(
            run_id=run_id,
            profile_key=request.profile_key,
            trade_date=request.trade_date,
            observation_time=request.observation_time,
            data_cutoff_at=request.data_cutoff_at,
            symbols=request.symbols,
            evidence=request.evidence,
            summary=request.summary,
        )
        return AgentBacktestObservationItem(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("Record observation failed", exc)


@router.post(
    "/runs/{run_id}/decisions",
    response_model=AgentBacktestDecisionItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Record one agent decision",
)
def record_decision(
    run_id: int,
    request: AgentBacktestDecisionCreateRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestDecisionItem:
    try:
        data = _service(db_manager).record_decision(
            run_id=run_id,
            profile_key=request.profile_key,
            observation_id=request.observation_id,
            trade_date=request.trade_date,
            decision_time=request.decision_time,
            action=request.action,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            limit_price=request.limit_price,
            confidence=request.confidence,
            rationale=request.rationale,
            risk_notes=request.risk_notes,
            policy_version_label=request.policy_version_label,
            raw_output=request.raw_output,
        )
        return AgentBacktestDecisionItem(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("Record decision failed", exc)


@router.post(
    "/runs/{run_id}/orders",
    response_model=AgentBacktestOrderItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Create simulated order from a decision",
)
def create_order(
    run_id: int,
    request: AgentBacktestOrderCreateRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestOrderItem:
    try:
        data = _service(db_manager).create_order(
            run_id=run_id,
            profile_key=request.profile_key,
            decision_id=request.decision_id,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            requested_quantity=request.requested_quantity,
            limit_price=request.limit_price,
            submitted_at=request.submitted_at,
            effective_at=request.effective_at,
        )
        return AgentBacktestOrderItem(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create order failed", exc)


@router.post(
    "/runs/{run_id}/orders/{order_id}/fills",
    response_model=AgentBacktestFillItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Record simulated fill and portfolio ledger trade",
)
def record_fill(
    run_id: int,
    order_id: int,
    request: AgentBacktestFillCreateRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestFillItem:
    try:
        data = _service(db_manager).record_fill(
            run_id=run_id,
            order_id=order_id,
            quantity=request.quantity,
            price=request.price,
            filled_at=request.filled_at,
            fee=request.fee,
            tax=request.tax,
            source=request.source,
        )
        return AgentBacktestFillItem(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("Record fill failed", exc)


@router.post(
    "/runs/{run_id}/profiles/{profile_key}/policies",
    response_model=AgentBacktestPolicyItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Append a forward-only agent policy version",
)
def evolve_policy(
    run_id: int,
    profile_key: str,
    request: AgentBacktestPolicyCreateRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestPolicyItem:
    try:
        data = _service(db_manager).evolve_policy(
            run_id=run_id,
            profile_key=profile_key,
            version_label=request.version_label,
            body_markdown=request.body_markdown,
            effective_from=request.effective_from,
            change_reason=request.change_reason,
        )
        return AgentBacktestPolicyItem(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("Append policy version failed", exc)


@router.get(
    "/runs/{run_id}/profiles/{profile_key}/policies",
    response_model=AgentBacktestPolicyListResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List policy versions for read-only browsing",
)
def list_policies(
    run_id: int,
    profile_key: str,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestPolicyListResponse:
    try:
        data = _service(db_manager).list_policies(run_id=run_id, profile_key=profile_key)
        return AgentBacktestPolicyListResponse(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("List policy versions failed", exc)


@router.post(
    "/runs/{run_id}/daily-nav",
    response_model=AgentBacktestDailyNavResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Capture daily NAV snapshots for all profiles in a run",
)
def record_daily_nav(
    run_id: int,
    request: AgentBacktestDailyNavRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestDailyNavResponse:
    try:
        data = _service(db_manager).record_daily_nav(run_id=run_id, trade_date=request.trade_date)
        return AgentBacktestDailyNavResponse(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("Record daily NAV failed", exc)


@router.get(
    "/runs/{run_id}/events",
    response_model=AgentBacktestEventsResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List observations, decisions, orders, fills, and NAV snapshots",
)
def list_events(
    run_id: int,
    profile_key: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> AgentBacktestEventsResponse:
    try:
        data = _service(db_manager).list_events(run_id=run_id, profile_key=profile_key, limit=limit)
        return AgentBacktestEventsResponse(**data)
    except AgentBacktestError as exc:
        raise _not_found_or_bad_request(exc)
    except Exception as exc:
        raise _internal_error("List events failed", exc)
