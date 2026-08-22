"""
Bitaxe Baller
Run: python app.py    (from source)
or:  open Bitaxe-Baller.app    (packaged release)

Then your default browser opens to the dashboard. Add devices and tune.
"""

import base64
import hashlib
import json
import re
import socket
import sqlite3
import sys
import time
import threading
import os
import webbrowser
from collections import deque
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, render_template, request

import relay_client


# Single source of truth for the app version. The PyInstaller spec's
# Info.plist/EXE version and the dashboard footer template should both
# match this string. Update bump checklist: APP_VERSION here, the spec's
# version="..." entries, and the v1.X.Y string in dashboard.html + device.html.
APP_VERSION = "1.21.2"


# Test-mode override: pretend to be an older version so the auto-update flow
# fires even when nothing newer has actually shipped. Validated only against
# our own _parse_semver — won't affect anything other than update checks.
# Documented in the test plan; never set on a user's install.
_VERSION_OVERRIDE = os.environ.get("BITAXE_BALLER_VERSION_OVERRIDE", "").strip()
if _VERSION_OVERRIDE:
    APP_VERSION = _VERSION_OVERRIDE


# ----- Resource & data paths -----
# When running from source, all paths live in the repo directory (current
# behavior). When PyInstaller-frozen, templates/static come from the bundle's
# Resources folder (read-only) and user-writable state goes to a per-user
# directory the OS lets us write to.
def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_resource_dir() -> str:
    """Where templates/ and static/ live (read-only when frozen)."""
    if _is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_data_dir() -> str:
    """User-writable directory for config.json and logs/. Source mode keeps
    them next to app.py so the existing dev workflow is unchanged. Container
    deploys (Umbrel, Docker, etc.) override via BITAXE_BALLER_DATA_DIR so the
    persistent volume sits outside the app directory."""
    override = os.environ.get("BITAXE_BALLER_DATA_DIR", "").strip()
    if override:
        return override
    if not _is_frozen():
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Bitaxe Baller")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Bitaxe Baller")
    return os.path.expanduser("~/.config/bitaxe-baller")


_RESOURCE_DIR = app_resource_dir()
_DATA_DIR = app_data_dir()
os.makedirs(_DATA_DIR, exist_ok=True)

app = Flask(
    __name__,
    template_folder=os.path.join(_RESOURCE_DIR, "templates"),
    static_folder=os.path.join(_RESOURCE_DIR, "static"),
)

CONFIG_PATH = os.path.join(_DATA_DIR, "config.json")
LOG_DIR = os.path.join(_DATA_DIR, "logs")
HISTORY_DB_PATH = os.path.join(_DATA_DIR, "history.db")
os.makedirs(LOG_DIR, exist_ok=True)

# Pro tier: persistent SQLite history. Retention defaults to 90 days; tunable
# below. The free tier keeps a rolling 1h in-memory deque + daily CSVs, both
# unchanged. SQLite is additive — every successful poll appends a row when Pro
# is active. With 3 devices @ 5s polling × 90 days = ~4.7M rows, ~300MB worst
# case. SQLite handles this trivially.
HISTORY_RETENTION_DAYS = 90

DEFAULT_POLL = 5
HISTORY_POINTS = 720  # 1 hour at 5s
ROLLING_WINDOWS = {"1m": 12, "5m": 60, "15m": 180, "1h": 720}

# Electricity cost estimation. Rate is per-kWh in the user's local currency; the
# currency is a display symbol only (we don't convert). Defaults to a ballpark
# US residential rate so the estimate is useful out of the box — the tooltip and
# settings panel make clear it's an estimate the user should set to their real
# rate. Cost is a free feature (a lead-in that makes "what is this costing me?"
# answerable without Pro).
DEFAULT_ELEC_RATE = 0.12       # $/kWh — sensible default, user-editable
DEFAULT_CURRENCY = "$"

# ----- Pro license server -----
# Self-hosted license server at bitaxeballer.com/api/license — same JSON
# response shape as the legacy Lemon Squeezy license API (we built the new
# server to match that shape so the desktop client didn't have to change
# beyond this URL flip). The license key itself is the credential — no API
# token or store ID needed in the desktop binary. Endpoints:
#   POST /activate    — consume 1 of 5 activations, returns instance_id
#   POST /validate    — verify key + instance still good
#   POST /deactivate  — free the activation slot
# These accept application/x-www-form-urlencoded bodies (not JSON).
LEMONSQUEEZY_API_BASE = "https://bitaxeballer.com/api/license"
# Re-validate against the license server at most once per 24h to catch
# refunds / expirations without hammering the endpoint on every request.
LICENSE_REVALIDATE_S = 24 * 3600

# Tuning presets for Gamma (BM1370)
PRESETS = {
    "stock":      {"frequency": 525, "coreVoltage": 1150, "label": "Stock"},
    "mild":       {"frequency": 550, "coreVoltage": 1170, "label": "Mild OC"},
    "balanced":   {"frequency": 575, "coreVoltage": 1185, "label": "Balanced"},
    "aggressive": {"frequency": 600, "coreVoltage": 1200, "label": "Aggressive"},
    "max":        {"frequency": 625, "coreVoltage": 1225, "label": "Max (risky)"},
}

# Sane bounds — refuse to send anything outside these. BM1370 (Gamma) can run
# stable past 700 MHz on good silicon with adequate cooling — empirically up to
# ~900 MHz at 1200 mV on well-cooled chips. The previous 700 cap was too
# conservative for users tuning silicon-lottery winners. The 1300 mV ceiling
# stays — that's Bitmain's spec, going higher accelerates chip wear sharply.
BOUNDS = {
    "frequency": (400, 900),     # MHz
    "coreVoltage": (1000, 1300), # mV
    "fanspeed": (0, 100),        # %
}

state_lock = threading.Lock()
state = {}                # ip -> device state
config_lock = threading.Lock()
poll_thread = None
poll_stop_flag = threading.Event()


def default_config():
    return {
        "devices": [],
        "poll_interval": DEFAULT_POLL,
        "electricity": {"rate": DEFAULT_ELEC_RATE, "currency": DEFAULT_CURRENCY},
    }


def _electricity_cfg():
    """Read the electricity rate + currency, tolerating configs written before
    this key existed (and bad hand-edits). Returns (rate: float, currency: str).
    A non-positive or non-numeric rate falls back to the default so we never
    show a nonsensical $0.00/day for every device."""
    with config_lock:
        raw = (load_config().get("electricity") or {})
    try:
        rate = float(raw.get("rate", DEFAULT_ELEC_RATE))
    except (TypeError, ValueError):
        rate = DEFAULT_ELEC_RATE
    if rate < 0:
        rate = DEFAULT_ELEC_RATE
    currency = str(raw.get("currency") or DEFAULT_CURRENCY)[:4]
    return rate, currency


def _cost_block(power_w, rate, currency):
    """Estimated running cost for a device drawing `power_w` watts continuously.
    perDay = kW × 24h × rate; perMonth uses a 30-day month. Returns rate +
    currency alongside so the frontend can render without a second fetch and the
    fleet total can sum perDay across devices."""
    kwh_per_day = (power_w or 0) / 1000.0 * 24.0
    per_day = kwh_per_day * rate
    return {
        "perDay": round(per_day, 2),
        "perMonth": round(per_day * 30, 2),
        "rate": rate,
        "currency": currency,
    }


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(default_config())
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def init_device_state(ip, label, chain_override=None, device_type=None):
    return {
        "ip": ip,
        "label": label,
        # "axeos" (Bitaxe / NerdQAxe / other ESP-Miner forks — full support) or
        # "braiins" (Braiins OS mini miners like the BMM-101 — monitor-only).
        "device_type": device_type or "axeos",
        "chain_override": (chain_override or None),  # manual chain pin; None = auto-detect
        "history": deque(maxlen=HISTORY_POINTS),
        "latest": None,
        "online": False,
        "consecutive_errors": 0,
        "last_error": "",
        "session_start": time.time(),
        "session_shares_start": None,
        "session_hwerrors_start": None,
        "events": deque(maxlen=50),         # tuning changes, restarts, etc
        "autotune": None,                   # populated when a sweep is in flight
        "share_events": deque(maxlen=50),   # streaming shares feed (rolling 50)
        "prev_shares_accepted": None,       # last polled value, for delta detection
        "prev_shares_rejected": None,
        "prev_best_diff_value": 0.0,        # for new-best detection
        # Block-found counter from firmware's `blockFound` field. Increments
        # only when this device finds a real network block. We compare the
        # current value against the last-seen on every poll — any positive
        # delta triggers a celebration record. None until the first poll
        # establishes a baseline so we don't false-fire on startup.
        "prev_block_found": None,
    }


# ---------- ARP-based MAC resolution (for MAC-less devices) ----------
#
# Some devices don't expose a MAC over their machine API — notably Braiins OS,
# whose CGMiner API has no MAC field. The public leaderboard keys entries on
# MAC (to de-dupe a board across submissions), so a MAC-less device can never
# make the board. But we already poll every device over TCP every 5s, which
# guarantees the host's ARP cache holds a fresh entry for it — so we can
# recover the real NIC MAC from there.
#
# Trust boundary: ARP is L2, so this only returns the device's own MAC when the
# device is on the same segment as the host. Across a router ARP would hand back
# the gateway's MAC — but every device Baller tracks is a LAN miner the scanner
# reached directly, so that case doesn't arise in practice.

_arp_cache: dict = {}          # ip -> (mac, ts); mac == "" caches a miss
_arp_cache_lock = threading.Lock()
_ARP_CACHE_TTL_S = 600         # MACs are stable; re-resolve at most every 10 min
_ARP_MISS_TTL_S = 30           # retry an unresolved IP soon (may be warming up)

_MAC_RE = re.compile(r"(?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}")
_MAC_ZERO = "00:00:00:00:00:00"


def _normalize_mac(raw: str) -> str:
    """Zero-pad and lowercase a colon-separated MAC. '' unless it's 6 octets."""
    parts = (raw or "").split(":")
    if len(parts) != 6:
        return ""
    try:
        return ":".join(f"{int(p, 16):02x}" for p in parts)
    except ValueError:
        return ""


def _arp_lookup_raw(ip: str) -> str:
    """Best-effort single-IP ARP lookup, no cache. Tries Linux /proc/net/arp
    first (exact, no subprocess) then the `arp` command (macOS/BSD, and Linux
    without /proc). Returns a normalized MAC or ''."""
    # Linux: /proc/net/arp columns are: IP HWtype Flags HWaddress Mask Device
    try:
        with open("/proc/net/arp") as f:
            next(f, None)  # header
            for line in f:
                cols = line.split()
                if len(cols) >= 4 and cols[0] == ip:
                    mac = _normalize_mac(cols[3])
                    if mac and mac != _MAC_ZERO:
                        return mac
    except OSError:
        pass  # not Linux / no procfs — fall through to the command
    # macOS/BSD (and Linux fallback): `arp -n <ip>`
    import subprocess
    try:
        out = subprocess.run(
            ["arp", "-n", ip], capture_output=True, text=True, timeout=2
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = _MAC_RE.search(out or "")
    if not m:
        return ""
    mac = _normalize_mac(m.group(0))
    return mac if mac != _MAC_ZERO else ""


def _resolve_mac(ip: str) -> str:
    """Cached ARP MAC lookup for `ip`. '' when it can't be resolved. Successes
    are cached for _ARP_CACHE_TTL_S, misses briefly so a just-online device
    (no ARP entry yet) gets retried on the next poll."""
    now = time.time()
    with _arp_cache_lock:
        hit = _arp_cache.get(ip)
        if hit:
            mac, ts = hit
            if (now - ts) < (_ARP_CACHE_TTL_S if mac else _ARP_MISS_TTL_S):
                return mac
    mac = _arp_lookup_raw(ip)
    with _arp_cache_lock:
        _arp_cache[ip] = (mac, now)
    return mac


# ---------- Braiins OS driver (BMM-101 etc.) — monitor-only ----------
#
# Braiins mini miners (BMM-101) run Braiins OS, not AxeOS: the web UI is an
# SPA that answers 200 HTML for ANY path (including /api/system/info), and the
# real machine interface is the classic CGMiner-compatible TCP API on :4028
# ("BOSer"). We poll it with one compound command and translate the response
# into the AxeOS /api/system/info field shape so the rest of the app (poll
# loop, CSV, charts, summary, block-found detection) works unchanged.
# Tuning / pool config / restart / firmware are NOT supported for this type —
# the mutating endpoints reject it and the UI hides those controls.

BRAIINS_API_PORT = 4028


def _braiins_cmd(ip, command, timeout=3):
    """One CGMiner-API request/response over TCP. Responses are a single JSON
    object, sometimes NUL-terminated."""
    with socket.create_connection((ip, BRAIINS_API_PORT), timeout=timeout) as sk:
        sk.settimeout(timeout)
        sk.sendall(json.dumps({"command": command}).encode())
        buf = b""
        while True:
            chunk = sk.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.rstrip(b"\x00").decode(errors="replace"))


def _braiins_section(resp, cmd, section):
    """Pull the first record of a section from a compound-command response."""
    try:
        return resp[cmd][0][section][0]
    except (KeyError, IndexError, TypeError):
        return {}


def fetch_braiins(ip, timeout=3):
    """Poll a Braiins OS device and return an AxeOS-shaped info dict."""
    resp = _braiins_cmd(ip, "summary+temps+fans+pools+tunerstatus+devs+version", timeout)
    summary = _braiins_section(resp, "summary", "SUMMARY")
    temps = _braiins_section(resp, "temps", "TEMPS")
    fans = _braiins_section(resp, "fans", "FANS")
    pools = _braiins_section(resp, "pools", "POOLS")
    tuner = _braiins_section(resp, "tunerstatus", "TUNERSTATUS")
    devs = _braiins_section(resp, "devs", "DEVS")
    ver = _braiins_section(resp, "version", "VERSION")

    # stratum+tcp://host:port -> (host, port)
    pool_url = pools.get("URL", "") or ""
    stratum_host, stratum_port = "", 0
    m = re.match(r"^(?:stratum\+tcp://)?([^:/]+)(?::(\d+))?", pool_url)
    if m:
        stratum_host = m.group(1)
        stratum_port = int(m.group(2) or 0)

    best = summary.get("Best Share", 0) or 0
    return {
        "deviceType": "braiins",
        "ASICModel": "BMM-101",
        "version": ver.get("BOSer", "") or "BOSer",
        "hostname": "",
        # The CGMiner API exposes no MAC, so recover it from the host ARP cache
        # (we've just polled :4028, so an entry exists). Lets the device reach
        # the MAC-keyed leaderboard; '' if it can't be resolved, which the
        # leaderboard submit then skips as before.
        "macAddr": _resolve_mac(ip),
        "hashRate": (summary.get("MHS 1m", 0) or summary.get("MHS 5m", 0) or 0) / 1000.0,  # GH/s
        "expectedHashrate": (devs.get("Nominal MHS", 0) or 0) / 1000.0,
        "temp": temps.get("Chip", 0) or 0,
        "vrTemp": 0,  # no VR sensor exposed; Board temp exists but isn't a VR reading — don't fake it
        "power": tuner.get("ApproximateMinerPowerConsumption", 0) or 0,
        "voltage": 0,
        "coreVoltage": 0,
        "frequency": 0,
        "fanspeed": fans.get("Speed", 0) or 0,
        "fanrpm": fans.get("RPM", 0) or 0,
        "autofanspeed": 1,  # BOSer manages its own fan curve
        "sharesAccepted": summary.get("Accepted", 0) or 0,
        "sharesRejected": summary.get("Rejected", 0) or 0,
        "bestDiff": str(best),          # BOSer only tracks since-restart best
        "bestSessionDiff": str(best),
        "uptimeSeconds": summary.get("Elapsed", 0) or 0,
        "blockFound": summary.get("Found Blocks", 0) or 0,
        "stratumURL": stratum_host,
        "stratumPort": stratum_port,
        "stratumUser": pools.get("User", "") or "",
        # BOSer reports the live stratum difficulty; surface it like AxeOS's poolDifficulty.
        "poolDifficulty": pools.get("Stratum Difficulty", 0) or pools.get("Work Difficulty", 0) or 0,
    }


def _is_monitor_only(ip) -> bool:
    """True when `ip` is a monitor-only device (Braiins OS) we must never send
    mutating AxeOS calls to. Boolean sibling of _monitor_only_error for use
    outside request handlers (e.g. the scheduler)."""
    with state_lock:
        return (state.get(ip) or {}).get("device_type", "axeos") == "braiins"


def _monitor_only_error(ip):
    """Returns a (response, status) rejection when `ip` is a monitor-only
    device (Braiins OS), else None. Used as a guard at the top of every
    endpoint that mutates device settings via the AxeOS HTTP API."""
    with state_lock:
        dtype = (state.get(ip) or {}).get("device_type", "axeos")
    if dtype == "braiins":
        return jsonify({
            "error": "This is a Braiins OS device — Bitaxe Baller monitors it, but tuning, "
                     "pool changes, restarts and firmware stay in its own web UI."
        }), 400
    return None


# Chips whose tuning PRESETS + safety BOUNDS we've actually validated on real
# hardware: the Bitaxe / NerdQAxe family. Frequency and core-voltage limits are
# chip-specific electrical bounds — the 400–900 MHz / 1000–1300 mV envelope is
# BM1370-derived. Pushing that at a different chip (e.g. the 3nm BM1373 in a
# Nexus S1) risks hardware damage we can't undo. So tuning is MONITOR-FIRST for
# any identified chip NOT in this set: Baller still polls, charts, logs and
# leaderboards it, but frequency/voltage tuning stays in the device's own web
# UI until we validate a real unit. Fan/pool/restart/identify are chip-agnostic
# and remain available. (An empty ASICModel = unidentifiable read → don't block,
# so a flaky poll never locks a genuine Bitaxe out of its own tune panel.)
_TUNABLE_CHIPS = {"BM1366", "BM1368", "BM1370"}


def _device_chip(ip) -> str:
    with state_lock:
        return str(((state.get(ip) or {}).get("latest") or {}).get("ASICModel", "")).strip()


def _tuning_supported(ip) -> bool:
    """False only when the device reports a known ASIC we haven't validated
    tuning bounds for (e.g. BM1373). Empty/unknown model → True (don't block)."""
    model = _device_chip(ip)
    return (not model) or (model in _TUNABLE_CHIPS)


def _tuning_unsupported_msg(ip) -> str:
    """Human-facing reason string when `ip`'s chip isn't tunable via Baller,
    else '' . Shared by the single-device guard and the bulk-tune per-row path."""
    if _tuning_supported(ip):
        return ""
    model = _device_chip(ip) or "this"
    return (
        f"Tuning isn't available for {model} chips in Bitaxe Baller yet — our "
        f"presets and safety bounds are validated for the Bitaxe family "
        f"(BM1366/1368/1370), and {model} has a different power envelope. Baller "
        f"monitors it fully; set frequency and voltage from the device's own web UI."
    )


def _tuning_unsupported_error(ip):
    """Returns a (response, status) rejection when `ip`'s ASIC isn't one we've
    validated tuning bounds for, else None. Sits alongside _monitor_only_error
    at the top of the frequency/voltage tuning endpoints."""
    msg = _tuning_unsupported_msg(ip)
    return (jsonify({"error": msg, "code": "chip_unsupported"}), 400) if msg else None


