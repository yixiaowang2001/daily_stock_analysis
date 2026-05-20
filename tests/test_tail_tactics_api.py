# -*- coding: utf-8 -*-
"""API tests for tail tactics workbench (no network)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import src.auth as auth
from api.app import create_app
from fastapi.testclient import TestClient
from src.config import Config
from src.services.tail_candidate_facts import build_tail_candidate_facts
from src.services.tail_conversation_archive import maybe_persist_tail_conversation
from src.services.tail_intraday_fetch import fetch_tail_intraday_cutoff_evidence
from src.storage import DatabaseManager


def _reset_auth_globals() -> None:
    auth._auth_enabled = None
    auth._session_secret = None
    auth._password_hash_salt = None
    auth._password_hash_stored = None
    auth._rate_limit = {}


class TailTacticsApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _reset_auth_globals()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.env_path = self.data_dir / ".env"
        self.db_path = self.data_dir / "tail_tactics_test.db"
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
        static_dir = self.data_dir / "empty-static"
        static_dir.mkdir(parents=True, exist_ok=True)
        app = create_app(static_dir=static_dir)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def test_strategy_version_crud_and_diff(self) -> None:
        r1 = self.client.post(
            "/api/v1/tail-tactics/strategy-versions",
            json={
                "version_label": "v1",
                "title": "T1",
                "body_markdown": "line a\nline b\n",
            },
        )
        self.assertEqual(r1.status_code, 200, r1.text)
        id1 = r1.json()["id"]

        r2 = self.client.post(
            "/api/v1/tail-tactics/strategy-versions",
            json={
                "version_label": "v2",
                "title": "T2",
                "body_markdown": "line a\nline c\n",
                "parent_version_id": id1,
            },
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        id2 = r2.json()["id"]

        lst = self.client.get("/api/v1/tail-tactics/strategy-versions")
        self.assertEqual(lst.status_code, 200)
        self.assertGreaterEqual(len(lst.json()["versions"]), 2)

        diff = self.client.get(f"/api/v1/tail-tactics/strategy-versions/{id1}/diff/{id2}")
        self.assertEqual(diff.status_code, 200)
        self.assertIn("unified_diff", diff.json())
        self.assertIn("line", diff.json()["unified_diff"])

        ex = self.client.post(
            "/api/v1/tail-tactics/experiments",
            json={
                "trade_date": "2026-05-10",
                "strategy_version_id": id1,
                "pasted_raw": "600519",
                "symbols": ["600519"],
            },
        )
        self.assertEqual(ex.status_code, 200, ex.text)
        eid = ex.json()["id"]
        blocked = self.client.delete(f"/api/v1/tail-tactics/strategy-versions/{id1}")
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.client.delete(f"/api/v1/tail-tactics/experiments/{eid}")
        ok_del = self.client.delete(f"/api/v1/tail-tactics/strategy-versions/{id1}")
        self.assertEqual(ok_del.status_code, 204, ok_del.text)
        gone = self.client.get(f"/api/v1/tail-tactics/strategy-versions/{id1}")
        self.assertEqual(gone.status_code, 404)

    def test_experiment_morning_metrics_compose(self) -> None:
        rv = self.client.post(
            "/api/v1/tail-tactics/strategy-versions",
            json={"version_label": "v0", "title": "base", "body_markdown": "body"},
        )
        sid = rv.json()["id"]

        ex = self.client.post(
            "/api/v1/tail-tactics/experiments",
            json={
                "trade_date": "2026-05-10",
                "strategy_version_id": sid,
                "pasted_raw": "600519 000001",
                "symbols": ["600519", "000001"],
                "param_snapshot": {
                    "workflow_goal": "conversation_first_tail_pick",
                    "risk_mode": "strict",
                    "score_weights": {"market": 15, "sector": 20},
                },
            },
        )
        self.assertEqual(ex.status_code, 200, ex.text)
        eid = ex.json()["id"]
        self.assertEqual(ex.json()["param_snapshot"]["risk_mode"], "strict")

        db = DatabaseManager.get_instance()
        db.save_daily_data(
            pd.DataFrame(
                [
                    {
                        "date": "2026-05-08",
                        "open": 100.0,
                        "high": 103.0,
                        "low": 99.0,
                        "close": 102.0,
                        "volume": 10000,
                        "amount": 1020000,
                        "pct_chg": 2.0,
                        "ma5": 100.0,
                        "ma10": 98.0,
                        "ma20": 96.0,
                        "volume_ratio": 1.2,
                    },
                    {
                        "date": "2026-05-10",
                        "open": 102.0,
                        "high": 108.0,
                        "low": 101.0,
                        "close": 107.0,
                        "volume": 18000,
                        "amount": 1900000,
                        "pct_chg": 4.9,
                        "ma5": 102.0,
                        "ma10": 99.0,
                        "ma20": 97.0,
                        "volume_ratio": 1.8,
                    },
                ]
            ),
            "600519",
            "TestSource",
        )

        lst = self.client.get("/api/v1/tail-tactics/experiments")
        self.assertEqual(lst.status_code, 200)
        ids = [x["id"] for x in lst.json()["experiments"]]
        self.assertIn(eid, ids)

        facts_response = self.client.get(f"/api/v1/tail-tactics/experiments/{eid}/candidate-facts")
        self.assertEqual(facts_response.status_code, 200, facts_response.text)
        facts = facts_response.json()
        self.assertEqual(facts["trade_date"], "2026-05-10")
        self.assertIn("DSA SQLite", facts["source_policy"]["primary"])
        self.assertEqual(facts["selection_cutoff"]["time"], "14:40")
        self.assertIn("must not drive scoring", facts["selection_cutoff"]["policy"])
        self.assertEqual(facts["candidates"][0]["code"], "600519")
        self.assertEqual(facts["candidates"][0]["data_status"], "partial")
        self.assertEqual(facts["candidates"][0]["trade_date_bar_scope"], "post_close_reference_not_for_scoring")
        self.assertIn("trade_date_intraday_until_cutoff", facts["candidates"][0]["missing_fields"])
        self.assertTrue(facts["candidates"][0]["derived_features"]["ma_bullish_alignment"])
        self.assertEqual(facts["candidates"][1]["data_status"], "missing")

        snapshots_before = self.client.get(
            f"/api/v1/tail-tactics/experiments/{eid}/candidate-fact-snapshots"
        )
        self.assertEqual(snapshots_before.status_code, 200, snapshots_before.text)
        self.assertEqual(snapshots_before.json()["snapshots"], [])

        comp = self.client.get(f"/api/v1/tail-tactics/experiments/{eid}/compose?kind=rank")
        self.assertEqual(comp.status_code, 200, comp.text)
        body = comp.json()
        self.assertTrue(body["session_id"].startswith("tail_exp_"))
        self.assertIn("tail_ranking_bundle", body["context"])
        bundle = body["context"]["tail_ranking_bundle"]
        self.assertEqual(bundle["param_snapshot"]["workflow_goal"], "conversation_first_tail_pick")
        self.assertIn("candidate_facts", bundle)
        self.assertEqual(bundle["candidate_facts"]["candidates"][0]["code"], "600519")
        self.assertGreater(bundle["candidate_facts"]["snapshot_id"], 0)
        self.assertEqual(bundle["candidate_facts"]["snapshot_kind"], "rank")
        self.assertIn("factor_scores", body["message"])
        self.assertIn("tail_score_result", body["message"])
        self.assertIn("next_open_forecast", body["message"])
        self.assertIn("中文报告", body["message"])
        self.assertIn("结论卡", body["message"])
        self.assertIn("14:40", body["message"])
        self.assertIn("证据层", body["message"])
        self.assertIn("候选池生成规则", body["message"])
        self.assertIn("Agent 的「评估/预测规则」", body["message"])
        self.assertIn("同花顺筛选条件", body["message"])

        snapshots_after = self.client.get(
            f"/api/v1/tail-tactics/experiments/{eid}/candidate-fact-snapshots?kind=rank"
        )
        self.assertEqual(snapshots_after.status_code, 200, snapshots_after.text)
        snapshots = snapshots_after.json()["snapshots"]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["id"], bundle["candidate_facts"]["snapshot_id"])
        self.assertEqual(snapshots[0]["kind"], "rank")
        self.assertEqual(snapshots[0]["facts"]["candidates"][0]["code"], "600519")
        self.assertIn("pre-cutoff intraday evidence", snapshots[0]["data_freshness_summary"])

        put = self.client.put(
            f"/api/v1/tail-tactics/experiments/{eid}/morning-metrics",
            json={
                "items": [
                    {"symbol": "600519", "surge_pct_prev_close_930_1000": 1.5},
                    {"symbol": "000001", "surge_pct_prev_close_930_1000": -0.5},
                ]
            },
        )
        self.assertEqual(put.status_code, 200, put.text)

        gm = self.client.get(f"/api/v1/tail-tactics/experiments/{eid}/morning-metrics")
        self.assertEqual(gm.status_code, 200)
        self.assertEqual(len(gm.json()["items"]), 2)

        comp2 = self.client.get(f"/api/v1/tail-tactics/experiments/{eid}/compose?kind=review")
        self.assertEqual(comp2.status_code, 200, comp2.text)
        self.assertIn("tail_review_bundle", comp2.json()["context"])
        self.assertIn("第一层同花顺候选池筛选条件", comp2.json()["message"])
        self.assertIn("第二层 Agent 评估/预测权重", comp2.json()["message"])

        patch = self.client.patch(
            f"/api/v1/tail-tactics/experiments/{eid}",
            json={
                "status": "closed",
                "case_summary": "done",
                "param_snapshot": {"risk_mode": "balanced"},
            },
        )
        self.assertEqual(patch.status_code, 200, patch.text)
        self.assertEqual(patch.json()["status"], "closed")
        self.assertEqual(patch.json()["param_snapshot"]["risk_mode"], "balanced")

        del_r = self.client.delete(f"/api/v1/tail-tactics/experiments/{eid}")
        self.assertEqual(del_r.status_code, 204, del_r.text)

        gone = self.client.get(f"/api/v1/tail-tactics/experiments/{eid}")
        self.assertEqual(gone.status_code, 404)

        lst2 = self.client.get("/api/v1/tail-tactics/experiments")
        self.assertEqual(lst2.status_code, 200)
        ids2 = [x["id"] for x in lst2.json()["experiments"]]
        self.assertNotIn(eid, ids2)

    def test_morning_metrics_auto_fetch_route_mocked(self) -> None:
        rv = self.client.post(
            "/api/v1/tail-tactics/strategy-versions",
            json={"version_label": "vaf", "title": "base", "body_markdown": "body"},
        )
        sid = rv.json()["id"]
        ex = self.client.post(
            "/api/v1/tail-tactics/experiments",
            json={
                "trade_date": "2026-05-08",
                "strategy_version_id": sid,
                "pasted_raw": "600519",
                "symbols": ["600519"],
            },
        )
        eid = ex.json()["id"]
        fake_items = [
            {"symbol": "600519", "surge_pct_prev_close_930_1000": 2.5, "source": "mock"},
        ]
        with patch(
            "api.v1.endpoints.tail_tactics.auto_fetch_tail_morning_metrics",
            return_value=(fake_items, date(2026, 5, 11), ["ok:mock"]),
        ):
            r = self.client.post(
                f"/api/v1/tail-tactics/experiments/{eid}/morning-metrics/auto-fetch",
                json={},
            )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["morning_trade_date"], "2026-05-11")
        self.assertEqual(data["notes"], ["ok:mock"])
        self.assertEqual(len(data["items"]), 1)
        self.assertAlmostEqual(data["items"][0]["surge_pct_prev_close_930_1000"], 2.5, places=4)

    def test_tail_conversation_archive_auto_creates_and_updates(self) -> None:
        rv = self.client.post(
            "/api/v1/tail-tactics/strategy-versions",
            json={"version_label": "vauto", "title": "base", "body_markdown": "body"},
        )
        self.assertEqual(rv.status_code, 200, rv.text)
        db = DatabaseManager.get_instance()

        outcome = maybe_persist_tail_conversation(
            message="0518 尾盘选出了 600519，帮我评估一下 T+1 早盘预判",
            response_content=(
                "评估报告：预计次日早盘有冲高观察价值。\n"
                '{"tail_score_result":[{"code":"600519","score":72}]}'
            ),
            session_id="chat_tail_1",
            context={},
            skills=["bull_trend"],
            today=date(2026, 5, 19),
            db=db,
        )

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertTrue(outcome.created)
        self.assertEqual(outcome.trade_date, "2026-05-18")
        self.assertEqual(outcome.symbols, ["600519"])

        exp = db.get_tail_experiment(outcome.experiment_id)
        self.assertIsNotNone(exp)
        assert exp is not None
        self.assertEqual(exp["trade_date"], "2026-05-18")
        self.assertEqual(exp["symbols"], ["600519"])
        self.assertEqual(exp["status"], "ranked")
        self.assertEqual(exp["ranking_session_id"], "chat_tail_1")
        self.assertIn("tail_score_result", exp["ranking_output"])
        self.assertEqual(exp["param_snapshot"]["workflow_goal"], "conversation_auto_tail_archive")

        outcome2 = maybe_persist_tail_conversation(
            message="继续看 0518 尾盘 600519 的预测",
            response_content="更新评估：次日早盘预判改为谨慎观察。",
            session_id="chat_tail_1",
            context={},
            today=date(2026, 5, 19),
            db=db,
        )
        self.assertIsNotNone(outcome2)
        assert outcome2 is not None
        self.assertFalse(outcome2.created)
        self.assertEqual(outcome2.experiment_id, outcome.experiment_id)
        exp2 = db.get_tail_experiment(outcome.experiment_id)
        assert exp2 is not None
        self.assertIn("更新评估", exp2["ranking_output"])

        db.save_conversation_message("chat_tail_history", "user", "0518 尾盘选出了 000001")
        history_outcome = maybe_persist_tail_conversation(
            message="查看当前尾盘策略",
            response_content="当前尾盘策略说明：按 14:40 截止，不追尾盘直线拉升。",
            session_id="chat_tail_history",
            context={},
            today=date(2026, 5, 19),
            db=db,
        )
        self.assertIsNotNone(history_outcome)
        assert history_outcome is not None
        self.assertEqual(history_outcome.trade_date, "2026-05-18")
        self.assertEqual(history_outcome.symbols, ["000001"])

    def test_tail_conversation_archive_explicit_rank_and_review(self) -> None:
        rv = self.client.post(
            "/api/v1/tail-tactics/strategy-versions",
            json={"version_label": "vctx", "title": "base", "body_markdown": "body"},
        )
        sid = rv.json()["id"]
        ex = self.client.post(
            "/api/v1/tail-tactics/experiments",
            json={
                "trade_date": "2026-05-18",
                "strategy_version_id": sid,
                "pasted_raw": "600519",
                "symbols": ["600519"],
            },
        )
        eid = ex.json()["id"]
        db = DatabaseManager.get_instance()

        rank = maybe_persist_tail_conversation(
            message="rank",
            response_content='{"tail_score_result":[{"code":"600519","score":70}]}',
            session_id="tail_exp_1",
            context={"tail_ranking_bundle": {"experiment_id": eid}},
            db=db,
        )
        self.assertIsNotNone(rank)
        exp = db.get_tail_experiment(eid)
        assert exp is not None
        self.assertEqual(exp["status"], "ranked")
        self.assertEqual(exp["ranking_session_id"], "tail_exp_1")
        self.assertIn("tail_score_result", exp["ranking_output"])

        review = maybe_persist_tail_conversation(
            message="review",
            response_content=(
                "复盘完成。\n"
                '{"tail_review_suggestions":{"case_summary":"600519 次日早盘冲高兑现"}}'
            ),
            session_id="tail_exp_1",
            context={"tail_review_bundle": {"experiment_id": eid}},
            db=db,
        )
        self.assertIsNotNone(review)
        exp2 = db.get_tail_experiment(eid)
        assert exp2 is not None
        self.assertEqual(exp2["status"], "closed")
        self.assertIn("复盘完成", exp2["review_note_markdown"])
        self.assertEqual(exp2["case_summary"], "600519 次日早盘冲高兑现")

    def test_intraday_cutoff_fetch_falls_back_to_next_provider(self) -> None:
        def failed_provider(symbol: str, trade_date: date, selection_cutoff: str):
            raise RuntimeError("primary unavailable")

        def ok_provider(symbol: str, trade_date: date, selection_cutoff: str):
            return (
                pd.DataFrame(
                    [
                        {
                            "day": "2026-05-19 14:39:00",
                            "open": "10.10",
                            "high": "10.20",
                            "low": "10.08",
                            "close": "10.18",
                            "volume": "1000",
                        },
                        {
                            "day": "2026-05-19 14:40:00",
                            "open": "10.18",
                            "high": "10.22",
                            "low": "10.16",
                            "close": "10.20",
                            "volume": "1200",
                        },
                        {
                            "day": "2026-05-19 14:41:00",
                            "open": "10.20",
                            "high": "10.50",
                            "low": "10.19",
                            "close": "10.45",
                            "volume": "2000",
                        },
                    ]
                ),
                "fake_minute_provider",
            )

        evidence = fetch_tail_intraday_cutoff_evidence(
            symbol="600519",
            trade_date=date(2026, 5, 19),
            selection_cutoff="14:40",
            previous_close=10.0,
            providers=[failed_provider, ok_provider],
        )

        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["source"], "fake_minute_provider")
        self.assertEqual(evidence["fields"]["last_bar_time"], "2026-05-19T14:40:00")
        self.assertEqual(evidence["fields"]["last_close"], 10.2)
        self.assertEqual(evidence["fields"]["pre_cutoff_high"], 10.22)
        self.assertEqual(evidence["fields"]["change_pct_at_cutoff"], 2.0)
        self.assertEqual(evidence["fields"]["last_volume"], 1200.0)
        self.assertEqual(evidence["fields"]["window_volume"], 2200.0)
        self.assertEqual(evidence["fallback_attempts"][0]["status"], "failed")
        self.assertEqual(evidence["fallback_attempts"][1]["status"], "ok")

    def test_candidate_facts_include_intraday_when_tail_window_fetch_succeeds(self) -> None:
        def fake_intraday_fetcher(**kwargs):
            self.assertEqual(kwargs["symbol"], "600519")
            self.assertEqual(kwargs["selection_cutoff"], "14:40")
            self.assertEqual(kwargs["previous_close"], 102.0)
            return {
                "code": "600519",
                "as_of": "2026-05-19 14:40:00",
                "available": True,
                "source": "test_minute",
                "fields": {
                    "last_bar_time": "2026-05-19T14:40:00",
                    "last_close": 107.0,
                    "pre_cutoff_high": 108.0,
                    "distance_to_pre_cutoff_high_pct": 0.9259,
                },
                "fallback_attempts": [{"source": "test_minute", "status": "ok"}],
                "source_scope": "test",
            }

        db = DatabaseManager.get_instance()
        db.save_daily_data(
            pd.DataFrame(
                [
                    {
                        "date": "2026-05-18",
                        "open": 100.0,
                        "high": 103.0,
                        "low": 99.0,
                        "close": 102.0,
                        "volume": 10000,
                        "amount": 1020000,
                        "pct_chg": 2.0,
                        "ma5": 100.0,
                        "ma10": 98.0,
                        "ma20": 96.0,
                        "volume_ratio": 1.2,
                    }
                ]
            ),
            "600519",
            "TestSource",
        )
        facts = build_tail_candidate_facts(
            experiment={
                "id": 999,
                "trade_date": "2026-05-19",
                "symbols": ["600519"],
                "param_snapshot": {"data_cutoff_time": "14:40"},
            },
            db=db,
            now=datetime(2026, 5, 19, 14, 41, tzinfo=ZoneInfo("Asia/Shanghai")),
            intraday_fetcher=fake_intraday_fetcher,
        )

        candidate = facts["candidates"][0]
        self.assertTrue(candidate["selection_cutoff_evidence"]["available"])
        self.assertEqual(candidate["selection_cutoff_evidence"]["source"], "test_minute")
        self.assertNotIn("trade_date_intraday_until_cutoff", candidate["missing_fields"])
        self.assertIn(
            "1/1 candidates have pre-cutoff intraday evidence",
            facts["data_freshness"]["summary"],
        )


if __name__ == "__main__":
    unittest.main()
