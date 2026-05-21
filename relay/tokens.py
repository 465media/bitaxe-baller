from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import config


class TokenError(Exception):
    """Raised when a session token is malformed, expired, or has a bad signature."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload: bytes) -> bytes:
    return hmac.new(config.SECRET.encode("utf-8"), payload, hashlib.sha256).digest()


def mint(license_key: str, ttl_s: int | None = None) -> str:
    """Returns a signed session token bound to `license_key`. Token is opaque
    to the client and self-contained for the relay (no server-side session
    store needed)."""
    if not license_key:
        raise TokenError("license_key required")
    ttl = ttl_s if ttl_s is not None else config.SESSION_TTL_S
    now = int(time.time())
    payload = {"k": license_key, "iat": now, "exp": now + ttl}
    payload_b = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = _sign(payload_b)
    return f"{_b64url_encode(payload_b)}.{_b64url_encode(sig)}"


def verify(token: str) -> str:
    """Verifies a token and returns the bound license_key. Raises TokenError
    on any failure (malformed, bad signature, expired)."""
    if not token or "." not in token:
        raise TokenError("Malformed token.")
    try:
        payload_part, sig_part = token.split(".", 1)
        payload_b = _b64url_decode(payload_part)
        provided_sig = _b64url_decode(sig_part)
    except (ValueError, TypeError):
        raise TokenError("Malformed token.")

    expected_sig = _sign(payload_b)
    if not hmac.compare_digest(provided_sig, expected_sig):
        raise TokenError("Bad signature.")

    try:
        payload = json.loads(payload_b)
    except ValueError:
        raise TokenError("Malformed token payload.")

    if not isinstance(payload, dict) or "k" not in payload or "exp" not in payload:
        raise TokenError("Malformed token payload.")

    if int(payload["exp"]) < int(time.time()):
        raise TokenError("Token expired.")

    return payload["k"]
