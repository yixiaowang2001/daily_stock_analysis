# -*- coding: utf-8 -*-
"""Tests for IBKR Flex Web Service helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import requests

from src.services.ibkr_flex_service import (
    IbkrFlexError,
    fetch_ibkr_flex_open_positions,
    parse_open_positions_from_csv,
    send_flex_request,
)


class IbkrFlexServiceTests(unittest.TestCase):
    def test_parse_open_positions_minimal_csv(self) -> None:
        csv_text = (
            "Statement,Title\n"
            "Open Positions\n"
            "DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,"
            "Cost Price,Cost Basis,Close Price,Value,Unrealized PnL\n"
            "Summary,STK,USD,AAPL,10,1,150,1500,180,1800,300\n"
        )
        rows = parse_open_positions_from_csv(csv_text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(rows[0]["market"], "us")
        self.assertEqual(rows[0]["currency"], "USD")
        self.assertAlmostEqual(rows[0]["quantity"], 10.0)
        self.assertAlmostEqual(rows[0]["total_cost"], 1500.0)

    def test_parse_hk_numeric_symbol(self) -> None:
        csv_text = (
            "Open Positions\n"
            "Symbol,Quantity,Mult,Currency,Asset Category,Listing Exchange,Cost Basis,Close Price,Value\n"
            "700,100,1,HKD,STK,SEHK,350000,380,38000\n"
        )
        rows = parse_open_positions_from_csv(csv_text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "HK00700")
        self.assertEqual(rows[0]["market"], "hk")

    def test_parse_client_account_id_flex_section(self) -> None:
        """Flex Query CSV with ClientAccountID header row (not Open Positions title)."""
        csv_text = (
            "ClientAccountID,LevelOfDetail,AssetClass,CurrencyPrimary,Symbol,Quantity,Mult,"
            "MarkPrice,PositionValue,CostBasisMoney,FifoPnlUnrealized\n"
            "U1234567,SUMMARY,STK,USD,MSFT,5,1,400,2000,1800,200\n"
        )
        rows = parse_open_positions_from_csv(csv_text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "MSFT")
        self.assertEqual(rows[0]["market"], "us")
        self.assertAlmostEqual(rows[0]["quantity"], 5.0)
        self.assertAlmostEqual(rows[0]["total_cost"], 1800.0)
        self.assertAlmostEqual(rows[0]["avg_cost"], 360.0)
        self.assertAlmostEqual(rows[0]["last_price"], 400.0)
        self.assertAlmostEqual(rows[0]["market_value_local"], 2000.0)
        self.assertAlmostEqual(rows[0]["unrealized_pnl_local"], 200.0)

    def test_parse_client_account_id_skips_lot_rows(self) -> None:
        csv_text = (
            "ClientAccountID,LevelOfDetail,Symbol,Quantity,Mult,MarkPrice,PositionValue,CostBasisMoney,"
            "FifoPnlUnrealized,AssetClass,CurrencyPrimary\n"
            "hdr,LOT,AAPL,10,1,100,1000,900,100,STK,USD\n"
            "hdr,SUMMARY,AAPL,10,1,100,1000,900,100,STK,USD\n"
        )
        rows = parse_open_positions_from_csv(csv_text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity"], 10.0)

    @patch("src.services.ibkr_flex_service.requests.get")
    def test_send_flex_request_reference_code(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.text = "<FlexStatementResponse><ReferenceCode>REF123</ReferenceCode></FlexStatementResponse>"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        ref = send_flex_request(token="tok", query_id="99")
        self.assertEqual(ref, "REF123")
        args, kwargs = mock_get.call_args
        self.assertIn("FlexStatementService.SendRequest", args[0])

    @patch("src.services.ibkr_flex_service.requests.get")
    def test_send_flex_request_connect_timeout_returns_503_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = requests.ConnectTimeout("timed out")
        with self.assertRaises(IbkrFlexError) as ctx:
            send_flex_request(token="tok", query_id="99")
        self.assertEqual(ctx.exception.suggested_status, 503)
        self.assertEqual(ctx.exception.error_detail, "ibkr_flex_network_error")
        self.assertIn("SendRequest", str(ctx.exception))

    @patch("src.services.ibkr_flex_service.get_flex_statement")
    @patch("src.services.ibkr_flex_service.send_flex_request")
    def test_fetch_ibkr_flex_open_positions_flow(
        self,
        mock_send: MagicMock,
        mock_get_stmt: MagicMock,
    ) -> None:
        mock_send.return_value = "REF"
        mock_get_stmt.return_value = (
            "Open Positions\n"
            "Symbol,Quantity,Mult,Currency,Asset Category,Cost Basis,Close Price,Value\n"
            "MSFT,2,1,USD,STK,500,600,1200\n"
        )
        positions, meta = fetch_ibkr_flex_open_positions(token="t", query_id="q", save_csv_path=None)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["symbol"], "MSFT")
        self.assertEqual(meta.get("reference_code"), "REF")

    def test_fetch_requires_credentials(self) -> None:
        with self.assertRaises(IbkrFlexError):
            fetch_ibkr_flex_open_positions(token="", query_id="")


if __name__ == "__main__":
    unittest.main()
