# -*- coding: utf-8 -*-
"""Tests for the Codex agent backtest runner."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from src.config import Config
from src.repositories.stock_repo import StockRepository
from src.services.agent_backtest_service import AgentBacktestService
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager
from scripts.run_agent_backtest_cycle import RunnerContextBuilder, command_apply_decision, command_fill_order


class AgentBacktestRunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"
        self.db_path = Path(self.temp_dir.name) / "agent_backtest_runner.db"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()

        self.db = DatabaseManager.get_instance()
        self.service = AgentBacktestService(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def test_cycle_generates_isolated_context_files_and_observations(self) -> None:
        run = self.service.create_run(
            name="Runner 验证",
            symbols=["600519"],
            start_date=date(2026, 1, 2),
        )
        context_dir = Path(self.temp_dir.name) / "contexts"
        builder = RunnerContextBuilder(
            service=self.service,
            portfolio_service=PortfolioService(),
            stock_repo=StockRepository(self.db),
        )

        result = builder.build_cycle(
            run_id=run["id"],
            phase="verify",
            trade_date=date(2026, 1, 2),
            observation_time="09:40:00",
            context_dir=context_dir,
            live_data=False,
        )

        self.assertEqual(len(result["generated"]), 3)
        short_context = next(item for item in result["generated"] if item["profile_key"] == "short")
        payload = json.loads(Path(short_context["context_json"]).read_text(encoding="utf-8"))
        self.assertNotIn("profiles", payload["run"])
        self.assertEqual(payload["profile"]["profile_key"], "short")
        self.assertEqual(payload["observation"]["sequence_no"], 1)
        self.assertTrue(Path(short_context["context_markdown"]).exists())

    def test_apply_decision_records_hold_without_order(self) -> None:
        run = self.service.create_run(
            name="Runner 决策写回",
            symbols=["600519"],
            start_date=date(2026, 1, 2),
        )
        args = SimpleNamespace(
            run_id=run["id"],
            profile_key="short",
            trade_date="2026-01-02",
            decision_time="09:43:00",
            observation_id=None,
            decision_json=json.dumps(
                {
                    "action": "hold",
                    "confidence": 0.6,
                    "rationale": "验证阶段不交易。",
                    "risk_notes": "等待真实行情。",
                },
                ensure_ascii=False,
            ),
            decision_file=None,
        )

        result = command_apply_decision(args)

        self.assertEqual(result["decision"]["action"], "hold")
        self.assertIsNone(result["order"])

    def test_apply_buy_decision_and_fill_order(self) -> None:
        run = self.service.create_run(
            name="Runner 买入闭环",
            symbols=["600519"],
            start_date=date(2026, 1, 2),
            initial_cash_per_agent=20000,
        )
        decision_args = SimpleNamespace(
            run_id=run["id"],
            profile_key="short",
            trade_date="2026-01-02",
            decision_time="09:43:00",
            observation_id=None,
            decision_json=json.dumps(
                {
                    "action": "buy",
                    "symbol": "600519",
                    "side": "buy",
                    "quantity": 100,
                    "order_type": "limit",
                    "limit_price": 10.0,
                    "submitted_at": "09:43:00",
                    "effective_at": "09:45:00",
                    "confidence": 0.62,
                    "rationale": "验证买入写回链路。",
                    "risk_notes": "仅测试成交落账。",
                },
                ensure_ascii=False,
            ),
            decision_file=None,
        )

        decision_result = command_apply_decision(decision_args)
        fill_args = SimpleNamespace(
            run_id=run["id"],
            order_id=decision_result["order"]["id"],
            quantity=100,
            price=10.0,
            trade_date="2026-01-02",
            filled_at="09:45:00",
            fee=0,
            tax=0,
            source="test",
        )
        fill_result = command_fill_order(fill_args)

        self.assertEqual(decision_result["order"]["side"], "buy")
        self.assertIsNotNone(fill_result["fill"]["portfolio_trade_id"])


if __name__ == "__main__":
    unittest.main()
