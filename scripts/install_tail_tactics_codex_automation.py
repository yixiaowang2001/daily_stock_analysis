#!/usr/bin/env python3
"""Install the Codex recurring automation for DSA tail tactics reviews."""

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
    hour: int
    minute: int
    prompt: str


def _review_prompt() -> str:
    return """Run the DSA tail tactics next-day morning review. Use the current Asia/Shanghai date as today.

This task reviews the previous A-share trading day's saved tail-session experiment after the morning session closes at 11:30. If today is not an A-share trading day, or there is no unreviewed ranked tail experiment for the previous A-share trading day, skip without changing data and explain briefly in Chinese.

Prepare the review context with:
python scripts/run_tail_tactics_codex_review.py prepare-review

Read the JSON result. If it contains `skipped: true`, stop after a concise Chinese summary of the reason. Otherwise, open `context_markdown` and write a Chinese review that:
- compares the saved tail_score_result / opening forecast with the stored 09:30-10:00 morning metrics;
- separates Layer 1 Tonghuashun candidate-pool rule changes from Layer 2 Agent evaluation/forecast-weight changes;
- treats Layer 1 changes as requests for the user to approve, never as automatic updates;
- writes Layer 2 Agent scoring/forecast calibration as self-iteration notes to apply in later scoring;
- does not invent missing market data and marks metric sources or fallbacks;
- ends with a pure JSON object containing `tail_review_suggestions.case_summary`, optional `layer2_calibration_notes`, and optional `layer1_change_requests`.

Save the review to a Markdown file under the same `.claude/reviews/tail_tactics/<trade_date>/` directory, then persist it with:
python scripts/run_tail_tactics_codex_review.py apply-review --experiment-id <experiment_id> --review-file <review_md_file>

End with a concise Chinese summary covering the reviewed experiment id, symbols, morning metric sources, whether the review was saved, Layer 2 calibration notes appended, any Layer 1 requests pending user approval, and data gaps."""


def build_specs() -> List[AutomationSpec]:
    return [
        AutomationSpec(
            automation_id="dsa-tail-tactics-midday-review",
            name="尾盘 · 上午收盘复盘",
            hour=11,
            minute=30,
            prompt=_review_prompt(),
        )
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
            "No completed runs yet. This automation runs DSA tail tactics morning reviews.\n",
            encoding="utf-8",
        )
    return f"wrote {automation_file}"


def install(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).expanduser().resolve()
    output_root = Path(args.codex_home).expanduser() / "automations"
    now_ms = int(time.time() * 1000)
    start_date = args.start_date or datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y%m%d")
    messages: List[str] = []

    for spec in build_specs():
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
        print("Restart Codex Desktop if the new automation is not visible immediately.")
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="DSA repository root")
    parser.add_argument("--codex-home", default=str(DEFAULT_CODEX_HOME), help="Codex home directory")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Codex model name")
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--status", choices=["ACTIVE", "PAUSED"], default="ACTIVE")
    parser.add_argument("--start-date", help="RRULE DTSTART date in YYYYMMDD, default today in Asia/Shanghai")
    parser.add_argument("--force", action="store_true", help="Overwrite existing automation.toml")
    parser.add_argument("--dry-run", action="store_true", help="Print target paths without writing files")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    return install(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
