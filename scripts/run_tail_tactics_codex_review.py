#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex runner for tail tactics next-day morning reviews."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import setup_env  # noqa: E402
from src.core.trading_calendar import is_market_open  # noqa: E402
from src.services.tail_conversation_archive import (  # noqa: E402
    extract_json_root,
    maybe_persist_tail_conversation,
)
from src.services.tail_morning_fetch import auto_fetch_tail_morning_metrics  # noqa: E402
from src.services.tail_tactics_compose import (  # noqa: E402
    build_review_compose,
    format_tail_review_context_message,
    get_tail_layer2_calibration_path,
)
from src.storage import DatabaseManager, get_db  # noqa: E402


TIMEZONE = "Asia/Shanghai"
DEFAULT_CONTEXT_DIR = ROOT / ".claude" / "reviews" / "tail_tactics"


def parse_date(value: Optional[str], *, default: Optional[date] = None) -> Optional[date]:
    if not value:
        return default
    return date.fromisoformat(str(value).strip())


def is_cn_trading_day(check_date: date) -> bool:
    """A-share trading-day check with a weekend guard for fail-open calendars."""

    if check_date.weekday() >= 5:
        return False
    return is_market_open("cn", check_date)


def previous_cn_trading_day_before(from_date: date, *, max_step: int = 40) -> Optional[date]:
    """Return the latest A-share trading day strictly before ``from_date``."""

    cur = from_date - timedelta(days=1)
    for _ in range(max_step):
        if is_cn_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    return None


def _today(args: argparse.Namespace) -> date:
    return parse_date(args.today, default=datetime.now(ZoneInfo(TIMEZONE)).date()) or date.today()


def _is_reviewable(row: Dict[str, Any], *, include_reviewed: bool = False) -> bool:
    if not row.get("ranking_output"):
        return False
    if include_reviewed:
        return True
    if row.get("review_note_markdown"):
        return False
    if str(row.get("status") or "").lower() == "closed":
        return False
    return True


def _select_target_experiment(
    *,
    db: DatabaseManager,
    experiment_id: Optional[int],
    trade_date: date,
    include_reviewed: bool = False,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], str]:
    if experiment_id is not None:
        row = db.get_tail_experiment(experiment_id)
        if row is None:
            return None, [], f"tail experiment not found: {experiment_id}"
        if not _is_reviewable(row, include_reviewed=include_reviewed):
            return None, [row], "target experiment is missing ranking_output or has already been reviewed"
        return row, [row], "selected by experiment_id"

    rows = db.list_tail_experiments(limit=50, from_date=trade_date, to_date=trade_date)
    reviewable = [r for r in rows if _is_reviewable(r, include_reviewed=include_reviewed)]
    if not reviewable:
        if not rows:
            return None, [], f"no tail experiment found for trade_date={trade_date.isoformat()}"
        return None, rows, "no unreviewed ranked tail experiment found for target trade date"
    return reviewable[0], rows, "selected latest unreviewed ranked experiment for target trade date"


def _ensure_morning_metrics(
    *,
    db: DatabaseManager,
    experiment: Dict[str, Any],
    morning_trade_date: Optional[date],
    auto_fetch: bool,
    force_auto_fetch: bool,
) -> Dict[str, Any]:
    experiment_id = int(experiment["id"])
    existing = db.list_tail_morning_metrics(experiment_id)
    if existing and not force_auto_fetch:
        return {
            "auto_fetch_attempted": False,
            "reason": "existing metrics kept",
            "morning_trade_date": morning_trade_date.isoformat() if morning_trade_date else None,
            "notes": [],
            "items": existing,
        }

    if not auto_fetch:
        return {
            "auto_fetch_attempted": False,
            "reason": "auto_fetch disabled",
            "morning_trade_date": morning_trade_date.isoformat() if morning_trade_date else None,
            "notes": [],
            "items": existing,
        }

    items, resolved_date, notes = auto_fetch_tail_morning_metrics(
        experiment=experiment,
        morning_trade_date=morning_trade_date,
    )
    if items:
        db.upsert_tail_morning_metrics(experiment_id, items)
    return {
        "auto_fetch_attempted": True,
        "reason": "auto_fetch completed",
        "morning_trade_date": resolved_date.isoformat(),
        "notes": notes,
        "items": db.list_tail_morning_metrics(experiment_id),
    }


