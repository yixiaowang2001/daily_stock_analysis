# -*- coding: utf-8 -*-
"""Tests for tail tactics morning metric fetch fallbacks."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from src.services.tail_morning_fetch import auto_fetch_tail_morning_metrics


class FakeDailyManager:
    def get_daily_data(self, symbol: str, days: int):  # noqa: ARG002
        return (
            pd.DataFrame(
                [
                    {"date": "2026-05-27", "close": 14.23, "high": 14.37},
                    {"date": "2026-05-28", "close": 14.80, "high": 15.15},
                ]
            ),
            "fake_daily",
        )


class TailMorningFetchTestCase(unittest.TestCase):
    def test_uses_shared_intraday_fallback_before_daily_high(self) -> None:
        experiment = {"trade_date": "2026-05-28", "symbols": ["603439"]}
        intraday_payload = {
            "available": True,
            "source": "akshare_stock_zh_a_minute_1m",
            "fields": {
                "pre_cutoff_high": 16.26,
                "pre_cutoff_high_pct": 9.8649,
            },
        }

        with patch(
            "src.services.tail_morning_fetch._minute_window_surge",
            return_value=(None, "akshare_minute:ProxyError"),
        ), patch(
            "src.services.tail_morning_fetch.fetch_tail_intraday_cutoff_evidence",
            return_value=intraday_payload,
        ):
            items, resolved_date, notes = auto_fetch_tail_morning_metrics(
                experiment=experiment,
                morning_trade_date=date(2026, 5, 29),
                manager=FakeDailyManager(),
                pause_sec=0,
            )

        self.assertEqual(resolved_date, date(2026, 5, 29))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "603439")
        self.assertAlmostEqual(items[0]["surge_pct_prev_close_930_1000"], 9.8649, places=4)
        self.assertEqual(items[0]["source"], "akshare_stock_zh_a_minute_1m_0930_1001")
        self.assertIn("1分钟线回退", notes[0])


if __name__ == "__main__":
    unittest.main()
