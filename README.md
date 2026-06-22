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

## Test

```bash
python -m unittest -v
```
