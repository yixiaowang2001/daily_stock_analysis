# -*- coding: utf-8 -*-
"""Iwencai OpenAPI fallback fetcher.

This fetcher intentionally keeps a narrow surface. Iwencai SkillHub has a
small shared daily quota, so DSA only uses it as a tail fallback for realtime
quote facts instead of a primary historical K-line source.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Iterable, Optional

import pandas as pd

from .base import BaseFetcher, DataFetchError, normalize_stock_code
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_int
from src.config import get_config
from src.services.iwencai_client import IwencaiAPIError, IwencaiClient, IwencaiQuotaExceeded, iter_response_items

logger = logging.getLogger(__name__)


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalize_compare_code(value: Any) -> str:
    text = _as_text(value).upper()
    if not text:
        return ""
    if text.startswith("HK"):
        return "HK" + text[2:].zfill(5)
    if text.endswith(".HK"):
        return "HK" + text[:-3].zfill(5)
    match = re.search(r"(\d{6})(?:\.(?:SH|SZ|BJ))?", text)
    if match:
        return match.group(1)
    return text


def _parse_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "--", "None", "nan"}:
        return None
    multiplier = 1.0
    if "亿" in text:
        multiplier = 100000000.0
    elif "万" in text:
        multiplier = 10000.0
    cleaned = (
        text.replace(",", "")
        .replace("%", "")
        .replace("元", "")
        .replace("股", "")
        .replace("手", "")
        .replace("亿", "")
        .replace("万", "")
        .strip()
    )
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group(0)) * multiplier
    except ValueError:
        return None


def _find_value(row: Dict[str, Any], candidates: Iterable[str]) -> Any:
    for candidate in candidates:
        if candidate in row:
            return row[candidate]
    lowered = [(str(k).lower(), k) for k in row.keys()]
    for candidate in candidates:
        needle = candidate.lower()
        for lower_key, original_key in lowered:
            if needle in lower_key:
                return row[original_key]
    return None


class IwencaiFetcher(BaseFetcher):
    """Low-priority Iwencai realtime quote fallback."""

    name = "IwencaiFetcher"
    priority = int(os.getenv("IWENCAI_PRIORITY", "90"))

    def __init__(self) -> None:
        config = get_config()
        self.enabled = bool(getattr(config, "enable_iwencai_fallback", False))
        self.client = IwencaiClient(
            api_key=getattr(config, "iwencai_api_key", None),
            base_url=getattr(config, "iwencai_base_url", "https://openapi.iwencai.com"),
            daily_limit=getattr(config, "iwencai_daily_call_limit", 100),
            timeout_seconds=getattr(config, "iwencai_timeout_seconds", 20.0),
            usage_path=getattr(config, "iwencai_usage_path", "./data/iwencai_usage.json"),
        )

    def is_available(self) -> bool:
        return self.enabled and self.client.is_available

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise DataFetchError("IwencaiFetcher only supports realtime fallback, not daily K-line data")

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        raise DataFetchError("IwencaiFetcher only supports realtime fallback, not daily K-line data")

    @staticmethod
    def _quote_query(stock_code: str) -> str:
        return (
            f"{stock_code} 最新价 涨跌幅 涨跌额 成交量 成交额 换手率 量比 "
            "振幅 开盘价 最高价 最低价 昨收价 市盈率 市净率 总市值 流通市值"
        )

    def _select_row(self, rows: Iterable[Dict[str, Any]], stock_code: str) -> Optional[Dict[str, Any]]:
        target = _normalize_compare_code(stock_code)
        first: Optional[Dict[str, Any]] = None
        for row in rows:
            if first is None:
                first = row
            row_code = _find_value(row, ("股票代码", "代码", "证券代码"))
            if _normalize_compare_code(row_code) == target:
                return row
        return first

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        if not self.is_available():
            return None

        normalized_code = normalize_stock_code(stock_code)
        query = self._quote_query(stock_code or normalized_code)

        try:
            raw = self.client.query2data(
                skill_id="hithink-market-query",
                query=query,
                limit=5,
            )
        except IwencaiQuotaExceeded as exc:
            logger.info("[IwencaiFetcher] daily quota exhausted, skip %s: %s", normalized_code, exc)
            return None
        except IwencaiAPIError as exc:
            raise DataFetchError(str(exc)) from exc

        rows = iter_response_items(raw)
        row = self._select_row(rows, normalized_code)
        if not row:
            logger.info("[IwencaiFetcher] %s returned no rows", normalized_code)
            return None

        quote = UnifiedRealtimeQuote(
            code=normalized_code,
            name=_as_text(_find_value(row, ("股票简称", "股票名称", "简称", "名称"))),
            source=RealtimeSource.IWENCAI,
            price=_parse_number(_find_value(row, ("最新价", "现价", "收盘价"))),
            change_pct=_parse_number(_find_value(row, ("涨跌幅", "涨幅"))),
            change_amount=_parse_number(_find_value(row, ("涨跌额",))),
            volume=safe_int(_parse_number(_find_value(row, ("成交量", "总手")))),
            amount=_parse_number(_find_value(row, ("成交额",))),
            volume_ratio=_parse_number(_find_value(row, ("量比",))),
            turnover_rate=_parse_number(_find_value(row, ("换手率",))),
            amplitude=_parse_number(_find_value(row, ("振幅",))),
            open_price=_parse_number(_find_value(row, ("开盘价", "今开"))),
            high=_parse_number(_find_value(row, ("最高价", "最高"))),
            low=_parse_number(_find_value(row, ("最低价", "最低"))),
            pre_close=_parse_number(_find_value(row, ("昨收价", "昨日收盘价", "昨收"))),
            pe_ratio=_parse_number(_find_value(row, ("市盈率", "PE"))),
            pb_ratio=_parse_number(_find_value(row, ("市净率", "PB"))),
            total_mv=_parse_number(_find_value(row, ("总市值",))),
            circ_mv=_parse_number(_find_value(row, ("流通市值",))),
        )

        if not quote.has_basic_data():
            logger.info("[IwencaiFetcher] %s returned row without basic price", normalized_code)
            return None

        logger.info(
            "[IwencaiFetcher] %s realtime fallback success: price=%s change_pct=%s",
            normalized_code,
            quote.price,
            quote.change_pct,
        )
        return quote
