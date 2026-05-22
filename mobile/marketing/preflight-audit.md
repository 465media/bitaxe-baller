# Pre-Flight Audit — App Store + Play Store Submission

**Date:** 2026-05-22
**Branch:** `feat/mobile-capacitor`
**Auditor:** automated pre-flight (research only, no fixes applied)

## Summary

**FAIL — 3 BLOCKERS, 4 WARNINGS, 2 NITs** before you should attempt a TestFlight upload or Play submission. Most are 60-second fixes. The good news: ATS / cleartext / encryption-export / icon-alpha / launch-screen / bundle-id are all clean. The Android side is in better shape than iOS.

---

## BLOCKERS (must fix before submission)

### B1. iOS DEVELOPMENT_TEAM is wrong
- **File:** `mobile/ios/App/App.xcodeproj/project.pbxproj` lines 300, 323
- **Current:** `DEVELOPMENT_TEAM = MVBDDDFNV6;`
- **Expected:** `DEVELOPMENT_TEAM = K7SNP7C2XJ;` (per `MEMORY.md` and `reference_mobile-capacitor.md` — same team as desktop Mac signing)
- **Why it blocks:** Xcode automatic signing will pull provisioning profiles for the wrong Apple Developer account. Archive succeeds but TestFlight upload will fail at the App Store Connect handshake ("No matching profiles found" or "Team does not have access to this bundle ID"). Cannot be patched after the upload — must fix locally and re-archive.
- **Fix:** In Xcode → App target → Signing & Capabilities → Team dropdown → select the K7SNP7C2XJ team. Or edit the two `DEVELOPMENT_TEAM = MVBDDDFNV6;` lines directly. Verify the bundle ID `com.bitaxeballer.app` is registered under K7SNP7C2XJ in the Apple Developer portal first.

### B2. iOS orientation declares landscape — contradicts the portrait-only product intent
- **File:** `mobile/ios/App/App/Info.plist` lines 39-51
- **Current:** `UISupportedInterfaceOrientations` includes Portrait + LandscapeLeft + LandscapeRight; iPad variant additionally includes PortraitUpsideDown.
- **Why it blocks (for App Store):** Apple requires landscape support on iPad if any landscape orientation is declared — the layout must look correct in all declared orientations. The dashboard SPA (`mobile/www/index.html`) is hand-tuned for portrait phone widths. App reviewers WILL rotate the device on iPad. Rotation breaks → rejection ("Guideline 4.0 — Design").
- **Fix (pick one):**
  - **Easiest:** Strip landscape from both arrays so the app advertises portrait-only. Apple won't test rotation on a portrait-only app.
  - Or: explicitly support iPhone-only via `TARGETED_DEVICE_FAMILY = "1"` (currently `"1,2"` — line 313, 335 of pbxproj). This sidesteps iPad rotation entirely.
- **Recommendation:** Phone-only + portrait-only matches the product. Less surface area = less reviewer churn.

