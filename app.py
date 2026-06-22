#!/usr/bin/env python3
"""Live US trading-halt scanner using Nasdaq's halt feed."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

NASDAQ_FEED = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
YAHOO_CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
EASTERN = ZoneInfo("America/New_York")
USER_AGENT = "halt-scanner/1.0"
NS = {"ndaq": "http://www.nasdaqtrader.com/"}

REASONS = {
    "T1": "News pending",
    "T2": "News released",
    "T5": "Single-stock trading pause",
    "T6": "Extraordinary market activity",
    "T8": "Exchange-requested halt",
    "T12": "Additional information requested",
    "H4": "Non-compliance",
    "H9": "Regulatory concern",
    "H10": "SEC trading suspension",
    "LUDP": "Limit Up-Limit Down pause",
    "M": "Volatility trading pause",
    "MWC1": "Market-wide circuit breaker level 1",
    "MWC2": "Market-wide circuit breaker level 2",
    "MWC3": "Market-wide circuit breaker level 3",
    "IPO1": "IPO release pending",
    "IPO2": "IPO release scheduled",
}

log = logging.getLogger("halt_scanner")


@dataclass(frozen=True)
class Halt:
    id: str
    symbol: str
    name: str
    market: str
    reason_code: str
    threshold_price: str
    halt_at: str
    resumption_quote_at: str | None
    resumption_trade_at: str | None


def _text(item: ET.Element, name: str) -> str:
    node = item.find(f"ndaq:{name}", NS)
    return (node.text or "").strip() if node is not None else ""


def _et_datetime(date_text: str, time_text: str) -> dt.datetime | None:
    if not date_text or not time_text:
        return None
    raw_time = time_text.split(".")[0]
    try:
        return dt.datetime.strptime(
            f"{date_text} {raw_time}", "%m/%d/%Y %H:%M:%S"
        ).replace(tzinfo=EASTERN)
    except ValueError:
        return None


def parse_nasdaq_feed(xml: bytes) -> list[Halt]:
    root = ET.fromstring(xml.decode("utf-8-sig"))
    halts: list[Halt] = []
    for item in root.findall("./channel/item"):
        symbol = _text(item, "IssueSymbol")
        halt_at = _et_datetime(_text(item, "HaltDate"), _text(item, "HaltTime"))
        if not symbol or halt_at is None:
            continue
        resumption_date = _text(item, "ResumptionDate")
        quote_at = _et_datetime(resumption_date, _text(item, "ResumptionQuoteTime"))
        trade_at = _et_datetime(resumption_date, _text(item, "ResumptionTradeTime"))
        halt_iso = halt_at.isoformat()
        halts.append(
            Halt(
                id=f"{symbol}:{halt_iso}",
                symbol=symbol,
                name=_text(item, "IssueName"),
                market=_text(item, "Market"),
                reason_code=_text(item, "ReasonCode"),
                threshold_price=_text(item, "PauseThresholdPrice"),
                halt_at=halt_iso,
                resumption_quote_at=quote_at.isoformat() if quote_at else None,
                resumption_trade_at=trade_at.isoformat() if trade_at else None,
            )
        )
    return halts


def parse_yahoo_chart(payload: bytes, halt_at: dt.datetime) -> tuple[float | None, list[list[float]]]:
    data = json.loads(payload)
    result = ((data.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None, []
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0])
    closes = quote.get("close") or []
    cutoff = int(halt_at.timestamp())
    start = cutoff - 30 * 60
    points = [
        [int(ts), round(float(close), 4)]
        for ts, close in zip(timestamps, closes, strict=False)
        if close is not None and start <= int(ts) <= cutoff
    ]
    if len(points) < 2:
        return None, points
    target = cutoff - 5 * 60
    baseline = next((point for point in reversed(points) if point[0] <= target), points[0])
    latest = points[-1]
    if baseline[1] <= 0 or baseline[0] == latest[0]:
        return None, points
    change = (latest[1] - baseline[1]) / baseline[1] * 100
    return round(change, 2), points


def fetch(url: str, *, timeout: float = 15) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, text/xml"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_yahoo_chart(symbol: str, halt_at: dt.datetime) -> tuple[float | None, list[list[float]]]:
    yahoo_symbol = symbol.replace(".", "-")
    query = urllib.parse.urlencode(
        {
            "period1": int(halt_at.timestamp()) - 3600,
            "period2": int(halt_at.timestamp()) + 300,
            "interval": "1m",
            "includePrePost": "true",
            "events": "div,splits",
        }
    )
    payload = fetch(f"{YAHOO_CHART.format(symbol=urllib.parse.quote(yahoo_symbol))}?{query}")
    return parse_yahoo_chart(payload, halt_at)


class Store:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS halts (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    threshold_price TEXT NOT NULL,
                    halt_at TEXT NOT NULL,
                    resumption_quote_at TEXT,
                    resumption_trade_at TEXT,
                    trend_pct REAL,
                    trend_points TEXT,
                    trend_checked_at TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """
            )

    def upsert(self, halts: list[Halt], seen_at: dt.datetime) -> None:
        now = seen_at.isoformat()
        with self.connect() as db:
            for halt in halts:
                db.execute(
                    """
                    INSERT INTO halts (
                        id, symbol, name, market, reason_code, threshold_price,
                        halt_at, resumption_quote_at, resumption_trade_at,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        market = excluded.market,
                        reason_code = excluded.reason_code,
                        threshold_price = excluded.threshold_price,
                        resumption_quote_at = excluded.resumption_quote_at,
                        resumption_trade_at = excluded.resumption_trade_at,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        halt.id,
                        halt.symbol,
                        halt.name,
                        halt.market,
                        halt.reason_code,
                        halt.threshold_price,
                        halt.halt_at,
                        halt.resumption_quote_at,
                        halt.resumption_trade_at,
                        now,
                        now,
                    ),
                )

    def prune_before(self, day: dt.date) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM halts WHERE substr(halt_at, 1, 10) < ?", (day.isoformat(),))

    def trend_candidates(self, day: dt.date, *, limit: int = 6) -> list[sqlite3.Row]:
        retry_before = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat()
        with self.connect() as db:
            return db.execute(
                """
                SELECT id, symbol, halt_at
                FROM halts
                WHERE substr(halt_at, 1, 10) = ?
                  AND trend_pct IS NULL
                  AND (trend_checked_at IS NULL OR trend_checked_at < ?)
                ORDER BY
                  CASE WHEN resumption_trade_at IS NULL THEN 0 ELSE 1 END,
                  halt_at DESC
                LIMIT ?
                """,
                (day.isoformat(), retry_before, limit),
            ).fetchall()

    def save_trend(
        self,
        halt_id: str,
        trend_pct: float | None,
        points: list[list[float]],
        checked_at: dt.datetime,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE halts
                SET trend_pct = ?, trend_points = ?, trend_checked_at = ?
                WHERE id = ?
                """,
                (trend_pct, json.dumps(points), checked_at.isoformat(), halt_id),
            )

    def rows_for_day(self, day: dt.date) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                """
                SELECT *
                FROM halts
                WHERE substr(halt_at, 1, 10) = ?
                ORDER BY halt_at DESC
                """,
                (day.isoformat(),),
            ).fetchall()


