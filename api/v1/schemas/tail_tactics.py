# -*- coding: utf-8 -*-
"""Pydantic schemas for tail tactics workbench API."""

from __future__ import annotations

from datetime import date
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator


class TailStrategyVersionCreate(BaseModel):
    version_label: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    body_markdown: str = Field(..., min_length=1)
    parent_version_id: Optional[int] = None


class TailStrategyVersionItem(BaseModel):
    id: int
    version_label: str
    title: str
    body_markdown: str
    parent_version_id: Optional[int] = None
    created_at: Optional[str] = None


class TailStrategyVersionListResponse(BaseModel):
    versions: List[TailStrategyVersionItem]


class TailStrategyDiffResponse(BaseModel):
    version_a_id: int
    version_b_id: int
    unified_diff: str


class TailExperimentCreate(BaseModel):
    trade_date: date
    strategy_version_id: int
    pasted_raw: str = Field(..., min_length=1)
    symbols: List[str] = Field(..., min_length=1, max_length=10)
    param_snapshot: Optional[dict[str, Any]] = None
    status: str = Field(default="draft", max_length=32)

    @field_validator("symbols")
    @classmethod
    def strip_symbols(cls, v: List[str]) -> List[str]:
        out = [s.strip() for s in v if s and str(s).strip()]
        if not out:
            raise ValueError("symbols must contain at least one non-empty code")
        if len(out) > 10:
            raise ValueError("at most 10 symbols")
        return out


class TailExperimentPatch(BaseModel):
    ranking_session_id: Optional[str] = Field(None, max_length=100)
    ranking_output: Optional[str] = None
    review_note_markdown: Optional[str] = None
    case_summary: Optional[str] = Field(None, max_length=500)
    param_snapshot: Optional[dict[str, Any]] = None
    status: Optional[str] = Field(None, max_length=32)


class TailExperimentItem(BaseModel):
    id: int
    trade_date: Optional[str] = None
    strategy_version_id: int
    pasted_raw: str
    symbols: List[str]
    param_snapshot: Optional[dict[str, Any]] = None
    ranking_session_id: Optional[str] = None
    ranking_output: Optional[str] = None
    review_note_markdown: Optional[str] = None
    case_summary: Optional[str] = None
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    morning_metrics_count: int = 0


class TailExperimentListResponse(BaseModel):
    experiments: List[TailExperimentItem]


class TailMorningMetricItem(BaseModel):
    symbol: str
    surge_pct_prev_close_930_1000: Optional[float] = None
    source: str = "manual"


class TailMorningMetricsPut(BaseModel):
    items: List[TailMorningMetricItem]


class TailMorningMetricsAutoFetchBody(BaseModel):
    """可选覆盖「早盘数据所在交易日」；默认取实验登记日 T 之后第一个 A 股交易日。"""

    morning_trade_date: Optional[date] = None


class TailCandidateFactSnapshotItem(BaseModel):
    id: int
    experiment_id: int
    kind: str
    facts: dict[str, Any]
    data_freshness_summary: Optional[str] = None
    generated_at: Optional[str] = None


class TailCandidateFactSnapshotListResponse(BaseModel):
    snapshots: List[TailCandidateFactSnapshotItem]


class TailAgentComposeResponse(BaseModel):
    """Payload for calling ``POST /api/v1/agent/chat/stream`` with ``X-DSA-Tail-Ranking: 1``."""

    session_id: str
    message: str
    context: dict[str, Any]
