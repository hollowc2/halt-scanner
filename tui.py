#!/usr/bin/env python3
"""Textual client for the halt scanner HTTP API."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from typing import Any

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Static


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


def fetch_json(url: str, path: str) -> Any:
    request = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        headers={"User-Agent": "halt-scanner-tui/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def fetch_payload(url: str) -> dict[str, Any]:
    return fetch_json(url, "/api/halts")


def halt_time(halt: dict[str, Any], key: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(halt[key]).astimezone() if halt.get(key) else None
    except (TypeError, ValueError):
        return None


def resume_sort_key(halt: dict[str, Any]) -> float:
    resume_at = halt_time(halt, "resumption_trade_at")
    return resume_at.timestamp() if resume_at else float("inf")


def halt_kind(halt: dict[str, Any]) -> str:
    code = str(halt.get("reason_code") or "").upper()
    reason = f"{halt.get('reason', '')} {halt.get('reason_code', '')}".lower()
    if code in {"H4", "H9", "H10"} or any(
        word in reason for word in ("regulatory", "compliance", "sec ", "finra")
    ):
        return "regulatory"
    if code in {"T1", "T2", "T12"} or any(
        word in reason for word in ("news", "information", "pending")
    ):
        return "news"
    if code in {"LUDP", "M", "T5", "MWC1", "MWC2", "MWC3"} or any(
        word in reason for word in ("volatility", "limit up", "limit down", "luld")
    ):
        return "volatility"
    return "other"


def history_table(history: dict[str, Any]) -> Table:
    table = Table(expand=True)
    table.add_column("Halted")
    table.add_column("Duration")
    table.add_column("Reason", ratio=2)
    table.add_column("Resumed")
    table.add_column("+5m")
    for halt in history.get("halts") or []:
        halted_at = halt_time(halt, "halt_at")
        resumed_at = halt_time(halt, "resumption_trade_at")
        post_move = halt.get("post_resume_pct")
        table.add_row(
            halted_at.strftime("%Y-%m-%d %H:%M") if halted_at else "—",
            duration(halt.get("halt_duration_seconds") or 0),
            halt.get("reason") or halt.get("reason_code") or "Unknown",
            resumed_at.strftime("%H:%M:%S") if resumed_at else "—",
            "—" if post_move is None else f"{post_move:+.2f}%",
        )
    if not table.rows:
        table.add_row("—", "—", "No retained halts", "—", "—")
    return table


class HaltCard(Static):
    can_focus = True

    def __init__(self, halt: dict[str, Any], *, resumed: bool = False) -> None:
        super().__init__(classes=f"halt-{halt_kind(halt)}")
        self.halt = halt
        self.resumed = resumed

    def render(self) -> Text | Table:
        halt = self.halt
        now = dt.datetime.now().astimezone()
        halted_at = halt_time(halt, "halt_at") or now
        resume_at = halt_time(halt, "resumption_trade_at")
        delayed_at = resume_at or halt_time(halt, "resumption_quote_at")
        delayed = not self.resumed and (
            bool(halt.get("is_delayed"))
            or (delayed_at is not None and delayed_at < now)
        )
        if self.resumed:
            timer = f"resumed {resume_at:%H:%M:%S}" if resume_at else "resumed"
        elif delayed and delayed_at:
            timer = f"RESUME DELAYED {duration((now - delayed_at).total_seconds())}"
        elif delayed:
            timer = "RESUME DELAYED"
        elif resume_at:
            timer = f"RESUME IN {duration((resume_at - now).total_seconds())}"
        else:
            timer = f"HALTED FOR {duration((now - halted_at).total_seconds())}"
        timer_style = "bold red" if delayed else ("bold green" if self.resumed else "bold yellow")

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
                    (timer, timer_style),
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
        text.append(f"\n{timer}", style=timer_style)
        text.append(f"\n halted {halted_at:%H:%M:%S}", style="dim")
        text.append(f"  •  prior trend {trend_text}{threshold}")
        text.append(f"\n{sparkline(halt.get('trend_points') or [])}", style=trend_style)
        return text


class DetailsScreen(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def __init__(self, symbol: str, history: Any) -> None:
        super().__init__()
        self.symbol = symbol
        self.history = history

    def compose(self) -> ComposeResult:
        with Vertical(id="details-dialog"):
            yield Static(f"{self.symbol} HALT HISTORY", id="details-title")
            yield Static(history_table(self.history), id="details-content")
            yield Static("Esc close", id="details-help")

    def action_dismiss(self) -> None:
        self.dismiss()


class HaltScannerTUI(App[None]):
    TITLE = "Halt Scanner"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_data", "Refresh"),
        ("/", "search", "Search"),
        ("f", "filters", "Filters"),
        ("enter", "details", "Details"),
        ("e", "export_csv", "Export CSV"),
    ]
    CSS = """
    Screen { background: #071018; color: #e8f1f6; }
    #title { height: 3; padding: 1 2; text-style: bold; color: #45d4ff; }
    #status { height: 2; padding: 0 2; color: #8fa3b1; }
    #counts { height: 3; padding: 0 2; }
    .count { width: 1fr; border: round #203342; padding: 0 1; }
    #filters { height: 3; padding: 0 2; }
    #filters Input { width: 1fr; margin-right: 1; }
    .hidden { display: none; }
    #body { height: 1fr; }
    .section-title { height: 2; margin: 1 2 0 2; text-style: bold; }
    .cards { height: auto; padding: 0 2; }
    HaltCard { height: auto; min-height: 7; margin-bottom: 1; padding: 0 1; border: round #203342; background: #0d1923; }
    HaltCard:focus { background: #132837; border: heavy #45d4ff; }
    HaltCard.halt-volatility { border-left: heavy yellow; }
    HaltCard.halt-news { border-left: heavy magenta; }
    HaltCard.halt-regulatory { border-left: heavy red; }
    HaltCard.halt-other { border-left: heavy cyan; }
    .empty { height: 3; padding: 1 2; color: #8fa3b1; border: dashed #203342; }
    DetailsScreen { align: center middle; background: #0008; }
    #details-dialog { width: 80%; height: 80%; padding: 1 2; border: round #45d4ff; background: #0d1923; }
    #details-title { height: 2; text-style: bold; color: #45d4ff; }
    #details-content { height: 1fr; overflow-y: auto; }
    #details-help { height: 1; color: #8fa3b1; }
    """

    class PayloadLoaded(Message):
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload
            super().__init__()

    class LoadFailed(Message):
        def __init__(self, error: str) -> None:
            self.error = error
            super().__init__()

    class HistoryLoaded(Message):
        def __init__(self, symbol: str, history: Any) -> None:
            self.symbol = symbol
            self.history = history
            super().__init__()

    class HistoryFailed(Message):
        def __init__(self, symbol: str, error: str) -> None:
            self.symbol = symbol
            self.error = error
            super().__init__()

    class Exported(Message):
        def __init__(self, path: str) -> None:
            self.path = path
            super().__init__()

    class ExportFailed(Message):
        def __init__(self, error: str) -> None:
            self.error = error
            super().__init__()

    def __init__(self, api_url: str) -> None:
        super().__init__()
        self.api_url = api_url
        self.payload: dict[str, Any] = {}
        self.seen_active: set[str] | None = None
        self.seen_schedules: set[str] = set()
        self.seen_resumptions: set[str] = set()
        self.filters = {"symbol": "", "market": "", "reason": ""}

    def compose(self) -> ComposeResult:
        yield Static("US TRADING HALTS", id="title")
        yield Static("Connecting…", id="status")
        with Horizontal(id="counts"):
            yield Static("0 ACTIVE", classes="count", id="active-count")
            yield Static("0 RESUMED TODAY", classes="count", id="resumed-count")
        with Horizontal(id="filters", classes="hidden"):
            yield Input(placeholder="Symbol", id="symbol-filter")
            yield Input(placeholder="Market", id="market-filter")
            yield Input(placeholder="Reason", id="reason-filter")
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

    def action_search(self) -> None:
        self.query_one("#filters").remove_class("hidden")
        self.query_one("#symbol-filter", Input).focus()

    def action_filters(self) -> None:
        filters = self.query_one("#filters")
        filters.toggle_class("hidden")
        if not filters.has_class("hidden"):
            self.query_one("#symbol-filter", Input).focus()

    def action_details(self) -> None:
        card = self.focused
        if isinstance(card, HaltCard):
            symbol = str(card.halt.get("symbol") or "")
            self.notify(f"Loading {symbol} history…", timeout=1)
            self.load_history(symbol)

    def action_export_csv(self) -> None:
        self.export_csv()

    @work(exclusive=True, thread=True)
    def load_data(self) -> None:
        try:
            payload = fetch_payload(self.api_url)
        except Exception as exc:
            self.post_message(self.LoadFailed(str(exc)))
            return
        self.post_message(self.PayloadLoaded(payload))

    @work(thread=True)
    def load_history(self, symbol: str) -> None:
        try:
            path = f"/api/history?symbol={urllib.parse.quote(symbol)}"
            self.post_message(self.HistoryLoaded(symbol, fetch_json(self.api_url, path)))
        except Exception as exc:
            self.post_message(self.HistoryFailed(symbol, str(exc)))

    @work(exclusive=True, thread=True, group="csv")
    def export_csv(self) -> None:
        try:
            request = urllib.request.Request(
                f"{self.api_url.rstrip('/')}/api/halts.csv",
                headers={"User-Agent": "halt-scanner-tui/1.0", "Accept": "text/csv"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                data = response.read()
            path = os.path.abspath("halts.csv")
            with open(path, "wb") as output:
                output.write(data)
            self.post_message(self.Exported(path))
        except Exception as exc:
            self.post_message(self.ExportFailed(str(exc)))

    def on_halt_scanner_tui_load_failed(self, message: LoadFailed) -> None:
        self.query_one("#status", Static).update(f"Scanner unavailable: {message.error}")

    def on_halt_scanner_tui_history_loaded(self, message: HistoryLoaded) -> None:
        self.push_screen(DetailsScreen(message.symbol, message.history))

    def on_halt_scanner_tui_history_failed(self, message: HistoryFailed) -> None:
        self.notify(f"{message.symbol} history unavailable: {message.error}", severity="warning")

    def on_halt_scanner_tui_exported(self, message: Exported) -> None:
        self.notify(f"CSV saved to {message.path}")

    def on_halt_scanner_tui_export_failed(self, message: ExportFailed) -> None:
        self.notify(f"CSV export unavailable: {message.error}", severity="warning")

    def on_input_changed(self, event: Input.Changed) -> None:
        field = {
            "symbol-filter": "symbol",
            "market-filter": "market",
            "reason-filter": "reason",
        }.get(event.input.id or "")
        if field:
            self.filters[field] = event.value.strip().lower()
            self.render_payload()

    def on_halt_scanner_tui_payload_loaded(self, message: PayloadLoaded) -> None:
        self.payload = message.payload
        active = self.payload.get("current") or []
        resumed = self.payload.get("resumed") or []
        active_events = {
            f"{halt.get('symbol')}:{halt.get('halt_at')}" for halt in active
        }
        resume_events = {
            f"{halt.get('symbol')}:{halt.get('resumption_trade_at')}" for halt in resumed
        }
        schedule_events = {
            f"{halt.get('symbol')}:{halt.get('resumption_trade_at')}"
            for halt in active
            if halt.get("resumption_trade_at")
        }
        if self.seen_active is not None:
            for event in active_events - self.seen_active:
                self.bell()
                self.notify(f"New halt: {event.split(':', 1)[0]}", severity="warning")
            for event in schedule_events - self.seen_schedules:
                symbol, resume_at = event.split(":", 1)
                self.bell()
                self.notify(f"{symbol} scheduled to resume {resume_at[11:19]}")
            for event in resume_events - self.seen_resumptions:
                self.bell()
                self.notify(f"Resumed: {event.split(':', 1)[0]}")
        self.seen_active = active_events
        self.seen_schedules = schedule_events
        self.seen_resumptions = resume_events
        self.render_payload()

    def render_payload(self) -> None:
        payload = self.payload
        active = self._filtered(payload.get("current") or [])
        resumed = self._filtered(payload.get("resumed") or [])
        active.sort(key=resume_sort_key)
        updated = payload.get("last_updated")
        status = f"Feed updated {dt.datetime.fromisoformat(updated).astimezone():%H:%M:%S}" if updated else "Feed pending"
        if payload.get("last_error"):
            status += f"  •  {payload['last_error']}"
        self.query_one("#status", Static).update(status)
        self.query_one("#active-count", Static).update(f"{len(active)} ACTIVE")
        self.query_one("#resumed-count", Static).update(f"{len(resumed)} RESUMED TODAY")
        self._replace_cards("#active", active, resumed=False)
        self._replace_cards("#resumed", resumed, resumed=True)

    def _filtered(self, halts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def matches(halt: dict[str, Any]) -> bool:
            reason = f"{halt.get('reason', '')} {halt.get('reason_code', '')}".lower()
            return (
                self.filters["symbol"] in str(halt.get("symbol", "")).lower()
                and self.filters["market"] in str(halt.get("market", "")).lower()
                and self.filters["reason"] in reason
            )

        return [halt for halt in halts if matches(halt)]

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
