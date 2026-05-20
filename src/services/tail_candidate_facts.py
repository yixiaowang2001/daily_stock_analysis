# -*- coding: utf-8 -*-
"""Build a compact evidence bundle for tail-session candidate scoring.

This module is intentionally DB-first and side-effect free.  It prepares the
facts that DSA already knows, while leaving judgement and scoring to the Agent.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.core.trading_calendar import next_cn_trading_day_after
from src.services.tail_intraday_fetch import fetch_tail_intraday_cutoff_evidence


def build_tail_candidate_facts(
    *,
    experiment: Dict[str, Any],
    db: Any,
    now: Optional[datetime] = None,
    lookback_days: int = 30,
    include_intraday_fetch: bool = True,
    intraday_fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return a structured facts bundle for one tail tactics experiment.

    The bundle separates observable evidence from interpretation. It is
    DB-first, then optionally augments current tail-session scoring with
    pre-cutoff minute evidence from fail-open providers.
    """

    trade_date = _parse_date(experiment.get("trade_date"))
    symbols = [str(s).strip() for s in experiment.get("symbols") or [] if str(s).strip()]
    generated_at = _market_now(now)
    morning_trade_date = next_cn_trading_day_after(trade_date) if trade_date else None
    param_snapshot = experiment.get("param_snapshot") or {}
    selection_cutoff = _resolve_selection_cutoff(param_snapshot)
    phase = _resolve_phase(
        trade_date=trade_date,
        morning_trade_date=morning_trade_date,
        now=generated_at,
    )

    morning_metrics = []
    experiment_id = experiment.get("id")
    if experiment_id is not None:
        try:
            morning_metrics = db.list_tail_morning_metrics(int(experiment_id))
        except Exception:
            morning_metrics = []
    morning_by_symbol = _index_morning_metrics(morning_metrics)

    candidates = []
    for symbol in symbols:
        resolved_code, bars = _select_daily_bars(
            db=db,
            symbol=symbol,
            trade_date=trade_date,
            lookback_days=lookback_days,
        )
        trade_bar, previous_bar = _split_trade_and_previous_bars(bars, trade_date)
        recent_bars = [_bar_to_payload(b) for b in bars[-6:]]
        cutoff_evidence = _build_cutoff_evidence(
            symbol=symbol,
            trade_date=trade_date,
            selection_cutoff=selection_cutoff,
            previous_close=_num(getattr(previous_bar, "close", None)) if previous_bar else None,
            should_fetch=include_intraday_fetch
            and _should_attempt_intraday_fetch(trade_date=trade_date, phase=phase),
            fetcher=intraday_fetcher or fetch_tail_intraday_cutoff_evidence,
        )
        normalized = _normalize_symbol(symbol)
        morning_metric = _lookup_morning_metric(morning_by_symbol, symbol)
        missing_fields = _missing_fields(
            trade_bar=trade_bar,
            morning_metric=morning_metric,
            cutoff_evidence=cutoff_evidence,
        )
        has_cutoff_evidence = bool(cutoff_evidence.get("available"))

        candidates.append(
            {
                "code": symbol,
                "resolved_data_code": resolved_code,
                "data_status": (
                    "ok" if not missing_fields else "partial" if trade_bar or has_cutoff_evidence else "missing"
                ),
                "missing_fields": missing_fields,
                "selection_cutoff_evidence": cutoff_evidence,
                "trade_date_bar": _bar_to_payload(trade_bar),
                "trade_date_bar_scope": "post_close_reference_not_for_scoring",
                "previous_trade_bar": _bar_to_payload(previous_bar),
                "recent_daily_bars": recent_bars,
                "recent_daily_bars_scope": "daily_bars_through_trade_date; trade_date row is post-close if present",
                "derived_features": _derive_daily_features(
                    trade_bar=trade_bar,
                    previous_bar=previous_bar,
                    recent_bars=bars,
                ),
                "derived_features_scope": "post_close_reference_not_for_scoring",
                "morning_metric": morning_metric,
                "lookup_codes_tried": _code_candidates(symbol),
                "normalized_code": normalized,
            }
        )

    limitations = _build_limitations(
        candidates=candidates,
        phase=phase,
        morning_trade_date=morning_trade_date,
    )

    return {
        "mode": _mode_from_phase(phase),
        "experiment_id": experiment_id,
        "trade_date": trade_date.isoformat() if trade_date else None,
        "morning_trade_date": morning_trade_date.isoformat() if morning_trade_date else None,
        "selection_cutoff": {
            "time": selection_cutoff,
            "timezone": "Asia/Shanghai",
            "policy": (
                "Scoring must only use information observable at or before this time on trade_date. "
                "Full-day T bars, T close, and post-cutoff highs are reference-only and must not drive scoring."
            ),
        },
        "generated_at": generated_at.isoformat(),
        "phase": phase,
        "data_freshness": {
            "summary": _freshness_summary(candidates, phase),
            "limitations": limitations,
        },
        "source_policy": {
            "primary": "DSA SQLite stock_daily and tail_morning_metric",
            "minute_fallback": (
                "When scoring is composed during the tail window or before T+1 open, DSA attempts "
                "AkShare/Eastmoney/efinance 1-minute bars capped at selection_cutoff. Failures are recorded "
                "without blocking compose."
            ),
            "fallback": "Agent may call realtime/news/minute tools only for missing or stale fields, constrained by selection_cutoff",
            "judgement_boundary": "DSA provides evidence; Agent owns scoring, risk interpretation, and confidence.",
            "leakage_boundary": "Do not score with trade_date full-day close/high/amount or any data after selection_cutoff.",
        },
        "source_conflicts": [],
        "candidates": candidates,
        "agent_judgement_prompt": (
            "Use candidate_facts as the first evidence layer. Keep factual gaps and source limits explicit; "
            "do not treat DSA's cached data as the final conclusion. Apply independent risk-gate reasoning "
            "before assigning action_level and score. Never convert a weak single candidate into a recommendation."
        ),
    }