def fetch_device(ip, timeout=3, device_type=None):
    """Fetch live status for a device. Known Braiins devices go straight to the
    CGMiner API; everything else tries AxeOS HTTP first, then falls back to a
    Braiins sniff when the HTTP answer isn't a miner (covers first-time adds,
    where the type isn't known yet)."""
    if device_type == "braiins":
        return fetch_braiins(ip, timeout)
    try:
        r = requests.get(f"http://{ip}/api/system/info", timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if "hashRate" not in d and "ASICModel" not in d:
            raise ValueError("responds over HTTP but not with AxeOS system info")
        return d
    except (ValueError, requests.exceptions.RequestException):
        if device_type is None:
            # Unknown type — maybe it's a Braiins OS device (SPA answers 200
            # HTML on every path, so the JSON parse above throws).
            try:
                return fetch_braiins(ip, timeout)
            except Exception:
                pass
        raise


def patch_device(ip, settings, timeout=5):
    r = requests.patch(
        f"http://{ip}/api/system",
        json=settings,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    r.raise_for_status()
    return True


def restart_device(ip, timeout=5):
    r = requests.post(f"http://{ip}/api/system/restart", timeout=timeout)
    r.raise_for_status()
    return True


def log_event(ip, msg):
    with state_lock:
        if ip in state:
            state[ip]["events"].appendleft({
                "t": time.time(),
                "msg": msg,
            })


def poll_one(ip, label, device_type=None):
    ts = time.time()
    try:
        data = fetch_device(ip, device_type=device_type)
        block_found_alert = None   # captured under the lock, dispatched (HTTP) AFTER it
        with state_lock:
            if ip not in state:
                return
            s = state[ip]
            was_offline = not s["online"]
            s["online"] = True
            s["consecutive_errors"] = 0
            s["last_error"] = ""
            s["latest"] = data

            if s["session_shares_start"] is None:
                s["session_shares_start"] = data.get("sharesAccepted", 0)
                s["session_hwerrors_start"] = data.get("sharesRejected", 0)

            # Share-event detection (live share feed). We don't have access to
            # the actual difficulty of each individual share — firmware only
            # exposes counters and the running best. So we synthesize two event
            # types: "accepted/rejected" deltas (with count, e.g. "+3 accepted")
            # and a "new best!" event when bestDiff crosses upward.
            cur_accepted = int(data.get("sharesAccepted", 0) or 0)
            cur_rejected = int(data.get("sharesRejected", 0) or 0)
            cur_best_val = _parse_diff(data.get("bestDiff", "0"))
            cur_best_str = str(data.get("bestDiff", "0"))
            if s["prev_shares_accepted"] is not None:
                d_acc = cur_accepted - s["prev_shares_accepted"]
                d_rej = cur_rejected - (s["prev_shares_rejected"] or 0)
                # Firmware can reset counters on restart; skip negative deltas.
                if d_acc > 0:
                    s["share_events"].appendleft({
                        "t": ts,
                        "type": "accepted",
                        "count": d_acc,
                    })
                if d_rej > 0:
                    s["share_events"].appendleft({
                        "t": ts,
                        "type": "rejected",
                        "count": d_rej,
                    })
                if cur_best_val > s["prev_best_diff_value"] and s["prev_best_diff_value"] > 0:
                    # Prepend so the most-recent event is at index 0.
                    s["share_events"].appendleft({
                        "t": ts,
                        "type": "new_best",
                        "best_diff": cur_best_str,
                        "best_diff_value": cur_best_val,
                    })
                    log_event(ip, f"new best share: {cur_best_str}")
            s["prev_shares_accepted"] = cur_accepted
            s["prev_shares_rejected"] = cur_rejected
            if cur_best_val > s["prev_best_diff_value"]:
                s["prev_best_diff_value"] = cur_best_val

            # BLOCK FOUND detection. The firmware exposes a counter that
            # increments on a real solved-block share — under one of three
            # different names depending on which firmware family this is
            # (see _BLOCK_COUNT_FIELDS). We baseline it on the first
            # successful poll (prev_block_found == None → just latch the
            # current value) so we never false-fire on startup from an
            # existing nonzero count. After baseline, any positive delta is
            # a celebration trigger — _block_finds_record persists it + the
            # dashboard's poll will pick up the unack'd find and throw
            # confetti. Multi-find catch-up (e.g. device offline for a while,
            # the counter jumped by N) produces a single record tagged with
            # the delta count.
            cur_block_found = _device_block_count(data)
            s["prev_block_found"], block_delta = _block_find_transition(
                s["prev_block_found"], cur_block_found
            )
            if block_delta > 0:
                delta = block_delta
                chain_id = s.get("chain_override") or _infer_chain(
                    data.get("stratumURL", ""),
                    data.get("stratumPort", 0),
                    data.get("stratumUser", ""),
                )
                # state_lock is held here, which _fleet_block_height requires.
                rec = _block_finds_record(ip, s["label"], data, chain_id,
                                          height_fallback=_fleet_block_height(chain_id))
                rec["delta"] = delta  # so the UI can say "+N blocks found"
                log_event(ip, f"🎉 BLOCK FOUND on {rec['chain_name']} (height {rec['block_height']}, diff {rec['best_diff']})")
                # Block-found is the one alert nobody wants to miss — but DON'T dispatch
                # it here: _alerts_dispatch does Discord/email HTTP, and running that under
                # state_lock means a slow or hung webhook freezes every device poll and the
                # dashboard (/api/devices blocks on the same lock). Capture it now, fire it
                # once the lock is released (below), same as the offline/temp alerts path.
                block_found_alert = (
                    s["label"], ip,
                    f"🎉 BLOCK FOUND: {s['label']} on {rec['chain_name']}",
                    f"Bitaxe {s['label']} just solved a {rec['chain_name']} block at height {rec['block_height']}. "
                    f"Difficulty: {rec['best_diff']}. Look at your wallet — this is the lottery hit.",
                )

            point = {
                "t": ts,
                "hashRate": data.get("hashRate", 0),
                "temp": data.get("temp", 0),
                "vrTemp": data.get("vrTemp", 0),
                "power": data.get("power", 0),
                "voltage": data.get("voltage", 0),
                "coreVoltage": data.get("coreVoltage", 0),
                "frequency": data.get("frequency", 0),
                "sharesAccepted": data.get("sharesAccepted", 0),
                "sharesRejected": data.get("sharesRejected", 0),
                "bestDiff": data.get("bestDiff", "0"),
                "bestSessionDiff": data.get("bestSessionDiff", "0"),
                "uptime": data.get("uptimeSeconds", 0),
            }
            s["history"].append(point)

        # Block-found alert — dispatched OUTSIDE state_lock (does Discord/email HTTP).
        if block_found_alert:
            try:
                _alerts_dispatch(block_found_alert[0], block_found_alert[1], "block_found",
                                 block_found_alert[2], block_found_alert[3])
            except Exception:
                pass

        if was_offline:
            log_event(ip, "Device back online")
        append_csv(label, point)
        _history_record(ip, point)

        # Advance auto-tune state machine if a sweep is running. Done under
        # state_lock to keep mutations consistent with the poll write above.
        # Resolve the electricity rate before taking state_lock so device_summary
        # never nests config_lock inside state_lock.
        elec = _electricity_cfg()
        with state_lock:
            if ip in state:
                _autotune_tick(ip, state[ip])
                summary_for_alerts = device_summary(state[ip], elec=elec)
                new_best_this_tick = bool(s["share_events"]) and s["share_events"][0].get("type") == "new_best" and s["share_events"][0].get("t") == ts
        # Alerts check uses the public summary (so the same shape the UI sees).
        # Done outside the state_lock since it does HTTP to Discord webhooks
        # and we don't want to hold the lock across a network round-trip.
        _alerts_check(ip, label, summary_for_alerts)
        # Leaderboard submission (Pro opt-in). Outside state_lock since it
        # does network I/O. Silent no-op when feature is disabled.
        try:
            _maybe_submit_leaderboard(summary_for_alerts, new_best=new_best_this_tick)
        except Exception:
            pass
    except Exception as e:
        elec = _electricity_cfg()  # resolve before state_lock (see note above)
        with state_lock:
            if ip not in state:
                return
            s = state[ip]
            was_online = s["online"]
            s["consecutive_errors"] += 1
            s["last_error"] = str(e)[:100]
            if s["consecutive_errors"] >= 3:
                s["online"] = False
            offline_summary = device_summary(s, elec=elec)
        if was_online and state.get(ip, {}).get("consecutive_errors", 0) == 3:
            log_event(ip, f"Device went offline: {str(e)[:60]}")
        # Alerts also need to fire on offline. The offline-rule logic in
        # _alerts_check handles the "how long" determination via consecutive_errors.
        _alerts_check(ip, label, offline_summary)


def poll_loop():
    while not poll_stop_flag.is_set():
        with config_lock:
            cfg = load_config()
            interval = cfg.get("poll_interval", DEFAULT_POLL)
            devices = list(cfg.get("devices", []))

        if devices:
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=max(4, len(devices))) as ex:
                futures = [ex.submit(poll_one, d["ip"], d["label"], d.get("type")) for d in devices]
                for f in futures:
                    try:
                        f.result(timeout=10)
                    except Exception:
                        pass
            # No-op unless 24h have elapsed since the last sweep.
            _history_sweep_retention()
            # Fire any due scheduled pool switches. Cheap no-op unless a schedule
            # matches the current minute (and Pro is active).
            try:
                _run_pool_schedules()
            except Exception:
                pass
            elapsed = time.time() - t0
            time.sleep(max(0.5, interval - elapsed))
        else:
            time.sleep(2)


def append_csv(label, point):
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(LOG_DIR, f"{safe_label}_{date_str}.csv")
    new_file = not os.path.exists(path)
    with open(path, "a") as f:
        if new_file:
            f.write("timestamp,iso_time,hashrate_ghs,asic_temp_c,vr_temp_c,power_w,"
                    "voltage_mv,core_voltage_mv,frequency_mhz,"
                    "shares_accepted,shares_rejected,uptime_s\n")
        iso = datetime.fromtimestamp(point["t"]).isoformat()
        f.write(f"{point['t']:.0f},{iso},{point['hashRate']:.2f},{point['temp']:.1f},"
                f"{point['vrTemp']:.1f},{point['power']:.2f},{point['voltage']:.0f},"
                f"{point['coreVoltage']:.0f},{point['frequency']:.0f},"
                f"{point['sharesAccepted']},{point['sharesRejected']},"
                f"{point['uptime']}\n")


def rolling_avg(history, window_size, key="hashRate"):
    if not history:
        return 0
    pts = list(history)[-window_size:]
    if not pts:
        return 0
    return sum(p[key] for p in pts) / len(pts)


def _clamp(val, key):
    lo, hi = BOUNDS[key]
    return max(lo, min(hi, val))


_SEVERITY_RANK = {"crit": 4, "warn": 3, "good": 2, "info": 1}


def _max_severity(recs):
    """Highest-severity rec (excluding the 'warming up' info rec). Drives the
    health border on the home page card. Returns None if nothing actionable."""
    actionable = [r for r in recs if r.get("id") != "warming_up"]
    if not actionable:
        return None
    return max(actionable, key=lambda r: _SEVERITY_RANK.get(r.get("severity"), 0))["severity"]


def compute_recommendations(s, hist, avgs, hw_rate_pct, shares_delta, j_per_th, ghs, expected_ghs):
    """Rule-based suggestions tied to current telemetry. Returns up to 3 recs,
    most-severe first. Each rec has an optional `action` the UI can fire one-click."""
    if not s["latest"]:
        return []

    # Braiins OS devices are monitor-only: BOSer runs its own autotuner and we
    # can't PATCH frequency/voltage/fan, so tuning advice would be noise (and
    # its one-click actions would 400). Offline severity is handled elsewhere.
    if s.get("device_type") == "braiins":
        return []

    age_s = time.time() - s["session_start"]
    samples = len(hist)
    latest = s["latest"]
    freq = latest.get("frequency", 525)
    volt = latest.get("coreVoltage", 1150)
    asic = latest.get("temp", 0)
    vr = latest.get("vrTemp", 0)
    autofan = latest.get("autofanspeed", 0)

    recs = []

    # Need a few minutes of data before tuning suggestions are meaningful.
    if age_s < 180 or samples < 30:
        recs.append({
            "id": "warming_up",
            "severity": "info",
            "title": "Gathering baseline data",
            "body": f"Wait {max(0, int(180 - age_s))}s — recommendations stabilize after ~3 min of polling.",
        })
        return recs

    # 1. CRIT: VR temp in danger zone
    if vr >= 65:
        new_v = _clamp(volt - 15, "coreVoltage")
        recs.append({
            "id": "vr_critical",
            "severity": "crit",
            "title": f"VR temp {vr:.0f}°C — danger zone",
            "body": f"VR is the part that kills boards. Drop core voltage to {new_v} mV and check airflow.",
            "action": {"type": "tune", "params": {"coreVoltage": new_v}, "label": f"set {new_v}mV"},
        })

    # 2. CRIT: HW error rate too high
    if shares_delta >= 20 and hw_rate_pct >= 1.0:
        new_v = _clamp(volt - 10, "coreVoltage")
        recs.append({
            "id": "hw_high",
            "severity": "crit",
            "title": f"HW error rate {hw_rate_pct:.2f}% — chip unstable",
            "body": f"Errors above 1% mean the chip is fighting the settings. Drop core voltage to {new_v} mV.",
            "action": {"type": "tune", "params": {"coreVoltage": new_v}, "label": f"set {new_v}mV"},
        })

    # 3. WARN: HW errors climbing (0.5 - 1%)
    elif shares_delta >= 20 and hw_rate_pct >= 0.5:
        new_v = _clamp(volt + 10, "coreVoltage")
        recs.append({
            "id": "hw_climbing",
            "severity": "warn",
            "title": f"HW errors at {hw_rate_pct:.2f}% — getting unstable",
            "body": f"You can either give it more voltage ({new_v} mV) for stability, or drop frequency to back off.",
            "action": {"type": "tune", "params": {"coreVoltage": new_v}, "label": f"add 10mV"},
        })

    # 4. WARN: ASIC running hot (and VR isn't already crit)
    if asic >= 65 and vr < 65:
        if autofan:
            recs.append({
                "id": "asic_hot",
                "severity": "warn",
                "title": f"ASIC at {asic:.0f}°C — hot",
                "body": "Auto-fan is on but the chip is still climbing. Consider dropping voltage 5–10 mV or improving case airflow.",
            })
        else:
            recs.append({
                "id": "asic_hot_manual",
                "severity": "warn",
                "title": f"ASIC at {asic:.0f}°C — hot",
                "body": "Switch fan to auto, or bump fan speed up.",
                "action": {"type": "tune", "params": {"autofanspeed": 1}, "label": "enable auto-fan"},
            })

    # 5. WARN: 5m hashrate noticeably below 15m — recent destabilization
    h5 = avgs.get("5m", 0)
    h15 = avgs.get("15m", 0)
    if h15 > 0 and h5 > 0 and (h5 / h15) < 0.92:
        recs.append({
            "id": "hash_dropping",
            "severity": "warn",
            "title": f"5m avg ({h5:.0f}) trailing 15m avg ({h15:.0f})",
            "body": "Hashrate destabilized recently. Reset the benchmark to re-baseline cleanly, or back off frequency 25 MHz.",
            "action": {"type": "reset_session", "label": "reset benchmark"},
        })

    # 6. GOOD: stable + low errors + has headroom → suggest pushing frequency
    has_headroom_freq = freq + 25 <= BOUNDS["frequency"][1]
    stable_long = age_s >= 900 and samples >= 150  # 15+ minutes of data
    if (stable_long and hw_rate_pct < 0.1 and vr < 60 and asic < 60
            and has_headroom_freq):
        new_f = _clamp(freq + 25, "frequency")
        recs.append({
            "id": "push_freq",
            "severity": "good",
            "title": "Stable for 15m+ with headroom",
            "body": f"Errors <0.1%, temps healthy. Try {new_f} MHz to push for more hashrate.",
            "action": {"type": "tune", "params": {"frequency": new_f}, "label": f"try {new_f}MHz"},
        })

    # 7. INFO: underperforming vs expected (only flag after 5 min of data)
    if (age_s >= 300 and expected_ghs > 0 and ghs > 0
            and (ghs / expected_ghs) < 0.85):
        pct = (ghs / expected_ghs) * 100
        recs.append({
            "id": "below_expected",
            "severity": "info",
            "title": f"Hashrate at {pct:.0f}% of expected",
            "body": "Could be silicon lottery, or chip is throttling on errors. Check HW error rate; if 0, this is just chip variance.",
        })

    # 8. GOOD: excellent efficiency, hold the line
    if (stable_long and j_per_th and j_per_th <= 16 and hw_rate_pct < 0.1
            and asic < 60 and vr < 60):
        recs.append({
            "id": "great_eff",
            "severity": "good",
            "title": f"Excellent efficiency — {j_per_th:.2f} J/TH",
            "body": "This is a solid operating point. Pushing harder may improve hashrate but cost efficiency.",
        })

    # For chips whose tuning bounds we haven't validated (e.g. BM1373 / Nexus
    # S1), the freq/voltage endpoints are locked (see _tuning_supported). Strip
    # the one-click action off any tuning rec so we don't render a button that
    # would just 400 — the advisory text (and its severity/health border) stays.
    model = str(latest.get("ASICModel", "")).strip()
    if model and model not in _TUNABLE_CHIPS:
        for r in recs:
            if isinstance(r.get("action"), dict) and r["action"].get("type") in ("tune", "preset"):
                r.pop("action", None)

    # Severity priority for trimming: crit > warn > good > info
    order = {"crit": 0, "warn": 1, "good": 2, "info": 3}
    recs.sort(key=lambda r: order.get(r["severity"], 9))
    return recs[:3]


# ---------- Solo block probability ----------
# Estimates the user's chance of finding a block at the current hashrate, plus
# how close their best-share difficulty is to the network's. Inferred chain
# (BTC / BCH / BSV / XEC / DGB / NMC) comes from the stratum URL; network
# difficulty and USD price come from free public APIs (mempool.space for BTC,
# blockchair for the rest), cached for 10 min since these values change slowly
# (BTC re-targets every ~2016 blocks, others vary).
#
# All six chains are SHA-256d — the same hashing the BM1370 already does. What
# changes per chain is the network difficulty (the "lottery odds"), the block
# subsidy, and the USD price; the hashrate math is identical.

# Block subsidies as of 2026. Pulled from the live block on each chain;
# refresh after the next halving epoch (BTC/BCH/BSV ~2028, XEC ~2028,
# DGB smoothly decreasing, NMC ~2028).
_BLOCK_REWARDS = {
    "btc": 3.125,
    "bch": 3.125,
    "bsv": 3.125,
    "xec": 3_125_000.0,   # eCash retained BCHA's 8-decimal redenomination — block subsidy is ~3.125M XEC
    "dgb": 575.0,         # Digibyte uses a smooth subsidy curve, ~575 at the time this code was written
    "nmc": 0.78125,       # NMC follows BTC's halving schedule (50 → 25 → 12.5 → 6.25 → 3.125 → 1.5625 → 0.78125 by 2024)
}

# Stratum URL needles → chain id. First match wins, so list more-specific
# chains before more-generic ones. Anything unmatched falls through to BTC.
#
# Coverage notes (2026-06-15 expansion):
# - DGB: added DGB-focused single-coin pools (digihash, dgbpool,
#   weminemore) and the DGB-leaning multi-coin pool letsmine.it
#   (Nathan's Bitaxe_004 case that prompted this audit).
# - XEC: added xeggex (multi-coin including XEC) and viabtc (the
#   biggest XEC pool by hashrate).
# - Don't add genuinely-multi-coin hostnames without port qualifiers
#   (e.g. coinotron, 2mars) — they'd misclassify other coins served
#   from the same host. We'd need a (host, port) → chain table for
#   those; punted until a user hits the case.
#
# Coverage notes (2026-08-17):
# - DGB: added the "digi." subdomain needle for hmpool (digi.hmpool.io).
#   hmpool is multi-coin, so we deliberately do NOT match the bare
#   "hmpool" host — the coin lives in the subdomain, and its BTC/BCH
#   siblings are already handled (bch.* by the BCH needle, btc.* by the
#   BTC fall-through).
_CHAIN_PATTERNS = [
    ("xec", ("xec.", "-xec.", "ecash", "bcha", "xeggex", "viabtc")),
    ("bsv", ("bsv.", "-bsv.", "bitcoin-sv", "bitcoin sv")),
    ("dgb", ("dgb.", "-dgb.", "digibyte", "digi.",
             "digihash", "dgbpool", "weminemore", "letsmine")),
    ("nmc", ("nmc.", "-nmc.", "namecoin")),
    # BCH last among the alts — its "bch" needle is a substring of common
    # solohash hostnames, which is OK because solohash already routes by
    # the port-3337 heuristic just below the pattern loop.
    ("bch", ("bch.", "-bch.", "bitcoin-cash", "bcash")),
]

_chain_stats_cache = {}   # chain_id → (fetched_at, stats_dict)
_CHAIN_TTL_SEC = 600

# Base58 alphabet (no 0/O/I/l). Used to sanity-check a legacy payout address
# so a worker name like "Downstairs-rig" can't masquerade as one.
_BASE58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _is_legacy_dgb_address(addr):
    """True for a DigiByte P2PKH address: base58, 'D' prefix, 34 chars.

    Among the SHA-256d chains this app supports, a leading 'D' is unique to
    DGB — BTC / BCH / BSV / XEC legacy addresses are '1' or '3', NMC is 'N'
    or 'M'. The historic reason for skipping this prefix was the DOGE
    collision, but DOGE is scrypt, so it can't be the target of any miner
    we monitor. That makes the prefix safe *here* even though it isn't
    safe in general.
    """
    return (
        len(addr) == 34
        and addr[:1] == "D"
        and all(c in _BASE58_ALPHABET for c in addr)
    )

def _infer_chain(stratum_url, stratum_port=0, stratum_user=""):
    """
    Detect the chain a miner is pointed at. Priority order, most reliable first:
      1. The stratumUser's payout-address prefix (CashAddr / BIP-21 style,
         or bech32 chain-tagged like 'dgb1...'). Pools require the worker
         name start with the actual payout address, so the address format
         is a definitive signal — "bitcoincash:..." is BCH, "ecash:..." is
         XEC, "dgb1..." is DGB. Pool URLs rebrand and multi-coin pools muddy
         the URL signal, but a chain-tagged address can only be one coin.
         Legacy base58 'D...' is read as DGB (see _is_legacy_dgb_address);
         the other legacy prefixes (BTC '1'/'3', LTC 'L') stay ambiguous
         across the chains we support, so the pool URL handles those.
      2. URL needles (legacy multi-coin pool subdomains like xec.pool.com).
      3. solohash.co.uk port heuristic (3337 = BCH, 3333 = BTC).
      4. Fall through to BTC.
    """
    user_raw = str(stratum_user or "")
    user = user_raw.lower()
    if user.startswith("bitcoincash:") or user.startswith("bchtest:"):
        return "bch"
    if user.startswith("ecash:"):
        return "xec"
    # DGB bech32 addresses start with 'dgb1' — unambiguous.
    if user.startswith("dgb1"):
        return "dgb"
    # Legacy base58 DGB address. Worker names are "<address>.<rig-name>", so
    # the address is everything before the first dot (case-sensitive — base58
    # is, and 'D' vs 'd' is the whole signal).
    if _is_legacy_dgb_address(user_raw.split(".", 1)[0]):
        return "dgb"
    if not stratum_url:
        return "btc"
    u = str(stratum_url).lower()
    # solohash.co.uk uses port 3337 for BCH (port 3333 is BTC). The host alone
    # doesn't tell us which, so peek at the port.
    if "solohash" in u and str(stratum_port) == "3337":
        return "bch"
    for chain, needles in _CHAIN_PATTERNS:
        if any(n in u for n in needles):
            return chain
    return "btc"


# Test fixtures for chain detection — real-world (URL, port, user) tuples
# from production miners + known pool docs. Used by tests/test_chain.py
# to catch regressions when we touch _CHAIN_PATTERNS. New chains should
# add at least one entry; new pool URLs that flow through here should
# also append so they're protected forever.
_CHAIN_INFERENCE_FIXTURES = [
    # (label, url, port, user, expected_chain)
    ("Public Pool BTC",        "public-pool.io",          21496, "bc1q...x.worker",        "btc"),
    ("Ocean BTC",              "mine.ocean.xyz",          3334,  "bc1q...x.worker",        "btc"),
    ("CKPool BCH",             "bch.ckpool.org",          3333,  "bitcoincash:qz...x",     "bch"),
    ("solohash BCH (port)",    "ng.solohash.co.uk",       3337,  "qz...x.worker",          "bch"),
    ("solohash BTC (port)",    "ng.solohash.co.uk",       3333,  "bc1q...x.worker",        "btc"),
    ("letsmine DGB (Nathan)",  "us1.letsmine.it",         3335,  "DALF5...Bitaxe_004",     "dgb"),
    ("digihash DGB",           "pool.digihash.co",        3008,  "DABCxyz.worker",         "dgb"),
    ("dgb1 bech32 worker",     "any.pool.example",        3333,  "dgb1qabcxyz.worker",     "dgb"),
    # hmpool DGB — Nathan's whole fleet, 2026-08-17. Caught by the "digi."
    # subdomain needle AND by the legacy 'D' address, independently.
    ("hmpool DGB (Bitaxe)",    "digi.hmpool.io",          3334,  "DALF5mgtRW8of8zdfwY69aGtR7NtrRpyy1.bitaxe001", "dgb"),
    ("hmpool DGB (NerdQAxe)",  "digi.hmpool.io",          3336,  "DALF5mgtRW8of8zdfwY69aGtR7NtrRpyy1.nerdaxe001", "dgb"),
    # Legacy 'D' address alone is enough, even on an unknown host.
    ("legacy D addr, no dot",  "unknown.pool.example",    3333,  "DALF5mgtRW8of8zdfwY69aGtR7NtrRpyy1", "dgb"),
    # ...but it must actually look like an address. These must NOT be DGB:
    ("D worker name only",     "public-pool.io",          21496, "Downstairs.rig-01",      "btc"),
    ("D-prefix, wrong length", "public-pool.io",          21496, "DALF5mgtRW8of8zdfwY69aGtR7Ntr.w", "btc"),
    ("D-prefix, non-base58",   "public-pool.io",          21496, "D0ALF5mgtRW8of8zdfwY69aGtR7NtrRpy.w", "btc"),
    # hmpool is multi-coin — the sibling subdomains must still route right.
    ("hmpool BCH sibling",     "bch.hmpool.io",           3334,  "bitcoincash:qz...x",     "bch"),
    ("ecash: address",         "any.pool.example",        3333,  "ecash:qz...x",           "xec"),
    ("viabtc XEC",             "xec.viabtc.com",          3333,  "user.worker",            "xec"),
    ("bsv pool",               "stratum.bsv.example",     3333,  "1abc...x.worker",        "bsv"),
    ("nmc pool",               "namecoin.example",        3333,  "N...x.worker",           "nmc"),
    ("unmatched → BTC",        "weirdpool.example",       3333,  "bc1q...x.worker",        "btc"),
]


def _parse_diff(value):
    """Firmware reports diffs as strings like '1.68G', '16.69G', '682.42M'. Returns float."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s == "0":
        return 0.0
    mult = 1.0
    if s and s[-1] in "kKmMgGtTpP":
        mult = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except (ValueError, TypeError):
        return 0.0

def _fetch_btc_stats():
    try:
        tip_hash = requests.get("https://mempool.space/api/blocks/tip/hash", timeout=4).text.strip()
        block = requests.get(f"https://mempool.space/api/block/{tip_hash}", timeout=4).json()
        prices = requests.get("https://mempool.space/api/v1/prices", timeout=4).json()
        return {
            "chain": "btc", "name": "Bitcoin", "symbol": "BTC",
            "difficulty": float(block.get("difficulty", 0)),
            "reward": _BLOCK_REWARDS["btc"],
            "priceUsd": float(prices.get("USD", 0)),
        }
    except Exception as e:
        print(f"[block-prob] BTC stats fetch failed: {e}")
        return None

def _fetch_blockchair_stats(chain_id, slug, name, symbol):
    """Shared blockchair fetcher — BCH and XEC both expose /<slug>/stats
    with the same field shape. Returns None for chains not on the free tier
    (BSV / DGB / NMC are 404 → they use the alt fetchers below)."""
    try:
        r = requests.get(f"https://api.blockchair.com/{slug}/stats", timeout=4).json()
        d = r.get("data", {})
        return {
            "chain": chain_id, "name": name, "symbol": symbol,
            "difficulty": float(d.get("difficulty", 0)),
            "reward": _BLOCK_REWARDS[chain_id],
            "priceUsd": float(d.get("market_price_usd", 0)),
        }
    except Exception as e:
        print(f"[block-prob] {symbol} stats fetch failed: {e}")
        return None

def _fetch_coingecko_price(slug):
    """CoinGecko's free /simple/price endpoint. No auth, no key, low rate-limit
    but we only hit it once per cache TTL (10 min) per chain."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": slug, "vs_currencies": "usd"},
            timeout=4,
        ).json()
        return float(r.get(slug, {}).get("usd", 0))
    except Exception as e:
        print(f"[block-prob] CoinGecko {slug} price fetch failed: {e}")
        return 0.0

def _fetch_bch_stats(): return _fetch_blockchair_stats("bch", "bitcoin-cash", "Bitcoin Cash", "BCH")
def _fetch_xec_stats(): return _fetch_blockchair_stats("xec", "ecash",        "eCash",        "XEC")

def _fetch_bsv_stats():
    # WhatsOnChain is BSV's canonical block explorer — `/chain/info` returns
    # the live difficulty. Price comes from CoinGecko (the `bitcoin-cash-sv`
    # id is correct as of 2026; BSV's CG slug changed once historically).
    try:
        info = requests.get("https://api.whatsonchain.com/v1/bsv/main/chain/info", timeout=4).json()
        return {
            "chain": "bsv", "name": "Bitcoin SV", "symbol": "BSV",
            "difficulty": float(info.get("difficulty", 0)),
            "reward": _BLOCK_REWARDS["bsv"],
            "priceUsd": _fetch_coingecko_price("bitcoin-cash-sv"),
        }
    except Exception as e:
        print(f"[block-prob] BSV stats fetch failed: {e}")
        return None

def _fetch_chainz_difficulty(chain_path):
    """chainz.cryptoid.info exposes per-chain `getdifficulty` as a bare number
    string. No auth, no key, no rate-limit at our cadence. Note: for
    multi-algo chains (like DGB), this returns the aggregate / active-algo
    diff, not algo-specific — see the DGB caveat in the release notes."""
    try:
        r = requests.get(f"https://chainz.cryptoid.info/{chain_path}/api.dws", params={"q": "getdifficulty"}, timeout=4)
        return float(r.text.strip())
    except Exception as e:
        print(f"[block-prob] chainz {chain_path} difficulty fetch failed: {e}")
        return 0.0

def _fetch_dgb_stats():
    # DGB is multi-algo (Scrypt / SHA-256 / Qubit / Skein / Odo); chainz
    # returns an aggregate diff. The SHA-256 algo-specific diff isn't on a
    # free public API I could find, so we use the aggregate as a reasonable
    # proxy. Flagged in the release notes as a known limitation.
    diff = _fetch_chainz_difficulty("dgb")
    if diff <= 0:
        return None
    return {
        "chain": "dgb", "name": "DigiByte", "symbol": "DGB",
        "difficulty": diff,
        "reward": _BLOCK_REWARDS["dgb"],
        "priceUsd": _fetch_coingecko_price("digibyte"),
    }

def _fetch_nmc_stats():
    # Namecoin is merge-mined with BTC — the same hashes that win BTC blocks
    # also win NMC blocks at NMC's network difficulty. Network diff comes back
    # close to BTC's because that's literally what miners are aiming at.
    # The math we expose is still useful (per-hash odds at the device's
    # hashrate) but a savvy reader should remember: if you're solo NMC,
    # you're getting BTC rewards too whenever a BTC block hits.
    #
    # Source fallback: chainz.cryptoid.info silently dropped NMC sometime
    # around mid-2026 ("Blockchain 'nmc' unknown or hosting expired") and
    # there's no clean free alternative for Namecoin-specific difficulty.
    # Since NMC is merge-mined with BTC, BTC's difficulty is virtually
    # identical (small lag during retargets, doesn't matter for years-to-
    # block math). Use chainz when it works, BTC's diff when it doesn't.
    diff = _fetch_chainz_difficulty("nmc")
    if diff <= 0:
        btc_stats = _CHAIN_FETCHERS["btc"]() if _CHAIN_FETCHERS.get("btc") else None
        if btc_stats and btc_stats.get("difficulty", 0) > 0:
            diff = btc_stats["difficulty"]
        else:
            return None
    return {
        "chain": "nmc", "name": "Namecoin", "symbol": "NMC",
        "difficulty": diff,
        "reward": _BLOCK_REWARDS["nmc"],
        "priceUsd": _fetch_coingecko_price("namecoin"),
    }

_CHAIN_FETCHERS = {
    "btc": _fetch_btc_stats,
    "bch": _fetch_bch_stats,
    "bsv": _fetch_bsv_stats,
    "xec": _fetch_xec_stats,
    "dgb": _fetch_dgb_stats,
    "nmc": _fetch_nmc_stats,
}

_chain_wanted = set()   # chains any device is using — the background refresher keeps these warm


def _chain_stats(chain_id):
    """NON-BLOCKING. Returns cached stats (or None if not fetched yet), and records the
    chain as 'wanted' so the background refresher keeps it warm. It must NEVER do network
    I/O inline: device_summary calls this while holding state_lock, so a synchronous fetch
    here would hold the lock across a slow chain-API call and freeze every device poll AND
    /api/devices (which builds summaries under the same lock) — the recurring 'not loading
    data' hang. The refresh happens off-thread in _chain_stats_refresh_loop instead."""
    _chain_wanted.add(chain_id)
    cached = _chain_stats_cache.get(chain_id)
    return cached[1] if cached else None


def _chain_stats_refresh_loop():
    """Keep chain difficulty/price stats warm OFF the request/poll path. Wakes periodically
    and (re)fetches only chains that are wanted and stale, so a slow or down chain API can
    never hold state_lock. A newly-used chain's stats populate within one interval."""
    while True:
        try:
            now = time.time()
            for chain_id in list(_chain_wanted):
                cached = _chain_stats_cache.get(chain_id)
                if cached and (now - cached[0]) < _CHAIN_TTL_SEC:
                    continue
                fetcher = _CHAIN_FETCHERS.get(chain_id)
                if not fetcher:
                    continue
                try:
                    fresh = fetcher()   # network I/O — in THIS bg thread, never under a lock
                    if fresh:
                        _chain_stats_cache[chain_id] = (time.time(), fresh)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(20)

def _block_probability_math(hashrate_ghs, network_diff, best_diff_value):
    """
    For solo mining: expected blocks/sec = hashrate / (2^32 × difficulty).
    Returns daily/monthly/yearly '1 in X' integers + a 0..1 proximity ratio.
    Proximity uses log10(best+1) / log10(diff+1) so the bar moves linearly
    across "orders of magnitude closer to a block" rather than linearly in
    raw difficulty — which would look stuck at zero forever.
    """
    if hashrate_ghs <= 0 or network_diff <= 0:
        return None
    per_sec = (hashrate_ghs * 1e9) / ((2 ** 32) * network_diff)
    if per_sec <= 0:
        return None
    import math
    daily_n   = max(1, round(1 / (per_sec * 86400)))
    monthly_n = max(1, round(1 / (per_sec * 86400 * 30)))
    yearly_n  = max(1, round(1 / (per_sec * 86400 * 365)))
    if best_diff_value > 0 and network_diff > 1:
        proximity = max(0.0, min(1.0,
            math.log10(best_diff_value + 1) / math.log10(network_diff + 1)))
    else:
        proximity = 0.0
    return {
        "dailyOneIn":   daily_n,
        "monthlyOneIn": monthly_n,
        "yearlyOneIn":  yearly_n,
        "proximity":    round(proximity, 4),
    }

