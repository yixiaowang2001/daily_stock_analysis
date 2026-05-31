# -*- coding: utf-8 -*-
"""
尾盘战术台：自动填充「次日早盘冲高 %」。

口径：相对 **前一交易日收盘价**，取 T+1 日 **09:30–10:01** 内分钟 K 的 **最高价** 计算冲高幅度（%）。
优先 AkShare `stock_zh_a_hist_min_em`；分钟数据不可用时复用尾盘 1 分钟多源
fallback（AkShare 分钟缓存 / efinance）；仍不可用时回退为 T+1 **日线最高价**
（口径变宽，仍写入 source 区分）。

仅处理 A 股（6 位数字 / 经 normalize 后）；港股、美股跳过并记入 notes。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from data_provider.base import DataFetcherManager, normalize_stock_code
from src.core.trading_calendar import get_market_for_stock, next_cn_trading_day_after
from src.services.tail_intraday_fetch import fetch_tail_intraday_cutoff_evidence

logger = logging.getLogger(__name__)


def _parse_trade_date(exp: Dict[str, Any]) -> Optional[date]:
    td = exp.get("trade_date")
    if not td:
        return None
    if isinstance(td, datetime):
        return td.date()
    if isinstance(td, date):
        return td
    s = str(td).strip()[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _prev_close_before(manager: DataFetcherManager, symbol: str, morning_d: date) -> Tuple[Optional[float], str]:
    try:
        df, src = manager.get_daily_data(symbol, days=160)
    except Exception as exc:
        logger.warning("tail morning: daily prev_close %s: %s", symbol, exc)
        return None, f"daily_error:{type(exc).__name__}"
    if df is None or df.empty or "close" not in df.columns or "date" not in df.columns:
        return None, "daily_empty"
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    m = pd.Timestamp(morning_d).normalize()
    prev = work.loc[work["date"] < m]
    if prev.empty:
        return None, "no_prev_bar"
    row = prev.iloc[-1]
    pc = row.get("close")
    if pc is None or (isinstance(pc, float) and pd.isna(pc)):
        return None, "prev_close_nan"
    v = float(pc)
    if v <= 0:
        return None, "prev_close_nonpositive"
    return v, src or "daily"


def _minute_window_surge(symbol: str, morning_d: date, prev_close: float) -> Tuple[Optional[float], str]:
    start = f"{morning_d.isoformat()} 09:30:00"
    end = f"{morning_d.isoformat()} 10:01:00"
    try:
        import akshare as ak

        df = ak.stock_zh_a_hist_min_em(
            symbol=symbol,
            period="5",
            adjust="",
            start_date=start,
            end_date=end,
        )
    except Exception as exc:
        logger.info("tail morning: minute %s: %s", symbol, exc)
        return None, f"akshare_minute:{type(exc).__name__}"
    if df is None or df.empty or "最高" not in df.columns:
        return None, "minute_empty"
    highs = pd.to_numeric(df["最高"], errors="coerce")
    mx = float(highs.max())
    if pd.isna(mx):
        return None, "minute_high_nan"
    pct = (mx / prev_close - 1.0) * 100.0
    return round(pct, 4), "akshare_5m_em_0930_1000"


def _fallback_minute_window_surge(
    symbol: str,
    morning_d: date,
    prev_close: float,
) -> Tuple[Optional[float], str]:
    """Fetch strict morning-window high via the shared 1-minute fallback chain."""

    try:
        payload = fetch_tail_intraday_cutoff_evidence(
            symbol=symbol,
            trade_date=morning_d,
            selection_cutoff="10:01",
            previous_close=prev_close,
        )
    except Exception as exc:
        logger.info("tail morning: fallback minute %s: %s", symbol, exc)
        return None, f"intraday_minute:{type(exc).__name__}"

    if not payload.get("available"):
        reason = str(payload.get("missing_reason") or "intraday_unavailable")
        return None, reason

    fields = payload.get("fields") or {}
    pct = fields.get("pre_cutoff_high_pct")
    if pct is None:
        high = fields.get("pre_cutoff_high")
        if high is None or prev_close <= 0:
            return None, "intraday_high_missing"
        pct = (float(high) / prev_close - 1.0) * 100.0
    try:
        pct_value = float(pct)
    except (TypeError, ValueError):
        return None, "intraday_high_nan"
    if pd.isna(pct_value):
        return None, "intraday_high_nan"

    source = str(payload.get("source") or "intraday_1m")
    return round(pct_value, 4), f"{source}_0930_1001"


def _daily_high_fallback(manager: DataFetcherManager, symbol: str, morning_d: date, prev_close: float) -> Tuple[Optional[float], str]:
    try:
        df, src = manager.get_daily_data(symbol, days=40)
    except Exception as exc:
        return None, f"daily_high_error:{type(exc).__name__}"
    if df is None or df.empty or "high" not in df.columns or "date" not in df.columns:
        return None, "daily_high_empty"
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    m = pd.Timestamp(morning_d).normalize()
    row = work.loc[work["date"] == m]
    if row.empty:
        return None, "no_t1_bar"
    hi = row.iloc[0].get("high")
    if hi is None or (isinstance(hi, float) and pd.isna(hi)):
        return None, "daily_high_nan"
    mx = float(hi)
    pct = (mx / prev_close - 1.0) * 100.0
    return round(pct, 4), f"daily_high_t1_fallback:{src or 'daily'}"


def auto_fetch_tail_morning_metrics(
    *,
    experiment: Dict[str, Any],
    morning_trade_date: Optional[date] = None,
    manager: Optional[DataFetcherManager] = None,
    pause_sec: float = 0.25,
) -> Tuple[List[Dict[str, Any]], date, List[str]]:
    """
    构建并返回可交给 ``DatabaseManager.upsert_tail_morning_metrics`` 的 ``items``（不写库）。

    Returns:
        (items, resolved_morning_trade_date, notes)
    """
    t0 = _parse_trade_date(experiment)
    if t0 is None:
        raise ValueError("experiment trade_date missing or invalid")

    m_date = morning_trade_date
    if m_date is None:
        m_date = next_cn_trading_day_after(t0)
    if m_date is None:
        raise ValueError("cannot resolve next A-share trading day after trade_date")

    mgr = manager or DataFetcherManager()
    notes: List[str] = []
    items: List[Dict[str, Any]] = []
    symbols = list(experiment.get("symbols") or [])

    for raw_sym in symbols:
        sym = normalize_stock_code(str(raw_sym).strip())
        if get_market_for_stock(sym) != "cn":
            notes.append(f"{sym}: 非 A 股，跳过自动拉取")
            items.append({"symbol": sym, "surge_pct_prev_close_930_1000": None, "source": "skipped_non_cn"})
            time.sleep(pause_sec)
            continue

        prev_close, prev_reason = _prev_close_before(mgr, sym, m_date)
        if prev_close is None:
            notes.append(f"{sym}: 无法取得昨收（{prev_reason}）")
            items.append({"symbol": sym, "surge_pct_prev_close_930_1000": None, "source": f"error:{prev_reason}"})
            time.sleep(pause_sec)
            continue

        pct, src = _minute_window_surge(sym, m_date, prev_close)
        primary_minute_source = src
        if pct is None:
            pct2, src2 = _fallback_minute_window_surge(sym, m_date, prev_close)
            pct, src = pct2, src2
            if pct is not None:
                notes.append(
                    f"{sym}: 东财5分钟无数据，已用1分钟线回退（{src}；primary={primary_minute_source}）"
                )

        if pct is None:
            intraday_source = src
            pct2, src2 = _daily_high_fallback(mgr, sym, m_date, prev_close)
            pct, src = pct2, src2
            if pct is None:
                notes.append(
                    f"{sym}: 分钟与日线回退均失败（primary={primary_minute_source}; "
                    f"intraday={intraday_source}; daily={src}）"
                )
            else:
                notes.append(
                    f"{sym}: 分钟无数据，已用日线最高回退（{src}；primary={primary_minute_source}; "
                    f"intraday={intraday_source}）"
                )

        items.append(
            {
                "symbol": sym,
                "surge_pct_prev_close_930_1000": pct,
                "source": src,
            }
        )
        time.sleep(pause_sec)

    return items, m_date, notes
