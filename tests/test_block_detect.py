"""
Regression tests for solved-block detection.

Catches the bug that hid two real DigiByte blocks: `poll_loop` read only
`blockFound`, which stock Bitaxe AxeOS exposes but the NerdQAxe fork does
not — the fork uses `foundBlocks` / `totalFoundBlocks`. `data.get(...) or 0`
turned "this firmware has no counter" into "this device has found nothing",
so the delta could never fire. nerdaxe_001 solved DGB blocks on 2026-08-15
and 2026-08-18 and the app showed nothing either time.

Field payloads below are trimmed from real `/api/system/info` responses.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import _device_block_count, _block_find_transition


# (label, /api/system/info fragment, expected count)
COUNT_FIXTURES = [
    ("Bitaxe Gamma, no finds",   {"blockFound": 0},                              0),
    ("Bitaxe Gamma, one find",   {"blockFound": 1},                              1),
    ("NerdQAxe++ (real, 8/18)",  {"foundBlocks": 2, "totalFoundBlocks": 2},      2),
    ("NerdQAxe++, no finds",     {"foundBlocks": 0, "totalFoundBlocks": 0},      0),
    # Lifetime count wins over the session count — a session counter resets on
    # reboot and would re-fire for a block we already celebrated.
    ("total beats session",      {"foundBlocks": 0, "totalFoundBlocks": 3},      3),
    # Stock field wins when a firmware somehow reports both.
    ("blockFound beats fork",    {"blockFound": 1, "totalFoundBlocks": 9},       1),
    # Braiins: fetch_braiins() maps "Found Blocks" onto blockFound upstream.
    ("Braiins BMM-101",          {"blockFound": 0},                              0),
    # No counter at all must stay None, NOT 0 — that distinction is the bug.
    ("firmware has no counter",  {"hashRate": 480.0},                         None),
    ("garbage value ignored",    {"blockFound": "n/a", "foundBlocks": 4},        4),
]

# (label, prev, cur, expected_new_prev, expected_delta)
TRANSITION_FIXTURES = [
    ("first poll baselines",         None, 0,    0,    0),
    ("first poll, nonzero baseline", None, 2,    2,    0),  # no retro celebration
    ("steady state",                 2,    2,    2,    0),
    ("real find",                    1,    2,    2,    1),
    ("catch-up after downtime",      0,    3,    3,    3),
    ("no counter holds prev",        2,    None, 2,    0),
    # Counter reset (firmware wipe, or an update that changes source field):
    # re-baseline, so the next real find still fires.
    ("counter reset re-baselines",   2,    0,    0,    0),
    ("find after a reset fires",     0,    1,    1,    1),
]


def main() -> int:
    failures = []

    for label, payload, expected in COUNT_FIXTURES:
        actual = _device_block_count(payload)
        if actual != expected:
            failures.append(f"  FAIL  count {label!r}: got {actual!r}, expected {expected!r}")

    for label, prev, cur, exp_prev, exp_delta in TRANSITION_FIXTURES:
        new_prev, delta = _block_find_transition(prev, cur)
        if (new_prev, delta) != (exp_prev, exp_delta):
            failures.append(
                f"  FAIL  transition {label!r}: prev={prev!r} cur={cur!r} → "
                f"got {(new_prev, delta)!r}, expected {(exp_prev, exp_delta)!r}"
            )

    total = len(COUNT_FIXTURES) + len(TRANSITION_FIXTURES)
    print(f"block detection: {total - len(failures)} / {total} pass")
    for f in failures:
        print(f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
