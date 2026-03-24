# -*- coding: utf-8 -*-
"""Tests for IBKR Flex Web Service helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

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