# Chains whose only free difficulty source is NOT the algo a Bitaxe actually
# mines, so solo-block odds derived from it would be badly misleading.
# DigiByte is multi-algo (Scrypt / SHA-256 / Qubit / Skein / Odocrypt) and
# chainz's `getdifficulty` returns an aggregate / last-block value — not the
# SHA-256 diff the Bitaxe competes against. Using it makes a ~6 TH/s miner look
# like it should solve a block every few minutes (real answer: weeks–months).
# Until a free SHA-256-specific DGB difficulty source is wired in, we still show
# the chain + reward, but suppress the (wrong) odds rather than mislead.
# See _fetch_dgb_stats and _block_probability_math.
_ODDS_UNRELIABLE_CHAINS = {"dgb"}

_ODDS_UNRELIABLE_NOTE = (
    "DigiByte is multi-algorithm — SHA-256 is one of its five algorithms. A free "
    "SHA-256-specific network-difficulty source isn't available yet, so solo-block "
    "odds computed from the aggregate difficulty would be wildly optimistic and are "
    "hidden. Your miners are working normally; this only affects the odds display."
)


def _solo_block_payload(stratum_url, stratum_port, hashrate_ghs, best_diff_str, stratum_user="", chain=None):
    """Top-level builder for the device_summary 'blockProbability' field. None if unavailable.
    `chain` pins the chain (manual override); falls back to auto-detection when None."""
    chain_id = chain or _infer_chain(stratum_url, stratum_port, stratum_user)
    stats = _chain_stats(chain_id)
    if not stats:
        return None
    prob = _block_probability_math(hashrate_ghs, stats["difficulty"], _parse_diff(best_diff_str))
    if not prob:
        return None
    reliable = chain_id not in _ODDS_UNRELIABLE_CHAINS
    payload = {
        "chain":        stats["chain"],
        "chainName":    stats["name"],
        "symbol":       stats["symbol"],
        "difficulty":   stats["difficulty"],
        "reward":       stats["reward"],
        "priceUsd":     stats["priceUsd"],
        "rewardUsd":    round(stats["reward"] * stats["priceUsd"], 2),
        "oddsReliable": reliable,
    }
    if reliable:
        payload.update(prob)
    else:
        # Suppress the derived odds/ETA/proximity — every one of them is a
        # function of the wrong (aggregate) difficulty. Keep the fields present
        # (as None) so older frontends don't blow up on missing keys.
        payload.update({"dailyOneIn": None, "monthlyOneIn": None,
                        "yearlyOneIn": None, "proximity": None,
                        "oddsNote": _ODDS_UNRELIABLE_NOTE})
    return payload


def device_summary(s, elec=None):
    if not s["latest"]:
        # Device has never been polled successfully (`latest` stays populated
        # after a device goes offline, so this branch only fires for fresh
        # adds). Default chain to "btc" since we have no stratum info.
        return {
            "ip": s["ip"],
            "label": s["label"],
            "online": s["online"],
            "lastError": s["last_error"],
            "deviceType": s.get("device_type", "axeos"),
            "history": [],
            "events": list(s["events"]),
            "recommendations": [],
            "severity": "crit" if not s["online"] else None,
            "chain": "btc",
        }

    latest = s["latest"]
    hist = list(s["history"])
    avgs = {name: rolling_avg(s["history"], n) for name, n in ROLLING_WINDOWS.items()}

    hw_delta = 0
    shares_delta = 0
    if s["session_shares_start"] is not None and hist:
        latest_pt = hist[-1]
        shares_delta = latest_pt["sharesAccepted"] - s["session_shares_start"]
        hw_delta = latest_pt["sharesRejected"] - (s["session_hwerrors_start"] or 0)

    hw_rate_pct = 0
    if shares_delta + hw_delta > 0:
        hw_rate_pct = (hw_delta / (shares_delta + hw_delta)) * 100

    ghs = latest.get("hashRate", 0)
    power = latest.get("power", 0)
    j_per_th = (power / (ghs / 1000)) if ghs > 0 else 0

    # Estimated electricity cost. Resolve the rate once per /api/devices call
    # (passed in by the caller) so a big fleet doesn't re-read config.json per
    # device; fall back to a fresh read for the single-device endpoint.
    elec_rate, elec_currency = elec if elec is not None else _electricity_cfg()
    cost = _cost_block(power, elec_rate, elec_currency)

    # Expected hashrate. Three sources, in priority order:
    #   1. firmware-reported `expectedHashrate` — chip-aware, matches AxeOS
    #   2. computed from `smallCoreCount × frequency / 1000` — same firmware formula,
    #      handy if a future firmware drops the convenience field
    #   3. flat 2.04 GH/s/MHz fallback — the empirical real-world average for the
    #      BM1370 Gamma. Old code used 2.28 (Bitmain's theoretical spec peak),
    #      which made every miner look like it was underperforming.
    freq = latest.get("frequency", 0) or 0
    firmware_expected = latest.get("expectedHashrate", 0) or 0
    small_cores = latest.get("smallCoreCount", 0) or 0
    # Multi-ASIC boards (e.g. NerdQAxe++ = 4× BM1370) report per-chip smallCoreCount
    # but whole-board hashRate. Scale the computed expectation by asicCount so a 4-ASIC
    # board isn't shown at ~400% of "expected" (which also poisons fleet-outlier stats).
    # Gammas report asicCount 1 (or omit it) → no change. firmware_expected, when present,
    # is already whole-board so it's used as-is.
    asic_count = latest.get("asicCount", 1) or 1
    if firmware_expected:
        expected_ghs = firmware_expected
    elif small_cores and freq:
        expected_ghs = (small_cores * freq) / 1000 * asic_count
    else:
        expected_ghs = (freq * 2.04 * asic_count) if freq > 0 else 0

    session_secs = max(1, time.time() - s["session_start"])
    shares_per_min = (shares_delta / (session_secs / 60)) if session_secs >= 60 else 0

    recs = compute_recommendations(s, hist, avgs, hw_rate_pct, shares_delta, j_per_th, ghs, expected_ghs)

    best_diff_value = _parse_diff(latest.get("bestDiff", "0"))
    best_session_diff_value = _parse_diff(latest.get("bestSessionDiff", "0"))
    # Manual chain pin wins over auto-detection. The detector can't tell a BCH
    # legacy address from a BTC one (identical format) on a private-IP pool, so
    # users pointing miners at their own node need to pin it.
    chain_id = s.get("chain_override") or _infer_chain(
        latest.get("stratumURL", ""), latest.get("stratumPort", 0), latest.get("stratumUser", "")
    )
    return {
        "ip": s["ip"],
        "label": s["label"],
        "online": s["online"],
        "lastError": s["last_error"],
        "deviceType": s.get("device_type", "axeos"),
        "model": latest.get("ASICModel", "unknown"),
        # Whether Baller offers frequency/voltage tuning for this chip. False for
        # identified ASICs whose bounds we haven't validated (e.g. BM1373 / Nexus
        # S1) so the UI hides the tune panel. Computed inline (not via
        # _tuning_supported, which takes state_lock) — device_summary already
        # runs under that lock.
        "tuningSupported": (lambda m: (not m) or (m in _TUNABLE_CHIPS))(
            str(latest.get("ASICModel", "")).strip()
        ),
        "version": latest.get("version", ""),
        "hostname": latest.get("hostname", ""),
        "macAddr": latest.get("macAddr", ""),
        "metrics": {
            "hashRate": round(ghs, 1),
            "temp": round(latest.get("temp", 0), 1),
            "vrTemp": round(latest.get("vrTemp", 0), 1),
            "power": round(power, 2),
            "voltage": latest.get("voltage", 0),
            "coreVoltage": latest.get("coreVoltage", 0),
            "frequency": latest.get("frequency", 0),
            "fanSpeed": int(latest.get("fanrpm", 0) or 0),
            "fanPercent": round(latest.get("fanspeed", 0) or 0),
            "autofanspeed": int(latest.get("autofanspeed", 0) or 0),
            "sharesAccepted": latest.get("sharesAccepted", 0),
            "sharesRejected": latest.get("sharesRejected", 0),
            "bestDiff": latest.get("bestDiff", "0"),
            "bestDiffValue": best_diff_value,
            "bestSessionDiff": latest.get("bestSessionDiff", "0"),
            "bestSessionDiffValue": best_session_diff_value,
            "poolDifficulty": latest.get("poolDifficulty", 0),
            "uptime": latest.get("uptimeSeconds", 0),
            "stratumUrl": latest.get("stratumURL", ""),
        },
        "shares": {
            "sessionAccepted": max(0, shares_delta),
            "sessionRejected": max(0, hw_delta),
            "lifetimeAccepted": latest.get("sharesAccepted", 0),
            "lifetimeRejected": latest.get("sharesRejected", 0),
            "perMin": round(shares_per_min, 2),
            "sessionSecs": int(session_secs),
        },
        "rolling": {k: round(v, 1) for k, v in avgs.items()},
        "efficiency": {
            "jPerTh": round(j_per_th, 2),
            "expectedGhs": round(expected_ghs, 1),
            "actualPctOfExpected": round((ghs / expected_ghs * 100) if expected_ghs > 0 else 0, 1),
        },
        "cost": cost,
        "hwErrors": {
            "shares": shares_delta,
            "hwErrors": hw_delta,
            "ratePct": round(hw_rate_pct, 3),
        },
        "stratum": {
            "url": latest.get("stratumURL", ""),
            "port": latest.get("stratumPort", 0),
            "user": latest.get("stratumUser", ""),
            "tls": int(latest.get("stratumTLS", 0) or 0),
            "suggestedDifficulty": latest.get("stratumSuggestedDifficulty", 0),
            "fallbackUrl": latest.get("fallbackStratumURL", ""),
            "fallbackPort": latest.get("fallbackStratumPort", 0),
            "fallbackUser": latest.get("fallbackStratumUser", ""),
            "fallbackTls": int(latest.get("fallbackStratumTLS", 0) or 0),
            "fallbackSuggestedDifficulty": latest.get("fallbackStratumSuggestedDifficulty", 0),
            "usingFallback": int(latest.get("isUsingFallbackStratum", 0) or 0),
            "connectionInfo": latest.get("poolConnectionInfo", ""),
        },
        "recommendations": recs,
        "severity": _max_severity(recs),
        "autotune": _autotune_summary(s),
        "chain": chain_id,
        "chainOverride": s.get("chain_override") or None,
        "blockProbability": _solo_block_payload(
            latest.get("stratumURL", ""),
            latest.get("stratumPort", 0),
            ghs,
            latest.get("bestDiff", "0"),
            latest.get("stratumUser", ""),
            chain=chain_id,
        ),
        "history": [
            {
                "t": p["t"],
                "h": round(p["hashRate"], 1),
                "asic": round(p["temp"], 1),
                "vr": round(p["vrTemp"], 1),
                "p": round(p["power"], 2),
            }
            for p in hist[-180:]
        ],
        "events": [{"t": e["t"], "msg": e["msg"]} for e in list(s["events"])[:10]],
        "shareEvents": [dict(e) for e in list(s["share_events"])[:50]],
    }


# ---------- Block-found celebration ----------
#
# The firmware exposes a solved-block counter that increments each time the
# device actually solves a network block (validated by the pool) — spelled
# `blockFound` on stock AxeOS, `totalFoundBlocks`/`foundBlocks` on the
# NerdQAxe fork; _device_block_count papers over the difference.
# When that counter ticks up between polls we treat it as a real, live,
# "the lottery hit" event — the rarest, most celebratory thing that can
# happen in solo mining — and persist a record so the dashboard can throw
# confetti until the user explicitly acknowledges it.
#
# Persistence model: plain JSON file in the data dir. Block finds are
# rare enough (BTC: never on a single Bitaxe in a human lifetime; XEC /
# DGB: meaningfully possible) that ~10 records/year per user is the
# upper bound. JSON is dead simple, atomic via write-temp-then-rename,
# survives restarts, no schema migrations. SQLite would be overkill.
#
# Free for everyone — this is delight, not a Pro hook. The Discord push
# on find IS Pro-gated (rides the existing alert pipeline) but the
# on-screen confetti fires for all users.

BLOCK_FINDS_PATH = os.path.join(_DATA_DIR, "block_finds.json")
# RLock (reentrant), NOT plain Lock — _block_finds_pending / _recent /
# _record / _ack all acquire this lock and then call _block_finds_load()
# which also acquires it. With a plain Lock that's an instant deadlock
# on the first request (caught in v1.16.0 within an hour of release).
_block_finds_lock = threading.RLock()
_block_finds_cache: list | None = None  # lazy-loaded on first read


def _block_finds_load() -> list:
    """Lazy-load + cache the block_finds list from disk. Returns a list of
    dicts; on a fresh install (file missing) returns an empty list."""
    global _block_finds_cache
    if _block_finds_cache is not None:
        return _block_finds_cache
    with _block_finds_lock:
        if _block_finds_cache is not None:
            return _block_finds_cache
        if not os.path.exists(BLOCK_FINDS_PATH):
            _block_finds_cache = []
            return _block_finds_cache
        try:
            with open(BLOCK_FINDS_PATH, "r") as f:
                data = json.load(f)
            _block_finds_cache = list(data.get("finds", []))
        except (json.JSONDecodeError, OSError):
            # Corrupt file — rare, but losing all block-find history would
            # be devastating, so we move it aside before starting fresh.
            try:
                os.rename(BLOCK_FINDS_PATH, BLOCK_FINDS_PATH + ".corrupt")
            except OSError:
                pass
            _block_finds_cache = []
        return _block_finds_cache


def _block_finds_save() -> None:
    """Persist the in-memory list to disk atomically (write tmp + rename
    so we can't half-write on a crash). Caller must hold _block_finds_lock."""
    if _block_finds_cache is None:
        return
    tmp = BLOCK_FINDS_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"finds": _block_finds_cache}, f, indent=2)
        os.replace(tmp, BLOCK_FINDS_PATH)
    except OSError as e:
        print(f"[block-finds] save failed: {e}", file=sys.stderr)


# Firmware counter field names, in preference order. Stock Bitaxe AxeOS uses
# `blockFound`; the NerdQAxe fork (shufps/ESP-Miner-NerdQAxePlus) has no such
# field at all and uses `foundBlocks` / `totalFoundBlocks` instead — so every
# NerdQAxe block find was silently invisible. Found the hard way: Nathan's
# nerdaxe_001 solved two DGB blocks in three days (2026-08-15, 2026-08-18)
# and the app showed nothing either time.
#
# `totalFoundBlocks` before `foundBlocks`: the "total" is the lifetime count
# that survives a reboot. A session-scoped counter resets to 0 on restart and
# climbs back up, which would re-fire the celebration for an old block.
_BLOCK_COUNT_FIELDS = ("blockFound", "totalFoundBlocks", "foundBlocks")


def _device_block_count(latest):
    """The device's solved-block counter, or None if this firmware exposes no
    such field.

    None is NOT 0. A device reporting 0 is telling us it has found nothing; a
    device reporting nothing at all must not be baselined as 0 — treating the
    two alike is exactly what disabled detection on the NerdQAxe fork.
    """
    for key in _BLOCK_COUNT_FIELDS:
        val = latest.get(key)
        if val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def _block_find_transition(prev, cur):
    """Decide what a new counter reading means. Returns (new_prev, delta);
    delta > 0 is a real find to celebrate, 0 means do nothing.

      cur is None  -> firmware has no counter; hold prev unchanged.
      prev is None -> first poll this session; baseline only, never fire.
      cur < prev   -> counter went backwards: firmware reset/wipe, or a
                      firmware update moved us to a different source field.
                      Re-baseline. Ignoring it instead would mean the climb
                      back up to the old value reads as "no new blocks" and
                      we'd miss the next real find.
      cur > prev   -> real find(s); delta covers catch-up if we were away.
    """
    if cur is None:
        return prev, 0
    if prev is None or cur < prev:
        return cur, 0
    if cur > prev:
        return cur, cur - prev
    return prev, 0


def _fleet_block_height(chain_id):
    """Best-known chain tip for `chain_id` across the fleet, or 0.

    The NerdQAxe fork doesn't report `blockHeight`, so a find on one of those
    records height 0 — but any other device on the SAME chain has the number.
    Chain-scoped on purpose: a mixed BTC+DGB fleet would otherwise stamp a BTC
    find with DGB's (much larger) height. Caller must hold state_lock.
    """
    heights = []
    for st in state.values():
        latest = st.get("latest") or {}
        other = st.get("chain_override") or _infer_chain(
            latest.get("stratumURL", ""),
            latest.get("stratumPort", 0),
            latest.get("stratumUser", ""),
        )
        if other != chain_id:
            continue
        try:
            h = int(latest.get("blockHeight") or 0)
        except (TypeError, ValueError):
            continue
        if h > 0:
            heights.append(h)
    return max(heights) if heights else 0


def _block_finds_record(ip: str, label: str, latest: dict, chain_id: str,
                        height_fallback: int = 0) -> dict:
    """Append a new block-find record to the persistent list. Returns the
    record (with the id assigned) so callers can attach it to the device
    event log and/or fire downstream notifications. Idempotent on its own
    inputs but the caller must gate on a real prev_block_found delta.

    `height_fallback` is used when the device's own firmware doesn't report
    `blockHeight` (the NerdQAxe fork doesn't) — see _fleet_block_height."""
    rec = {
        # Monotonic id is enough since this list is per-install. UUID4 would
        # work too but adds a dep + we don't need cross-install uniqueness.
        "id": f"bf-{int(time.time() * 1000)}-{ip.replace('.', '-')}",
        "device_ip": ip,
        "device_label": label or latest.get("hostname") or ip,
        "mac_addr": (latest.get("macAddr") or "").upper(),
        "chain": chain_id,
        "chain_name": {
            "btc": "Bitcoin", "bch": "Bitcoin Cash", "bsv": "Bitcoin SV",
            "xec": "eCash",   "dgb": "DigiByte",     "nmc": "Namecoin",
        }.get(chain_id, chain_id.upper()),
        "block_height": int(latest.get("blockHeight") or 0) or int(height_fallback or 0),
        "best_diff": str(latest.get("bestDiff") or "0"),
        "best_session_diff": str(latest.get("bestSessionDiff") or "0"),
        "found_at": int(time.time()),
        "acknowledged": False,
    }
    with _block_finds_lock:
        _block_finds_load()
        _block_finds_cache.append(rec)  # type: ignore[union-attr]
        _block_finds_save()
    return rec


def _block_finds_ack(find_id: str) -> bool:
    """Mark a block-find as acknowledged (dashboard dismiss). Returns True
    if the id existed and was newly ack'd, False otherwise."""
    with _block_finds_lock:
        finds = _block_finds_load()
        for f in finds:
            if f.get("id") == find_id and not f.get("acknowledged"):
                f["acknowledged"] = True
                f["acknowledged_at"] = int(time.time())
                _block_finds_save()
                return True
    return False


def _block_finds_pending() -> list:
    """Unack'd block finds. The dashboard checks this and shows the confetti
    overlay if non-empty. Newest first."""
    with _block_finds_lock:
        finds = _block_finds_load()
        return sorted(
            (dict(f) for f in finds if not f.get("acknowledged")),
            key=lambda r: r.get("found_at", 0),
            reverse=True,
        )


def _block_finds_recent(limit: int = 20) -> list:
    """All recent finds, ack'd or not. For the device card badge + history."""
    with _block_finds_lock:
        finds = _block_finds_load()
        return sorted(
            (dict(f) for f in finds),
            key=lambda r: r.get("found_at", 0),
            reverse=True,
        )[:limit]


# ---------- Alerts (Pro) ----------
#
# v1 scope: Discord webhook channel + three rule types (offline, VR temp,
# ASIC temp). 30-min cooldown per (device, trigger) pair so a hot Gamma
# doesn't spam the channel every 5 seconds.
#
# v2 (v1.16.0): email channel routed through bitaxeballer.com. The site
# server (which has the Resend API key) validates the license and sends
# via Resend. Keeps the API key off user desktops.
#
# Future: SMS via Twilio, mobile push via APNs+FCM (after the mobile
# companion stabilizes), HW-error-rate-sustained trigger, custom rules.
ALERTS_DEFAULT_CONFIG = {
    "enabled": True,
    "channels": {
        "discord_webhook": "",  # https://discord.com/api/webhooks/<id>/<token>
        "email_to": "",         # one address; routed through bitaxeballer.com
    },
    "rules": {
        "offline_minutes": 5,
        "vr_temp_c": 65,
        "asic_temp_c": 65,
    },
    "cooldown_minutes": 30,
}

alerts_lock = threading.Lock()
# Per-device alert state: keys are device IPs, values are dicts mapping a
# trigger identifier ("offline", "vr_temp", "asic_temp") to the unix ts of the
# last time we fired that alert. Used purely for cooldown gating.
_alerts_last_fired: dict = {}

# Quiet period after process start. Prevents the post-restart storm: when
# the app launches fresh, the in-memory _alerts_last_fired dict is empty,
# so the very first poll cycle would fire an alert for every device whose
# state currently meets a trigger (caught in v1.16.0 — Nathan got 5 emails
# the moment he saved his address because the app had just restarted into
# v1.16.1 and the cooldown table was empty). Suppress ALL channel firings
# for the first 60s so the poll loop can baseline what's currently true
# without surprising the user.
ALERTS_STARTUP_QUIET_S = 60
_alerts_startup_at = time.time()


def _alerts_get_config() -> dict:
    with config_lock:
        cfg = load_config()
    a = cfg.get("alerts") or {}
    # Merge with defaults so partial config doesn't error on a missing key.
    out = json.loads(json.dumps(ALERTS_DEFAULT_CONFIG))
    if isinstance(a.get("channels"), dict):
        out["channels"].update(a["channels"])
    if isinstance(a.get("rules"), dict):
        out["rules"].update(a["rules"])
    if "enabled" in a:
        out["enabled"] = bool(a["enabled"])
    if "cooldown_minutes" in a:
        try:
            out["cooldown_minutes"] = int(a["cooldown_minutes"])
        except (TypeError, ValueError):
            pass
    return out


def _alerts_save_config(new_cfg: dict) -> None:
    with config_lock:
        cfg = load_config()
        cfg["alerts"] = new_cfg
        save_config(cfg)


def _alerts_post_discord(webhook_url: str, title: str, body: str) -> tuple:
    """Send an alert to a Discord webhook. Returns (ok, msg). Never raises —
    we don't want a network blip in the alert pipeline to disrupt polling."""
    if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
        return False, "Invalid or missing Discord webhook URL"
    payload = {
        "username": "Bitaxe Baller",
        "embeds": [{
            "title": title[:256],
            "description": body[:2000],
            "color": 0xff3860,  # red-ish — matches the crit theme
        }],
    }
    try:
        r = requests.post(
            webhook_url,
            json=payload,
            timeout=5,
            headers={"User-Agent": f"BitaxeBaller/{APP_VERSION}"},
        )
        if r.status_code in (200, 204):
            return True, "delivered"
        return False, f"Discord HTTP {r.status_code}: {r.text[:80]}"
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


# Email alert relay through bitaxeballer.com. The site validates the
# license + sends via Resend, which keeps the Resend API key OFF user
# machines (publishing it would let anyone empty our send quota). The
# desktop just sends a small JSON blob; the site is the trusted side.
_ALERTS_EMAIL_RELAY_URL = "https://bitaxeballer.com/api/alerts/email"

def _alerts_post_email(email_to: str, title: str, body: str) -> tuple:
    """Forward an alert to the user's email through the bitaxeballer.com
    relay. Returns (ok, msg). Like _alerts_post_discord, this never
    raises — the alert pipeline must stay resilient to network blips.
    Requires an active Pro license; the site rejects unauthenticated
    requests with a 401 / 402."""
    email_to = (email_to or "").strip()
    if not email_to or "@" not in email_to:
        return False, "Invalid or missing email address"
    lic = _get_license() or {}
    license_key = (lic.get("key") or "").strip()
    if not license_key:
        return False, "Email alerts require an active Pro license"
    try:
        r = requests.post(
            _ALERTS_EMAIL_RELAY_URL,
            json={
                "license_key": license_key,
                "email_to": email_to,
                "title": title[:256],
                "body": body[:4000],
            },
            timeout=8,
            headers={"User-Agent": f"BitaxeBaller/{APP_VERSION}"},
        )
        if r.status_code == 200:
            return True, "delivered"
        # Site returns descriptive JSON errors like {"error": "..."}; surface that.
        try:
            err = r.json().get("error") or r.text
        except Exception:
            err = r.text
        return False, f"Email HTTP {r.status_code}: {str(err)[:120]}"
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _alerts_should_fire(ip: str, trigger: str, cooldown_s: int) -> bool:
    """Return True iff this (ip, trigger) hasn't fired within the cooldown window.

    Two extra guards on top of the cooldown:
      1. Startup quiet period — during the first ALERTS_STARTUP_QUIET_S
         seconds of process lifetime we suppress all firings + record the
         current time as the "last fired" so the cooldown then runs from
         restart, not from epoch. This is what prevents the post-restart
         alert storm.
      2. Normal cooldown — record on success so the next fire is gated.
    """
    now = time.time()
    if (now - _alerts_startup_at) < ALERTS_STARTUP_QUIET_S:
        # Still in the startup quiet window. Record the trigger time so
        # the cooldown clock starts ticking from "now" — once the quiet
        # window passes, the user gets fresh alerts for any condition
        # that's still tripping, but no historical-state flood.
        with alerts_lock:
            _alerts_last_fired.setdefault(ip, {})[trigger] = now
        return False
    with alerts_lock:
        last = _alerts_last_fired.get(ip, {}).get(trigger, 0)
        if now - last < cooldown_s:
            return False
        _alerts_last_fired.setdefault(ip, {})[trigger] = now
        return True


def _alerts_dispatch(label: str, ip: str, trigger: str, title: str, body: str) -> None:
    """Fan out to all configured channels. Logs to the device event log so
    the user has an in-app record of every alert fired. Channels fire in
    parallel logically (sequential because the GIL is fine here) — a failure
    on one doesn't prevent the others. Status lines include each channel's
    outcome so a user grep'ing logs can see which paths worked."""
    cfg = _alerts_get_config()
    if not cfg.get("enabled"):
        return
    channels = cfg.get("channels", {}) or {}
    webhook = (channels.get("discord_webhook") or "").strip()
    email_to = (channels.get("email_to") or "").strip()

    parts = []
    if webhook:
        ok, msg = _alerts_post_discord(webhook, title, body)
        parts.append(f"discord={'ok' if ok else f'fail({msg})'}")
    if email_to:
        ok, msg = _alerts_post_email(email_to, title, body)
        parts.append(f"email={'ok' if ok else f'fail({msg})'}")
    if not parts:
        parts.append("no channel configured")
    log_event(ip, f"[alert] {trigger}: {' · '.join(parts)}")


