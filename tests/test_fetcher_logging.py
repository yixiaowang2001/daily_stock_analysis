import logging
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import BaseFetcher, DataFetchError, DataFetcherManager
from data_provider.efinance_fetcher import EfinanceFetcher
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from data_provider.us_market_fetchers import (
    IbkrFetcher,
    MassiveFetcher,
    TwelveDataFetcher,
    _get_intraday_json,
)


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2026-03-06", "2026-03-07"],
            "open": [10.0, 10.2],
            "high": [10.5, 10.4],
            "low": [9.8, 10.1],
            "close": [10.3, 10.35],
            "volume": [1000, 1200],
            "amount": [10300, 12420],
            "pct_chg": [1.0, 0.49],
        }
    )


class _SuccessFetcher(BaseFetcher):
    name = "SuccessFetcher"
    priority = 1

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return _sample_df()

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df


class _FailureFetcher(BaseFetcher):
    name = "FailureFetcher"
    priority = 0

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise DataFetchError(
            "Eastmoney 历史K线接口失败: "
            "endpoint=push2his.eastmoney.com/api/qt/stock/kline/get, "
            "category=remote_disconnect"
        )

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df


class _StaleMassiveFetcher(BaseFetcher):
    name = "MassiveFetcher"
    priority = 0

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "date": ["2026-03-06", "2026-03-07"],
                "open": [10.0, 10.2],
                "high": [10.5, 10.4],
                "low": [9.8, 10.1],
                "close": [10.3, 10.35],
                "volume": [1000, 1200],
                "amount": [10300, 12420],
                "pct_chg": [1.0, 0.49],
            }
        )

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df


class _FreshTwelveDataFetcher(BaseFetcher):
    name = "TwelveDataFetcher"
    priority = 1

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "date": ["2026-03-07", "2026-03-08"],
                "open": [10.2, 10.4],
                "high": [10.4, 10.8],
                "low": [10.1, 10.2],
                "close": [10.35, 10.7],
                "volume": [1200, 1500],
                "amount": [12420, 16050],
                "pct_chg": [0.49, 3.38],
            }
        )

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df


class _FreshIbkrFetcher(BaseFetcher):
    name = "IbkrFetcher"
    priority = 0

    def _is_available(self) -> bool:
        return True

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "date": ["2026-03-07", "2026-03-08"],
                "open": [190.0, 191.0],
                "high": [193.0, 194.0],
                "low": [188.0, 190.5],
                "close": [192.0, 193.5],
                "volume": [56000000, 61000000],
                "amount": [10752000000, 11803500000],
                "pct_chg": [1.1, 0.78],
            }
        )

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df


class _PrimaryUSQuoteFetcher(BaseFetcher):
    name = "TwelveDataFetcher"
    priority = 0

    def __init__(self):
        super().__init__()
        self.calls = 0

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return pd.DataFrame()

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df

    def get_realtime_quote(self, stock_code: str) -> UnifiedRealtimeQuote:
        self.calls += 1
        return UnifiedRealtimeQuote(
            code=stock_code,
            source=RealtimeSource.TWELVE_DATA,
            price=100.0,
            change_pct=1.0,
            volume=1000,
        )


class _SupplementUSQuoteFetcher(BaseFetcher):
    name = "FinnhubFetcher"
    priority = 1

    def __init__(self):
        super().__init__()
        self.calls = 0

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return pd.DataFrame()

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df

    def get_realtime_quote(self, stock_code: str) -> UnifiedRealtimeQuote:
        self.calls += 1
        return UnifiedRealtimeQuote(
            code=stock_code,
            source=RealtimeSource.FINNHUB,
            price=101.0,
            amplitude=2.0,
        )


