# -*- coding: utf-8 -*-
"""Tests for the Iwencai realtime quote fallback fetcher."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from data_provider.base import DataFetcherManager
from data_provider.iwencai_fetcher import IwencaiFetcher
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.services.iwencai_client import IwencaiQuotaExceeded


class _DummyIwencaiClient:
    is_available = True

    def __init__(self, raw=None, error=None):
        self.raw = raw
        self.error = error
        self.queries = []

    def query2data(self, **kwargs):
        self.queries.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.raw


class _DummyFetcher:
    def __init__(self, name: str, priority: int, result=None, error: Exception | None = None):
        self.name = name
        self.priority = priority
        self._result = result
        self._error = error

    def get_realtime_quote(self, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return self._result


def _make_manager_quote(source: RealtimeSource = RealtimeSource.IWENCAI) -> UnifiedRealtimeQuote:
    return UnifiedRealtimeQuote(
        code="600519",
        name="贵州茅台",
        source=source,
        price=1688.0,
        change_pct=1.23,
        volume_ratio=1.2,
        turnover_rate=0.88,
        pe_ratio=30.0,
        pb_ratio=8.0,
        total_mv=2000000000000.0,
        circ_mv=2000000000000.0,
        amplitude=2.0,
    )


def test_iwencai_fetcher_parses_realtime_quote(monkeypatch):
    config = SimpleNamespace(
        enable_iwencai_fallback=True,
        iwencai_api_key="iw-test-key",
        iwencai_base_url="https://example.test",
        iwencai_daily_call_limit=100,
        iwencai_timeout_seconds=3.0,
        iwencai_usage_path="./data/test_iwencai_usage.json",
    )
    monkeypatch.setattr("data_provider.iwencai_fetcher.get_config", lambda: config)

    fetcher = IwencaiFetcher()
    client = _DummyIwencaiClient(
        {
            "datas": [
                {
                    "股票代码": "600519.SH",
                    "股票简称": "贵州茅台",
                    "最新价": "1688.00",
                    "涨跌幅": "1.23%",
                    "涨跌额": "20.50",
                    "成交量": "12.3万手",
                    "成交额": "4.5亿",
                    "换手率": "0.88%",
                    "量比": "1.2",
                    "振幅": "2.0%",
                    "市盈率": "30",
                    "市净率": "8",
                }
            ]
        }
    )
    fetcher.client = client

    quote = fetcher.get_realtime_quote("600519")

    assert quote is not None
    assert quote.source == RealtimeSource.IWENCAI
    assert quote.code == "600519"
    assert quote.name == "贵州茅台"
    assert quote.price == 1688.0
    assert quote.change_pct == 1.23
    assert quote.volume == 123000
    assert quote.amount == 450000000.0
    assert quote.turnover_rate == 0.88
    assert quote.volume_ratio == 1.2
    assert client.queries[0]["skill_id"] == "hithink-market-query"


def test_iwencai_fetcher_returns_none_when_local_quota_exhausted(monkeypatch):
    config = SimpleNamespace(
        enable_iwencai_fallback=True,
        iwencai_api_key="iw-test-key",
        iwencai_base_url="https://example.test",
        iwencai_daily_call_limit=100,
        iwencai_timeout_seconds=3.0,
        iwencai_usage_path="./data/test_iwencai_usage.json",
    )
    monkeypatch.setattr("data_provider.iwencai_fetcher.get_config", lambda: config)

    fetcher = IwencaiFetcher()
    fetcher.client = _DummyIwencaiClient(error=IwencaiQuotaExceeded("quota used"))

    assert fetcher.get_realtime_quote("600519") is None


@patch("src.config.get_config")
def test_manager_tries_iwencai_realtime_tail_fallback(mock_get_config):
    mock_get_config.return_value = SimpleNamespace(
        enable_realtime_quote=True,
        realtime_source_priority="efinance,iwencai",
    )
    manager = DataFetcherManager(
        fetchers=[
            _DummyFetcher("EfinanceFetcher", 0, error=RuntimeError("efinance timeout")),
            _DummyFetcher("IwencaiFetcher", 90, result=_make_manager_quote()),
        ]
    )

    quote = manager.get_realtime_quote("600519")

    assert quote is not None
    assert quote.source == RealtimeSource.IWENCAI
    assert quote.price == 1688.0
