# -*- coding: utf-8 -*-
"""Regression tests for post-merge Tushare follow-up fixes."""

import importlib.util
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.tushare_fetcher import TushareFetcher


class TestTushareFetcherFollowUps(unittest.TestCase):
    """Cover rate limiting and cross-day trade-calendar refresh behavior."""

    @staticmethod
    def _make_fetcher() -> TushareFetcher:
        with patch.object(TushareFetcher, "_init_api", return_value=None):
            fetcher = TushareFetcher()
        fetcher._api = MagicMock()
        fetcher.priority = 2
        return fetcher

    def test_get_trade_time_refreshes_trade_calendar_when_day_changes(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.side_effect = [
            pd.DataFrame({"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}),
            pd.DataFrame({"cal_date": ["20260318", "20260317"], "is_open": [1, 1]}),
        ]

        with patch.object(
            fetcher,
            "_get_china_now",
            side_effect=[
                datetime(2026, 3, 17, 20, 0),
                datetime(2026, 3, 17, 20, 0),
                datetime(2026, 3, 18, 20, 0),
                datetime(2026, 3, 18, 20, 0),
            ],
        ), patch.object(fetcher, "_check_rate_limit") as rate_limit_mock:
            self.assertEqual(fetcher.get_trade_time(early_time="00:00", late_time="19:00"), "20260317")
            self.assertEqual(fetcher.get_trade_time(early_time="00:00", late_time="19:00"), "20260318")

        self.assertEqual(fetcher._api.trade_cal.call_count, 2)
        self.assertEqual(rate_limit_mock.call_count, 2)
    def test_get_trade_time_returns_latest_trade_date_on_non_trade_day(self) -> None:
        """Non-trade day (e.g. Saturday) should return the most recent trade
        date (Friday), not the one before it (Thursday).  Fixes #1009."""
        fetcher = self._make_fetcher()
        # 2026-03-21 is Saturday; Friday 20 and Thursday 19 are trade dates
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {
                "cal_date": ["20260314", "20260315", "20260316",
                             "20260317", "20260318", "20260319",
                             "20260320", "20260321"],
                "is_open": [0, 0, 1, 1, 1, 1, 1, 0],
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            # called twice: once by get_trade_time, once by _get_trade_dates
            side_effect=[datetime(2026, 3, 21, 10, 0)] * 2,
        ), patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_trade_time(early_time="00:00", late_time="19:00")

        # Should be Friday (20th), NOT Thursday (19th)
        self.assertEqual(result, "20260320")

    def test_get_trade_time_trade_day_before_data_ready_returns_previous(self) -> None:
        """On a trade day within the early-late window, should return the
        previous trade date (data not ready yet for today)."""
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {
                "cal_date": ["20260319", "20260320"],
                "is_open": [1, 1],
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            # Friday 10:00 AM - within 00:00~19:00 window, data not ready
            side_effect=[datetime(2026, 3, 20, 10, 0)] * 2,
        ), patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_trade_time(early_time="00:00", late_time="19:00")

        # Data not ready, should fall back to Thursday (19th)
        self.assertEqual(result, "20260319")
        
          
    def test_get_sector_rankings_rate_limits_calendar_and_rankings_api(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}
        )
        fetcher._api.moneyflow_ind_ths.return_value = pd.DataFrame(
            {
                "industry": ["AI", "消费"],
                "pct_change": [1.8, -0.6],
            }
        )

        with patch.object(fetcher, "_get_china_now", return_value=datetime(2026, 3, 17, 16, 0)), patch.object(
            fetcher, "_check_rate_limit"
        ) as rate_limit_mock:
            top, bottom = fetcher.get_sector_rankings(n=1)

        self.assertEqual(top, [{"name": "AI", "change_pct": 1.8}])
        self.assertEqual(bottom, [{"name": "消费", "change_pct": -0.6}])
        self.assertEqual(rate_limit_mock.call_count, 2)

    def test_get_chip_distribution_rate_limits_all_tushare_calls(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}
        )
        fetcher._api.cyq_chips.return_value = pd.DataFrame(
            {
                "price": [9.0, 10.0, 11.0],
                "percent": [20.0, 50.0, 30.0],
            }
        )
        fetcher._api.daily.return_value = pd.DataFrame({"close": [10.5]})

        with patch.object(fetcher, "_get_china_now", return_value=datetime(2026, 3, 17, 20, 0)), patch.object(
            fetcher, "_check_rate_limit"
        ) as rate_limit_mock:
            chip = fetcher.get_chip_distribution("600519")

        self.assertIsNotNone(chip)
        if chip is None:
            self.fail("expected chip distribution data")
        self.assertEqual(chip.date, "2026-03-17")
        self.assertAlmostEqual(chip.profit_ratio, 0.7)
        self.assertAlmostEqual(chip.avg_cost, 10.1)
        self.assertAlmostEqual(chip.concentration_90, 0.1)
        self.assertAlmostEqual(chip.concentration_70, 0.1)
        self.assertEqual(rate_limit_mock.call_count, 3)

    def test_get_fundamental_bundle_normalizes_tushare_financial_blocks(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.daily_basic.return_value = pd.DataFrame(
            [
                {
                    "ts_code": "600519.SH",
                    "trade_date": "20260317",
                    "close": 100.0,
                    "pe": 20.0,
                    "pe_ttm": 19.8,
                    "pb": 5.0,
                    "dv_ratio": 2.1,
                    "dv_ttm": 2.3,
                    "total_mv": 123456.0,
                    "circ_mv": 100000.0,
                    "turnover_rate": 0.8,
                    "volume_ratio": 1.2,
                }
            ]
        )
        fetcher._api.fina_indicator.return_value = pd.DataFrame(
            [
                {
                    "ts_code": "600519.SH",
                    "ann_date": "20260320",
                    "end_date": "20251231",
                    "roe": 30.0,
                    "roe_dt": 29.0,
                    "roa": 20.0,
                    "grossprofit_margin": 80.0,
                    "netprofit_margin": 50.0,
                    "ocfps": 10.0,
                    "eps": 5.0,
                    "bps": 60.0,
                    "netprofit_yoy": 12.0,
                    "or_yoy": 8.0,
                    "debt_to_assets": 25.0,
                }
            ]
        )
        fetcher._api.income.return_value = pd.DataFrame(
            [
                {
                    "ts_code": "600519.SH",
                    "ann_date": "20260320",
                    "end_date": "20251231",
                    "total_revenue": 1000.0,
                    "revenue": 900.0,
                    "n_income_attr_p": 300.0,
                    "total_profit": 400.0,
                }
            ]
        )
        fetcher._api.cashflow.return_value = pd.DataFrame(
            [
                {
                    "ts_code": "600519.SH",
                    "ann_date": "20260320",
                    "end_date": "20251231",
                    "n_cashflow_act": 280.0,
                }
            ]
        )
        fetcher._api.dividend.return_value = pd.DataFrame(
            [
                {
                    "ts_code": "600519.SH",
                    "end_date": "20251231",
                    "ann_date": "20260320",
                    "div_proc": "实施",
                    "cash_div": 0.9,
                    "cash_div_tax": 1.0,
                    "record_date": "20260330",
                    "ex_date": "20260331",
                }
            ]
        )

        with patch.object(fetcher, "_get_china_now", return_value=datetime(2026, 4, 1, 10, 0)), patch.object(
            fetcher, "_check_rate_limit"
        ) as rate_limit_mock:
            bundle = fetcher.get_fundamental_bundle("600519")

        self.assertEqual(bundle["status"], "ok")
        self.assertEqual(bundle["valuation"]["pe_ratio"], 20.0)
        self.assertEqual(bundle["valuation"]["total_mv"], 123456.0 * 10000.0)
        self.assertEqual(bundle["growth"]["revenue_yoy"], 8.0)
        report = bundle["earnings"]["financial_report"]
        self.assertEqual(report["report_date"], "2025-12-31")
        self.assertEqual(report["net_profit_parent"], 300.0)
        self.assertEqual(report["operating_cash_flow"], 280.0)
        dividend = bundle["earnings"]["dividend"]
        self.assertEqual(dividend["ttm_cash_dividend_per_share"], 1.0)
        self.assertTrue(dividend["events"][0]["is_pre_tax"])
        self.assertIn("valuation:tushare_daily_basic", bundle["source_chain"])
        self.assertEqual(rate_limit_mock.call_count, 5)

    def test_convert_stock_code_accepts_exchange_prefixed_a_share(self) -> None:
        fetcher = self._make_fetcher()

        self.assertEqual(fetcher._convert_stock_code("SZ000001"), "000001.SZ")
        self.assertEqual(fetcher._convert_stock_code("SH600519"), "600519.SH")
        self.assertEqual(fetcher._convert_stock_code("600519.SS"), "600519.SH")

    @patch.dict(sys.modules, {"tushare": MagicMock()})
    def test_legacy_realtime_quote_keeps_sz_hint_as_stock_symbol(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.quotation.side_effect = Exception("quota")

        tushare_module = sys.modules["tushare"]
        tushare_module.get_realtime_quotes.return_value = pd.DataFrame(
            [
                {
                    "name": "平安银行",
                    "price": "10.94",
                    "pre_close": "10.88",
                    "volume": "1000",
                    "amount": "2000",
                    "high": "11.00",
                    "low": "10.80",
                    "open": "10.90",
                }
            ]
        )

        quote = fetcher.get_realtime_quote("SZ000001")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.code, "000001")
        self.assertEqual(quote.name, "平安银行")
        tushare_module.get_realtime_quotes.assert_called_once_with("000001")
