import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.install_agent_backtest_codex_automations import install, parse_args


class AgentBacktestAutomationInstallerTestCase(unittest.TestCase):
    def test_installs_five_decision_codex_automations_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            repo_root = root / "repo"
            repo_root.mkdir()

            args = parse_args(
                [
                    "--run-id",
                    "42",
                    "--repo-root",
                    str(repo_root),
                    "--codex-home",
                    str(codex_home),
                    "--start-date",
                    "20260528",
                ]
            )

            self.assertEqual(install(args), 0)

            expected_ids = {
                "dsa-agent-backtest-morning",
                "dsa-agent-backtest-late-morning",
                "dsa-agent-backtest-pre-noon",
                "dsa-agent-backtest-midday",
                "dsa-agent-backtest-tail",
                "dsa-agent-backtest-close",
            }
            actual_ids = {
                path.parent.name
                for path in (codex_home / "automations").glob("*/automation.toml")
            }
            self.assertEqual(actual_ids, expected_ids)

            morning = (
                codex_home
                / "automations"
                / "dsa-agent-backtest-morning"
                / "automation.toml"
            ).read_text(encoding="utf-8")
            self.assertIn('id = "dsa-agent-backtest-morning"', morning)
            self.assertIn('name = "0940看盘"', morning)
            self.assertIn("run_id=42", morning)
            self.assertIn("--run-id 42 --phase morning", morning)
            self.assertIn("every active profile", morning)
            self.assertIn("observe/hold with no trade is valid", morning)
            self.assertIn("exit_only_symbols may be researched", morning)
            self.assertIn("codex_research_fallback.status=\"recommended\"", morning)
            self.assertIn("source title/date/URL", morning)
            self.assertIn(f'cwds = ["{repo_root.resolve()}"]', morning)

            late_morning = (
                codex_home
                / "automations"
                / "dsa-agent-backtest-late-morning"
                / "automation.toml"
            ).read_text(encoding="utf-8")
            self.assertIn("--run-id 42 --phase late_morning", late_morning)
            self.assertIn("DTSTART:20260528T103000", late_morning)
            self.assertIn('name = "1030看盘"', late_morning)

            pre_noon = (
                codex_home
                / "automations"
                / "dsa-agent-backtest-pre-noon"
                / "automation.toml"
            ).read_text(encoding="utf-8")
            self.assertIn("--run-id 42 --phase pre_noon", pre_noon)
            self.assertIn("DTSTART:20260528T112000", pre_noon)
            self.assertIn('name = "1120看盘"', pre_noon)

            midday = (
                codex_home
                / "automations"
                / "dsa-agent-backtest-midday"
                / "automation.toml"
            ).read_text(encoding="utf-8")
            self.assertIn('name = "1335看盘"', midday)
            self.assertIn("DTSTART:20260528T133500", midday)

            close = (
                codex_home
                / "automations"
                / "dsa-agent-backtest-close"
                / "automation.toml"
            ).read_text(encoding="utf-8")
            self.assertIn("--phase close", close)
            self.assertIn('name = "收盘复盘"', close)
            self.assertIn("--phase close --trade-date <YYYY-MM-DD> --live-data", close)
            self.assertIn("Do not create buy/sell/hold decisions or orders", close)
            self.assertIn("evolve-policy --run-id 42", close)
            self.assertIn("self-review markdown", close)
            self.assertIn("all active traders", close)
            self.assertIn("valuation_stale", close)
            self.assertIn("codex_research_fallback.status=\"recommended\"", close)

    def test_shell_schedule_close_dry_run_keeps_live_data_and_active_profiles(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        env = {
            **os.environ,
            "AGENT_BACKTEST_RUN_ID": "42",
            "AGENT_BACKTEST_LIVE_DATA": "true",
            "AGENT_BACKTEST_RUN_WEEKENDS": "true",
        }

        completed = subprocess.run(
            [
                str(repo_root / "scripts" / "run_agent_backtest_codex_schedule.sh"),
                "--phase",
                "close",
                "--dry-run",
            ],
            cwd=repo_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--phase close --trade-date", completed.stdout)
        self.assertIn("--live-data", completed.stdout)
        self.assertIn("所有 active profile", completed.stdout)
        self.assertIn("cycle 输出 generated 列表中的每个 context_markdown", completed.stdout)
        self.assertIn("codex_research_fallback", completed.stdout)
        self.assertNotIn("short_context.md、medium_context.md、long_context.md", completed.stdout)


if __name__ == "__main__":
    unittest.main()
