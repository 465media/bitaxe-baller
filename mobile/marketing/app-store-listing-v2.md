# App Store listing v2 — post-rejection rewrite (2026-05-27)

Apple rejected the original submission under **Guideline 2.1(b) — Performance: App Completeness**, citing that the app references a "Pro Subscription" but the associated In-App Purchase products haven't been submitted for review.

This v2 listing follows the **reader-app model under Guideline 3.1.3(b) (Multiplatform Services)** — same model Netflix, Spotify, Kindle, and others use. The iOS app is a free companion to a service the user already pays for elsewhere; nothing about that service is sold, marketed, or linked to inside the iOS app.

**Changes from v1:**
- Removed all mentions of "Pro subscription", pricing, and the `bitaxeballer.com/pro` URL from the description
- Removed the "REQUIREMENTS — A Bitaxe Baller Pro subscription ($29/year)" section that named the price and the purchase URL
- Reframed app description as "free companion app" without paywall-adjacent language
- Login screen + UI in the app webroot updated to match (PR `mobile/www/index.html`)

The companion app remains free in the App Store. License-key entry still works — Apple permits a "sign in to access" pattern under 3.1.3(b), as long as the app doesn't try to upsell or link out to the purchase flow.

---

## Apple App Store — replacement copy

### Name (30 char max)
```
Bitaxe Baller
```

### Subtitle (30 char max)
```
Monitor your Bitaxe fleet
```

### Promotional Text (170 char max — can update without resubmitting)
```
Reach your Bitaxe fleet from anywhere. Face ID unlock. Live hashrate, temps, and history — no port forwarding, no VPN, no fixed IP needed.
```

### Description (4000 char max) — REWRITTEN
```
Bitaxe Baller is a native iOS companion app for the Bitaxe Baller desktop application, the open-source dashboard a lot of Bitaxe hobbyists already use on their Mac or PC for monitoring and tuning their home Bitcoin solo-mining hardware.

If you already run the desktop app at home, this companion app brings the live view to your phone. No port forwarding, no VPN, no fixed IP. The desktop app on your home network opens an outbound connection to relay.bitaxeballer.com; the iOS app talks to the same relay and is routed to your devices using your license key as the credential. The relay does dumb routing only — all product logic stays on your local install.


WHAT THE APP SHOWS

• Live fleet view — total hashrate, total power draw, average efficiency in J/TH, online count. Updates every five seconds.
• Per-device drill-down — tap any device card for live metrics, rolling averages (1-minute through 1-hour), recent hashrate chart, ASIC and VR temperature charts.
• Severity-colored cards — green for stable, yellow for elevated temps or HW errors climbing, red for critical (overheating, offline, error rate over five percent).
• Pool and stratum readout — primary and fallback URLs, suggested difficulty, fallback flag, current pool difficulty.
• Recent events — tuning changes, restarts, pool changes.


SECURITY

• Face ID or Touch ID fires on every cold launch — your license key never sits in a webview-readable place without biometric unlock first.
• 24-hour session tokens, signed server-side. Revokable instantly by turning off remote access in the desktop app.
• No analytics, no tracking, no third-party SDKs. The app makes exactly three network calls: POST /login at startup with your license key, a WebSocket connection to relay.bitaxeballer.com for routing your data, and the standard system-level calls iOS makes regardless.


READ-ONLY COMPANION

The iOS app is a read-only viewer. Tuning your miners, scanning your LAN for new devices, and configuring pools all happen on the desktop app. We're a remote-monitoring tool, not a standalone controller.


REQUIREMENTS

• The Bitaxe Baller desktop application running on your home network (free download — see our website for setup instructions).
• One or more Bitaxe Bitcoin miners on the same LAN as the desktop install.
• A license key obtained from your desktop install (entered once on first launch; persisted to the device's secure enclave behind biometric unlock).


OPEN SOURCE

The desktop app and this iOS wrapper are MIT-licensed. The relay protocol is documented in the same repo. If you want to audit the license validation flow or the biometric storage layer, all of it is plain Python and JavaScript on GitHub.

Questions? Visit our support site or open a GitHub Issue.
```

(~2,650 char — well under the 4,000 limit.)

### Keywords (100 char max)
```
bitaxe,mining,bitcoin,solo mining,asic,hashrate,nerdaxe,gamma,supra,monitor,miner
```

### Support URL
```
https://bitaxeballer.com/support.html
```

### Marketing URL (optional)
```
https://bitaxeballer.com
```

### Privacy Policy URL
```
https://bitaxeballer.com/privacy.html
```

### What's New (for resubmission)
```
v1.0 — first public release. Native iOS app for monitoring your Bitaxe miners from your phone. Face ID gated, live fleet view and per-device detail with charts. Works with your existing Bitaxe Baller desktop install.
```

### Age rating / category / pricing
- **Primary category:** Utilities
- **Secondary category:** Finance
- **Age rating:** 4+ (no objectionable content)
- **Price:** Free
- **In-App Purchases:** None

---

## Response to send in App Store Connect Resolution Center

```
Thank you for the additional review.

Bitaxe Baller for iOS is a free read-only companion app to our open-source Bitaxe Baller desktop application. The iOS app does not include in-app purchases because there is nothing to purchase through the App Store — all paid functionality lives on the desktop application, which is not distributed through the App Store.

Per Guideline 3.1.3(b) (Multiplatform Services), users who already use the desktop application's paid features can also use them in the iOS companion app without restriction, in the same way Netflix, Spotify, Kindle, and other "reader" apps allow access to content/services obtained outside the App Store.

We have removed all references to paid subscription pricing and external purchase URLs from the iOS app's UI and from the App Store listing. The previous description and the long-term history section's "Pro" label and "Activate Pro in your desktop app" copy have both been replaced — the new build hides the long-term history section entirely for any user whose license does not grant access to that feature, so iOS users without entitlement see no marketing of any paid feature anywhere in the app.

The app's license-key entry screen is permitted under 3.1.3(b) ("sign in to access content/services that you've already purchased elsewhere"). The license key entered there is validated against the user's desktop install via our relay; no payment information is collected on iOS.

The updated binary has been uploaded. Please let us know if there is anything else we need to adjust.

Thank you,
465 Media
```

---

## What's been changed in the codebase

1. `mobile/www/index.html`:
   - Login screen "Pro license key" → "license key from your Bitaxe Baller desktop install"
   - `<h2>long-term history <span class="pro-tag">PRO</span></h2>` → `<h2>long-term history</h2>` (badge removed)
   - "Pro unlocks weeks or months ... Activate Pro in your desktop app" teaser removed entirely
   - `loadHistory()` now hides the whole section on 402/403 instead of swapping a locked/unlocked div
2. `mobile/ios/App/App/public/index.html` — synced via `npx cap sync ios`

## What's NOT changed

- Server-side gating still works — Pro license unlocks long-term history via the same `/api/device/<ip>/history` endpoint with the same 402 response for unentitled users. The iOS UI is the only difference: just hides instead of soliciting.
- Desktop apps (Mac/Windows) keep the Pro badge + upsell — those are not distributed via App Store and aren't subject to Apple's external-purchase rules.
- Android app submission was approved without this fight; we don't need to touch it.
