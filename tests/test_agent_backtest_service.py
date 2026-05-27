# -*- coding: utf-8 -*-
"""Tests for multi-agent paper-trading backtest service."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from src.config import Config
from src.services.agent_backtest_service import AgentBacktestError, AgentBacktestService
from src.storage import DatabaseManager, PortfolioTrade


class AgentBacktestServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"
        self.db_path = Path(self.temp_dir.name) / "agent_backtest.db"
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

    def test_create_run_creates_isolated_profiles_and_accounts(self) -> None:
        run = self.service.create_run(
            name="A股三周期实验",
            symbols=["600519", "000001"],
            start_date=date(2026, 1, 2),
            initial_cash_per_agent=20000,
        )

        self.assertEqual(run["symbols"], ["600519", "000001"])
        self.assertEqual(len(run["profiles"]), 3)
        self.assertEqual({p["profile_key"] for p in run["profiles"]}, {"short", "medium", "long"})
        self.assertEqual(len({p["account_id"] for p in run["profiles"]}), 3)
        self.assertTrue(all(p["context_namespace"].startswith(f"agent_backtest:{run['id']}:") for p in run["profiles"]))

    def test_observation_limit_is_per_profile_per_day(self) -> None:
        run = self.service.create_run(
            name="观察次数实验",
            symbols=["600519"],
            start_date=date(2026, 1, 2),
            max_observations_per_day=2,
        )

        for hhmm in ["09:40", "14:30"]:
            self.service.record_observation(
                run_id=run["id"],
                profile_key="short",
                trade_date=date(2026, 1, 2),
                observation_time=hhmm,
                data_cutoff_at=datetime(2026, 1, 2, int(hhmm[:2]), int(hhmm[3:])),
                evidence={"quote": "frozen"},
            )

        with self.assertRaises(AgentBacktestError):
            self.service.record_observation(
                run_id=run["id"],
                profile_key="short",
                trade_date=date(2026, 1, 2),
                observation_time="14:50",
                data_cutoff_at=datetime(2026, 1, 2, 14, 50),
            )

        row = self.service.record_observation(
            run_id=run["id"],
            profile_key="medium",
            trade_date=date(2026, 1, 2),
            observation_time="14:50",
            data_cutoff_at=datetime(2026, 1, 2, 14, 50),
        )
        self.assertEqual(row["sequence_no"], 1)

    def test_fills_enforce_a_share_lot_and_t_plus_one(self) -> None:
        run = self.service.create_run(
            name="A股规则实验",
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
            effective_at=datetime(2026, 1, 2, 9, 43),
        )

        with self.assertRaises(AgentBacktestError):
            self.service.record_fill(
                run_id=run["id"],
                order_id=order["id"],
                quantity=50,
                price=10.0,
                filled_at=datetime(2026, 1, 2, 9, 43),
            )

        fill = self.service.record_fill(
            run_id=run["id"],
            order_id=order["id"],
            quantity=100,
            price=10.0,
            filled_at=datetime(2026, 1, 2, 9, 43),
            fee=0,
            tax=0,
        )
        self.assertIsNotNone(fill["portfolio_trade_id"])

        sell_order = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="600519",
            side="sell",
            order_type="limit",
            requested_quantity=100,
            limit_price=10.5,
            submitted_at=datetime(2026, 1, 2, 14, 0),
            effective_at=datetime(2026, 1, 2, 14, 1),
        )
        with self.assertRaisesRegex(AgentBacktestError, "T\\+1"):
            self.service.record_fill(
                run_id=run["id"],
                order_id=sell_order["id"],
                quantity=100,
                price=10.5,
                filled_at=datetime(2026, 1, 2, 14, 1),
                fee=0,
                tax=0,
            )

        next_day_order = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="600519",
            side="sell",
            order_type="limit",
            requested_quantity=100,
            limit_price=11.0,
            submitted_at=datetime(2026, 1, 3, 9, 40),
            effective_at=datetime(2026, 1, 3, 9, 43),
        )
        next_day_fill = self.service.record_fill(
            run_id=run["id"],
            order_id=next_day_order["id"],
            quantity=100,
            price=11.0,
            filled_at=datetime(2026, 1, 3, 9, 43),
            fee=0,
            tax=0,
        )
        self.assertIsNotNone(next_day_fill["portfolio_trade_id"])

        with self.db.get_session() as session:
            trades = session.execute(
                select(PortfolioTrade).order_by(PortfolioTrade.id.asc())
            ).scalars().all()
        self.assertEqual([trade.side for trade in trades], ["buy", "sell"])

    def test_policy_evolution_updates_only_current_profile_policy(self) -> None:
        run = self.service.create_run(
            name="策略演进实验",
            symbols=["600519"],
            start_date=date(2026, 1, 2),
        )
        policy = self.service.evolve_policy(
            run_id=run["id"],
            profile_key="long",
            version_label="v1.1",
            body_markdown="长线风格不变；降低事件噪音权重。",
            effective_from=date(2026, 1, 3),
            change_reason="复盘后调整",
        )
        self.assertEqual(policy["version_label"], "v1.1")

        updated = self.service.get_run(run["id"])
        long_profile = next(p for p in updated["profiles"] if p["profile_key"] == "long")
        short_profile = next(p for p in updated["profiles"] if p["profile_key"] == "short")
        self.assertEqual(long_profile["policy_version_label"], "v1.1")
        self.assertEqual(short_profile["policy_version_label"], "v1.0")


if __name__ == "__main__":
    unittest.main()
