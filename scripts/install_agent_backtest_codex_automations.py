#!/usr/bin/env python3
"""Install Codex recurring automations for the DSA agent backtest."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CODEX_HOME = Path.home() / ".codex"
DEFAULT_MODEL = "gpt-5-codex"
DEFAULT_REASONING_EFFORT = "high"
TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class AutomationSpec:
    automation_id: str
    name: str
    phase: str
    hour: int
    minute: int
    prompt: str


def _review_prompt(*, run_id: int, phase: str, label: str) -> str:
    return f"""Run the DSA Codex paper-trading {label} cycle for agent backtest run_id={run_id}. Use the current Asia/Shanghai date as trade_date. If today is clearly not an A-share trading day, skip without creating observations, decisions, orders, or NAV rows.

Generate isolated contexts with:
python scripts/run_agent_backtest_cycle.py cycle --run-id {run_id} --phase {phase} --trade-date <YYYY-MM-DD> --live-data

Read each generated context_markdown independently for every active profile returned by the cycle command. Act as the sole decision maker for each profile, keep profile contexts isolated, and do not use or infer other profiles' decisions. For each generated profile, produce exactly one decision JSON that follows the context contract; observe/hold with no trade is valid when that profile's strategy calls for waiting. Persist it with:
python scripts/run_agent_backtest_cycle.py apply-decision --run-id {run_id} --profile-key <profile_key> --trade-date <YYYY-MM-DD> --decision-file <decision_json_file>

Before deciding for a profile, inspect evidence.symbol_facts[*].information_context, fundamental_context, data_quality, and codex_research_fallback. If any watched or held symbol has codex_research_fallback.status="recommended", use Codex's available web/search/browser tooling as a labeled external public-evidence source for the listed gaps, including news, announcements, fundamentals, valuation, or public fund-flow clues. For broad watchlists, triage held symbols, likely buy/sell candidates, and large movers first; record unresolved lower-impact gaps instead of doing unfocused broad searches. Prefer exchange/company announcements, financial reports, and reputable timestamped financial media, use only information available at or before data_cutoff_at, and include source title/date/URL in rationale, risk_notes, and the final Chinese summary when used. If search tooling is unavailable, record the information gap explicitly instead of treating it as clean evidence. Do not write Codex supplemental facts back into DSA provider fields; label them as Codex 外部兜底.

Respect A-share constraints: buy/add only symbols in evidence.symbol_scope.buy_allowed_symbols; symbols in evidence.symbol_scope.exit_only_symbols may be researched, held, reduced, or sold but not bought/added; buy/sell quantity must be a positive multiple of 100; no same-day sell for shares bought today; no short selling; and use only evidence at or before data_cutoff_at. If a buy/sell order is created, record a simulated fill only when there is reliable executable price evidence at or before data_cutoff_at; otherwise leave it unfilled and explain. End with a concise Chinese summary covering each trader's action, rationale, order/fill status, data gaps, and main risk."""


def _close_prompt(*, run_id: int) -> str:
    return f"""Run the DSA Codex paper-trading close review cycle for agent backtest run_id={run_id}. Use the current Asia/Shanghai date as trade_date. If today is clearly not an A-share trading day, skip without creating observations, decisions, orders, policy versions, or NAV rows.

Record the close NAV snapshots with:
python scripts/run_agent_backtest_cycle.py cycle --run-id {run_id} --phase close --trade-date <YYYY-MM-DD> --live-data

Do not create buy/sell/hold decisions or orders in this close task. Read each generated close context_markdown independently for every active profile. For each profile, write a concise Chinese self-review markdown file under the generated close context directory, covering today's actions, missed opportunities, risk discipline, data-quality limits, and the next-session focus. Use the close self-review contract in the context.

When a close context marks codex_research_fallback.status="recommended" for a held or watched symbol, use Codex's available web/search/browser tooling as a labeled external public-evidence source before writing the self-review. For broad watchlists, triage held symbols, likely strategy lessons, and large movers first. Respect data_cutoff_at, prefer official announcements, financial reports, and reputable timestamped financial media, cite source title/date/URL when used, and record the gap if search tooling is unavailable. Do not write Codex supplemental facts back into DSA provider fields; label them as Codex 外部兜底.

If the profile finds a durable strategy lesson, create a forward-only policy JSON with version_label, body_markdown, effective_from, and change_reason, then persist it with:
python scripts/run_agent_backtest_cycle.py evolve-policy --run-id {run_id} --profile-key <profile_key> --policy-file <policy_json_file>

Do not update policy just to react to one noisy day; keep the profile's style boundary intact. Review the returned daily_nav payload for every active profile and cross-check each open position's last_price, valuation_date, valuation_source, and valuation_stale against today's fills/orders and close_price_overrides. If valuation_stale is true or a held symbol has no reliable same-day price, do not rank that trader as if the NAV were final; explain the stale valuation risk and any estimated mark-to-market separately. End with a concise Chinese summary comparing all active traders by ending equity, daily PnL, return, self-review conclusions, and any policy versions created."""