def _market_now(now: Optional[datetime]) -> datetime:
    tz = ZoneInfo("Asia/Shanghai")
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _resolve_selection_cutoff(param_snapshot: Dict[str, Any]) -> str:
    raw = (
        param_snapshot.get("data_cutoff_time")
        or param_snapshot.get("selection_cutoff_time")
        or param_snapshot.get("cutoff_time")
        or "14:40"
    )
    text = str(raw).strip()
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return "14:40"


def _resolve_phase(
    *,
    trade_date: Optional[date],
    morning_trade_date: Optional[date],
    now: datetime,
) -> str:
    if trade_date is None:
        return "unknown"
    today = now.date()
    now_t = now.time()

    if today < trade_date:
        return "before_trade_date"
    if today == trade_date:
        if now_t < time(14, 30):
            return "trade_date_before_tail"
        if now_t < time(15, 0):
            return "trade_date_tail_window"
        return "trade_date_after_close"

    if morning_trade_date and today == morning_trade_date:
        if now_t < time(9, 15):
            return "t_plus_1_pre_call_auction"
        if now_t < time(9, 30):
            return "t_plus_1_call_auction"
        if now_t < time(10, 0):
            return "t_plus_1_morning_window"
        if now_t < time(15, 0):
            return "t_plus_1_intraday_after_morning"
        return "t_plus_1_after_close"

    if morning_trade_date and today > morning_trade_date:
        return "after_t_plus_1"
    return "between_trade_date_and_t_plus_1"


def _mode_from_phase(phase: str) -> str:
    if phase in {"t_plus_1_pre_call_auction", "t_plus_1_call_auction"}:
        return "pre_open_rank"
    if phase == "t_plus_1_morning_window":
        return "morning_review_in_progress"
    if phase.startswith("t_plus_1") or phase == "after_t_plus_1":
        return "morning_review_ready"
    if phase in {"trade_date_after_close", "between_trade_date_and_t_plus_1"}:
        return "pre_open_rank"
    return "tail_rank"


def _should_attempt_intraday_fetch(*, trade_date: Optional[date], phase: str) -> bool:
    if trade_date is None:
        return False
    return phase in {
        "trade_date_tail_window",
        "trade_date_after_close",
        "between_trade_date_and_t_plus_1",
        "t_plus_1_pre_call_auction",
        "t_plus_1_call_auction",
    }


def _normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if "." in s:
        s = s.split(".", 1)[0]
    if s.startswith(("SZ", "SH", "BJ")) and len(s) >= 8:
        s = s[2:]
    return s


def _code_candidates(symbol: str) -> List[str]:
    raw = (symbol or "").strip().upper()
    normalized = _normalize_symbol(raw)
    candidates = [raw, normalized]
    if normalized.isdigit() and len(normalized) == 6:
        if normalized.startswith(("0", "2", "3")):
            candidates.extend([f"{normalized}.SZ", f"SZ{normalized}"])
        elif normalized.startswith(("6", "9")):
            candidates.extend([f"{normalized}.SH", f"SH{normalized}"])
        elif normalized.startswith(("4", "8")):
            candidates.extend([f"{normalized}.BJ", f"BJ{normalized}"])
    return _dedupe(c for c in candidates if c)