class Scanner:
    def __init__(self, store: Store, *, poll_seconds: int = 60) -> None:
        self.store = store
        self.poll_seconds = poll_seconds
        self.last_updated: str | None = None
        self.last_error: str | None = None
        self.stop_event = threading.Event()

    def poll_once(self) -> None:
        now = dt.datetime.now(EASTERN)
        try:
            halts = parse_nasdaq_feed(fetch(NASDAQ_FEED))
            self.store.prune_before(now.date())
            self.store.upsert(halts, now)
            self.last_updated = now.isoformat()
            self.last_error = None
            log.info("loaded %d halt records", len(halts))
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Nasdaq feed failed: %s", exc)
            return

        for row in self.store.trend_candidates(now.date()):
            try:
                halt_at = dt.datetime.fromisoformat(row["halt_at"])
                trend_pct, points = fetch_yahoo_chart(row["symbol"], halt_at)
                self.store.save_trend(row["id"], trend_pct, points, dt.datetime.now(dt.timezone.utc))
            except Exception as exc:
                self.store.save_trend(row["id"], None, [], dt.datetime.now(dt.timezone.utc))
                log.info("Yahoo chart unavailable for %s: %s", row["symbol"], exc)
            time.sleep(0.25)

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.poll_once()
            self.stop_event.wait(self.poll_seconds)

    def payload(self) -> dict[str, Any]:
        now = dt.datetime.now(EASTERN)
        current: list[dict[str, Any]] = []
        resumed: list[dict[str, Any]] = []
        for row in self.store.rows_for_day(now.date()):
            item = dict(row)
            item["trend_points"] = json.loads(item["trend_points"] or "[]")
            item["reason"] = REASONS.get(item["reason_code"], item["reason_code"] or "Unknown")
            item.pop("trend_checked_at", None)
            item.pop("first_seen_at", None)
            item.pop("last_seen_at", None)
            trade_at = (
                dt.datetime.fromisoformat(item["resumption_trade_at"])
                if item["resumption_trade_at"]
                else None
            )
            (resumed if trade_at and trade_at <= now else current).append(item)
        return {
            "generated_at": now.isoformat(),
            "last_updated": self.last_updated,
            "last_error": self.last_error,
            "current": current,
            "resumed": resumed,
        }


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>US Trading Halts</title>
<style>
:root{color-scheme:dark;--bg:#071018;--panel:#0d1923;--line:#203342;--text:#e8f1f6;--muted:#8fa3b1;--cyan:#45d4ff;--green:#40d98b;--red:#ff6577;--amber:#ffc857}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#102637 0,#071018 42%);color:var(--text);font:14px/1.45 system-ui,sans-serif}
main{max-width:1200px;margin:auto;padding:28px 18px 60px}header{display:flex;gap:20px;justify-content:space-between;align-items:end;margin-bottom:22px}
h1{font-size:clamp(28px,5vw,48px);line-height:1;margin:0;letter-spacing:-.04em}h2{font-size:18px;margin:30px 0 10px}
.sub,.muted{color:var(--muted)}.status{text-align:right}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px}
.summary{display:flex;gap:10px;margin:14px 0 24px}.pill{border:1px solid var(--line);background:#0b1720;padding:8px 12px;border-radius:999px}
.grid{display:grid;gap:10px}.card{display:grid;grid-template-columns:minmax(90px,.65fr) minmax(190px,1.7fr) minmax(150px,1fr) minmax(150px,1fr) 180px;gap:16px;align-items:center;background:linear-gradient(135deg,#0e1c27,#0b151e);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.symbol{font-size:21px;font-weight:800;color:var(--cyan)}.name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tag{font-size:11px;border:1px solid var(--line);border-radius:999px;padding:2px 7px;color:var(--muted)}
.timer{font-variant-numeric:tabular-nums;font-size:17px;font-weight:700;color:var(--amber)}.up{color:var(--green)}.down{color:var(--red)}svg{width:180px;height:52px;overflow:visible}.empty{padding:28px;border:1px dashed var(--line);border-radius:12px;color:var(--muted);text-align:center}
.error{display:none;color:#ffd1d7;background:#35151c;border:1px solid #69303b;padding:10px;border-radius:8px;margin-bottom:12px}
@media(max-width:800px){header{align-items:start;flex-direction:column}.status{text-align:left}.card{grid-template-columns:1fr 1fr}.chart{grid-column:1/-1}svg{width:100%}}
</style>
</head>
<body><main>
<header><div><h1>US Trading Halts</h1><div class="sub">Official Nasdaq consolidated feed · scheduled times are not guarantees</div></div><div class="status"><span class="dot"></span><span id="updated">Loading…</span></div></header>
<div id="error" class="error"></div>
<div class="summary"><div class="pill"><b id="live-count">0</b> halted</div><div class="pill"><b id="resumed-count">0</b> resumed today</div></div>
<h2>Currently halted</h2><div id="current" class="grid"></div>
<h2>Resumed today</h2><div id="resumed" class="grid"></div>
</main>
<script>
let data={current:[],resumed:[]};const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const clock=iso=>new Date(iso).toLocaleTimeString([],{hour:"numeric",minute:"2-digit",second:"2-digit"});
const duration=s=>{s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?`${h}h ${m}m ${x}s`:`${m}m ${x}s`};
function timer(h){
 const now=Date.now(),start=new Date(h.halt_at).getTime(),resume=h.resumption_trade_at?new Date(h.resumption_trade_at).getTime():null;
 if(resume&&resume>now)return `Scheduled in ${duration((resume-now)/1000)}`;
 if(resume)return `Scheduled ${clock(h.resumption_trade_at)}`;
 return `Awaiting time · ${duration((now-start)/1000)}`;
}
function spark(points,trend){
 if(!points||points.length<2)return `<span class="muted">Price unavailable</span>`;
 const values=points.map(p=>p[1]),lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||1;
 const coords=values.map((v,i)=>`${i/(values.length-1)*180},${48-(v-lo)/span*44}`).join(" ");
 const cls=(trend??0)>=0?"up":"down";return `<svg viewBox="0 0 180 52" role="img" aria-label="30 minute pre-halt price chart"><polyline class="${cls}" points="${coords}" fill="none" stroke="currentColor" stroke-width="2.5" vector-effect="non-scaling-stroke"/></svg>`;
}
function card(h){
 const trend=h.trend_pct==null?`<span class="muted">5m move unavailable</span>`:`<b class="${h.trend_pct>=0?"up":"down"}">${h.trend_pct>=0?"+":""}${h.trend_pct.toFixed(2)}%</b> over 5m`;
 return `<article class="card"><div><div class="symbol">${esc(h.symbol)}</div><span class="tag">${esc(h.market)}</span></div><div><div class="name" title="${esc(h.name)}">${esc(h.name)}</div><div class="muted">${esc(h.reason)} · halted ${clock(h.halt_at)}</div></div><div class="timer">${timer(h)}</div><div>${trend}${h.threshold_price?`<div class="muted">Threshold $${esc(h.threshold_price)}</div>`:""}</div><div class="chart">${spark(h.trend_points,h.trend_pct)}</div></article>`;
}
function render(){
 $("live-count").textContent=data.current.length;$("resumed-count").textContent=data.resumed.length;
 $("current").innerHTML=data.current.length?data.current.map(card).join(""):`<div class="empty">No current halts reported.</div>`;
 $("resumed").innerHTML=data.resumed.length?data.resumed.map(card).join(""):`<div class="empty">No resumptions reported today.</div>`;
}
async function load(){
 try{const r=await fetch("/api/halts",{cache:"no-store"});if(!r.ok)throw Error(`HTTP ${r.status}`);data=await r.json();
 $("updated").textContent=data.last_updated?`Feed ${clock(data.last_updated)}`:"Feed pending";
 $("error").style.display=data.last_error?"block":"none";$("error").textContent=data.last_error||"";render()}
 catch(e){$("error").style.display="block";$("error").textContent=`Scanner unavailable: ${e.message}`}
}
load();setInterval(load,60000);setInterval(render,1000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    scanner: Scanner

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, "text/html; charset=utf-8", HTML.encode())
        elif self.path == "/api/halts":
            self._send(
                200,
                "application/json",
                json.dumps(self.scanner.payload(), separators=(",", ":")).encode(),
            )
        elif self.path == "/healthz":
            status = 200 if self.scanner.last_updated else 503
            self._send(status, "text/plain", b"ok\n" if status == 200 else b"starting\n")
        else:
            self._send(404, "text/plain", b"not found\n")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8010"))
    store = Store(os.getenv("DB_PATH", "data/halts.db"))
    scanner = Scanner(store, poll_seconds=int(os.getenv("POLL_SECONDS", "60")))
    Handler.scanner = scanner
    worker = threading.Thread(target=scanner.run, name="halt-poller", daemon=True)
    worker.start()
    server = ThreadingHTTPServer((host, port), Handler)

    def stop(*_: object) -> None:
        scanner.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("serving http://%s:%d", host, port)
    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()

