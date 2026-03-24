# -*- coding: utf-8 -*-
"""IBKR Flex Statement Web Service: fetch Open Positions CSV and normalize rows.

Uses Client Portal Flex Query token + query id (not TWS/IB Gateway).
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import requests

from data_provider.base import canonical_stock_code

logger = logging.getLogger(__name__)

FLEX_SERVLET_BASE = "https://www.interactivebrokers.com/Universal/servlet"
FLEX_VERSION = "3"
DEFAULT_TIMEOUT = (10, 120)
PROGRESS_PHRASE = "statement generation in progress"
MAX_GET_ATTEMPTS = 30
GET_POLL_INTERVAL_SEC = 2.0


class IbkrFlexError(Exception):
    """Flex Web Service returned an error or unexpected payload."""


def _requests_proxies() -> Optional[Dict[str, str]]:
    http = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not http and not https:
        return None
    out: Dict[str, str] = {}
    if http:
        out["http"] = http
    if https:
        out["https"] = https
    return out


def send_flex_request(*, token: str, query_id: str) -> str:
    """Call SendRequest; return ReferenceCode (string)."""
    url = f"{FLEX_SERVLET_BASE}/FlexStatementService.SendRequest"
    params = {"t": token, "q": query_id, "v": FLEX_VERSION}
    resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT, proxies=_requests_proxies())
    resp.raise_for_status()
    text = resp.text or ""
    if "<ErrorCode>" in text or "errorCode" in text.lower():
        code = _xml_find_text(text, "ErrorCode") or ""
        msg = _xml_find_text(text, "ErrorMessage") or text[:500]
        raise IbkrFlexError(f"SendRequest failed: {code} {msg}".strip())

    ref = _xml_find_text(text, "ReferenceCode")
    if not ref:
        raise IbkrFlexError("SendRequest: missing ReferenceCode in response")
    return ref.strip()


def get_flex_statement(*, token: str, reference_code: str) -> str:
    """Call GetStatement; poll until CSV/statement is ready."""
    url = f"{FLEX_SERVLET_BASE}/FlexStatementService.GetStatement"
    params = {"t": token, "q": reference_code, "v": FLEX_VERSION}
    last_body = ""
    for attempt in range(1, MAX_GET_ATTEMPTS + 1):
        resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT, proxies=_requests_proxies())
        resp.raise_for_status()
        last_body = resp.text or ""
        lower = last_body.lower()
        if PROGRESS_PHRASE in lower:
            logger.info("IBKR Flex: statement in progress, attempt %s/%s", attempt, MAX_GET_ATTEMPTS)
            time.sleep(GET_POLL_INTERVAL_SEC)
            continue
        if "<ErrorCode>" in last_body:
            code = _xml_find_text(last_body, "ErrorCode") or ""
            msg = _xml_find_text(last_body, "ErrorMessage") or last_body[:500]
            raise IbkrFlexError(f"GetStatement failed: {code} {msg}".strip())
        return last_body

    raise IbkrFlexError(f"GetStatement: still in progress after {MAX_GET_ATTEMPTS} attempts")


def _xml_find_text(xml_text: str, tag: str) -> Optional[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # Fallback: regex for simple Flex responses
        m = re.search(rf"<{tag}>([^<]*)</{tag}>", xml_text, re.IGNORECASE)
        return m.group(1).strip() if m else None
    for el in root.iter():
        if el.tag.endswith(tag) or el.tag == tag:
            if el.text and el.text.strip():
                return el.text.strip()
    return None


def _parse_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if not s or s.lower() in ("nan", "none", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _infer_market_and_symbol(
    *,
    raw_symbol: str,
    asset_category: str,
    listing_exchange: str,
    currency: str,
) -> Tuple[str, str]:
    """Return (canonical_symbol, market) with market in cn/hk/us."""
    sym = (raw_symbol or "").strip().upper()
    ac = (asset_category or "").strip().upper()
    ex = (listing_exchange or "").strip().upper()
    cur = (currency or "").strip().upper()

    # Hong Kong: numeric codes
    if sym.isdigit() and len(sym) >= 4:
        padded = sym.zfill(5) if len(sym) < 5 else sym
        return canonical_stock_code(f"HK{padded}"), "hk"

    hk_ex = {"SEHK", "HKG", "HKEX", "SEHKNTL", "HKFE"}
    if ex in hk_ex or (cur == "HKD" and sym.isdigit()):
        padded = sym.zfill(5) if sym.isdigit() and len(sym) < 5 else sym
        if sym.isdigit():
            return canonical_stock_code(f"HK{padded}"), "hk"
        return canonical_stock_code(sym), "hk"

    cn_ex = {"SSE", "SHA", "SHE", "SZSE", "SZE", "CN"}
    if ex in cn_ex or (cur == "CNY" and sym.isdigit() and len(sym) == 6):
        return canonical_stock_code(sym), "cn"

    # US-style tickers
    if ac in ("STK", "ETF", "FUND", "WAR", "IOPT", "OPT", "FOP", "CFD"):
        if cur == "USD" or ex in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "PINK", "OTC"}:
            return canonical_stock_code(sym), "us"

    if cur == "USD" and re.match(r"^[A-Z]{1,5}$", sym):
        return canonical_stock_code(sym), "us"

    # Default: US for Latin tickers, else CN for digits
    if sym.isdigit() and len(sym) == 6:
        return canonical_stock_code(sym), "cn"
    if re.match(r"^[A-Z]{1,5}(\.[A-Z])?$", sym):
        return canonical_stock_code(sym), "us"

    return canonical_stock_code(sym), "us"


def parse_open_positions_from_csv(text: str) -> List[Dict[str, Any]]:
    """Parse Flex CSV text; extract Open Positions rows into normalized dicts."""
    if not text or not text.strip():
        raise IbkrFlexError("Empty Flex statement body")

    # Strip BOM
    if text.startswith("\ufeff"):
        text = text[1:]

    reader = csv.reader(io.StringIO(text))
    rows: List[List[str]] = [list(r) for r in reader]

    sections: List[Tuple[str, List[str], List[List[str]]]] = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if not row or all(not (c or "").strip() for c in row):
            i += 1
            continue
        first = (row[0] or "").strip()
        if first == "Open Positions":
            if len(row) > 1 and any((c or "").strip() for c in row[1:]):
                header = [c.strip() for c in row[1:]]
                i += 1
                data_rows: List[List[str]] = []
                while i < len(rows):
                    r = rows[i]
                    if not r or all(not (c or "").strip() for c in r):
                        break
                    if (r[0] or "").strip() and (r[0] or "").strip() != "Open Positions":
                        if len(r) == 1 or (r[0] or "").strip() in (
                            "Trades",
                            "Cash Transactions",
                            "Corporate Actions",
                            "Interest Accruals",
                            "Change in Dividend Accruals",
                            "Open Dividend Accruals",
                            "Forex Balances",
                            "Net Asset Value",
                            "Statement",
                            "Account Information",
                        ):
                            break
                    data_rows.append(r)
                    i += 1
                sections.append(("Open Positions", header, data_rows))
                continue
            i += 1
            if i >= len(rows):
                break
            header = [c.strip() for c in rows[i]]
            i += 1
            data_rows = []
            while i < len(rows):
                r = rows[i]
                if not r or all(not (c or "").strip() for c in r):
                    break
                label = (r[0] or "").strip()
                if label == "Open Positions":
                    break
                if label in (
                    "Trades",
                    "Cash Transactions",
                    "Corporate Actions",
                    "Statement",
                    "Account Information",
                ):
                    break
                data_rows.append(r)
                i += 1
            sections.append(("Open Positions", header, data_rows))
            continue
        i += 1

    if not sections:
        raise IbkrFlexError("No Open Positions section found in Flex CSV")

    header, data_rows = sections[0][1], sections[0][2]
    if not header:
        raise IbkrFlexError("Open Positions: missing header row")

    header_lc = {str(h).strip().lower(): i for i, h in enumerate(header) if str(h).strip()}

    def get_cell(r: List[str], *candidates: str) -> Optional[str]:
        for c in candidates:
            idx = header_lc.get(c.lower())
            if idx is not None and idx < len(r):
                v = (r[idx] or "").strip()
                if v:
                    return v
        return None

    out: List[Dict[str, Any]] = []
    for r in data_rows:
        if not r or all(not (c or "").strip() for c in r):
            continue
        disc = (get_cell(r, "DataDiscriminator", "dataDiscriminator") or "").strip()
        if disc and disc.lower() not in ("summary", "detail", ""):
            if disc.lower() in ("header", "total"):
                continue

        raw_sym = get_cell(r, "Symbol", "symbol", "Ticker", "ticker")
        if not raw_sym:
            continue

        qty = _parse_float(get_cell(r, "Quantity", "quantity", "Position", "position"))
        if qty is None or abs(qty) < 1e-12:
            continue

        asset_cat = get_cell(r, "Asset Category", "AssetCategory", "assetCategory") or ""
        listing_ex = get_cell(r, "Listing Exchange", "ListingExchange", "listingExchange") or ""
        cur = (get_cell(r, "Currency", "currency") or "USD").upper()

        symbol, market = _infer_market_and_symbol(
            raw_symbol=raw_sym,
            asset_category=asset_cat,
            listing_exchange=listing_ex,
            currency=cur,
        )

        mult = _parse_float(get_cell(r, "Mult", "multiplier")) or 1.0
        qty_eff = float(qty) * float(mult)

        cost_basis = _parse_float(
            get_cell(r, "Cost Basis", "CostBasis", "costBasis", "Fifo Pnl Realized", "FifoPnlRealized")
        )
        cost_price = _parse_float(get_cell(r, "Cost Price", "CostPrice", "costPrice", "Average Cost", "AverageCost"))

        close_price = _parse_float(
            get_cell(r, "Close Price", "ClosePrice", "Mark Price", "MarkPrice", "closePrice", "markPrice")
        )

        value_local = _parse_float(get_cell(r, "Value", "value", "Position Value", "PositionValue"))
        unreal_local = _parse_float(
            get_cell(r, "Unrealized PnL", "UnrealizedPnL", "Fifo Pnl Unrealized", "FifoPnlUnrealized")
        )

        if cost_basis is None and cost_price is not None and qty_eff:
            cost_basis = abs(qty_eff) * cost_price

        if cost_basis is None:
            continue

        if close_price is None and value_local is not None and qty_eff:
            close_price = value_local / abs(qty_eff) if abs(qty_eff) > 1e-12 else None

        if close_price is None:
            close_price = cost_price if cost_price is not None else cost_basis / abs(qty_eff) if abs(qty_eff) > 1e-12 else 0.0

        avg_cost = cost_basis / abs(qty_eff) if abs(qty_eff) > 1e-12 else 0.0

        if value_local is None and close_price is not None:
            value_local = abs(qty_eff) * close_price

        out.append(
            {
                "symbol": symbol,
                "market": market,
                "currency": cur,
                "quantity": abs(qty_eff),
                "avg_cost": float(avg_cost),
                "total_cost": float(cost_basis),
                "last_price": float(close_price or 0.0),
                "market_value_local": float(value_local) if value_local is not None else None,
                "unrealized_pnl_local": float(unreal_local) if unreal_local is not None else None,
                "asset_category": asset_cat,
                "listing_exchange": listing_ex,
                "raw_symbol": raw_sym,
            }
        )

    return out


def fetch_ibkr_flex_open_positions(
    *,
    token: Optional[str],
    query_id: Optional[str],
    save_csv_path: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Full flow: SendRequest -> GetStatement -> parse positions.

    Returns (positions, meta) where meta includes reference_code and optional path.
    """
    if not (token or "").strip() or not (query_id or "").strip():
        raise IbkrFlexError("IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID must be set")

    ref = send_flex_request(token=token.strip(), query_id=query_id.strip())
    body = get_flex_statement(token=token.strip(), reference_code=ref)

    if save_csv_path:
        try:
            with open(save_csv_path, "w", encoding="utf-8") as f:
                f.write(body)
        except OSError as exc:
            logger.warning("Could not save Flex CSV to %s: %s", save_csv_path, exc)

    positions = parse_open_positions_from_csv(body)
    meta = {
        "reference_code": ref,
        "position_count": len(positions),
        "saved_path": save_csv_path,
    }
    return positions, meta
