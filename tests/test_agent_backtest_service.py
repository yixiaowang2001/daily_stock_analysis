# -*- coding: utf-8 -*-
"""Tests for multi-agent paper-trading backtest service."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from src.config import Config
from src.services.agent_backtest_service import AgentBacktestError, AgentBacktestService
from src.storage import DatabaseManager, PortfolioAccount, PortfolioTrade


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

    def _save_close(self, symbol: str, on_date: date, close: float) -> None:
        df = pd.DataFrame(
            [
                {
                    "date": on_date,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1.0,
                    "amount": close,
                    "pct_chg": 0.0,
                }
            ]
        )
        self.db.save_daily_data(df, code=symbol, data_source="unit-test")

    def test_create_run_creates_isolated_profiles_and_accounts(self) -> None:
        run = self.service.create_run(
            name="A股三周期实验",
            symbols=["600519", "002975"],
            start_date=date(2026, 1, 2),
            initial_cash_per_agent=20000,
        )

        self.assertEqual(run["symbols"], ["600519", "002975"])
        self.assertEqual(run["symbol_names"]["600519"], "贵州茅台")
        self.assertEqual(run["symbol_names"]["002975"], "博杰股份")
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

    def test_update_run_settings_changes_symbols_and_daily_limit(self) -> None:
        run = self.service.create_run(
            name="设置维护实验",
            symbols=["600519"],
            start_date=date(2026, 1, 2),
            max_observations_per_day=2,
        )

        updated = self.service.update_run_settings(
            run_id=run["id"],
            symbols=["000001", "000001", "600519"],
            max_observations_per_day=4,
        )

        self.assertEqual(updated["symbols"], ["000001", "600519"])
        self.assertEqual(updated["max_observations_per_day"], 4)

        with self.assertRaisesRegex(AgentBacktestError, "outside run symbols"):
            self.service.record_decision(
                run_id=run["id"],
                profile_key="short",
                trade_date=date(2026, 1, 2),
                decision_time=datetime(2026, 1, 2, 9, 43),
                action="buy",
                symbol="002475",
            )

        self.service.record_decision(
            run_id=run["id"],
            profile_key="short",
            trade_date=date(2026, 1, 2),
            decision_time=datetime(2026, 1, 2, 9, 44),
            action="buy",
            symbol="000001",
            )

    def test_removed_watchlist_holding_can_exit_but_not_rebuy(self) -> None:
        run = self.service.create_run(
            name="移出关注池持仓实验",
            symbols=["600519", "000001"],
            start_date=date(2026, 1, 2),
            initial_cash_per_agent=20000,
        )
        buy_order = self.service.create_order(
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
            order_id=buy_order["id"],
            quantity=100,
            price=10.0,
            filled_at=datetime(2026, 1, 2, 9, 41),
            fee=0,
            tax=0,
        )
        self.service.update_run_settings(run_id=run["id"], symbols=["000001"], max_observations_per_day=5)

        hold = self.service.record_decision(
            run_id=run["id"],
            profile_key="short",
            trade_date=date(2026, 1, 3),
            decision_time=datetime(2026, 1, 3, 9, 40),
            action="hold",
            symbol="600519",
            confidence=0.5,
        )
        self.assertEqual(hold["symbol"], "600519")

        sell_order = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="600519",
            side="sell",
            order_type="limit",
            requested_quantity=100,
            limit_price=11.0,
            submitted_at=datetime(2026, 1, 3, 10, 30),
            effective_at=datetime(2026, 1, 3, 10, 31),
        )
        self.assertEqual(sell_order["side"], "sell")

        with self.assertRaisesRegex(AgentBacktestError, "outside run symbols"):
            self.service.create_order(
                run_id=run["id"],
                profile_key="short",
                symbol="600519",
                side="buy",
                order_type="limit",
                requested_quantity=100,
                limit_price=10.5,
                submitted_at=datetime(2026, 1, 3, 10, 32),
                effective_at=datetime(2026, 1, 3, 10, 33),
            )

        self.service.record_fill(
            run_id=run["id"],
            order_id=sell_order["id"],
            quantity=100,
            price=11.0,
            filled_at=datetime(2026, 1, 3, 10, 31),
            fee=0,
            tax=0,
        )
        with self.assertRaisesRegex(AgentBacktestError, "outside run symbols"):
            self.service.record_decision(
                run_id=run["id"],
                profile_key="short",
                trade_date=date(2026, 1, 3),
                decision_time=datetime(2026, 1, 3, 14, 40),
                action="buy",
                symbol="600519",
            )

    def test_add_profile_creates_later_joined_isolated_trader(self) -> None:
        run = self.service.create_run(
            name="新增操盘手实验",
            symbols=["600519"],
            start_date=date(2026, 1, 2),
            initial_cash_per_agent=20000,
        )

        profile = self.service.add_profile(
            run_id=run["id"],
            profile={
                "profile_key": "aggressive_short",
                "display_name": "激进短线操盘手",
                "style_profile": "aggressive_short",
                "policy_version_label": "v1.0",
                "policy_markdown": "风格边界：更激进的短线交易员，允许更高换手，但必须遵守 A 股交易规则。",
            },
        )

        self.assertEqual(profile["profile_key"], "aggressive_short")
        self.assertIsNotNone(profile["created_at"])
        updated = self.service.get_run(run["id"])
        self.assertEqual(len(updated["profiles"]), 4)
        self.assertEqual(len({p["account_id"] for p in updated["profiles"]}), 4)

        with self.assertRaisesRegex(AgentBacktestError, "duplicate profile_key"):
            self.service.add_profile(
                run_id=run["id"],
                profile={
                    "profile_key": "aggressive_short",
                    "display_name": "重复",
                    "style_profile": "aggressive_short",
                    "policy_markdown": "重复。",
                },
            )

    def test_deactivate_profile_removes_from_active_run(self) -> None:
        run = self.service.create_run(
            name="删除操盘手实验",
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

        updated = self.service.deactivate_profile(run_id=run["id"], profile_key="aggressive_short")

        self.assertEqual({p["profile_key"] for p in updated["profiles"]}, {"short", "medium", "long"})
        nav = self.service.record_daily_nav(run_id=run["id"], trade_date=date(2026, 1, 2))
        self.assertEqual(
            {item["profile_id"] for item in nav["items"]},
            {profile["id"] for profile in updated["profiles"]},
        )
        with self.assertRaisesRegex(AgentBacktestError, "profile inactive"):
            self.service.record_observation(
                run_id=run["id"],
                profile_key="aggressive_short",
                trade_date=date(2026, 1, 2),
                observation_time="09:40",
                data_cutoff_at=datetime(2026, 1, 2, 9, 40),
            )

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

    def test_create_us_run_uses_us_profiles_and_usd_accounts(self) -> None:
        run = self.service.create_run(
            name="美股现金账户实验",
            market="us",
            symbols=["aapl", "NVDA"],
            start_date=date(2026, 1, 5),
            initial_cash_per_agent=1000,
        )

        self.assertEqual(run["market"], "us")
        self.assertEqual(run["rule_version"], "us_cash_ibkr_v1")
        self.assertEqual(run["symbols"], ["AAPL", "NVDA"])
        self.assertEqual(run["config"]["base_currency"], "USD")
        self.assertEqual(run["config"]["cash_settlement"], "T+1")
        self.assertEqual({p["profile_key"] for p in run["profiles"]}, {"short", "medium", "long"})
        self.assertEqual(
            {p["profile_key"]: p["display_name"] for p in run["profiles"]},
            {"short": "短线操盘手", "medium": "中线操盘手", "long": "长线操盘手"},
        )

        with self.db.get_session() as session:
            accounts = session.execute(select(PortfolioAccount)).scalars().all()
        self.assertEqual({account.market for account in accounts}, {"us"})
        self.assertEqual({account.base_currency for account in accounts}, {"USD"})

    def test_us_run_requires_whole_shares(self) -> None:
        run = self.service.create_run(
            name="美股整股实验",
            market="us",
            symbols=["AAPL"],
            start_date=date(2026, 1, 5),
            initial_cash_per_agent=1000,
        )

        with self.assertRaisesRegex(AgentBacktestError, "whole-share"):
            self.service.create_order(
                run_id=run["id"],
                profile_key="short",
                symbol="AAPL",
                side="buy",
                order_type="limit",
                requested_quantity=1.5,
                limit_price=100.0,
                submitted_at=datetime(2026, 1, 5, 9, 40),
                effective_at=datetime(2026, 1, 5, 9, 41),
            )

    def test_us_cash_account_sell_proceeds_settle_next_business_day(self) -> None:
        run = self.service.create_run(
            name="美股现金结算实验",
            market="us",
            symbols=["AAPL", "NVDA"],
            start_date=date(2026, 1, 5),
            initial_cash_per_agent=100,
        )
        buy = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            requested_quantity=1,
            limit_price=100.0,
            submitted_at=datetime(2026, 1, 5, 9, 40),
            effective_at=datetime(2026, 1, 5, 9, 41),
        )
        self.service.record_fill(
            run_id=run["id"],
            order_id=buy["id"],
            quantity=1,
            price=100.0,
            filled_at=datetime(2026, 1, 5, 9, 41),
            fee=0,
            tax=0,
        )
        sell = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="AAPL",
            side="sell",
            order_type="limit",
            requested_quantity=1,
            limit_price=100.0,
            submitted_at=datetime(2026, 1, 5, 10, 0),
            effective_at=datetime(2026, 1, 5, 10, 1),
        )
        self.service.record_fill(
            run_id=run["id"],
            order_id=sell["id"],
            quantity=1,
            price=100.0,
            filled_at=datetime(2026, 1, 5, 10, 1),
            fee=0,
            tax=0,
        )

        same_day_buy = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="NVDA",
            side="buy",
            order_type="limit",
            requested_quantity=1,
            limit_price=100.0,
            submitted_at=datetime(2026, 1, 5, 10, 30),
            effective_at=datetime(2026, 1, 5, 10, 31),
        )
        with self.assertRaisesRegex(AgentBacktestError, "insufficient settled cash"):
            self.service.record_fill(
                run_id=run["id"],
                order_id=same_day_buy["id"],
                quantity=1,
                price=100.0,
                filled_at=datetime(2026, 1, 5, 10, 31),
                fee=0,
                tax=0,
            )

        next_business_day_buy = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="NVDA",
            side="buy",
            order_type="limit",
            requested_quantity=1,
            limit_price=100.0,
            submitted_at=datetime(2026, 1, 6, 9, 40),
            effective_at=datetime(2026, 1, 6, 9, 41),
        )
        fill = self.service.record_fill(
            run_id=run["id"],
            order_id=next_business_day_buy["id"],
            quantity=1,
            price=100.0,
            filled_at=datetime(2026, 1, 6, 9, 41),
            fee=0,
            tax=0,
        )
        self.assertIsNotNone(fill["portfolio_trade_id"])

    def test_us_cash_account_limits_day_trades_to_one_per_window(self) -> None:
        run = self.service.create_run(
            name="美股日内回转实验",
            market="us",
            symbols=["AAPL", "NVDA"],
            start_date=date(2026, 1, 5),
            initial_cash_per_agent=1000,
        )

        first_buy = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            requested_quantity=1,
            limit_price=100.0,
            submitted_at=datetime(2026, 1, 5, 9, 40),
            effective_at=datetime(2026, 1, 5, 9, 41),
        )
        self.service.record_fill(
            run_id=run["id"],
            order_id=first_buy["id"],
            quantity=1,
            price=100.0,
            filled_at=datetime(2026, 1, 5, 9, 41),
            fee=0,
            tax=0,
        )
        first_sell = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="AAPL",
            side="sell",
            order_type="limit",
            requested_quantity=1,
            limit_price=105.0,
            submitted_at=datetime(2026, 1, 5, 10, 0),
            effective_at=datetime(2026, 1, 5, 10, 1),
        )
        self.service.record_fill(
            run_id=run["id"],
            order_id=first_sell["id"],
            quantity=1,
            price=105.0,
            filled_at=datetime(2026, 1, 5, 10, 1),
            fee=0,
            tax=0,
        )

        second_buy = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="NVDA",
            side="buy",
            order_type="limit",
            requested_quantity=1,
            limit_price=100.0,
            submitted_at=datetime(2026, 1, 5, 10, 30),
            effective_at=datetime(2026, 1, 5, 10, 31),
        )
        self.service.record_fill(
            run_id=run["id"],
            order_id=second_buy["id"],
            quantity=1,
            price=100.0,
            filled_at=datetime(2026, 1, 5, 10, 31),
            fee=0,
            tax=0,
        )
        second_sell = self.service.create_order(
            run_id=run["id"],
            profile_key="short",
            symbol="NVDA",
            side="sell",
            order_type="limit",
            requested_quantity=1,
            limit_price=105.0,
            submitted_at=datetime(2026, 1, 5, 11, 0),
            effective_at=datetime(2026, 1, 5, 11, 1),
        )
        with self.assertRaisesRegex(AgentBacktestError, "day-trade allowance already used"):
            self.service.record_fill(
                run_id=run["id"],
                order_id=second_sell["id"],
                quantity=1,
                price=105.0,
                filled_at=datetime(2026, 1, 5, 11, 1),
                fee=0,
                tax=0,
            )

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

        policies = self.service.list_policies(run_id=run["id"], profile_key="long")
        self.assertEqual(policies["total"], 2)
        self.assertEqual([item["version_label"] for item in policies["items"]], ["v1.1", "v1.0"])
        self.assertEqual(policies["items"][0]["change_reason"], "复盘后调整")
        self.assertIn("降低事件噪音权重", policies["items"][0]["body_markdown"])

    def test_daily_nav_uses_price_overrides_and_exposes_valuation_stale(self) -> None:
        run = self.service.create_run(
            name="净值估值实验",
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
            effective_at=datetime(2026, 1, 2, 9, 40),
        )
        self.service.record_fill(
            run_id=run["id"],
            order_id=order["id"],
            quantity=100,
            price=10.0,
            filled_at=datetime(2026, 1, 2, 9, 40),
            fee=0,
            tax=0,
        )
        self._save_close("600519", date(2026, 1, 1), 8.0)

        stale_nav = self.service.record_daily_nav(run_id=run["id"], trade_date=date(2026, 1, 2))
        stale_short = next(item for item in stale_nav["items"] if item["profile_id"] == run["profiles"][0]["id"])
        self.assertTrue(stale_short["valuation_stale"])
        self.assertAlmostEqual(stale_short["total_equity"], 19800.0, places=6)
        self.assertEqual(stale_short["payload"]["positions"][0]["valuation_date"], "2026-01-01")

        fresh_nav = self.service.record_daily_nav(
            run_id=run["id"],
            trade_date=date(2026, 1, 2),
            price_overrides={"600519": 12.0},
            price_override_sources={"600519": "realtime_quote:unit-test"},
        )
        fresh_short = next(item for item in fresh_nav["items"] if item["profile_id"] == run["profiles"][0]["id"])
        self.assertFalse(fresh_short["valuation_stale"])
        self.assertAlmostEqual(fresh_short["total_equity"], 20200.0, places=6)
        position = fresh_short["payload"]["positions"][0]
        self.assertEqual(position["valuation_date"], "2026-01-02")
        self.assertEqual(position["valuation_source"], "realtime_quote:unit-test")


if __name__ == "__main__":
    unittest.main()
