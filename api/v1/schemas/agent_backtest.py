# -*- coding: utf-8 -*-
"""Agent backtest API schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class AgentBacktestProfileConfig(BaseModel):
    profile_key: str = Field(..., min_length=1, max_length=24)
    display_name: str = Field(..., min_length=1, max_length=64)
    style_profile: str = Field(..., min_length=1, max_length=24)
    policy_version_label: str = Field("v1.0", min_length=1, max_length=64)
    policy_markdown: str = Field(..., min_length=1)


class AgentBacktestRunCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    symbols: List[str] = Field(..., min_length=1, description="A-share symbol list")
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    initial_cash_per_agent: float = Field(20000.0, gt=0)
    max_observations_per_day: int = Field(3, ge=1, le=5)
    rule_version: str = Field("cn_a_v1", min_length=1, max_length=32)
    config: Dict[str, Any] = Field(default_factory=dict)
    profiles: Optional[List[AgentBacktestProfileConfig]] = None


class AgentBacktestProfileItem(BaseModel):
    id: int
    run_id: int
    account_id: int
    profile_key: str
    display_name: str
    style_profile: str
    policy_version_label: str
    policy_markdown: Optional[str] = None
    latest_policy_id: Optional[int] = None
    context_namespace: str
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AgentBacktestRunItem(BaseModel):
    id: int
    name: str
    status: str
    market: str
    symbols: List[str] = Field(default_factory=list)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_cash_per_agent: float
    max_observations_per_day: int
    rule_version: str
    config: Dict[str, Any] = Field(default_factory=dict)
    profiles: List[AgentBacktestProfileItem] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AgentBacktestRunListResponse(BaseModel):
    items: List[AgentBacktestRunItem] = Field(default_factory=list)
    total: int


class AgentBacktestObservationCreateRequest(BaseModel):
    profile_key: str = Field(..., min_length=1, max_length=24)
    trade_date: date
    observation_time: str = Field(..., description="HH:MM or HH:MM:SS")
    data_cutoff_at: datetime
    symbols: Optional[List[str]] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)
    summary: Optional[str] = None


class AgentBacktestObservationItem(BaseModel):
    id: int
    run_id: int
    profile_id: int
    trade_date: Optional[str] = None
    observation_time: str
    data_cutoff_at: Optional[str] = None
    sequence_no: int
    symbols: List[str] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    summary: Optional[str] = None
    created_at: Optional[str] = None


class AgentBacktestDecisionCreateRequest(BaseModel):
    profile_key: str = Field(..., min_length=1, max_length=24)
    observation_id: Optional[int] = None
    trade_date: date
    decision_time: datetime
    action: str = Field(..., min_length=1, max_length=24)
    symbol: Optional[str] = Field(None, max_length=16)
    side: Optional[Literal["buy", "sell"]] = None
    quantity: Optional[float] = Field(None, gt=0)
    order_type: Optional[Literal["limit", "market"]] = None
    limit_price: Optional[float] = Field(None, gt=0)
    confidence: Optional[float] = Field(None, ge=0, le=1)
    rationale: Optional[str] = None
    risk_notes: Optional[str] = None
    policy_version_label: Optional[str] = Field(None, max_length=64)
    raw_output: Dict[str, Any] = Field(default_factory=dict)


class AgentBacktestDecisionItem(BaseModel):
    id: int
    run_id: int
    profile_id: int
    observation_id: Optional[int] = None
    trade_date: Optional[str] = None
    decision_time: Optional[str] = None
    action: str
    symbol: Optional[str] = None
    side: Optional[str] = None
    quantity: Optional[float] = None
    order_type: Optional[str] = None
    limit_price: Optional[float] = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None
    risk_notes: Optional[str] = None
    policy_version_label: str
    raw_output: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class AgentBacktestOrderCreateRequest(BaseModel):
    profile_key: str = Field(..., min_length=1, max_length=24)
    decision_id: Optional[int] = None
    symbol: str = Field(..., min_length=1, max_length=16)
    side: Literal["buy", "sell"]
    order_type: Literal["limit", "market"] = "limit"
    requested_quantity: float = Field(..., gt=0)
    limit_price: Optional[float] = Field(None, gt=0)
    submitted_at: datetime
    effective_at: datetime


class AgentBacktestOrderItem(BaseModel):
    id: int
    run_id: int
    profile_id: int
    decision_id: Optional[int] = None
    symbol: str
    side: str
    order_type: str
    requested_quantity: float
    limit_price: Optional[float] = None
    submitted_at: Optional[str] = None
    effective_at: Optional[str] = None
    status: str
    reject_reason: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AgentBacktestFillCreateRequest(BaseModel):
    quantity: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    filled_at: datetime
    fee: Optional[float] = Field(None, ge=0)
    tax: Optional[float] = Field(None, ge=0)
    source: str = Field("manual", min_length=1, max_length=64)


class AgentBacktestFillItem(BaseModel):
    id: int
    run_id: int
    profile_id: int
    order_id: int
    portfolio_trade_id: Optional[int] = None
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    tax: float
    filled_at: Optional[str] = None
    source: str
    created_at: Optional[str] = None


class AgentBacktestPolicyCreateRequest(BaseModel):
    version_label: str = Field(..., min_length=1, max_length=64)
    body_markdown: str = Field(..., min_length=1)
    effective_from: Optional[date] = None
    change_reason: Optional[str] = None


class AgentBacktestPolicyItem(BaseModel):
    id: int
    run_id: int
    profile_id: int
    version_label: str
    body_markdown: str
    parent_policy_id: Optional[int] = None
    effective_from: Optional[str] = None
    change_reason: Optional[str] = None
    status: str
    created_at: Optional[str] = None


class AgentBacktestDailyNavRequest(BaseModel):
    trade_date: date


class AgentBacktestDailyNavItem(BaseModel):
    id: int
    run_id: int
    profile_id: int
    trade_date: Optional[str] = None
    cash: float
    market_value: float
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AgentBacktestDailyNavResponse(BaseModel):
    run_id: int
    trade_date: str
    items: List[AgentBacktestDailyNavItem] = Field(default_factory=list)


class AgentBacktestEventsResponse(BaseModel):
    observations: List[AgentBacktestObservationItem] = Field(default_factory=list)
    decisions: List[AgentBacktestDecisionItem] = Field(default_factory=list)
    orders: List[AgentBacktestOrderItem] = Field(default_factory=list)
    fills: List[AgentBacktestFillItem] = Field(default_factory=list)
    daily_nav: List[AgentBacktestDailyNavItem] = Field(default_factory=list)