def _render_context_markdown(payload: Dict[str, Any]) -> str:
    experiment = payload["experiment"]
    compose = payload["compose"]
    bundle = compose["context"]["tail_review_bundle"]
    lines = [
        "# Codex Tail Tactics Morning Review Context",
        "",
        "You are reviewing a saved DSA tail-session experiment after the next morning session.",
        "Use only the saved ranking output, strategy text, parameter snapshot, and morning metrics below unless you explicitly mark any additional data source and freshness.",
        "",
        "## Task",
        "- Compare the previous tail-session forecast with the 09:30-10:00 morning result.",
        "- Separate Layer 1 selection-rule changes from Layer 2 Agent evaluation/forecast-weight changes.",
        "- Do not rewrite `watch` / `candidate` / `priority` into Tonghuashun screening rules.",
        "- Put Layer 1 changes into `layer1_change_requests` for the user to approve; do not apply them automatically.",
        "- Put Layer 2 Agent scoring/forecast calibration into `layer2_calibration_notes`; these notes will be appended to local self-iteration memory.",
        "- End the review with a pure JSON object containing `tail_review_suggestions.case_summary` and optional `layer2_calibration_notes` / `layer1_change_requests` arrays.",
        "",
        "## Persistence",
        "After writing the review, persist it with:",
        "",
        "```bash",
        f"python scripts/run_tail_tactics_codex_review.py apply-review --experiment-id {experiment['id']} --review-file <review_md_file>",
        "```",
        "",
        "## Compose Message",
        compose["message"],
        "",
        "## Review Bundle",
        format_tail_review_context_message(bundle),
    ]
    return "\n".join(lines) + "\n"


def _write_context_files(
    *,
    context_dir: Path,
    experiment: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, str]:
    trade_date = str(experiment.get("trade_date") or "unknown-date")
    experiment_id = int(experiment["id"])
    target_dir = context_dir / trade_date
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / f"exp_{experiment_id}_review_context.json"
    md_path = target_dir / f"exp_{experiment_id}_review_context.md"
    json_payload = dict(payload)
    json_payload["context_markdown"] = str(md_path)
    json_payload["context_json"] = str(json_path)
    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    md_path.write_text(_render_context_markdown(json_payload), encoding="utf-8")
    return {"context_json": str(json_path), "context_markdown": str(md_path)}


def command_prepare_review(args: argparse.Namespace) -> Dict[str, Any]:
    db = get_db()
    today = _today(args)
    if not args.allow_non_trading_day and not is_cn_trading_day(today):
        return {
            "skipped": True,
            "reason": f"{today.isoformat()} is not an A-share trading day",
            "today": today.isoformat(),
        }

    explicit_trade_date = parse_date(args.trade_date)
    target_trade_date = explicit_trade_date
    if target_trade_date is None:
        target_trade_date = previous_cn_trading_day_before(today)
    if target_trade_date is None:
        return {
            "skipped": True,
            "reason": "cannot resolve previous A-share trading day",
            "today": today.isoformat(),
        }

    target, candidates, select_reason = _select_target_experiment(
        db=db,
        experiment_id=args.experiment_id,
        trade_date=target_trade_date,
        include_reviewed=args.include_reviewed,
    )
    if target is None:
        return {
            "skipped": True,
            "reason": select_reason,
            "today": today.isoformat(),
            "target_trade_date": target_trade_date.isoformat(),
            "candidate_count": len(candidates),
            "candidate_ids": [r.get("id") for r in candidates],
        }

    strategy = db.get_tail_strategy_version(int(target["strategy_version_id"]))
    if strategy is None:
        return {
            "skipped": True,
            "reason": f"strategy version missing: {target['strategy_version_id']}",
            "experiment_id": target["id"],
        }

    morning_trade_date = parse_date(args.morning_trade_date, default=today)
    metrics_payload = _ensure_morning_metrics(
        db=db,
        experiment=target,
        morning_trade_date=morning_trade_date,
        auto_fetch=not args.no_auto_fetch,
        force_auto_fetch=args.force_auto_fetch,
    )
    metrics = metrics_payload["items"]
    if not metrics:
        return {
            "skipped": True,
            "reason": "morning metrics are missing; auto-fetch did not produce any rows",
            "experiment_id": target["id"],
            "target_trade_date": target_trade_date.isoformat(),
            "auto_fetch": metrics_payload,
        }

    refreshed = db.get_tail_experiment(int(target["id"])) or target
    compose = build_review_compose(
        experiment_id=int(refreshed["id"]),
        experiment=refreshed,
        strategy=strategy,
        morning_metrics=metrics,
    )
    payload = {
        "skipped": False,
        "today": today.isoformat(),
        "target_trade_date": target_trade_date.isoformat(),
        "selection_reason": select_reason,
        "experiment": refreshed,
        "strategy": strategy,
        "morning_metrics": metrics_payload,
        "compose": compose,
        "next_step": (
            "Read context_markdown, write a Chinese review with tail_review_suggestions JSON, "
            "then call apply-review with the review file."
        ),
    }
    paths = _write_context_files(
        context_dir=Path(args.context_dir),
        experiment=refreshed,
        payload=payload,
    )
    return {
        "skipped": False,
        "experiment_id": refreshed["id"],
        "trade_date": refreshed.get("trade_date"),
        "symbols": refreshed.get("symbols") or [],
        "morning_metrics_count": len(metrics),
        **paths,
        "next_step": payload["next_step"],
    }


