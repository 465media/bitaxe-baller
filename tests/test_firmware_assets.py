"""
Regression tests for firmware asset resolution.

AxeOS v2.15.0 embedded the web UI into esp-miner.bin and stopped shipping
www.bin entirely. The catalog + flash path required a matched www+firmware
pair, so a v2.15.0 entry resolved to None ("no blessed firmware in catalog")
and could not be flashed at all. Older releases still ship both files and must
keep flashing as a pair.

The dangerous non-fix is substituting esp-miner.bin for the missing www.bin:
that uploads a firmware image to the device's OTAWWW (web-UI) endpoint. These
fixtures pin the shapes so nobody is ever tempted back into requiring a pair.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app
from app import _firmware_pair_for


def _asset(kind, board=0):
    return {"board_version": board, "asic_model": "all", "kind": kind,
            "url": f"https://example.invalid/{kind}.bin",
            "sha256": "0" * 64, "size": 1234}


# Real shapes: v2.15.0 is firmware-only, v2.14.2 is a pair.
V2_15_0 = {"version": "v2.15.0", "assets": [_asset("firmware")]}
V2_14_2 = {"version": "v2.14.2", "assets": [_asset("firmware"), _asset("www")]}
BROKEN  = {"version": "v9.9.9",  "assets": [_asset("www")]}          # no firmware
BOARD_PINNED = {"version": "v3.0.0", "assets": [_asset("firmware", board=601)]}

# (label, catalog, requested_version, expected_version, expects_www)
FIXTURES = [
    ("v2.15.0 firmware-only resolves", [V2_15_0],           None,      "v2.15.0", False),
    ("v2.14.2 pair still resolves",    [V2_14_2],           None,      "v2.14.2", True),
    ("latest wins across shapes",      [V2_14_2, V2_15_0],  None,      "v2.15.0", False),
    ("pinned older version",           [V2_14_2, V2_15_0],  "v2.14.2", "v2.14.2", True),
    ("no firmware asset -> None",      [BROKEN],            None,      None,      None),
    ("empty catalog -> None",          [],                  None,      None,      None),
    ("unknown version -> None",        [V2_15_0],           "v1.0.0",  None,      None),
    # board_version 0 means universal; a board-pinned asset is not a universal one.
    ("board-pinned is not universal",  [BOARD_PINNED],      None,      None,      None),
]


def main() -> int:
    failures = []
    original = app._fetch_firmware_releases
    try:
        for label, catalog, want, exp_version, exp_www in FIXTURES:
            app._fetch_firmware_releases = lambda _c=catalog: _c
            pair = _firmware_pair_for(want)

            if exp_version is None:
                if pair is not None:
                    failures.append(f"  FAIL  {label!r}: expected None, got {pair!r}")
                continue

            if pair is None:
                failures.append(f"  FAIL  {label!r}: expected {exp_version}, got None")
                continue
            if pair["version"] != exp_version:
                failures.append(f"  FAIL  {label!r}: version {pair['version']!r} != {exp_version!r}")
            if pair["firmware"] is None:
                failures.append(f"  FAIL  {label!r}: firmware asset missing")
            if bool(pair["www"]) != exp_www:
                failures.append(
                    f"  FAIL  {label!r}: www present={bool(pair['www'])}, expected {exp_www}")
            # The substitution guard: www must never be silently filled from firmware.
            if pair["www"] is not None and pair["www"]["url"] == pair["firmware"]["url"]:
                failures.append(f"  FAIL  {label!r}: www url is the firmware url — "
                                f"that would flash firmware to the web-UI partition")
    finally:
        app._fetch_firmware_releases = original

    total = len(FIXTURES)
    print(f"firmware assets: {total - len(failures)} / {total} pass")
    for f in failures:
        print(f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
