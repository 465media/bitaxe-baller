"""
Bitaxe Baller
Run: python app.py    (from source)
or:  open Bitaxe-Baller.app    (packaged release)

Then your default browser opens to the dashboard. Add devices and tune.
"""

import json
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
APP_VERSION = "1.9.1"


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
    them next to app.py so the existing dev workflow is unchanged."""
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

# Sane bounds — refuse to send anything outside these
BOUNDS = {
    "frequency": (400, 700),     # MHz
    "coreVoltage": (1000, 1300), # mV
    "fanspeed": (0, 100),        # %
}

state_lock = threading.Lock()
state = {}                # ip -> device state
config_lock = threading.Lock()
poll_thread = None
poll_stop_flag = threading.Event()


def default_config():
    return {"devices": [], "poll_interval": DEFAULT_POLL}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(default_config())
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def init_device_state(ip, label):
    return {
        "ip": ip,
        "label": label,
        "history": deque(maxlen=HISTORY_POINTS),
        "latest": None,
        "online": False,
        "consecutive_errors": 0,
        "last_error": "",
        "session_start": time.time(),
        "session_shares_start": None,
        "session_hwerrors_start": None,
        "events": deque(maxlen=50),  # tuning changes, restarts, etc
        "autotune": None,            # populated when a sweep is in flight
    }


def fetch_device(ip, timeout=3):
    r = requests.get(f"http://{ip}/api/system/info", timeout=timeout)
    r.raise_for_status()
    return r.json()


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


def poll_one(ip, label):
    ts = time.time()
    try:
        data = fetch_device(ip)
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

        if was_offline:
            log_event(ip, "Device back online")
        append_csv(label, point)
        _history_record(ip, point)

        # Advance auto-tune state machine if a sweep is running. Done under
        # state_lock to keep mutations consistent with the poll write above.
        with state_lock:
            if ip in state:
                _autotune_tick(ip, state[ip])
                summary_for_alerts = device_summary(state[ip])
        # Alerts check uses the public summary (so the same shape the UI sees).
        # Done outside the state_lock since it does HTTP to Discord webhooks
        # and we don't want to hold the lock across a network round-trip.
        _alerts_check(ip, label, summary_for_alerts)
    except Exception as e:
        with state_lock:
            if ip not in state:
                return
            s = state[ip]
            was_online = s["online"]
            s["consecutive_errors"] += 1
            s["last_error"] = str(e)[:100]
            if s["consecutive_errors"] >= 3:
                s["online"] = False
            offline_summary = device_summary(s)
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
                futures = [ex.submit(poll_one, d["ip"], d["label"]) for d in devices]
                for f in futures:
                    try:
                        f.result(timeout=10)
                    except Exception:
                        pass
            # No-op unless 24h have elapsed since the last sweep.
            _history_sweep_retention()
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

    # Severity priority for trimming: crit > warn > good > info
    order = {"crit": 0, "warn": 1, "good": 2, "info": 3}
    recs.sort(key=lambda r: order.get(r["severity"], 9))
    return recs[:3]


def device_summary(s):
    if not s["latest"]:
        return {
            "ip": s["ip"],
            "label": s["label"],
            "online": s["online"],
            "lastError": s["last_error"],
            "history": [],
            "events": list(s["events"]),
            "recommendations": [],
            "severity": "crit" if not s["online"] else None,
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
    if firmware_expected:
        expected_ghs = firmware_expected
    elif small_cores and freq:
        expected_ghs = (small_cores * freq) / 1000
    else:
        expected_ghs = freq * 2.04 if freq > 0 else 0

    session_secs = max(1, time.time() - s["session_start"])
    shares_per_min = (shares_delta / (session_secs / 60)) if session_secs >= 60 else 0

    recs = compute_recommendations(s, hist, avgs, hw_rate_pct, shares_delta, j_per_th, ghs, expected_ghs)

    return {
        "ip": s["ip"],
        "label": s["label"],
        "online": s["online"],
        "lastError": s["last_error"],
        "model": latest.get("ASICModel", "unknown"),
        "version": latest.get("version", ""),
        "hostname": latest.get("hostname", ""),
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
            "bestSessionDiff": latest.get("bestSessionDiff", "0"),
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
    }


# ---------- Alerts (Pro) ----------
#
# v1 scope: Discord webhook channel + three rule types (offline, VR temp,
# ASIC temp). 30-min cooldown per (device, trigger) pair so a hot Gamma
# doesn't spam the channel every 5 seconds.
#
# Future: SMTP email channel, HW-error-rate-sustained trigger, custom rules.
ALERTS_DEFAULT_CONFIG = {
    "enabled": True,
    "channels": {
        "discord_webhook": "",  # https://discord.com/api/webhooks/<id>/<token>
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


def _alerts_should_fire(ip: str, trigger: str, cooldown_s: int) -> bool:
    """Return True iff this (ip, trigger) hasn't fired within the cooldown window."""
    with alerts_lock:
        last = _alerts_last_fired.get(ip, {}).get(trigger, 0)
        if time.time() - last < cooldown_s:
            return False
        _alerts_last_fired.setdefault(ip, {})[trigger] = time.time()
        return True


