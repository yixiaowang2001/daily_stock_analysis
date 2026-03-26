# -*- coding: utf-8 -*-
"""
美股 REST 兜底（yfinance 失败后的顺序：Alpha Vantage -> Massive/Polygon -> Twelve Data）。

- 密钥仅从环境变量读取，不写死。
- 不注册进 DataFetcherManager 的全局 fetcher 列表，仅由 base 层美股分支显式调用。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from .base import STANDARD_COLUMNS, DataFetchError
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int

logger = logging.getLogger(__name__)

ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
# Polygon REST 与 Massive 文档声明密钥与路径兼容；此处使用广泛稳定的 v2 域名
POLYGON_REST_BASE = "https://api.polygon.io"
TWELVE_DATA_BASE = "https://api.twelvedata.com"

SOURCE_AV_DAILY = "AlphaVantageUsFallback"
SOURCE_POLYGON_DAILY = "MassivePolygonUsFallback"
SOURCE_TWELVE_DAILY = "TwelveDataUsFallback"

_HTTP_TIMEOUT = (10, 45)

_av_lock = threading.Lock()
_av_last_request_mono: float = 0.0
_AV_MIN_INTERVAL_SEC = 12.0


def get_alpha_vantage_key() -> Optional[str]:
    return (os.getenv("ALPHA_VANTAGE_API_KEY") or "").strip() or None


def get_massive_or_polygon_key() -> Optional[str]:
    return (os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY") or "").strip() or None


def get_twelve_data_key() -> Optional[str]:
    return (os.getenv("TWELVE_DATA_API_KEY") or "").strip() or None


def _throttle_alpha_vantage() -> None:
    global _av_last_request_mono
    with _av_lock:
        now = time.monotonic()
        wait = _AV_MIN_INTERVAL_SEC - (now - _av_last_request_mono)
        if wait > 0:
            logger.debug(f"[AlphaVantage] 限速等待 {wait:.1f}s")
            time.sleep(wait)
        _av_last_request_mono = time.monotonic()


def _http_get_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.get(url, params=params, timeout=_HTTP_TIMEOUT)
    if r.status_code == 429:
        raise DataFetchError(f"HTTP 429 rate limit: {url}")
    r.raise_for_status()
    return r.json()


def resolve_daily_range(
    end_date: Optional[str],
    start_date: Optional[str],
    days: int,
) -> Tuple[str, str]:
    """与 BaseFetcher.get_daily_data 一致的日历范围（end 默认今天，start 默认回溯约 days*2 日历日）。"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if start_date is None:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=max(1, days) * 2)
        start_date = start_dt.strftime("%Y-%m-%d")
    return start_date, end_date


