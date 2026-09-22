# Bitaxe Baller — App Creation Per Platform

> How the app gets built and packaged for each distribution channel.
> Verified against the actual build scripts, CI workflows, and manifests in the repo.

## The shared core (important context first)

Every desktop and server channel packages the **exact same Flask app** — `app.py` + `templates/` + `static/`. The web UI is vanilla JS with **no build step**. What differs per platform is only the *packaging wrapper*. The two desktop builds even share one PyInstaller spec (`build/bitaxe-baller.spec`); Docker uses a leaner variant. Mobile is the one true exception — it's a separate codebase in a separate repo.

The product ships through **5 channels across 3 repos**, plus a supporting relay service.

---

## 1. macOS desktop (signed + notarized DMG)

| | |
|---|---|
| **Tooling** | PyInstaller → `codesign` → `notarytool` → `hdiutil` (DMG) |
| **Where** | **Local only** — `build/build-mac.sh` on Nate's Mac (needs Apple notary creds + the Ed25519 auto-update key; not in CI) |
| **Output** | `Bitaxe-Baller-Mac.dmg` |

**Pipeline** (the script does all of this in order):

1. PyInstaller bundles the Flask app + **pywebview** (Cocoa backend) into `Bitaxe Baller.app` — pywebview gives it a native window instead of opening a browser tab.
2. `codesign` with a Developer ID Application cert, Hardened Runtime, and `build/entitlements.plist`.
3. Submit to Apple's notary service (`xcrun notarytool submit --wait`), then **staple** the ticket.
4. Wrap into a drag-to-Applications `.dmg`, then sign + notarize + staple the DMG too.
5. **Ed25519-sign** the DMG and generate an `appcast.xml` entry for in-app auto-update.

