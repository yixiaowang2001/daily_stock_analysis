# -*- coding: utf-8 -*-
"""Small shared client for Iwencai OpenAPI fallback calls."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

DEFAULT_IWENCAI_BASE_URL = "https://openapi.iwencai.com"
DEFAULT_IWENCAI_USAGE_PATH = "./data/iwencai_usage.json"
_USAGE_LOCKS: Dict[str, RLock] = {}
_USAGE_LOCKS_GUARD = RLock()


class IwencaiAPIError(Exception):
    """Raised when Iwencai OpenAPI returns an invalid or failed response."""


class IwencaiQuotaExceeded(IwencaiAPIError):
    """Raised before a request when the local daily quota guard is exhausted."""


def _usage_lock_for(path: Path) -> RLock:
    key = str(path.expanduser().resolve())
    with _USAGE_LOCKS_GUARD:
        lock = _USAGE_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _USAGE_LOCKS[key] = lock
        return lock


class IwencaiClient:
    """Thin OpenAPI client with a local daily-call guard.

    The guard is intentionally conservative: it reserves one call before the
    HTTP request because failed upstream attempts still consume the practical
    daily budget for our fallback usage.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str],
        base_url: str = DEFAULT_IWENCAI_BASE_URL,
        daily_limit: int = 100,
        timeout_seconds: float = 20.0,
        usage_path: str = DEFAULT_IWENCAI_USAGE_PATH,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_IWENCAI_BASE_URL).rstrip("/")
        self.daily_limit = max(0, int(daily_limit or 0))
        self.timeout_seconds = max(0.1, float(timeout_seconds or 20.0))
        self.usage_path = Path(usage_path or DEFAULT_IWENCAI_USAGE_PATH).expanduser().resolve()
        self._usage_lock = _usage_lock_for(self.usage_path)

    @property
    def is_available(self) -> bool:
        return bool(self.api_key) and self.daily_limit > 0

    @staticmethod
    def _today_key() -> str:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

    def _read_usage(self) -> Dict[str, Any]:
        if not self.usage_path.exists():
            return {"date": self._today_key(), "count": 0}
        try:
            raw = json.loads(self.usage_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("[Iwencai] usage file is unreadable; resetting counter")
            return {"date": self._today_key(), "count": 0}
        if not isinstance(raw, dict):
            return {"date": self._today_key(), "count": 0}
        today = self._today_key()
        if raw.get("date") != today:
            return {"date": today, "count": 0}
        try:
            count = int(raw.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        return {"date": today, "count": max(0, count)}

    def _write_usage(self, usage: Dict[str, Any]) -> None:
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": usage.get("date") or self._today_key(),
            "count": int(usage.get("count", 0)),
            "limit": self.daily_limit,
        }
        tmp_path = self.usage_path.with_suffix(self.usage_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.usage_path)

    def reserve_call(self) -> int:
        """Reserve one local quota slot and return remaining calls."""
        if not self.is_available:
            raise IwencaiAPIError("Iwencai fallback is not configured")
        with self._usage_lock:
            usage = self._read_usage()
            count = int(usage.get("count", 0))
            if count >= self.daily_limit:
                raise IwencaiQuotaExceeded(
                    f"Iwencai daily call limit reached ({count}/{self.daily_limit})"
                )
            usage["count"] = count + 1
            self._write_usage(usage)
            return max(0, self.daily_limit - int(usage["count"]))

    def _headers(self, *, skill_id: str, call_type: str = "normal") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Claw-Call-Type": call_type,
            "X-Claw-Skill-Id": skill_id,
            "X-Claw-Skill-Version": "1.0.0",
            "X-Claw-Plugin-Id": "none",
            "X-Claw-Plugin-Version": "none",
            "X-Claw-Trace-Id": secrets.token_hex(32),
        }

    def post_json(self, path: str, *, skill_id: str, payload: Dict[str, Any], call_type: str = "normal") -> Dict[str, Any]:
        remaining = self.reserve_call()
        url = f"{self.base_url}{path}"
        logger.info("[Iwencai] 调用 fallback skill=%s, remaining_today=%s", skill_id, remaining)
        try:
            response = requests.post(
                url,
                headers=self._headers(skill_id=skill_id, call_type=call_type),
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise IwencaiAPIError(f"Iwencai request failed: {exc}") from exc

        if response.status_code >= 400:
            raise IwencaiAPIError(f"Iwencai HTTP {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise IwencaiAPIError("Iwencai response is not valid JSON") from exc
        if isinstance(data, list):
            return {"data": data}
        if not isinstance(data, dict):
            raise IwencaiAPIError("Iwencai response JSON is not an object")
        return data

    def query2data(
        self,
        *,
        skill_id: str,
        query: str,
        page: int = 1,
        limit: int = 10,
        call_type: str = "normal",
    ) -> Dict[str, Any]:
        return self.post_json(
            "/v1/query2data",
            skill_id=skill_id,
            call_type=call_type,
            payload={
                "query": query,
                "page": str(page),
                "limit": str(limit),
                "is_cache": "1",
                "expand_index": "true",
            },
        )

    def comprehensive_search(
        self,
        *,
        skill_id: str,
        channels: Iterable[str],
        query: str,
        call_type: str = "normal",
    ) -> Dict[str, Any]:
        return self.post_json(
            "/v1/comprehensive/search",
            skill_id=skill_id,
            call_type=call_type,
            payload={
                "channels": list(channels),
                "app_id": "AIME_SKILL",
                "query": query,
            },
        )


def iter_response_items(raw: Any) -> List[Dict[str, Any]]:
    """Best-effort extraction of row/list items from heterogeneous responses."""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []

    for key in ("datas", "data", "result", "results", "items", "records", "list"):
        value = raw.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = iter_response_items(value)
            if nested:
                return nested
    return []
