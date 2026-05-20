# -*- coding: utf-8 -*-
"""REST API for tail tactics workbench (strategy versions, experiments, morning metrics)."""

from __future__ import annotations

import difflib
import logging
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import Response

from api.v1.schemas.tail_tactics import (
    TailAgentComposeResponse,
    TailCandidateFactSnapshotItem,
    TailCandidateFactSnapshotListResponse,
    TailExperimentCreate,
    TailExperimentItem,
    TailExperimentListResponse,
    TailExperimentPatch,
    TailMorningMetricsPut,
    TailMorningMetricsAutoFetchBody,
    TailStrategyDiffResponse,
    TailStrategyVersionCreate,
    TailStrategyVersionItem,
    TailStrategyVersionListResponse,
)
from src.services.tail_candidate_facts import build_tail_candidate_facts
from src.services.tail_morning_fetch import auto_fetch_tail_morning_metrics
from src.services.tail_tactics_compose import build_ranking_compose, build_review_compose
from src.storage import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


def _version_item(d: dict) -> TailStrategyVersionItem:
    return TailStrategyVersionItem(
        id=d["id"],
        version_label=d["version_label"],
        title=d["title"],
        body_markdown=d["body_markdown"],
        parent_version_id=d.get("parent_version_id"),
        created_at=d.get("created_at"),
    )


def _experiment_item(d: dict) -> TailExperimentItem:
    return TailExperimentItem(
        id=d["id"],
        trade_date=d.get("trade_date"),
        strategy_version_id=d["strategy_version_id"],
        pasted_raw=d["pasted_raw"],
        symbols=list(d.get("symbols") or []),
        param_snapshot=d.get("param_snapshot"),
        ranking_session_id=d.get("ranking_session_id"),
        ranking_output=d.get("ranking_output"),
        review_note_markdown=d.get("review_note_markdown"),
        case_summary=d.get("case_summary"),
        status=d["status"],
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        morning_metrics_count=int(d.get("morning_metrics_count") or 0),
    )


def _candidate_fact_snapshot_item(d: dict) -> TailCandidateFactSnapshotItem:
    return TailCandidateFactSnapshotItem(
        id=d["id"],
        experiment_id=d["experiment_id"],
        kind=d["kind"],
        facts=d.get("facts") or {},
        data_freshness_summary=d.get("data_freshness_summary"),
        generated_at=d.get("generated_at"),
    )


@router.get("/strategy-versions", response_model=TailStrategyVersionListResponse)
async def list_strategy_versions(limit: int = Query(100, ge=1, le=200)):
    db = get_db()
    rows = db.list_tail_strategy_versions(limit=limit)
    return TailStrategyVersionListResponse(versions=[_version_item(r) for r in rows])


@router.post("/strategy-versions", response_model=TailStrategyVersionItem)
async def create_strategy_version(body: TailStrategyVersionCreate):
    db = get_db()
    if body.parent_version_id is not None:
        parent = db.get_tail_strategy_version(body.parent_version_id)
        if parent is None:
            raise HTTPException(status_code=400, detail="parent_version_id not found")
    try:
        new_id = db.create_tail_strategy_version(
            version_label=body.version_label,
            title=body.title,
            body_markdown=body.body_markdown,
            parent_version_id=body.parent_version_id,
        )
    except Exception as exc:
        logger.exception("create_tail_strategy_version failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    row = db.get_tail_strategy_version(new_id)
    if row is None:
        raise HTTPException(status_code=500, detail="failed to read created version")
    return _version_item(row)


@router.get("/strategy-versions/{version_id}", response_model=TailStrategyVersionItem)
async def get_strategy_version(version_id: int):
    db = get_db()
    row = db.get_tail_strategy_version(version_id)
    if row is None:
        raise HTTPException(status_code=404, detail="strategy version not found")
    return _version_item(row)


@router.delete("/strategy-versions/{version_id}")
async def delete_strategy_version(version_id: int):
    db = get_db()
    err = db.delete_tail_strategy_version(version_id)
    if err == "not_found":
        raise HTTPException(status_code=404, detail="strategy version not found")
    if err == "in_use":
        raise HTTPException(
            status_code=409,
            detail="strategy version is referenced by one or more experiments; delete or reassign experiments first",
        )
    return Response(status_code=204)


@router.get(
    "/strategy-versions/{version_a}/diff/{version_b}",
    response_model=TailStrategyDiffResponse,
)
async def diff_strategy_versions(version_a: int, version_b: int):
    db = get_db()
    a = db.get_tail_strategy_version(version_a)
    b = db.get_tail_strategy_version(version_b)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="one or both versions not found")
    ta = (a.get("body_markdown") or "").splitlines(keepends=True)
    tb = (b.get("body_markdown") or "").splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        ta,
        tb,
        fromfile=f"a/{version_a}",
        tofile=f"b/{version_b}",
        lineterm="",
    )
    unified = "\n".join(diff_lines)
    return TailStrategyDiffResponse(
        version_a_id=version_a,
        version_b_id=version_b,
        unified_diff=unified,
    )


@router.get("/experiments", response_model=TailExperimentListResponse)
async def list_experiments(
    limit: int = Query(50, ge=1, le=200),
    strategy_version_id: Optional[int] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
):
    db = get_db()
    rows = db.list_tail_experiments(
        limit=limit,
        strategy_version_id=strategy_version_id,
        from_date=from_date,
        to_date=to_date,
    )
    return TailExperimentListResponse(experiments=[_experiment_item(r) for r in rows])