def _load_review_content(args: argparse.Namespace) -> str:
    if args.review_text:
        return args.review_text
    if args.review_file:
        if args.review_file == "-":
            return sys.stdin.read()
        return Path(args.review_file).read_text(encoding="utf-8")
    raise ValueError("review_file or review_text is required")


def _normalize_notes(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = [str(value)]
    out: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _append_notes(path: Path, *, title: str, experiment: Dict[str, Any], notes: List[str]) -> Dict[str, Any]:
    if not notes:
        return {"updated": False, "path": str(path), "count": 0}
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines: List[str] = []
    if not existing.strip():
        lines.extend([f"# {title}", ""])
    lines.extend(
        [
            f"## {now} · experiment {experiment.get('id')} · T={experiment.get('trade_date')}",
            "",
        ]
    )
    symbols = ", ".join([str(s) for s in experiment.get("symbols") or []])
    if symbols:
        lines.append(f"- symbols: {symbols}")
    for note in notes:
        lines.append(f"- {note}")
    lines.append("")
    with path.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write("\n".join(lines))
    return {"updated": True, "path": str(path), "count": len(notes)}


def _update_iteration_memory(review_content: str, experiment: Dict[str, Any]) -> Dict[str, Any]:
    suggestions = extract_json_root(review_content, "tail_review_suggestions") or {}
    payload = suggestions.get("tail_review_suggestions")
    if not isinstance(payload, dict):
        return {
            "layer2": {"updated": False, "path": str(get_tail_layer2_calibration_path()), "count": 0},
            "layer1": {"updated": False, "path": str(_layer1_requests_path()), "count": 0},
        }

    layer2_notes = _normalize_notes(payload.get("layer2_calibration_notes"))
    layer1_requests = _normalize_notes(payload.get("layer1_change_requests"))
    return {
        "layer2": _append_notes(
            get_tail_layer2_calibration_path(),
            title="Tail Tactics Layer 2 Agent Evaluation Calibration",
            experiment=experiment,
            notes=layer2_notes,
        ),
        "layer1": _append_notes(
            _layer1_requests_path(),
            title="Tail Tactics Layer 1 Change Requests Pending User Approval",
            experiment=experiment,
            notes=layer1_requests,
        ),
    }


def _layer1_requests_path() -> Path:
    layer2_path = get_tail_layer2_calibration_path()
    return layer2_path.with_name("layer1_change_requests.md")


def command_apply_review(args: argparse.Namespace) -> Dict[str, Any]:
    db = get_db()
    review_content = _load_review_content(args)
    exp = db.get_tail_experiment(args.experiment_id)
    if exp is None:
        raise ValueError(f"tail experiment not found: {args.experiment_id}")
    outcome = maybe_persist_tail_conversation(
        message="tail tactics scheduled review",
        response_content=review_content,
        session_id=args.session_id or f"tail_exp_{args.experiment_id}",
        context={"tail_review_bundle": {"experiment_id": args.experiment_id}},
        db=db,
    )
    if outcome is None:
        raise ValueError("review persistence did not match tail review context")
    refreshed = db.get_tail_experiment(args.experiment_id)
    iteration_memory = _update_iteration_memory(review_content, refreshed or exp)
    return {
        "experiment_id": args.experiment_id,
        "outcome": outcome.to_dict(),
        "status": refreshed.get("status") if refreshed else None,
        "case_summary": refreshed.get("case_summary") if refreshed else None,
        "iteration_memory": iteration_memory,
    }


def print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare-review", help="Generate a Codex review context for the previous tail experiment")
    prepare.add_argument("--experiment-id", type=int, help="Review a specific experiment instead of resolving by date")
    prepare.add_argument("--trade-date", help="Tail experiment trade date T in YYYY-MM-DD")
    prepare.add_argument("--morning-trade-date", help="Morning metrics date in YYYY-MM-DD; default today")
    prepare.add_argument("--today", help="Override Asia/Shanghai today for tests or manual backfill")
    prepare.add_argument("--context-dir", default=str(DEFAULT_CONTEXT_DIR))
    prepare.add_argument("--no-auto-fetch", action="store_true", help="Do not fetch morning metrics automatically")
    prepare.add_argument("--force-auto-fetch", action="store_true", help="Refresh morning metrics even if rows already exist")
    prepare.add_argument("--include-reviewed", action="store_true", help="Allow regenerating context for a reviewed experiment")
    prepare.add_argument("--allow-non-trading-day", action="store_true", help="Do not skip when today is not a CN trading day")
    prepare.set_defaults(func=command_prepare_review)

    apply_review = sub.add_parser("apply-review", help="Persist a Codex-produced tail review")
    apply_review.add_argument("--experiment-id", type=int, required=True)
    apply_review.add_argument("--review-file")
    apply_review.add_argument("--review-text")
    apply_review.add_argument("--session-id")
    apply_review.set_defaults(func=command_apply_review)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    setup_env()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        payload = args.func(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False))
        return 1
    print_json(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
