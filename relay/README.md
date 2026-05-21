# Bitaxe Baller — Remote-access relay

Self-hosted WebSocket relay that lets Pro users reach their Bitaxe Baller
dashboard from outside their LAN. The desktop app opens a persistent
outbound WebSocket to this service; remote browsers (and later mobile apps)
connect to the same service and get their requests routed to the right
app socket.

The relay itself is **dumb** — it doesn't know about devices, tuning, or
pool config. It validates license keys against the self-hosted license
server at `bitaxeballer.com/api/license` and forwards opaque JSON
messages by license key. All product logic stays in the app.

## Architecture at a glance

```
  app on user's LAN  ──outbound WSS──►  relay.bitaxeballer.com  ◄──WSS──  remote browser / mobile
       (license-key auth)                  (in-memory router)              (session-token auth)
```

See the scoping doc in Notion for the full design:
https://www.notion.so/3629cef8928f8192bd6bfc2d81ddaaa7

## Run locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Dev mode: bypass license validation for one fixed key.
export RELAY_DEV_LICENSE_KEY=dev-license-key
export RELAY_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")

uvicorn main:app --host 127.0.0.1 --port 8787 --reload
```

The relay listens on `:8787` by default. Endpoints:

- `GET  /health` — liveness probe
- `POST /login` — exchange a license key for a session token
- `WS   /ws/app` — app-side socket (header `Authorization: Bearer <license_key>`)
- `WS   /ws/client` — browser/mobile socket (header `Authorization: Bearer <session_token>`)

## Smoke test

```bash
python tests/smoke.py
```

Spins up a mock app socket + mock client and verifies the round-trip
through a running relay (defaults to `ws://127.0.0.1:8787`).

## Deploy notes

For launch the relay rides on the existing `bitaxe-baller-site` VPS
alongside the marketing site and (eventually) the licensing API. Put it
behind nginx/Caddy with a `relay.bitaxeballer.com` subdomain and a
Let's Encrypt cert. Stateless; restart is cheap.

Required env vars in production:

- `RELAY_SECRET` — 32+ byte random string. Signs session tokens. **Rotate this and every active session is invalidated.**
- `RELAY_HOST` (default `0.0.0.0`)
- `RELAY_PORT` (default `8787`)

Optional:

- `RELAY_DEV_LICENSE_KEY` — bypass license validation for one fixed key. **Never set in production.**
- `RELAY_LICENSE_API_BASE` (default `https://bitaxeballer.com/api/license`) — base URL of the license server. Override for staging.
- `RELAY_SESSION_TTL_S` (default `86400` — 24h)
- `RELAY_IDLE_DISCONNECT_S` (default `3600` — 1h of no client traffic closes the app socket)
