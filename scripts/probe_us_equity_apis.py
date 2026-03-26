# -*- coding: utf-8 -*-
"""
本地探测美股 REST 兜底 API：从环境变量读取 Key，打印 AAPL 的原始 JSON 摘要与解析结果。
不使用 argparse；运行前在项目根目录配置 .env 或导出环境变量。
"""

import logging
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("probe_us_equity")

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import requests

from data_provider.us_equity_api_fallback import (
    ALPHA_VANTAGE_URL,
    POLYGON_REST_BASE,
    TWELVE_DATA_BASE,
    fetch_alpha_vantage_daily_json,
    fetch_polygon_daily_json,
    fetch_twelve_daily_json,
    get_alpha_vantage_key,
    get_massive_or_polygon_key,
    get_twelve_data_key,
    probe_sample_json_summary,
    quote_alpha_vantage_global_json,
    quote_polygon_last_trade_json,
    quote_twelve_price_json,
    resolve_daily_range,
)

SYMBOL = "AAPL"
_HTTP_TIMEOUT = (10, 45)


def main() -> None:
    start_date, end_date = resolve_daily_range(None, None, 30)
    print(f"探测标的={SYMBOL} 日线范围 {start_date} ~ {end_date}\n")

    av_key = get_alpha_vantage_key()
    if av_key:
        print("--- Alpha Vantage TIME_SERIES_DAILY ---")
        r = requests.get(
            ALPHA_VANTAGE_URL,
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": SYMBOL,
                "outputsize": "compact",
                "apikey": av_key,
            },
            timeout=_HTTP_TIMEOUT,
        )
        print("HTTP", r.status_code)
        dj = r.json()
        print("JSON摘要:", probe_sample_json_summary(dj))
        try:
            df = fetch_alpha_vantage_daily_json(dj, SYMBOL, start_date, end_date)
            print("解析日线 rows=", len(df), "cols=", list(df.columns))
            if not df.empty:
                print(df.tail(3).to_string())
        except Exception as exc:
            print("解析日线失败:", exc)

        # 免费版约 5 次/分钟，避免连续请求触发 Note
        time.sleep(13)

        print("\n--- Alpha Vantage GLOBAL_QUOTE ---")
        r2 = requests.get(
            ALPHA_VANTAGE_URL,
            params={"function": "GLOBAL_QUOTE", "symbol": SYMBOL, "apikey": av_key},
            timeout=_HTTP_TIMEOUT,
        )
        print("HTTP", r2.status_code)
        qj = r2.json()
        print("JSON摘要:", probe_sample_json_summary(qj))
        q = quote_alpha_vantage_global_json(qj, SYMBOL)
        print("解析报价:", q)
    else:
        print("跳过 Alpha Vantage（未设置 ALPHA_VANTAGE_API_KEY）")

    poly_key = get_massive_or_polygon_key()
    if poly_key:
        print("\n--- Polygon/Massive v2 aggs daily ---")
        url = f"{POLYGON_REST_BASE}/v2/aggs/ticker/{SYMBOL}/range/1/day/{start_date}/{end_date}"
        r = requests.get(
            url,
            params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": poly_key},
            timeout=_HTTP_TIMEOUT,
        )
        print("HTTP", r.status_code)
        dj = r.json()
        print("JSON摘要:", probe_sample_json_summary(dj))
        try:
            df = fetch_polygon_daily_json(dj, SYMBOL, start_date, end_date)
            print("解析日线 rows=", len(df), "cols=", list(df.columns))
            if not df.empty:
                print(df.tail(3).to_string())
        except Exception as exc:
            print("解析日线失败:", exc)

        print("\n--- Polygon/Massive v2 last trade ---")
        url_t = f"{POLYGON_REST_BASE}/v2/last/trade/{SYMBOL}"
        r3 = requests.get(url_t, params={"apiKey": poly_key}, timeout=_HTTP_TIMEOUT)
        print("HTTP", r3.status_code)
        tj = r3.json()
        print("JSON摘要:", probe_sample_json_summary(tj))
        print("解析报价:", quote_polygon_last_trade_json(tj, SYMBOL))
    else:
        print("\n跳过 Polygon/Massive（未设置 MASSIVE_API_KEY / POLYGON_API_KEY）")

    td_key = get_twelve_data_key()
    if td_key:
        print("\n--- Twelve Data time_series ---")
        r = requests.get(
            f"{TWELVE_DATA_BASE}/time_series",
            params={
                "symbol": SYMBOL,
                "interval": "1day",
                "start_date": start_date,
                "end_date": end_date,
                "apikey": td_key,
                "outputsize": 5000,
            },
            timeout=_HTTP_TIMEOUT,
        )
        print("HTTP", r.status_code)
        dj = r.json()
        print("JSON摘要:", probe_sample_json_summary(dj))
        try:
            df = fetch_twelve_daily_json(dj, SYMBOL, start_date, end_date)
            print("解析日线 rows=", len(df), "cols=", list(df.columns))
            if not df.empty:
                print(df.tail(3).to_string())
        except Exception as exc:
            print("解析日线失败:", exc)

        print("\n--- Twelve Data price ---")
        r4 = requests.get(
            f"{TWELVE_DATA_BASE}/price",
            params={"symbol": SYMBOL, "apikey": td_key},
            timeout=_HTTP_TIMEOUT,
        )
        print("HTTP", r4.status_code)
        pj = r4.json()
        print("JSON摘要:", probe_sample_json_summary(pj))
        print("解析报价:", quote_twelve_price_json(pj, SYMBOL))
    else:
        print("\n跳过 Twelve Data（未设置 TWELVE_DATA_API_KEY）")

    print("\n探测结束。")


if __name__ == "__main__":
    main()
