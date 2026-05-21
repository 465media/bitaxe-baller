"""Bitaxe Baller remote-access relay.

Run with::

    uvicorn main:app --host 0.0.0.0 --port 8787

See README.md for env vars and architecture notes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Optional

from pathlib import Path

from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse

import config
import licensing
import protocol
import tokens
from registry import AppConn, Registry


log = logging.getLogger("relay")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


registry = Registry()
_idle_task: Optional[asyncio.Task] = None


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global _idle_task
    _idle_task = asyncio.create_task(_idle_disconnect_loop())
    log.info("relay started host=%s port=%s", config.HOST, config.PORT)
    try:
        yield
    finally:
        if _idle_task:
            _idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _idle_task


app = FastAPI(title="bitaxe-baller-relay", lifespan=lifespan)


_WEB_DIR = Path(__file__).parent / "web"


@app.get("/")
def index():
    """Single-page client. Login form + minimal dashboard. The browser then
    talks to the relay via WS on the same origin, so no CORS dance."""
    return FileResponse(_WEB_DIR / "index.html")


@app.get("/health")
def health():
    return {
        "ok": True,
        "connected_apps": len(registry.all_apps()),
        "version": "0.1.0",
    }


@app.post("/login")
def login(license_key: str = Form(...), activation_id: Optional[str] = Form(None)):
    """Exchange a license key (+ optional activation_id) for a session token.

    Form-encoded to match the LS conventions the app already uses.
    """
    try:
        info = licensing.validate(license_key, activation_id)
    except licensing.LicenseError as e:
        # 401 is correct here — caller authentication failed.
        return JSONResponse(status_code=401, content={"ok": False, "error": str(e)})

    token = tokens.mint(info.key)
    return {
        "ok": True,
        "token": token,
        "expires_in": config.SESSION_TTL_S,
        "email": info.email,
    }


def _extract_bearer(request_headers: dict, query_param: Optional[str]) -> Optional[str]:
    """Pull a credential from `Authorization: Bearer <x>` header, or fall
    back to a query parameter. Returns None if neither is present."""
    auth = request_headers.get("authorization") or request_headers.get("Authorization")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    return (query_param or "").strip() or None


@app.websocket("/ws/app")
async def ws_app(ws: WebSocket, key: Optional[str] = None, activation_id: Optional[str] = None):
    """App-side socket. Auth = license key (Authorization header or `?key=`).

    The relay validates the key against LS once at connect time. We do not
    keep a long-lived poll going for revocation in v0 — accepted trade-off
    for a 1-hour-ish lag on revocation, fine for launch.
    """
    headers = {k.decode().lower(): v.decode() for k, v in ws.scope.get("headers", [])}
    license_key = _extract_bearer(headers, key)
    if not license_key:
        await ws.close(code=4401)
        return

    try:
        info = licensing.validate(license_key, activation_id)
    except licensing.LicenseError as e:
        log.info("app reject license=%s reason=%s", _redact(license_key), e)
        await ws.accept()
        await ws.close(code=4401, reason=str(e)[:120])
        return

    await ws.accept()
    conn = await registry.register_app(info.key, ws)
    log.info("app connected license=%s email=%s", _redact(info.key), info.email)

    try:
        await _app_read_loop(conn)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("app loop error license=%s", _redact(info.key))
    finally:
        await registry.unregister_app(conn)
        log.info("app disconnected license=%s", _redact(info.key))


async def _app_read_loop(conn: AppConn) -> None:
    while True:
        raw = await conn.ws.receive_text()
        if len(raw) > protocol.MAX_BODY_BYTES:
            log.warning("app message too big license=%s bytes=%d", _redact(conn.license_key), len(raw))
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue

        mtype = msg.get("type")
        if mtype == "response":
            req_id = msg.get("id")
            fut = conn.pending.pop(req_id, None) if req_id else None
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif mtype == "hello":
            log.info(
                "app hello license=%s version=%s",
                _redact(conn.license_key),
                msg.get("version"),
            )
        elif mtype == "ping":
            await _send_json(conn, {"type": "pong"})
        # silently ignore other types — forward-compat


@app.websocket("/ws/client")
async def ws_client(ws: WebSocket, token: Optional[str] = None):
    """Client-side socket (browser, mobile). Auth = session token in
    `?token=...` or Authorization header.
    """
    headers = {k.decode().lower(): v.decode() for k, v in ws.scope.get("headers", [])}
    bearer = _extract_bearer(headers, token)
    if not bearer:
        await ws.close(code=4401)
        return

    try:
        license_key = tokens.verify(bearer)
    except tokens.TokenError as e:
        await ws.accept()
        await ws.close(code=4401, reason=str(e)[:120])
        return

    await ws.accept()
    log.info("client connected license=%s", _redact(license_key))

    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > protocol.MAX_BODY_BYTES:
                await ws.send_text(json.dumps(
                    protocol.make_error_response("", 413, "Request too large.")
                ))
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            await _handle_client_request(ws, license_key, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("client loop error license=%s", _redact(license_key))
    finally:
        log.info("client disconnected license=%s", _redact(license_key))


async def _handle_client_request(client_ws: WebSocket, license_key: str, msg: dict) -> None:
    if isinstance(msg, dict) and msg.get("type") == "ping":
        await client_ws.send_text(json.dumps({"type": "pong"}))
        return

    try:
        method, path, body = protocol.validate_client_request(msg)
    except ValueError as e:
        await client_ws.send_text(json.dumps(
            protocol.make_error_response(msg.get("id") if isinstance(msg, dict) else "", 400, str(e))
        ))
        return

    conn = registry.get_app(license_key)
    if conn is None:
        await client_ws.send_text(json.dumps(
            protocol.make_error_response("", 502, "App is not connected to the relay.")
        ))
        return

    req_id = protocol.new_request_id()
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    conn.pending[req_id] = fut
    conn.touch()

    try:
        await _send_json(conn, protocol.make_request(req_id, method, path, body))
    except Exception as e:
        conn.pending.pop(req_id, None)
        await client_ws.send_text(json.dumps(
            protocol.make_error_response("", 502, f"Failed to reach app: {e.__class__.__name__}")
        ))
        return

    try:
        response_msg = await asyncio.wait_for(fut, timeout=config.REQUEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        conn.pending.pop(req_id, None)
        await client_ws.send_text(json.dumps(
            protocol.make_error_response("", 504, "App did not respond in time.")
        ))
        return
    except Exception as e:
        await client_ws.send_text(json.dumps(
            protocol.make_error_response("", 502, str(e))
        ))
        return

    # Pass the app response straight through, but stamp the client-visible
    # id field empty (clients on the same socket don't need it — they
    # receive responses in the order they sent requests in v0).
    out = dict(response_msg)
    out["type"] = "response"
    out.pop("id", None)
    await client_ws.send_text(json.dumps(out))


async def _send_json(conn: AppConn, payload: dict) -> None:
    data = json.dumps(payload)
    async with conn.write_lock:
        await conn.ws.send_text(data)


async def _idle_disconnect_loop() -> None:
    """Closes app sockets that haven't seen client traffic in IDLE_DISCONNECT_S.
    Keeps long-idle tabs from chewing bandwidth.
    """
    interval = max(30, config.IDLE_DISCONNECT_S // 10)
    while True:
        await asyncio.sleep(interval)
        for conn in registry.all_apps():
            if conn.idle_seconds() > config.IDLE_DISCONNECT_S:
                log.info(
                    "app idle disconnect license=%s idle_s=%d",
                    _redact(conn.license_key),
                    int(conn.idle_seconds()),
                )
                with contextlib.suppress(Exception):
                    await conn.ws.close(code=1000, reason="idle")


def _redact(license_key: str) -> str:
    if not license_key:
        return ""
    if len(license_key) <= 8:
        return "***"
    return f"{license_key[:4]}…{license_key[-4:]}"
