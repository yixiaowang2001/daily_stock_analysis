#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex runner for multi-agent paper-trading backtests.

This script deliberately does not call the repo's built-in Agent pipeline. It
creates bounded decision contexts for Codex to read, then provides a structured
write-back path for Codex-produced decisions.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_provider.base import canonical_stock_code  # noqa: E402
from src.config import setup_env  # noqa: E402
from src.repositories.stock_repo import StockRepository  # noqa: E402
from src.services.agent_backtest_service import AgentBacktestError, AgentBacktestService  # noqa: E402
from src.services.portfolio_service import PortfolioService  # noqa: E402


PHASE_DEFAULT_TIMES = {
    "verify": "09:40:00",
    "morning": "09:40:00",
    "midday": "13:30:00",
    "tail": "14:40:00",
    "close": "16:05:00",
}
ACTIONABLE_ACTIONS = {"buy", "sell"}


def parse_symbols(value: str) -> List[str]:
    items = [item.strip() for item in (value or "").replace("，", ",").split(",")]
    symbols: List[str] = []
    for item in items:
        if not item:
            continue
        code = canonical_stock_code(item)
        if code not in symbols:
            symbols.append(code)
    return symbols


def parse_date(value: Optional[str], *, default: Optional[date] = None) -> Optional[date]:
    if not value:
        return default
    return date.fromisoformat(value)


def parse_dt_for_trade_date(trade_date: date, intraday_time: str) -> datetime:
    text = (intraday_time or "").strip()
    if "T" in text or " " in text:
        return datetime.fromisoformat(text)
    parts = text.split(":")
    if len(parts) < 2:
        raise ValueError("time must be HH:MM or HH:MM:SS")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) > 2 else 0
    return datetime(trade_date.year, trade_date.month, trade_date.day, hour, minute, second)


def safe_json_loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