@router.post("/experiments", response_model=TailExperimentItem)
async def create_experiment(body: TailExperimentCreate):
    db = get_db()
    if db.get_tail_strategy_version(body.strategy_version_id) is None:
        raise HTTPException(status_code=400, detail="strategy_version_id not found")
    try:
        new_id = db.create_tail_experiment(
            trade_date=body.trade_date,
            strategy_version_id=body.strategy_version_id,
            pasted_raw=body.pasted_raw,
            symbols=body.symbols,
            param_snapshot=body.param_snapshot,
            status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("create_tail_experiment failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    row = db.get_tail_experiment(new_id)
    if row is None:
        raise HTTPException(status_code=500, detail="failed to read created experiment")
    return _experiment_item(row)


@router.get("/experiments/{experiment_id}", response_model=TailExperimentItem)
async def get_experiment(experiment_id: int):
    db = get_db()
    row = db.get_tail_experiment(experiment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return _experiment_item(row)


@router.patch("/experiments/{experiment_id}", response_model=TailExperimentItem)
async def patch_experiment(experiment_id: int, body: TailExperimentPatch):
    db = get_db()
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        row = db.get_tail_experiment(experiment_id)
        if row is None:
            raise HTTPException(status_code=404, detail="experiment not found")
        return _experiment_item(row)
    ok = db.patch_tail_experiment(experiment_id, updates)
    if not ok:
        raise HTTPException(status_code=404, detail="experiment not found")
    row = db.get_tail_experiment(experiment_id)
    assert row is not None
    return _experiment_item(row)


@router.delete("/experiments/{experiment_id}")
async def delete_experiment(experiment_id: int):
    db = get_db()
    if not db.delete_tail_experiment(experiment_id):
        raise HTTPException(status_code=404, detail="experiment not found")
    return Response(status_code=204)


@router.get("/experiments/{experiment_id}/candidate-facts")
async def get_candidate_facts(experiment_id: int):
    """Return DB-first evidence for tail-session candidates; judgement stays with the Agent."""
    db = get_db()
    exp = db.get_tail_experiment(experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return build_tail_candidate_facts(experiment=exp, db=db)


@router.get(
    "/experiments/{experiment_id}/candidate-fact-snapshots",
    response_model=TailCandidateFactSnapshotListResponse,
)
async def list_candidate_fact_snapshots(
    experiment_id: int,
    kind: Optional[str] = Query(None, max_length=16),
    limit: int = Query(20, ge=1, le=100),
):
    """List persisted evidence snapshots actually handed to Agent workflows."""
    db = get_db()
    try:
        rows = db.list_tail_candidate_fact_snapshots(experiment_id, kind=kind, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return TailCandidateFactSnapshotListResponse(
        snapshots=[_candidate_fact_snapshot_item(r) for r in rows]
    )


@router.get("/experiments/{experiment_id}/compose", response_model=TailAgentComposeResponse)
async def compose_agent_payload(
    experiment_id: int,
    kind: Literal["rank", "review"] = Query(..., description="rank (compat name for scoring) or review"),
):
    db = get_db()
    exp = db.get_tail_experiment(experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    strat = db.get_tail_strategy_version(exp["strategy_version_id"])
    if strat is None:
        raise HTTPException(status_code=500, detail="strategy version missing for experiment")

    if kind == "rank":
        candidate_facts = build_tail_candidate_facts(experiment=exp, db=db)
        try:
            snapshot_id = db.create_tail_candidate_fact_snapshot(
                experiment_id=experiment_id,
                kind="rank",
                facts=candidate_facts,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        candidate_facts = {
            **candidate_facts,
            "snapshot_id": snapshot_id,
            "snapshot_kind": "rank",
        }
        payload = build_ranking_compose(
            experiment_id=experiment_id,
            experiment=exp,
            strategy=strat,
            candidate_facts=candidate_facts,
        )
    else:
        metrics = db.list_tail_morning_metrics(experiment_id)
        if not metrics:
            raise HTTPException(
                status_code=400,
                detail="morning metrics required before review compose; auto-fetch or save metrics first",
            )
        payload = build_review_compose(
            experiment_id=experiment_id,
            experiment=exp,
            strategy=strat,
            morning_metrics=metrics,
        )
    return TailAgentComposeResponse(
        session_id=payload["session_id"],
        message=payload["message"],
        context=payload["context"],
    )


@router.put("/experiments/{experiment_id}/morning-metrics")
async def put_morning_metrics(experiment_id: int, body: TailMorningMetricsPut):
    db = get_db()
    if db.get_tail_experiment(experiment_id) is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    items = [m.model_dump() for m in body.items]
    try:
        db.upsert_tail_morning_metrics(experiment_id, items)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "count": len(items)}


@router.post("/experiments/{experiment_id}/morning-metrics/auto-fetch")
async def auto_fetch_morning_metrics(
    experiment_id: int,
    body: TailMorningMetricsAutoFetchBody = Body(default_factory=TailMorningMetricsAutoFetchBody),
):
    """从行情源自动计算早盘冲高 % 并写入数据库（依赖网络与 AkShare / 日线数据源）。"""
    db = get_db()
    exp = db.get_tail_experiment(experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    try:
        items, m_date, notes = auto_fetch_tail_morning_metrics(
            experiment=exp,
            morning_trade_date=body.morning_trade_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("auto_fetch_tail_morning_metrics failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if items:
        try:
            db.upsert_tail_morning_metrics(experiment_id, items)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "morning_trade_date": m_date.isoformat(),
        "notes": notes,
        "items": db.list_tail_morning_metrics(experiment_id),
    }


@router.get("/experiments/{experiment_id}/morning-metrics")
async def get_morning_metrics(experiment_id: int):
    db = get_db()
    if db.get_tail_experiment(experiment_id) is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return {"items": db.list_tail_morning_metrics(experiment_id)}
