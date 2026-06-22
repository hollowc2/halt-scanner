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

Press `r` to refresh immediately or `q` to quit. Set `HALT_SCANNER_URL` or pass
`--url` when the scanner is not at `http://127.0.0.1:8010`.

## Test

```bash
.venv/bin/python -m unittest -v
```