def _alerts_check(ip: str, label: str, summary: dict) -> None:
    """Evaluate every rule against the current device summary. Called from the
    poll loop on every successful poll cycle. No-op if Pro is inactive."""
    if not is_pro_active():
        return
    cfg = _alerts_get_config()
    if not cfg.get("enabled"):
        return
    # Need at least one configured channel — Discord webhook OR email.
    channels = cfg.get("channels", {}) or {}
    if not (channels.get("discord_webhook") or channels.get("email_to")):
        return  # no destination, nothing to do

    rules = cfg["rules"]
    cooldown_s = int(cfg.get("cooldown_minutes", 30)) * 60

    # Offline
    offline_min = int(rules.get("offline_minutes", 5))
    if not summary.get("online"):
        # We tag "offline" when device has been offline this long. The
        # consecutive_errors counter on the state is the best proxy we have
        # without storing per-device offline timestamps.
        with state_lock:
            s = state.get(ip)
            errs = s.get("consecutive_errors", 0) if s else 0
        # consecutive_errors ticks once per poll; convert to minutes via poll interval.
        offline_for_min = (errs * DEFAULT_POLL) / 60
        if offline_for_min >= offline_min and _alerts_should_fire(ip, "offline", cooldown_s):
            _alerts_dispatch(
                label, ip, "offline",
                f"⚠ {label} offline",
                f"Device at {ip} hasn't responded for ~{int(offline_for_min)} minutes (threshold: {offline_min}). Check power and network.",
            )
        return  # don't evaluate temp rules when offline (no data)

    metrics = summary.get("metrics", {})
    vr = float(metrics.get("vrTemp") or 0)
    asic = float(metrics.get("temp") or 0)

    vr_limit = float(rules.get("vr_temp_c", 65))
    if vr >= vr_limit and _alerts_should_fire(ip, "vr_temp", cooldown_s):
        _alerts_dispatch(
            label, ip, "vr_temp",
            f"🔥 {label} — VR temp {vr:.1f}°C",
            f"VR temperature on {ip} hit {vr:.1f}°C (threshold: {vr_limit}°C). VR is the part that kills boards — drop core voltage or improve airflow now.",
        )

    asic_limit = float(rules.get("asic_temp_c", 65))
    if asic >= asic_limit and _alerts_should_fire(ip, "asic_temp", cooldown_s):
        _alerts_dispatch(
            label, ip, "asic_temp",
            f"🔥 {label} — ASIC temp {asic:.1f}°C",
            f"ASIC temperature on {ip} hit {asic:.1f}°C (threshold: {asic_limit}°C). Drop voltage or push more cooling.",
        )


# ---------- Auto-tune sweep (Pro) ----------
#
# Strategy for v1: FREQUENCY-ONLY sweep. We never touch core voltage during a
# sweep — voltage changes risk killing the VR, and we want users to trust the
# auto-tuner before we ratchet it up. A later release can add voltage probes.
#
# Loop (one "step" per AUTOTUNE_OBSERVE_S, evaluated by the poll thread):
#   1. Hold current freq for AUTOTUNE_OBSERVE_S
#   2. Sample HW error rate + temps
#   3. If temps or HW rate exceed hard ceilings → ABORT (restore baseline)
#   4. If HW errors low AND freq still has headroom AND step budget remains
#      → bump frequency by +25 MHz, continue
#   5. Else → declare ceiling. best_stable = previous (last fully-stable) freq.
#      Apply best_stable. Mark COMPLETE.
#
# Hard ceilings (instant abort, restore baseline):
#   - ASIC temp ≥ 65°C — the chip's own thermal envelope
#   - VR temp ≥ 85°C — the BM1370 board's voltage-regulator pads are spec'd
#     around 95°C; 85 is conservative. Earlier code used a single 65°C
#     threshold for both, which caused instant abort on Gamma 602 boards
#     (their VR routinely idles at 68-70°C, well below any actual risk).
#   - HW error rate ≥ 5% (clearly destabilized — drop everything)
AUTOTUNE_OBSERVE_S = 90      # seconds at each freq before evaluating
AUTOTUNE_STEP_MHZ = 25       # frequency increment per step
AUTOTUNE_MAX_STEPS = 8       # worst case: ~12 minutes
AUTOTUNE_HW_GOOD_PCT = 0.5   # below this → keep pushing
AUTOTUNE_HW_CEILING_PCT = 2.0  # at or above → declare ceiling (back off)
AUTOTUNE_HW_ABORT_PCT = 5.0  # at or above → ABORT (chip clearly destabilized)
AUTOTUNE_ASIC_ABORT_C = 65.0
AUTOTUNE_VR_ABORT_C   = 85.0


def _autotune_log(s: dict, msg: str) -> None:
    """Append to the per-device autotune event log. Also pushed to the main
    device event log so the user sees it in the existing event panel."""
    if not s.get("autotune"):
        return
    ts = time.time()
    s["autotune"]["events"].append({"t": ts, "msg": msg})
    # Keep autotune events bounded; the main events deque has its own bound.
    s["autotune"]["events"] = s["autotune"]["events"][-30:]
    s["events"].appendleft({"t": ts, "msg": f"[auto-tune] {msg}"})


def _autotune_apply_freq(ip: str, freq: int, s: dict) -> bool:
    """Apply a frequency change as part of the sweep. Returns True on success.
    On failure logs and aborts the sweep."""
    try:
        patch_device(ip, {"frequency": freq})
    except Exception as e:
        _autotune_log(s, f"PATCH failed @ {freq} MHz: {type(e).__name__}: {str(e)[:60]}")
        _autotune_abort(ip, s, f"PATCH error: {type(e).__name__}")
        return False
    # Reset the HW-error baseline so the next observation window is clean.
    if s["latest"]:
        s["session_shares_start"] = s["latest"].get("sharesAccepted", 0)
        s["session_hwerrors_start"] = s["latest"].get("sharesRejected", 0)
        s["session_start"] = time.time()
    return True


def _autotune_abort(ip: str, s: dict, reason: str) -> None:
    """Stop the sweep and restore the baseline settings. Always called under
    state_lock by the caller (or in the poll-loop tick that holds the lock)."""
    a = s.get("autotune")
    if not a:
        return
    baseline = a.get("baseline") or {}
    if baseline.get("frequency"):
        try:
            patch_device(ip, {"frequency": int(baseline["frequency"])})
        except Exception as e:
            _autotune_log(s, f"baseline restore failed: {type(e).__name__}")
    a["status"] = "aborted"
    a["abort_reason"] = reason
    a["next_step_at"] = None
    _autotune_log(s, f"ABORT — {reason} — restored baseline {baseline.get('frequency')} MHz")


def _autotune_complete(ip: str, s: dict, best_freq: int) -> None:
    """End the sweep, applying the best stable frequency we found."""
    a = s.get("autotune")
    if not a:
        return
    baseline_freq = (a.get("baseline") or {}).get("frequency") or best_freq
    if best_freq != s["latest"].get("frequency"):
        try:
            patch_device(ip, {"frequency": best_freq})
        except Exception as e:
            _autotune_log(s, f"apply best_stable failed: {type(e).__name__}")
    a["status"] = "complete"
    a["best_stable"] = {"frequency": best_freq, "delta_from_baseline_mhz": best_freq - baseline_freq}
    a["next_step_at"] = None
    _autotune_log(s, f"DONE — best stable {best_freq} MHz ({best_freq - baseline_freq:+d} MHz vs baseline)")


def _autotune_tick(ip: str, s: dict) -> None:
    """One evaluation tick. Called from the poll loop after each successful poll
    while autotune is running. Must be called under state_lock."""
    a = s.get("autotune")
    if not a or a.get("status") != "running":
        return
    if not s.get("online") or not s.get("latest"):
        return
    next_at = a.get("next_step_at") or 0
    if time.time() < next_at:
        return

    latest = s["latest"]
    asic = latest.get("temp") or 0
    vr = latest.get("vrTemp") or 0
    current_freq = latest.get("frequency") or 0

    # HW error rate over the current observation window. We use the session
    # counters that are reset every time we apply a new frequency.
    shares_delta = 0
    hw_delta = 0
    if s.get("session_shares_start") is not None and s["latest"]:
        shares_delta = latest.get("sharesAccepted", 0) - s["session_shares_start"]
        hw_delta = latest.get("sharesRejected", 0) - (s.get("session_hwerrors_start") or 0)
    total = shares_delta + hw_delta
    hw_pct = (hw_delta / total * 100) if total > 0 else 0.0

    # --- Hard safety ceilings ---
    if vr >= AUTOTUNE_VR_ABORT_C:
        _autotune_abort(ip, s, f"VR temp {vr:.1f}°C ≥ {AUTOTUNE_VR_ABORT_C}°C")
        return
    if asic >= AUTOTUNE_ASIC_ABORT_C:
        _autotune_abort(ip, s, f"ASIC temp {asic:.1f}°C ≥ {AUTOTUNE_ASIC_ABORT_C}°C")
        return
    if hw_pct >= AUTOTUNE_HW_ABORT_PCT:
        _autotune_abort(ip, s, f"HW error rate {hw_pct:.2f}% ≥ {AUTOTUNE_HW_ABORT_PCT}%")
        return

    a["last_observed"] = {
        "freq_mhz": current_freq,
        "hw_pct": round(hw_pct, 3),
        "samples": total,
        "asic_temp": round(asic, 1),
        "vr_temp": round(vr, 1),
    }

    # --- Decision ---
    max_freq = int(a.get("max_freq") or BOUNDS["frequency"][1])
    step = int(a.get("step") or 0)
    if hw_pct >= AUTOTUNE_HW_CEILING_PCT:
        # Errors are climbing — declare current freq the ceiling, back off one step.
        best = max(int(a.get("baseline", {}).get("frequency", current_freq)), current_freq - AUTOTUNE_STEP_MHZ)
        _autotune_log(s, f"step {step}: HW {hw_pct:.2f}% @ {current_freq} MHz → ceiling; falling back to {best} MHz")
        _autotune_complete(ip, s, best)
        return

    next_freq = current_freq + AUTOTUNE_STEP_MHZ
    out_of_budget = step + 1 >= AUTOTUNE_MAX_STEPS
    out_of_freq = next_freq > max_freq
    if hw_pct <= AUTOTUNE_HW_GOOD_PCT and not out_of_freq and not out_of_budget:
        if not _autotune_apply_freq(ip, next_freq, s):
            return
        a["step"] = step + 1
        a["current_freq"] = next_freq
        a["next_step_at"] = time.time() + AUTOTUNE_OBSERVE_S
        _autotune_log(s, f"step {a['step']}: stable @ {current_freq} MHz ({hw_pct:.2f}%) — try {next_freq} MHz")
        return

    # Stable but out of room (either budget or freq ceiling) → declare current as best.
    reason = "max freq" if out_of_freq else "max steps"
    _autotune_log(s, f"step {step}: stable @ {current_freq} MHz ({hw_pct:.2f}%) → done ({reason})")
    _autotune_complete(ip, s, current_freq)


def _autotune_summary(s: dict) -> dict:
    """Public-facing autotune state for the device_summary payload. Always
    returns a small dict; never the internal mutable structure."""
    a = s.get("autotune")
    if not a:
        return {"status": "idle"}
    return {
        "status": a.get("status", "idle"),
        "step": a.get("step", 0),
        "max_steps": AUTOTUNE_MAX_STEPS,
        "observe_s": AUTOTUNE_OBSERVE_S,
        "current_freq": a.get("current_freq"),
        "max_freq": a.get("max_freq"),
        "baseline": a.get("baseline"),
        "best_stable": a.get("best_stable"),
        "abort_reason": a.get("abort_reason"),
        "started_at": a.get("started_at"),
        "next_step_at": a.get("next_step_at"),
        "last_observed": a.get("last_observed"),
        "events": list(a.get("events", []))[-10:],
    }


# ---------- Routes ----------

def _request_is_from_host() -> bool:
    """True when the request comes from a browser on the same machine as the
    app — the only case where opening Finder/Explorer is useful to the person
    clicking. Loopback covers the desktop app (it opens localhost); matching
    the LAN IP covers browsing http://bitaxe-baller.local from the host
    itself. Relay-forwarded requests also arrive via loopback, so the relay
    client marks them with X-Baller-Relay and we exclude those first."""
    if request.headers.get("X-Baller-Relay"):
        return False
    addr = request.remote_addr or ""
    return addr in ("127.0.0.1", "::1") or addr == detect_lan_ip()


@app.route("/")
def index():
    return render_template("dashboard.html", presets=PRESETS, bounds=BOUNDS,
                           show_logs_link=_request_is_from_host())


@app.route("/healthz")
def healthz():
    """Liveness probe for container orchestrators (Umbrel, Docker compose,
    Kubernetes). Returns 200 with a tiny JSON body once Flask is serving;
    intentionally does NOT touch the polling state or the SQLite history so
    a sick poll thread doesn't flap the container."""
    return jsonify({"ok": True, "version": APP_VERSION}), 200


@app.route("/device/<ip>")
def device_detail(ip):
    """Per-device detail page — full metrics, tuning, pool config, event log."""
    with state_lock:
        if ip not in state:
            return ("Device not found. <a href='/'>Back to overview</a>", 404)
    return render_template("device.html", ip=ip, presets=PRESETS, bounds=BOUNDS,
                           show_logs_link=_request_is_from_host())


# Fleet outlier detection — informational rec for devices materially
# under-performing or over-erroring vs. their peers on the same chain.
#
# Underperformance is judged on each board's actual-vs-EXPECTED hashrate
# (a percentage already normalized for chip count and frequency), NOT raw
# GH/s. Comparing raw GH/s flagged healthy small boards as outliers whenever
# they shared a fleet with higher-hashrate machines (e.g. a ~1.2 TH/s Gamma
# next to a multi-chip board) — the median got dragged up and the Gamma fell
# below the floor despite running perfectly. Normalizing fixes that: a healthy
# board reads ~100% regardless of model or size, so only a board lagging its
# OWN spec relative to its siblings is flagged.
FLEET_OUTLIER_MIN_DEVICES    = 3      # statistically meaningless below this
FLEET_OUTLIER_PERF_FLOOR_PCT = 0.80   # board's actual%-of-expected < 80% of fleet median → flag
FLEET_OUTLIER_HW_MULTIPLE    = 2.0    # device HW% > 2× fleet median (and > 1%) → flag


