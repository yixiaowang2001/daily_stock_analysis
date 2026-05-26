# -*- coding: utf-8 -*-
"""Tests for Iwencai search fallback integration."""

from __future__ import annotations

from src.search_service import IwencaiSearchProvider, SearchService


class _DummyIwencaiSearchClient:
    is_available = True

    def __init__(self):
        self.queries = []

    def comprehensive_search(self, **kwargs):
        self.queries.append(kwargs)
        return {
            "data": [
                {
                    "title": "贵州茅台发布经营数据",
                    "summary": "公司披露最新经营情况。",
                    "url": "https://example.test/news/1",
                    "publish_date": "2026-05-26 09:30:00",
                    "source": "同花顺财经",
                },
                {
                    "title": "贵州茅台发布经营数据",
                    "summary": "重复 URL 应被去重。",
                    "url": "https://example.test/news/1",
                    "publish_date": "2026-05-26 09:30:00",
                    "source": "同花顺财经",
                },
            ]
        }


def test_iwencai_search_provider_parses_news_results(tmp_path):
    provider = IwencaiSearchProvider(
        "iw-test-key",
        base_url="https://example.test",
        usage_path=str(tmp_path / "usage.json"),
    )
    client = _DummyIwencaiSearchClient()
    provider._client = client

    response = provider.search("贵州茅台 新闻", max_results=5)

    assert response.success is True
    assert response.provider == "Iwencai"
    assert len(response.results) == 1
    assert response.results[0].title == "贵州茅台发布经营数据"
    assert response.results[0].source == "同花顺财经"
    assert client.queries[0]["skill_id"] == "news-search"
    assert client.queries[0]["channels"] == ["news"]


def test_search_service_appends_iwencai_as_last_provider():
    service = SearchService(
        bocha_keys=["bocha-test-key"],
        searxng_public_instances_enabled=False,
        iwencai_api_key="iw-test-key",
        enable_iwencai_fallback=True,
    )

    provider_names = [provider.name for provider in service._providers]

    assert provider_names[0] == "Bocha"
    assert provider_names[-1] == "Iwencai"
