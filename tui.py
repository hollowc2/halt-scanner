#!/usr/bin/env python3
"""Textual client for the halt scanner HTTP API."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.request
from typing import Any

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Static


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def sparkline(points: list[list[float]], width: int = 24) -> str:
    values = [float(point[1]) for point in points if len(point) > 1]
    if len(values) < 2:
        return "price unavailable"
    if len(values) > width:
        values = [values[round(i * (len(values) - 1) / (width - 1))] for i in range(width)]
    low, high = min(values), max(values)
    if low == high:
        return "▄" * len(values)
    bars = "▁▂▃▄▅▆▇█"
    return "".join(bars[round((value - low) / (high - low) * 7)] for value in values)


def fetch_payload(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/halts",
        headers={"User-Agent": "halt-scanner-tui/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


class HaltCard(Static):
    def __init__(self, halt: dict[str, Any], *, resumed: bool = False) -> None:
        super().__init__()
        self.halt = halt
        self.resumed = resumed

    def render(self) -> Text | Table:
        halt = self.halt
        now = dt.datetime.now().astimezone()
        halted_at = dt.datetime.fromisoformat(halt["halt_at"]).astimezone()
        resume_at = (
            dt.datetime.fromisoformat(halt["resumption_trade_at"]).astimezone()
            if halt.get("resumption_trade_at")
            else None
        )
        if self.resumed:
            timer = f"resumed {resume_at:%H:%M:%S}" if resume_at else "resumed"
        elif resume_at:
            timer = f"RESUME IN {duration((resume_at - now).total_seconds())}"
        else:
            timer = f"HALTED FOR {duration((now - halted_at).total_seconds())}"

        trend = halt.get("trend_pct")
        trend_text = "unavailable" if trend is None else f"{trend:+.2f}% over 5m"
        threshold = (
            f"  •  threshold ${halt['threshold_price']}" if halt.get("threshold_price") else ""
        )
        trend_style = "green" if (trend or 0) >= 0 else "red"

        if self.size.width >= 90:
            table = Table.grid(expand=True, padding=(0, 2))
            table.add_column(ratio=1, min_width=10)
            table.add_column(ratio=3, min_width=24)
            table.add_column(ratio=2, min_width=20)
            table.add_column(ratio=2, min_width=18)
            table.add_column(ratio=2, min_width=24)
            table.add_row(
                Text.assemble(
                    (halt["symbol"], "bold cyan"),
                    f"\n{halt.get('market') or '—'}",
                ),
                Text.assemble(
                    (halt.get("name") or "Unknown issuer", "bold"),
                    (
                        f"\n{halt.get('reason') or halt.get('reason_code') or 'Unknown reason'}",
                        "dim",
                    ),
                ),
                Text.assemble(
                    (timer, "bold green" if self.resumed else "bold yellow"),
                    (f"\n halted {halted_at:%H:%M:%S}", "dim"),
                ),
                Text.assemble(
                    ("PRIOR TREND\n", "dim"),
                    (f"{trend_text}{threshold}", trend_style),
                ),
                Text.assemble(
                    ("30M PRICE\n", "dim"),
                    (sparkline(halt.get("trend_points") or []), trend_style),
                ),
            )
            return table

        text = Text()
        text.append(halt["symbol"], style="bold cyan")
        text.append(f"  {halt.get('market') or '—'}", style="dim")
        text.append(f"\n{halt.get('name') or 'Unknown issuer'}", style="bold")
        text.append(
            f"\n{halt.get('reason') or halt.get('reason_code') or 'Unknown reason'}",
            style="dim",
        )
        text.append(f"\n{timer}", style="bold green" if self.resumed else "bold yellow")
        text.append(f"\n halted {halted_at:%H:%M:%S}", style="dim")
        text.append(f"  •  prior trend {trend_text}{threshold}")
        text.append(f"\n{sparkline(halt.get('trend_points') or [])}", style=trend_style)
        return text


class HaltScannerTUI(App[None]):
    TITLE = "Halt Scanner"
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh_data", "Refresh")]
    CSS = """
    Screen { background: #071018; color: #e8f1f6; }
    #title { height: 3; padding: 1 2; text-style: bold; color: #45d4ff; }
    #status { height: 2; padding: 0 2; color: #8fa3b1; }
    #counts { height: 3; padding: 0 2; }
    .count { width: 1fr; border: round #203342; padding: 0 1; }
    #body { height: 1fr; }
    .section-title { height: 2; margin: 1 2 0 2; text-style: bold; }
    .cards { height: auto; padding: 0 2; }
    HaltCard { height: auto; min-height: 7; margin-bottom: 1; padding: 0 1; border: round #203342; background: #0d1923; }
    .empty { height: 3; padding: 1 2; color: #8fa3b1; border: dashed #203342; }
    """

    class PayloadLoaded(Message):
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload
            super().__init__()

    class LoadFailed(Message):
        def __init__(self, error: str) -> None:
            self.error = error
            super().__init__()

    def __init__(self, api_url: str) -> None:
        super().__init__()
        self.api_url = api_url

    def compose(self) -> ComposeResult:
        yield Static("US TRADING HALTS", id="title")
        yield Static("Connecting…", id="status")
        with Horizontal(id="counts"):
            yield Static("0 ACTIVE", classes="count", id="active-count")
            yield Static("0 RESUMED TODAY", classes="count", id="resumed-count")
        with VerticalScroll(id="body"):
            yield Static("ACTIVE HALTS", classes="section-title")
            yield Vertical(id="active", classes="cards")
            yield Static("RESUMED TODAY", classes="section-title")
            yield Vertical(id="resumed", classes="cards")
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh_data()
        self.set_interval(1, self.refresh_countdowns)
        self.set_interval(30, self.action_refresh_data)

    def refresh_countdowns(self) -> None:
        for card in self.query(HaltCard):
            card.refresh()

    def action_refresh_data(self) -> None:
        self.load_data()

    @work(exclusive=True, thread=True)
    def load_data(self) -> None:
        try:
            payload = fetch_payload(self.api_url)
        except Exception as exc:
            self.post_message(self.LoadFailed(str(exc)))
            return
        self.post_message(self.PayloadLoaded(payload))

    def on_halt_scanner_tui_load_failed(self, message: LoadFailed) -> None:
        self.query_one("#status", Static).update(f"Scanner unavailable: {message.error}")

    def on_halt_scanner_tui_payload_loaded(self, message: PayloadLoaded) -> None:
        payload = message.payload
        active = payload.get("current") or []
        resumed = payload.get("resumed") or []
        updated = payload.get("last_updated")
        status = f"Feed updated {dt.datetime.fromisoformat(updated).astimezone():%H:%M:%S}" if updated else "Feed pending"
        if payload.get("last_error"):
            status += f"  •  {payload['last_error']}"
        self.query_one("#status", Static).update(status)
        self.query_one("#active-count", Static).update(f"{len(active)} ACTIVE")
        self.query_one("#resumed-count", Static).update(f"{len(resumed)} RESUMED TODAY")
        self._replace_cards("#active", active, resumed=False)
        self._replace_cards("#resumed", resumed, resumed=True)

    def _replace_cards(
        self, selector: str, halts: list[dict[str, Any]], *, resumed: bool
    ) -> None:
        container = self.query_one(selector, Vertical)
        container.remove_children()
        if halts:
            container.mount(*(HaltCard(halt, resumed=resumed) for halt in halts))
        else:
            container.mount(
                Static(
                    "No active halts." if not resumed else "No resumptions reported today.",
                    classes="empty",
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.getenv("HALT_SCANNER_URL", "http://127.0.0.1:8010"),
        help="halt scanner base URL",
    )
    HaltScannerTUI(parser.parse_args().url).run()


if __name__ == "__main__":
    main()
