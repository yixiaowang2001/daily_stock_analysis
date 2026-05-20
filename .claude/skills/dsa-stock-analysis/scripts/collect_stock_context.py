#!/usr/bin/env python3
"""Collect DSA stock facts as JSON for Agent-side analysis."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _find_repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in (current.parent, *current.parents):
        if (parent / "main.py").exists() and (parent / "AGENTS.md").exists():
            return parent
    raise RuntimeError("Could not locate DSA repository root")


REPO_ROOT = _find_repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _jsonable(value.to_dict())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item") and callable(value.item):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    return str(value)


def _frame_records(df: Any, limit: int = 10) -> List[Dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    subset = df.tail(limit).copy()
    records = subset.to_dict(orient="records")
    return _jsonable(records)


def _frame_summary(df: Any, source: str, recent_limit: int = 10) -> Dict[str, Any]:
    if df is None or getattr(df, "empty", True):
        return {
            "source": source,
            "rows": 0,
            "start_date": None,
            "end_date": None,
            "latest": None,
            "recent": [],
        }

    date_values = list(df.get("date", [])) if "date" in df else []
    start_date = date_values[0] if date_values else None
    end_date = date_values[-1] if date_values else None
    latest = _frame_records(df, limit=1)
    return {
        "source": source,
        "rows": int(len(df)),
        "start_date": _jsonable(start_date),
        "end_date": _jsonable(end_date),
        "latest": latest[0] if latest else None,
        "recent": _frame_records(df, limit=recent_limit),
    }


def _safe_json_loads(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _latest_reports(db: Any, code: str, days: int, limit: int) -> List[Dict[str, Any]]:
    reports = []
    for row in db.get_analysis_history(code=code, days=days, limit=limit):
        raw = _safe_json_loads(getattr(row, "raw_result", None)) or {}
        dashboard = raw.get("dashboard") if isinstance(raw, dict) else None
        reports.append(
            {
                "id": getattr(row, "id", None),
                "query_id": getattr(row, "query_id", None),
                "created_at": getattr(row, "created_at", None),
                "report_type": getattr(row, "report_type", None),
                "sentiment_score": getattr(row, "sentiment_score", None),
                "operation_advice": getattr(row, "operation_advice", None),
                "trend_prediction": getattr(row, "trend_prediction", None),
                "analysis_summary": getattr(row, "analysis_summary", None),
                "ideal_buy": getattr(row, "ideal_buy", None),
                "secondary_buy": getattr(row, "secondary_buy", None),
                "stop_loss": getattr(row, "stop_loss", None),
                "take_profit": getattr(row, "take_profit", None),
                "risk_warning": raw.get("risk_warning") if isinstance(raw, dict) else None,
                "current_price": raw.get("current_price") if isinstance(raw, dict) else None,
                "change_pct": raw.get("change_pct") if isinstance(raw, dict) else None,
                "dashboard_core_conclusion": (
                    dashboard.get("core_conclusion")
                    if isinstance(dashboard, dict)
                    else None
                ),
            }
        )
    return _jsonable(reports)


def _build_search_service(config: Any) -> Any:
    from src.search_service import SearchService

    return SearchService(
        bocha_keys=getattr(config, "bocha_api_keys", []),
        tavily_keys=getattr(config, "tavily_api_keys", []),
        anspire_keys=getattr(config, "anspire_api_keys", []),
        brave_keys=getattr(config, "brave_api_keys", []),
        serpapi_keys=getattr(config, "serpapi_keys", []),
        minimax_keys=getattr(config, "minimax_api_keys", []),
        searxng_base_urls=getattr(config, "searxng_base_urls", []),
        searxng_public_instances_enabled=getattr(
            config, "searxng_public_instances_enabled", True
        ),
        news_max_age_days=getattr(config, "news_max_age_days", 3),
        news_strategy_profile=getattr(config, "news_strategy_profile", "short"),
    )


def _collect_news(config: Any, code: str, name: str, max_results: int) -> Dict[str, Any]:
    try:
        service = _build_search_service(config)
        if not service.is_available:
            return {"available": False, "reason": "search service is not configured"}
        response = service.search_stock_news(code, name or code, max_results=max_results)
        return {
            "available": True,
            "query": response.query,
            "provider": response.provider,
            "success": response.success,
            "error_message": response.error_message,
            "search_time": response.search_time,
            "results": [
                {
                    "title": item.title,
                    "snippet": item.snippet,
                    "url": item.url,
                    "source": item.source,
                    "published_date": item.published_date,
                }
                for item in response.results[:max_results]
            ],
        }
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _collect_one(args: argparse.Namespace, raw_code: str, manager: Any, db: Any, config: Any) -> Dict[str, Any]:
    from data_provider.base import canonical_stock_code, normalize_stock_code
    from src.core.trading_calendar import get_market_for_stock
    from src.stock_analyzer import StockTrendAnalyzer

    code = canonical_stock_code(normalize_stock_code(raw_code))
    stock: Dict[str, Any] = {
        "input": raw_code,
        "code": code,
        "market": get_market_for_stock(code),
        "name": None,
        "errors": [],
    }

    try:
        stock["name"] = manager.get_stock_name(code, allow_realtime=False)
    except Exception as exc:
        stock["errors"].append(f"stock_name: {type(exc).__name__}: {exc}")

    quote = None
    try:
        quote = manager.get_realtime_quote(code, log_final_failure=False)
        if quote is not None and not stock.get("name") and getattr(quote, "name", None):
            stock["name"] = getattr(quote, "name")
        stock["quote"] = _jsonable(quote)
    except Exception as exc:
        stock["quote"] = None
        stock["errors"].append(f"quote: {type(exc).__name__}: {exc}")

    daily_df = None
    daily_source = "none"
    try:
        daily_df, daily_source = manager.get_daily_data(code, days=args.days)
        if args.save_db and daily_df is not None and not daily_df.empty:
            saved_count = db.save_daily_data(daily_df, code, daily_source)
            stock["daily_saved_rows"] = saved_count
    except Exception as exc:
        stock["errors"].append(f"daily: {type(exc).__name__}: {exc}")

    stock["daily"] = _frame_summary(daily_df, daily_source, recent_limit=args.recent_bars)

    try:
        if daily_df is not None and not daily_df.empty:
            trend = StockTrendAnalyzer().analyze(daily_df, code)
            stock["trend"] = _jsonable(trend)
        else:
            stock["trend"] = None
    except Exception as exc:
        stock["trend"] = None
        stock["errors"].append(f"trend: {type(exc).__name__}: {exc}")

    try:
        chip = manager.get_chip_distribution(code)
        stock["chip"] = _jsonable(chip)
    except Exception as exc:
        stock["chip"] = None
        stock["errors"].append(f"chip: {type(exc).__name__}: {exc}")

    try:
        stock["fundamental_context"] = _jsonable(
            manager.get_fundamental_context(
                code,
                budget_seconds=args.fundamental_budget_seconds,
            )
        )
    except Exception as exc:
        stock["fundamental_context"] = None
        stock["errors"].append(f"fundamental: {type(exc).__name__}: {exc}")

    try:
        stock["capital_flow_context"] = _jsonable(
            manager.get_capital_flow_context(
                code,
                budget_seconds=args.fundamental_budget_seconds,
            )
        )
    except Exception as exc:
        stock["capital_flow_context"] = None
        stock["errors"].append(f"capital_flow: {type(exc).__name__}: {exc}")

    if args.include_latest_report:
        try:
            stock["latest_reports"] = _latest_reports(
                db,
                code,
                days=args.report_days,
                limit=args.report_limit,
            )
        except Exception as exc:
            stock["latest_reports"] = []
            stock["errors"].append(f"latest_reports: {type(exc).__name__}: {exc}")

    if args.include_news:
        stock["news"] = _jsonable(
            _collect_news(
                config,
                code,
                stock.get("name") or code,
                max_results=args.news_results,
            )
        )

    return _jsonable(stock)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect DSA stock context as JSON for Agent-side analysis."
    )
    parser.add_argument("codes", nargs="+", help="Stock codes, e.g. 000021 HK00700 AAPL")
    parser.add_argument("--days", type=int, default=120, help="Daily K-line lookback days")
    parser.add_argument("--recent-bars", type=int, default=10, help="Recent bars to include")
    parser.add_argument(
        "--save-db",
        dest="save_db",
        action="store_true",
        default=False,
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
        action="store_true",
        help="Include latest saved DSA analysis reports from local DB",
    )
    parser.add_argument("--report-days", type=int, default=30, help="Latest report lookback")
    parser.add_argument("--report-limit", type=int, default=3, help="Latest report count")
    parser.add_argument(
        "--fundamental-budget-seconds",
        type=float,
        default=2.0,
        help="Per-stock budget for DSA fundamental blocks",
    )
    parser.add_argument("--compact", action="store_true", help="Print compact JSON")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)

    from src.config import get_config, setup_env

    setup_env()

    from data_provider import DataFetcherManager
    from src.storage import get_db

    config = get_config()
    db = get_db()
    manager = DataFetcherManager()
    try:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "repo_root": str(REPO_ROOT),
            "mode": "dsa_data_collection",
            "save_db": bool(args.save_db),
            "stocks": [
                _collect_one(args, raw_code, manager, db, config)
                for raw_code in args.codes
            ],
        }
        print(
            json.dumps(
                _jsonable(payload),
                ensure_ascii=False,
                indent=None if args.compact else 2,
                sort_keys=False,
            )
        )
        return 0
    finally:
        if hasattr(manager, "close"):
            manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