def _standardize_ohlcv_df(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    """输出与 YfinanceFetcher._normalize_data 一致的列子集（含 code + STANDARD_COLUMNS）。"""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "close" in out.columns:
        out["pct_chg"] = out["close"].pct_change() * 100
        out["pct_chg"] = out["pct_chg"].fillna(0).round(2)
    else:
        out["pct_chg"] = 0.0
    if "volume" in out.columns and "close" in out.columns:
        out["amount"] = out["volume"] * out["close"]
    else:
        out["amount"] = 0.0
    out["code"] = stock_code.strip().upper()
    keep = ["code"] + STANDARD_COLUMNS
    existing = [c for c in keep if c in out.columns]
    return out[existing]


def _filter_date_range(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df
    d0 = pd.to_datetime(start_date)
    d1 = pd.to_datetime(end_date)
    mask = (df["date"] >= d0) & (df["date"] <= d1)
    return df.loc[mask].reset_index(drop=True)


def fetch_alpha_vantage_daily_json(data: Dict[str, Any], symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """解析 TIME_SERIES_DAILY JSON（供测试与内部复用）。"""
    if "Note" in data or "Information" in data:
        msg = data.get("Note") or data.get("Information")
        raise DataFetchError(f"Alpha Vantage limit or info: {msg}")
    series = data.get("Time Series (Daily)")
    if not series:
        raise DataFetchError("Alpha Vantage: missing Time Series (Daily)")
    rows: List[Dict[str, Any]] = []
    for d_str, bar in series.items():
        rows.append(
            {
                "date": pd.to_datetime(d_str),
                "open": float(bar["1. open"]),
                "high": float(bar["2. high"]),
                "low": float(bar["3. low"]),
                "close": float(bar["4. close"]),
                "volume": float(bar["5. volume"]),
            }
        )
    df = pd.DataFrame(rows)
    df = df.sort_values("date", ascending=True).reset_index(drop=True)
    df = _filter_date_range(df, start_date, end_date)
    return _standardize_ohlcv_df(df, symbol)


def fetch_alpha_vantage_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    key = get_alpha_vantage_key()
    if not key:
        raise DataFetchError("ALPHA_VANTAGE_API_KEY not set")
    _throttle_alpha_vantage()
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol.strip().upper(),
        "outputsize": "compact",
        "apikey": key,
    }
    data = _http_get_json(ALPHA_VANTAGE_URL, params)
    return fetch_alpha_vantage_daily_json(data, symbol, start_date, end_date)


def fetch_polygon_daily_json(data: Dict[str, Any], symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    if data.get("status") == "ERROR" or (data.get("error")):
        raise DataFetchError(f"Polygon: {data.get('error', data)}")
    results = data.get("results") or []
    if not results:
        raise DataFetchError("Polygon: empty results")
    rows = []
    for bar in results:
        ts = bar.get("t")
        if ts is None:
            continue
        rows.append(
            {
                "date": pd.to_datetime(int(ts), unit="ms").normalize(),
                "open": float(bar["o"]),
                "high": float(bar["h"]),
                "low": float(bar["l"]),
                "close": float(bar["c"]),
                "volume": float(bar.get("v", 0)),
            }
        )
    df = pd.DataFrame(rows)
    df = df.sort_values("date", ascending=True).reset_index(drop=True)
    df = _filter_date_range(df, start_date, end_date)
    return _standardize_ohlcv_df(df, symbol)


def fetch_polygon_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    key = get_massive_or_polygon_key()
    if not key:
        raise DataFetchError("MASSIVE_API_KEY or POLYGON_API_KEY not set")
    sym = symbol.strip().upper()
    url = f"{POLYGON_REST_BASE}/v2/aggs/ticker/{sym}/range/1/day/{start_date}/{end_date}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}
    data = _http_get_json(url, params)
    return fetch_polygon_daily_json(data, symbol, start_date, end_date)


def fetch_twelve_daily_json(data: Dict[str, Any], symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    if data.get("status") == "error":
        raise DataFetchError(f"Twelve Data: {data.get('message', data)}")
    vals = data.get("values")
    if not vals:
        raise DataFetchError("Twelve Data: empty values")
    rows = []
    for bar in vals:
        rows.append(
            {
                "date": pd.to_datetime(bar["datetime"]),
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": float(bar.get("volume", 0)),
            }
        )
    df = pd.DataFrame(rows)
    df = df.sort_values("date", ascending=True).reset_index(drop=True)
    df = _filter_date_range(df, start_date, end_date)
    return _standardize_ohlcv_df(df, symbol)


def fetch_twelve_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    key = get_twelve_data_key()
    if not key:
        raise DataFetchError("TWELVE_DATA_API_KEY not set")
    params = {
        "symbol": symbol.strip().upper(),
        "interval": "1day",
        "start_date": start_date,
        "end_date": end_date,
        "apikey": key,
        "outputsize": 5000,
    }
    data = _http_get_json(f"{TWELVE_DATA_BASE}/time_series", params)
    return fetch_twelve_daily_json(data, symbol, start_date, end_date)


def try_us_stock_daily_fallback(symbol: str, start_date: str, end_date: str) -> Tuple[pd.DataFrame, str]:
    """
    按顺序尝试三源，返回 (标准化后、未 clean/indicators 的 DataFrame, 来源标签)。
    全部失败则抛出最后一个异常或汇总 DataFetchError。
    """
    errors: List[str] = []
    symbol_u = symbol.strip().upper()

    if get_alpha_vantage_key():
        try:
            df = fetch_alpha_vantage_daily(symbol_u, start_date, end_date)
            if df is not None and not df.empty:
                return df, SOURCE_AV_DAILY
        except Exception as e:
            errors.append(f"AlphaVantage: {e}")
            logger.warning(f"[UsEquityFallback] {symbol_u} Alpha Vantage 日线失败: {e}")

    if get_massive_or_polygon_key():
        try:
            df = fetch_polygon_daily(symbol_u, start_date, end_date)
            if df is not None and not df.empty:
                return df, SOURCE_POLYGON_DAILY
        except Exception as e:
            errors.append(f"Polygon: {e}")
            logger.warning(f"[UsEquityFallback] {symbol_u} Polygon/Massive 日线失败: {e}")

    if get_twelve_data_key():
        try:
            df = fetch_twelve_daily(symbol_u, start_date, end_date)
            if df is not None and not df.empty:
                return df, SOURCE_TWELVE_DAILY
        except Exception as e:
            errors.append(f"TwelveData: {e}")
            logger.warning(f"[UsEquityFallback] {symbol_u} Twelve Data 日线失败: {e}")

    raise DataFetchError(
        "美股 REST 兜底全部失败: " + ("; ".join(errors) if errors else "未配置任何 API Key")
    )


def _try_stock_name(symbol: str) -> str:
    try:
        from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name

        raw = STOCK_NAME_MAP.get(symbol.strip().upper(), "")
        return raw if is_meaningful_stock_name(raw, symbol) else ""
    except Exception:
        return ""


def quote_alpha_vantage_global_json(data: Dict[str, Any], symbol: str) -> Optional[UnifiedRealtimeQuote]:
    if "Note" in data or "Information" in data:
        logger.warning(f"[AlphaVantage] quote: {data.get('Note') or data.get('Information')}")
        return None
    gq = data.get("Global Quote") or {}
    if not gq:
        return None
    price = safe_float(gq.get("05. price"))
    if price is None:
        return None
    pre = safe_float(gq.get("08. previous close"))
    chg = safe_float(gq.get("09. change"))
    chg_pct_raw = gq.get("10. change percent")
    chg_pct = None
    if isinstance(chg_pct_raw, str) and chg_pct_raw.endswith("%"):
        chg_pct = safe_float(chg_pct_raw.rstrip("%").strip())
    elif chg_pct_raw is not None:
        chg_pct = safe_float(chg_pct_raw)
    if chg_pct is None and pre and chg is not None:
        chg_pct = (chg / pre) * 100 if pre else None
    sym = symbol.strip().upper()
    return UnifiedRealtimeQuote(
        code=sym,
        name=_try_stock_name(sym),
        source=RealtimeSource.ALPHA_VANTAGE,
        price=price,
        change_pct=round(chg_pct, 2) if chg_pct is not None else None,
        change_amount=round(chg, 4) if chg is not None else None,
        volume=safe_int(gq.get("06. volume")),
        amount=None,
        open_price=safe_float(gq.get("02. open")),
        high=safe_float(gq.get("03. high")),
        low=safe_float(gq.get("04. low")),
        pre_close=pre,
    )


def quote_alpha_vantage(symbol: str) -> Optional[UnifiedRealtimeQuote]:
    key = get_alpha_vantage_key()
    if not key:
        return None
    _throttle_alpha_vantage()
    params = {"function": "GLOBAL_QUOTE", "symbol": symbol.strip().upper(), "apikey": key}
    data = _http_get_json(ALPHA_VANTAGE_URL, params)
    return quote_alpha_vantage_global_json(data, symbol)


def quote_polygon_last_trade_json(data: Dict[str, Any], symbol: str) -> Optional[UnifiedRealtimeQuote]:
    if str(data.get("status", "")).upper() == "ERROR" or data.get("error"):
        logger.warning(f"[Polygon] last trade: {data.get('error', data)}")
        return None
    res = data.get("results")
    if not isinstance(res, dict):
        return None
    # Polygon v2 last trade: results.p = price, results.s = size
    price = safe_float(res.get("p") if "p" in res else res.get("price"))
    size = safe_int(res.get("s") if "s" in res else res.get("size"))
    if price is None:
        return None
    sym = symbol.strip().upper()
    return UnifiedRealtimeQuote(
        code=sym,
        name=_try_stock_name(sym),
        source=RealtimeSource.MASSIVE,
        price=price,
        volume=size,
        amount=None,
    )


def quote_polygon_last_trade(symbol: str) -> Optional[UnifiedRealtimeQuote]:
    key = get_massive_or_polygon_key()
    if not key:
        return None
    sym = symbol.strip().upper()
    url = f"{POLYGON_REST_BASE}/v2/last/trade/{sym}"
    data = _http_get_json(url, {"apiKey": key})
    return quote_polygon_last_trade_json(data, symbol)


def quote_twelve_price_json(data: Dict[str, Any], symbol: str) -> Optional[UnifiedRealtimeQuote]:
    if data.get("status") == "error":
        logger.warning(f"[TwelveData] price: {data.get('message', data)}")
        return None
    price = safe_float(data.get("price"))
    if price is None:
        return None
    sym = symbol.strip().upper()
    return UnifiedRealtimeQuote(
        code=sym,
        name=_try_stock_name(sym),
        source=RealtimeSource.TWELVE_DATA,
        price=price,
        amount=None,
    )


def quote_twelve_price(symbol: str) -> Optional[UnifiedRealtimeQuote]:
    key = get_twelve_data_key()
    if not key:
        return None
    params = {"symbol": symbol.strip().upper(), "apikey": key}
    data = _http_get_json(f"{TWELVE_DATA_BASE}/price", params)
    return quote_twelve_price_json(data, symbol)


def try_us_stock_realtime_fallback(symbol: str) -> Optional[UnifiedRealtimeQuote]:
    """yfinance（含 Stooq）均失败后，按 AV -> Polygon -> Twelve 尝试。"""
    sym = symbol.strip().upper()
    for fn in (quote_alpha_vantage, quote_polygon_last_trade, quote_twelve_price):
        try:
            q = fn(sym)
            if q is not None and q.price is not None:
                return q
        except Exception as e:
            logger.warning(f"[UsEquityFallback] {sym} 实时 {fn.__name__} 失败: {e}")
    return None


def probe_sample_json_summary(obj: Any, max_len: int = 1200) -> str:
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)[:max_len]
        return s + ("..." if len(json.dumps(obj, default=str)) > max_len else "")
    except Exception:
        return str(obj)[:max_len]
