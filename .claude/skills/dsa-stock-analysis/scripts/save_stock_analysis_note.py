#!/usr/bin/env python3
"""Persist an Agent-side DSA stock analysis note into analysis_history."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = "dsa_stock_analysis_note.v1"


def _is_repo_root(path: Path) -> bool:
    return (path / "main.py").exists() and (path / "AGENTS.md").exists()


def _find_repo_root() -> Path:
    current = Path(__file__).resolve()
    candidates: List[Path] = []

    env_root = os.getenv("DSA_REPO_ROOT")
    if env_root:
        candidates.append(Path(env_root).expanduser())

    candidates.extend([current.parent, *current.parents])

    try:
        cwd = Path.cwd().resolve()
        candidates.extend([cwd, *cwd.parents])
    except Exception:
        pass

    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_repo_root(resolved):
            return resolved

    raise RuntimeError(
        "Could not locate DSA repository root. Set DSA_REPO_ROOT to the daily_stock_analysis path."
    )


REPO_ROOT = _find_repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except Exception:
        return str(value)


def _read_payload(path: str) -> Dict[str, Any]:
    if path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("analysis note payload must be a JSON object")
    return payload


def _pick(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _coerce_int(value: Any, default: int) -> int:
    try:
        number = int(float(value))
    except Exception:
        return default
    return max(0, min(100, number))


def _normalize_code(raw_code: str) -> str:
    try:
        from data_provider.base import canonical_stock_code, normalize_stock_code

        return canonical_stock_code(normalize_stock_code(raw_code))
    except Exception:
        return raw_code.strip().upper()


def _build_query_id(code: str, payload: Dict[str, Any]) -> str:
    provided = payload.get("query_id")
    if provided:
        return str(provided)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"agent-stock-{code}-{timestamp}"


def _build_result_payload(payload: Dict[str, Any], code: str) -> Dict[str, Any]:
    levels = payload.get("levels") or {}
    if not isinstance(levels, dict):
        levels = {}

    time_horizon = payload.get("time_horizon") if isinstance(payload.get("time_horizon"), dict) else {}
    factors = payload.get("factors") if isinstance(payload.get("factors"), dict) else {}
    action_plan = payload.get("action_plan") if isinstance(payload.get("action_plan"), dict) else {}

    sniper_points = {
        "ideal_buy": _pick(levels, "ideal_buy", "entry", "entry_price", "buy_zone"),
        "secondary_buy": _pick(levels, "secondary_buy", "add_position", "secondary_entry"),
        "support": _pick(levels, "support", "support_level", "near_support"),
        "resistance": _pick(levels, "resistance", "resistance_level", "near_resistance"),
        "breakdown": _pick(levels, "breakdown", "breakdown_level", "invalidation"),
        "stop_loss": _pick(levels, "stop_loss", "breakdown", "breakdown_level", "invalidation"),
        "take_profit": _pick(levels, "take_profit", "target", "target_price", "resistance"),
    }

    summary = str(
        payload.get("analysis_summary")
        or payload.get("summary")
        or payload.get("conclusion")
        or ""
    )

    dashboard = payload.get("dashboard") or {}
    if not isinstance(dashboard, dict):
        dashboard = {}
    dashboard.setdefault(
        "core_conclusion",
        {
            "one_sentence": payload.get("conclusion") or summary,
            "no_position_plan": _pick(action_plan, "no_position", "no_position_plan"),
            "has_position_plan": _pick(action_plan, "has_position", "has_position_plan"),
        },
    )
    dashboard.setdefault("battle_plan", {})
    if isinstance(dashboard["battle_plan"], dict):
        dashboard["battle_plan"].setdefault("sniper_points", sniper_points)
        checklist = _pick(action_plan, "checklist", "watch_points")
        if checklist:
            dashboard["battle_plan"].setdefault("action_checklist", checklist)
    dashboard.setdefault("key_levels", levels)
    dashboard.setdefault("time_horizon", time_horizon)
    dashboard.setdefault("factors", factors)
    dashboard.setdefault(
        "validity",
        {
            "analysis_date": payload.get("analysis_date"),
            "data_cutoff": payload.get("data_cutoff"),
            "valid_until": payload.get("valid_until"),
            "staleness_rule": payload.get("staleness_rule"),
        },
    )

    raw_payload = {
        "schema": SCHEMA_VERSION,
        "code": code,
        "analysis_date": payload.get("analysis_date"),
        "data_cutoff": payload.get("data_cutoff"),
        "valid_until": payload.get("valid_until"),
        "current_price": payload.get("current_price"),
        "change_pct": payload.get("change_pct"),
        "time_horizon": time_horizon,
        "factors": factors,
        "levels": levels,
        "action_plan": action_plan,
        "agent_note": payload.get("agent_note") or payload.get("markdown") or summary,
        "source_snapshot": payload.get("source_snapshot") or payload.get("context_snapshot"),
        "warnings": payload.get("warnings") or [],
    }

    return {
        "summary": summary,
        "dashboard": dashboard,
        "raw_payload": _jsonable(raw_payload),
    }


def _build_context_snapshot(payload: Dict[str, Any], query_id: str) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "query_id": query_id,
        "analysis_date": payload.get("analysis_date"),
        "data_cutoff": payload.get("data_cutoff"),
        "valid_until": payload.get("valid_until"),
        "staleness_rule": payload.get(
            "staleness_rule",
            "Treat intraday/short-term levels as stale after the next trading session unless refreshed.",
        ),
        "source": "codex:dsa-stock-analysis",
        "source_snapshot": payload.get("source_snapshot") or payload.get("context_snapshot"),
    }


def save_note(payload: Dict[str, Any]) -> Dict[str, Any]:
    from src.analyzer import AnalysisResult
    from src.config import setup_env
    from src.storage import get_db

    raw_code = str(payload.get("code") or payload.get("symbol") or "").strip()
    if not raw_code:
        raise ValueError("payload must include code or symbol")

    os.chdir(REPO_ROOT)
    setup_env()

    code = _normalize_code(raw_code)
    name = str(payload.get("name") or code)
    query_id = _build_query_id(code, payload)
    result_payload = _build_result_payload(payload, code)
    time_horizon = payload.get("time_horizon") if isinstance(payload.get("time_horizon"), dict) else {}
    factors = payload.get("factors") if isinstance(payload.get("factors"), dict) else {}

    result = AnalysisResult(
        code=code,
        name=name,
        sentiment_score=_coerce_int(payload.get("sentiment_score"), 50),
        trend_prediction=str(payload.get("trend_prediction") or "待观察"),
        operation_advice=str(payload.get("operation_advice") or "观望"),
        decision_type=str(payload.get("decision_type") or "hold"),
        confidence_level=str(payload.get("confidence_level") or payload.get("confidence") or "中"),
        dashboard=result_payload["dashboard"],
        trend_analysis=str(payload.get("trend_analysis") or ""),
        short_term_outlook=str(time_horizon.get("short") or payload.get("short_term_outlook") or ""),
        medium_term_outlook=str(time_horizon.get("medium") or payload.get("medium_term_outlook") or ""),
        technical_analysis=str(factors.get("technical") or ""),
        fundamental_analysis=str(factors.get("fundamental") or ""),
        market_sentiment=str(factors.get("sentiment") or ""),
        analysis_summary=result_payload["summary"],
        risk_warning=str(payload.get("risk_warning") or ""),
        current_price=payload.get("current_price"),
        change_pct=payload.get("change_pct"),
        raw_response=result_payload["raw_payload"],
        data_sources=str(payload.get("data_sources") or "DSA fact collector + Agent judgment"),
        query_id=query_id,
    )

    db = get_db()
    saved_count = db.save_analysis_history(
        result=result,
        query_id=query_id,
        report_type=str(payload.get("report_type") or "agent_note"),
        news_content=payload.get("news_content"),
        context_snapshot=_build_context_snapshot(payload, query_id),
        save_snapshot=True,
    )
    records = db.get_analysis_history(query_id=query_id, limit=1)
    record_id = getattr(records[0], "id", None) if records else None

    return {
        "saved_count": saved_count,
        "record_id": record_id,
        "query_id": query_id,
        "code": code,
        "report_type": str(payload.get("report_type") or "agent_note"),
        "schema": SCHEMA_VERSION,
    }


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persist an Agent-side DSA stock analysis note into analysis_history."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="JSON payload path, or '-' for stdin",
    )
    parser.add_argument("--compact", action="store_true", help="Print compact JSON")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    payload = _read_payload(args.input)
    result = save_note(payload)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
