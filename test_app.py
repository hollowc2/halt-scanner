from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from rich.table import Table
from rich.text import Text
from textual.geometry import Size

from app import Halt, Scanner, Store, parse_nasdaq_feed, parse_yahoo_chart
from tui import HaltCard, duration, sparkline

ET = ZoneInfo("America/New_York")


class HaltScannerTests(unittest.TestCase):
    def test_tui_formatters(self) -> None:
        self.assertEqual(duration(3661.9), "01:01:01")
        self.assertEqual(duration(-1), "00:00:00")
        self.assertEqual(sparkline([[1, 10], [2, 20]]), "▁█")
        self.assertEqual(sparkline([]), "price unavailable")

    def test_tui_card_responds_to_width(self) -> None:
        class SizedCard(HaltCard):
            width = 120

            @property
            def size(self) -> Size:
                return Size(self.width, 7)

        halt = {
            "symbol": "TEST",
            "market": "NASDAQ",
            "name": "Test Company",
            "reason": "Volatility Trading Pause",
            "halt_at": dt.datetime.now().astimezone().isoformat(),
            "trend_pct": 2.5,
            "trend_points": [[1, 10], [2, 11]],
        }
        card = SizedCard(halt)
        self.assertIsInstance(card.render(), Table)
        card.width = 80
        self.assertIsInstance(card.render(), Text)

    def test_nasdaq_feed_parsing(self) -> None:
        xml = b"""<?xml version="1.0"?>
        <rss xmlns:ndaq="http://www.nasdaqtrader.com/"><channel><item>
        <ndaq:HaltDate>06/22/2026</ndaq:HaltDate>
        <ndaq:HaltTime>09:57:48.636</ndaq:HaltTime>
        <ndaq:IssueSymbol>TEST</ndaq:IssueSymbol>
        <ndaq:IssueName>Test Company</ndaq:IssueName>
        <ndaq:Market>NASDAQ</ndaq:Market>
        <ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
        <ndaq:PauseThresholdPrice>10.25</ndaq:PauseThresholdPrice>
        <ndaq:ResumptionDate>06/22/2026</ndaq:ResumptionDate>
        <ndaq:ResumptionQuoteTime>10:02:48</ndaq:ResumptionQuoteTime>
        <ndaq:ResumptionTradeTime>10:07:48</ndaq:ResumptionTradeTime>
        </item></channel></rss>"""
        halt = parse_nasdaq_feed(xml)[0]
        self.assertEqual(halt.symbol, "TEST")
        self.assertEqual(halt.threshold_price, "10.25")
        self.assertEqual(halt.halt_at, "2026-06-22T09:57:48-04:00")
        self.assertEqual(halt.resumption_trade_at, "2026-06-22T10:07:48-04:00")

    def test_yahoo_five_minute_move_and_sparse_data(self) -> None:
        halt_at = dt.datetime(2026, 6, 22, 10, 0, tzinfo=ET)
        base = int(halt_at.timestamp())
        payload = {
            "chart": {
                "result": [{
                    "timestamp": [base - 600, base - 300, base],
                    "indicators": {"quote": [{"close": [9.5, 10.0, 11.0]}]},
                }]
            }
        }
        trend, points = parse_yahoo_chart(json.dumps(payload).encode(), halt_at)
        self.assertEqual(trend, 10.0)
        self.assertEqual(len(points), 3)
        empty_trend, empty_points = parse_yahoo_chart(
            b'{"chart":{"result":[{"timestamp":[],"indicators":{"quote":[{"close":[]}]}}]}}',
            halt_at,
        )
        self.assertIsNone(empty_trend)
        self.assertEqual(empty_points, [])

    def test_store_upsert_updates_resumption_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "halts.db")
            store = Store(path)
            halt_at = "2026-06-22T09:57:48-04:00"
            first = Halt(
                "TEST:" + halt_at, "TEST", "Test", "NASDAQ", "LUDP", "", halt_at, None, None
            )
            seen = dt.datetime(2026, 6, 22, 10, 0, tzinfo=ET)
            store.upsert([first], seen)
            updated = Halt(
                first.id,
                "TEST",
                "Test",
                "NASDAQ",
                "LUDP",
                "",
                halt_at,
                "2026-06-22T10:02:48-04:00",
                "2026-06-22T10:07:48-04:00",
            )
            Store(path).upsert([updated], seen)
            row = Store(path).rows_for_day(seen.date())[0]
            self.assertEqual(row["resumption_trade_at"], "2026-06-22T10:07:48-04:00")

    def test_payload_splits_current_and_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = Store(str(Path(temp) / "halts.db"))
            today = dt.datetime.now(ET)
            old = (today - dt.timedelta(minutes=10)).isoformat()
            future = (today + dt.timedelta(minutes=5)).isoformat()
            past = (today - dt.timedelta(minutes=5)).isoformat()
            store.upsert(
                [
                    Halt("LIVE", "LIVE", "Live", "NYSE", "T1", "", old, None, future),
                    Halt("DONE", "DONE", "Done", "NASDAQ", "LUDP", "", old, None, past),
                ],
                today,
            )
            payload = Scanner(store).payload()
            self.assertEqual([item["symbol"] for item in payload["current"]], ["LIVE"])
            self.assertEqual([item["symbol"] for item in payload["resumed"]], ["DONE"])


if __name__ == "__main__":
    unittest.main()
