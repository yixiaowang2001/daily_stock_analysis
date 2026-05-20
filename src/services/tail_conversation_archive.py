# -*- coding: utf-8 -*-
"""Persist tail-session chat turns into tail tactics experiments.

This module is intentionally post-processing only: chat generation should not
depend on archive success.  The goal is to keep tail-session picks and model
assessment text traceable even when the user works from the normal Agent chat
instead of the Tail Tactics workbench page.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from data_provider.base import normalize_stock_code
from src.storage import DatabaseManager, get_db

logger = logging.getLogger(__name__)

TAIL_ARCHIVE_WORKFLOW_GOAL = "conversation_auto_tail_archive"

_TAIL_KEYWORDS = (
    "尾盘",
    "尾盘股",
    "尾盘选",
    "战术台",
    "t+1",
    "T+1",
    "次日早盘",
    "早盘",
    "冲高",
    "开盘预判",
    "tail_score_result",
)

_EVALUATION_MARKERS = (
    "tail_score_result",
    "评分",
    "评估",
    "预测",
    "预判",
    "开盘",
    "次日",
    "早盘",
    "冲高",
    "action_level",
    "next_open_forecast",
)

_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:SH|SZ|BJ)?([0369]\d{5})(?:\.(?:SH|SZ|SS|BJ))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TailArchiveOutcome:
    """Small serializable description of an archive write."""

    experiment_id: int
    mode: str
    created: bool
    trade_date: str
    symbols: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "mode": self.mode,
            "created": self.created,
            "trade_date": self.trade_date,
            "symbols": self.symbols,
        }


def maybe_persist_tail_conversation(
    *,
    message: str,
    response_content: str,
    session_id: str,
    context: Optional[Dict[str, Any]] = None,
    skills: Optional[Iterable[str]] = None,
    today: Optional[date] = None,
    db: Optional[DatabaseManager] = None,
) -> Optional[TailArchiveOutcome]:
    """
    Persist a finished Agent turn if it belongs to the tail tactics workflow.

    Explicit Tail Tactics workbench calls are identified by
    ``tail_ranking_bundle`` / ``tail_review_bundle`` in the request context and
    write back to the existing experiment.  Normal chat turns are archived only
    when the combined prompt/answer is tail-related and contains at least one
    A-share code.
    """

    if not response_content:
        return None

    db = db or get_db()
    ctx = context or {}

    ranking_bundle = ctx.get("tail_ranking_bundle")
    if isinstance(ranking_bundle, dict):
        return _persist_explicit_ranking(
            db=db,
            bundle=ranking_bundle,
            response_content=response_content,
            session_id=session_id,
        )

    review_bundle = ctx.get("tail_review_bundle")
    if isinstance(review_bundle, dict):
        return _persist_explicit_review(
            db=db,
            bundle=review_bundle,
            response_content=response_content,
            session_id=session_id,
        )

    combined = "\n".join([message or "", response_content or ""])
    archive_text = _with_recent_session_text(db=db, session_id=session_id, current_text=combined)
    if not _looks_like_tail_archive_candidate(archive_text, skills=skills):
        return None

    symbols = extract_a_share_symbols(archive_text)
    if not symbols:
        return None

    trade_date = extract_tail_trade_date(archive_text, today=today)
    strategy_id = _ensure_strategy_version(db)
    existing = _find_existing_auto_experiment(
        db=db,
        trade_date=trade_date,
        symbols=symbols,
        session_id=session_id,
    )

    status = "ranked" if _looks_like_evaluation(response_content) else "draft"
    param_snapshot = _build_auto_param_snapshot(
        message=message,
        response_content=response_content,
        session_id=session_id,
        skills=skills,
        trade_date=trade_date,
    )

    if existing is not None:
        db.patch_tail_experiment(
            int(existing["id"]),
            {
                "ranking_session_id": session_id,
                "ranking_output": response_content,
                "param_snapshot": param_snapshot,
                "status": status,
            },
        )
        return TailArchiveOutcome(
            experiment_id=int(existing["id"]),
            mode="auto_rank",
            created=False,
            trade_date=trade_date.isoformat(),
            symbols=symbols,
        )

    exp_id = db.create_tail_experiment(
        trade_date=trade_date,
        strategy_version_id=strategy_id,
        pasted_raw=message.strip() or "(auto archived tail conversation)",
        symbols=symbols,
        param_snapshot=param_snapshot,
        status=status,
    )
    db.patch_tail_experiment(
        exp_id,
        {
            "ranking_session_id": session_id,
            "ranking_output": response_content,
            "status": status,
        },
    )
    return TailArchiveOutcome(
        experiment_id=exp_id,
        mode="auto_rank",
        created=True,
        trade_date=trade_date.isoformat(),
        symbols=symbols,
    )


def extract_a_share_symbols(text: str, *, limit: int = 10) -> List[str]:
    """Extract ordered, de-duplicated A-share numeric symbols from text."""

    out: List[str] = []
    for match in _CODE_RE.finditer(text or ""):
        sym = normalize_stock_code(match.group(1)).strip()
        if not (sym.isdigit() and len(sym) == 6):
            continue
        if sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    return out


def extract_tail_trade_date(text: str, *, today: Optional[date] = None) -> date:
    """Resolve a trade date from common Chinese tail-session phrasing."""

    base = today or datetime.now().date()
    s = text or ""

    m = re.search(r"(20\d{2})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})", s)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), base)

    m = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日?(?!\d)", s)
    if m:
        return _safe_date(base.year, int(m.group(1)), int(m.group(2)), base)

    m = re.search(r"(?<!\d)(\d{4})(?!\d)", s)
    if m:
        raw = m.group(1)
        month = int(raw[:2])
        day = int(raw[2:])
        if 1 <= month <= 12 and 1 <= day <= 31:
            return _safe_date(base.year, month, day, base)

    if "昨天" in s:
        return base - timedelta(days=1)
    if "前天" in s:
        return base - timedelta(days=2)
    return base


def extract_json_root(content: str, root_key: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of a JSON object containing ``root_key``."""

    text = content or ""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and root_key in obj:
            return obj
    return None


