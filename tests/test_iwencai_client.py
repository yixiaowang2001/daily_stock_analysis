# -*- coding: utf-8 -*-
"""Tests for the Iwencai OpenAPI fallback client."""

from __future__ import annotations

import json

import pytest

from src.services.iwencai_client import IwencaiClient, IwencaiQuotaExceeded, iter_response_items


class _FakeResponse:
    status_code = 200
    text = '{"datas":[]}'

    def json(self):
        return {"datas": [{"股票代码": "600519.SH", "最新价": "1688.00"}]}


class _FakeListResponse:
    status_code = 200
    text = '[{"title":"news"}]'

    def json(self):
        return [{"title": "news"}]


def test_iwencai_client_reserves_daily_quota_before_request(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return _FakeResponse()

    monkeypatch.setattr("src.services.iwencai_client.requests.post", fake_post)

    usage_path = tmp_path / "iwencai_usage.json"
    client = IwencaiClient(
        api_key="iw-test-key",
        base_url="https://example.test",
        daily_limit=1,
        timeout_seconds=3,
        usage_path=str(usage_path),
    )

    response = client.query2data(
        skill_id="hithink-market-query",
        query="贵州茅台 最新价",
    )

    assert response["datas"][0]["股票代码"] == "600519.SH"
    assert len(calls) == 1
    assert calls[0]["url"] == "https://example.test/v1/query2data"
    assert calls[0]["headers"]["Authorization"] == "Bearer iw-test-key"
    assert calls[0]["headers"]["X-Claw-Skill-Id"] == "hithink-market-query"
    assert calls[0]["json"]["query"] == "贵州茅台 最新价"
    assert json.loads(usage_path.read_text(encoding="utf-8"))["count"] == 1

    with pytest.raises(IwencaiQuotaExceeded):
        client.query2data(
            skill_id="hithink-market-query",
            query="贵州茅台 最新价",
        )
    assert len(calls) == 1


def test_iwencai_client_wraps_list_response(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.services.iwencai_client.requests.post",
        lambda *args, **kwargs: _FakeListResponse(),
    )
    client = IwencaiClient(
        api_key="iw-test-key",
        base_url="https://example.test",
        usage_path=str(tmp_path / "usage.json"),
    )

    response = client.comprehensive_search(
        skill_id="news-search",
        channels=["news"],
        query="贵州茅台 新闻",
    )

    assert response == {"data": [{"title": "news"}]}


def test_iwencai_clients_share_usage_lock_for_same_path(tmp_path):
    usage_path = tmp_path / "iwencai_usage.json"
    first = IwencaiClient(api_key="iw-test-key", usage_path=str(usage_path))
    second = IwencaiClient(api_key="iw-test-key", usage_path=str(usage_path))

    assert first._usage_lock is second._usage_lock


def test_iter_response_items_extracts_common_nested_shapes():
    assert iter_response_items({"data": [{"title": "a"}, {"title": "b"}]}) == [
        {"title": "a"},
        {"title": "b"},
    ]
    assert iter_response_items({"result": {"items": [{"title": "nested"}]}}) == [
        {"title": "nested"},
    ]
    assert iter_response_items({"data": "not-a-list"}) == []
