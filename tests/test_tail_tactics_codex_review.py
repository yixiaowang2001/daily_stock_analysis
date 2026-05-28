# -*- coding: utf-8 -*-
"""Tests for the Codex tail tactics review runner."""

from __future__ import annotations

import os
import tempfile
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path

from scripts.run_tail_tactics_codex_review import (
    command_apply_review,
    command_prepare_review,
)
from src.config import Config
from src.storage import DatabaseManager


class TailTacticsCodexReviewRunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "tail_review_test.db"
        self.env_path = self.data_dir / ".env"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ["TAIL_LAYER2_CALIBRATION_PATH"] = str(self.data_dir / "layer2_calibration.md")
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.strategy_id = self.db.create_tail_strategy_version(
            version_label="v1",
            title="尾盘默认策略",
            body_markdown="主板，涨幅 3%-7%，14:40 截止评分。",
        )
        self.experiment_id = self.db.create_tail_experiment(
            trade_date=date(2026, 5, 27),
            strategy_version_id=self.strategy_id,
            pasted_raw="600519",
            symbols=["600519"],
            param_snapshot={"workflow_goal": "candidate_pool_strategy_experiment"},
            status="ranked",
        )
        self.db.patch_tail_experiment(
            self.experiment_id,
            {
                "ranking_session_id": "tail_exp_test",
                "ranking_output": (
                    "评分报告\n"
                    '{"tail_score_result":[{"code":"600519","score":72,'
                    '"action_level":"candidate","next_open_forecast":{"direction":"gap_up"}}]}'
                ),
            },
        )
        self.db.upsert_tail_morning_metrics(
            self.experiment_id,
            [
                {
                    "symbol": "600519",
                    "surge_pct_prev_close_930_1000": 1.25,
                    "source": "mock",
                }
            ],
        )

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("TAIL_LAYER2_CALIBRATION_PATH", None)
        self.temp_dir.cleanup()

    def test_prepare_review_generates_context_and_apply_persists_review(self) -> None:
        context_dir = self.data_dir / "contexts"
        prepare_payload = command_prepare_review(
            Namespace(
                experiment_id=None,
                trade_date=None,
                morning_trade_date="2026-05-28",
                today="2026-05-28",
                context_dir=str(context_dir),
                no_auto_fetch=True,
                force_auto_fetch=False,
                include_reviewed=False,
                allow_non_trading_day=False,
            )
        )

        self.assertFalse(prepare_payload["skipped"])
        self.assertEqual(prepare_payload["experiment_id"], self.experiment_id)
        context_md = Path(prepare_payload["context_markdown"])
        self.assertTrue(context_md.is_file())
        self.assertIn("tail_review_suggestions", context_md.read_text(encoding="utf-8"))

        review_file = context_md.parent / "exp_1_review.md"
        review_file.write_text(
            "复盘：预测方向基本兑现，但样本仍少。\n"
            '{"tail_review_suggestions":{"case_summary":"600519 次日早盘小幅冲高兑现",'
            '"layer2_calibration_notes":["尾盘贴高且最后5分钟放量时，早盘冲高目标上沿应提高"],'
            '"layer1_change_requests":["仅记录：如多次出现尾盘缩量不贴高失败，再建议用户确认是否改同花顺过滤"]}}',
            encoding="utf-8",
        )
        apply_payload = command_apply_review(
            Namespace(
                experiment_id=self.experiment_id,
                review_file=str(review_file),
                review_text=None,
                session_id=None,
            )
        )

        self.assertEqual(apply_payload["status"], "closed")
        self.assertEqual(apply_payload["case_summary"], "600519 次日早盘小幅冲高兑现")
        self.assertTrue(apply_payload["iteration_memory"]["layer2"]["updated"])
        layer2_text = Path(os.environ["TAIL_LAYER2_CALIBRATION_PATH"]).read_text(encoding="utf-8")
        self.assertIn("早盘冲高目标上沿应提高", layer2_text)
        layer1_text = Path(os.environ["TAIL_LAYER2_CALIBRATION_PATH"]).with_name(
            "layer1_change_requests.md"
        ).read_text(encoding="utf-8")
        self.assertIn("建议用户确认", layer1_text)
        refreshed = self.db.get_tail_experiment(self.experiment_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertIn("预测方向基本兑现", refreshed["review_note_markdown"])

    def test_prepare_review_skips_when_previous_trade_date_has_no_ranked_experiment(self) -> None:
        payload = command_prepare_review(
            Namespace(
                experiment_id=None,
                trade_date=None,
                morning_trade_date="2026-05-29",
                today="2026-05-29",
                context_dir=str(self.data_dir / "contexts"),
                no_auto_fetch=True,
                force_auto_fetch=False,
                include_reviewed=False,
                allow_non_trading_day=False,
            )
        )

        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["target_trade_date"], "2026-05-28")


if __name__ == "__main__":
    unittest.main()
