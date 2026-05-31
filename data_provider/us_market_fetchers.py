# -*- coding: utf-8 -*-
"""Optional US market data fetchers with free-tier friendly fail-open behavior."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int
from .us_index_mapping import is_us_stock_code

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; DSA/1.0; +https://github.com/ZhuLinsen/daily_stock_analysis)"

_IBKR_INFO_CODES = {2104, 2106, 2107, 2108, 2157, 2158}


def _redact_sensitive_text(value: Any) -> str:
    text = str(value or "")
    for env_name in (
        "ALPHA_VANTAGE_API_KEY",
        "FINNHUB_API_KEY",
        "TWELVEDATA_API_KEY",
        "MASSIVE_API_KEY",
        "POLYGON_API_KEY",
    ):
        secret = (os.getenv(env_name) or "").strip()
        if secret:
            text = text.replace(secret, "***")
    return re.sub(r"(?i)(apikey|apiKey|token)=([^&\s]+)", r"\1=***", text)


def _get_json(url: str, *, timeout: float = 15.0) -> Dict[str, Any]:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", "ignore")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise DataFetchError(_redact_sensitive_text(str(exc))) from exc
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DataFetchError("response is not JSON") from exc
    if not isinstance(data, dict):
        raise DataFetchError("response JSON is not an object")
    return data


def _intraday_429_retry_seconds() -> float:
    raw = (os.getenv("US_INTRADAY_429_RETRY_SECONDS") or "15").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 15.0


def _get_intraday_json(url: str, *, timeout: float = 20.0, attempts: int = 4) -> Dict[str, Any]:
    last_error: Optional[DataFetchError] = None
    for attempt in range(max(1, attempts)):
        try:
            return _get_json(url, timeout=timeout)
        except DataFetchError as exc:
            last_error = exc
            if "429" not in str(exc) or attempt >= attempts - 1:
                raise
            delay = _intraday_429_retry_seconds()
            if delay > 0:
                time.sleep(delay * (attempt + 1))
    raise last_error or DataFetchError("intraday request failed")


def _pct_change(price: Optional[float], prev_close: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    if price is None or prev_close is None or prev_close <= 0:
        return None, None
    change = price - prev_close
    return round(change, 4), round(change / prev_close * 100, 2)


def _amplitude(high: Optional[float], low: Optional[float], prev_close: Optional[float]) -> Optional[float]:
    if high is None or low is None or prev_close is None or prev_close <= 0:
        return None
    return round((high - low) / prev_close * 100, 2)


def _list_at(values: Any, idx: int) -> Any:
    if not isinstance(values, list) or idx >= len(values):
        return None
    return values[idx]


def _standardize_daily_rows(rows: list[Dict[str, Any]], stock_code: str) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["code", *STANDARD_COLUMNS])
    df["code"] = stock_code
    if "pct_chg" not in df.columns:
        df["pct_chg"] = pd.to_numeric(df["close"], errors="coerce").pct_change() * 100
    if "amount" not in df.columns:
        df["amount"] = pd.to_numeric(df["volume"], errors="coerce") * pd.to_numeric(df["close"], errors="coerce")
    keep_cols = ["code", *STANDARD_COLUMNS]
    for col in keep_cols:
        if col not in df.columns:
            df[col] = None
    return df[keep_cols]


def _ibkr_safe_int_100_share_lots(value: Any) -> Optional[int]:
    """IBKR reports US stock volume in 100-share lots for historical bars."""
    volume = safe_float(value)
    if volume is None:
        return None
    return int(volume * 100)


def _ibkr_bar_date(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def _ibkr_bar_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return (
                datetime.fromtimestamp(int(text), tz=timezone.utc)
                .astimezone(ZoneInfo("America/New_York"))
                .replace(tzinfo=None)
            )
        except (OSError, ValueError):
            pass
    parts = text.split()
    if len(parts) >= 2 and len(parts[0]) == 8 and parts[0].isdigit():
        try:
            return datetime.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d %H:%M:%S")
        except ValueError:
            pass
    cleaned = (
        text.replace(" US/Eastern", "")
        .replace(" America/New_York", "")
        .replace(" EDT", "")
        .replace(" EST", "")
    )
    parsed = pd.to_datetime(cleaned, errors="coerce")
    if pd.isna(parsed):
        return None
    if getattr(parsed, "tzinfo", None) is not None:
        parsed = parsed.tz_convert("America/New_York").tz_localize(None)
    return parsed.to_pydatetime()


def _as_eastern_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)


def _parse_provider_intraday_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    if getattr(parsed, "tzinfo", None) is not None:
        parsed = parsed.tz_convert("America/New_York").tz_localize(None)
    return parsed.to_pydatetime()


def _provider_intraday_row(bar_dt: datetime, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    close = safe_float(payload.get("close") or payload.get("c"))
    if close is None:
        return None
    volume = safe_float(payload.get("volume") or payload.get("v"))
    return {
        "timestamp": bar_dt.isoformat(sep=" "),
        "date": bar_dt.date().isoformat(),
        "time": bar_dt.time().isoformat(),
        "open": safe_float(payload.get("open") or payload.get("o")),
        "high": safe_float(payload.get("high") or payload.get("h")),
        "low": safe_float(payload.get("low") or payload.get("l")),
        "close": close,
        "volume": volume,
        "amount": volume * close if volume is not None else None,
    }


def _validate_intraday_request(symbol: str, cutoff_at: datetime, *, bar_size: str) -> tuple[str, datetime]:
    normalized_bar = (bar_size or "").replace(" ", "").lower()
    if normalized_bar not in {"1min", "1m"}:
        raise DataFetchError(f"only 1-minute intraday bars are supported, got {bar_size}")
    local_cutoff = _as_eastern_naive(cutoff_at)
    return symbol, local_cutoff


def _regular_hours_contains(value: datetime) -> bool:
    current = value.time()
    return dt_time(9, 30) <= current <= dt_time(16, 0)


def _intraday_cache_dir() -> Path:
    return Path(os.getenv("US_INTRADAY_CACHE_DIR") or "data/us_intraday_cache")


def _intraday_cache_path(provider: str, symbol: str, trade_date: str) -> Path:
    safe_symbol = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol.upper())
    return _intraday_cache_dir() / provider / f"{safe_symbol}_{trade_date}.json"


def _filter_intraday_rows_until(rows: list[Dict[str, Any]], cutoff_at: datetime) -> list[Dict[str, Any]]:
    cutoff_local = _as_eastern_naive(cutoff_at)
    cutoff_text = cutoff_local.isoformat(sep=" ")
    trade_date_text = cutoff_local.date().isoformat()
    scoped = [
        row
        for row in rows
        if str(row.get("date") or "") == trade_date_text
        and str(row.get("timestamp") or "") <= cutoff_text
    ]
    scoped.sort(key=lambda item: str(item.get("timestamp") or ""))
    return scoped


def _intraday_cache_covers_cutoff(rows: list[Dict[str, Any]], cutoff_at: datetime) -> bool:
    scoped = _filter_intraday_rows_until(rows, cutoff_at)
    if not scoped:
        return False
    last_dt = _parse_provider_intraday_datetime(scoped[-1].get("timestamp"))
    if last_dt is None:
        return False
    cutoff_local = _as_eastern_naive(cutoff_at)
    lag_minutes = max(0.0, (cutoff_local - last_dt).total_seconds() / 60)
    return lag_minutes <= 90


def _read_intraday_cache(provider: str, symbol: str, cutoff_at: datetime) -> Optional[list[Dict[str, Any]]]:
    path = _intraday_cache_path(provider, symbol, _as_eastern_naive(cutoff_at).date().isoformat())
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    normalized_rows = [row for row in rows if isinstance(row, dict)]
    if not _intraday_cache_covers_cutoff(normalized_rows, cutoff_at):
        return None
    return _filter_intraday_rows_until(normalized_rows, cutoff_at)


def _write_intraday_cache(provider: str, symbol: str, trade_date: str, rows: list[Dict[str, Any]]) -> None:
    if not rows:
        return
    path = _intraday_cache_path(provider, symbol, trade_date)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "provider": provider,
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                    "rows": rows,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("[US intraday cache] write failed for %s %s: %s", provider, symbol, exc)


def _ibkr_duration(start_date: str, end_date: str) -> str:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    days = max(1, (end_dt - start_dt).days + 7)
    if days <= 364:
        return f"{days} D"
    return f"{(days + 364) // 365} Y"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _clean_text(value: Any, *, max_len: Optional[int] = None) -> Optional[str]:
    text = str(value or "").strip()
    if not text or text.lower() in {"-", "--", "none", "null", "nan", "n/a", "na"}:
        return None
    if max_len is not None and len(text) > max_len:
        return text[:max_len].rstrip() + "..."
    return text


def _first_float(payload: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = safe_float(payload.get(key))
        if value is not None:
            return value
    return None


def _compact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str) and _clean_text(value) is None:
            continue
        compact[key] = value
    return compact


def _payload_has_data(payload: Any) -> bool:
    if payload is None:
        return False
    if isinstance(payload, dict):
        return any(_payload_has_data(value) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_payload_has_data(value) for value in payload)
    if isinstance(payload, str):
        return _clean_text(payload) is not None
    return True


def _snapshot_status(*blocks: Dict[str, Any]) -> str:
    return "ok" if any(_payload_has_data(block) for block in blocks) else "not_supported"


class _USKeyedFetcher(BaseFetcher):
    """Base helper for optional keyed US data APIs."""

    api_key_env: str = ""
    source: RealtimeSource = RealtimeSource.FALLBACK

    def _api_key(self) -> Optional[str]:
        return (os.getenv(self.api_key_env) or "").strip() or None

    def _ensure_symbol(self, stock_code: str) -> str:
        symbol = (stock_code or "").strip().upper()
        if not is_us_stock_code(symbol):
            raise DataFetchError(f"{self.name} only supports US stock tickers")
        return symbol

    def _is_available(self) -> bool:
        return bool(self._api_key())

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df


class IbkrFetcher(BaseFetcher):
    """Interactive Brokers Gateway US market data fetcher.

    The implementation only requests historical bars and market-data snapshots.
    It does not use order, account modification, or trading endpoints.
    """

    name = "IbkrFetcher"
    priority = int(os.getenv("IBKR_PRIORITY", "4"))
    source = RealtimeSource.IBKR
    _cooldown_lock = threading.Lock()
    _cooldown_until = 0.0
    _cooldown_reason: Optional[str] = None

    def _ensure_symbol(self, stock_code: str) -> str:
        symbol = (stock_code or "").strip().upper()
        if not is_us_stock_code(symbol):
            raise DataFetchError("IbkrFetcher only supports US stock tickers")
        return symbol

    def _host(self) -> str:
        return (os.getenv("IBKR_HOST") or "127.0.0.1").strip()

    def _port(self) -> int:
        raw = (os.getenv("IBKR_PORT") or "4001").strip()
        try:
            return int(raw)
        except ValueError as exc:
            raise DataFetchError(f"invalid IBKR_PORT: {raw}") from exc

    def _client_id(self, offset: int = 0) -> int:
        raw = (os.getenv("IBKR_CLIENT_ID") or "9701").strip()
        try:
            return int(raw) + offset
        except ValueError as exc:
            raise DataFetchError(f"invalid IBKR_CLIENT_ID: {raw}") from exc

    def _timeout(self) -> float:
        raw = (os.getenv("IBKR_TIMEOUT_SECONDS") or "20").strip()
        try:
            return max(3.0, float(raw))
        except ValueError as exc:
            raise DataFetchError(f"invalid IBKR_TIMEOUT_SECONDS: {raw}") from exc

    def _failure_cooldown_seconds(self) -> float:
        raw = (os.getenv("IBKR_FAILURE_COOLDOWN_SECONDS") or "300").strip()
        try:
            return max(0.0, float(raw))
        except ValueError as exc:
            raise DataFetchError(f"invalid IBKR_FAILURE_COOLDOWN_SECONDS: {raw}") from exc

    def _market_data_type(self) -> int:
        raw = (os.getenv("IBKR_MARKET_DATA_TYPE") or "1").strip()
        try:
            return int(raw)
        except ValueError as exc:
            raise DataFetchError(f"invalid IBKR_MARKET_DATA_TYPE: {raw}") from exc

    @classmethod
    def reset_failure_cooldown(cls) -> None:
        with cls._cooldown_lock:
            cls._cooldown_until = 0.0
            cls._cooldown_reason = None

    def _cooldown_remaining_seconds(self) -> float:
        with self.__class__._cooldown_lock:
            return max(0.0, self.__class__._cooldown_until - time.monotonic())

    def _cooldown_message(self) -> Optional[str]:
        remaining = self._cooldown_remaining_seconds()
        if remaining <= 0:
            return None
        with self.__class__._cooldown_lock:
            reason = self.__class__._cooldown_reason or "recent IBKR market-data failure"
        return f"IBKR temporarily disabled for {remaining:.0f}s after failure: {reason}"

    def _mark_failure_cooldown(self, reason: str) -> None:
        cooldown = self._failure_cooldown_seconds()
        if cooldown <= 0:
            return
        reason_text = _redact_sensitive_text(reason)
        with self.__class__._cooldown_lock:
            self.__class__._cooldown_until = time.monotonic() + cooldown
            self.__class__._cooldown_reason = reason_text
        logger.warning("[IBKR] market-data requests cooling down for %.0fs: %s", cooldown, reason_text)

    def _clear_failure_cooldown(self) -> None:
        self.reset_failure_cooldown()

    def _socket_is_available(self) -> bool:
        try:
            with socket.create_connection((self._host(), self._port()), timeout=1.0):
                return True
        except OSError:
            return False

    def _is_available(self) -> bool:
        if self._cooldown_remaining_seconds() > 0:
            return False
        return self._socket_is_available()

    def _unavailable_error(self) -> str:
        cooldown = self._cooldown_message()
        if cooldown:
            return cooldown
        return f"IBKR Gateway is not listening at {self._host()}:{self._port()}"

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        symbol = self._ensure_symbol(stock_code)
        if not self._is_available():
            raise DataFetchError(self._unavailable_error())
        try:
            df = self._fetch_historical_bars(symbol, start_date, end_date)
            self._clear_failure_cooldown()
            return df
        except DataFetchError as exc:
            self._mark_failure_cooldown(str(exc))
            raise

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        try:
            symbol = self._ensure_symbol(stock_code)
            if not self._is_available():
                return None
            quote = self._fetch_snapshot_quote(symbol)
            if quote and quote.has_basic_data():
                return quote

            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
            df = self._fetch_historical_bars(symbol, start_date, end_date)
            if df.empty:
                return None
            self._clear_failure_cooldown()
            df = df.sort_values("date")
            latest = df.iloc[-1]
            prev_close = safe_float(df.iloc[-2]["close"]) if len(df) >= 2 else None
            price = safe_float(latest.get("close"))
            if price is None or price <= 0:
                return None
            change_amount, change_pct = _pct_change(price, prev_close)
            high = safe_float(latest.get("high"))
            low = safe_float(latest.get("low"))
            volume = safe_int(latest.get("volume"))
            return UnifiedRealtimeQuote(
                code=symbol,
                source=self.source,
                price=price,
                change_pct=change_pct,
                change_amount=change_amount,
                volume=volume,
                amount=volume * price if volume is not None else None,
                open_price=safe_float(latest.get("open")),
                high=high,
                low=low,
                pre_close=prev_close,
                amplitude=_amplitude(high, low, prev_close),
            )
        except Exception as exc:
            if isinstance(exc, DataFetchError):
                self._mark_failure_cooldown(str(exc))
            logger.debug("[IBKR] realtime failed for %s: %s", stock_code, exc)
            return None

    def _contract(self, symbol: str):
        try:
            from ibapi.contract import Contract
        except ImportError as exc:
            raise DataFetchError("ibapi is not installed; run `pip install -r requirements.txt`") from exc

        contract = Contract()
        contract.symbol = symbol.replace(".", " ")
        contract.secType = "STK"
        contract.exchange = os.getenv("IBKR_EXCHANGE", "SMART")
        contract.currency = os.getenv("IBKR_CURRENCY", "USD")
        primary_exchange = (os.getenv("IBKR_PRIMARY_EXCHANGE") or "").strip()
        if primary_exchange:
            contract.primaryExchange = primary_exchange
        return contract

    def _fetch_historical_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        try:
            from ibapi.client import EClient
            from ibapi.wrapper import EWrapper
        except ImportError as exc:
            raise DataFetchError("ibapi is not installed; run `pip install -r requirements.txt`") from exc

        class HistoricalApp(EWrapper, EClient):
            def __init__(self):
                EWrapper.__init__(self)
                EClient.__init__(self, self)
                self.ready = threading.Event()
                self.done = threading.Event()
                self.bars = []
                self.errors = []

            def nextValidId(self, orderId):  # noqa: N802 - ibapi callback name
                self.ready.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802
                self.errors.append((reqId, errorCode, errorString))
                if reqId >= 0 and errorCode not in _IBKR_INFO_CODES:
                    self.done.set()

            def historicalData(self, reqId, bar):  # noqa: N802
                day = _ibkr_bar_date(bar.date)
                close = safe_float(bar.close)
                volume = _ibkr_safe_int_100_share_lots(bar.volume)
                if day and close is not None:
                    self.bars.append(
                        {
                            "date": day,
                            "open": safe_float(bar.open),
                            "high": safe_float(bar.high),
                            "low": safe_float(bar.low),
                            "close": close,
                            "volume": volume,
                            "amount": volume * close if volume is not None else None,
                        }
                    )

            def historicalDataEnd(self, reqId, start, end):  # noqa: N802
                self.done.set()

        req_id = 9001
        app = HistoricalApp()
        thread = self._connect_app(app, client_id=self._client_id())
        try:
            end_datetime = datetime.strptime(end_date, "%Y-%m-%d").strftime("%Y%m%d 23:59:59 US/Eastern")
            app.reqHistoricalData(
                req_id,
                self._contract(symbol),
                end_datetime,
                _ibkr_duration(start_date, end_date),
                "1 day",
                "TRADES",
                1,
                1,
                False,
                [],
            )
            if not app.done.wait(self._timeout()):
                try:
                    app.cancelHistoricalData(req_id)
                except Exception:
                    pass
                raise DataFetchError(f"IBKR historical data timed out for {symbol}")
            if not app.bars:
                raise DataFetchError(self._ibkr_error_summary(app.errors) or f"IBKR returned no bars for {symbol}")
            rows = [
                row
                for row in app.bars
                if start_date <= str(row.get("date", "")) <= end_date
            ]
            return _standardize_daily_rows(rows, symbol)
        finally:
            self._disconnect_app(app, thread)

    def get_intraday_bars_until(
        self,
        stock_code: str,
        cutoff_at: datetime,
        *,
        bar_size: str = "1 min",
        duration: str = "1 D",
    ) -> list[Dict[str, Any]]:
        """Return IBKR intraday bars for ``cutoff_at.date()`` capped at cutoff.

        ``cutoff_at`` is interpreted as US/Eastern when it is timezone-naive.
        The method is read-only and uses ``reqHistoricalData``.
        """

        symbol = self._ensure_symbol(stock_code)
        if not self._is_available():
            raise DataFetchError(self._unavailable_error())
        if cutoff_at.tzinfo is not None:
            cutoff_local = cutoff_at.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
        else:
            cutoff_local = cutoff_at
        try:
            rows = self._fetch_intraday_bars(symbol, cutoff_local, bar_size=bar_size, duration=duration)
            self._clear_failure_cooldown()
            return rows
        except DataFetchError as exc:
            self._mark_failure_cooldown(str(exc))
            raise

    def _fetch_intraday_bars(
        self,
        symbol: str,
        cutoff_at: datetime,
        *,
        bar_size: str,
        duration: str,
    ) -> list[Dict[str, Any]]:
        try:
            from ibapi.client import EClient
            from ibapi.wrapper import EWrapper
        except ImportError as exc:
            raise DataFetchError("ibapi is not installed; run `pip install -r requirements.txt`") from exc

        class IntradayApp(EWrapper, EClient):
            def __init__(self):
                EWrapper.__init__(self)
                EClient.__init__(self, self)
                self.ready = threading.Event()
                self.done = threading.Event()
                self.bars = []
                self.errors = []

            def nextValidId(self, orderId):  # noqa: N802 - ibapi callback name
                self.ready.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802
                self.errors.append((reqId, errorCode, errorString))
                if reqId >= 0 and errorCode not in _IBKR_INFO_CODES:
                    self.done.set()

            def historicalData(self, reqId, bar):  # noqa: N802
                bar_dt = _ibkr_bar_datetime(bar.date)
                close = safe_float(bar.close)
                volume = _ibkr_safe_int_100_share_lots(bar.volume)
                if bar_dt is not None and close is not None:
                    self.bars.append(
                        {
                            "timestamp": bar_dt.isoformat(sep=" "),
                            "date": bar_dt.date().isoformat(),
                            "time": bar_dt.time().isoformat(),
                            "open": safe_float(bar.open),
                            "high": safe_float(bar.high),
                            "low": safe_float(bar.low),
                            "close": close,
                            "volume": volume,
                            "amount": volume * close if volume is not None else None,
                        }
                    )

            def historicalDataEnd(self, reqId, start, end):  # noqa: N802
                self.done.set()

        req_id = 9003
        app = IntradayApp()
        thread = self._connect_app(app, client_id=self._client_id(offset=2))
        try:
            use_rth = 1 if _env_bool("IBKR_INTRADAY_USE_RTH", False) else 0
            end_datetime = cutoff_at.strftime("%Y%m%d %H:%M:%S US/Eastern")
            app.reqHistoricalData(
                req_id,
                self._contract(symbol),
                end_datetime,
                duration,
                bar_size,
                "TRADES",
                use_rth,
                2,
                False,
                [],
            )
            if not app.done.wait(self._timeout()):
                try:
                    app.cancelHistoricalData(req_id)
                except Exception:
                    pass
                error_text = self._ibkr_error_summary(app.errors)
                if error_text:
                    raise DataFetchError(f"IBKR intraday data timed out for {symbol}: {error_text}")
                raise DataFetchError(f"IBKR intraday data timed out for {symbol}")
            if not app.bars:
                raise DataFetchError(self._ibkr_error_summary(app.errors) or f"IBKR returned no intraday bars for {symbol}")

            cutoff_text = cutoff_at.isoformat(sep=" ")
            trade_date_text = cutoff_at.date().isoformat()
            rows = [
                row
                for row in app.bars
                if row.get("date") == trade_date_text and str(row.get("timestamp") or "") <= cutoff_text
            ]
            rows.sort(key=lambda item: str(item.get("timestamp") or ""))
            if not rows:
                raise DataFetchError(f"IBKR returned no intraday bars at or before cutoff for {symbol}")
            return rows
        finally:
            self._disconnect_app(app, thread)

    def _fetch_snapshot_quote(self, symbol: str) -> Optional[UnifiedRealtimeQuote]:
        try:
            from ibapi.client import EClient
            from ibapi.wrapper import EWrapper
        except ImportError as exc:
            raise DataFetchError("ibapi is not installed; run `pip install -r requirements.txt`") from exc

        class SnapshotApp(EWrapper, EClient):
            def __init__(self):
                EWrapper.__init__(self)
                EClient.__init__(self, self)
                self.ready = threading.Event()
                self.done = threading.Event()
                self.prices = {}
                self.sizes = {}
                self.errors = []

            def nextValidId(self, orderId):  # noqa: N802 - ibapi callback name
                self.ready.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802
                self.errors.append((reqId, errorCode, errorString))
                if reqId >= 0 and errorCode not in _IBKR_INFO_CODES:
                    self.done.set()

            def tickPrice(self, reqId, tickType, price, attrib):  # noqa: N802
                if price and price > 0:
                    self.prices[int(tickType)] = float(price)

            def tickSize(self, reqId, tickType, size):  # noqa: N802
                if size is not None and size >= 0:
                    self.sizes[int(tickType)] = int(size)

            def tickSnapshotEnd(self, reqId):  # noqa: N802
                self.done.set()

        req_id = 9002
        app = SnapshotApp()
        thread = self._connect_app(app, client_id=self._client_id(offset=1))
        try:
            app.reqMarketDataType(self._market_data_type())
            app.reqMktData(req_id, self._contract(symbol), "", True, False, [])
            app.done.wait(min(self._timeout(), 10.0))
            price = self._first_tick_price(app.prices, (4, 68, 1, 66, 2, 67, 9, 75))
            if price is None or price <= 0:
                return None
            prev_close = self._first_tick_price(app.prices, (9, 75))
            change_amount, change_pct = _pct_change(price, prev_close)
            high = self._first_tick_price(app.prices, (6, 72))
            low = self._first_tick_price(app.prices, (7, 73))
            volume = self._first_tick_size(app.sizes, (8, 74))
            volume = volume * 100 if volume is not None else None
            return UnifiedRealtimeQuote(
                code=symbol,
                source=self.source,
                price=price,
                change_pct=change_pct,
                change_amount=change_amount,
                volume=volume,
                amount=volume * price if volume is not None else None,
                open_price=self._first_tick_price(app.prices, (14, 76)),
                high=high,
                low=low,
                pre_close=prev_close,
                amplitude=_amplitude(high, low, prev_close),
            )
        finally:
            self._disconnect_app(app, thread)

    def _connect_app(self, app: Any, *, client_id: int) -> threading.Thread:
        try:
            app.connect(self._host(), self._port(), clientId=client_id)
        except Exception as exc:
            raise DataFetchError(f"IBKR connect failed at {self._host()}:{self._port()}: {exc}") from exc
        thread = threading.Thread(target=app.run, name=f"ibkr-client-{client_id}", daemon=True)
        thread.start()
        if not app.ready.wait(self._timeout()):
            self._disconnect_app(app, thread)
            raise DataFetchError("IBKR API did not become ready before timeout")
        return thread

    def _disconnect_app(self, app: Any, thread: Optional[threading.Thread]) -> None:
        try:
            app.disconnect()
        except Exception:
            pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    @staticmethod
    def _first_tick_price(values: Dict[int, float], keys: tuple[int, ...]) -> Optional[float]:
        for key in keys:
            price = safe_float(values.get(key))
            if price is not None and price > 0:
                return price
        return None

    @staticmethod
    def _first_tick_size(values: Dict[int, int], keys: tuple[int, ...]) -> Optional[int]:
        for key in keys:
            size = safe_int(values.get(key))
            if size is not None and size >= 0:
                return size
        return None

    @staticmethod
    def _ibkr_error_summary(errors: list[tuple[int, int, str]]) -> Optional[str]:
        for req_id, code, message in errors:
            if req_id >= 0 and code not in _IBKR_INFO_CODES:
                return f"IBKR error {code}: {message}"
        return None


class AlphaVantageFetcher(_USKeyedFetcher):
    """Alpha Vantage US daily and quote fallback."""

    name = "AlphaVantageFetcher"
    priority = int(os.getenv("ALPHA_VANTAGE_PRIORITY", "6"))
    api_key_env = "ALPHA_VANTAGE_API_KEY"
    source = RealtimeSource.ALPHA_VANTAGE
    base_url = "https://www.alphavantage.co/query"

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        symbol = self._ensure_symbol(stock_code)
        api_key = self._api_key()
        if not api_key:
            raise DataFetchError("ALPHA_VANTAGE_API_KEY is not configured")
        data = _get_json(
            f"{self.base_url}?{urlencode({'function': 'TIME_SERIES_DAILY_ADJUSTED', 'symbol': symbol, 'outputsize': 'full', 'apikey': api_key})}"
        )
        series = data.get("Time Series (Daily)")
        if not isinstance(series, dict):
            raise DataFetchError(str(data.get("Note") or data.get("Information") or data.get("Error Message") or "no daily data"))
        rows = []
        for day, values in series.items():
            if day < start_date or day > end_date:
                continue
            rows.append(
                {
                    "date": day,
                    "open": safe_float(values.get("1. open")),
                    "high": safe_float(values.get("2. high")),
                    "low": safe_float(values.get("3. low")),
                    "close": safe_float(values.get("5. adjusted close") or values.get("4. close")),
                    "volume": safe_int(values.get("6. volume"), 0),
                }
            )
        rows.sort(key=lambda item: str(item["date"]))
        return _standardize_daily_rows(rows, symbol)

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        try:
            symbol = self._ensure_symbol(stock_code)
            api_key = self._api_key()
            if not api_key:
                return None
            data = _get_json(
                f"{self.base_url}?{urlencode({'function': 'GLOBAL_QUOTE', 'symbol': symbol, 'apikey': api_key})}",
                timeout=10,
            )
            quote = data.get("Global Quote") or {}
            price = safe_float(quote.get("05. price"))
            prev_close = safe_float(quote.get("08. previous close"))
            if price is None:
                return None
            change_amount = safe_float(quote.get("09. change"))
            change_pct_text = str(quote.get("10. change percent") or "").replace("%", "")
            change_pct = safe_float(change_pct_text)
            if change_amount is None or change_pct is None:
                change_amount, change_pct = _pct_change(price, prev_close)
            high = safe_float(quote.get("03. high"))
            low = safe_float(quote.get("04. low"))
            return UnifiedRealtimeQuote(
                code=symbol,
                source=self.source,
                price=price,
                change_pct=change_pct,
                change_amount=change_amount,
                volume=safe_int(quote.get("06. volume")),
                open_price=safe_float(quote.get("02. open")),
                high=high,
                low=low,
                pre_close=prev_close,
                amplitude=_amplitude(high, low, prev_close),
            )
        except Exception as exc:
            logger.debug("[AlphaVantage] realtime failed for %s: %s", stock_code, exc)
            return None

    def get_fundamental_snapshot(self, stock_code: str) -> Dict[str, Any]:
        """Return latest Alpha Vantage company overview as DSA fundamental blocks."""
        symbol = self._ensure_symbol(stock_code)
        api_key = self._api_key()
        if not api_key:
            raise DataFetchError("ALPHA_VANTAGE_API_KEY is not configured")

        data = _get_json(
            f"{self.base_url}?{urlencode({'function': 'OVERVIEW', 'symbol': symbol, 'apikey': api_key})}",
            timeout=10,
        )
        api_error = data.get("Note") or data.get("Information") or data.get("Error Message")
        if api_error:
            raise DataFetchError(_redact_sensitive_text(api_error))
        if not _payload_has_data(data) or _clean_text(data.get("Symbol")) is None:
            raise DataFetchError("Alpha Vantage returned no company overview")

        market_cap = safe_float(data.get("MarketCapitalization"))
        valuation = _compact_payload(
            {
                "pe_ratio": safe_float(data.get("PERatio")),
                "pb_ratio": safe_float(data.get("PriceToBookRatio")),
                "peg_ratio": safe_float(data.get("PEGRatio")),
                "ps_ratio_ttm": safe_float(data.get("PriceToSalesRatioTTM")),
                "ev_to_revenue": safe_float(data.get("EVToRevenue")),
                "ev_to_ebitda": safe_float(data.get("EVToEBITDA")),
                "market_cap": market_cap,
                "total_mv": market_cap,
                "market_cap_currency": _clean_text(data.get("Currency")) or "USD",
                "beta": safe_float(data.get("Beta")),
            }
        )
        growth = _compact_payload(
            {
                "revenue_ttm": safe_float(data.get("RevenueTTM")),
                "gross_profit_ttm": safe_float(data.get("GrossProfitTTM")),
                "ebitda": safe_float(data.get("EBITDA")),
                "quarterly_revenue_growth_yoy": safe_float(data.get("QuarterlyRevenueGrowthYOY")),
                "quarterly_earnings_growth_yoy": safe_float(data.get("QuarterlyEarningsGrowthYOY")),
                "profit_margin": safe_float(data.get("ProfitMargin")),
                "operating_margin_ttm": safe_float(data.get("OperatingMarginTTM")),
                "return_on_equity_ttm": safe_float(data.get("ReturnOnEquityTTM")),
                "return_on_assets_ttm": safe_float(data.get("ReturnOnAssetsTTM")),
            }
        )
        earnings = _compact_payload(
            {
                "eps": safe_float(data.get("EPS")),
                "diluted_eps_ttm": safe_float(data.get("DilutedEPSTTM")),
                "trailing_pe": safe_float(data.get("TrailingPE")),
                "forward_pe": safe_float(data.get("ForwardPE")),
                "analyst_target_price": safe_float(data.get("AnalystTargetPrice")),
                "dividend_per_share": safe_float(data.get("DividendPerShare")),
                "dividend_yield": safe_float(data.get("DividendYield")),
                "dividend_date": _clean_text(data.get("DividendDate")),
                "ex_dividend_date": _clean_text(data.get("ExDividendDate")),
                "latest_quarter": _clean_text(data.get("LatestQuarter")),
            }
        )
        boards = _compact_payload(
            {
                "name": _clean_text(data.get("Name")),
                "symbol": _clean_text(data.get("Symbol")) or symbol,
                "asset_type": _clean_text(data.get("AssetType")),
                "exchange": _clean_text(data.get("Exchange")),
                "currency": _clean_text(data.get("Currency")),
                "country": _clean_text(data.get("Country")),
                "sector": _clean_text(data.get("Sector")),
                "industry": _clean_text(data.get("Industry")),
                "fiscal_year_end": _clean_text(data.get("FiscalYearEnd")),
                "cik": _clean_text(data.get("CIK")),
                "business_description": _clean_text(data.get("Description"), max_len=600),
            }
        )
        status = _snapshot_status(valuation, growth, earnings, boards)
        return {
            "status": status,
            "source_chain": [{"provider": "alpha_vantage_overview", "result": status, "duration_ms": 0}],
            "valuation": valuation,
            "growth": growth,
            "earnings": earnings,
            "institution": {},
            "boards": boards,
            "errors": [],
            "snapshot_semantics": "latest_provider_snapshot_not_point_in_time",
        }


class FinnhubFetcher(_USKeyedFetcher):
    """Finnhub US daily and quote fallback."""

    name = "FinnhubFetcher"
    priority = int(os.getenv("FINNHUB_PRIORITY", "7"))
    api_key_env = "FINNHUB_API_KEY"
    source = RealtimeSource.FINNHUB
    base_url = "https://finnhub.io/api/v1"

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        symbol = self._ensure_symbol(stock_code)
        api_key = self._api_key()
        if not api_key:
            raise DataFetchError("FINNHUB_API_KEY is not configured")
        start_ts = int(datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc).timestamp())
        end_ts = int(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp())
        params = urlencode({"symbol": symbol, "resolution": "D", "from": start_ts, "to": end_ts, "token": api_key})
        data = _get_json(f"{self.base_url}/stock/candle?{params}")
        if data.get("s") != "ok":
            raise DataFetchError(str(data.get("s") or "no daily data"))
        rows = []
        for idx, ts in enumerate(data.get("t") or []):
            day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
            rows.append(
                {
                    "date": day,
                    "open": safe_float(_list_at(data.get("o"), idx)),
                    "high": safe_float(_list_at(data.get("h"), idx)),
                    "low": safe_float(_list_at(data.get("l"), idx)),
                    "close": safe_float(_list_at(data.get("c"), idx)),
                    "volume": safe_int(_list_at(data.get("v"), idx), 0),
                }
            )
        return _standardize_daily_rows(rows, symbol)

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        try:
            symbol = self._ensure_symbol(stock_code)
            api_key = self._api_key()
            if not api_key:
                return None
            data = _get_json(f"{self.base_url}/quote?{urlencode({'symbol': symbol, 'token': api_key})}", timeout=10)
            price = safe_float(data.get("c"))
            prev_close = safe_float(data.get("pc"))
            if price is None or price <= 0:
                return None
            change_amount = safe_float(data.get("d"))
            change_pct = safe_float(data.get("dp"))
            if change_amount is None or change_pct is None:
                change_amount, change_pct = _pct_change(price, prev_close)
            high = safe_float(data.get("h"))
            low = safe_float(data.get("l"))
            name = ""
            try:
                profile = _get_json(
                    f"{self.base_url}/stock/profile2?{urlencode({'symbol': symbol, 'token': api_key})}",
                    timeout=8,
                )
                name = str(profile.get("name") or "").strip()
            except Exception:
                pass
            return UnifiedRealtimeQuote(
                code=symbol,
                name=name,
                source=self.source,
                price=price,
                change_pct=change_pct,
                change_amount=change_amount,
                open_price=safe_float(data.get("o")),
                high=high,
                low=low,
                pre_close=prev_close,
                amplitude=_amplitude(high, low, prev_close),
            )
        except Exception as exc:
            logger.debug("[Finnhub] realtime failed for %s: %s", stock_code, exc)
            return None

    def get_fundamental_snapshot(self, stock_code: str) -> Dict[str, Any]:
        """Return latest Finnhub profile and basic financial metrics as DSA blocks."""
        symbol = self._ensure_symbol(stock_code)
        api_key = self._api_key()
        if not api_key:
            raise DataFetchError("FINNHUB_API_KEY is not configured")

        errors = []
        profile: Dict[str, Any] = {}
        metrics_root: Dict[str, Any] = {}

        try:
            profile = _get_json(
                f"{self.base_url}/stock/profile2?{urlencode({'symbol': symbol, 'token': api_key})}",
                timeout=10,
            )
        except Exception as exc:
            errors.append(f"profile2: {_redact_sensitive_text(exc)}")

        try:
            metrics_root = _get_json(
                f"{self.base_url}/stock/metric?{urlencode({'symbol': symbol, 'metric': 'all', 'token': api_key})}",
                timeout=10,
            )
        except Exception as exc:
            errors.append(f"metric: {_redact_sensitive_text(exc)}")

        metric = metrics_root.get("metric") if isinstance(metrics_root, dict) else {}
        if not isinstance(metric, dict):
            metric = {}
        if not _payload_has_data(profile) and not _payload_has_data(metric):
            raise DataFetchError("; ".join(errors) or "Finnhub returned no profile or metric data")

        market_cap_million = safe_float(profile.get("marketCapitalization") or metric.get("marketCapitalization"))
        total_mv = market_cap_million * 1_000_000 if market_cap_million is not None else None
        valuation = _compact_payload(
            {
                "pe_ratio": _first_float(metric, "peBasicExclExtraTTM", "peExclExtraTTM", "peNormalizedAnnual"),
                "pb_ratio": _first_float(metric, "pbQuarterly", "pbAnnual"),
                "ps_ratio_ttm": _first_float(metric, "psTTM", "psAnnual"),
                "pcf_ratio_ttm": safe_float(metric.get("pcfShareTTM")),
                "market_cap_million": market_cap_million,
                "total_mv": total_mv,
                "market_cap_currency": _clean_text(profile.get("currency")) or "USD",
                "beta": safe_float(metric.get("beta")),
                "dividend_yield_indicated_annual": safe_float(metric.get("dividendYieldIndicatedAnnual")),
            }
        )
        growth = _compact_payload(
            {
                "revenue_growth_ttm_yoy": safe_float(metric.get("revenueGrowthTTMYoy")),
                "revenue_growth_quarterly_yoy": safe_float(metric.get("revenueGrowthQuarterlyYoy")),
                "revenue_growth_3y": safe_float(metric.get("revenueGrowth3Y")),
                "revenue_growth_5y": safe_float(metric.get("revenueGrowth5Y")),
                "eps_growth_ttm_yoy": safe_float(metric.get("epsGrowthTTMYoy")),
                "eps_growth_quarterly_yoy": safe_float(metric.get("epsGrowthQuarterlyYoy")),
                "eps_growth_3y": safe_float(metric.get("epsGrowth3Y")),
                "eps_growth_5y": safe_float(metric.get("epsGrowth5Y")),
                "gross_margin_ttm": safe_float(metric.get("grossMarginTTM")),
                "operating_margin_ttm": safe_float(metric.get("operatingMarginTTM")),
                "net_profit_margin_ttm": safe_float(metric.get("netProfitMarginTTM")),
                "return_on_equity": safe_float(metric.get("roeRfy")),
                "return_on_assets": safe_float(metric.get("roaRfy")),
                "return_on_investment_annual": safe_float(metric.get("roiAnnual")),
            }
        )
        earnings = _compact_payload(
            {
                "eps": _first_float(metric, "epsBasicExclExtraItemsTTM", "epsExclExtraItemsTTM", "epsAnnual"),
                "eps_basic_excl_extra_items_ttm": safe_float(metric.get("epsBasicExclExtraItemsTTM")),
                "eps_incl_extra_items_ttm": safe_float(metric.get("epsInclExtraItemsTTM")),
                "dividend_per_share_annual": safe_float(metric.get("dividendPerShareAnnual")),
                "current_dividend_yield_ttm": safe_float(metric.get("currentDividendYieldTTM")),
                "payout_ratio_ttm": safe_float(metric.get("payoutRatioTTM")),
                "book_value_per_share_quarterly": safe_float(metric.get("bookValuePerShareQuarterly")),
                "tangible_book_value_per_share_quarterly": safe_float(metric.get("tangibleBookValuePerShareQuarterly")),
            }
        )
        boards = _compact_payload(
            {
                "name": _clean_text(profile.get("name")),
                "symbol": _clean_text(profile.get("ticker")) or symbol,
                "exchange": _clean_text(profile.get("exchange")),
                "currency": _clean_text(profile.get("currency")),
                "country": _clean_text(profile.get("country")),
                "industry": _clean_text(profile.get("finnhubIndustry")),
                "ipo_date": _clean_text(profile.get("ipo")),
                "web_url": _clean_text(profile.get("weburl")),
                "logo": _clean_text(profile.get("logo")),
                "shares_outstanding_million": safe_float(profile.get("shareOutstanding")),
            }
        )
        status = _snapshot_status(valuation, growth, earnings, boards)
        return {
            "status": "partial" if status == "ok" and errors else status,
            "source_chain": [{"provider": "finnhub_profile_metric", "result": status, "duration_ms": 0}],
            "valuation": valuation,
            "growth": growth,
            "earnings": earnings,
            "institution": {},
            "boards": boards,
            "errors": errors,
            "snapshot_semantics": "latest_provider_snapshot_not_point_in_time",
        }


class TwelveDataFetcher(_USKeyedFetcher):
    """Twelve Data US daily and quote fallback."""

    name = "TwelveDataFetcher"
    priority = int(os.getenv("TWELVEDATA_PRIORITY", "8"))
    api_key_env = "TWELVEDATA_API_KEY"
    source = RealtimeSource.TWELVE_DATA
    base_url = "https://api.twelvedata.com"

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        symbol = self._ensure_symbol(stock_code)
        api_key = self._api_key()
        if not api_key:
            raise DataFetchError("TWELVEDATA_API_KEY is not configured")
        params = urlencode(
            {
                "symbol": symbol,
                "interval": "1day",
                "start_date": start_date,
                "end_date": end_date,
                "outputsize": 5000,
                "apikey": api_key,
            }
        )
        data = _get_json(f"{self.base_url}/time_series?{params}")
        values = data.get("values")
        if not isinstance(values, list):
            raise DataFetchError(str(data.get("message") or data.get("status") or "no daily data"))
        rows = []
        for item in values:
            rows.append(
                {
                    "date": item.get("datetime"),
                    "open": safe_float(item.get("open")),
                    "high": safe_float(item.get("high")),
                    "low": safe_float(item.get("low")),
                    "close": safe_float(item.get("close")),
                    "volume": safe_int(item.get("volume"), 0),
                }
            )
        rows.sort(key=lambda item: str(item["date"]))
        return _standardize_daily_rows(rows, symbol)

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        try:
            symbol = self._ensure_symbol(stock_code)
            api_key = self._api_key()
            if not api_key:
                return None
            data = _get_json(f"{self.base_url}/quote?{urlencode({'symbol': symbol, 'apikey': api_key})}", timeout=10)
            price = safe_float(data.get("close") or data.get("price"))
            prev_close = safe_float(data.get("previous_close"))
            if price is None or price <= 0:
                return None
            change_amount = safe_float(data.get("change"))
            change_pct = safe_float(data.get("percent_change"))
            if change_amount is None or change_pct is None:
                change_amount, change_pct = _pct_change(price, prev_close)
            high = safe_float(data.get("high"))
            low = safe_float(data.get("low"))
            return UnifiedRealtimeQuote(
                code=symbol,
                name=str(data.get("name") or "").strip(),
                source=self.source,
                price=price,
                change_pct=change_pct,
                change_amount=change_amount,
                volume=safe_int(data.get("volume")),
                open_price=safe_float(data.get("open")),
                high=high,
                low=low,
                pre_close=prev_close,
                amplitude=_amplitude(high, low, prev_close),
            )
        except Exception as exc:
            logger.debug("[TwelveData] realtime failed for %s: %s", stock_code, exc)
            return None

    def get_intraday_bars_until(
        self,
        stock_code: str,
        cutoff_at: datetime,
        *,
        bar_size: str = "1 min",
        duration: str = "1 D",
    ) -> list[Dict[str, Any]]:
        symbol, cutoff_local = _validate_intraday_request(
            self._ensure_symbol(stock_code),
            cutoff_at,
            bar_size=bar_size,
        )
        api_key = self._api_key()
        if not api_key:
            raise DataFetchError("TWELVEDATA_API_KEY is not configured")

        cached_rows = _read_intraday_cache("twelvedata", symbol, cutoff_local)
        if cached_rows is not None:
            return cached_rows

        outside_regular = not _regular_hours_contains(cutoff_local)
        request_start = cutoff_local.replace(
            hour=4 if outside_regular else 9,
            minute=0 if outside_regular else 30,
            second=0,
            microsecond=0,
        )
        request_end = cutoff_local.replace(hour=20 if outside_regular else 16, minute=0, second=0, microsecond=0)
        if outside_regular:
            request_end = min(request_end, cutoff_local.replace(hour=20, minute=0, second=0, microsecond=0))

        params = {
            "symbol": symbol,
            "interval": "1min",
            "start_date": request_start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": request_end.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "America/New_York",
            "outputsize": 5000,
            "apikey": api_key,
        }
        if outside_regular:
            params["prepost"] = "true"

        data = _get_intraday_json(f"{self.base_url}/time_series?{urlencode(params)}", timeout=20)
        values = data.get("values")
        if not isinstance(values, list):
            raise DataFetchError(str(data.get("message") or data.get("code") or data.get("status") or "no intraday data"))

        rows_all: list[Dict[str, Any]] = []
        trade_date_text = cutoff_local.date().isoformat()
        for item in values:
            if not isinstance(item, dict):
                continue
            bar_dt = _parse_provider_intraday_datetime(item.get("datetime"))
            if bar_dt is None:
                continue
            row = _provider_intraday_row(bar_dt, item)
            if row is not None and row.get("date") == trade_date_text:
                rows_all.append(row)
        rows_all.sort(key=lambda item: str(item.get("timestamp") or ""))
        _write_intraday_cache("twelvedata", symbol, trade_date_text, rows_all)
        rows = _filter_intraday_rows_until(rows_all, cutoff_local)
        if not rows:
            raise DataFetchError(f"Twelve Data returned no intraday bars at or before cutoff for {symbol}")
        return rows


class MassiveFetcher(_USKeyedFetcher):
    """Massive/Polygon-compatible US aggregate and snapshot fallback."""

    name = "MassiveFetcher"
    priority = int(os.getenv("MASSIVE_PRIORITY", "9"))
    source = RealtimeSource.MASSIVE
    api_key_env = "MASSIVE_API_KEY"

    def _api_key(self) -> Optional[str]:
        return (os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY") or "").strip() or None

    def _base_url(self) -> str:
        return (os.getenv("MASSIVE_BASE_URL") or "https://api.massive.com").rstrip("/")

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        symbol = self._ensure_symbol(stock_code)
        api_key = self._api_key()
        if not api_key:
            raise DataFetchError("MASSIVE_API_KEY or POLYGON_API_KEY is not configured")
        params = urlencode({"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key})
        url = f"{self._base_url()}/v2/aggs/ticker/{symbol}/range/1/day/{start_date}/{end_date}?{params}"
        data = _get_json(url)
        results = data.get("results")
        if not isinstance(results, list):
            raise DataFetchError(str(data.get("error") or data.get("message") or data.get("status") or "no daily data"))
        rows = []
        for item in results:
            ts = item.get("t")
            day = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).date().isoformat() if ts else None
            rows.append(
                {
                    "date": day,
                    "open": safe_float(item.get("o")),
                    "high": safe_float(item.get("h")),
                    "low": safe_float(item.get("l")),
                    "close": safe_float(item.get("c")),
                    "volume": safe_int(item.get("v"), 0),
                }
            )
        return _standardize_daily_rows(rows, symbol)

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        try:
            symbol = self._ensure_symbol(stock_code)
            api_key = self._api_key()
            if not api_key:
                return None
            data = _get_json(
                f"{self._base_url()}/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}?{urlencode({'apiKey': api_key})}",
                timeout=10,
            )
            ticker = data.get("ticker") or {}
            day = ticker.get("day") or {}
            prev = ticker.get("prevDay") or {}
            last_trade = ticker.get("lastTrade") or {}
            price = safe_float(last_trade.get("p") or day.get("c"))
            prev_close = safe_float(prev.get("c"))
            if price is None or price <= 0:
                return None
            change_amount, change_pct = _pct_change(price, prev_close)
            high = safe_float(day.get("h"))
            low = safe_float(day.get("l"))
            return UnifiedRealtimeQuote(
                code=symbol,
                source=self.source,
                price=price,
                change_pct=change_pct,
                change_amount=change_amount,
                volume=safe_int(day.get("v")),
                amount=(
                    safe_float(day.get("v")) * price
                    if safe_float(day.get("v")) is not None
                    else None
                ),
                open_price=safe_float(day.get("o")),
                high=high,
                low=low,
                pre_close=prev_close,
                amplitude=_amplitude(high, low, prev_close),
            )
        except Exception as exc:
            logger.debug("[Massive] realtime failed for %s: %s", stock_code, exc)
            return None

    def get_intraday_bars_until(
        self,
        stock_code: str,
        cutoff_at: datetime,
        *,
        bar_size: str = "1 min",
        duration: str = "1 D",
    ) -> list[Dict[str, Any]]:
        symbol, cutoff_local = _validate_intraday_request(
            self._ensure_symbol(stock_code),
            cutoff_at,
            bar_size=bar_size,
        )
        api_key = self._api_key()
        if not api_key:
            raise DataFetchError("MASSIVE_API_KEY or POLYGON_API_KEY is not configured")

        trade_date_text = cutoff_local.date().isoformat()
        cached_rows = _read_intraday_cache("massive", symbol, cutoff_local)
        if cached_rows is not None:
            return cached_rows

        params = urlencode({"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key})
        data = _get_intraday_json(
            f"{self._base_url()}/v2/aggs/ticker/{symbol}/range/1/minute/{trade_date_text}/{trade_date_text}?{params}",
            timeout=20,
        )
        results = data.get("results")
        if not isinstance(results, list):
            raise DataFetchError(str(data.get("error") or data.get("message") or data.get("status") or "no intraday data"))

        rows_all: list[Dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            timestamp_ms = item.get("t")
            if timestamp_ms is None:
                continue
            try:
                bar_dt = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc).astimezone(
                    ZoneInfo("America/New_York")
                ).replace(tzinfo=None)
            except (OSError, TypeError, ValueError):
                continue
            row = _provider_intraday_row(bar_dt, item)
            if row is not None and row.get("date") == trade_date_text:
                rows_all.append(row)
        rows_all.sort(key=lambda item: str(item.get("timestamp") or ""))
        _write_intraday_cache("massive", symbol, trade_date_text, rows_all)
        rows = _filter_intraday_rows_until(rows_all, cutoff_local)
        if not rows:
            raise DataFetchError(f"Massive returned no intraday bars at or before cutoff for {symbol}")
        return rows
