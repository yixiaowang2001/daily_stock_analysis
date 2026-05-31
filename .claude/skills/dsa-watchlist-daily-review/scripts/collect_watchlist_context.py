#!/usr/bin/env python3
"""Collect DSA watchlist facts for after-close Agent review."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

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


def _read_watchlist(path: Optional[str]) -> List[str]:
    if not path:
        return []
    text = Path(path).expanduser().read_text(encoding="utf-8")
    codes: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        for part in line.replace(",", " ").replace(";", " ").split():
            item = part.strip()
            if item:
                codes.append(item)
    return codes


def _dedupe(items: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in items:
        normalized = item.strip()
        if not normalized:
            continue
        key = normalized.upper()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _parse_trade_date(value: Optional[str]) -> date:
    if value:
        return date.fromisoformat(value[:10])
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _collect_base_context(args: argparse.Namespace, codes: List[str]) -> Dict[str, Any]:
    collector = REPO_ROOT / ".claude" / "skills" / "dsa-stock-analysis" / "scripts" / "collect_stock_context.py"
    if not collector.exists():
        raise FileNotFoundError(f"base collector is missing: {collector}")

    cmd = [
        sys.executable,
        str(collector),
        *codes,
        "--days",
        str(args.days),
        "--recent-bars",
        str(args.recent_bars),
        "--fundamental-budget-seconds",
        str(args.fundamental_budget_seconds),
    ]
    cmd.append("--save-db" if args.save_db else "--no-save-db")
    if args.include_news:
        cmd.extend(["--include-news", "--news-results", str(args.news_results)])
    if args.include_latest_report:
        cmd.extend(
            [
                "--include-latest-report",
                "--report-days",
                str(args.report_days),
                "--report-limit",
                str(args.report_limit),
            ]
        )
    if args.compact:
        cmd.append("--compact")

    env = os.environ.copy()
    env.setdefault("DSA_REPO_ROOT", str(REPO_ROOT))
    completed = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _date_value(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _float_value(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _previous_close(stock: Dict[str, Any], trade_date: date) -> Optional[float]:
    daily = stock.get("daily") if isinstance(stock.get("daily"), dict) else {}
    bars = daily.get("recent") if isinstance(daily.get("recent"), list) else []
    candidates: List[Dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        bar_date = _date_value(bar.get("date"))
        close = _float_value(bar.get("close"))
        if bar_date is not None and close is not None and bar_date < trade_date:
            candidates.append({"date": bar_date, "close": close})
    if not candidates:
        return None
    candidates.sort(key=lambda item: item["date"])
    return candidates[-1]["close"]


def _append_unique(items: List[str], value: str) -> List[str]:
    if value not in items:
        items.append(value)
    return items


def _merge_codex_research_fallback(
    stock: Dict[str, Any],
    *,
    reason: str,
    scope: str,
) -> None:
    fallback = stock.get("codex_research_fallback")
    if not isinstance(fallback, dict):
        fallback = {
            "enabled": True,
            "needed": False,
            "reasons": [],
            "allowed_scopes": [],
        }

    reasons = fallback.get("reasons") if isinstance(fallback.get("reasons"), list) else []
    scopes = (
        fallback.get("allowed_scopes")
        if isinstance(fallback.get("allowed_scopes"), list)
        else []
    )
    fallback["reasons"] = _append_unique([str(item) for item in reasons], reason)
    fallback["allowed_scopes"] = _append_unique([str(item) for item in scopes], scope)
    fallback["needed"] = bool(fallback["reasons"])
    fallback["enabled"] = True
    fallback.setdefault(
        "policy",
        (
            "Codex may use external web/browser/finance research as fallback when DSA "
            "providers cannot supply usable facts. Label source, timestamp, and cutoff "
            "explicitly, and keep fallback facts separate from DSA-collected facts."
        ),
    )
    stock["codex_research_fallback"] = fallback


def _attach_intraday(payload: Dict[str, Any], *, trade_date: date, cutoff_time: str) -> None:
    from src.services.tail_intraday_fetch import fetch_tail_intraday_cutoff_evidence

    for stock in payload.get("stocks", []):
        if not isinstance(stock, dict):
            continue
        code = str(stock.get("code") or stock.get("input") or "").strip()
        if not code:
            continue
        try:
            evidence = fetch_tail_intraday_cutoff_evidence(
                symbol=code,
                trade_date=trade_date,
                selection_cutoff=cutoff_time,
                previous_close=_previous_close(stock, trade_date),
            )
            stock["post_close_intraday_evidence"] = evidence
            if not isinstance(evidence, dict) or evidence.get("available") is False:
                _merge_codex_research_fallback(
                    stock,
                    reason="minute_evidence_missing",
                    scope="intraday price and volume cross-check",
                )
        except Exception as exc:
            stock["post_close_intraday_evidence"] = {
                "code": code,
                "available": False,
                "missing_reason": f"{type(exc).__name__}: {exc}",
                "fields": {},
                "fallback_attempts": [],
            }
            _merge_codex_research_fallback(
                stock,
                reason="minute_evidence_missing",
                scope="intraday price and volume cross-check",
            )


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect DSA watchlist context for an after-close Agent review."
    )
    parser.add_argument("codes", nargs="*", help="Stock codes, e.g. 000021 HK00700 AAPL")
    parser.add_argument(
        "--watchlist-file",
        help="Optional text file with codes separated by commas, spaces, semicolons, or newlines",
    )
    parser.add_argument("--trade-date", help="Trade date for intraday evidence, YYYY-MM-DD")
    parser.add_argument("--cutoff-time", default="15:00", help="Minute evidence cutoff, HH:MM")
    parser.add_argument("--days", type=int, default=160, help="Daily K-line lookback days")
    parser.add_argument("--recent-bars", type=int, default=15, help="Recent bars to include")
    parser.add_argument(
        "--save-db",
        dest="save_db",
        action="store_true",
        default=True,
        help="Save fetched daily bars into DSA's local stock_daily table",
    )
    parser.add_argument(
        "--no-save-db",
        dest="save_db",
        action="store_false",
        help="Do not write fetched bars to the local DB",
    )
    parser.add_argument(
        "--include-news",
        action="store_true",
        help="Use configured DSA SearchService providers for recent news",
    )
    parser.add_argument("--news-results", type=int, default=5, help="Max news results")
    parser.add_argument(
        "--include-latest-report",
        dest="include_latest_report",
        action="store_true",
        default=True,
        help="Include latest saved DSA analysis reports from local DB",
    )
    parser.add_argument(
        "--no-latest-report",
        dest="include_latest_report",
        action="store_false",
        help="Do not include latest saved DSA reports",
    )
    parser.add_argument("--report-days", type=int, default=30, help="Latest report lookback")
    parser.add_argument("--report-limit", type=int, default=3, help="Latest report count")
    parser.add_argument(
        "--fundamental-budget-seconds",
        type=float,
        default=2.0,
        help="Per-stock budget for DSA fundamental blocks",
    )
    parser.add_argument(
        "--no-intraday",
        dest="include_intraday",
        action="store_false",
        help="Skip A-share minute evidence fetch",
    )
    parser.add_argument("--compact", action="store_true", help="Print compact JSON")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    codes = _dedupe([*args.codes, *_read_watchlist(args.watchlist_file)])
    if not codes:
        raise SystemExit("No stock codes provided. Pass codes or --watchlist-file.")

    os.chdir(REPO_ROOT)
    trade_date = _parse_trade_date(args.trade_date)
    payload = _collect_base_context(args, codes)
    if args.include_intraday:
        _attach_intraday(payload, trade_date=trade_date, cutoff_time=args.cutoff_time)

    payload["watchlist_review"] = {
        "mode": "after_close_watchlist_context",
        "trade_date": trade_date.isoformat(),
        "timezone": "Asia/Shanghai",
        "minute_cutoff_time": args.cutoff_time,
        "intraday_included": bool(args.include_intraday),
        "horizons": {
            "short": "1-3 trading days",
            "medium": "1-4 weeks",
            "long": "one quarter or longer",
        },
        "source_policy": (
            "DSA provides evidence; Agent owns judgment, rankings, price zones, "
            "risk penalties, and persistence decisions."
        ),
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
