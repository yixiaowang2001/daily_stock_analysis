# -*- coding: utf-8 -*-
"""Regression tests for quota-limited Iwencai fallback integration."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from data_provider.base import DataFetcherManager
from data_provider.iwencai_fetcher import IwencaiFetcher
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.config import Config
from src.search_service import SearchService
from src.services.iwencai_client import IwencaiClient, IwencaiQuotaExceeded


class _DummyRealtimeFetcher:
    def __init__(self, name: str, priority: int, result=None, error: Exception | None = None):
        self.name = name
        self.priority = priority
        self.result = result
        self.error = error
        self.calls = 0

    def get_realtime_quote(self, *_args, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def teardown_function():
    Config.reset_instance()


@patch("src.config.setup_env")
@patch.object(Config, "_parse_litellm_yaml", return_value=[])
def test_iwencai_realtime_priority_is_appended_as_tail_fallback(
    _mock_parse_yaml,
    _mock_setup_env,
):
    with patch.dict(
        os.environ,
        {
            "STOCK_LIST": "600519",
            "REALTIME_SOURCE_PRIORITY": "tencent,akshare_sina",
            "IWENCAI_API_KEY": "iwencai-secret",
        },
        clear=True,
    ):
        config = Config._load_from_env()

    assert config.enable_iwencai_fallback is True
    assert config.realtime_source_priority == "tencent,akshare_sina,iwencai"


@patch("src.config.setup_env")
@patch.object(Config, "_parse_litellm_yaml", return_value=[])
def test_iwencai_realtime_priority_respects_explicit_disable(
    _mock_parse_yaml,
    _mock_setup_env,
):
    with patch.dict(
        os.environ,
        {
            "STOCK_LIST": "600519",
            "REALTIME_SOURCE_PRIORITY": "tencent",
            "IWENCAI_API_KEY": "iwencai-secret",
            "ENABLE_IWENCAI_FALLBACK": "false",
        },
        clear=True,
    ):
        config = Config._load_from_env()

    assert config.enable_iwencai_fallback is False
    assert config.realtime_source_priority == "tencent"


def test_iwencai_client_uses_shared_daily_usage_file(tmp_path):
    usage_path = tmp_path / "iwencai_usage.json"
    first = IwencaiClient(
        api_key="iwencai-secret",
        daily_limit=2,
        usage_path=str(usage_path),
    )
    second = IwencaiClient(
        api_key="iwencai-secret",
        daily_limit=2,
        usage_path=str(usage_path),
    )

    assert first.reserve_call() == 1
    assert second.reserve_call() == 0
    with pytest.raises(IwencaiQuotaExceeded):
        first.reserve_call()

    payload = json.loads(usage_path.read_text(encoding="utf-8"))
    assert payload["count"] == 2
    assert payload["limit"] == 2


def test_iwencai_fetcher_normalizes_realtime_quote_response():
    class _Client:
        is_available = True

        def query2data(self, **kwargs):
            assert kwargs["skill_id"] == "hithink-market-query"
            assert "600519" in kwargs["query"]
            return {
                "datas": [
                    {
                        "股票代码": "600519.SH",
                        "股票简称": "贵州茅台",
                        "最新价": "1688.50",
                        "涨跌幅": "1.23%",
                        "成交量": "12.3万手",
                        "成交额": "2.4亿",
                        "换手率": "0.45%",
                        "量比": "1.08",
                    }
                ]
            }

    fetcher = IwencaiFetcher.__new__(IwencaiFetcher)
    fetcher.enabled = True
    fetcher.client = _Client()

    quote = fetcher.get_realtime_quote("SH600519")

    assert quote is not None
    assert quote.code == "600519"
    assert quote.name == "贵州茅台"
    assert quote.source == RealtimeSource.IWENCAI
    assert quote.price == 1688.50
    assert quote.change_pct == 1.23
    assert quote.volume == 123000
    assert quote.amount == 240000000


@patch("src.config.get_config")
def test_manager_reaches_iwencai_only_after_normal_realtime_sources_fail(mock_get_config):
    mock_get_config.return_value = SimpleNamespace(
        enable_realtime_quote=True,
        realtime_source_priority="efinance,iwencai",
    )
    iwencai_quote = UnifiedRealtimeQuote(
        code="600519",
        name="贵州茅台",
        source=RealtimeSource.IWENCAI,
        price=1688.50,
        change_pct=1.23,
    )
    efinance = _DummyRealtimeFetcher(
        "EfinanceFetcher",
        0,
        error=RuntimeError("efinance timeout"),
    )
    iwencai = _DummyRealtimeFetcher("IwencaiFetcher", 90, result=iwencai_quote)
    manager = DataFetcherManager(fetchers=[iwencai, efinance])

    quote = manager.get_realtime_quote("600519")

    assert quote is iwencai_quote
    assert efinance.calls == 1
    assert iwencai.calls == 1


@patch("src.config.get_config")
def test_manager_does_not_use_iwencai_to_supplement_existing_quote(mock_get_config):
    mock_get_config.return_value = SimpleNamespace(
        enable_realtime_quote=True,
        realtime_source_priority="efinance,iwencai",
    )
    efinance_quote = UnifiedRealtimeQuote(
        code="600519",
        name="贵州茅台",
        source=RealtimeSource.EFINANCE,
        price=1688.50,
        change_pct=1.23,
    )
    iwencai_quote = UnifiedRealtimeQuote(
        code="600519",
        name="贵州茅台",
        source=RealtimeSource.IWENCAI,
        price=1688.50,
        change_pct=1.23,
        volume_ratio=1.08,
        turnover_rate=0.45,
    )
    efinance = _DummyRealtimeFetcher("EfinanceFetcher", 0, result=efinance_quote)
    iwencai = _DummyRealtimeFetcher("IwencaiFetcher", 90, result=iwencai_quote)
    manager = DataFetcherManager(fetchers=[efinance, iwencai])

    quote = manager.get_realtime_quote("600519")

    assert quote is efinance_quote
    assert efinance.calls == 1
    assert iwencai.calls == 0


def test_search_service_appends_iwencai_provider_last():
    service = SearchService(
        bocha_keys=["bocha-secret"],
        searxng_public_instances_enabled=False,
        iwencai_api_key="iwencai-secret",
        enable_iwencai_fallback=True,
    )

    assert [provider.name for provider in service._providers] == ["Bocha", "Iwencai"]


def test_search_service_does_not_add_iwencai_when_disabled():
    service = SearchService(
        bocha_keys=["bocha-secret"],
        searxng_public_instances_enabled=False,
        iwencai_api_key="iwencai-secret",
        enable_iwencai_fallback=False,
    )

    assert [provider.name for provider in service._providers] == ["Bocha"]
