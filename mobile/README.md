# Bitaxe Baller — Mobile

iOS + Android wrapper that talks to the production relay at `relay.bitaxeballer.com`. Built on Capacitor 8, single-file SPA in `www/`, native shells in `ios/` and `android/`.

The mobile app is a thin native wrapper around the same WebSocket-routed UI the desktop's relay SPA uses. License key lives in the device's secure preferences, gated behind a biometric prompt (Face ID / Touch ID / Android biometric) on every cold launch.

## Status (v0.1.0)

Scaffold only — read-only fleet view with biometric unlock. Goal of this iteration is to prove the Capacitor + relay pipeline works end-to-end and have something to side-load to TestFlight + Google Play internal track.

What works:
- Biometric-gated unlock of the stored license key
- `POST /login` → token → `WSS /ws/client` → poll `/api/devices` every 5s
- Summary cells (total hashrate / power / efficiency / online count)
- Device cards (severity-colored border, key metrics)
- Forget / log-out flows

What's intentionally NOT here yet:
- Tuning, scanning, adding devices (same scope decision as the relay SPA — read-only v0)
- Per-device detail page
- Push notifications (for alerts)
- App icon + splash screen art (Capacitor's defaults for now)
- Secure-enclave-wrapped key storage — v0 uses `@capacitor/preferences` with biometric as the access gate. Fine for TestFlight; tighten before public release.

## Prereqs

- **Node 20+**, npm 10+
- **Xcode 15+** for iOS builds — Mac App Store, ~7 GB. Open it once after install so it accepts the license + installs additional components.
- **Android Studio + Android SDK** for Android builds. Set `ANDROID_HOME` once installed.
- **CocoaPods** for iOS dependency management: `brew install cocoapods` (or via `sudo gem install cocoapods` if you prefer the gem path).

You don't need any of the native tooling to edit `www/`. Run `npm run sync` after `www/` changes and it'll copy assets into both platform projects.

## Run it

```bash
# Bootstrap once
npm install

# Open the iOS project in Xcode and run on a simulator or device:
npm run open:ios

# Or build + run directly to a connected device / simulator:
npm run ios

# Android equivalents:
npm run open:android
npm run android
```

If you make changes under `www/`:
```bash
npm run sync
```

Then rerun from Xcode / Android Studio.

## How biometrics work in this app

Plugin: [@aparajita/capacitor-biometric-auth](https://github.com/aparajita/capacitor-biometric-auth)

1. First launch (no stored key): user enters license key in the Sign-in card. App stores it in `@capacitor/preferences` and immediately prompts to set up biometrics ("would you like to use Face ID / Touch ID next time?"). Either answer works; subsequent launches will still prompt.
2. Subsequent launches: app sees a stored key, shows the **Unlock** card with a Face ID / Touch ID button. Biometric prompt → on success, fetch a fresh session token from the relay and connect.
3. **Forget this device** wipes the stored key. Tapping it on any launch puts you back at the Sign-in card.

For Apple App Store review: this is the "more than a thin wrapper" hook. The app uses native platform capabilities (LocalAuthentication on iOS, BiometricPrompt on Android) to secure a credential — reviewers will see real native functionality, not just a webview pointed at a URL.

## What lives in this repo (and what's regenerated)

Committed:
- `www/` — the bundled web app the webview loads
- `package.json` / `package-lock.json`
- `capacitor.config.json`
- `ios/App/App/Info.plist` — bundle ID, permissions strings, etc.
- `android/app/src/main/AndroidManifest.xml` — same idea for Android
- `ios/App/Podfile`, `android/build.gradle`, etc. — version pins

Regenerated (gitignored):
- `node_modules/`
- `ios/App/Pods/`
- `ios/App/build/`, `android/build/`, `android/app/build/`
- `ios/App/App/public/`, `android/app/src/main/assets/public/` (these are populated from `www/` on `cap sync`)

## Bundle identifiers

- iOS: `com.bitaxeballer.mobile`
- Android: `com.bitaxeballer.mobile`

Both need to be claimed in your Apple Developer + Google Play accounts before you can submit to the stores.

## Roadmap

Per the project handoff notes:
1. **v0.1** (this scaffold) — Capacitor wrap, biometric unlock, read-only fleet view, tested in simulators
2. **v0.2** — TestFlight + Google Play internal track. Real device testing. App icon, splash screen.
3. **v0.3** — Push notifications for alert events (server-side endpoint + Capacitor plugin)
4. **v0.4** — Tuning over relay (mirrors desktop modal patterns). Per-device detail page.
5. **v1.0** — Public App Store + Play submission.