def build_specs(run_id: int) -> List[AutomationSpec]:
    return [
        AutomationSpec(
            automation_id="dsa-agent-backtest-morning",
            name="0940看盘",
            phase="morning",
            hour=9,
            minute=40,
            prompt=_review_prompt(run_id=run_id, phase="morning", label="morning"),
        ),
        AutomationSpec(
            automation_id="dsa-agent-backtest-late-morning",
            name="1030看盘",
            phase="late_morning",
            hour=10,
            minute=30,
            prompt=_review_prompt(run_id=run_id, phase="late_morning", label="late-morning"),
        ),
        AutomationSpec(
            automation_id="dsa-agent-backtest-pre-noon",
            name="1120看盘",
            phase="pre_noon",
            hour=11,
            minute=20,
            prompt=_review_prompt(run_id=run_id, phase="pre_noon", label="pre-noon"),
        ),
        AutomationSpec(
            automation_id="dsa-agent-backtest-midday",
            name="1335看盘",
            phase="midday",
            hour=13,
            minute=35,
            prompt=_review_prompt(run_id=run_id, phase="midday", label="midday"),
        ),
        AutomationSpec(
            automation_id="dsa-agent-backtest-tail",
            name="1440看盘",
            phase="tail",
            hour=14,
            minute=40,
            prompt=_review_prompt(run_id=run_id, phase="tail", label="tail-session"),
        ),
        AutomationSpec(
            automation_id="dsa-agent-backtest-close",
            name="收盘复盘",
            phase="close",
            hour=16,
            minute=5,
            prompt=_close_prompt(run_id=run_id),
        ),
    ]


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_automation(
    spec: AutomationSpec,
    *,
    repo_root: Path,
    model: str,
    reasoning_effort: str,
    status: str,
    start_date: str,
    now_ms: int,
) -> str:
    start_time = f"{spec.hour:02d}{spec.minute:02d}00"
    rrule = f"DTSTART:{start_date}T{start_time}\nRRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    cwd = str(repo_root.resolve())
    return "\n".join(
        [
            "version = 1",
            f"id = {_toml_string(spec.automation_id)}",
            'kind = "cron"',
            f"name = {_toml_string(spec.name)}",
            "prompt = '''",
            spec.prompt,
            "'''",
            f"status = {_toml_string(status)}",
            f"rrule = {_toml_string(rrule)}",
            f"model = {_toml_string(model)}",
            f"reasoning_effort = {_toml_string(reasoning_effort)}",
            'execution_environment = "local"',
            f"cwds = [{_toml_string(cwd)}]",
            f"created_at = {now_ms}",
            f"updated_at = {now_ms}",
            "",
        ]
    )


def _write_automation(
    spec: AutomationSpec,
    *,
    output_root: Path,
    content: str,
    force: bool,
    dry_run: bool,
) -> str:
    target_dir = output_root / spec.automation_id
    automation_file = target_dir / "automation.toml"
    memory_file = target_dir / "memory.md"
    if automation_file.exists() and not force:
        return f"skip existing {automation_file}; pass --force to overwrite"
    if dry_run:
        return f"would write {automation_file}"
    target_dir.mkdir(parents=True, exist_ok=True)
    automation_file.write_text(content, encoding="utf-8")
    if not memory_file.exists() or force:
        memory_file.write_text(
            f"No completed runs yet. This automation runs DSA agent backtest phase {spec.phase}.\n",
            encoding="utf-8",
        )
    return f"wrote {automation_file}"


def install(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).expanduser().resolve()
    output_root = Path(args.codex_home).expanduser() / "automations"
    now_ms = int(time.time() * 1000)
    start_date = args.start_date or datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y%m%d")
    messages: List[str] = []

    for spec in build_specs(args.run_id):
        content = _render_automation(
            spec,
            repo_root=repo_root,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            status=args.status,
            start_date=start_date,
            now_ms=now_ms,
        )
        messages.append(
            _write_automation(
                spec,
                output_root=output_root,
                content=content,
                force=args.force,
                dry_run=args.dry_run,
            )
        )

    for message in messages:
        print(message)
    if not args.dry_run:
        print("Restart Codex Desktop if the new automations are not visible immediately.")
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, default=1, help="Agent backtest run id, default 1")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="DSA repository root")
    parser.add_argument("--codex-home", default=str(DEFAULT_CODEX_HOME), help="Codex home directory")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Codex model name")
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--status", choices=["ACTIVE", "PAUSED"], default="ACTIVE")
    parser.add_argument("--start-date", help="RRULE DTSTART date in YYYYMMDD, default today in Asia/Shanghai")
    parser.add_argument("--force", action="store_true", help="Overwrite existing automation.toml files")
    parser.add_argument("--dry-run", action="store_true", help="Print target paths without writing files")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    return install(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
