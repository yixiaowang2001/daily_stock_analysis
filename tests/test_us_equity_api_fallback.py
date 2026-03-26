# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from data_provider.base import DataFetchError
from data_provider.realtime_types import RealtimeSource
from data_provider import us_equity_api_fallback as fb


class TestUsEquityApiFallbackParse(unittest.TestCase):
    def test_alpha_vantage_daily_json_ok(self):
        data = {
            "Time Series (Daily)": {
                "2025-03-01": {
                    "1. open": "10",
                    "2. high": "11",
                    "3. low": "9",
                    "4. close": "10.5",
                    "5. volume": "1000",
                },
                "2025-03-02": {
                    "1. open": "10.5",
                    "2. high": "12",
                    "3. low": "10",
                    "4. close": "11.5",
                    "5. volume": "2000",
                },
            }
        }
        df = fb.fetch_alpha_vantage_daily_json(data, "TEST", "2025-03-01", "2025-03-02")
        self.assertEqual(len(df), 2)
        self.assertIn("pct_chg", df.columns)
        self.assertEqual(df["code"].iloc[0], "TEST")

    def test_alpha_vantage_daily_note_raises(self):
        data = {"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is 5 calls per minute."}
        with self.assertRaises(DataFetchError):
            fb.fetch_alpha_vantage_daily_json(data, "AAPL", "2025-01-01", "2025-12-31")

    def test_polygon_daily_json_ok(self):
        # ms timestamps for two UTC midnights
        t0 = int(pd.Timestamp("2025-03-01", tz="UTC").timestamp() * 1000)
        t1 = int(pd.Timestamp("2025-03-02", tz="UTC").timestamp() * 1000)
        data = {
            "status": "OK",
            "results": [
                {"t": t0, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000},
                {"t": t1, "o": 10.5, "h": 12, "l": 10, "c": 11.5, "v": 2000},
            ],
        }
        df = fb.fetch_polygon_daily_json(data, "AAPL", "2025-03-01", "2025-03-02")
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 11.5)

    def test_polygon_daily_empty_raises(self):
        with self.assertRaises(DataFetchError):
            fb.fetch_polygon_daily_json({"status": "OK", "results": []}, "AAPL", "2025-01-01", "2025-01-31")

    def test_twelve_daily_json_ok(self):
        data = {
            "status": "ok",
            "values": [
                {
                    "datetime": "2025-03-01",
                    "open": "10",
                    "high": "11",
                    "low": "9",
                    "close": "10.5",
                    "volume": "1000",
                },
                {
                    "datetime": "2025-03-02",
                    "open": "10.5",
                    "high": "12",
                    "low": "10",
                    "close": "11.5",
                    "volume": "2000",
                },
            ],
        }
        df = fb.fetch_twelve_daily_json(data, "AAPL", "2025-03-01", "2025-03-02")
        self.assertEqual(len(df), 2)

    def test_twelve_daily_error_raises(self):
        with self.assertRaises(DataFetchError):
            fb.fetch_twelve_daily_json(
                {"status": "error", "message": "Invalid API key"}, "AAPL", "2025-01-01", "2025-01-31"
            )

    def test_quote_alpha_vantage_global_json(self):
        data = {
            "Global Quote": {
                "01. symbol": "AAPL",
                "05. price": "180.12",
                "08. previous close": "179.00",
                "09. change": "1.12",
                "10. change percent": "0.6257%",
                "06. volume": "50000000",
                "02. open": "179.5",
                "03. high": "181.0",
                "04. low": "179.0",
            }
        }
        q = fb.quote_alpha_vantage_global_json(data, "AAPL")
        self.assertIsNotNone(q)
        assert q is not None
        self.assertEqual(q.source, RealtimeSource.ALPHA_VANTAGE)
        self.assertEqual(q.price, 180.12)
        self.assertEqual(q.pre_close, 179.0)

    def test_quote_polygon_last_trade_json(self):
        data = {"status": "OK", "results": {"p": 265.3, "s": 100}}
        q = fb.quote_polygon_last_trade_json(data, "AAPL")
        self.assertIsNotNone(q)
        assert q is not None
        self.assertEqual(q.source, RealtimeSource.MASSIVE)
        self.assertEqual(q.price, 265.3)
        self.assertEqual(q.volume, 100)

    def test_quote_twelve_price_json(self):
        q = fb.quote_twelve_price_json({"price": "199.99", "symbol": "AAPL"}, "AAPL")
        self.assertIsNotNone(q)
        assert q is not None
        self.assertEqual(q.source, RealtimeSource.TWELVE_DATA)
        self.assertEqual(q.price, 199.99)


class TestUsEquityHttp429(unittest.TestCase):
    @patch("data_provider.us_equity_api_fallback.requests.get")
    def test_http_429_raises(self, mock_get):
        resp = MagicMock()
        resp.status_code = 429
        mock_get.return_value = resp
        with self.assertRaises(DataFetchError) as ctx:
            fb._http_get_json("https://example.com", {})
        self.assertIn("429", str(ctx.exception))


class TestTryUsStockDailyFallbackOrder(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "ALPHA_VANTAGE_API_KEY": "k1",
            "POLYGON_API_KEY": "k2",
            "TWELVE_DATA_API_KEY": "k3",
        },
        clear=False,
    )
    @patch("data_provider.us_equity_api_fallback.fetch_alpha_vantage_daily")
    @patch("data_provider.us_equity_api_fallback.fetch_polygon_daily")
    @patch("data_provider.us_equity_api_fallback.fetch_twelve_daily")
    def test_stops_at_first_success(self, mock_td, mock_poly, mock_av):
        mock_av.side_effect = DataFetchError("empty")
        df_ok = pd.DataFrame(
            {
                "date": pd.to_datetime(["2025-01-02"]),
                "open": [1.0],
                "high": [1.1],
                "low": [0.9],
                "close": [1.05],
                "volume": [100.0],
                "amount": [105.0],
                "pct_chg": [0.0],
                "code": ["NVDA"],
            }
        )
        mock_poly.return_value = df_ok
        df, src = fb.try_us_stock_daily_fallback("NVDA", "2025-01-01", "2025-01-31")
        self.assertFalse(df.empty)
        self.assertEqual(src, fb.SOURCE_POLYGON_DAILY)
        mock_td.assert_not_called()


if __name__ == "__main__":
    unittest.main()