def _persist_explicit_ranking(
    *,
    db: DatabaseManager,
    bundle: Dict[str, Any],
    response_content: str,
    session_id: str,
) -> Optional[TailArchiveOutcome]:
    exp_id = _coerce_int(bundle.get("experiment_id"))
    if exp_id is None:
        return None
    exp = db.get_tail_experiment(exp_id)
    if exp is None:
        return None

    db.patch_tail_experiment(
        exp_id,
        {
            "ranking_session_id": session_id,
            "ranking_output": response_content,
            "status": "ranked",
        },
    )
    return TailArchiveOutcome(
        experiment_id=exp_id,
        mode="rank",
        created=False,
        trade_date=str(exp.get("trade_date") or ""),
        symbols=list(exp.get("symbols") or []),
    )


def _persist_explicit_review(
    *,
    db: DatabaseManager,
    bundle: Dict[str, Any],
    response_content: str,
    session_id: str,
) -> Optional[TailArchiveOutcome]:
    exp_id = _coerce_int(bundle.get("experiment_id"))
    if exp_id is None:
        return None
    exp = db.get_tail_experiment(exp_id)
    if exp is None:
        return None

    suggestions = extract_json_root(response_content, "tail_review_suggestions") or {}
    review_suggestions = suggestions.get("tail_review_suggestions")
    case_summary = None
    if isinstance(review_suggestions, dict):
        raw_summary = review_suggestions.get("case_summary")
        if raw_summary:
            case_summary = str(raw_summary)[:500]

    updates: Dict[str, Any] = {
        "review_note_markdown": response_content,
        "status": "closed",
    }
    if case_summary:
        updates["case_summary"] = case_summary
    db.patch_tail_experiment(exp_id, updates)
    return TailArchiveOutcome(
        experiment_id=exp_id,
        mode="review",
        created=False,
        trade_date=str(exp.get("trade_date") or ""),
        symbols=list(exp.get("symbols") or []),
    )


