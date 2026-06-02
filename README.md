# Health Connect Webhook Receiver

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/H2H1ZAPL)

Self-hosted receiver for Android Health Connect webhook exports. Point an Android exporter such as HC Webhook / Health Connect to Webhook at this service and it stores:

- raw JSON payloads in SQLite
- normalized sleep sessions
- normalized vitals: heart rate, HRV RMSSD, oxygen saturation, resting heart rate, respiratory rate

Designed for local-first health tracking behind Cloudflare Tunnel, Tailscale, or LAN.

> Not affiliated with Google Health Connect, Samsung Health, or HC Webhook.

## Why

Samsung Health and Android Health Connect are phone-local permission systems, not WHOOP-style cloud APIs. If you want to ingest Galaxy Watch / Galaxy Ring / Android wearable data into your own database, you need a phone-side exporter and a receiver.

This project is the receiver.

```text
Wearable / Samsung Health / Health Connect
→ Android exporter app
→ HTTPS/LAN webhook
→ hcwebhook-receiver
→ SQLite
```

## Features

- Single-file stdlib Python HTTP server, packaged as a CLI
- Token auth via `X-Webhook-Token` header or `?token=` query parameter
- Raw payload preservation for future re-parsing
- SQLite storage with idempotent raw-event dedupe by SHA256
- Convenience normalized tables for sleep and vitals
- Works behind Cloudflare Tunnel, Tailscale, reverse proxies, or plain LAN
- macOS launchd, Linux systemd, and Docker examples

## Install

From source:

```bash
git clone https://github.com/wysie/health-connect-webhook-receiver.git
cd health-connect-webhook-receiver
python3 -m pip install .
```

Or run without installing:

```bash
python3 -m hcwebhook_receiver --host 0.0.0.0 --port 8787
```

## Configure

Set a long random token:

```bash
mkdir -p ~/.local/share/hcwebhook-receiver
python3 - <<'PY' > ~/.local/share/hcwebhook-receiver/webhook_token
import secrets
print(secrets.token_urlsafe(48))
PY
chmod 600 ~/.local/share/hcwebhook-receiver/webhook_token
```

Default paths:

- token: `~/.local/share/hcwebhook-receiver/webhook_token`
- database: `~/.local/share/hcwebhook-receiver/health_connect.sqlite`

Environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `HCWEBHOOK_HOST` | `0.0.0.0` | Bind host |
| `HCWEBHOOK_PORT` | `8787` | Bind port |
| `HCWEBHOOK_DB` | `~/.local/share/hcwebhook-receiver/health_connect.sqlite` | SQLite path |
| `HCWEBHOOK_TOKEN` | unset | Token value directly |
| `HCWEBHOOK_TOKEN_FILE` | `~/.local/share/hcwebhook-receiver/webhook_token` | Token file path |
| `HCWEBHOOK_MAX_BODY_MB` | `25` | Max JSON request size |

## Run

```bash
hcwebhook-receiver --host 0.0.0.0 --port 8787
```

Health check:

```bash
curl http://127.0.0.1:8787/health
```

Expected:

```json
{"ok": true, "service": "health-connect-receiver", "time": "..."}
```

## Send data

Endpoint pattern:

```text
POST /health-connect/<person>
```

Preferred auth with header:

```bash
TOKEN=$(cat ~/.local/share/hcwebhook-receiver/webhook_token)
curl -X POST http://127.0.0.1:8787/health-connect/alice \
  -H "X-Webhook-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d @examples/sample-payload.json
```

Query-token auth also works:

```bash
curl -X POST "http://127.0.0.1:8787/health-connect/alice?token=$TOKEN" \
  -H 'Content-Type: application/json' \
  -d @examples/sample-payload.json
```

## Android exporter setup

With HC Webhook / Health Connect to Webhook:

1. Enable Samsung Health → Health Connect sync on the phone.
2. Grant the exporter app Health Connect permissions for the data you want.
3. Add webhook URL:

```text
https://health.example.com/health-connect/alice
```

4. Add header:

```text
X-Webhook-Token: <your-long-token>
```

5. Test with Manual Sync → Past 1 Day.
6. Backfill with Past 30 Days / Custom selection.

If your exporter cannot send custom headers, use:

```text
https://health.example.com/health-connect/alice?token=<your-long-token>
```

## Cloudflare Tunnel example

If `cloudflared` runs on another LAN machine, point the service to the receiver's LAN IP:

```yaml
ingress:
  - hostname: health.example.com
    service: http://192.168.1.160:8787
  - service: http_status:404
```

Avoid browser-based Cloudflare Access on this endpoint unless your exporter supports service-token headers.

## SQLite schema

Tables:

- `raw_events`: raw JSON payloads, SHA256 deduped by person
- `sync_log`: accepted/duplicate events and parser counts
- `sleep_sessions`: normalized convenience index for sleep sessions
- `vitals`: normalized convenience index for vitals

Raw payloads are the source of truth. Normalized tables are intentionally conservative and can be rebuilt in future versions.

Quick inspection:

```bash
sqlite3 ~/.local/share/hcwebhook-receiver/health_connect.sqlite \
  "select metric, count(*), min(time), max(time) from vitals group by metric;"

sqlite3 ~/.local/share/hcwebhook-receiver/health_connect.sqlite \
  "select count(*), min(session_start), max(session_end), avg(duration_seconds)/3600 from sleep_sessions;"
```

## Samsung Health HRV caveat

Health Connect and this receiver support HRV RMSSD records. However, Samsung Health / Galaxy Watch often does not write HRV, resting heart rate, or respiratory rate to Health Connect even when Samsung Health displays related metrics internally. If HRV does not arrive, check whether Health Connect itself shows recent HRV entries. The receiver cannot export data that the phone never sends.

## Security

Health data is sensitive.

- Always require a long random token.
- Prefer HTTPS via Cloudflare Tunnel/Tailscale/reverse proxy when posting over the internet.
- Do not expose an unauthenticated endpoint.
- Do not commit real tokens, domains, or payloads.
- Consider firewalling the port if you only use a tunnel.

## Development

```bash
python3 -m pip install -e . pytest
pytest -q
```

## License

MIT