def _alerts_dispatch(label: str, ip: str, trigger: str, title: str, body: str) -> None:
    """Fan out to all configured channels. Logs to the device event log so
    the user has an in-app record of every alert fired."""
    cfg = _alerts_get_config()
    if not cfg.get("enabled"):
        return
    webhook = cfg.get("channels", {}).get("discord_webhook", "")
    ok, msg = _alerts_post_discord(webhook, title, body) if webhook else (False, "no channel configured")
    log_event(ip, f"[alert] {trigger}: {msg}")


def _alerts_check(ip: str, label: str, summary: dict) -> None:
    """Evaluate every rule against the current device summary. Called from the
    poll loop on every successful poll cycle. No-op if Pro is inactive."""
    if not is_pro_active():
        return
    cfg = _alerts_get_config()
    if not cfg.get("enabled"):
        return
    if not cfg.get("channels", {}).get("discord_webhook"):
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
#   - VR temp ≥ 65°C
#   - ASIC temp ≥ 65°C
#   - HW error rate ≥ 5% (clearly destabilized — drop everything)
AUTOTUNE_OBSERVE_S = 90      # seconds at each freq before evaluating
AUTOTUNE_STEP_MHZ = 25       # frequency increment per step
AUTOTUNE_MAX_STEPS = 8       # worst case: ~12 minutes
AUTOTUNE_HW_GOOD_PCT = 0.5   # below this → keep pushing
AUTOTUNE_HW_CEILING_PCT = 2.0  # at or above → declare ceiling (back off)
AUTOTUNE_HW_ABORT_PCT = 5.0  # at or above → ABORT (chip clearly destabilized)
AUTOTUNE_TEMP_ABORT_C = 65.0


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
    if vr >= AUTOTUNE_TEMP_ABORT_C:
        _autotune_abort(ip, s, f"VR temp {vr:.1f}°C ≥ {AUTOTUNE_TEMP_ABORT_C}°C")
        return
    if asic >= AUTOTUNE_TEMP_ABORT_C:
        _autotune_abort(ip, s, f"ASIC temp {asic:.1f}°C ≥ {AUTOTUNE_TEMP_ABORT_C}°C")
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

@app.route("/")
def index():
    return render_template("dashboard.html", presets=PRESETS, bounds=BOUNDS)


@app.route("/device/<ip>")
def device_detail(ip):
    """Per-device detail page — full metrics, tuning, pool config, event log."""
    with state_lock:
        if ip not in state:
            return ("Device not found. <a href='/'>Back to overview</a>", 404)
    return render_template("device.html", ip=ip, presets=PRESETS, bounds=BOUNDS)


@app.route("/api/devices")
def api_devices():
    with state_lock:
        return jsonify([device_summary(s) for s in state.values()])


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
    """Called once on startup. Starts the connector if (a) Pro is active and
    (b) the user has enabled remote access. Silent no-op otherwise."""
    if not is_pro_active():
        return
    rc = _remote_access_cfg()
    if not rc["enabled"]:
        return
    lic = _get_license()
    key = (lic.get("key") or "").strip() if lic else ""
    if not key:
        return
    try:
        relay_client.start(
            key,
            relay_url=rc["relay_url"],
            app_port=app_port,
            app_version=APP_VERSION,
        )
    except Exception as e:
        print(f"[relay] could not start connector: {type(e).__name__}: {e}", file=sys.stderr)


@app.route("/api/remote/status", methods=["GET"])
def api_remote_status():
    cfg = _remote_access_cfg()
    return jsonify({
        "pro_required": True,
        "pro_active": is_pro_active(),
        "configured": cfg,
        "runtime": relay_client.get_status(),
    })


