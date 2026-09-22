# Bitaxe Baller — Project Handoff

Living state snapshot for picking this up in a fresh Claude Code session. **Keep this current** — stale handoffs are the #1 cause of lost context between sessions. Update the "Current state" and "Open threads" sections whenever something meaningful ships.

_Last updated: 2026-09-22 · Current version: **v1.22.0** (shipped 2026-08-27) · **v1.23.0 staged, not yet released**_

## What this is

A monitoring + tuning product for Bitaxe Gamma (BM1370) miners, built around a Flask app + vanilla-JS dashboard. It is **not** a single app — it ships through five distribution channels across three repos. The checked-in `CLAUDE.md` has the authoritative architecture + ecosystem map; read it first. Don't trust version strings in any doc — verify against `git` / `gh`.

Built for Nate's Gammas. Bench hardware on his LAN:

| Device | IP | Notes |
|---|---|---|
| Bitaxe Gamma | `192.168.1.223` | BM1370, `boardVersion` 601 — the primary test device |
| NerdQAxe++ Rev7 | `192.168.1.216` / `.218` | shufps fork firmware, no `boardVersion`, nested `stratum.pools[]` |
| Braiins BMM-101 | `192.168.1.100` | monitor-only, CGMiner TCP API on :4028 |

## The five channels (see CLAUDE.md for the full table)

All five are in sync at the current release except mobile, which runs its own version line.

1. **macOS desktop** — signed + notarized DMG, built locally (`build/build-mac.sh` + `release-mac.sh`). ✅ 1.22.0
2. **Windows desktop** — Authenticode-signed EXE, built in CI on tag push. ✅ 1.22.0
3. **Umbrel self-host** — Docker + `465media/umbrel-bitaxe-baller-store`. ✅ 1.22.0, digest `sha256:1e17b152`
4. **iOS** — App Store **v1.2.2**. Source: private `465media/bitaxe-baller-mobile`.
5. **Android** — Play Store **v1.2.1**. Same repo, one Capacitor codebase.

The **relay** (`relay/`, deployed at `relay.bitaxeballer.com`) is the spine connecting desktop ⇄ remote browser ⇄ mobile. Note the live relay at `/opt/bitaxe-baller-relay` is loose scp'd files, **not** a git checkout — always diff before overwriting.

## Current state (2026-09-22)

- **v1.22.0 (card drag-to-reorder, Pro) is fully shipped** across desktop, Umbrel, site changelog and Discord. Mobile needed zero changes — it renders `/api/devices` straight through, so a Pro user's custom order shows up on their phone over the relay automatically.
- `main` is clean. The only commits since the v1.22.0 tag are the Umbrel digest pin, a PRO_FEATURES bookkeeping line, and the automated `store-monitor` state commits.
- **Mobile has not moved since 2026-06-18.** No tags, no releases, no CI. The repo still can't reproduce the shipped Android app from a fresh clone (frozen Capacitor gradle files, missing MLKit barcode plugin → broken QR pairing).
- Nothing in the app is half-finished. What's left is new work plus the hardening list below.

## Unshipped work sitting in branches

- **v1.23.0 is staged on `claude/v1.23.0-quai-port`** — Quai chain support (was PR #10) and the in-app listen-port setting, both rebased off their stale v1.19.1 / v1.20.0 version bumps onto 1.22.0, then bumped together to **1.23.0** via `build/release_prep.py`. Release notes are written to `build/release-notes/v1.23.0.md`. All six test scripts pass; the port endpoint was smoke-tested end-to-end (set / validate / persist / reset, host-only 403, `PORT`-env 409). **Not merged, not tagged.**
  - Conflict resolutions worth knowing: the Quai `0x` address check sits *after* the legacy-DGB base58 check (independent signals, both kept); the port branch predated the electricity feature, so the default-config dict and the `/api/config/*` routes keep both sides.
  - `docs/changelog-v1.20.0-draft.md` was dropped — it hand-rolled changelog copy that `build/release_prep.py notes` now generates, under the wrong version header.
- **Baller Board product page** — site repo branch `feat/baller-board-page`, 1 commit, unmerged. `bitaxeballer.com/baller-board.html` currently **404s**. Blocked on Nate, not on code: real product photos, the second Bitaxe-stats display mode, and Stripe shipping-address collection. Deliberately **not** in nav or sitemap while pre-launch.

## How to release

Follow the `release-process` memory file — the steps are order-sensitive (version-bump checklist → merge to main → `gh release create` triggers Win/Docker/Discord CI → local Mac build → `release-mac.sh` merges the Mac entry into the appcast AFTER Windows CI → separate Umbrel digest bump → site changelog + `notify-changelog.py`). Signing secrets live in the **main** repo's `build/` (`.env.signing`, `.update-signing-key`), not in worktrees; 1Password holds escrow copies.

## Open threads / roadmap (see PRO_FEATURES.md for full scoping)

**Hardening, ranked by blast radius:**

1. **No auth on any dashboard endpoint** — tune/pool/restart/flash are all open. Fine as LAN-trust; less fine since remote access went free in v1.16.5 and the relay forwards mutating calls. Umbrel's host networking is the most exposed deploy.
2. **Manual multipart flash bypasses the brick guard** — deliberate (user's own files = legitimate revert-to-stock), but it's the one remaining brick vector and has no explicit in-UI confirmation.
3. **Mac build + signing on one laptop**, no CI. Secrets are escrowed; the machine is still the bus factor.
4. **Mobile repo can't reproduce the shipped apps** (see above). Also: token in plain Preferences rather than Keychain/Keystore, Android pairs as `platform:'ios'`.
5. **Month-end draw miss guard** — the leaderboard draw only fires 23:00–23:59 UTC on the last day of the month; a process outage across that hour silently skips the month.

**Features, cheapest first:**

- **Chip-level normalized comparison** (GH/s per W per MHz across the fleet) — ~half day, free tier, all inputs already polled.
- **Live share feed + best-shares leaderboard** — ~1–2 days, free tier, high goodwill.
- **Combined Schedule engine** — the pool half shipped in v1.20.0; power scheduler + standby is the other output of the same ticker. ~1 day on existing infrastructure.
- **Fleet auto-tune campaign** — sequential across selected devices with a rollup view. ~1 day, Pro.
- **Tuning-over-relay** — remote dashboard is read-only today; the write path was parked.
- **Push notifications on mobile** — the core mobile value prop, still unbuilt.
- **Umbrel official store** — never submitted to `getumbrel/umbrel-apps`; competitors are listed there.

**Parked deliberately:** Thermal Guardian (no user signal yet), DGB SHA-256 difficulty source (no clean free feed — odds are hidden, so it's cosmetic), NerdQAxe pool-WRITE to the shufps fork (untested nested `stratum.pools[]`; users configure via the device UI).

## Picking up cold

> Read CLAUDE.md and the memory files (`project-ecosystem-map`, `release-process`), then verify the live version with `gh release list`. Tell me which of the five channels we're touching and I'll confirm current state before changing anything.