class TestFetcherLogging(unittest.TestCase):
    def test_base_fetcher_logs_start_and_success(self):
        fetcher = _SuccessFetcher()

        with self.assertLogs("data_provider.base", level="INFO") as captured:
            df = fetcher.get_daily_data("600519", start_date="2026-03-01", end_date="2026-03-08")

        log_text = "\n".join(captured.output)
        self.assertFalse(df.empty)
        self.assertIn("[SuccessFetcher] 开始获取 600519 日线数据", log_text)
        self.assertIn("[SuccessFetcher] 600519 获取成功:", log_text)
        self.assertIn("rows=2", log_text)

    def test_manager_logs_fallback_and_final_success(self):
        manager = DataFetcherManager(fetchers=[_FailureFetcher(), _SuccessFetcher()])

        with self.assertLogs("data_provider.base", level="INFO") as captured:
            df, source = manager.get_daily_data("601006", start_date="2026-01-07", end_date="2026-03-08")

        log_text = "\n".join(captured.output)
        self.assertFalse(df.empty)
        self.assertEqual(source, "SuccessFetcher")
        self.assertIn("[数据源尝试 1/2] [FailureFetcher] 获取 601006...", log_text)
        self.assertIn("[数据源失败 1/2] [FailureFetcher] 601006:", log_text)
        self.assertIn("[数据源切换] 601006: [FailureFetcher] -> [SuccessFetcher]", log_text)
        self.assertIn("[数据源完成] 601006 使用 [SuccessFetcher] 获取成功:", log_text)

    def test_us_daily_data_falls_back_when_first_source_is_stale_for_requested_end_date(self):
        manager = DataFetcherManager(fetchers=[_StaleMassiveFetcher(), _FreshTwelveDataFetcher()])

        with patch.dict(os.environ, {"US_DAILY_DATA_SOURCE_PRIORITY": "massive,twelvedata"}):
            with self.assertLogs("data_provider.base", level="INFO") as captured:
                df, source = manager.get_daily_data("AAPL", start_date="2026-03-01", end_date="2026-03-08")

        log_text = "\n".join(captured.output)
        self.assertEqual(source, "TwelveDataFetcher")
        self.assertEqual(str(pd.to_datetime(df["date"]).max().date()), "2026-03-08")
        self.assertIn("[数据源不完整", log_text)
        self.assertIn("latest=2026-03-07, requested_end=2026-03-08", log_text)

    def test_us_daily_data_can_use_ibkr_priority(self):
        manager = DataFetcherManager(fetchers=[_FreshIbkrFetcher(), _FreshTwelveDataFetcher()])

        with patch.dict(os.environ, {"US_DAILY_DATA_SOURCE_PRIORITY": "ibkr,twelvedata"}):
            df, source = manager.get_daily_data("AAPL", start_date="2026-03-01", end_date="2026-03-08")

        self.assertEqual(source, "IbkrFetcher")
        self.assertEqual(str(pd.to_datetime(df["date"]).max().date()), "2026-03-08")

    def test_us_realtime_can_stop_after_basic_quote_to_reduce_provider_calls(self) -> None:
        primary = _PrimaryUSQuoteFetcher()
        supplement = _SupplementUSQuoteFetcher()
        manager = DataFetcherManager(fetchers=[primary, supplement])

        with patch.dict(
            os.environ,
            {
                "US_REALTIME_DATA_SOURCE_PRIORITY": "twelvedata,finnhub",
                "US_REALTIME_STOP_AFTER_BASIC_QUOTE": "true",
            },
        ):
            quote = manager.get_realtime_quote("AAPL")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, RealtimeSource.TWELVE_DATA)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(supplement.calls, 0)

    def test_us_realtime_supplements_by_default(self) -> None:
        primary = _PrimaryUSQuoteFetcher()
        supplement = _SupplementUSQuoteFetcher()
        manager = DataFetcherManager(fetchers=[primary, supplement])

        with patch.dict(
            os.environ,
            {
                "US_REALTIME_DATA_SOURCE_PRIORITY": "twelvedata,finnhub",
                "US_REALTIME_STOP_AFTER_BASIC_QUOTE": "",
            },
        ):
            quote = manager.get_realtime_quote("AAPL")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, RealtimeSource.TWELVE_DATA)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(supplement.calls, 1)

    def test_ibkr_market_data_timeout_trips_process_cooldown(self):
        IbkrFetcher.reset_failure_cooldown()
        fetcher = IbkrFetcher()

        try:
            with patch.dict(os.environ, {"IBKR_FAILURE_COOLDOWN_SECONDS": "60"}), patch.object(
                fetcher, "_socket_is_available", return_value=True
            ), patch.object(
                fetcher,
                "_fetch_intraday_bars",
                side_effect=DataFetchError("IBKR intraday data timed out for AAPL"),
            ) as intraday_mock:
                with self.assertRaisesRegex(DataFetchError, "timed out"):
                    fetcher.get_intraday_bars_until("AAPL", datetime(2026, 5, 29, 9, 40))
                self.assertEqual(intraday_mock.call_count, 1)

                with self.assertRaisesRegex(DataFetchError, "temporarily disabled"):
                    fetcher.get_intraday_bars_until("NVDA", datetime(2026, 5, 29, 9, 40))
                self.assertEqual(intraday_mock.call_count, 1)
        finally:
            IbkrFetcher.reset_failure_cooldown()

    def test_massive_intraday_bars_are_filtered_to_cutoff(self):
        payload = {
            "status": "OK",
            "results": [
                {"t": 1780061400000, "o": 314.0, "h": 314.2, "l": 313.9, "c": 314.1, "v": 100},
                {"t": 1780062060000, "o": 315.0, "h": 315.2, "l": 314.9, "c": 315.1, "v": 120},
            ],
        }

        with tempfile.TemporaryDirectory() as cache_dir:
            with patch.dict(os.environ, {"MASSIVE_API_KEY": "test-key", "US_INTRADAY_CACHE_DIR": cache_dir}), patch(
                "data_provider.us_market_fetchers._get_json",
                return_value=payload,
            ) as get_json:
                fetcher = MassiveFetcher()
                rows = fetcher.get_intraday_bars_until("AAPL", datetime(2026, 5, 29, 9, 35))
                cached_rows = fetcher.get_intraday_bars_until("AAPL", datetime(2026, 5, 29, 9, 41))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp"], "2026-05-29 09:30:00")
        self.assertEqual(rows[0]["close"], 314.1)
        self.assertEqual(rows[0]["volume"], 100.0)
        self.assertEqual(len(cached_rows), 2)
        self.assertEqual(get_json.call_count, 1)

    def test_twelvedata_intraday_bars_are_sorted_and_filtered(self):
        payload = {
            "status": "ok",
            "values": [
                {
                    "datetime": "2026-05-29 09:41:00",
                    "open": "315.0",
                    "high": "315.2",
                    "low": "314.9",
                    "close": "315.1",
                    "volume": "120",
                },
                {
                    "datetime": "2026-05-29 09:39:00",
                    "open": "314.0",
                    "high": "314.2",
                    "low": "313.9",
                    "close": "314.1",
                    "volume": "100",
                },
            ],
        }

        with tempfile.TemporaryDirectory() as cache_dir:
            with patch.dict(os.environ, {"TWELVEDATA_API_KEY": "test-key", "US_INTRADAY_CACHE_DIR": cache_dir}), patch(
                "data_provider.us_market_fetchers._get_json",
                return_value=payload,
            ):
                rows = TwelveDataFetcher().get_intraday_bars_until("AAPL", datetime(2026, 5, 29, 9, 40))

        self.assertEqual([row["timestamp"] for row in rows], ["2026-05-29 09:39:00"])
        self.assertAlmostEqual(rows[0]["amount"], 31410.0)

    def test_intraday_json_retries_http_429(self):
        with patch.dict(os.environ, {"US_INTRADAY_429_RETRY_SECONDS": "0"}), patch(
            "data_provider.us_market_fetchers._get_json",
            side_effect=[DataFetchError("HTTP Error 429: Too Many Requests"), {"status": "OK"}],
        ) as get_json:
            data = _get_intraday_json("https://example.test", attempts=2)

        self.assertEqual(data, {"status": "OK"})
        self.assertEqual(get_json.call_count, 2)

    def test_efinance_logs_eastmoney_endpoint_on_remote_disconnect(self):
        fetcher = EfinanceFetcher()
        fake_efinance = types.SimpleNamespace(
            stock=types.SimpleNamespace(
                get_quote_history=lambda **kwargs: (_ for _ in ()).throw(
                    requests.exceptions.ConnectionError("Remote end closed connection without response")
                )
            )
        )

        with patch.dict(sys.modules, {"efinance": fake_efinance}):
            with patch.object(fetcher, "_set_random_user_agent", return_value=None), patch.object(
                fetcher, "_enforce_rate_limit", return_value=None
            ):
                with self.assertLogs(level="INFO") as captured:
                    with self.assertRaises(DataFetchError):
                        fetcher.get_daily_data("601006", start_date="2026-01-07", end_date="2026-03-08")

        log_text = "\n".join(captured.output)
        self.assertIn("Eastmoney 历史K线接口失败:", log_text)
        self.assertIn("endpoint=push2his.eastmoney.com/api/qt/stock/kline/get", log_text)
        self.assertIn("category=remote_disconnect", log_text)
        self.assertIn("[EfinanceFetcher] 601006 获取失败:", log_text)


if __name__ == "__main__":
    unittest.main()