def _enrich_fleet_outliers(summaries):
    """Add a `fleet_outlier` recommendation to each summary whose metrics
    deviate materially from the fleet (within the same chain). Underperformance
    is measured on actual-vs-expected hashrate %, so mixing board sizes/models
    no longer produces false positives. No-op below FLEET_OUTLIER_MIN_DEVICES."""
    online = [s for s in summaries if s.get("online") and s.get("metrics")]
    if len(online) < FLEET_OUTLIER_MIN_DEVICES:
        return summaries

    # Bucket by chain so cross-chain comparisons don't muddy the median.
    by_chain = {}
    for s in online:
        by_chain.setdefault(s.get("chain", "btc"), []).append(s)

    for chain, group in by_chain.items():
        if len(group) < FLEET_OUTLIER_MIN_DEVICES:
            continue
        # Normalized performance: each board vs its OWN expected output. Boards
        # without a valid expected figure yet (just added, no freq) report 0 —
        # exclude them from the median and never flag them.
        perf_values = sorted(
            s["efficiency"]["actualPctOfExpected"] for s in group
            if s["efficiency"]["actualPctOfExpected"] > 0
        )
        hw_values = sorted(s["hwErrors"]["ratePct"] for s in group)
        median_hw = hw_values[len(hw_values) // 2]
        hw_ceiling = max(1.0, median_hw * FLEET_OUTLIER_HW_MULTIPLE)
        # Need enough boards reporting a valid expected-vs-actual to form a median.
        median_perf = (
            perf_values[len(perf_values) // 2]
            if len(perf_values) >= FLEET_OUTLIER_MIN_DEVICES else None
        )
        perf_floor = median_perf * FLEET_OUTLIER_PERF_FLOOR_PCT if median_perf else None

        for s in group:
            pct = s["efficiency"]["actualPctOfExpected"]
            hw_pct = s["hwErrors"]["ratePct"]
            issues = []
            if perf_floor is not None and pct > 0 and pct < perf_floor:
                issues.append(
                    f"delivering {pct:.0f}% of its rated hashrate vs the fleet's "
                    f"~{median_perf:.0f}%"
                )
            if hw_pct > hw_ceiling:
                issues.append(
                    f"HW errors {hw_pct:.2f}% vs fleet median {median_hw:.2f}%"
                )
            if not issues:
                continue
            s.setdefault("recommendations", []).append({
                "id": "fleet_outlier",
                "severity": "info",
                "title": "Fleet outlier",
                "body": "This device is " + " and ".join(issues) + ". "
                        "Check cooling, mounting, or chip lottery vs. its siblings.",
            })
    return summaries


@app.route("/api/devices")
def api_devices():
    elec = _electricity_cfg()  # resolve once, not per device
    with state_lock:
        summaries = [device_summary(s, elec=elec) for s in state.values()]
    return jsonify(_enrich_fleet_outliers(summaries))


@app.route("/api/block-finds")
def api_block_finds():
    """Return pending (unack'd) + recent block finds. Dashboard polls this on
    the same 5s cadence as /api/devices — block finds are rare so a second
    small HTTP call adds essentially nothing, and keeping it separate means
    /api/devices stays a backward-compatible array."""
    return jsonify({
        "pending": _block_finds_pending(),
        "recent":  _block_finds_recent(20),
    })


@app.route("/api/block-finds/ack", methods=["POST"])
def api_block_finds_ack():
    """Mark a block-find as acknowledged so its confetti overlay stops firing
    on subsequent dashboard polls. Body: {id}. Idempotent: ack'ing an already-
    ack'd id returns 200 with already_acked=true."""
    body = request.get_json(silent=True) or {}
    find_id = str(body.get("id") or "").strip()
    if not find_id:
        return jsonify({"error": "id required"}), 400
    ok = _block_finds_ack(find_id)
    return jsonify({"ok": True, "already_acked": not ok})


@app.route("/api/block-finds/_test", methods=["POST"])
def api_block_finds_test():
    """DEV-only: inject a synthetic block-find for testing the celebration UI.
    Gated behind the BITAXE_BALLER_DEV_PRO env var so a production install
    can't accidentally fire confetti by curl. Useful for verifying the
    overlay + ack flow without waiting for a real block to be solved."""
    if not os.environ.get("BITAXE_BALLER_DEV_PRO"):
        return jsonify({"error": "dev mode only — set BITAXE_BALLER_DEV_PRO=1"}), 403
    body = request.get_json(silent=True) or {}
    fake_latest = {
        "blockHeight": int(body.get("block_height") or 17_623_815),
        "bestDiff":    str(body.get("best_diff") or "1.18T"),
        "bestSessionDiff": str(body.get("best_session_diff") or "1.18T"),
        "macAddr": "DE:AD:BE:EF:00:01",
    }
    chain = str(body.get("chain") or "dgb")
    rec = _block_finds_record(
        ip=str(body.get("device_ip") or "192.168.1.226"),
        label=str(body.get("device_label") or "Bitaxe_004"),
        latest=fake_latest,
        chain_id=chain,
    )
    return jsonify({"ok": True, "find": rec})


@app.route("/api/device/<ip>")
def api_device_one(ip):
    with state_lock:
        if ip not in state:
            return jsonify({"error": "device not found"}), 404
        return jsonify(device_summary(state[ip]))


@app.route("/api/device/<ip>/history")
def api_device_history(ip):
    """Pro-tier endpoint: downsampled long-range history from the SQLite store.
    Query param `range` ∈ HISTORY_RANGES keys (default 24h). Returns a series of
    bucketed averages plus the range/bucket metadata so the chart can label axes.

    Free tier: returns 402 — the in-memory rolling history is already on the page
    via /api/device/<ip>, this endpoint exists purely for the long-range view."""
    if not is_pro_active():
        return jsonify({
            "error": "Long-term history is a Pro feature.",
            "code": "pro_required",
        }), 402

    with state_lock:
        if ip not in state:
            return jsonify({"error": "device not found"}), 404

    rng = request.args.get("range", "24h")
    if rng not in HISTORY_RANGES:
        return jsonify({"error": f"Unknown range. Valid: {', '.join(HISTORY_RANGES)}"}), 400
    window_s, bucket_s = HISTORY_RANGES[rng]
    start_ts = int(time.time() - window_s)

    if not os.path.exists(HISTORY_DB_PATH):
        return jsonify({"range": rng, "bucket_s": bucket_s, "points": []})

    try:
        _history_db_init()
        conn = sqlite3.connect(HISTORY_DB_PATH, timeout=5.0)
        try:
            rows = conn.execute(
                """
                SELECT (ts / ?) * ? AS bucket_ts,
                       AVG(hashrate)   AS hashrate,
                       AVG(temp)       AS temp,
                       AVG(vr_temp)    AS vr_temp,
                       AVG(power)      AS power,
                       AVG(frequency)  AS frequency,
                       AVG(core_voltage) AS core_voltage,
                       MAX(shares_accepted) AS shares_accepted,
                       MAX(shares_rejected) AS shares_rejected,
                       COUNT(*)        AS samples
                FROM metrics
                WHERE ip = ? AND ts >= ?
                GROUP BY bucket_ts
                ORDER BY bucket_ts
                """,
                (bucket_s, bucket_s, ip, start_ts),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return jsonify({"error": f"history db: {type(e).__name__}: {e}"}), 500

    points = [
        {
            "t": int(r[0]),
            "hashRate": round(r[1] or 0, 1),
            "temp": round(r[2] or 0, 1),
            "vrTemp": round(r[3] or 0, 1),
            "power": round(r[4] or 0, 2),
            "frequency": int(r[5] or 0),
            "coreVoltage": int(r[6] or 0),
            "sharesAccepted": int(r[7] or 0),
            "sharesRejected": int(r[8] or 0),
            "samples": int(r[9] or 0),
        }
        for r in rows
    ]
    return jsonify({
        "range": rng,
        "bucket_s": bucket_s,
        "window_s": window_s,
        "points": points,
        "total_points": len(points),
    })


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(load_config())


@app.route("/api/config/electricity", methods=["GET"])
def api_electricity_get():
    """Current electricity rate + currency for the cost estimator. Free feature —
    no Pro gate."""
    rate, currency = _electricity_cfg()
    return jsonify({"rate": rate, "currency": currency})


@app.route("/api/config/electricity", methods=["POST"])
def api_electricity_set():
    """Set the per-kWh rate and currency symbol used for cost estimates. Body:
    {rate, currency}. Rate is clamped to a sane range; currency is a short
    display symbol only (we don't convert between currencies)."""
    body = request.get_json(force=True) or {}
    try:
        rate = float(body.get("rate"))
    except (TypeError, ValueError):
        return jsonify({"error": "rate must be a number"}), 400
    if not (0 <= rate <= 100):
        return jsonify({"error": "rate must be between 0 and 100 per kWh"}), 400
    currency = str(body.get("currency") or DEFAULT_CURRENCY).strip()[:4] or DEFAULT_CURRENCY
    with config_lock:
        cfg = load_config()
        cfg["electricity"] = {"rate": round(rate, 4), "currency": currency}
        save_config(cfg)
    return jsonify({"ok": True, "rate": round(rate, 4), "currency": currency})


@app.route("/api/logs/open", methods=["POST"])
def api_logs_open():
    """Open the CSV logs folder in the host OS file manager (Finder /
    Explorer / xdg-open). Only honored for requests from the host machine
    itself — a Finder window popping on the host is useless (and confusing)
    to a phone or remote browser, so those get the path back instead. The
    UI also hides the link for non-host sessions; this is the backstop."""
    if not _request_is_from_host():
        return jsonify({"ok": False, "path": LOG_DIR,
                        "message": f"Remote session — the logs folder lives on "
                                   f"the machine running Bitaxe Baller, at {LOG_DIR}"})
    import subprocess
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", LOG_DIR])
        elif sys.platform == "win32":
            os.startfile(LOG_DIR)
        else:
            subprocess.Popen(["xdg-open", LOG_DIR])
        return jsonify({"ok": True, "path": LOG_DIR})
    except Exception as e:
        return jsonify({"ok": False, "path": LOG_DIR,
                        "message": f"No file manager available — logs live at {LOG_DIR}",
                        "error": f"{type(e).__name__}: {e}"})


# ---------- History (Pro: persistent SQLite) ----------

# Buckets for the long-range chart. Keys are the range labels the frontend
# sends; values are (window_seconds, bucket_seconds). The bucket_seconds
# determines how aggressively we downsample so we never ship 17,280 points
# (a full day at 5s polling) to the browser.
HISTORY_RANGES = {
    "1h":  (3600,        30),     # 120 points, 30-sec bucket
    "24h": (86_400,      300),    # 288 points, 5-min bucket
    "7d":  (604_800,     3600),   # 168 points, 1-h bucket
    "30d": (2_592_000,   21_600), # 120 points, 6-h bucket
    "90d": (7_776_000,   86_400), # 90 points, 1-day bucket
}

_history_db_init_done = False
_history_db_lock = threading.Lock()


def _history_db_init() -> None:
    """Create the schema if needed. Idempotent. Called lazily on first write/read.
    WAL mode lets the poll thread keep writing while a request handler reads."""
    global _history_db_init_done
    if _history_db_init_done:
        return
    with _history_db_lock:
        if _history_db_init_done:
            return
        conn = sqlite3.connect(HISTORY_DB_PATH)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metrics (
                    ip TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    hashrate REAL,
                    temp REAL,
                    vr_temp REAL,
                    power REAL,
                    frequency INTEGER,
                    core_voltage INTEGER,
                    shares_accepted INTEGER,
                    shares_rejected INTEGER,
                    PRIMARY KEY (ip, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
            """)
            conn.commit()
        finally:
            conn.close()
        _history_db_init_done = True


def _history_record(ip: str, point: dict) -> None:
    """Append one poll's worth of metrics to the SQLite history. No-op when Pro
    is inactive — the free tier sticks with in-memory + daily CSV only."""
    if not is_pro_active():
        return
    try:
        _history_db_init()
        conn = sqlite3.connect(HISTORY_DB_PATH, timeout=2.0)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO metrics "
                "(ip, ts, hashrate, temp, vr_temp, power, frequency, core_voltage, shares_accepted, shares_rejected) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ip,
                    int(point["t"]),
                    float(point.get("hashRate") or 0),
                    float(point.get("temp") or 0),
                    float(point.get("vrTemp") or 0),
                    float(point.get("power") or 0),
                    int(point.get("frequency") or 0),
                    int(point.get("coreVoltage") or 0),
                    int(point.get("sharesAccepted") or 0),
                    int(point.get("sharesRejected") or 0),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # Never let history bookkeeping block live polling. Log and move on.
        print(f"[history] write failed for {ip}: {type(e).__name__}: {e}", file=sys.stderr)


# Run the retention sweep at most once per process per day. Cheap to call
# repeatedly because of the timer guard; expensive (single DELETE) only when due.
_history_last_sweep = 0.0


def _history_sweep_retention() -> None:
    """Drop rows older than HISTORY_RETENTION_DAYS. Idempotent; safe to call often."""
    global _history_last_sweep
    now = time.time()
    if now - _history_last_sweep < 86_400:  # at most once per 24h per process
        return
    _history_last_sweep = now
    if not os.path.exists(HISTORY_DB_PATH):
        return
    try:
        cutoff = int(now - HISTORY_RETENTION_DAYS * 86_400)
        conn = sqlite3.connect(HISTORY_DB_PATH, timeout=5.0)
        try:
            conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[history] retention sweep failed: {type(e).__name__}: {e}", file=sys.stderr)


# ---------- License (Polar) ----------

license_lock = threading.Lock()


def _machine_label() -> str:
    """Best-effort identifier for this machine — shown on the Polar dashboard
    so the user can tell their activations apart (laptop vs desktop vs NAS)."""
    try:
        return socket.gethostname() or "bitaxe-baller"
    except Exception:
        return "bitaxe-baller"


def _get_license() -> dict:
    """Returns the stored license block, or {} if none. Always read fresh from
    disk so a manually-edited config.json doesn't get masked by stale state."""
    with config_lock:
        cfg = load_config()
    return cfg.get("pro_license") or {}


def _save_license(lic: dict) -> None:
    with config_lock:
        cfg = load_config()
        cfg["pro_license"] = lic
        save_config(cfg)


def _clear_license() -> None:
    with config_lock:
        cfg = load_config()
        cfg.pop("pro_license", None)
        save_config(cfg)


def _license_summary(lic: dict, *, active: bool) -> dict:
    """Shape the local license state for the frontend. Never include the full
    key — only the last 4 chars — since the modal stays visible after activation."""
    if not lic:
        return {"active": False}
    key = lic.get("key", "")
    return {
        "active": bool(active),
        "key_suffix": key[-4:] if len(key) >= 4 else key,
        "email": lic.get("email"),
        "expires_at": lic.get("expires_at"),
        "activation_id": lic.get("activation_id"),
        "machine_label": lic.get("machine_label"),
        "last_validated": lic.get("last_validated"),
    }


def _ls_license_post(path: str, body: dict, timeout: float = 8.0):
    """POST to the Lemon Squeezy license API. Returns (status_code, parsed_json).
    These endpoints take form-encoded bodies (NOT JSON) and the license key
    itself is the credential — no Authorization header in the desktop binary."""
    url = f"{LEMONSQUEEZY_API_BASE}{path}"
    r = requests.post(
        url,
        data=body,  # form-encoded
        headers={
            "Accept": "application/json",
            "User-Agent": f"BitaxeBaller/{APP_VERSION}",
        },
        timeout=timeout,
    )
    try:
        payload = r.json()
    except ValueError:
        payload = {"error": r.text[:200]}
    return r.status_code, payload


def _extract_license_fields(payload: dict, *, key_fallback: str = "") -> dict:
    """Normalize an LS activate/validate response into the fields we store
    locally. LS shape on activate:
      { activated: true, license_key: {key, status, expires_at, activation_limit,
        activation_usage, ...}, instance: {id, name, created_at}, meta: {
        customer_email, customer_name, product_name, ...} }
    On validate the shape is similar but `valid` instead of `activated`."""
    if not isinstance(payload, dict):
        return {}
    lk = payload.get("license_key") or {}
    instance = payload.get("instance") or {}
    meta = payload.get("meta") or {}
    return {
        "key": lk.get("key") or key_fallback,
        "activation_id": instance.get("id"),
        "email": meta.get("customer_email"),
        "name": meta.get("customer_name"),
        "expires_at": lk.get("expires_at"),
        "status": lk.get("status"),
        "limit_activations": lk.get("activation_limit"),
        "machine_label": _machine_label(),
        "last_validated": time.time(),
    }


def _ls_error_message(payload: dict, status: int) -> str:
    """Pull a friendly message out of a Lemon Squeezy error response. LS license
    endpoints return either {error: "..."} on failure or {license_key: {...},
    error: null} on success. Some endpoints additionally include a top-level
    `valid: false` or `activated: false`."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, str) and err:
            low = err.lower()
            if "not found" in low or "could not" in low or "invalid" in low:
                return "License key not recognized. Double-check it matches what we emailed you after purchase."
            if "activation" in low and ("limit" in low or "max" in low or "already" in low):
                return "Activation limit reached — you've used all 5 slots. Deactivate one on a machine you're no longer using, or contact support."
            if "expired" in low:
                return "This license is expired. Renew via the customer portal."
            if "disabled" in low or "revoked" in low or "refunded" in low:
                return "This license is no longer valid (refunded or revoked). Contact support if this is a mistake."
            return err
    return f"License server returned HTTP {status}"


def _dev_pro_override() -> bool:
    """Local-development bypass — set BITAXE_BALLER_DEV_PRO=1 to unlock Pro
    features without a real license key. Never set in shipped builds."""
    return os.environ.get("BITAXE_BALLER_DEV_PRO", "") in ("1", "true", "yes")


def is_pro_active() -> bool:
    """Cheap check used by feature gates. Considers the locally cached state
    only — does NOT call LS. The background validation refresh keeps the
    cache honest. LS license keys have status "active" when valid."""
    if _dev_pro_override():
        return True
    lic = _get_license()
    if not lic or not lic.get("key") or not lic.get("activation_id"):
        return False
    # LS uses "active" for healthy licenses; "inactive"/"expired"/"disabled" all bad.
    if lic.get("status") and lic["status"] != "active":
        return False
    expires = lic.get("expires_at")
    if expires:
        try:
            # Polar returns ISO-8601 with 'Z' suffix; fromisoformat needs +00:00.
            dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if dt.timestamp() < time.time():
                return False
        except (ValueError, AttributeError):
            pass
    return True


@app.route("/api/license/status", methods=["GET"])
def api_license_status():
    """Frontend polls this to render the Pro modal. Triggers a lazy LS
    re-validation if the cached state is older than LICENSE_REVALIDATE_S."""
    # Dev override path: short-circuit before any network call so the
    # developer never accidentally consumes an activation slot during feature work.
    if _dev_pro_override():
        lic = _get_license()
        return jsonify({
            "active": True,
            "dev_mode": True,
            "email": (lic.get("email") if lic else None) or "dev@localhost",
            "machine_label": "DEV OVERRIDE (env var)",
            "key_suffix": "DEV",
        })

    lic = _get_license()
    active = is_pro_active()

    # Lazy revalidation: if active, key + instance present, and stale, re-check.
    last = lic.get("last_validated", 0) if lic else 0
    if lic.get("key") and lic.get("activation_id") and (time.time() - (last or 0)) > LICENSE_REVALIDATE_S:
        try:
            status, payload = _ls_license_post(
                "/validate",
                {"license_key": lic["key"], "instance_id": lic["activation_id"]},
            )
            # LS's validate endpoint returns 200 with `valid: false` for revoked/
            # expired/deactivated keys, not a 4xx. Inspect the body.
            if status == 200 and payload.get("valid"):
                merged = {**lic, **_extract_license_fields(payload, key_fallback=lic["key"])}
                _save_license(merged)
                lic = merged
                active = is_pro_active()
            else:
                # Key revoked / refunded / instance gone — clear locally.
                _clear_license()
                lic = {}
                active = False
        except requests.RequestException:
            # Network blip — keep cached state, try again next poll.
            pass

    return jsonify(_license_summary(lic, active=active))


@app.route("/api/license/activate", methods=["POST"])
def api_license_activate():
    body = request.get_json(force=True) or {}
    key = (body.get("key") or "").strip()
    if not key:
        return jsonify({"error": "License key required"}), 400

    with license_lock:
        existing = _get_license()
        if existing.get("key") == key and existing.get("activation_id"):
            # Idempotent: already activated on this machine. Return success.
            return jsonify({"ok": True, "license": _license_summary(existing, active=is_pro_active())})

        try:
            status, payload = _ls_license_post(
                "/activate",
                {"license_key": key, "instance_name": _machine_label()},
            )
        except requests.RequestException as e:
            return jsonify({"error": f"Could not reach license server: {type(e).__name__}"}), 502

        # LS returns 200 with `activated: false` + an error string on failure,
        # rather than a 4xx. Treat either form as failure.
        if status >= 400 or not payload.get("activated"):
            return jsonify({"error": _ls_error_message(payload, status)}), 400

        fields = _extract_license_fields(payload, key_fallback=key)
        if not fields.get("activation_id"):
            return jsonify({"error": "License server response missing instance id"}), 502

        _save_license(fields)
        # Pro just activated: if remote access is already running on the free
        # path, restart the connector so it reconnects on the license-validated
        # (full-fleet) path. Without this, a long-running install (e.g. Umbrel,
        # which rarely restarts) stays capped at 1 device remotely until the
        # next app restart.
        try:
            rc = _remote_access_cfg()
            if rc["enabled"] and is_pro_active() and relay_client.is_running():
                relay_client.stop()
                relay_client.start(
                    key,
                    relay_url=rc.get("relay_url") or relay_client.default_relay_url(),
                    app_port=PORT,
                    app_version=APP_VERSION,
                    install_uuid=_install_uuid(),
                )
        except Exception as e:
            print(f"[relay] post-activation relay refresh failed: {type(e).__name__}: {e}", file=sys.stderr)
        return jsonify({"ok": True, "license": _license_summary(fields, active=is_pro_active())})


@app.route("/api/license/deactivate", methods=["POST"])
def api_license_deactivate():
    """Release this machine's activation slot so a future install can use it."""
    with license_lock:
        lic = _get_license()
        if not lic.get("key") or not lic.get("activation_id"):
            _clear_license()
            return jsonify({"ok": True, "license": {"active": False}})

        try:
            status, payload = _ls_license_post(
                "/deactivate",
                {"license_key": lic["key"], "instance_id": lic["activation_id"]},
            )
        except requests.RequestException as e:
            # We don't want a network issue to lock a user into their own machine.
            # Clear local state so they can re-activate elsewhere; the slot on
            # LS's side will eventually need manual cleanup, but that's a rare
            # edge case.
            _clear_license()
            return jsonify({
                "ok": True,
                "license": {"active": False},
                "warning": f"Cleared locally but license server unreachable ({type(e).__name__}). The slot may need manual deactivation in the customer portal.",
            })

        # LS returns {deactivated: true} on success. Tolerate already-gone
        # instances (whether 200 with deactivated:false meaning "not found",
        # or 404) — local state has been cleared either way.
        if status >= 400 and status != 404:
            return jsonify({"error": _ls_error_message(payload, status)}), 400

        _clear_license()
        return jsonify({"ok": True, "license": {"active": False}})


# ----- Pro: remote access (relay-routed dashboard) -----
# The desktop app opens an outbound WSS to relay.bitaxeballer.com when
# the user enables remote access. The relay routes inbound requests
# (from remote browsers or mobile clients) onto our local /api/* endpoints.
# All product logic stays here; the relay is dumb routing. See ./relay/
# for the relay service, ./relay_client.py for the connector here.

def _remote_access_cfg() -> dict:
    """Stored shape: {enabled: bool, relay_url: str}. Missing means disabled."""
    cfg = load_config()
    raw = cfg.get("remote_access") or {}
    return {
        "enabled": bool(raw.get("enabled")),
        "relay_url": raw.get("relay_url") or relay_client.default_relay_url(),
    }


def _save_remote_access_cfg(enabled: bool, relay_url: str) -> None:
    with config_lock:
        cfg = load_config()
        cfg["remote_access"] = {"enabled": bool(enabled), "relay_url": relay_url}
        save_config(cfg)


def _maybe_start_relay_client(app_port: int) -> None:
    """Called once on startup. Starts the connector if the user has enabled
    remote access. Pro connects with the license key (full fleet); free tier
    connects with install_uuid only (relay caps to 1 device). No-op if remote
    access is disabled or there's nothing to authenticate with."""
    rc = _remote_access_cfg()
    if not rc["enabled"]:
        return
    install_uuid = _install_uuid()
    lic = _get_license()
    key = (lic.get("key") or "").strip() if (lic and is_pro_active()) else ""
    if not key and not install_uuid:
        return
    try:
        relay_client.start(
            key,
            relay_url=rc["relay_url"],
            app_port=app_port,
            app_version=APP_VERSION,
            install_uuid=install_uuid,
        )
    except Exception as e:
        print(f"[relay] could not start connector: {type(e).__name__}: {e}", file=sys.stderr)


@app.route("/api/remote/status", methods=["GET"])
def api_remote_status():
    cfg = _remote_access_cfg()
    return jsonify({
        "pro_required": False,        # free tier allowed; relay caps it to 1 device
        "free_device_limit": 1,
        "pro_active": is_pro_active(),
        "configured": cfg,
        "runtime": relay_client.get_status(),
    })


@app.route("/api/remote/enable", methods=["POST"])
def api_remote_enable():
    # Remote access is available to everyone. Pro users with a real license key
    # connect on the license-validated path (full fleet); everyone else (free
    # tier, or dev-override Pro without a key) connects by install_uuid, which
    # the relay caps to 1 device server-side — so this can't leak the paid tier.
    # The 1-miner boundary lives on the relay, not here.
    install_uuid = _install_uuid()
    lic = _get_license()
    key = (lic.get("key") or "").strip() if (lic and is_pro_active()) else ""
    if not key and not install_uuid:
        return jsonify({
            "error": "No install ID available yet — restart the app and try again.",
        }), 400

    body = request.get_json(silent=True) or {}
    # Order of preference: explicit body override → previously-configured URL
    # → production default. The middle case matters: if the user has been
    # pointing at a custom relay (staging, self-hosted) and toggles off/on,
    # we should preserve that, not silently reset them to the prod URL.
    relay_url = (body.get("relay_url") or "").strip()
    if not relay_url:
        relay_url = (_remote_access_cfg().get("relay_url") or "").strip() or relay_client.default_relay_url()
    if not (relay_url.startswith("ws://") or relay_url.startswith("wss://")):
        return jsonify({"error": "relay_url must start with ws:// or wss://"}), 400

    _save_remote_access_cfg(enabled=True, relay_url=relay_url)

    # Restart the connector with the new URL if it was already running.
    if relay_client.is_running():
        relay_client.stop()
    relay_client.start(
        key,
        relay_url=relay_url,
        app_port=PORT,
        app_version=APP_VERSION,
        install_uuid=install_uuid,
    )
    return jsonify({"ok": True, "tier": "pro" if key else "free", "status": relay_client.get_status()})


@app.route("/api/remote/disable", methods=["POST"])
def api_remote_disable():
    _save_remote_access_cfg(enabled=False, relay_url=_remote_access_cfg()["relay_url"])
    relay_client.stop()
    return jsonify({"ok": True, "status": relay_client.get_status()})


# ---------- Public leaderboard (free + Pro opt-in, v1.12+) ----------
#
# As of v1.12, any user can opt-in to submit their best-share difficulty to
# the public leaderboard at bitaxeballer.com/leaderboard.
#
#   - Pro users authenticate with their license key (no email needed)
#   - Free users authenticate with a locally-generated install_uuid plus an
#     email (verified one-click via the leaderboard server). Email is used
#     ONLY to deliver monthly prizes; no marketing emails.
#
# Submission is per-device (one row per MAC address). Display name is
# user-chosen; the server rejects profanity/bad characters.
#
# Background submitter pushes every ~5 minutes per device, and immediately on
# a new personal-best.

LEADERBOARD_SUBMIT_URL = os.environ.get(
    "BITAXE_BALLER_LEADERBOARD_URL",
    "https://bitaxeballer.com/api/leaderboard/submit",
)
_LEADERBOARD_DISPLAY_NAME_MAX = 30
_LEADERBOARD_SUBMIT_INTERVAL_S = 300  # 5 min
_leaderboard_last_submit: dict = {}   # mac -> ts of last successful submit


def _install_uuid() -> str:
    """Stable per-install identifier used to authenticate free-tier
    leaderboard submissions. Generated once on first read, persisted to
    config.json. NOT a hardware fingerprint — moving the install to a new
    machine = new uuid, and that's the design intent."""
    with config_lock:
        cfg = load_config()
        uuid_val = (cfg.get("install_uuid") or "").strip()
        if not uuid_val:
            import uuid as _uuid_mod
            uuid_val = str(_uuid_mod.uuid4())
            cfg["install_uuid"] = uuid_val
            save_config(cfg)
        return uuid_val


def _leaderboard_cfg() -> dict:
    cfg = load_config()
    raw = cfg.get("public_leaderboard") or {}
    return {
        "enabled": bool(raw.get("enabled")),
        "display_name": (raw.get("display_name") or "").strip()[:_LEADERBOARD_DISPLAY_NAME_MAX],
        "email": (raw.get("email") or "").strip().lower()[:200],
    }


def _leaderboard_save_cfg(enabled: bool, display_name: str, email: str) -> None:
    with config_lock:
        cfg = load_config()
        cfg["public_leaderboard"] = {
            "enabled": bool(enabled),
            "display_name": (display_name or "").strip()[:_LEADERBOARD_DISPLAY_NAME_MAX],
            "email": (email or "").strip().lower()[:200],
        }
        save_config(cfg)


def _leaderboard_sanitize_name(name: str) -> str:
    """Strip control chars, collapse whitespace, enforce length. Display-side
    profanity filter lives on the server — we don't try to be the moral
    police inside a desktop app."""
    if not name:
        return ""
    out = "".join(ch for ch in name if ch.isprintable() and ch not in "\n\r\t")
    out = " ".join(out.split())
    return out[:_LEADERBOARD_DISPLAY_NAME_MAX]


def _leaderboard_validate_email(email: str) -> bool:
    """Light client-side check; the server is the authority. We just want to
    bounce obviously-invalid input before paying for the round-trip."""
    if not email:
        return False
    s = email.strip().lower()
    if "@" not in s or "." not in s.split("@")[-1]:
        return False
    if any(c in s for c in " \t\n\r,;") or len(s) > 200:
        return False
    return True


def _leaderboard_submit_one(summary: dict, force: bool = False) -> bool:
    """Submit a single device's best-share data. No-op if recently submitted
    (per-mac throttle) unless force=True. Silent on failure — the leaderboard
    is best-effort and shouldn't surface errors into the polling path."""
    mac = (summary.get("macAddr") or "").strip()
    if not mac:
        return False
    now = time.time()
    last = _leaderboard_last_submit.get(mac, 0)
    if not force and (now - last) < _LEADERBOARD_SUBMIT_INTERVAL_S:
        return False
    cfg = _leaderboard_cfg()
    if not cfg["enabled"] or not cfg["display_name"]:
        return False
    payload = {
        "display_name": cfg["display_name"],
        "mac_addr": mac,
        "model": (summary.get("model") or "").strip()[:40],
        "best_diff_career": float(summary.get("metrics", {}).get("bestDiffValue", 0) or 0),
        "best_diff_session": float(summary.get("metrics", {}).get("bestSessionDiffValue", 0) or 0),
        "hashrate_th_avg": float(summary.get("rolling", {}).get("15m", 0) or 0) / 1000.0,
        "app_version": APP_VERSION,
    }
    # Auth: Pro = license_key; free = install_uuid + email
    if is_pro_active():
        lic = _get_license()
        key = (lic.get("key") or "").strip() if lic else ""
        if key:
            payload["license_key"] = key
    if "license_key" not in payload:
        if not cfg["email"]:
            return False  # free-tier requires email
        payload["install_uuid"] = _install_uuid()
        payload["email"] = cfg["email"]
    try:
        r = requests.post(LEADERBOARD_SUBMIT_URL, json=payload, timeout=8)
        if 200 <= r.status_code < 300:
            _leaderboard_last_submit[mac] = now
            return True
    except requests.RequestException:
        pass
    return False


def _maybe_submit_leaderboard(summary: dict, new_best: bool) -> None:
    """Hook called from poll_one after each device tick. Skips silently if
    feature off / no display name / (free tier) no email."""
    cfg = _leaderboard_cfg()
    if not cfg["enabled"] or not cfg["display_name"]:
        return
    # Free tier additionally requires an email
    if not is_pro_active() and not cfg["email"]:
        return
    _leaderboard_submit_one(summary, force=new_best)


@app.route("/api/leaderboard/status", methods=["GET"])
def api_leaderboard_status():
    return jsonify({
        "pro_active": is_pro_active(),
        "configured": _leaderboard_cfg(),
        "install_uuid": _install_uuid(),
        "public_url": "https://bitaxeballer.com/leaderboard",
    })


@app.route("/api/leaderboard/save", methods=["POST"])
def api_leaderboard_save():
    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled"))
    display_name = _leaderboard_sanitize_name(str(body.get("display_name") or ""))
    email = (str(body.get("email") or "")).strip().lower()[:200]
    if enabled and not display_name:
        return jsonify({"error": "Display name is required to enable the public leaderboard."}), 400
    # Free tier requires email; Pro tier can submit without (license is the credential)
    if enabled and not is_pro_active() and not _leaderboard_validate_email(email):
        return jsonify({"error": "Email is required for free-tier leaderboard submission (used only to deliver prizes if you win a monthly contest)."}), 400
    _leaderboard_save_cfg(enabled=enabled, display_name=display_name, email=email)
    return jsonify({"ok": True, "configured": _leaderboard_cfg()})


# ============================================================
# iOS v1.1 pairing — desktop proxy endpoints
# ============================================================
# The Pair iPhone UI in the Pro modal calls these to generate + manage
# pair tokens against the site server's /api/relay/* endpoints. License
# key stays server-side; the browser never sees it.
_PAIR_API_BASE = os.environ.get("BITAXE_BALLER_PAIR_API_BASE", "https://bitaxeballer.com").rstrip("/")
_PAIR_API_TIMEOUT_S = 8.0


def _pair_credentials():
    """Returns (install_uuid, license_key_or_empty) for the current install."""
    uuid_val = _install_uuid()
    lic = _get_license() or {}
    key = (lic.get("key") or "").strip() if is_pro_active() else ""
    return uuid_val, key


@app.route("/api/relay/pair-init", methods=["POST"])
def api_relay_pair_init():
    """Generates a short-lived pair token via the site server. UI displays
    the token to the user (text or QR), they enter it in their iOS app."""
    uuid_val, key = _pair_credentials()
    if not uuid_val:
        return jsonify({"error": "No install_uuid configured."}), 400
    body = {"install_uuid": uuid_val}
    if key:
        body["license_key"] = key
    # Optional friendly label for the desktop ("Nathan's Mac mini").
    machine_label = (request.get_json(silent=True) or {}).get("display_label")
    if machine_label and isinstance(machine_label, str):
        body["display_label"] = machine_label.strip()[:80]
    try:
        r = requests.post(f"{_PAIR_API_BASE}/api/relay/pair-init", json=body, timeout=_PAIR_API_TIMEOUT_S)
    except requests.RequestException as e:
        return jsonify({"error": f"Pair server unavailable: {type(e).__name__}"}), 503
    try:
        payload = r.json()
    except ValueError:
        return jsonify({"error": f"Pair server returned HTTP {r.status_code}"}), 502
    return jsonify(payload), r.status_code


@app.route("/api/relay/devices", methods=["GET"])
def api_relay_devices():
    """Lists currently-paired iOS/Android devices for this install."""
    uuid_val, _ = _pair_credentials()
    if not uuid_val:
        return jsonify({"devices": []})
    try:
        r = requests.get(
            f"{_PAIR_API_BASE}/api/relay/devices",
            params={"install_uuid": uuid_val},
            timeout=_PAIR_API_TIMEOUT_S,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"Pair server unavailable: {type(e).__name__}"}), 503
    try:
        payload = r.json()
    except ValueError:
        return jsonify({"error": f"Pair server returned HTTP {r.status_code}"}), 502
    return jsonify(payload), r.status_code


@app.route("/api/relay/device-revoke", methods=["POST"])
def api_relay_device_revoke():
    """Revokes a paired iOS/Android device."""
    uuid_val, _ = _pair_credentials()
    if not uuid_val:
        return jsonify({"error": "No install_uuid configured."}), 400
    body = request.get_json(silent=True) or {}
    device_id = str(body.get("device_token_id") or "").strip()
    if not device_id:
        return jsonify({"error": "device_token_id required"}), 400
    try:
        r = requests.post(
            f"{_PAIR_API_BASE}/api/relay/device-revoke",
            json={"install_uuid": uuid_val, "device_token_id": device_id},
            timeout=_PAIR_API_TIMEOUT_S,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"Pair server unavailable: {type(e).__name__}"}), 503
    try:
        payload = r.json()
    except ValueError:
        return jsonify({"error": f"Pair server returned HTTP {r.status_code}"}), 502
    return jsonify(payload), r.status_code


def _is_private_v4(ip):
    """Cheap RFC1918 check so we don't accidentally scan a public range."""
    try:
        a, b, _, _ = (int(p) for p in ip.split("."))
    except (ValueError, AttributeError):
        return False
    if a == 10:
        return True
    if a == 192 and b == 168:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    return False


# ----- Auto-update (appcast feed + Ed25519-verified in-place install) -----
#
# Source of truth is appcast.xml (Sparkle 2 schema), hosted on bitaxeballer.com.
# Each <enclosure> carries an Ed25519 signature over the artifact bytes;
# we verify against UPDATE_SIGNING_PUBKEY before swapping the running app.
#
# Failure modes are designed to never break the dashboard:
#   - offline / DNS down / 5xx        → "no update available" (banner stays hidden)
#   - malformed XML                   → same
#   - signature missing or mismatch   → install aborts, banner still shows download link
#   - source mode (not frozen)        → check works, install endpoint returns 400
#
# Note: this replaces the previous GitHub Releases API check. Migration is
# transparent to the UI — same /api/update-check shape (current, latest,
# newer_available, release_url, platform_download_url, error). We add new
# /api/update-install + /api/update-progress on top.

# GitHub Releases auto-redirects /latest/download/<file> to the most recent
# release's asset of that name — so we never have to maintain a separate
# hosting endpoint, and the appcast lives alongside the artifacts it describes.
# Override via env var if you need to point at a staging feed during testing.
APPCAST_URL = os.environ.get(
    "BITAXE_BALLER_APPCAST_URL",
    "https://github.com/465media/bitaxe-baller/releases/latest/download/appcast.xml",
)

# Public half of the Ed25519 keypair that signs releases. Generated by
# build/gen-update-keypair.py — paste the pubkey it prints into this constant.
# Empty string means "auto-install disabled" — the banner still shows a manual
# download link, but the install button is hidden. Useful during the rollout
# window before the first signed release ships.
UPDATE_SIGNING_PUBKEY = "TC9gCKe/1OCeU7acxGbkPXaeMns4uh+JUn0SLkppQrI="

_UPDATE_CHECK_TTL = 3600  # 1 hour — appcast.xml is tiny but parsing is real work
_update_cache: dict = {"fetched_at": 0.0, "payload": None}
_update_cache_lock = threading.Lock()

_APPCAST_NS = {"sparkle": "http://www.andymatuschak.org/xml-namespaces/sparkle"}


def _parse_semver(s: str) -> tuple:
    """Tuple-comparable (major, minor, patch). Returns (0,0,0) on parse failure
    so an unparseable string never claims to be newer than the running app."""
    try:
        nums = s.lstrip("v").split(".")[:3]
        return tuple(int(n) for n in nums)
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _banner_recommended(cur: str, latest: str) -> bool:
    """Decide whether the dashboard banner should surface this update.

    Rule: banner on any newer version (major, minor, OR patch). The earlier
    "only banner on minor/major bumps" heuristic suppressed real hotfixes —
    e.g. v1.14.1 (settings persistence bug) and v1.14.2 (duplicate-card bug)
    both got swallowed silently for users on v1.14.0, even though they fixed
    user-visible regressions.

    The dismiss button stores dismissals per-version in localStorage, so a
    user who genuinely doesn't care can mute one release without muting all
    future ones — the original "trains users to dismiss reflexively" concern
    is handled there, not by gating which versions get to banner at all.
    """
    c = _parse_semver(cur)
    l = _parse_semver(latest)
    return l > c


def _current_platform_key() -> str:
    """Maps Python's sys.platform onto the sparkle:os values we write in the appcast."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def _parse_appcast(xml_text: str) -> list[dict]:
    """Parses the appcast and returns a list of {version, url, size, signature, os, notes_url}
    dicts, one per <item> matching a known platform. Bad entries are skipped, not raised."""
    import xml.etree.ElementTree as ET
    items: list[dict] = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        enc = item.find("enclosure")
        if enc is None:
            continue
        version = (
            enc.get("{%s}shortVersionString" % _APPCAST_NS["sparkle"])
            or enc.get("{%s}version" % _APPCAST_NS["sparkle"])
        )
        url = enc.get("url")
        size = enc.get("length")
        sig = enc.get("{%s}edSignature" % _APPCAST_NS["sparkle"])
        os_key = enc.get("{%s}os" % _APPCAST_NS["sparkle"]) or "macos"
        if not (version and url and sig):
            continue
        notes_el = item.find("{%s}releaseNotesLink" % _APPCAST_NS["sparkle"])
        items.append({
            "version": version,
            "url": url,
            "size": int(size) if size and size.isdigit() else None,
            "signature": sig,
            "os": os_key,
            "notes_url": (notes_el.text if notes_el is not None else None),
        })
    return items


def _fetch_latest_release() -> dict:
    """Returns the update-check payload. Cached in memory for _UPDATE_CHECK_TTL.
    Output shape is stable across the GitHub-API → appcast.xml migration so
    the existing banner JS keeps working unchanged."""
    now = time.time()
    with _update_cache_lock:
        cached = _update_cache["payload"]
        if cached and (now - _update_cache["fetched_at"]) < _UPDATE_CHECK_TTL:
            return cached

    payload: dict = {
        "current": APP_VERSION,
        "latest": None,
        "newer_available": False,
        "banner_recommended": False,
        "release_url": None,
        "platform_download_url": (
            "https://bitaxeballer.com/download/mac"
            if sys.platform == "darwin"
            else "https://bitaxeballer.com/download/windows"
        ),
        "released_at": None,
        "error": None,
        # In-place install is a Pro feature. Free users still see the banner
        # and get the legacy "download & install" link out to the website.
        "install_supported": _is_frozen() and bool(UPDATE_SIGNING_PUBKEY) and is_pro_active(),
        "artifact_url": None,
        "artifact_size": None,
        "artifact_signature": None,
    }

    try:
        r = requests.get(
            APPCAST_URL,
            headers={"User-Agent": f"BitaxeBaller/{APP_VERSION}"},
            timeout=5,
        )
        r.raise_for_status()
        items = _parse_appcast(r.text)
        platform = _current_platform_key()
        # Pick the highest-versioned item that matches the current OS.
        candidates = [it for it in items if it["os"] == platform]
        if candidates:
            best = max(candidates, key=lambda it: _parse_semver(it["version"]))
            payload["latest"] = best["version"]
            payload["newer_available"] = _parse_semver(best["version"]) > _parse_semver(APP_VERSION)
            payload["banner_recommended"] = payload["newer_available"] and _banner_recommended(APP_VERSION, best["version"])
            payload["release_url"] = best["notes_url"]
            payload["artifact_url"] = best["url"]
            payload["artifact_size"] = best["size"]
            payload["artifact_signature"] = best["signature"]
    except Exception as e:
        payload["error"] = type(e).__name__

    with _update_cache_lock:
        _update_cache["fetched_at"] = now
        _update_cache["payload"] = payload
    return payload


@app.route("/api/update-check")
def api_update_check():
    return jsonify(_fetch_latest_release())


# ----- AxeOS firmware update check (reads the curated catalog on the site) -----
_FIRMWARE_CATALOG_URL = os.environ.get("BBR_FIRMWARE_CATALOG_URL", "https://bitaxeballer.com/api/firmware/catalog")
_FIRMWARE_TTL = 6 * 3600
_firmware_cache = {"fetched_at": 0.0, "payload": None}
_firmware_cache_lock = threading.Lock()


def _fetch_firmware_catalog() -> dict:
    """Latest blessed AxeOS release from the curated catalog on the site. Cached 6h."""
    now = time.time()
    with _firmware_cache_lock:
        cached = _firmware_cache["payload"]
        if cached and (now - _firmware_cache["fetched_at"]) < _FIRMWARE_TTL:
            return cached
    payload = {"latest": None, "notes_url": None, "channel": None, "error": None}
    try:
        r = requests.get(_FIRMWARE_CATALOG_URL, headers={"User-Agent": f"BitaxeBaller/{APP_VERSION}"}, timeout=5)
        r.raise_for_status()
        releases = (r.json() or {}).get("releases", []) or []
        if releases:
            best = max(releases, key=lambda rel: _parse_semver(rel.get("version", "0")))
            payload["latest"] = best.get("version")
            payload["notes_url"] = best.get("notes_url")
            payload["channel"] = best.get("channel")
    except Exception as e:
        payload["error"] = type(e).__name__
    with _firmware_cache_lock:
        _firmware_cache["fetched_at"] = now
        _firmware_cache["payload"] = payload
    return payload


# Known Bitaxe board revisions (AxeOS `boardVersion`). The curated catalog ships stock
# bitaxeorg/ESP-Miner, whose OTA binaries are universal across BITAXE boards — but NOT
# across other vendors. NerdAxe/NerdQAxe run a different ESP-Miner fork; pushing the
# Bitaxe image to one would be the WRONG firmware → brick. So firmware updating is
# FAIL-CLOSED: a device is only offered the notice / flash when its boardVersion is a
# recognized Bitaxe rev. Unknown boards (a NerdAxe, anything new) are still monitored +
# tuned normally — we just never flash them. Confirmed on real hardware: 601, 602 (Gamma).
# The non-Gamma entries are provisional — tighten against a real NerdAxe before the
# v1.18.0 firmware release (ASICModel alone can't tell them apart; NerdQAxe++ is BM1370 too).
# Confirmed on real hardware: 601, 602 (Gamma). The 2xx/4xx entries are documented
# Bitaxe families but UNVERIFIED here. Deliberately NO 7xx: a NerdQaxe++ "Revision 7"
# could plausibly report a boardVersion in that range, and a false *inclusion* fails
# OPEN → the exact wrong-firmware brick this guard exists to prevent. When unsure, leave
# it OUT (a real Bitaxe just won't get the notice — annoying, not dangerous). Lock this
# down against the real NerdAxe rev7's reported boardVersion before the v1.18.0 cut, and
# ideally move it catalog-driven (the site already curates per-board).
#
# Non-Bitaxe devices that MUST stay excluded (regression-tested in
# tests/test_firmware_guard.py): the Nexus S1 (ASICModel BM1373, a different
# vendor on a 3nm S23 chip stock Bitaxe firmware doesn't target) and NerdQAxe
# (its own ESP-Miner fork). Whatever boardVersion those forks report, do NOT
# add it here — a false inclusion fails OPEN into the exact brick this prevents.
_BITAXE_BOARD_VERSIONS = {
    "200", "201", "202", "203", "204", "205",   # Ultra (BM1366) — documented, unverified
    "400", "401", "402", "403",                  # Supra (BM1368) — documented, unverified
    "600", "601", "602", "603", "604",           # Gamma (BM1370) — 601/602 hardware-confirmed
}


def _is_bitaxe_board(info: dict) -> bool:
    """True only if a device's AxeOS /api/system/info identifies it as a known Bitaxe
    board we can safely flash with stock bitaxeorg firmware. Fail-closed: unknown or
    missing boardVersion → False (monitored, but never flashed by us)."""
    if not info:
        return False
    return str(info.get("boardVersion", "")).strip() in _BITAXE_BOARD_VERSIONS


# Stock AxeOS reports a clean semantic version like "v2.14.1". Firmware FORKS running on
# genuine Bitaxe hardware (LottoAxe OS, NerdQAxe's fork, dev / -beta / -LTS builds) brand
# their version string differently — extra parts, suffixes, or names. The board allowlist
# can't catch a real Bitaxe board running someone else's firmware, so we ALSO require the
# reported version to look like stock AxeOS before ever offering an update — otherwise a
# one-click "update" could flash stock AxeOS OVER a user's custom firmware. Fail-closed.
_AXEOS_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+$")


def _is_stock_axeos(info: dict) -> bool:
    ver = str((info or {}).get("version") or (info or {}).get("axeOSVersion") or "").strip()
    return bool(_AXEOS_VERSION_RE.match(ver))


def _fw_flashable(info: dict) -> bool:
    """A device is eligible for our AxeOS update flow only if it's BOTH a recognized Bitaxe
    board AND running stock AxeOS (clean version string). This is the single gate for the
    notice, the behind-check, and the actual flash — it guards against pushing stock Bitaxe
    firmware onto another vendor's hardware OR over a Bitaxe running a custom firmware fork."""
    return _is_bitaxe_board(info) and _is_stock_axeos(info)


@app.route("/api/firmware-check")
def api_firmware_check():
    """Which tracked miners are behind the latest blessed AxeOS release. Drives the
    fleet-level firmware notice + per-card badges. Separate from /api/devices so
    that endpoint stays a backward-compatible array. Non-Bitaxe boards (e.g. a NerdAxe)
    are excluded entirely — we don't flash them Bitaxe firmware (see _is_bitaxe_board)."""
    cat = _fetch_firmware_catalog()
    latest = cat.get("latest")
    behind, total = [], 0
    if latest:
        latest_sem = _parse_semver(latest)
        with state_lock:
            for s in state.values():
                info = s.get("latest") or {}
                if not _fw_flashable(info):
                    continue   # non-Bitaxe board OR non-stock firmware — never surface an update
                total += 1
                cur = info.get("version", "")
                if cur and _parse_semver(cur) < latest_sem:
                    behind.append({"ip": s["ip"], "label": s["label"], "current": cur})
    return jsonify({
        "latest": latest,
        "notes_url": cat.get("notes_url"),
        "channel": cat.get("channel"),
        "behind": behind,
        "behind_count": len(behind),
        "total": total,
        "error": cat.get("error"),
    })


# ----- AxeOS firmware flashing (free: single-device, user-supplied files; Pro: catalog auto-fetch + bulk) -----
#
# Orchestrates the AxeOS OTA flash across one or more miners. When there are two files
# the order is a safety detail (see the spec): www.bin (web UI) FIRST — it does NOT
# reboot — then esp-miner.bin (firmware) LAST, which reboots the device. If the upload
# format is ever wrong, OTAWWW fails before anything reboots, so the device is left
# untouched.
#
# AxeOS v2.15.0 embedded the web UI into esp-miner.bin and dropped www.bin, so from
# that release on there is only one upload and the flashing_www phase is skipped. Note
# that this also removes the no-reboot canary above: the sha256 check in
# _ensure_firmware_binary is then the only thing standing between a bad download and a
# device rebooting into it. Never substitute esp-miner.bin for www.bin to "keep the
# pair" — that writes a firmware image to the web-UI partition.
#
# Per-device phases: queued → (downloading) → pausing → [flashing_www] → flashing_firmware
#                    → rebooting → verifying → done | failed | skipped
# Devices flash SEQUENTIALLY with stop-on-failure — one bad release can't take out the
# whole fleet at once. The UI polls /api/firmware/flash-progress.

FIRMWARE_CACHE_DIR = os.path.join(_DATA_DIR, "firmware-cache")
_firmware_full_cache = {"fetched_at": 0.0, "releases": None}


def _fetch_firmware_releases() -> list:
    """Full blessed-release list (with assets + sha256) from the curated catalog. Cached 6h.
    Separate from _fetch_firmware_catalog(), which trims to just the latest version string."""
    now = time.time()
    with _firmware_cache_lock:
        if _firmware_full_cache["releases"] is not None and (now - _firmware_full_cache["fetched_at"]) < _FIRMWARE_TTL:
            return _firmware_full_cache["releases"]
    releases = []
    try:
        r = requests.get(_FIRMWARE_CATALOG_URL, headers={"User-Agent": f"BitaxeBaller/{APP_VERSION}"}, timeout=8)
        r.raise_for_status()
        releases = (r.json() or {}).get("releases", []) or []
    except Exception:
        releases = []
    with _firmware_cache_lock:
        _firmware_full_cache["fetched_at"] = now
        _firmware_full_cache["releases"] = releases
    return releases


def _firmware_pair_for(version=None) -> "dict | None":
    """The universal OTA assets for a blessed version (latest blessed if version is
    None). Returns {'version', 'www': {url,sha256,size} | None, 'firmware': {...}} or
    None if the version isn't blessed / has no firmware asset.

    `www` is optional: AxeOS v2.15.0 embedded the web UI into esp-miner.bin and
    stopped shipping www.bin. Releases at v2.14.x and older still ship both and are
    still flashed as a pair. esp-miner.bin is the one asset we cannot do without."""
    releases = _fetch_firmware_releases()
    if not releases:
        return None
    if version:
        rel = next((r for r in releases if r.get("version") == version), None)
    else:
        rel = max(releases, key=lambda r: _parse_semver(r.get("version", "0")))
    if not rel:
        return None
    pair = {"version": rel.get("version"), "www": None, "firmware": None}
    for a in rel.get("assets", []) or []:
        # board_version 0 == universal OTA (applies to every board)
        if a.get("board_version") in (0, None) and a.get("kind") in ("www", "firmware"):
            pair[a["kind"]] = {"url": a.get("url"), "sha256": a.get("sha256"), "size": a.get("size")}
    if not pair["firmware"]:
        return None
    return pair


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _firmware_cached_path(version, kind) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", version or "unknown")
    d = os.path.join(FIRMWARE_CACHE_DIR, safe)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{kind}.bin")


def _ensure_firmware_binary(version, kind, asset) -> str:
    """Download (if not already cached + valid) and sha256-verify a binary; return its
    local path. Raises ValueError on checksum mismatch — we NEVER flash an unverified binary."""
    path = _firmware_cached_path(version, kind)
    if os.path.exists(path) and _sha256_file(path) == asset.get("sha256"):
        return path
    tmp = path + ".part"
    with requests.get(asset["url"], stream=True, timeout=60,
                      headers={"User-Agent": f"BitaxeBaller/{APP_VERSION}"}) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
    got = _sha256_file(tmp)
    if got != asset.get("sha256"):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ValueError(f"checksum mismatch on {kind}: expected {str(asset.get('sha256'))[:12]}…, got {got[:12]}…")
    os.replace(tmp, path)
    return path


def _ota_upload(ip, endpoint, data: bytes, timeout=180) -> None:
    """Stream a binary to an AxeOS OTA endpoint. AxeOS reads the raw request body
    (octet-stream), not multipart. OTAWWW = web UI (no reboot); OTA = firmware (reboots)."""
    r = requests.post(
        f"http://{ip}/api/system/{endpoint}",
        data=data,
        headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(data))},
        timeout=timeout,
    )
    r.raise_for_status()


def _device_version(ip, timeout=4) -> str:
    info = fetch_device(ip, timeout=timeout)
    return info.get("version") or info.get("axeOSVersion") or ""


def _wait_for_version(ip, target, timeout=200) -> bool:
    """Poll a rebooting device until it reports >= target version (or timeout)."""
    tsem = _parse_semver(target)
    deadline = time.time() + timeout
    time.sleep(8)  # let the reboot actually start before we poll
    while time.time() < deadline:
        try:
            cur = _device_version(ip)
            if cur and _parse_semver(cur) >= tsem:
                return True
        except Exception:
            pass  # device is mid-reboot / not yet answering — expected
        time.sleep(5)
    return False


# Flash job state — single job at a time (sequential, stop-on-failure).
_flash_state = {
    "active": False,
    "done": False,
    "version": None,
    "started_at": 0,
    "error": None,
    "devices": [],   # [{ip, label, phase, error}]
}
_flash_lock = threading.Lock()
_flash_thread: "threading.Thread | None" = None


def _flash_set(ip, phase, error=None):
    with _flash_lock:
        for d in _flash_state["devices"]:
            if d["ip"] == ip:
                d["phase"] = phase
                if error is not None:
                    d["error"] = error
                break


def _flash_worker(target_version, items, www_path, fw_path, from_catalog):
    """Run the sequential flash job. items = [{ip,label}]. When from_catalog, www_path/
    fw_path are None up front and resolved (download+verify) inside; otherwise they are
    user-supplied file paths."""
    try:
        if from_catalog:
            for it in items:
                _flash_set(it["ip"], "downloading")
            pair = _firmware_pair_for(target_version)
            if not pair:
                raise ValueError(f"no blessed firmware in catalog for {target_version or 'latest'}")
            target_version = pair["version"]
            with _flash_lock:
                _flash_state["version"] = target_version
            www_path = (_ensure_firmware_binary(target_version, "www", pair["www"])
                        if pair["www"] else None)
            fw_path = _ensure_firmware_binary(target_version, "firmware", pair["firmware"])

        # www_data stays None for v2.15.0+ (web UI embedded in esp-miner.bin) and for a
        # manual upload where the user only had the one file.
        www_data = None
        if www_path:
            with open(www_path, "rb") as f:
                www_data = f.read()
        with open(fw_path, "rb") as f:
            fw_data = f.read()

        # Binaries are in hand. The catalog path briefly marks everything "downloading"
        # during the single upfront fetch — reset the not-yet-started ones to "queued"
        # so a miner waiting its turn doesn't read "downloading" the whole time.
        for it in items:
            _flash_set(it["ip"], "queued")

        for it in items:
            ip, label = it["ip"], it["label"]
            try:
                # Catalog flashes push stock Bitaxe firmware — refuse unless the device is
                # BOTH a recognized Bitaxe board AND running stock AxeOS, even if the API was
                # hit directly. This prevents clobbering another vendor's hardware or a Bitaxe
                # running a custom firmware fork (LottoAxe, NerdQAxe, etc.). (Manual uploads
                # are the user's own files + explicit choice, so they're exempt.)
                if from_catalog:
                    try:
                        if not _fw_flashable(fetch_device(ip)):
                            _flash_set(ip, "skipped", error="not a stock-AxeOS Bitaxe — won't flash Bitaxe firmware over it")
                            continue
                    except Exception:
                        pass  # unreachable now will surface on the flash attempt below
                # Already current? (Pro bulk may include mixed versions.) Skip, don't reflash.
                if target_version and target_version != "(manual)":
                    try:
                        cur = _device_version(ip)
                        if cur and _parse_semver(cur) >= _parse_semver(target_version):
                            _flash_set(ip, "skipped")
                            continue
                    except Exception:
                        pass  # unreachable here will surface on the pause/flash below

                _flash_set(ip, "pausing")
                try:
                    requests.post(f"http://{ip}/api/system/pause", timeout=5)
                except Exception:
                    pass  # best-effort; mining also stops during flash anyway

                if www_data is not None:
                    _flash_set(ip, "flashing_www")
                    _ota_upload(ip, "OTAWWW", www_data)  # web UI first — no reboot

                _flash_set(ip, "flashing_firmware")
                try:
                    _ota_upload(ip, "OTA", fw_data)      # firmware last — reboots
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.ReadTimeout,
                        requests.exceptions.ChunkedEncodingError):
                    # The device often reboots before sending its HTTP response, dropping the
                    # connection. That's expected on success — the version poll below is the
                    # real verdict, so don't treat a post-upload disconnect as a failure.
                    pass

                _flash_set(ip, "rebooting")
                if target_version and target_version != "(manual)":
                    ok = _wait_for_version(ip, target_version)
                    _flash_set(ip, "verifying")
                    if not ok:
                        _flash_set(ip, "failed", error="device didn't report the new version in time")
                        with _flash_lock:
                            _flash_state["error"] = f"{label}: version not confirmed after flash"
                        break  # stop-on-failure
                else:
                    # Manual files: we can't know the target version string — give it time to come back.
                    time.sleep(20)
                    try:
                        _device_version(ip)
                    except Exception:
                        pass
                _flash_set(ip, "done")
                log_event(ip, f"AxeOS firmware flashed → {target_version}")
            except Exception as e:
                _flash_set(ip, "failed", error=str(e)[:160])
                with _flash_lock:
                    _flash_state["error"] = f"{label}: {str(e)[:140]}"
                break  # stop-on-failure
    except Exception as e:
        with _flash_lock:
            _flash_state["error"] = str(e)[:200]
            for d in _flash_state["devices"]:
                if d["phase"] in ("queued", "downloading"):
                    d["phase"] = "failed"
    finally:
        if not from_catalog:   # user-supplied uploads were saved to temp files — clean them up
            for pth in (www_path, fw_path):
                try:
                    if pth and os.path.exists(pth):
                        os.remove(pth)
                except OSError:
                    pass
        with _flash_lock:
            _flash_state["active"] = False
            _flash_state["done"] = True