@app.route("/api/remote/enable", methods=["POST"])
def api_remote_enable():
    if not is_pro_active():
        return jsonify({"error": "Remote access is a Pro feature."}), 402

    lic = _get_license()
    key = (lic.get("key") or "").strip() if lic else ""
    if not key:
        # is_pro_active() returned true so there should be a key; dev override
        # is the one path where there isn't, and we can't relay-route requests
        # without a real license key — the relay rejects empty bearers.
        return jsonify({
            "error": "Remote access needs a real license key. Activate Pro first.",
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
    )
    return jsonify({"ok": True, "status": relay_client.get_status()})


@app.route("/api/remote/disable", methods=["POST"])
def api_remote_disable():
    _save_remote_access_cfg(enabled=False, relay_url=_remote_access_cfg()["relay_url"])
    relay_client.stop()
    return jsonify({"ok": True, "status": relay_client.get_status()})


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

    Default rule: minor/major bumps (1.8.x → 1.9.0, 1.x → 2.0) banner; patch-only
    bumps (1.8.1 → 1.8.2) stay silent. Patch releases are usually internal/cosmetic
    and banner-blasting every one trains users to dismiss reflexively.

    Users always see the new version on next launch (no notification), they can
    also check manually via the dashboard. The banner is the "hey, look at this"
    nudge, reserved for changes that actually warrant the interruption.
    """
    c = _parse_semver(cur)
    l = _parse_semver(latest)
    return c[:2] != l[:2]


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
    if not lan_ip or not _is_private_v4(lan_ip):
        return jsonify({"error": "couldn't detect a private LAN IP to scan"}), 400

    parts = lan_ip.split(".")
    base = ".".join(parts[:3])

    with config_lock:
        existing = {d["ip"] for d in load_config().get("devices", [])}

    candidates = [
        f"{base}.{i}" for i in range(1, 255)
        if f"{base}.{i}" != lan_ip and f"{base}.{i}" not in existing
    ]

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
        except Exception:
            return None

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

    with config_lock:
        cfg = load_config()
        if any(d["ip"] == ip for d in cfg["devices"]):
            return jsonify({"error": f"{ip} is already added"}), 400
        cfg["devices"].append({"ip": ip, "label": label})
        save_config(cfg)

    with state_lock:
        state[ip] = init_device_state(ip, label)

    log_event(ip, f"Device added (model: {info.get('ASICModel', '?')}, fw: {info.get('version', '?')})")
    return jsonify({"ok": True, "ip": ip, "label": label, "model": info.get("ASICModel")})


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


@app.route("/api/devices/tune", methods=["POST"])
def api_device_tune():
    """Apply settings (frequency, coreVoltage, fanspeed, autofanspeed) to a device."""
    body = request.get_json(force=True)
    ip = body.get("ip")
    if not ip:
        return jsonify({"error": "IP required"}), 400

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
    sensitive enough that we mask them in the response by default."""
    cfg = _alerts_get_config()
    # Mask the webhook so it never round-trips back through XHR or screen
    # recordings. The UI shows the masked value as a placeholder; if the user
    # wants to change it, they paste a new one.
    webhook = cfg.get("channels", {}).get("discord_webhook", "")
    if webhook:
        cfg["channels"]["discord_webhook_masked"] = webhook[:36] + "…" + webhook[-6:] if len(webhook) > 50 else "***"
        cfg["channels"]["discord_webhook"] = ""
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
    if isinstance(body.get("channels"), dict):
        webhook = body["channels"].get("discord_webhook")
        if webhook is not None:
            webhook = str(webhook).strip()
            if webhook and not webhook.startswith("https://discord.com/api/webhooks/"):
                return jsonify({"error": "Discord webhook URL must start with https://discord.com/api/webhooks/"}), 400
            cur["channels"]["discord_webhook"] = webhook
    _alerts_save_config(cur)
    return jsonify({"ok": True})


@app.route("/api/alerts/test", methods=["POST"])
def api_alerts_test():
    """Send a test message through configured channels. Useful for verifying
    the Discord webhook is wired up before relying on real alerts."""
    if not is_pro_active():
        return jsonify({"error": "Alerts are a Pro feature.", "code": "pro_required"}), 402
    cfg = _alerts_get_config()
    webhook = cfg.get("channels", {}).get("discord_webhook", "")
    if not webhook:
        return jsonify({"error": "No Discord webhook configured"}), 400
    ok, msg = _alerts_post_discord(
        webhook,
        "✓ Bitaxe Baller test alert",
        "If you can read this, your Discord webhook is correctly wired up. Real alerts will fire when devices go offline or temps cross thresholds.",
    )
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 502)


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
        webview.start()
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
            state[d["ip"]] = init_device_state(d["ip"], d["label"])

    global poll_thread
    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

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