class RunnerContextBuilder:
    """Build per-profile Codex decision contexts with profile isolation."""

    def __init__(
        self,
        *,
        service: AgentBacktestService,
        portfolio_service: PortfolioService,
        stock_repo: StockRepository,
    ) -> None:
        self.service = service
        self.portfolio_service = portfolio_service
        self.stock_repo = stock_repo

    def build_cycle(
        self,
        *,
        run_id: int,
        phase: str,
        trade_date: date,
        observation_time: str,
        context_dir: Path,
        live_data: bool = False,
        record_observation: bool = True,
    ) -> Dict[str, Any]:
        run = self.service.get_run(run_id)
        context_root = context_dir / f"run_{run_id}" / trade_date.isoformat() / phase
        context_root.mkdir(parents=True, exist_ok=True)

        if phase == "close":
            nav = self.service.record_daily_nav(run_id=run_id, trade_date=trade_date)
        else:
            nav = None

        generated: List[Dict[str, Any]] = []
        cutoff = parse_dt_for_trade_date(trade_date, observation_time)
        public_run = {key: value for key, value in run.items() if key != "profiles"}
        for profile in run.get("profiles") or []:
            profile_key = profile["profile_key"]
            evidence = self._collect_evidence(
                run=run,
                profile=profile,
                trade_date=trade_date,
                phase=phase,
                data_cutoff_at=cutoff,
                live_data=live_data,
            )
            observation = None
            if record_observation and phase != "close":
                observation = self.service.record_observation(
                    run_id=run_id,
                    profile_key=profile_key,
                    trade_date=trade_date,
                    observation_time=observation_time,
                    data_cutoff_at=cutoff,
                    symbols=run.get("symbols") or [],
                    evidence=evidence,
                    summary=f"{phase} cycle context generated for Codex decision.",
                )

            context_payload = {
                "run": public_run,
                "profile": profile,
                "phase": phase,
                "trade_date": trade_date.isoformat(),
                "observation_time": observation_time,
                "data_cutoff_at": cutoff.isoformat(),
                "observation": observation,
                "evidence": evidence,
                "output_contract": self._decision_contract(),
            }
            stem = f"{profile_key}_context"
            json_path = context_root / f"{stem}.json"
            md_path = context_root / f"{stem}.md"
            json_path.write_text(
                json.dumps(context_payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            md_path.write_text(self._render_markdown_context(context_payload), encoding="utf-8")
            generated.append(
                {
                    "profile_key": profile_key,
                    "observation_id": observation.get("id") if observation else None,
                    "context_json": str(json_path),
                    "context_markdown": str(md_path),
                }
            )

        return {
            "run_id": run_id,
            "phase": phase,
            "trade_date": trade_date.isoformat(),
            "observation_time": observation_time,
            "context_dir": str(context_root),
            "live_data": live_data,
            "record_observation": record_observation,
            "generated": generated,
            "daily_nav": nav,
            "next_step": (
                "Codex should read each context_markdown independently and call apply-decision for each profile."
                if phase != "close"
                else "Close phase recorded daily NAV; review daily_nav for report."
            ),
        }

    def _collect_evidence(
        self,
        *,
        run: Dict[str, Any],
        profile: Dict[str, Any],
        trade_date: date,
        phase: str,
        data_cutoff_at: datetime,
        live_data: bool,
    ) -> Dict[str, Any]:
        account_id = int(profile["account_id"])
        snapshot = self.portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=trade_date,
            cost_method="fifo",
        )
        own_events = self.service.list_events(
            run_id=int(run["id"]),
            profile_key=profile["profile_key"],
            limit=20,
        )
        symbols = run.get("symbols") or []
        symbol_facts = {
            symbol: self._symbol_facts(symbol=symbol, live_data=live_data)
            for symbol in symbols
        }
        return {
            "source": "codex_runner",
            "phase": phase,
            "data_cutoff_at": data_cutoff_at.isoformat(),
            "visibility": {
                "profile_scope": profile["profile_key"],
                "other_profiles_hidden": True,
                "decision_source": "codex_scheduled",
            },
            "portfolio_snapshot": snapshot,
            "own_recent_events": own_events,
            "symbol_facts": symbol_facts,
            "data_quality": {
                "live_data_enabled": live_data,
                "note": "Realtime quote fetch is fail-open; use DB daily bars and explicit gaps when live data is unavailable.",
            },
        }

    def _symbol_facts(self, *, symbol: str, live_data: bool) -> Dict[str, Any]:
        latest_rows = self.stock_repo.get_latest(symbol, days=20)
        daily = [
            {
                "date": row.date.isoformat() if row.date else None,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "amount": row.amount,
                "pct_chg": row.pct_chg,
            }
            for row in reversed(latest_rows)
        ]
        realtime: Dict[str, Any]
        if live_data:
            realtime = self._fetch_realtime_quote(symbol)
        else:
            realtime = {"skipped": True, "reason": "live_data_disabled"}
        return {
            "symbol": symbol,
            "daily_bars": daily,
            "realtime_quote": realtime,
        }

    @staticmethod
    def _fetch_realtime_quote(symbol: str) -> Dict[str, Any]:
        try:
            from data_provider.base import DataFetcherManager

            quote = DataFetcherManager().get_realtime_quote(symbol)
        except Exception as exc:
            return {"error": str(exc), "source": "realtime_quote", "retriable": True}
        if quote is None:
            return {"error": "No realtime quote available", "source": "realtime_quote", "retriable": True}
        return quote.to_dict() if hasattr(quote, "to_dict") else dict(quote)

    @staticmethod
    def _decision_contract() -> Dict[str, Any]:
        return {
            "required_json_fields": [
                "action",
                "rationale",
                "risk_notes",
                "confidence",
            ],
            "action_values": ["observe", "hold", "buy", "sell", "cancel"],
            "buy_sell_required_fields": [
                "symbol",
                "side",
                "quantity",
                "order_type",
                "limit_price",
                "submitted_at",
                "effective_at",
            ],
            "a_share_constraints": [
                "Only trade symbols in run.symbols.",
                "Quantity must be a positive multiple of 100.",
                "No same-day sell for shares bought today.",
                "Use only evidence at or before data_cutoff_at.",
            ],
        }

    @staticmethod
    def _render_markdown_context(payload: Dict[str, Any]) -> str:
        run = payload["run"]
        profile = payload["profile"]
        observation = payload.get("observation") or {}
        evidence = payload["evidence"]
        lines = [
            "# Codex Agent Backtest Decision Context",
            "",
            "You are the only decision maker for this isolated profile. Do not use or infer other profiles' decisions.",
            "",
            "## Run",
            f"- run_id: {run['id']}",
            f"- symbols: {', '.join(run.get('symbols') or [])}",
            f"- rule_version: {run.get('rule_version')}",
            f"- max_observations_per_day: {run.get('max_observations_per_day')}",
            "",
            "## Profile",
            f"- profile_key: {profile['profile_key']}",
            f"- display_name: {profile['display_name']}",
            f"- style_profile: {profile['style_profile']}",
            f"- policy_version_label: {profile['policy_version_label']}",
            f"- context_namespace: {profile['context_namespace']}",
            "",
            "## Current Policy",
            profile.get("policy_markdown") or "(empty)",
            "",
            "## Timing",
            f"- phase: {payload['phase']}",
            f"- trade_date: {payload['trade_date']}",
            f"- observation_time: {payload['observation_time']}",
            f"- data_cutoff_at: {payload['data_cutoff_at']}",
            f"- observation_id: {observation.get('id')}",
            "",
            "## Evidence JSON",
            "```json",
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## Required Decision JSON",
            "Return one JSON object. For observe/hold, omit order fields. For buy/sell, include all order fields.",
            "```json",
            json.dumps(payload["output_contract"], ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
        return "\n".join(lines) + "\n"


def command_prepare(args: argparse.Namespace) -> Dict[str, Any]:
    service = AgentBacktestService()
    run = service.create_run(
        name=args.name,
        symbols=parse_symbols(args.symbols),
        start_date=parse_date(args.start_date),
        end_date=parse_date(args.end_date),
        initial_cash_per_agent=args.initial_cash_per_agent,
        max_observations_per_day=args.max_observations_per_day,
    )
    return {
        "created": True,
        "run": run,
        "next_step": f"Run: python scripts/run_agent_backtest_cycle.py cycle --run-id {run['id']} --phase verify",
    }


def command_cycle(args: argparse.Namespace) -> Dict[str, Any]:
    service = AgentBacktestService()
    builder = RunnerContextBuilder(
        service=service,
        portfolio_service=PortfolioService(),
        stock_repo=StockRepository(),
    )
    trade_date = parse_date(args.trade_date, default=date.today())
    if trade_date is None:
        raise ValueError("trade_date is required")
    observation_time = args.observation_time or PHASE_DEFAULT_TIMES[args.phase]
    return builder.build_cycle(
        run_id=args.run_id,
        phase=args.phase,
        trade_date=trade_date,
        observation_time=observation_time,
        context_dir=Path(args.context_dir),
        live_data=args.live_data,
        record_observation=not args.no_record_observation,
    )


def command_apply_decision(args: argparse.Namespace) -> Dict[str, Any]:
    service = AgentBacktestService()
    decision = load_decision_payload(args)
    run = service.get_run(args.run_id)
    profile = next((p for p in run.get("profiles") or [] if p["profile_key"] == args.profile_key), None)
    if profile is None:
        raise AgentBacktestError(f"profile not found: {args.profile_key}")

    trade_date = parse_date(decision.get("trade_date") or args.trade_date, default=date.today())
    if trade_date is None:
        raise AgentBacktestError("trade_date is required")
    decision_time = parse_decision_datetime(decision.get("decision_time") or args.decision_time, trade_date)
    action = str(decision.get("action") or "observe").strip().lower()
    symbol = decision.get("symbol")
    side = decision.get("side")
    quantity = decision.get("quantity")
    order_type = decision.get("order_type")
    limit_price = decision.get("limit_price")
    observation_id = decision.get("observation_id") or args.observation_id

    decision_row = service.record_decision(
        run_id=args.run_id,
        profile_key=args.profile_key,
        observation_id=observation_id,
        trade_date=trade_date,
        decision_time=decision_time,
        action=action,
        symbol=symbol,
        side=side,
        quantity=float(quantity) if quantity is not None else None,
        order_type=order_type,
        limit_price=float(limit_price) if limit_price is not None else None,
        confidence=float(decision["confidence"]) if decision.get("confidence") is not None else None,
        rationale=decision.get("rationale"),
        risk_notes=decision.get("risk_notes"),
        policy_version_label=decision.get("policy_version_label") or profile.get("policy_version_label"),
        raw_output=decision,
    )
    order_row = None
    if action in ACTIONABLE_ACTIONS:
        submitted_at = parse_decision_datetime(decision.get("submitted_at"), trade_date)
        effective_at = parse_decision_datetime(decision.get("effective_at"), trade_date)
        order_row = service.create_order(
            run_id=args.run_id,
            profile_key=args.profile_key,
            decision_id=decision_row["id"],
            symbol=str(symbol or ""),
            side=str(side or action),
            requested_quantity=float(quantity),
            order_type=str(order_type or "limit"),
            limit_price=float(limit_price) if limit_price is not None else None,
            submitted_at=submitted_at,
            effective_at=effective_at,
        )
    return {
        "run_id": args.run_id,
        "profile_key": args.profile_key,
        "decision": decision_row,
        "order": order_row,
    }


def command_daily_nav(args: argparse.Namespace) -> Dict[str, Any]:
    trade_date = parse_date(args.trade_date, default=date.today())
    if trade_date is None:
        raise ValueError("trade_date is required")
    return AgentBacktestService().record_daily_nav(run_id=args.run_id, trade_date=trade_date)


def command_fill_order(args: argparse.Namespace) -> Dict[str, Any]:
    trade_date = parse_date(args.trade_date, default=date.today())
    if trade_date is None:
        raise ValueError("trade_date is required")
    filled_at = parse_decision_datetime(args.filled_at, trade_date)
    fill = AgentBacktestService().record_fill(
        run_id=args.run_id,
        order_id=args.order_id,
        quantity=args.quantity,
        price=args.price,
        filled_at=filled_at,
        fee=args.fee,
        tax=args.tax,
        source=args.source,
    )
    return {"run_id": args.run_id, "order_id": args.order_id, "fill": fill}


def load_decision_payload(args: argparse.Namespace) -> Dict[str, Any]:
    if args.decision_json:
        return json.loads(args.decision_json)
    if args.decision_file:
        if args.decision_file == "-":
            return json.loads(sys.stdin.read())
        return json.loads(Path(args.decision_file).read_text(encoding="utf-8"))
    raise AgentBacktestError("decision_json or decision_file is required")


def parse_decision_datetime(value: Optional[str], trade_date: date) -> datetime:
    if value:
        return parse_dt_for_trade_date(trade_date, value)
    now = datetime.now()
    if now.date() == trade_date:
        return now.replace(microsecond=0)
    return datetime(trade_date.year, trade_date.month, trade_date.day, 14, 40)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex runner for agent backtest cycles")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Create a run with isolated short/medium/long profiles")
    prepare.add_argument("--name", required=True)
    prepare.add_argument("--symbols", required=True, help="Comma-separated A-share symbols")
    prepare.add_argument("--start-date")
    prepare.add_argument("--end-date")
    prepare.add_argument("--initial-cash-per-agent", type=float, default=20000.0)
    prepare.add_argument("--max-observations-per-day", type=int, default=3)
    prepare.set_defaults(func=command_prepare)

    cycle = sub.add_parser("cycle", help="Generate isolated Codex decision contexts for one phase")
    cycle.add_argument("--run-id", type=int, required=True)
    cycle.add_argument("--phase", choices=sorted(PHASE_DEFAULT_TIMES), required=True)
    cycle.add_argument("--trade-date")
    cycle.add_argument("--observation-time")
    cycle.add_argument("--context-dir", default=str(ROOT / ".claude" / "reviews" / "agent_backtest"))
    cycle.add_argument("--live-data", action="store_true", help="Fetch live realtime quotes fail-open")
    cycle.add_argument("--no-record-observation", action="store_true")
    cycle.set_defaults(func=command_cycle)

    apply_decision = sub.add_parser("apply-decision", help="Persist one Codex-produced decision and optional order")
    apply_decision.add_argument("--run-id", type=int, required=True)
    apply_decision.add_argument("--profile-key", required=True)
    apply_decision.add_argument("--trade-date")
    apply_decision.add_argument("--decision-time")
    apply_decision.add_argument("--observation-id", type=int)
    apply_decision.add_argument("--decision-json")
    apply_decision.add_argument("--decision-file")
    apply_decision.set_defaults(func=command_apply_decision)

    daily_nav = sub.add_parser("daily-nav", help="Record daily NAV snapshots")
    daily_nav.add_argument("--run-id", type=int, required=True)
    daily_nav.add_argument("--trade-date")
    daily_nav.set_defaults(func=command_daily_nav)

    fill_order = sub.add_parser("fill-order", help="Record a simulated fill for an order")
    fill_order.add_argument("--run-id", type=int, required=True)
    fill_order.add_argument("--order-id", type=int, required=True)
    fill_order.add_argument("--quantity", type=float, required=True)
    fill_order.add_argument("--price", type=float, required=True)
    fill_order.add_argument("--trade-date")
    fill_order.add_argument("--filled-at")
    fill_order.add_argument("--fee", type=float)
    fill_order.add_argument("--tax", type=float)
    fill_order.add_argument("--source", default="manual")
    fill_order.set_defaults(func=command_fill_order)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    setup_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.func(args)
    except Exception as exc:
        print_json({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return 1
    print_json({"ok": True, **payload})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
