import tempfile
import unittest
from pathlib import Path

from scripts.install_agent_backtest_codex_automations import install, parse_args


class AgentBacktestAutomationInstallerTestCase(unittest.TestCase):
    def test_installs_four_codex_automations(self) -> None:
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
            self.assertIn('name = "操盘 · 早盘复盘"', morning)
            self.assertIn("run_id=42", morning)
            self.assertIn("--run-id 42 --phase morning", morning)
            self.assertIn(f'cwds = ["{repo_root.resolve()}"]', morning)

            close = (
                codex_home
                / "automations"
                / "dsa-agent-backtest-close"
                / "automation.toml"
            ).read_text(encoding="utf-8")
            self.assertIn("--phase close", close)
            self.assertIn("Do not create buy/sell/hold decisions", close)


if __name__ == "__main__":
    unittest.main()
