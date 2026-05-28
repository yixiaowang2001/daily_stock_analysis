# -*- coding: utf-8 -*-
"""Tests for the Codex agent backtest runner."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from src.config import Config
from src.repositories.stock_repo import StockRepository
from src.services.agent_backtest_service import AgentBacktestService
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager
from scripts.run_agent_backtest_cycle import (
    RunnerContextBuilder,
    command_apply_decision,
    command_evolve_policy,
    command_fill_order,
)


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
        self.service.add_profile(
            run_id=run["id"],
            profile={
                "profile_key": "aggressive_short",
                "display_name": "激进短线操盘手",
                "style_profile": "aggressive_short",
                "policy_version_label": "v1.0",
                "policy_markdown": "更激进的短线风格，但仍遵守 A 股交易规则。",
            },
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

        self.assertEqual(len(result["generated"]), 4)
        short_context = next(item for item in result["generated"] if item["profile_key"] == "short")
        aggressive_context = next(item for item in result["generated"] if item["profile_key"] == "aggressive_short")
        payload = json.loads(Path(short_context["context_json"]).read_text(encoding="utf-8"))
        self.assertNotIn("profiles", payload["run"])
        self.assertEqual(payload["profile"]["profile_key"], "short")
        self.assertEqual(payload["observation"]["sequence_no"], 1)
        self.assertTrue(Path(short_context["context_markdown"]).exists())
        self.assertTrue(Path(aggressive_context["context_markdown"]).exists())

    def test_cycle_researches_removed_watchlist_holdings_as_exit_only(self) -> None:
        run = self.service.create_run(
            name="Runner 退出观察",
            symbols=["600519", "000001", "000002"],
            start_date=date(2026, 1, 2),
            initial_cash_per_agent=20000,
            max_observations_per_day=5,
        )
        short_order = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="600519",
            side="buy",
            order_type="limit",
            requested_quantity=100,
            limit_price=10.0,
            submitted_at=datetime(2026, 1, 2, 9, 40),
            effective_at=datetime(2026, 1, 2, 9, 41),
        )
        self.service.record_fill(
            run_id=run["id"],
            order_id=short_order["id"],
            quantity=100,
            price=10.0,
            filled_at=datetime(2026, 1, 2, 9, 41),
            fee=0,
            tax=0,
        )
        medium_order = self.service.create_order(
            run_id=run["id"],
            profile_key="medium",
            symbol="000002",
            side="buy",
            order_type="limit",
            requested_quantity=100,
            limit_price=10.0,
            submitted_at=datetime(2026, 1, 2, 9, 40),
            effective_at=datetime(2026, 1, 2, 9, 41),
        )
        self.service.record_fill(
            run_id=run["id"],
            order_id=medium_order["id"],
            quantity=100,
            price=10.0,
            filled_at=datetime(2026, 1, 2, 9, 41),
            fee=0,
            tax=0,
        )
        self.service.update_run_settings(run_id=run["id"], symbols=["000001"], max_observations_per_day=5)
        context_dir = Path(self.temp_dir.name) / "contexts"
        builder = RunnerContextBuilder(
            service=self.service,
            portfolio_service=PortfolioService(),
            stock_repo=StockRepository(self.db),
        )

        result = builder.build_cycle(
            run_id=run["id"],
            phase="late_morning",
            trade_date=date(2026, 1, 3),
            observation_time="10:30:00",
            context_dir=context_dir,
            live_data=False,
        )

        short_context = next(item for item in result["generated"] if item["profile_key"] == "short")
        payload = json.loads(Path(short_context["context_json"]).read_text(encoding="utf-8"))
        scope = payload["evidence"]["symbol_scope"]
        self.assertEqual(scope["watchlist_symbols"], ["000001"])
        self.assertEqual(scope["held_symbols"], ["600519"])
        self.assertEqual(scope["own_held_symbols"], ["600519"])
        self.assertEqual(scope["portfolio_held_symbols"], ["600519", "000002"])
        self.assertEqual(scope["research_symbols"], ["000001", "600519", "000002"])
        self.assertEqual(scope["buy_allowed_symbols"], ["000001"])
        self.assertEqual(scope["exit_only_symbols"], ["600519"])
        self.assertEqual(scope["research_only_symbols"], ["000002"])
        self.assertEqual(payload["observation"]["symbols"], ["000001", "600519", "000002"])

        fact = payload["evidence"]["symbol_facts"]["000001"]
        self.assertEqual(fact["schema_version"], "agent_backtest_symbol_facts_v2")
        self.assertIn("market_data", fact)
        self.assertIn("technical_context", fact)
        self.assertIn("fundamental_context", fact)
        self.assertIn("information_context", fact)
        self.assertIn("sentiment_context", fact)
        self.assertEqual(fact["fundamental_context"], {"status": "skipped", "reason": "live_data_disabled"})

        medium_context = next(item for item in result["generated"] if item["profile_key"] == "medium")
        medium_payload = json.loads(Path(medium_context["context_json"]).read_text(encoding="utf-8"))
        medium_scope = medium_payload["evidence"]["symbol_scope"]
        self.assertEqual(medium_scope["research_symbols"], ["000001", "600519", "000002"])
        self.assertEqual(medium_scope["held_symbols"], ["000002"])
        self.assertEqual(medium_scope["exit_only_symbols"], ["000002"])
        self.assertEqual(medium_scope["research_only_symbols"], ["600519"])
        self.assertEqual(
            payload["evidence"]["symbol_facts"],
            medium_payload["evidence"]["symbol_facts"],
        )

    def test_close_cycle_uses_self_review_contract_without_observation(self) -> None:
        run = self.service.create_run(
            name="Runner 收盘复盘",
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
            phase="close",
            trade_date=date(2026, 1, 2),
            observation_time="16:05:00",
            context_dir=context_dir,
            live_data=False,
        )

        short_context = next(item for item in result["generated"] if item["profile_key"] == "short")
        payload = json.loads(Path(short_context["context_json"]).read_text(encoding="utf-8"))
        markdown = Path(short_context["context_markdown"]).read_text(encoding="utf-8")
        self.assertIsNone(payload["observation"])
        self.assertEqual(payload["output_contract"]["required_json_fields"], ["profile_key", "self_review", "policy_update"])
        self.assertIn("Required Close Self-Review JSON", markdown)
        self.assertIn("Do not create trading decisions or orders", markdown)

    def test_close_cycle_context_uses_same_price_overrides_as_daily_nav(self) -> None:
        run = self.service.create_run(
            name="Runner 收盘估值",
            symbols=["600519"],
            start_date=date(2026, 1, 2),
            initial_cash_per_agent=20000,
        )
        order = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="600519",
            side="buy",
            order_type="limit",
            requested_quantity=100,
            limit_price=10.0,
            submitted_at=datetime(2026, 1, 2, 9, 40),
            effective_at=datetime(2026, 1, 2, 9, 41),
        )
        self.service.record_fill(
            run_id=run["id"],
            order_id=order["id"],
            quantity=100,
            price=10.0,
            filled_at=datetime(2026, 1, 2, 9, 41),
            fee=0,
            tax=0,
        )
        context_dir = Path(self.temp_dir.name) / "contexts"
        builder = RunnerContextBuilder(
            service=self.service,
            portfolio_service=PortfolioService(),
            stock_repo=StockRepository(self.db),
        )

        original_fetch = RunnerContextBuilder._fetch_realtime_quote
        RunnerContextBuilder._fetch_realtime_quote = staticmethod(
            lambda symbol: {"price": 12.0, "source": "unit-test", "name": "贵州茅台"}
        )
        try:
            result = builder.build_cycle(
                run_id=run["id"],
                phase="close",
                trade_date=date(2026, 1, 2),
                observation_time="16:05:00",
                context_dir=context_dir,
                live_data=True,
            )
        finally:
            RunnerContextBuilder._fetch_realtime_quote = staticmethod(original_fetch)

        self.assertEqual(result["close_price_overrides"], {"600519": 12.0})
        short_context = next(item for item in result["generated"] if item["profile_key"] == "short")
        payload = json.loads(Path(short_context["context_json"]).read_text(encoding="utf-8"))
        evidence = payload["evidence"]
        position = evidence["portfolio_snapshot"]["accounts"][0]["positions"][0]
        self.assertEqual(position["last_price"], 12.0)
        self.assertEqual(position["valuation_source"], "realtime_quote:unit-test")
        self.assertFalse(position["valuation_stale"])
        close_nav_position = evidence["close_daily_nav"]["payload"]["positions"][0]
        self.assertEqual(close_nav_position["last_price"], 12.0)
        self.assertFalse(evidence["close_daily_nav"]["valuation_stale"])

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

    def test_evolve_policy_command_persists_forward_policy(self) -> None:
        run = self.service.create_run(
            name="Runner 策略迭代",
            symbols=["600519"],
            start_date=date(2026, 1, 2),
        )
        policy_path = Path(self.temp_dir.name) / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "version_label": "v1.1-close-review",
                    "body_markdown": "短线风格不变；收盘复盘后提高风险闸门权重。",
                    "effective_from": "2026-01-03",
                    "change_reason": "收盘自评：减少追高。",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        args = SimpleNamespace(
            run_id=run["id"],
            profile_key="short",
            version_label=None,
            body_file=None,
            effective_from=None,
            change_reason=None,
            policy_json=None,
            policy_file=str(policy_path),
        )

        result = command_evolve_policy(args)

        self.assertEqual(result["policy"]["version_label"], "v1.1-close-review")
        updated = self.service.get_run(run["id"])
        short_profile = next(p for p in updated["profiles"] if p["profile_key"] == "short")
        self.assertEqual(short_profile["policy_version_label"], "v1.1-close-review")


if __name__ == "__main__":
    unittest.main()
