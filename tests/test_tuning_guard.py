"""
Regression tests for the chip-level tuning guard (_tuning_supported).

Baller's PRESETS and BOUNDS (400-900 MHz / 1000-1300 mV) are validated for the
Bitaxe / NerdQAxe family (BM1366/1368/1370). A different chip like the 3nm
BM1373 in a Nexus S1 has a different electrical envelope, so tuning is
MONITOR-FIRST for it: the freq/voltage endpoints reject it and the UI hides the
tune panel. Pool/restart/identify stay available (chip-agnostic).

These pin the predicate so a future chip addition can't silently open tuning on
unvalidated silicon, and an empty/flaky ASICModel read can't lock a real Bitaxe
out of its own tune panel.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app

# (label, ASICModel value on latest, expected _tuning_supported)
FIXTURES = [
    ("Gamma BM1370",          "BM1370", True),
    ("Ultra BM1366",          "BM1366", True),
    ("Supra BM1368",          "BM1368", True),
    ("NerdQAxe++ (BM1370)",   "BM1370", True),
    ("Nexus S1 BM1373",       "BM1373", False),   # the case this guards
    ("hypothetical BM1397",   "BM1397", False),   # any future/unknown chip
    ("Braiins BMM-101",       "BMM-101", False),  # (also device_type-gated)
    ("empty model (flaky read)", "", True),        # must NOT lock a real Bitaxe
]


def _set_device(ip, model):
    app.state[ip] = {"ip": ip, "latest": {"ASICModel": model}}


def main() -> int:
    failures = []
    for i, (label, model, expected) in enumerate(FIXTURES):
        ip = f"10.0.0.{i}"
        _set_device(ip, model)

        supported = app._tuning_supported(ip)
        if supported != expected:
            failures.append(f"  FAIL  {label!r}: _tuning_supported → {supported}, expected {expected}")

        # A blocked chip must yield a non-empty message; a supported one, ''.
        msg = app._tuning_unsupported_msg(ip)
        if expected and msg:
            failures.append(f"  FAIL  {label!r}: supported chip returned a block message: {msg!r}")
        if not expected and not msg:
            failures.append(f"  FAIL  {label!r}: blocked chip returned no message")
        # The message should name the chip so the user knows why.
        if not expected and model and model not in msg:
            failures.append(f"  FAIL  {label!r}: block message doesn't name the chip {model!r}")

    # Untracked IP must not raise and must not block (nothing known to protect).
    if app._tuning_supported("192.168.222.222") is not True:
        failures.append("  FAIL  untracked ip: _tuning_supported should be True (no-op)")

    total = len(FIXTURES) + 1
    print(f"tuning guard: {total - len(failures)} / {total} pass")
    for f in failures:
        print(f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
