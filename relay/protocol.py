"""JSON message envelopes exchanged over the relay WebSockets.

Two sockets, four message types:

  Client → Relay → App        type="request"   {id, method, path, body?}
  App    → Relay → Client     type="response"  {id, status, body?}
  App    → Relay              type="hello"     {version, app_id?}        (sent once after connect)
  either                      type="ping"|"pong"                          (keepalive)

Request IDs are allocated by the relay (UUID4 hex) so multiple clients
sharing a license key can't collide. The app echoes the id verbatim in
its response.
"""

from __future__ import annotations

import uuid
from typing import Any


ALLOWED_METHODS = {"GET", "POST", "PATCH", "DELETE"}
MAX_PATH_LEN = 512
MAX_BODY_BYTES = 256 * 1024  # 256 KB per message — more than enough for any /api/* route


def new_request_id() -> str:
    return uuid.uuid4().hex


def make_request(req_id: str, method: str, path: str, body: Any) -> dict:
    return {"type": "request", "id": req_id, "method": method, "path": path, "body": body}


def make_response(req_id: str, status: int, body: Any) -> dict:
    return {"type": "response", "id": req_id, "status": status, "body": body}


def make_error_response(req_id: str, status: int, message: str) -> dict:
    return {"type": "response", "id": req_id, "status": status, "body": {"error": message}}


def validate_client_request(msg: dict) -> tuple[str, str, Any]:
    """Returns (method, path, body) or raises ValueError with a user-safe
    message describing what's wrong with the request envelope."""
    if not isinstance(msg, dict):
        raise ValueError("Request must be a JSON object.")
    if msg.get("type") != "request":
        raise ValueError("Unexpected message type.")
    method = (msg.get("method") or "").upper()
    if method not in ALLOWED_METHODS:
        raise ValueError(f"Method '{method}' not allowed.")
    path = msg.get("path") or ""
    if not isinstance(path, str) or not path.startswith("/api/"):
        raise ValueError("Path must start with /api/.")
    if len(path) > MAX_PATH_LEN:
        raise ValueError("Path too long.")
    body = msg.get("body")
    return method, path, body