def _dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _build_cutoff_evidence(
    *,
    symbol: str,
    trade_date: Optional[date],
    selection_cutoff: str,
    previous_close: Optional[float] = None,
    should_fetch: bool = False,
    fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Describe the pre-cutoff evidence available for scoring."""

    if trade_date and should_fetch and fetcher is not None:
        try:
            return fetcher(
                symbol=symbol,
                trade_date=trade_date,
                selection_cutoff=selection_cutoff,
                previous_close=previous_close,
            )
        except Exception as exc:
            return {
                "code": symbol,
                "as_of": f"{trade_date.isoformat()} {selection_cutoff}:00",
                "available": False,
                "source": None,
                "fields": {},
                "missing_reason": "intraday_pre_cutoff_fetch_exception",
                "fallback_attempts": [
                    {
                        "source": getattr(fetcher, "__name__", fetcher.__class__.__name__),
                        "status": "failed",
                        "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                    }
                ],
                "allowed_fallback": "Agent may fetch historical minute data capped at selection_cutoff; full-day T bar is not a valid fallback for scoring.",
            }

    reason = "intraday_fetch_skipped_outside_scoring_window" if trade_date else "trade_date_missing"
    return {
        "code": symbol,
        "as_of": f"{trade_date.isoformat()} {selection_cutoff}:00" if trade_date else None,
        "available": False,
        "source": None,
        "fields": {},
        "missing_reason": reason,
        "allowed_fallback": "Agent may fetch historical minute data capped at selection_cutoff; full-day T bar is not a valid fallback for scoring.",
    }


def _select_daily_bars(
    *,
    db: Any,
    symbol: str,
    trade_date: Optional[date],
    lookback_days: int,
) -> Tuple[Optional[str], List[Any]]:
    if trade_date is None:
        return None, []
    start = trade_date - timedelta(days=max(lookback_days * 2, 10))
    best_code = None
    best_bars: List[Any] = []
    best_key = None
    for code in _code_candidates(symbol):
        try:
            bars = list(db.get_data_range(code, start, trade_date) or [])
        except Exception:
            bars = []
        if not bars:
            continue
        has_trade_bar = any(_bar_date(b) == trade_date for b in bars)
        latest = max((_bar_date(b) for b in bars), default=date.min)
        key = (has_trade_bar, latest, len(bars), code == _normalize_symbol(symbol))
        if best_key is None or key > best_key:
            best_key = key
            best_code = code
            best_bars = sorted(bars, key=_bar_date)
    return best_code, best_bars


def _split_trade_and_previous_bars(
    bars: List[Any],
    trade_date: Optional[date],
) -> Tuple[Optional[Any], Optional[Any]]:
    if trade_date is None:
        return None, None
    trade_bar = None
    previous_bar = None
    for bar in bars:
        d = _bar_date(bar)
        if d == trade_date:
            trade_bar = bar
        elif d < trade_date:
            previous_bar = bar
    return trade_bar, previous_bar


def _bar_date(bar: Any) -> date:
    if bar is None:
        return date.min
    value = getattr(bar, "date", None)
    parsed = _parse_date(value)
    return parsed or date.min


def _bar_to_payload(bar: Any) -> Optional[Dict[str, Any]]:
    if bar is None:
        return None
    data = bar.to_dict() if hasattr(bar, "to_dict") else dict(bar)
    payload: Dict[str, Any] = {}
    for key in [
        "code",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pct_chg",
        "ma5",
        "ma10",
        "ma20",
        "volume_ratio",
        "data_source",
    ]:
        value = data.get(key)
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        payload[key] = value
    return payload


def _derive_daily_features(
    *,
    trade_bar: Optional[Any],
    previous_bar: Optional[Any],
    recent_bars: List[Any],
) -> Dict[str, Any]:
    if trade_bar is None:
        return {}
    close = _num(getattr(trade_bar, "close", None))
    high = _num(getattr(trade_bar, "high", None))
    low = _num(getattr(trade_bar, "low", None))
    open_ = _num(getattr(trade_bar, "open", None))
    ma5 = _num(getattr(trade_bar, "ma5", None))
    ma10 = _num(getattr(trade_bar, "ma10", None))
    ma20 = _num(getattr(trade_bar, "ma20", None))
    previous_close = _num(getattr(previous_bar, "close", None)) if previous_bar else None
    pct_chg = _num(getattr(trade_bar, "pct_chg", None))

    day_range = (high - low) if high is not None and low is not None else None
    close_position = None
    if close is not None and low is not None and day_range and day_range > 0:
        close_position = round((close - low) / day_range, 4)

    close_to_high_pct = None
    if close is not None and high is not None and previous_close:
        close_to_high_pct = round((high - close) / previous_close * 100, 4)

    open_to_close_pct = None
    if close is not None and open_:
        open_to_close_pct = round((close - open_) / open_ * 100, 4)

    recent_closes = [_num(getattr(b, "close", None)) for b in recent_bars if _num(getattr(b, "close", None)) is not None]
    recent_5d_return_pct = None
    if len(recent_closes) >= 2 and recent_closes[-min(5, len(recent_closes))]:
        anchor = recent_closes[-min(5, len(recent_closes))]
        recent_5d_return_pct = round((recent_closes[-1] - anchor) / anchor * 100, 4)

    return {
        "close_position_in_day_range": close_position,
        "close_to_high_pct": close_to_high_pct,
        "open_to_close_pct": open_to_close_pct,
        "pct_change": pct_chg,
        "amount_yuan": _num(getattr(trade_bar, "amount", None)),
        "volume": _num(getattr(trade_bar, "volume", None)),
        "volume_ratio": _num(getattr(trade_bar, "volume_ratio", None)),
        "above_ma5": _greater(close, ma5),
        "above_ma10": _greater(close, ma10),
        "above_ma20": _greater(close, ma20),
        "ma_bullish_alignment": bool(
            close is not None
            and ma5 is not None
            and ma10 is not None
            and ma20 is not None
            and close >= ma5 >= ma10 >= ma20
        ),
        "previous_day_pct_change": _num(getattr(previous_bar, "pct_chg", None)) if previous_bar else None,
        "recent_5d_return_pct": recent_5d_return_pct,
    }


def _num(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _greater(left: Optional[float], right: Optional[float]) -> Optional[bool]:
    if left is None or right is None:
        return None
    return left >= right


def _index_morning_metrics(metrics: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    indexed: Dict[str, Dict[str, Any]] = {}
    for item in metrics:
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        for key in _code_candidates(symbol):
            indexed[key] = item
    return indexed


def _lookup_morning_metric(
    indexed: Dict[str, Dict[str, Any]],
    symbol: str,
) -> Optional[Dict[str, Any]]:
    for key in _code_candidates(symbol):
        if key in indexed:
            return indexed[key]
    return None


def _missing_fields(
    *,
    trade_bar: Optional[Any],
    morning_metric: Optional[Dict[str, Any]],
    cutoff_evidence: Optional[Dict[str, Any]] = None,
) -> List[str]:
    missing = []
    if not cutoff_evidence or not cutoff_evidence.get("available"):
        missing.append("trade_date_intraday_until_cutoff")
    if trade_bar is None:
        missing.append("trade_date_daily_bar")
    if morning_metric is None:
        missing.append("t_plus_1_morning_metric")
    return missing


def _build_limitations(
    *,
    candidates: List[Dict[str, Any]],
    phase: str,
    morning_trade_date: Optional[date],
) -> List[str]:
    limitations = []
    missing_daily = [c["code"] for c in candidates if c.get("trade_date_bar") is None]
    missing_cutoff = [
        c["code"]
        for c in candidates
        if not (c.get("selection_cutoff_evidence") or {}).get("available")
    ]
    missing_morning = [c["code"] for c in candidates if c.get("morning_metric") is None]
    if missing_cutoff:
        limitations.append(
            f"Missing pre-cutoff intraday evidence for: {', '.join(missing_cutoff)}; "
            "full-day T bars are reference-only and must not be used for scoring."
        )
    if missing_daily:
        limitations.append(f"Missing T-day daily bars for: {', '.join(missing_daily)}")
    if missing_morning:
        if phase in {
            "before_trade_date",
            "trade_date_before_tail",
            "trade_date_tail_window",
            "trade_date_after_close",
            "between_trade_date_and_t_plus_1",
            "t_plus_1_pre_call_auction",
            "t_plus_1_call_auction",
        }:
            limitations.append("T+1 morning metric is not expected to be complete before the 09:30-10:00 window.")
        else:
            label = morning_trade_date.isoformat() if morning_trade_date else "T+1"
            limitations.append(f"Missing {label} 09:30-10:00 morning metrics for: {', '.join(missing_morning)}")
    if missing_cutoff:
        limitations.append(
            "Minute-level selection evidence is attempted only for active tail scoring windows; "
            "unavailable candidates retain explicit missing_reason/fallback_attempts."
        )
    else:
        limitations.append("Minute-level selection evidence is included and capped at selection_cutoff.")
    return limitations


def _freshness_summary(candidates: List[Dict[str, Any]], phase: str) -> str:
    total = len(candidates)
    daily_ready = sum(1 for c in candidates if c.get("trade_date_bar"))
    cutoff_ready = sum(1 for c in candidates if (c.get("selection_cutoff_evidence") or {}).get("available"))
    morning_ready = sum(1 for c in candidates if c.get("morning_metric"))
    return (
        f"{cutoff_ready}/{total} candidates have pre-cutoff intraday evidence; "
        f"{daily_ready}/{total} candidates have T-day daily bars; "
        f"{morning_ready}/{total} candidates have T+1 morning metrics; phase={phase}."
    )