def _looks_like_tail_archive_candidate(text: str, *, skills: Optional[Iterable[str]]) -> bool:
    skill_text = " ".join([str(s) for s in skills or []])
    haystack = f"{text}\n{skill_text}"
    if not any(k in haystack for k in _TAIL_KEYWORDS):
        return False
    return bool(extract_a_share_symbols(haystack, limit=1))


def _looks_like_evaluation(text: str) -> bool:
    return any(k in (text or "") for k in _EVALUATION_MARKERS)


def _with_recent_session_text(*, db: DatabaseManager, session_id: str, current_text: str) -> str:
    try:
        messages = db.get_conversation_history(session_id, limit=20)
    except Exception:
        return current_text
    history_parts = [
        str(item.get("content") or "")
        for item in messages
        if isinstance(item, dict) and item.get("content")
    ]
    if not history_parts:
        return current_text
    return "\n".join([current_text, *history_parts])


def _build_auto_param_snapshot(
    *,
    message: str,
    response_content: str,
    session_id: str,
    skills: Optional[Iterable[str]],
    trade_date: date,
) -> Dict[str, Any]:
    score_json = extract_json_root(response_content, "tail_score_result")
    return {
        "workflow_goal": TAIL_ARCHIVE_WORKFLOW_GOAL,
        "agent_role": "A股尾盘候选池逐票评分与复盘助手",
        "risk_mode": "strict",
        "intended_holding_period": "T+1 / 次日早盘复盘",
        "data_cutoff_time": "14:40",
        "source": "agent_chat_postprocess",
        "source_session_id": session_id,
        "trade_date": trade_date.isoformat(),
        "skills": [str(s) for s in skills or []],
        "archive_reason": "normal Agent chat mentioned tail-session candidates and A-share symbols",
        "has_tail_score_result": bool(score_json),
        "user_message_excerpt": (message or "").strip()[:500],
    }


def _find_existing_auto_experiment(
    *,
    db: DatabaseManager,
    trade_date: date,
    symbols: List[str],
    session_id: str,
) -> Optional[Dict[str, Any]]:
    rows = db.list_tail_experiments(limit=200, from_date=trade_date, to_date=trade_date)
    for row in rows:
        row_symbols = [str(s) for s in row.get("symbols") or []]
        snapshot = row.get("param_snapshot") or {}
        if row.get("ranking_session_id") == session_id and row_symbols == symbols:
            return row
        if (
            isinstance(snapshot, dict)
            and snapshot.get("workflow_goal") == TAIL_ARCHIVE_WORKFLOW_GOAL
            and snapshot.get("source_session_id") == session_id
            and row_symbols == symbols
        ):
            return row
    for row in rows:
        row_symbols = [str(s) for s in row.get("symbols") or []]
        snapshot = row.get("param_snapshot") or {}
        if (
            isinstance(snapshot, dict)
            and snapshot.get("workflow_goal") == TAIL_ARCHIVE_WORKFLOW_GOAL
            and row_symbols == symbols
        ):
            return row
    return None


def _ensure_strategy_version(db: DatabaseManager) -> int:
    versions = db.list_tail_strategy_versions(limit=1)
    if versions:
        return int(versions[0]["id"])
    return db.create_tail_strategy_version(
        version_label="auto",
        title="尾盘自动归档默认策略",
        body_markdown=(
            "# 尾盘自动归档默认策略\n\n"
            "由普通 Agent 对话自动归档时使用。该版本仅作为兜底策略版本，"
            "用于保证候选池、评估报告与次日复盘可追溯。"
        ),
    )


def _safe_date(year: int, month: int, day: int, fallback: date) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        return fallback


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
