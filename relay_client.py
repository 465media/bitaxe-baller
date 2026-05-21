"""Outbound WebSocket connector to the bitaxe-baller relay.

Pro feature: lets users reach their local dashboard from outside their
LAN. We never accept inbound connections — this opens an outbound WSS
to relay.bitaxeballer.com, listens for `{type:"request", method, path,
body}` envelopes, dispatches each as a loopback HTTP call against the
local Flask app, and sends the response back.

The connector is the security perimeter for any /api request arriving
from off-LAN. The relay validates the license key before it ever
forwards anything, but we defensively re-check the envelope shape and
allow-list of methods/paths here too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import threading
import time
from typing import Any, Optional

import requests
import websockets

# certifi ships a Mozilla CA bundle. The `requests` library already uses it
# implicitly; we need it explicitly for websockets because the stdlib's
# ssl.create_default_context() defaults to the system CA chain, which on
# macOS Homebrew Python and on PyInstaller-frozen builds doesn't reliably
# find roots like Let's Encrypt / DigiCert. With certifi.where() the chain
# resolves identically across dev (source) and prod (.app/.exe bundle).
try:
    import certifi
    _CA_BUNDLE: Optional[str] = certifi.where()
except ImportError:
    _CA_BUNDLE = None


log = logging.getLogger("relay_client")


_ALLOWED_METHODS = {"GET", "POST", "PATCH", "DELETE"}
_MAX_MESSAGE_BYTES = 512 * 1024  # 512 KB; matches relay's per-message cap
_DISPATCH_TIMEOUT_S = 12.0
_DEFAULT_RELAY_URL = "wss://relay.bitaxeballer.com"


# Single module-level state — there's exactly one connector per app instance.
_state_lock = threading.Lock()
_state: dict = {
    "enabled": False,
    "connected": False,
    "relay_url": "",
    "connected_since": 0.0,
    "last_error": "",
    "last_connect_attempt": 0.0,
}
_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None


def default_relay_url() -> str:
    return _DEFAULT_RELAY_URL


def get_status() -> dict:
    """Snapshot of the connector state for the UI."""
    with _state_lock:
        return dict(_state)


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def _update(**kwargs: Any) -> None:
    with _state_lock:
        _state.update(kwargs)


def start(license_key: str, *, relay_url: str, app_port: int, app_version: str) -> None:
    """Start the connector. Idempotent — second call is a no-op if already
    running. Returns immediately; connection happens on a background thread."""
    global _thread, _stop_event
    if is_running():
        return
    if not license_key:
        raise ValueError("license_key is required")
    _stop_event = threading.Event()
    _update(
        enabled=True,
        relay_url=relay_url,
        last_error="",
        connected=False,
        connected_since=0.0,
    )
    _thread = threading.Thread(
        target=_run_thread,
        args=(license_key, relay_url, app_port, app_version, _stop_event),
        name="relay-client",
        daemon=True,
    )
    _thread.start()


def stop(*, timeout: float = 5.0) -> None:
    """Signal the connector to shut down. Blocks up to `timeout` seconds for
    the thread to exit. Safe to call when not running."""
    global _thread, _stop_event
    if _stop_event:
        _stop_event.set()
    t = _thread
    if t:
        t.join(timeout=timeout)
    _thread = None
    _stop_event = None
    _update(enabled=False, connected=False, connected_since=0.0)


# ---------------- internals ----------------

def _run_thread(
    license_key: str,
    relay_url: str,
    app_port: int,
    app_version: str,
    stop_event: threading.Event,
) -> None:
    try:
        asyncio.run(_main_loop(license_key, relay_url, app_port, app_version, stop_event))
    except Exception:
        log.exception("relay client thread crashed")
        _update(connected=False, last_error="Connector crashed; check logs.")


async def _main_loop(
    license_key: str,
    relay_url: str,
    app_port: int,
    app_version: str,
    stop_event: threading.Event,
) -> None:
    """Connect → serve → reconnect. Exponential backoff capped at 60s.
    Returns when stop_event is set."""
    backoff = 1.0
    while not stop_event.is_set():
        _update(last_connect_attempt=time.time())
        try:
            await _connect_and_serve(
                license_key, relay_url, app_port, app_version, stop_event
            )
            # Clean exit from serve loop = stop requested or server closed; reset backoff.
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            msg = f"{e.__class__.__name__}: {e}"
            log.warning("relay connect failed: %s", msg)
            _update(connected=False, last_error=msg)
        if stop_event.is_set():
            break
        await _interruptible_sleep(backoff, stop_event)
        backoff = min(backoff * 2, 60.0)


async def _interruptible_sleep(seconds: float, stop_event: threading.Event) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_event.is_set():
            return
        await asyncio.sleep(min(0.5, end - time.monotonic()))


async def _connect_and_serve(
    license_key: str,
    relay_url: str,
    app_port: int,
    app_version: str,
    stop_event: threading.Event,
) -> None:
    """One WS lifecycle. Raises on connect failure, returns on clean close."""
    url = relay_url.rstrip("/") + "/ws/app"
    headers = [("Authorization", f"Bearer {license_key}")]
    log.info("relay connecting url=%s", url)
    ssl_ctx = None
    if url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context(cafile=_CA_BUNDLE) if _CA_BUNDLE else ssl.create_default_context()
    async with websockets.connect(
        url,
        additional_headers=headers,
        open_timeout=15,
        close_timeout=5,
        ping_interval=30,
        ping_timeout=15,
        max_size=_MAX_MESSAGE_BYTES,
        ssl=ssl_ctx,
    ) as ws:
        _update(connected=True, connected_since=time.time(), last_error="")
        log.info("relay connected url=%s", url)

        await ws.send(json.dumps({"type": "hello", "version": app_version}))

        try:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    return
                # Each request runs as its own task so a slow /api handler
                # doesn't stall the read loop or starve pings.
                asyncio.create_task(_handle_envelope(ws, raw, app_port))
        finally:
            _update(connected=False)


async def _handle_envelope(ws, raw: str, app_port: int) -> None:
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(msg, dict):
        return

    mtype = msg.get("type")
    if mtype == "ping":
        await _safe_send(ws, {"type": "pong"})
        return
    if mtype != "request":
        return  # forward-compat: silently ignore unknown types

    req_id = msg.get("id") or ""
    method = (msg.get("method") or "").upper()
    path = msg.get("path") or ""
    body = msg.get("body")

    if method not in _ALLOWED_METHODS:
        await _safe_send(ws, _error(req_id, 405, "Method not allowed."))
        return
    if not isinstance(path, str) or not path.startswith("/api/"):
        await _safe_send(ws, _error(req_id, 400, "Only /api/* paths are routable."))
        return

    loop = asyncio.get_running_loop()
    try:
        status, payload = await loop.run_in_executor(
            None, _dispatch_http, method, path, body, app_port
        )
    except Exception as e:
        log.exception("dispatch error path=%s", path)
        await _safe_send(ws, _error(req_id, 500, e.__class__.__name__))
        return

    await _safe_send(ws, {
        "type": "response",
        "id": req_id,
        "status": status,
        "body": payload,
    })


def _dispatch_http(method: str, path: str, body: Any, app_port: int) -> tuple[int, Any]:
    """Loopback HTTP into the local Flask app. Returns (status, parsed_body).

    Body is sent as JSON for POST/PATCH. The local app accepts no auth — this
    is the same path a browser tab on the LAN would take, only the trigger
    comes from a remote client via the relay rather than from local fetch().
    """
    url = f"http://127.0.0.1:{app_port}{path}"
    kwargs: dict = {"timeout": _DISPATCH_TIMEOUT_S}
    if body is not None and method in {"POST", "PATCH"}:
        kwargs["json"] = body
    r = requests.request(method, url, **kwargs)
    try:
        return r.status_code, r.json()
    except ValueError:
        # Non-JSON response (shouldn't happen for /api routes; included as a
        # safety net so remote clients get *something* rather than a 500).
        return r.status_code, {"text": r.text[:4096]}


def _error(req_id: str, status: int, message: str) -> dict:
    return {
        "type": "response",
        "id": req_id,
        "status": status,
        "body": {"error": message},
    }


async def _safe_send(ws, payload: dict) -> None:
    try:
        await ws.send(json.dumps(payload))
    except Exception:
        log.debug("failed to send envelope")
