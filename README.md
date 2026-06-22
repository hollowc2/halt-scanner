# Halt Scanner

Private live list of US-listed trading halts from Nasdaq's consolidated halt
feed, with scheduled resumption countdowns and best-effort Yahoo pre-halt charts.

## Run

```bash
docker compose up -d --build
curl http://127.0.0.1:8010/healthz
```

Open `http://127.0.0.1:8010/`.

The service is bound to localhost. Halt data remains available when Yahoo chart
requests fail or are rate-limited.

## Terminal UI

With the scanner running:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tui.py
```

The TUI sorts active halts by nearest scheduled resume, rings the terminal bell
for halt/resume events, and color-codes volatility, news, and regulatory halts.

- `/` searches by symbol.
- `f` opens symbol, market, and reason filters.
- `Enter` opens history for the focused halt.
- `e` exports retained history to `halts.csv`.
- `r` refreshes and `q` quits.

Set `HALT_SCANNER_URL` or pass `--url` when the scanner is not at
`http://127.0.0.1:8010`.

## API

- `GET /api/halts` returns today's active and resumed halts.
- `GET /api/history?symbol=XYZ` returns retained symbol history.
- `GET /api/halts.csv` exports up to one year of retained halt history.

## Test

```bash
.venv/bin/python -m unittest -v
```
