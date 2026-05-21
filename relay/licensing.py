from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

import config


@dataclass
class LicenseInfo:
    key: str
    activation_id: Optional[str]
    status: str
    expires_at: Optional[str]
    email: Optional[str]


class LicenseError(Exception):
    """Raised when a license key fails validation. Message is safe to surface
    to the caller (no internal details, no secrets)."""


def _is_expired(expires_at: Optional[str]) -> bool:
    if not expires_at:
        return False
    try:
        dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return dt.timestamp() < datetime.now(timezone.utc).timestamp()


def validate(license_key: str, activation_id: Optional[str] = None) -> LicenseInfo:
    """Validates a license key against the bitaxe-baller license server.
    Raises LicenseError on any failure mode (bad key, expired, revoked,
    network failure)."""
    key = (license_key or "").strip()
    if not key:
        raise LicenseError("Missing license key.")

    if config.DEV_LICENSE_KEY and key == config.DEV_LICENSE_KEY:
        return LicenseInfo(
            key=key,
            activation_id=activation_id or "dev-activation",
            status="active",
            expires_at=None,
            email="dev@localhost",
        )

    body = {"license_key": key}
    if activation_id:
        body["instance_id"] = activation_id

    try:
        r = requests.post(
            f"{config.LICENSE_API_BASE}/validate",
            data=body,
            headers={"Accept": "application/json"},
            timeout=config.LICENSE_VALIDATE_TIMEOUT_S,
        )
    except requests.RequestException as e:
        raise LicenseError(f"License check unavailable: {e.__class__.__name__}.") from e

    try:
        payload = r.json()
    except ValueError:
        raise LicenseError(f"License check returned HTTP {r.status_code}.")

    if r.status_code >= 400:
        err = payload.get("error") if isinstance(payload, dict) else None
        raise LicenseError(err or f"License check returned HTTP {r.status_code}.")

    if not isinstance(payload, dict) or not payload.get("valid"):
        err = payload.get("error") if isinstance(payload, dict) else None
        raise LicenseError(err or "License key is not valid.")

    lk = payload.get("license_key") or {}
    instance = payload.get("instance") or {}
    meta = payload.get("meta") or {}

    status = (lk.get("status") or "").lower()
    if status and status != "active":
        raise LicenseError(f"License is {status}.")

    expires_at = lk.get("expires_at")
    if _is_expired(expires_at):
        raise LicenseError("License is expired.")

    return LicenseInfo(
        key=lk.get("key") or key,
        activation_id=instance.get("id") or activation_id,
        status=status or "active",
        expires_at=expires_at,
        email=meta.get("customer_email"),
    )