@app.route("/api/firmware/flash", methods=["POST"])
def api_firmware_flash():
    """Start a firmware flash job.

    Two request shapes:
      • multipart/form-data — FREE single-device manual flash: field `ip`, file
        `firmware` (required) and `www` (optional — AxeOS v2.15.0+ has no www.bin).
        Single device only.
      • application/json — PRO catalog flash: `{ips:[...], version?}` (auto-fetches the
        blessed binaries; version defaults to latest blessed). Bulk (>1) is Pro-only.
    """
    global _flash_thread
    with _flash_lock:
        if _flash_state["active"]:
            return jsonify({"error": "A firmware update is already running"}), 409

    items, www_path, fw_path, from_catalog, target_version = [], None, None, False, None

    if request.content_type and "multipart/form-data" in request.content_type:
        ip = (request.form.get("ip") or "").strip()
        if not ip:
            return jsonify({"error": "ip required"}), 400
        www = request.files.get("www")
        fw = request.files.get("firmware")
        # www.bin is optional — AxeOS v2.15.0+ ships esp-miner.bin only (web UI embedded),
        # so a user updating to it has just the one file. esp-miner.bin is required.
        if not fw:
            return jsonify({"error": "esp-miner.bin is required"}), 400
        os.makedirs(FIRMWARE_CACHE_DIR, exist_ok=True)
        import tempfile
        www_path = None
        if www:
            wfd, www_path = tempfile.mkstemp(suffix="_www.bin", dir=FIRMWARE_CACHE_DIR); os.close(wfd)
            www.save(www_path)
        ffd, fw_path = tempfile.mkstemp(suffix="_esp-miner.bin", dir=FIRMWARE_CACHE_DIR); os.close(ffd)
        fw.save(fw_path)
        target_version = "(manual)"
        with state_lock:
            label = state.get(ip, {}).get("label", ip)
        items = [{"ip": ip, "label": label}]
    else:
        body = request.get_json(force=True) or {}
        ips = body.get("ips") or ([body["ip"]] if body.get("ip") else [])
        ips = [str(x).strip() for x in ips if str(x).strip()]
        if not ips:
            return jsonify({"error": "ips required"}), 400
        # Catalog auto-fetch is a Pro convenience; bulk is Pro. Free must supply files (multipart).
        if not is_pro_active():
            return jsonify({"error": "Auto-update from the catalog is a Pro feature. On the free tier, update a single miner by supplying the two .bin files yourself."}), 403
        if len(ips) > 1 and not is_pro_active():
            return jsonify({"error": "Bulk update is a Pro feature"}), 403
        from_catalog = True
        target_version = body.get("version")
        with state_lock:
            items = [{"ip": ip, "label": state.get(ip, {}).get("label", ip)} for ip in ips]

    with _flash_lock:
        _flash_state.update({
            "active": True, "done": False, "version": target_version,
            "started_at": time.time(), "error": None,
            "devices": [{"ip": it["ip"], "label": it["label"], "phase": "queued", "error": None} for it in items],
        })
    _flash_thread = threading.Thread(
        target=_flash_worker, args=(target_version, items, www_path, fw_path, from_catalog), daemon=True)
    _flash_thread.start()
    return jsonify({"ok": True, "started": True, "count": len(items)})


@app.route("/api/firmware/flash-progress")
def api_firmware_flash_progress():
    with _flash_lock:
        return jsonify({
            "active": _flash_state["active"],
            "done": _flash_state["done"],
            "version": _flash_state["version"],
            "error": _flash_state["error"],
            "devices": [dict(d) for d in _flash_state["devices"]],
        })


@app.route("/api/devices/identify", methods=["POST"])
def api_device_identify():
    """Blink a miner's screen/LED so the user can tell which physical box it is."""
    ip = (request.get_json(force=True) or {}).get("ip", "").strip()
    if not ip:
        return jsonify({"error": "ip required"}), 400
    blocked = _monitor_only_error(ip)
    if blocked:
        return blocked
    try:
        requests.post(f"http://{ip}/api/system/identify", timeout=5)
    except Exception as e:
        return jsonify({"error": str(e)[:120]}), 502
    return jsonify({"ok": True})


# ----- In-place auto-install -----
#
# State machine: idle → downloading → verifying → installing → relaunching → (process exit)
# On any failure: → failed (with error message). The UI polls /api/update-progress
# every 500ms while running; on "relaunching" it disconnects and waits for the
# new app to come back up.

_install_state: dict = {
    "phase": "idle",          # idle | downloading | verifying | installing | relaunching | failed
    "progress": 0,            # 0..100 (downloading phase only; others are instantaneous-ish)
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "version": None,
    "error": None,
}
_install_state_lock = threading.Lock()
_install_thread: "threading.Thread | None" = None


def _set_install_state(**kwargs) -> None:
    with _install_state_lock:
        _install_state.update(kwargs)


def _running_app_bundle_path() -> "str | None":
    """On a frozen Mac build, returns the absolute path to the .app bundle that
    is currently running. None if not frozen or not on Mac."""
    if not _is_frozen() or sys.platform != "darwin":
        return None
    exe = os.path.realpath(sys.executable)
    # exe is .../Bitaxe Baller.app/Contents/MacOS/Bitaxe Baller
    # walk up to the .app
    p = exe
    for _ in range(4):
        p = os.path.dirname(p)
        if p.endswith(".app"):
            return p
    return None


