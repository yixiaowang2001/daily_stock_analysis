# -*- coding: utf-8 -*-
"""Fetch pre-cutoff intraday evidence for tail-session candidate scoring.

The service is deliberately fail-open: external minute feeds are useful evidence
but must not block experiment compose or replace the Agent's judgement.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd


MinuteProvider = Callable[[str, date, str], Tuple[Optional[pd.DataFrame], str]]


def fetch_tail_intraday_cutoff_evidence(
    *,
    symbol: str,
    trade_date: date,
    selection_cutoff: str = "14:40",
    previous_close: Optional[float] = None,
    providers: Optional[List[MinuteProvider]] = None,
) -> Dict[str, Any]:
    """Return minute-level evidence capped at ``selection_cutoff``.

    Provider order:
    1. AkShare Eastmoney historical minute K (strict date range, 1m).
    2. AkShare Sina minute K (recent intraday/minute cache, 1m).
    3. efinance Eastmoney quote history (1m).

    Every failed attempt is recorded in ``fallback_attempts``.
    """

    code = _normalize_symbol(symbol)
    as_of_dt = _cutoff_datetime(trade_date, selection_cutoff)
    attempts: List[Dict[str, Any]] = []

    if not (code.isdigit() and len(code) == 6):
        return _unavailable(
            code=code or symbol,
            as_of_dt=as_of_dt,
            attempts=attempts,
            reason="unsupported_symbol",
        )

    funcs = providers or [
        _fetch_akshare_hist_min_em,
        _fetch_akshare_minute,
        _fetch_efinance_quote_history,
    ]
    for provider in funcs:
        provider_name = _provider_name(provider)
        try:
            raw_df, source = provider(code, trade_date, selection_cutoff)
        except Exception as exc:
            attempts.append(
                {
                    "source": provider_name,
                    "status": "failed",
                    "reason": _short_reason(exc),
                }
            )
            continue

        normalized = _normalize_minute_frame(raw_df, trade_date=trade_date, cutoff=as_of_dt)
        if normalized.empty:
            attempts.append(
                {
                    "source": source or provider_name,
                    "status": "empty",
                    "reason": "no_minute_rows_at_or_before_cutoff",
                }
            )
            continue

        attempts.append({"source": source or provider_name, "status": "ok"})
        return _available_payload(
            code=code,
            as_of_dt=as_of_dt,
            source=source or provider_name,
            minute_df=normalized,
            previous_close=previous_close,
            attempts=attempts,
        )

    return _unavailable(
        code=code,
        as_of_dt=as_of_dt,
        attempts=attempts,
        reason="intraday_pre_cutoff_fetch_failed",
    )


def _fetch_akshare_hist_min_em(
    code: str,
    trade_date: date,
    selection_cutoff: str,
) -> Tuple[Optional[pd.DataFrame], str]:
    import akshare as ak

    start = f"{trade_date.isoformat()} 09:30:00"
    end = f"{trade_date.isoformat()} {selection_cutoff}:00"
    df = ak.stock_zh_a_hist_min_em(
        symbol=code,
        period="1",
        adjust="",
        start_date=start,
        end_date=end,
    )
    return df, "akshare_hist_min_em_1m"


def _fetch_akshare_minute(
    code: str,
    trade_date: date,
    selection_cutoff: str,
) -> Tuple[Optional[pd.DataFrame], str]:
    import akshare as ak

    prefix = _exchange_prefix(code)
    if not prefix:
        return None, "akshare_minute_1m_unsupported_exchange"
    df = ak.stock_zh_a_minute(symbol=f"{prefix}{code}", period="1", adjust="")
    return df, "akshare_stock_zh_a_minute_1m"


def _fetch_efinance_quote_history(
    code: str,
    trade_date: date,
    selection_cutoff: str,
) -> Tuple[Optional[pd.DataFrame], str]:
    import efinance as ef

    day = trade_date.strftime("%Y%m%d")
    try:
        df = ef.stock.get_quote_history(code, beg=day, end=day, klt=1, fqt=0)
    except TypeError:
        df = ef.stock.get_quote_history(code, beg=day, end=day, klt=1)
    return df, "efinance_quote_history_1m"


def _normalize_minute_frame(
    df: Optional[pd.DataFrame],
    *,
    trade_date: date,
    cutoff: datetime,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    source = df.copy()
    colmap = {
        "dt": _first_column(source, ["时间", "日期", "day", "datetime", "date"]),
        "open": _first_column(source, ["开盘", "open"]),
        "high": _first_column(source, ["最高", "high"]),
        "low": _first_column(source, ["最低", "low"]),
        "close": _first_column(source, ["收盘", "close"]),
        "volume": _first_column(source, ["成交量", "volume", "vol"]),
        "amount": _first_column(source, ["成交额", "amount"]),
    }
    required = ["dt", "open", "high", "low", "close"]
    if any(colmap[k] is None for k in required):
        return pd.DataFrame()

    work = pd.DataFrame()
    raw_dt = source[colmap["dt"]]
    parsed_dt = pd.to_datetime(raw_dt, errors="coerce")
    if parsed_dt.notna().sum() == 0:
        parsed_dt = pd.to_datetime(
            trade_date.isoformat() + " " + raw_dt.astype(str),
            errors="coerce",
        )
    work["dt"] = parsed_dt
    for key in ["open", "high", "low", "close", "volume", "amount"]:
        col = colmap.get(key)
        work[key] = pd.to_numeric(source[col], errors="coerce") if col else None

    start_dt = datetime.combine(trade_date, time(9, 30))
    work = work.dropna(subset=["dt", "open", "high", "low", "close"])
    work = work[
        (work["dt"].dt.date == trade_date)
        & (work["dt"] >= start_dt)
        & (work["dt"] <= cutoff)
    ]
    if work.empty:
        return pd.DataFrame()
    return work.sort_values("dt").reset_index(drop=True)


def _available_payload(
    *,
    code: str,
    as_of_dt: datetime,
    source: str,
    minute_df: pd.DataFrame,
    previous_close: Optional[float],
    attempts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    last = minute_df.iloc[-1]
    window_high = float(minute_df["high"].max())
    window_low = float(minute_df["low"].min())
    last_close = float(last["close"])
    day_range = window_high - window_low

    fields: Dict[str, Any] = {
        "last_bar_time": _dt_iso(last["dt"]),
        "bar_count": int(len(minute_df)),
        "last_open": _round_or_none(last["open"]),
        "last_high": _round_or_none(last["high"]),
        "last_low": _round_or_none(last["low"]),
        "last_close": _round_or_none(last_close),
        "last_volume": _round_or_none(last.get("volume")),
        "last_amount": _round_or_none(last.get("amount")),
        "pre_cutoff_high": _round_or_none(window_high),
        "pre_cutoff_low": _round_or_none(window_low),
        "distance_to_pre_cutoff_high_pct": _round_or_none(
            (window_high - last_close) / window_high * 100.0 if window_high > 0 else None
        ),
        "close_position_in_pre_cutoff_range": _round_or_none(
            (last_close - window_low) / day_range if day_range > 0 else None,
            digits=4,
        ),
        "window_volume": _series_sum_or_none(minute_df.get("volume")),
        "window_amount": _series_sum_or_none(minute_df.get("amount")),
        "window_start": "09:30:00",
        "window_end": as_of_dt.strftime("%H:%M:%S"),
    }
    if previous_close and previous_close > 0:
        fields["change_pct_at_cutoff"] = _round_or_none((last_close / previous_close - 1.0) * 100.0)
        fields["pre_cutoff_high_pct"] = _round_or_none((window_high / previous_close - 1.0) * 100.0)

    if len(minute_df) >= 6:
        recent_5m = minute_df.tail(5)
        anchor = float(minute_df.iloc[-6]["close"])
        if anchor > 0:
            fields["last_5m_return_pct"] = _round_or_none((last_close / anchor - 1.0) * 100.0)
        last_5m_volume = _series_sum_or_none(recent_5m.get("volume"))
        if last_5m_volume is not None:
            fields["last_5m_volume"] = last_5m_volume
            fields["last_5m_avg_volume"] = _round_or_none(last_5m_volume / 5.0)
            window_volume = _series_sum_or_none(minute_df.get("volume"))
            if window_volume and window_volume > 0:
                fields["last_5m_volume_share"] = _round_or_none(last_5m_volume / window_volume, digits=4)
    if len(minute_df) >= 36:
        recent_5m = minute_df.tail(5)
        previous_30m = minute_df.iloc[-35:-5]
        last_5m_avg_volume = _series_sum_or_none(recent_5m.get("volume"))
        previous_30m_avg_volume = _series_sum_or_none(previous_30m.get("volume"))
        if last_5m_avg_volume is not None:
            last_5m_avg_volume = last_5m_avg_volume / 5.0
        if previous_30m_avg_volume is not None:
            previous_30m_avg_volume = previous_30m_avg_volume / 30.0
        fields["previous_30m_avg_volume"] = _round_or_none(previous_30m_avg_volume)
        if last_5m_avg_volume is not None and previous_30m_avg_volume and previous_30m_avg_volume > 0:
            fields["last_5m_vs_previous_30m_volume_ratio"] = _round_or_none(
                last_5m_avg_volume / previous_30m_avg_volume,
                digits=4,
            )

    return {
        "code": code,
        "as_of": as_of_dt.isoformat(sep=" "),
        "available": True,
        "source": source,
        "fields": {k: v for k, v in fields.items() if v is not None},
        "fallback_attempts": attempts,
        "source_scope": "1-minute bars filtered to trade_date and capped at selection_cutoff; post-cutoff bars are excluded.",
        "leakage_guard": "Do not use rows after selection_cutoff or full-day T bar values for score/action_level.",
    }


def _unavailable(
    *,
    code: str,
    as_of_dt: datetime,
    attempts: List[Dict[str, Any]],
    reason: str,
) -> Dict[str, Any]:
    return {
        "code": code,
        "as_of": as_of_dt.isoformat(sep=" "),
        "available": False,
        "source": None,
        "fields": {},
        "missing_reason": reason,
        "fallback_attempts": attempts,
        "allowed_fallback": "Agent may fetch minute data capped at selection_cutoff; full-day T bar is not a valid fallback for scoring.",
    }


def _normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if "." in s:
        s = s.split(".", 1)[0]
    if s.startswith(("SZ", "SH", "BJ")) and len(s) >= 8:
        s = s[2:]
    return s


def _exchange_prefix(code: str) -> Optional[str]:
    if code.startswith(("0", "2", "3")):
        return "sz"
    if code.startswith(("6", "9")):
        return "sh"
    return None


def _cutoff_datetime(trade_date: date, selection_cutoff: str) -> datetime:
    try:
        cutoff = datetime.strptime(str(selection_cutoff)[:5], "%H:%M").time()
    except ValueError:
        cutoff = time(14, 40)
    return datetime.combine(trade_date, cutoff)


def _first_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    lower_map = {str(c).lower(): c for c in df.columns}
    for name in candidates:
        if name in df.columns:
            return name
        col = lower_map.get(name.lower())
        if col is not None:
            return col
    return None


def _provider_name(provider: MinuteProvider) -> str:
    return getattr(provider, "__name__", provider.__class__.__name__)


def _short_reason(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").strip()
    if len(text) > 160:
        text = text[:157] + "..."
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _round_or_none(value: Any, *, digits: int = 4) -> Optional[float]:
    try:
        if value is None or pd.isna(value):
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _series_sum_or_none(series: Any) -> Optional[float]:
    if series is None:
        return None
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None
    return _round_or_none(clean.sum())


def _dt_iso(value: Any) -> Optional[str]:
    try:
        if pd.isna(value):
            return None
        return pd.Timestamp(value).isoformat()
    except Exception:
        return str(value) if value is not None else None
