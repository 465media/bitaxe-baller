"""
Regression tests for the AxeOS firmware-flash guard (_fw_flashable).

The guard is the single thing standing between "one-click update" and bricking
someone's miner by pushing stock Bitaxe firmware onto hardware it wasn't built
for. It is FAIL-CLOSED: a device is only flashable when it is BOTH a recognized
Bitaxe board (boardVersion allowlist) AND running stock AxeOS (clean semver).

These fixtures pin the real shapes we've seen on the bench so a future edit to
the allowlist or the version regex can't silently open the gate on a non-Bitaxe
board. The headline case is the Nexus S1 (BM1373): a different vendor's device
running an ESP-Miner/AxeOS fork on a chip stock Bitaxe firmware doesn't target —
it MUST never be flashable, whatever boardVersion its fork happens to report.
"""

import sys
from pathlib import Path

# Allow running as `python3 tests/test_firmware_guard.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import _fw_flashable, _is_bitaxe_board, _is_stock_axeos

# (label, /api/system/info-ish dict, expected _fw_flashable)
FIXTURES = [
    # --- genuine Bitaxe on stock AxeOS: the ONLY thing we ever flash ---
    ("Gamma 601 / stock v2.14.0",
     {"boardVersion": "601", "ASICModel": "BM1370", "version": "v2.14.0"}, True),
    ("Gamma 602 / stock v2.14.1",
     {"boardVersion": "602", "ASICModel": "BM1370", "version": "v2.14.1"}, True),
    ("Supra 401 / stock v2.13.0",
     {"boardVersion": "401", "ASICModel": "BM1368", "version": "v2.13.0"}, True),

    # --- Nexus S1 (BM1373): the case this session is about. A different vendor,
    #     ESP-Miner/AxeOS fork, 3nm S23 silicon. Must be fail-closed no matter
    #     which boardVersion shape the fork reports. ---
    ("Nexus S1 BM1373 / fork reports NO boardVersion",
     {"ASICModel": "BM1373", "asicCount": 4, "version": "v1.0.0"}, False),
    ("Nexus S1 BM1373 / fork reports empty boardVersion",
     {"boardVersion": "", "ASICModel": "BM1373", "version": "v1.0.0"}, False),
    ("Nexus S1 BM1373 / fork reports a non-Bitaxe boardVersion",
     {"boardVersion": "700", "ASICModel": "BM1373", "version": "v1.0.0"}, False),
    ("Nexus S1 BM1373 / fork reports a branded version",
     {"boardVersion": "700", "ASICModel": "BM1373", "version": "NexusOS-1.0"}, False),

    # --- NerdQAxe++ Rev7: BM1370 too, but its own fork + no boardVersion ---
    ("NerdQAxe++ Rev7 / fork v1.1.0, no boardVersion",
     {"ASICModel": "BM1370", "deviceModel": "NerdQAxe++ TPS546", "version": "v1.1.0"}, False),

    # --- genuine Bitaxe board, but running a CUSTOM firmware fork: the version
    #     gate must stop us flashing stock AxeOS over it ---
    ("Bitaxe 601 running LottoAxe / branded version",
     {"boardVersion": "601", "ASICModel": "BM1370", "version": "LottoAxe-1.2"}, False),
    ("Bitaxe 601 / -LTS build",
     {"boardVersion": "601", "ASICModel": "BM1370", "version": "v1.0.37.2-LTS"}, False),

    # --- Braiins BMM-101: monitor-only, no boardVersion at all ---
    ("Braiins BMM-101",
     {"ASICModel": "BMM-101", "version": "BOSer"}, False),

    # --- junk / empty ---
    ("empty info", {}, False),
]


def main() -> int:
    failures = []
    for label, info, expected in FIXTURES:
        actual = _fw_flashable(info)
        if actual != expected:
            failures.append(
                f"  FAIL  {label!r}: _fw_flashable → got {actual}, expected {expected} "
                f"(board={_is_bitaxe_board(info)}, stock={_is_stock_axeos(info)})"
            )

    # Explicit invariant: no BM1373 fixture is ever flashable, whatever it reports.
    for label, info, _ in FIXTURES:
        if info.get("ASICModel") == "BM1373" and _fw_flashable(info):
            failures.append(f"  FAIL  BM1373 board flashable — brick risk: {label!r}")

    total = len(FIXTURES)
    print(f"firmware guard: {total - len(failures)} / {total} pass")
    for f in failures:
        print(f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
