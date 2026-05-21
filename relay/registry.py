from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket


@dataclass
class AppConn:
    """One connected app instance. Phase 1 allows only one app socket per
    license key — a new connection evicts the old one (e.g. user restarts
    the app, the stale socket gets closed).

    `pending` maps relay-allocated request IDs → Futures that resolve when
    the app sends back a matching response. The Future's result is the raw
    decoded message dict.
    """
    license_key: str
    ws: WebSocket
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    connected_at: float = field(default_factory=time.time)
    last_client_traffic: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_client_traffic = time.time()

    def idle_seconds(self) -> float:
        return time.time() - self.last_client_traffic


class Registry:
    """In-memory registry of currently-connected apps, keyed by license_key.

    Stateless across restarts — every connected client/app has to re-handshake
    after a relay restart. That's the trade-off we accepted for single-VPS
    simplicity; multi-instance would need Redis pub/sub here.
    """

    def __init__(self) -> None:
        self._apps: dict[str, AppConn] = {}
        self._lock = asyncio.Lock()

    async def register_app(self, license_key: str, ws: WebSocket) -> AppConn:
        async with self._lock:
            existing = self._apps.get(license_key)
            if existing is not None:
                # Evict the stale socket. Close best-effort; don't await — we
                # don't want a hung old socket to block the new one.
                try:
                    asyncio.create_task(existing.ws.close(code=1000))
                except Exception:
                    pass
                self._fail_pending(existing, "App reconnected; request dropped.")
            conn = AppConn(license_key=license_key, ws=ws)
            self._apps[license_key] = conn
            return conn

    async def unregister_app(self, conn: AppConn) -> None:
        async with self._lock:
            current = self._apps.get(conn.license_key)
            if current is conn:
                del self._apps[conn.license_key]
        self._fail_pending(conn, "App disconnected.")

    def get_app(self, license_key: str) -> Optional[AppConn]:
        return self._apps.get(license_key)

    def all_apps(self) -> list[AppConn]:
        return list(self._apps.values())

    @staticmethod
    def _fail_pending(conn: AppConn, reason: str) -> None:
        for fut in list(conn.pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        conn.pending.clear()