def _verify_signature(data: bytes, sig_b64: str) -> "tuple[bool, str]":
    """Verify an Ed25519 signature against UPDATE_SIGNING_PUBKEY.
    Returns (ok, detail) — detail is empty on success, or describes the failure
    so the UI / logs can surface a useful message rather than just 'failed'."""
    if not UPDATE_SIGNING_PUBKEY:
        return False, "no pubkey embedded in build"
    try:
        import base64
        from nacl.signing import VerifyKey
        vk = VerifyKey(base64.b64decode(UPDATE_SIGNING_PUBKEY))
        vk.verify(data, base64.b64decode(sig_b64))
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _download_artifact(url: str, dest: str, expected_size: "int | None") -> None:
    """Stream the artifact to dest, updating _install_state.progress as we go."""
    _set_install_state(phase="downloading", progress=0, downloaded_bytes=0,
                       total_bytes=expected_size or 0, error=None)
    with requests.get(url, stream=True, timeout=30,
                      headers={"User-Agent": f"BitaxeBaller/{APP_VERSION}"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or expected_size or 0)
        _set_install_state(total_bytes=total)
        written = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
                if total > 0:
                    pct = min(100, int(written * 100 / total))
                    _set_install_state(progress=pct, downloaded_bytes=written)
                else:
                    _set_install_state(downloaded_bytes=written)


def _spawn_mac_installer(dmg_path: str, target_app: str) -> None:
    """Spawn a detached shell script that waits for our PID to exit, mounts the
    DMG, swaps the bundle, detaches, and relaunches. The script lives in the
    user's temp dir so it survives our process exit."""
    import tempfile
    pid = os.getpid()
    mount_point = tempfile.mkdtemp(prefix="bitaxe-update-")
    script_path = os.path.join(tempfile.gettempdir(), f"bitaxe-update-{pid}.sh")
    log_path = os.path.join(tempfile.gettempdir(), f"bitaxe-update-{pid}.log")
    script = f"""#!/bin/sh
set -e
exec >"{log_path}" 2>&1
echo "[update] waiting for pid {pid} to exit"
while kill -0 {pid} 2>/dev/null; do sleep 0.3; done
echo "[update] parent exited, mounting dmg"
hdiutil attach -nobrowse -noautoopen -mountpoint "{mount_point}" "{dmg_path}"
NEW_APP=$(/bin/ls -d "{mount_point}"/*.app 2>/dev/null | head -1)
if [ -z "$NEW_APP" ]; then
  echo "[update] no .app inside dmg, aborting"
  hdiutil detach "{mount_point}" -force || true
  exit 1
fi
echo "[update] swapping $NEW_APP -> {target_app}"
rm -rf "{target_app}"
/usr/bin/ditto "$NEW_APP" "{target_app}"
hdiutil detach "{mount_point}" -force || true
rm -f "{dmg_path}"
echo "[update] relaunching"
open "{target_app}"
"""
    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)
    # Detach completely so the script outlives this process.
    import subprocess
    subprocess.Popen(
        ["/bin/sh", script_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _spawn_win_installer(exe_path: str) -> None:
    """Inno Setup installers handle running-app replacement natively (the
    RestartManager dialog), so we just spawn it and exit. /SILENT skips the
    wizard UI but still shows progress. /VERYSILENT would be a worse UX —
    users want to see *something* happening when their app vanishes."""
    import subprocess
    subprocess.Popen(
        [exe_path, "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )


def _do_install(payload: dict) -> None:
    """Background worker. Runs the full download → verify → swap → relaunch sequence."""
    import tempfile
    try:
        url = payload["artifact_url"]
        sig = payload["artifact_signature"]
        size = payload.get("artifact_size")
        version = payload["latest"]

        suffix = ".dmg" if sys.platform == "darwin" else ".exe"
        fd, dl_path = tempfile.mkstemp(prefix="bitaxe-update-", suffix=suffix)
        os.close(fd)

        _set_install_state(version=version)
        _download_artifact(url, dl_path, size)

        _set_install_state(phase="verifying", progress=100)
        with open(dl_path, "rb") as f:
            data = f.read()
        # Also log key inputs so any verify failure is debuggable from the log
        import hashlib
        print(f"[update] verifying: dl_size={len(data)} expected={size} "
              f"dl_sha256={hashlib.sha256(data).hexdigest()[:16]}... "
              f"sig_b64_len={len(sig)} pubkey_b64_len={len(UPDATE_SIGNING_PUBKEY)}",
              file=sys.stderr, flush=True)
        ok, detail = _verify_signature(data, sig)
        if not ok:
            try: os.remove(dl_path)
            except OSError: pass
            print(f"[update] signature verify FAILED: {detail}", file=sys.stderr, flush=True)
            _set_install_state(phase="failed", error=f"signature_verify_failed: {detail}")
            return
        print("[update] signature OK", file=sys.stderr, flush=True)

        _set_install_state(phase="installing")
        if sys.platform == "darwin":
            target = _running_app_bundle_path()
            if not target:
                _set_install_state(phase="failed", error="cannot_locate_running_app")
                return
            _spawn_mac_installer(dl_path, target)
        elif sys.platform == "win32":
            _spawn_win_installer(dl_path)
        else:
            _set_install_state(phase="failed", error="unsupported_platform")
            return

        _set_install_state(phase="relaunching")
        # Give the response time to flush, then exit so the trampoline can swap.
        def _exit_soon():
            time.sleep(1.5)
            os._exit(0)
        threading.Thread(target=_exit_soon, daemon=True).start()
    except Exception as e:
        _set_install_state(phase="failed", error=f"{type(e).__name__}: {e}")


@app.route("/api/update-install", methods=["POST"])
def api_update_install():
    global _install_thread
    if not _is_frozen():
        return jsonify({"error": "Auto-install requires the packaged app. Update manually from bitaxeballer.com."}), 400
    if not UPDATE_SIGNING_PUBKEY:
        return jsonify({"error": "Auto-install is disabled in this build (no public key embedded)."}), 400
    # In-place auto-update is a Pro feature. Free users keep the existing
    # banner-with-download-link UX shipped in v1.7.x.
    if not is_pro_active():
        return jsonify({"error": "Auto-install is a Pro feature."}), 402

    with _install_state_lock:
        if _install_state["phase"] not in ("idle", "failed"):
            return jsonify({"error": f"Install already in progress ({_install_state['phase']})."}), 409

    payload = _fetch_latest_release()
    if not payload.get("newer_available") or not payload.get("artifact_url") or not payload.get("artifact_signature"):
        return jsonify({"error": "No newer version available."}), 400

    _set_install_state(phase="downloading", progress=0, downloaded_bytes=0,
                       total_bytes=payload.get("artifact_size") or 0,
                       version=payload["latest"], error=None)
    _install_thread = threading.Thread(target=_do_install, args=(payload,), daemon=True)
    _install_thread.start()
    return jsonify({"started": True, "version": payload["latest"]})


@app.route("/api/update-progress")
def api_update_progress():
    with _install_state_lock:
        return jsonify(dict(_install_state))


@app.route("/api/lan-info")
def api_lan_info():
    """Lightweight LAN-introspection endpoint. Used by the dashboard to render
    the scanning animation when the page is loaded via localhost or
    bitaxe-baller.local (i.e. the URL has no IP literal to derive the subnet from)."""
    lan_ip = detect_lan_ip()
    override = _scan_subnet_override()
    if override:
        return jsonify({"lan_ip": lan_ip, "subnet_prefix": override, "subnet": f"{override}.0/24"})
    if not lan_ip or not _is_private_v4(lan_ip):
        return jsonify({"lan_ip": lan_ip, "subnet_prefix": None, "subnet": None})
    prefix = ".".join(lan_ip.split(".")[:3])
    return jsonify({"lan_ip": lan_ip, "subnet_prefix": prefix, "subnet": f"{prefix}.0/24"})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Scan the host's /24 LAN for Bitaxes by probing /api/system/info on
    each address in parallel. Returns the ones that respond. Already-added
    devices and the host itself are skipped."""
    lan_ip = detect_lan_ip()
    override = _scan_subnet_override()
    if override:
        # Bridged container: our own IP is on the Docker network, so trust the
        # operator-supplied LAN /24 instead of auto-detection. lan_ip (a bridge
        # address) is still fine for the skip-self filter — it won't match a LAN
        # candidate — so we don't hard-fail when it isn't private here.
        base = override
    else:
        if not lan_ip or not _is_private_v4(lan_ip):
            return jsonify({"error": "couldn't detect a private LAN IP to scan"}), 400
        base = ".".join(lan_ip.split(".")[:3])

    with config_lock:
        existing = {d["ip"] for d in load_config().get("devices", [])}

    candidates = [
        f"{base}.{i}" for i in range(1, 255)
        if f"{base}.{i}" != lan_ip and f"{base}.{i}" not in existing
    ]

    def probe_braiins(ip):
        # Braiins OS (BMM-101 etc.) speaks the CGMiner TCP API on :4028, not
        # AxeOS HTTP. Only reached when the HTTP probe found nothing, so the
        # common case (empty IP) already failed fast.
        try:
            d = fetch_braiins(ip, timeout=1.5)
            return {
                "ip": ip,
                "hostname": d.get("hostname", ""),
                "model": d.get("ASICModel", ""),
                "version": d.get("version", ""),
                "hashRate": round(d.get("hashRate", 0), 0),
            }
        except Exception:
            return None

    def probe(ip):
        try:
            r = requests.get(
                f"http://{ip}/api/system/info",
                timeout=1.5,
                headers={"User-Agent": "Bitaxe-Baller-Scanner"},
            )
            if r.status_code != 200:
                return None
            d = r.json()
            # Crude Bitaxe sniff — every Bitaxe response carries hashRate + ASICModel.
            if "hashRate" not in d or "ASICModel" not in d:
                return None
            return {
                "ip": ip,
                "hostname": d.get("hostname", ""),
                "model": d.get("ASICModel", ""),
                "version": d.get("version", ""),
                "hashRate": round(d.get("hashRate", 0), 0),
            }
        except ValueError:
            # HTTP 200 but not JSON — a Braiins OS SPA answers every path
            # with its HTML shell, so this is exactly the BMM fingerprint.
            return probe_braiins(ip)
        except requests.exceptions.Timeout:
            # No HTTP answer at all — almost certainly an empty IP. Don't
            # spend another connect-timeout probing :4028 on it.
            return None
        except Exception:
            # Host answered but not usefully (port 80 closed/refused, weird
            # server) — cheap to check for a Braiins CGMiner API before giving up.
            return probe_braiins(ip)

    found = []
    with ThreadPoolExecutor(max_workers=64) as ex:
        for result in ex.map(probe, candidates, timeout=15):
            if result:
                found.append(result)

    found.sort(key=lambda x: tuple(int(p) for p in x["ip"].split(".")))
    return jsonify({
        "found": found,
        "scanned": len(candidates),
        "subnet": f"{base}.0/24",
        "host": lan_ip,
        "skipped_existing": len(existing),
    })


@app.route("/api/devices/add", methods=["POST"])
def api_device_add():
    body = request.get_json(force=True)
    ip = (body.get("ip") or "").strip()
    label = (body.get("label") or "").strip()

    if not ip:
        return jsonify({"error": "IP required"}), 400
    if not label:
        label = ip.replace(".", "-")

    # Try to reach it
    try:
        info = fetch_device(ip, timeout=4)
    except Exception as e:
        return jsonify({"error": f"Could not reach {ip}: {str(e)[:120]}"}), 400

    new_mac = (info.get("macAddr") or "").strip().lower()

    with config_lock:
        cfg = load_config()
        if any(d["ip"] == ip for d in cfg["devices"]):
            return jsonify({"error": f"{ip} is already added"}), 400

        # MAC-based dedup. If the new device's MAC matches one we already
        # track at a different IP, this is the same physical Bitaxe — the
        # user probably just switched DHCP ↔ static or relocated it. Quietly
        # re-bind: drop the old IP entry, carry over its label and event log
        # to the new one. Without this, scan → set-static-IP → re-add yields
        # two cards for one miner, one of them perpetually offline.
        rebound_from = None
        rebound_label = None
        if new_mac:
            with state_lock:
                for d in list(cfg["devices"]):
                    existing_ip = d["ip"]
                    s = state.get(existing_ip)
                    if not s:
                        continue
                    latest = s.get("latest") or {}
                    existing_mac = (latest.get("macAddr") or "").strip().lower()
                    if existing_mac and existing_mac == new_mac:
                        rebound_from = existing_ip
                        rebound_label = d.get("label")
                        cfg["devices"] = [x for x in cfg["devices"] if x["ip"] != existing_ip]
                        state.pop(existing_ip, None)
                        break

        # Preserve the user's existing label across the rebind unless they
        # passed an explicit override in the body.
        if rebound_label and not (body.get("label") or "").strip():
            label = rebound_label

        device_type = info.get("deviceType") or "axeos"
        entry = {"ip": ip, "label": label}
        if device_type != "axeos":
            entry["type"] = device_type
        cfg["devices"].append(entry)
        save_config(cfg)

    with state_lock:
        state[ip] = init_device_state(ip, label, device_type=device_type)

    if rebound_from:
        log_event(ip, f"Device re-bound from {rebound_from} (same MAC {new_mac})")
    elif device_type == "braiins":
        log_event(ip, f"Device added (model: {info.get('ASICModel', '?')}, fw: {info.get('version', '?')}) — Braiins OS, monitor-only")
    else:
        log_event(ip, f"Device added (model: {info.get('ASICModel', '?')}, fw: {info.get('version', '?')})")
    return jsonify({
        "ok": True,
        "ip": ip,
        "label": label,
        "model": info.get("ASICModel"),
        "rebound_from": rebound_from,
    })


@app.route("/api/devices/remove", methods=["POST"])
def api_device_remove():
    body = request.get_json(force=True)
    ip = body.get("ip")
    if not ip:
        return jsonify({"error": "IP required"}), 400

    with config_lock:
        cfg = load_config()
        cfg["devices"] = [d for d in cfg["devices"] if d["ip"] != ip]
        save_config(cfg)

    with state_lock:
        state.pop(ip, None)

    return jsonify({"ok": True})


@app.route("/api/devices/rename", methods=["POST"])
def api_device_rename():
    body = request.get_json(force=True)
    ip = body.get("ip")
    label = (body.get("label") or "").strip()
    if not ip or not label:
        return jsonify({"error": "ip and label required"}), 400

    with config_lock:
        cfg = load_config()
        for d in cfg["devices"]:
            if d["ip"] == ip:
                d["label"] = label
        save_config(cfg)

    with state_lock:
        if ip in state:
            state[ip]["label"] = label

    return jsonify({"ok": True})


# Chains a user can manually pin a device to. Matches the chain_name map +
# the stats fetchers; auto-detection remains the default.
_VALID_CHAINS = {"btc", "bch", "bsv", "xec", "dgb", "nmc"}


@app.route("/api/devices/chain", methods=["POST"])
def api_device_chain():
    """Manually pin a device's chain, overriding auto-detection. Send chain="" or
    "auto" to clear and return to auto-detect. Needed when a miner points at a
    private-IP node with a legacy (1.../3...) address — those are byte-identical
    between BTC and BCH, so the detector can't tell them apart and defaults to BTC."""
    body = request.get_json(force=True)
    ip = body.get("ip")
    chain = (body.get("chain") or "").strip().lower()
    if not ip:
        return jsonify({"error": "ip required"}), 400
    if chain in ("", "auto"):
        chain = None
    elif chain not in _VALID_CHAINS:
        return jsonify({"error": f"chain must be one of {sorted(_VALID_CHAINS)} or 'auto'"}), 400

    with config_lock:
        cfg = load_config()
        found = False
        for d in cfg["devices"]:
            if d["ip"] == ip:
                found = True
                if chain:
                    d["chain"] = chain
                else:
                    d.pop("chain", None)
        if not found:
            return jsonify({"error": "device not tracked"}), 404
        save_config(cfg)

    with state_lock:
        if ip in state:
            state[ip]["chain_override"] = chain

    return jsonify({"ok": True, "chain": chain or "auto"})


@app.route("/api/devices/tune", methods=["POST"])
def api_device_tune():
    """Apply settings (frequency, coreVoltage, fanspeed, autofanspeed) to a device."""
    body = request.get_json(force=True)
    ip = body.get("ip")
    if not ip:
        return jsonify({"error": "IP required"}), 400
    blocked = _monitor_only_error(ip) or _tuning_unsupported_error(ip)
    if blocked:
        return blocked

    settings = {}
    for key in ("frequency", "coreVoltage", "fanspeed", "autofanspeed"):
        if key in body and body[key] is not None:
            try:
                val = int(body[key])
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} must be a number"}), 400

            if key in BOUNDS:
                lo, hi = BOUNDS[key]
                if val < lo or val > hi:
                    return jsonify({"error": f"{key}={val} outside safe range {lo}-{hi}"}), 400
            settings[key] = val

    if not settings:
        return jsonify({"error": "No settings to apply"}), 400

    try:
        patch_device(ip, settings)
    except Exception as e:
        return jsonify({"error": f"Failed to apply: {str(e)[:120]}"}), 500

    parts = ", ".join(f"{k}={v}" for k, v in settings.items())
    log_event(ip, f"Tuning applied: {parts}")

    # Settings are usually applied without restart, but bump any ongoing baseline tracking
    with state_lock:
        if ip in state:
            # Reset hardware-error baseline so we measure errors at the new setting
            s = state[ip]
            if s["latest"]:
                s["session_shares_start"] = s["latest"].get("sharesAccepted", 0)
                s["session_hwerrors_start"] = s["latest"].get("sharesRejected", 0)
                s["session_start"] = time.time()

    return jsonify({"ok": True, "applied": settings})


@app.route("/api/devices/preset", methods=["POST"])
def api_device_preset():
    body = request.get_json(force=True)
    ip = body.get("ip")
    name = body.get("preset")
    if not ip or name not in PRESETS:
        return jsonify({"error": "Bad preset"}), 400
    blocked = _monitor_only_error(ip) or _tuning_unsupported_error(ip)
    if blocked:
        return blocked
    p = PRESETS[name]
    settings = {"frequency": p["frequency"], "coreVoltage": p["coreVoltage"]}
    try:
        patch_device(ip, settings)
    except Exception as e:
        return jsonify({"error": f"Failed: {str(e)[:120]}"}), 500
    log_event(ip, f"Preset applied: {p['label']} ({p['frequency']} MHz / {p['coreVoltage']} mV)")
    with state_lock:
        if ip in state and state[ip]["latest"]:
            s = state[ip]
            s["session_shares_start"] = s["latest"].get("sharesAccepted", 0)
            s["session_hwerrors_start"] = s["latest"].get("sharesRejected", 0)
            s["session_start"] = time.time()
    return jsonify({"ok": True, "applied": settings, "preset": p["label"]})


@app.route("/api/alerts/config", methods=["GET"])
def api_alerts_config_get():
    """Return the alerts config. Webhook URLs are not secrets in the strict
    sense (anyone with one can post to your Discord channel), but they're
    sensitive enough that we mask them in the response by default. Email
    addresses are NOT masked — they're the user's own and showing the
    saved value is the obvious UX."""
    cfg = _alerts_get_config()
    # Mask the webhook so it never round-trips back through XHR or screen
    # recordings. The UI shows the masked value as a placeholder; if the user
    # wants to change it, they paste a new one.
    webhook = cfg.get("channels", {}).get("discord_webhook", "")
    if webhook:
        cfg["channels"]["discord_webhook_masked"] = webhook[:36] + "…" + webhook[-6:] if len(webhook) > 50 else "***"
        cfg["channels"]["discord_webhook"] = ""
    # Email address: keep visible so the user knows what's saved.
    cfg["channels"].setdefault("email_to", "")
    return jsonify({"pro_active": is_pro_active(), **cfg})


@app.route("/api/alerts/config", methods=["POST"])
def api_alerts_config_set():
    if not is_pro_active():
        return jsonify({"error": "Alerts are a Pro feature.", "code": "pro_required"}), 402
    body = request.get_json(force=True) or {}
    cur = _alerts_get_config()
    if "enabled" in body:
        cur["enabled"] = bool(body["enabled"])
    if "cooldown_minutes" in body:
        try:
            cur["cooldown_minutes"] = max(1, min(1440, int(body["cooldown_minutes"])))
        except (TypeError, ValueError):
            return jsonify({"error": "cooldown_minutes must be an integer"}), 400
    if isinstance(body.get("rules"), dict):
        for k, v in body["rules"].items():
            if k in ("offline_minutes", "vr_temp_c", "asic_temp_c"):
                try:
                    cur["rules"][k] = max(0, min(200, int(v)))
                except (TypeError, ValueError):
                    return jsonify({"error": f"rules.{k} must be an integer"}), 400
    new_channel_added = False  # track whether we crossed an empty → set boundary
    if isinstance(body.get("channels"), dict):
        webhook = body["channels"].get("discord_webhook")
        if webhook is not None:
            webhook = str(webhook).strip()
            if webhook and not webhook.startswith("https://discord.com/api/webhooks/"):
                return jsonify({"error": "Discord webhook URL must start with https://discord.com/api/webhooks/"}), 400
            if webhook and not (cur["channels"].get("discord_webhook") or "").strip():
                new_channel_added = True
            cur["channels"]["discord_webhook"] = webhook
        email_to = body["channels"].get("email_to")
        if email_to is not None:
            email_to = str(email_to).strip()
            # Loose email regex — full RFC 5322 is overkill for our use; we
            # just want to catch obvious typos. The site-side relay does the
            # authoritative validation when the alert actually fires.
            if email_to and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_to):
                return jsonify({"error": "Email address looks malformed"}), 400
            if email_to and not (cur["channels"].get("email_to") or "").strip():
                new_channel_added = True
            cur["channels"]["email_to"] = email_to
    _alerts_save_config(cur)

    # Suppress the alert-storm-on-first-channel-add. Nathan caught this in
    # v1.16.0: he restarted the app (which cleared the in-memory cooldown
    # state), then added his email address to the alerts panel. The next
    # poll fired alerts for every miner that already had a tripping
    # condition — five emails in one shot, all for state that had already
    # been pinged to Discord. Pre-arm every (device, trigger) cooldown
    # entry to NOW whenever a NEW channel is being added so the user's
    # fresh destination only sees alerts that trip AFTER they enabled it.
    # The existing channel's pings already covered the historical state.
    if new_channel_added:
        cooldown_s = int(cur.get("cooldown_minutes", 30)) * 60  # for log only
        with alerts_lock:
            now = time.time()
            with state_lock:
                for dev_ip in state.keys():
                    _alerts_last_fired.setdefault(dev_ip, {})
                    for trig in ("offline", "vr_temp", "asic_temp"):
                        _alerts_last_fired[dev_ip][trig] = now
        print(f"[alerts] channel added — primed cooldowns for {len(state)} device(s) (~{cooldown_s // 60}min quiet)")

    return jsonify({"ok": True})


@app.route("/api/alerts/test", methods=["POST"])
def api_alerts_test():
    """Send a test message through configured channels. Useful for verifying
    a channel is wired up before relying on real alerts. Optional body
    {"channel": "discord"|"email"} fires only that channel; default fires
    every channel that has a value configured."""
    if not is_pro_active():
        return jsonify({"error": "Alerts are a Pro feature.", "code": "pro_required"}), 402
    body = request.get_json(silent=True) or {}
    target = (body.get("channel") or "").strip().lower()
    cfg = _alerts_get_config()
    channels = cfg.get("channels", {}) or {}
    webhook = (channels.get("discord_webhook") or "").strip()
    email_to = (channels.get("email_to") or "").strip()

    title = "✓ Bitaxe Baller test alert"
    body_text = (
        "If you can read this, your alert channel is correctly wired up. "
        "Real alerts will fire when devices go offline or temps cross thresholds."
    )

    results = {}
    fired_any = False
    if (not target or target == "discord") and webhook:
        ok, msg = _alerts_post_discord(webhook, title, body_text)
        results["discord"] = {"ok": ok, "message": msg}
        fired_any = True
    if (not target or target == "email") and email_to:
        ok, msg = _alerts_post_email(email_to, title, body_text)
        results["email"] = {"ok": ok, "message": msg}
        fired_any = True

    if not fired_any:
        return jsonify({"error": "No alert channel configured"}), 400
    # 200 if every channel that fired succeeded; 502 if any failed
    all_ok = all(r["ok"] for r in results.values())
    return jsonify({"ok": all_ok, "results": results}), (200 if all_ok else 502)


@app.route("/api/devices/autotune/start", methods=["POST"])
def api_autotune_start():
    """Pro feature. Kick off an automated frequency sweep on one device.
    Captures the current freq as baseline (restored on abort) and bumps
    +25 MHz every 90s until errors climb or the ceiling/temp limits hit."""
    if not is_pro_active():
        return jsonify({"error": "Auto-tune is a Pro feature.", "code": "pro_required"}), 402
    body = request.get_json(force=True) or {}
    ip = body.get("ip")
    if not ip:
        return jsonify({"error": "IP required"}), 400
    blocked = _monitor_only_error(ip) or _tuning_unsupported_error(ip)
    if blocked:
        return blocked
    with state_lock:
        if ip not in state:
            return jsonify({"error": "Device not tracked"}), 404
        s = state[ip]
        if not s.get("online") or not s.get("latest"):
            return jsonify({"error": "Device offline — wait for it to come back, then start again"}), 400
        if s.get("autotune") and s["autotune"].get("status") == "running":
            return jsonify({"error": "Auto-tune already running on this device"}), 409

        latest = s["latest"]
        baseline = {
            "frequency": int(latest.get("frequency") or 0),
            "coreVoltage": int(latest.get("coreVoltage") or 0),
        }
        if baseline["frequency"] <= 0:
            return jsonify({"error": "Couldn't read current frequency from device"}), 400

        max_freq = int(body.get("max_freq") or BOUNDS["frequency"][1])
        lo, hi = BOUNDS["frequency"]
        if max_freq < baseline["frequency"] or max_freq > hi or max_freq < lo:
            return jsonify({"error": f"max_freq must be between {baseline['frequency']} and {hi}"}), 400

        s["autotune"] = {
            "status": "running",
            "step": 0,
            "started_at": time.time(),
            "baseline": baseline,
            "current_freq": baseline["frequency"],
            "max_freq": max_freq,
            "best_stable": None,
            "abort_reason": None,
            "next_step_at": time.time() + AUTOTUNE_OBSERVE_S,
            "events": [],
        }
        # Clean HW-error baseline before the first observation window.
        s["session_shares_start"] = latest.get("sharesAccepted", 0)
        s["session_hwerrors_start"] = latest.get("sharesRejected", 0)
        s["session_start"] = time.time()
        _autotune_log(s, f"START — baseline {baseline['frequency']} MHz, target ≤ {max_freq} MHz")

    return jsonify({"ok": True, "autotune": _autotune_summary(state[ip])})


@app.route("/api/devices/autotune/stop", methods=["POST"])
def api_autotune_stop():
    """Stop a running sweep. Restores the captured baseline frequency."""
    body = request.get_json(force=True) or {}
    ip = body.get("ip")
    if not ip:
        return jsonify({"error": "IP required"}), 400
    with state_lock:
        if ip not in state:
            return jsonify({"error": "Device not tracked"}), 404
        s = state[ip]
        a = s.get("autotune")
        if not a or a.get("status") != "running":
            return jsonify({"ok": True, "autotune": _autotune_summary(s)})
        _autotune_abort(ip, s, "stopped by user")
        return jsonify({"ok": True, "autotune": _autotune_summary(s)})


@app.route("/api/devices/bulk_tune", methods=["POST"])
def api_devices_bulk_tune():
    """Pro-tier feature. Apply the same tuning (preset OR manual freq/voltage/fan)
    to multiple devices in parallel. Returns per-device success/error so the UI
    can show a per-row outcome instead of a single all-or-nothing result.

    Body: {
      ips: ["192.168.1.223", ...],
      preset?: "balanced",  -- mutually exclusive with frequency/coreVoltage below
      frequency?: int, coreVoltage?: int,
      fanspeed?: int, autofanspeed?: 0|1,
    }
    """
    if not is_pro_active():
        return jsonify({
            "error": "Bulk tuning is a Pro feature.",
            "code": "pro_required",
        }), 402

    body = request.get_json(force=True) or {}
    ips = body.get("ips") or []
    if not isinstance(ips, list) or not ips:
        return jsonify({"error": "ips (non-empty list) required"}), 400
    if len(ips) > 64:
        return jsonify({"error": "Too many devices in one call (max 64)"}), 400

    # Build the settings payload once. preset and (frequency|coreVoltage) are
    # mutually exclusive — if both arrive, preset wins (matches the single-device
    # preset endpoint's behavior, where applying a preset overwrites manual freq/v).
    preset_name = body.get("preset")
    if preset_name:
        if preset_name not in PRESETS:
            return jsonify({"error": f"Unknown preset: {preset_name}"}), 400
        p = PRESETS[preset_name]
        settings = {"frequency": p["frequency"], "coreVoltage": p["coreVoltage"]}
        # Fan settings can still ride along with a preset (e.g. "Balanced + autofan on")
        for k in ("fanspeed", "autofanspeed"):
            if k in body and body[k] is not None:
                settings[k] = body[k]
        applied_label = p["label"]
    else:
        settings = {}
        for key in ("frequency", "coreVoltage", "fanspeed", "autofanspeed"):
            if key in body and body[key] is not None:
                settings[key] = body[key]
        applied_label = None

    if not settings:
        return jsonify({"error": "No settings supplied"}), 400

    # Validate bounds once — same logic as the single-device tune endpoint.
    for key, val in list(settings.items()):
        try:
            val = int(val)
        except (TypeError, ValueError):
            return jsonify({"error": f"{key} must be a number"}), 400
        if key in BOUNDS:
            lo, hi = BOUNDS[key]
            if val < lo or val > hi:
                return jsonify({"error": f"{key}={val} outside safe range {lo}-{hi}"}), 400
        settings[key] = val

    # Only act on IPs we actually know about. Silently dropping unknown IPs
    # would be confusing; surface them in the response.
    with state_lock:
        known_ips = set(state.keys())
    unknown = [ip for ip in ips if ip not in known_ips]
    targets = [ip for ip in ips if ip in known_ips]

    def apply_one(ip):
        # Skip devices we must never push AxeOS tuning at, with a per-row reason
        # so the UI shows why (rather than a confusing raw PATCH failure).
        if _is_monitor_only(ip):
            return {"ip": ip, "ok": False,
                    "error": "Monitor-only device (Braiins OS) — tuning stays in its own web UI."}
        chip_msg = _tuning_unsupported_msg(ip)
        if chip_msg:
            return {"ip": ip, "ok": False, "error": chip_msg}
        try:
            patch_device(ip, settings)
            # Reset per-device baseline so the next measurement window starts clean.
            with state_lock:
                if ip in state and state[ip]["latest"]:
                    s = state[ip]
                    s["session_shares_start"] = s["latest"].get("sharesAccepted", 0)
                    s["session_hwerrors_start"] = s["latest"].get("sharesRejected", 0)
                    s["session_start"] = time.time()
            label = f"Bulk preset: {applied_label}" if applied_label else "Bulk tuning applied: " + ", ".join(f"{k}={v}" for k, v in settings.items())
            log_event(ip, label)
            return {"ip": ip, "ok": True, "applied": settings}
        except Exception as e:
            return {"ip": ip, "ok": False, "error": str(e)[:120]}

    results = []
    if targets:
        with ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
            for r in ex.map(apply_one, targets):
                results.append(r)
    for ip in unknown:
        results.append({"ip": ip, "ok": False, "error": "Device not tracked"})

    success_count = sum(1 for r in results if r["ok"])
    return jsonify({
        "ok": True,
        "applied_settings": settings,
        "preset": applied_label,
        "results": results,
        "succeeded": success_count,
        "failed": len(results) - success_count,
    })


@app.route("/api/devices/restart", methods=["POST"])
def api_device_restart():
    body = request.get_json(force=True)
    ip = body.get("ip")
    if not ip:
        return jsonify({"error": "IP required"}), 400
    blocked = _monitor_only_error(ip)
    if blocked:
        return blocked
    try:
        restart_device(ip)
    except Exception as e:
        return jsonify({"error": str(e)[:120]}), 500
    log_event(ip, "Restart command sent")
    return jsonify({"ok": True})


POOL_FIELDS = {
    "stratumURL", "stratumPort", "stratumUser", "stratumPassword",
    "stratumTLS", "stratumSuggestedDifficulty",
    "fallbackStratumURL", "fallbackStratumPort", "fallbackStratumUser", "fallbackStratumPassword",
    "fallbackStratumTLS", "fallbackStratumSuggestedDifficulty",
}

# Pool profile fields — same as POOL_FIELDS minus the passwords. Pool passwords
# are write-only per the wider convention (worker passwords are sensitive and
# never echoed back from the device), so profiles deliberately don't store
# them. If a user wants a non-default password, they enter it on the pool form
# after applying the profile.
POOL_PROFILE_FIELDS = {f for f in POOL_FIELDS if "Password" not in f}


def _load_pool_profiles():
    with config_lock:
        cfg = load_config()
    return list(cfg.get("pool_profiles") or [])


def _save_pool_profiles(profiles):
    with config_lock:
        cfg = load_config()
        cfg["pool_profiles"] = profiles
        save_config(cfg)


def _new_profile_id() -> str:
    """6-char URL-safe random id. Profiles are user-facing config, so collisions
    inside the same install are vanishingly unlikely — and a collision would
    just mean the first save wins, which is recoverable."""
    return base64.urlsafe_b64encode(os.urandom(5)).decode("ascii").rstrip("=")[:6]


def _filter_profile_payload(body: dict) -> dict:
    """Normalize the pool fields out of a request body into a profile dict.
    Drops unknown keys, normalizes types, leaves missing fields unset rather
    than empty-stringed (so 'apply' won't send empty overrides to the device)."""
    out = {}
    for key in POOL_PROFILE_FIELDS:
        if key not in body:
            continue
        v = body[key]
        if v is None or v == "":
            continue
        if key.endswith("Port") or key.endswith("TLS") or key.endswith("SuggestedDifficulty"):
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        else:
            v = str(v).strip()
            if not v:
                continue
        out[key] = v
    return out


@app.route("/api/devices/pool", methods=["POST"])
def api_device_pool():
    """Update primary and/or fallback stratum config on a device. The Bitaxe
    firmware applies pool changes on the next stratum reconnect, so a restart
    is usually needed. Caller is expected to follow up with /api/devices/restart
    if `restart` is truthy in the body."""
    body = request.get_json(force=True)
    ip = body.get("ip")
    if not ip:
        return jsonify({"error": "IP required"}), 400
    blocked = _monitor_only_error(ip)
    if blocked:
        return blocked

    settings = {}
    for key in POOL_FIELDS:
        if key in body and body[key] not in (None, ""):
            v = body[key]
            if key.endswith("Port"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} must be an integer"}), 400
                if v <= 0 or v > 65535:
                    return jsonify({"error": f"{key} out of range"}), 400
            elif key.endswith("TLS") or key.endswith("SuggestedDifficulty"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} must be an integer"}), 400
            else:
                v = str(v).strip()
            settings[key] = v

    if not settings:
        return jsonify({"error": "No pool settings to apply"}), 400

    try:
        patch_device(ip, settings)
    except Exception as e:
        return jsonify({"error": f"Failed to apply: {str(e)[:120]}"}), 500

    safe_keys = sorted(k for k in settings if "Password" not in k)
    log_event(ip, f"Pool config updated: {', '.join(safe_keys)}")

    restarted = False
    if body.get("restart"):
        try:
            restart_device(ip)
            restarted = True
            log_event(ip, "Restart sent (pool config change)")
        except Exception as e:
            return jsonify({"ok": True, "applied": safe_keys, "restartError": str(e)[:120]}), 200

    return jsonify({"ok": True, "applied": safe_keys, "restarted": restarted})


# ----- Pool profiles (named pool configs you can save + reapply) -----
# Save once, switch with one click. Useful when bouncing between BTC and BCH
# pools, or between two different BTC pools (Public Pool vs Ocean vs Solo
# CKPool). Passwords are NOT stored — worker passwords are write-only per
# the wider convention; if a profile needs a non-default password, the user
# types it once on the pool form after applying. The Pro-gated scheduler
# (time-of-day pool switching) is a separate follow-up and out of scope for
# this MVP — manual switch only.

@app.route("/api/pool-profiles", methods=["GET"])
def api_pool_profiles_list():
    return jsonify({"profiles": _load_pool_profiles()})


@app.route("/api/pool-profiles", methods=["POST"])
def api_pool_profiles_create():
    body = request.get_json(force=True) or {}
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    if len(name) > 60:
        return jsonify({"error": "name too long (max 60 chars)"}), 400

    fields = _filter_profile_payload(body)
    if not fields.get("stratumURL") or not fields.get("stratumPort"):
        return jsonify({"error": "stratumURL and stratumPort required"}), 400

    profiles = _load_pool_profiles()
    if any(p.get("name", "").lower() == name.lower() for p in profiles):
        return jsonify({"error": "a profile with that name already exists"}), 409

    profile = {
        "id": _new_profile_id(),
        "name": name,
        "created_at": int(time.time()),
        **fields,
    }
    profiles.append(profile)
    _save_pool_profiles(profiles)
    return jsonify({"ok": True, "profile": profile})


@app.route("/api/pool-profiles/<pid>/delete", methods=["POST"])
def api_pool_profiles_delete(pid):
    profiles = _load_pool_profiles()
    remaining = [p for p in profiles if p.get("id") != pid]
    if len(remaining) == len(profiles):
        return jsonify({"error": "profile not found"}), 404
    _save_pool_profiles(remaining)
    return jsonify({"ok": True})


@app.route("/api/pool-profiles/<pid>/apply", methods=["POST"])
def api_pool_profiles_apply(pid):
    """Apply a saved profile to one device. Body: {ip, restart?}. Reuses the
    same patch_device path as /api/devices/pool — same bounds checks, same
    failure semantics, same log event."""
    body = request.get_json(force=True) or {}
    ip = body.get("ip")
    if not ip:
        return jsonify({"error": "IP required"}), 400

    profile = next((p for p in _load_pool_profiles() if p.get("id") == pid), None)
    if not profile:
        return jsonify({"error": "profile not found"}), 404

    settings = {k: v for k, v in profile.items() if k in POOL_PROFILE_FIELDS}
    if not settings:
        return jsonify({"error": "profile has no applicable fields"}), 400

    try:
        patch_device(ip, settings)
    except Exception as e:
        return jsonify({"error": f"Failed to apply: {str(e)[:120]}"}), 500

    log_event(ip, f"Pool profile applied: '{profile['name']}' (primary {profile.get('stratumURL', '?')}:{profile.get('stratumPort', '?')})")

    restarted = False
    if body.get("restart"):
        try:
            restart_device(ip)
            restarted = True
            log_event(ip, "Restart sent (pool profile change)")
        except Exception as e:
            return jsonify({"ok": True, "applied_profile_id": pid, "restartError": str(e)[:120]}), 200

    return jsonify({"ok": True, "applied_profile_id": pid, "restarted": restarted})


# ----- Pool schedules (Pro): time-of-day + weekday pool switching -----
# A schedule applies a saved pool profile to one or more devices at a chosen
# local time on chosen weekdays — e.g. "23:00 Sat & Sun → solo-lottery pool".
# Built on the existing pool-profiles feature (a schedule references a profile
# id, it doesn't store pool fields itself). Evaluated inside poll_loop; no cron
# dependency. Weekdays use Python's convention: Monday=0 … Sunday=6.
#
# Missed-fire semantics: a schedule fires when the app is running during its
# target minute. If the machine is asleep/off at that minute, that day's switch
# is skipped (no catch-up) — the once-per-day guard is a date string, not a
# backlog. This is the honest behavior for a desktop app and is documented in
# the UI tooltip.

SCHEDULE_DIRTY_LOCK = threading.Lock()


def _load_pool_schedules():
    with config_lock:
        cfg = load_config()
    return list(cfg.get("pool_schedules") or [])


def _save_pool_schedules(schedules):
    with config_lock:
        cfg = load_config()
        cfg["pool_schedules"] = schedules
        save_config(cfg)


def _new_schedule_id() -> str:
    return base64.urlsafe_b64encode(os.urandom(5)).decode("ascii").rstrip("=")[:6]


def _validate_schedule_payload(body: dict):
    """Normalize + validate an incoming schedule. Returns (fields, error). On
    success error is None; on failure fields is None and error is a message."""
    profile_id = str(body.get("profile_id") or "").strip()
    if not profile_id:
        return None, "profile_id required"
    if not any(p.get("id") == profile_id for p in _load_pool_profiles()):
        return None, "unknown profile_id"

    t = str(body.get("time") or "").strip()
    try:
        hh, mm = t.split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        t = f"{hh:02d}:{mm:02d}"
    except (ValueError, AttributeError):
        return None, "time must be HH:MM (24-hour)"

    raw_days = body.get("days")
    if not isinstance(raw_days, list) or not raw_days:
        return None, "days must be a non-empty list of 0-6 (Mon=0)"
    days = sorted({int(d) for d in raw_days if isinstance(d, (int, float)) and 0 <= int(d) <= 6})
    if not days:
        return None, "days must contain integers 0-6 (Mon=0)"

    known_ips = {d["ip"] for d in (load_config().get("devices") or [])}
    raw_ips = body.get("ips")
    if not isinstance(raw_ips, list) or not raw_ips:
        return None, "ips must be a non-empty list of tracked device IPs"
    ips = [str(ip).strip() for ip in raw_ips if str(ip).strip() in known_ips]
    if not ips:
        return None, "none of the given ips are tracked devices"

    return {
        "profile_id": profile_id,
        "time": t,
        "days": days,
        "ips": ips,
        "restart": bool(body.get("restart", True)),
        "enabled": bool(body.get("enabled", True)),
    }, None


@app.route("/api/pool-schedules", methods=["GET"])
def api_pool_schedules_list():
    return jsonify({"schedules": _load_pool_schedules(), "pro_active": is_pro_active()})


@app.route("/api/pool-schedules", methods=["POST"])
def api_pool_schedules_create():
    if not is_pro_active():
        return jsonify({"error": "Pool scheduling is a Pro feature.", "code": "pro_required"}), 402
    body = request.get_json(force=True) or {}
    fields, err = _validate_schedule_payload(body)
    if err:
        return jsonify({"error": err}), 400
    label = str(body.get("label") or "").strip()[:60]
    schedules = _load_pool_schedules()
    schedule = {
        "id": _new_schedule_id(),
        "label": label,
        "created_at": int(time.time()),
        "last_run": "",
        **fields,
    }
    schedules.append(schedule)
    _save_pool_schedules(schedules)
    return jsonify({"ok": True, "schedule": schedule})


@app.route("/api/pool-schedules/<sid>/update", methods=["POST"])
def api_pool_schedules_update(sid):
    """Edit a schedule, or just flip `enabled`. Enabling requires Pro (it
    activates a Pro feature); disabling/deleting is always allowed so a lapsed
    user can turn things off."""
    body = request.get_json(force=True) or {}
    schedules = _load_pool_schedules()
    sch = next((s for s in schedules if s.get("id") == sid), None)
    if not sch:
        return jsonify({"error": "schedule not found"}), 404

    # Enable-only fast path (the toggle switch in the UI).
    if set(body.keys()) <= {"enabled"} and "enabled" in body:
        want = bool(body["enabled"])
        if want and not is_pro_active():
            return jsonify({"error": "Pool scheduling is a Pro feature.", "code": "pro_required"}), 402
        sch["enabled"] = want
        if not want:
            sch["last_run"] = ""  # so re-enabling can fire again today
        _save_pool_schedules(schedules)
        return jsonify({"ok": True, "schedule": sch})

    # Full edit requires Pro.
    if not is_pro_active():
        return jsonify({"error": "Pool scheduling is a Pro feature.", "code": "pro_required"}), 402
    fields, err = _validate_schedule_payload(body)
    if err:
        return jsonify({"error": err}), 400
    sch.update(fields)
    sch["label"] = str(body.get("label") or sch.get("label") or "").strip()[:60]
    sch["last_run"] = ""  # settings changed — allow it to fire again today
    _save_pool_schedules(schedules)
    return jsonify({"ok": True, "schedule": sch})


@app.route("/api/pool-schedules/<sid>/delete", methods=["POST"])
def api_pool_schedules_delete(sid):
    schedules = _load_pool_schedules()
    remaining = [s for s in schedules if s.get("id") != sid]
    if len(remaining) == len(schedules):
        return jsonify({"error": "schedule not found"}), 404
    _save_pool_schedules(remaining)
    return jsonify({"ok": True})


def _run_pool_schedules():
    """Fire any due pool schedules. Called once per poll cycle. Cheap in the
    common case (no schedules, or no minute match). Fails safe: if Pro isn't
    active the schedules simply don't run (config is retained)."""
    if not is_pro_active():
        return
    if not _load_pool_schedules():  # fast path — skip the lock when nothing is scheduled
        return
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    weekday = now.weekday()  # Mon=0 … Sun=6

    # Serialize evaluation so a slow apply (network I/O) can't overlap a second
    # invocation and double-fire the same schedule.
    with SCHEDULE_DIRTY_LOCK:
        schedules = _load_pool_schedules()  # re-read inside the lock
        profiles = {p["id"]: p for p in _load_pool_profiles()}
        dirty = False
        for sch in schedules:
            if not sch.get("enabled"):
                continue
            if weekday not in (sch.get("days") or []):
                continue
            if sch.get("time") != hhmm:
                continue
            if sch.get("last_run") == today:
                continue

            prof = profiles.get(sch.get("profile_id"))
            if not prof:
                # Profile was deleted out from under the schedule. Burn today's
                # slot so we don't retry every 5s, and move on.
                sch["last_run"] = today
                dirty = True
                continue

            settings = {k: v for k, v in prof.items() if k in POOL_PROFILE_FIELDS}
            for ip in (sch.get("ips") or []):
                if _is_monitor_only(ip):
                    log_event(ip, f"Scheduled pool switch skipped — monitor-only device")
                    continue
                try:
                    patch_device(ip, settings)
                    log_event(ip, f"Scheduled pool switch → '{prof.get('name', '?')}' at {sch.get('time')}")
                    if sch.get("restart"):
                        restart_device(ip)
                        log_event(ip, "Restart sent (scheduled pool switch)")
                except Exception as e:
                    log_event(ip, f"Scheduled pool switch failed: {str(e)[:80]}")
            sch["last_run"] = today
            dirty = True

        if dirty:
            _save_pool_schedules(schedules)


@app.route("/api/devices/reset_session", methods=["POST"])
def api_reset_session():
    """Reset the rolling-average / HW-error baseline so a tuning experiment starts clean."""
    body = request.get_json(force=True)
    ip = body.get("ip")
    if not ip:
        return jsonify({"error": "IP required"}), 400
    with state_lock:
        if ip in state:
            s = state[ip]
            s["history"].clear()
            if s["latest"]:
                s["session_shares_start"] = s["latest"].get("sharesAccepted", 0)
                s["session_hwerrors_start"] = s["latest"].get("sharesRejected", 0)
            s["session_start"] = time.time()
    log_event(ip, "Benchmark session reset")
    return jsonify({"ok": True})


def detect_lan_ip():
    """Best-effort: find the LAN IP this machine uses to reach the local network.
    No packet is actually sent — the UDP socket only triggers a routing decision."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None


def _scan_subnet_override():
    """Operator override for which /24 the LAN scanner probes, via the
    BBR_SCAN_SUBNET env var. Needed when Baller runs in a BRIDGED container
    (e.g. a 5tratumOS / Umbrel app on the default Docker network): the container's
    own IP is on the Docker bridge, not the LAN, so auto-detection would scan the
    wrong subnet. Set it to the real LAN /24 — '192.168.1.0/24' or just
    '192.168.1' both work. Returns the 3-octet base prefix (e.g. '192.168.1')
    or None when unset / malformed / non-private."""
    raw = (os.environ.get("BBR_SCAN_SUBNET") or "").strip()
    if not raw:
        return None
    octets = raw.split("/")[0].split(".")   # tolerate '.../24', '.0', bare prefix
    if len(octets) < 3:
        return None
    try:
        vals = [int(o) for o in octets[:3]]
    except ValueError:
        return None
    if any(v < 0 or v > 255 for v in vals):
        return None
    base = ".".join(str(v) for v in vals)
    return base if _is_private_v4(base + ".1") else None


HOST = os.environ.get("HOST", "0.0.0.0")
MDNS_NAME = os.environ.get("MDNS_NAME", "bitaxe-baller")
MDNS_ENABLED = os.environ.get("MDNS_ENABLED", "1") not in ("0", "false", "no", "")


def _can_bind(port):
    """Best-effort check that we can bind HOST:port. Closes immediately."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, port))
        s.close()
        return True
    except OSError:
        return False


def _pick_port():
    """If PORT env var is set, honor it. Otherwise prefer 80 for clean URLs
    (no :port in the address bar) and fall back to 5050 when port 80 isn't
    available — typically because the app isn't running as root."""
    if "PORT" in os.environ:
        return int(os.environ["PORT"])
    if _can_bind(80):
        return 80
    return 5050


PORT = _pick_port()


def _url(host, port):
    """Format URL, omitting :80 since browsers default to it for http://."""
    return f"http://{host}" if port == 80 else f"http://{host}:{port}"


def start_mdns(lan_ip, port, name=MDNS_NAME):
    """Publish an mDNS / Bonjour service so the dashboard is reachable at
    http://<name>.local:<port> from any device on the LAN. Returns
    (zeroconf, info) or (None, None) on any failure."""
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
    except ImportError:
        print(f"[mdns] zeroconf not installed; skipping. pip install zeroconf to enable")
        return None, None
    if not lan_ip:
        return None, None
    try:
        zc = Zeroconf(ip_version=IPVersion.V4Only)
        info = ServiceInfo(
            "_http._tcp.local.",
            f"{name}._http._tcp.local.",
            addresses=[socket.inet_aton(lan_ip)],
            port=port,
            properties={"path": "/"},
            server=f"{name}.local.",
        )
        # allow_name_change=True lets zeroconf auto-suffix the name on a
        # NonUniqueNameException — common when another Bitaxe Baller is already
        # on the LAN (a coworker's machine, an old TTL, the dev instance still
        # advertising). The suffixed name (bitaxe-baller-2.local, etc.) is
        # written back into `info.name`.
        zc.register_service(info, allow_name_change=True)
        return zc, info
    except Exception as e:
        # Surface the full traceback — earlier we were swallowing the actual
        # cause (often a missing zeroconf submodule when frozen).
        import traceback
        print(f"[mdns] failed to register: {type(e).__name__}: {e!r}")
        traceback.print_exc()
        return None, None


def _open_browser_when_ready(url: str, delay_s: float = 1.5) -> None:
    """Source-mode helper: open the user's default browser shortly after Flask
    starts listening. Only used when BITAXE_BALLER_OPEN_BROWSER=1 is set —
    devs running `python app.py` usually already have a tab open."""
    def _go() -> None:
        try:
            time.sleep(delay_s)
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


def _should_auto_open_browser() -> bool:
    override = os.environ.get("BITAXE_BALLER_OPEN_BROWSER")
    if override is not None:
        return override not in ("0", "false", "no", "")
    # Default to no auto-browser-open. Frozen mode uses pywebview instead;
    # source mode leaves the dev to open their own tab.
    return False


def _wait_for_port(host: str, port: int, timeout_s: float = 8.0) -> bool:
    """Poll until something accepts a TCP connection at host:port. Used to
    block the webview window from opening before Flask has bound the socket."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _run_webview(zc, info) -> None:
    """Packaged-app entry: Flask in a daemon thread, native webview window
    on the main thread. When the window closes, the app quits cleanly."""
    import webview  # imported lazily so source-mode doesn't pay the import cost

    if sys.platform == "win32":
        # Bind this process to an AppUserModelID so the Windows taskbar uses
        # the icon embedded in our .exe (PyInstaller PE resource) instead of
        # the generic Python/blank fallback. Must run before any window opens.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "com.465-media.bitaxe-baller"
            )
        except Exception:
            pass  # non-fatal — falls back to whatever Windows picks

    def _serve() -> None:
        # use_reloader=False is required when not on the main thread
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False)

    threading.Thread(target=_serve, daemon=True).start()

    if not _wait_for_port("127.0.0.1", PORT):
        print("[webview] Flask did not start listening within 8s — aborting", file=sys.stderr)
        return

    _maybe_start_relay_client(PORT)

    try:
        webview.create_window(
            title="Bitaxe Baller",
            url=f"http://127.0.0.1:{PORT}",
            width=1440,
            height=900,
            min_size=(960, 600),
            background_color="#0a0d0c",
            confirm_close=False,
        )
        # webview.start() blocks the main thread until the window is closed.
        # macOS GUI work *must* happen on the main thread, hence this layout.
        #
        # private_mode=False + storage_path=<persistent dir> tells the embedded
        # WKWebView (macOS) / WebView2 (Windows) to persist localStorage,
        # cookies, IndexedDB, etc. across app restarts. pywebview defaults
        # private_mode=True, which gave us incognito-style behavior — the
        # theme toggle, hashrate unit (GH/s ↔ TH/s), temperature unit (°C/°F),
        # and every other localStorage-backed UI preference were getting wiped
        # every launch. Storage lives under the same data dir we use for
        # config.json / history.db so it survives Mac DMG reinstalls and
        # Umbrel app updates (bind-mounted to /data on Umbrel).
        webview_storage = os.path.join(_DATA_DIR, "webview")
        os.makedirs(webview_storage, exist_ok=True)
        webview.start(private_mode=False, storage_path=webview_storage)
    finally:
        # Flask thread is daemonized so it dies with the process; just clean
        # up the mDNS service so we don't leave a stale TTL on the LAN.
        if zc is not None:
            try:
                zc.unregister_service(info)
                zc.close()
            except Exception:
                pass


def main():
    cfg = load_config()
    with state_lock:
        for d in cfg.get("devices", []):
            state[d["ip"]] = init_device_state(d["ip"], d["label"], d.get("chain"), d.get("type"))

    global poll_thread
    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    # Keep chain difficulty/price stats warm off the poll path (never under state_lock).
    threading.Thread(target=_chain_stats_refresh_loop, daemon=True).start()

    lan_ip = detect_lan_ip()

    zc = info = None
    if MDNS_ENABLED and HOST == "0.0.0.0":
        zc, info = start_mdns(lan_ip, PORT)

    print()
    print("=" * 64)
    print("  Bitaxe Baller  -  open the dashboard at:")
    print(f"    {_url('localhost', PORT)}".ljust(40) + "(this machine)")
    if lan_ip:
        print(f"    {_url(lan_ip, PORT)}".ljust(40) + "(from any device on your LAN)")
    else:
        print(f"    {_url('<this-machine-ip>', PORT)}".ljust(40) + "(from other devices)")
    if zc:
        # info.server reflects the actual registered name (may be auto-suffixed
        # by zeroconf if another instance had the original name)
        actual_host = (info.server or f"{MDNS_NAME}.local.").rstrip(".")
        print(f"    {_url(actual_host, PORT)}".ljust(40) + "(via mDNS / Bonjour)")
    print("=" * 64)
    if HOST == "0.0.0.0":
        print("  Bound to 0.0.0.0 - reachable from other devices on the network.")
        print("  macOS may prompt about incoming connections on first run; allow it.")
        if zc:
            print(f"  mDNS published as '{MDNS_NAME}.local' (Bonjour/Avahi).")
        if PORT != 80:
            print(f"  Tip: run with sudo to bind port 80 and drop ':{PORT}' from the URL")
            print(f"       sudo $(which python) app.py")
        print("=" * 64)
    print()

    if _is_frozen():
        # Packaged app: native window. Flask runs in a daemon thread.
        _run_webview(zc, info)
    else:
        # Source mode: Flask blocks the main thread, dev opens their own tab.
        if _should_auto_open_browser():
            _open_browser_when_ready(_url("localhost", PORT))
        # Outbound relay connection (Pro). Safe to start before Flask is
        # listening — only the loopback HTTP dispatch needs Flask up, and
        # that only fires when a remote client actually sends a request.
        _maybe_start_relay_client(PORT)
        try:
            app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
        finally:
            if zc:
                try:
                    zc.unregister_service(info)
                    zc.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