Notable app config (from the spec's `info_plist`): bundle id `com.465-media.bitaxe-baller`, min macOS 12.0, an **App Transport Security exception** (Bitaxes only serve plaintext HTTP on the LAN), and a Local Network privacy-prompt string.

**Distribution:** attached to a GitHub Release; shipped clients auto-update via the signed appcast feed.

---

## 2. Windows desktop (Authenticode-signed EXE)

| | |
|---|---|
| **Tooling** | PyInstaller (one-folder) → **Inno Setup** installer → Azure Trusted Signing |
| **Where** | **CI** — `.github/workflows/build-windows.yml`, triggered on `v*.*.*` tag push (or manual dispatch) |
| **Output** | `Bitaxe-Baller-Windows.exe` (an installer) |

**Pipeline:**

1. PyInstaller produces a one-folder build (`.exe` + `_internal/`) — one-folder avoids the slow `%TEMP%` extraction on every launch.
2. Authenticode-sign the **inner** app `.exe` via Azure Trusted Signing.
3. `build/installer.iss` (Inno Setup) wraps it into a per-user installer → `%LOCALAPPDATA%\Programs\BitaxeBaller`, with Start Menu + optional desktop shortcut and an uninstaller. User data (CSV logs, config) lives in `%APPDATA%` so uninstall doesn't wipe history.
4. Authenticode-sign the **outer** installer `.exe` (this is what SmartScreen checks).
5. Generate the Ed25519 appcast entry for auto-update.

> ⚠️ **Caveat:** the Authenticode signing steps are gated on `if: has_azure == 'true'`. Until the Azure service-principal setup is finished, builds ship **unsigned** and Windows SmartScreen shows "unknown publisher." Auto-update still works regardless.

**Distribution:** uploaded to the GitHub Release for that tag.

---

## 3. Umbrel / Docker self-host

| | |
|---|---|
| **Tooling** | Multi-stage `Dockerfile` (`python:3.12-slim`) |
| **Where** | **CI** — `.github/workflows/build-docker.yml`, on `v*.*.*` tag push |
| **Output** | Multi-arch image `ghcr.io/465media/bitaxe-baller` (linux/amd64 + linux/arm64) |

**Pipeline:** Buildx + QEMU build a two-stage image (deps into a venv, then a slim runtime with `tini` + `gosu`). It's the **same Flask app minus pywebview** — containers are browser-accessed, so no native UI layer. Runs as unprivileged `baller` (uid 1000) after fixing bind-mount ownership.

**Runtime requirements** baked into the design:

- **`network_mode: host`** is mandatory — the LAN scanner and mDNS publishing both break under bridge networking.
- A `/data` volume persists config, logs, and history across restarts.

**Umbrel packaging** (`umbrel/`): `umbrel-app.yml` (manifest) + `docker-compose.yml`. Uses ports **13700/13701** (13700 is a nod to the BM1370 chip) to avoid collisions with other Umbrel apps, and puts the tile behind Umbrel's `app_proxy` login.

**Distribution — two-step, and the compose pins by digest:** the image builds in CI, but the `docker-compose.yml` references it by `@sha256:...`, and that digest has to be **manually bumped** and mirrored to the separate public repo **`465media/umbrel-bitaxe-baller-store`**. This is a deliberate separate release step, not automatic.

---

## 4 & 5. iOS + Android mobile

| | |
|---|---|
| **Tooling** | **Capacitor** (one project → `ios/` + `android/` + shared `www/`) |
| **Where** | **Separate private repo** — `465media/bitaxe-baller-mobile` (⚠️ *not* in this repo) |
| **Output** | App Store build (iOS) + Play Store build (Android) |

- **One Capacitor codebase covers both platforms.** It has its **own version line (1.2.x)**, independent of the dashboard's (currently 1.19.0).
- The apps are **thin relay clients** — they don't run the Flask app; they reach the user's LAN dashboard remotely through the relay (see below).
- Both are **live**: iOS on the App Store, Android on Google Play.
- History note: mobile was extracted (2026-06-18) via `git subtree split` from the old `feat/mobile-capacitor` branch of this repo, which is now superseded. The Android signing keystore (`upload-keystore.jks`) is kept untracked/local — never in git.

You'll need access to that private repo to touch mobile — nothing about it lives in the main repo.

---

## Supporting infra: the Relay (not a shippable "app," but it's the spine)

`relay/`, deployed at `relay.bitaxeballer.com`. A dumb in-memory WebSocket router: the desktop app opens an outbound WSS, and remote browsers + the mobile apps connect and get routed by license key. All product logic and safety bounds stay in the local app — the relay just moves bytes. It's what connects **desktop ⇄ remote browser ⇄ mobile**.

---

## Quick reference table

| Channel | Build tool | Built where | Signing | Trigger | Artifact |
|---|---|---|---|---|---|
| **macOS** | PyInstaller + DMG | Local (Nate's Mac) | Apple Developer ID + notarize | Manual script | `.dmg` → GitHub Release |
| **Windows** | PyInstaller + Inno Setup | GitHub Actions | Azure Trusted Signing* | Tag push `v*.*.*` | `.exe` → GitHub Release |
| **Umbrel/Docker** | Dockerfile (multi-arch) | GitHub Actions | — (digest-pinned) | Tag push `v*.*.*` | GHCR image → community store repo |
| **iOS** | Capacitor | Separate private repo | Apple signing | Manual (Xcode/CI in that repo) | App Store |
| **Android** | Capacitor | Separate private repo | JKS keystore | Manual | Play Store |

\* *Windows Authenticode signing is conditional — unsigned until Azure SP setup lands.*

---

## Two things to call out

1. **Desktop/server = same app, three wrappers.** Mac and Windows share a PyInstaller spec; Docker drops pywebview. Mobile is genuinely separate.
2. **Releasing is a multi-step, order-sensitive sequence** (tag → CI for Windows/Docker → local Mac build + appcast → separate Umbrel digest bump). If you'll be cutting releases, follow the `release-process` notes rather than improvising.