### B3. iOS deprecated `armv7` device capability
- **File:** `mobile/ios/App/App/Info.plist` lines 35-38
- **Current:** `UIRequiredDeviceCapabilities = [armv7]`
- **Why it blocks:** `armv7` is 32-bit ARM. Apple stopped accepting 32-bit binaries in 2017, and `IPHONEOS_DEPLOYMENT_TARGET = 15.0` means this app only runs on arm64 devices anyway. Listing `armv7` as a *required* capability tells the App Store the app needs a 32-bit device — which no iPhone since the iPhone 5s has. Modern store validation rejects this with "Invalid Required Architecture" or silently restricts download eligibility.
- **Fix:** Either remove the `UIRequiredDeviceCapabilities` key entirely (it's optional), or change `armv7` to `arm64`.

---

## WARNINGS (will not block submission but will cause friction)

### W1. Android `compileSdk` / `targetSdk` = 36 — newer than Play requires, but watch the policy date
- **File:** `mobile/android/variables.gradle` lines 3-4
- **Current:** `compileSdkVersion = 36`, `targetSdkVersion = 36`
- **Status:** Play Store as of Aug 2024 requires API 34 minimum for new apps; API 35+ is recommended; 36 is allowed. **This is fine.** Flagging because the audit brief expected exactly 34 — the project is actually ahead. No action.

### W2. Android `allowBackup = true` exposes the license key via ADB backup
- **File:** `mobile/android/app/src/main/AndroidManifest.xml` line 4
- **Current:** `android:allowBackup="true"` (Android default)
- **Risk:** With a debug-enabled device (developer mode on, USB debugging), anyone with physical access can `adb backup` the app's data partition and extract the contents of `@capacitor/preferences` storage — which is where the Pro license key lives. Even with biometric gating on the UI, the underlying SharedPreferences blob is recoverable.
- **Fix:** Set `android:allowBackup="false"`. If you want Auto Backup to Google Drive for app continuity, instead add `android:fullBackupContent="@xml/backup_rules"` and exclude the license key.

### W3. iOS marketing version is `1.0`, not `1.0.0`
- **File:** `mobile/ios/App/App.xcodeproj/project.pbxproj` lines 307, 330
- **Current:** `MARKETING_VERSION = 1.0;`
- **Status:** Apple accepts both `1.0` and `1.0.0` (CFBundleShortVersionString just needs to be 1-3 dot-separated integers). Asymmetry with Android (`versionName "1.0.0"`) is cosmetic but causes user confusion in support tickets ("the App Store says 1.0 but my Android says 1.0.0").
- **Fix:** Align to `1.0.0` for parity.

### W4. iOS `TARGETED_DEVICE_FAMILY = "1,2"` (iPhone + iPad) but no iPad design
- **File:** `mobile/ios/App/App.xcodeproj/project.pbxproj` lines 313, 335
- **Related to B2.** Declaring iPad support means reviewers will test on iPad. The SPA renders fine on iPad (responsive), but you'll be on the hook for iPad screenshots in App Store Connect, and rotation testing.
- **Recommendation:** Set to `"1"` (iPhone-only) for v1.0. Add iPad later if there's demand.

---

## NITs (cosmetic / future cleanup)

### N1. `config.xml` is legacy Cordova cruft
- **File:** `mobile/ios/App/App/config.xml` and `mobile/android/app/src/main/res/xml/config.xml`
- These are leftovers from Cordova templates. Capacitor doesn't use them. Harmless but they confuse new contributors. Optional cleanup.

### N2. `package.json` version is `0.1.0` while the apps ship `1.0.0`
- **File:** `mobile/package.json` line 3
- The package.json version isn't shipped in either binary, but if you ever wire up release tooling that reads it, you'll get a mismatch. Bump to `1.0.0` to match.

---

## What passed (green checks — sleep easy on these)

- **Bundle ID parity:** iOS `PRODUCT_BUNDLE_IDENTIFIER = com.bitaxeballer.app` matches Android `applicationId "com.bitaxeballer.app"` matches `capacitor.config.json` `appId`. Locked in across all four sources of truth.
- **App name parity:** "Bitaxe Baller" (with space) in `CFBundleDisplayName`, Android `strings.xml` (`<string name="app_name">`), and `capacitor.config.json` `appName`.
- **`NSFaceIDUsageDescription`:** present (line 29-30 of Info.plist), user-facing copy ("Unlock Bitaxe Baller with Face ID so your license key stays gated behind your biometric"), not generic boilerplate. App Store review passes.
- **`ITSAppUsesNonExemptEncryption = false`:** present (line 25-26 of Info.plist). Skips the per-upload export-compliance prompt in App Store Connect. Valid claim since the app uses only stock HTTPS/WSS.
- **App Transport Security:** No `NSAppTransportSecurity` / `NSAllowsArbitraryLoads` exception in Info.plist. Default-deny is in effect. All network goes to `https://relay.bitaxeballer.com` and `wss://relay.bitaxeballer.com` (verified in `www/index.html` lines 391-392). ATS-clean.
- **`UIRequiresFullScreen` not set:** allows multitasking / Split View on iPad (good — Apple penalizes apps that force fullscreen without reason).
- **Launch screen:** `LaunchScreen.storyboard` present at `mobile/ios/App/App/Base.lproj/LaunchScreen.storyboard`, references the `Splash` image asset, declared in Info.plist as `UILaunchStoryboardName`.
- **iOS marketing icon:** `AppIcon-512@2x.png` is 1024x1024, RGB (no alpha channel — Apple rejects alpha in marketing icons). Capacitor 8 uses single-icon mode; the asset catalog `Contents.json` correctly declares `idiom: universal, size: 1024x1024, platform: ios`. iOS auto-derives the smaller sizes at build time. **No missing icon sizes.**
- **Android `applicationId`:** `com.bitaxeballer.app` is set and matches iOS. Locked in — cannot change after first Play upload, and we're aligned correctly.
- **Android `versionCode = 1` / `versionName = "1.0.0"`:** initial release values, correct types (integer / string).
- **Android `minSdkVersion = 24`:** above the 23 floor required by `androidx.biometric`. BiometricPrompt API will function.
- **Android permissions:** `INTERNET` in app manifest; `USE_BIOMETRIC` + `USE_FINGERPRINT` auto-merged from the `@aparajita/capacitor-biometric-auth` AAR (verified in `app/build/intermediates/merged_manifests/.../AndroidManifest.xml`). No surprise permissions (no CAMERA / LOCATION / READ_CONTACTS / etc.) — Play's data-safety form will be minimal.
- **Android `usesCleartextTraffic`:** unset (defaults to `false` on `targetSdk ≥ 28`). HTTPS-only traffic. No accidental cleartext exception.
- **Android icons:** `ic_launcher.png`, `ic_launcher_round.png`, `ic_launcher_foreground.png`, `ic_launcher_background.png` present in all required densities (mdpi 48px, hdpi, xhdpi, xxhdpi, xxxhdpi 192px). Adaptive icon XMLs in `mipmap-anydpi-v26`. Splash drawables in port/land × density × day/night variants. Play upload won't choke on missing densities.
- **Android `android:label` / `android:icon` / `android:roundIcon`:** all set (manifest lines 5-7).
- **Capacitor config:** `appId`, `appName`, `webDir` correct. No `server.url` override (would force loading from a remote URL — disastrous for offline launch). No `server.cleartext = true`. The generated copies in `ios/App/App/capacitor.config.json` and `android/app/src/main/assets/capacitor.config.json` are in sync with root `capacitor.config.json`.
- **www bundle:** `index.html` (861 lines) and `plugins.js` (6 lines — esbuild-minified IIFE) both present in `mobile/www/`. `index.html` line 373 references `<script src="plugins.js">`. Silent-biometric-bypass fix from commit 052007d is in effect (line 434 of index.html: `throw new Error('Biometric plugin not loaded (plugins.js missing?).');` — hard fail rather than silent skip).
- **Capacitor 8.3.4** and plugin versions in `package.json` align with what the memory notes describe — no version drift from the documented stack.

---

## Recommended fix order (when you sit down to address)

1. **B1** (DEVELOPMENT_TEAM) — 10 seconds in Xcode. Without this, upload fails at the door.
2. **B2 + W4** together (orientation + device family) — strip landscape, set iPhone-only. 30 seconds in the plist + pbxproj.
3. **B3** (armv7 capability) — delete the key. 10 seconds.
4. **W2** (`allowBackup`) — 20 seconds in AndroidManifest.xml. Important for the Pro license-key threat model.
5. **W3, N2** (version alignment to `1.0.0`) — 20 seconds.
6. **N1** (Cordova `config.xml` cleanup) — defer to a future cleanup branch.

After fixes: rerun `npm run sync` to push capacitor.config changes into the platform projects, then archive iOS / `./gradlew bundleRelease` Android.
