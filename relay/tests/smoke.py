"""End-to-end smoke test for a running relay.

Run a relay in dev mode in one shell::

    cd relay
    export RELAY_DEV_LICENSE_KEY=dev-license-key
    export RELAY_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
    uvicorn main:app --host 127.0.0.1 --port 8787

Then run this in another::

    python tests/smoke.py

Exit code 0 = round-trip works.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional

import requests
import websockets


RELAY_HTTP = os.environ.get("RELAY_HTTP", "http://127.0.0.1:8787")
RELAY_WS = os.environ.get("RELAY_WS", "ws://127.0.0.1:8787")
LICENSE_KEY = os.environ.get("RELAY_DEV_LICENSE_KEY", "dev-license-key")


async def mock_app(ready: asyncio.Event, stop: asyncio.Event) -> None:
    """Connects as the app side, answers a single request, then waits for stop."""
    url = f"{RELAY_WS}/ws/app?key={LICENSE_KEY}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "hello", "version": "smoke-test"}))
        ready.set()
        try:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                if msg.get("type") == "request":
                    response = {
                        "type": "response",
                        "id": msg["id"],
                        "status": 200,
                        "body": {"echoed_path": msg["path"], "echoed_body": msg.get("body")},
                    }
                    await ws.send(json.dumps(response))
        except websockets.ConnectionClosed:
            pass


async def run_client_request() -> dict:
    r = requests.post(f"{RELAY_HTTP}/login", data={"license_key": LICENSE_KEY}, timeout=8)
    r.raise_for_status()
    token = r.json()["token"]

    url = f"{RELAY_WS}/ws/client?token={token}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "type": "request",
            "method": "GET",
            "path": "/api/devices",
            "body": None,
        }))
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        return json.loads(raw)


async def main() -> int:
    health = requests.get(f"{RELAY_HTTP}/health", timeout=4)
    health.raise_for_status()
    assert health.json()["ok"] is True, "health endpoint not ok"

    ready = asyncio.Event()
    stop = asyncio.Event()
    app_task = asyncio.create_task(mock_app(ready, stop))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        # Give the relay a beat to register the connection.
        await asyncio.sleep(0.2)

        response = await run_client_request()
        assert response.get("type") == "response", f"bad shape: {response}"
        assert response.get("status") == 200, f"non-200: {response}"
        body = response.get("body") or {}
        assert body.get("echoed_path") == "/api/devices", f"echo mismatch: {body}"
        print("SMOKE OK:", response)
        return 0
    finally:
        stop.set()
        app_task.cancel()
        try:
            await app_task
        except (asyncio.CancelledError, Exception):
            pass


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except AssertionError as e:
        print(f"SMOKE FAIL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"SMOKE ERROR: {e.__class__.__name__}: {e}", file=sys.stderr)
        sys.exit(2)
