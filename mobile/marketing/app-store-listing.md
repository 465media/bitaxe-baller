# App Store / Play Store listing copy

Source of truth for the textual content on the iOS App Store and the Google Play Store. Update here, copy-paste into both portals. All limits are the official ones from Apple Developer / Google Play Console docs.

---

## Apple App Store

### Name (30 char max)

```
Bitaxe Baller
```

(13 char — well under)

### Subtitle (30 char max)

```
Monitor your Bitaxe fleet
```

(24 char)

Alternates that also fit, in case the above gets rejected:
- `Bitaxe miner dashboard` (22)
- `Bitcoin home miner monitor` (26)
- `Bitaxe Pro remote dashboard` (27)

### Promotional Text (170 char max — can update without resubmitting)

```
Reach your Bitaxe fleet from anywhere. Face ID unlock. Live hashrate, temps, and 90 days of history — no port forwarding, no VPN, no fixed IP needed.
```

(148 char)

### Description (4000 char max)

```
Bitaxe Baller is a native iOS app for monitoring your home Bitcoin solo-mining rigs from anywhere. It's the mobile companion to the open-source Bitaxe Baller desktop app — the dashboard a lot of Bitaxe hobbyists already use on their Mac or PC for tuning, alerts, and long-term history.

If you have one or more Bitaxe miners (Gamma, Supra, Ultra, Hex, or a Nerdaxe) running at home, this app gives you a real-time view of the whole fleet on your phone. No port forwarding, no VPN, no fixed IP. The desktop app on your home network opens an outbound connection to relay.bitaxeballer.com; the phone app talks to the same relay; your data is routed through with your license key as the credential. The relay is dumb routing — all the product logic stays on your local Bitaxe Baller install.


WHAT YOU GET

• Live fleet view — total hashrate, total power draw, average efficiency in J/TH, online count. Updates every five seconds.
• Per-device drill-down — tap any device card for live metrics, rolling averages (1-minute through 1-hour), recent hashrate chart, ASIC and VR temperature charts.
• Pro long-term history — 24-hour, 7-day, 30-day, and 90-day views of hashrate, temperatures, and efficiency. Spot silicon degradation, dust buildup, ambient temperature drift, and tuning regressions.
• Severity-colored cards — green for stable, yellow for elevated temps or HW errors climbing, red for critical (overheating, offline, error rate over five percent). Glance-able health at a glance.
• Pool and stratum readout — primary and fallback URLs, suggested difficulty, fallback flag, current pool difficulty.
• Recent events — tuning changes, restarts, pool changes.

THE SECURITY MODEL

• Face ID or Touch ID auto-fires on every cold launch — your license key never sits in a webview-readable place without biometric unlock first.
• 24-hour session tokens, signed server-side. Revokable instantly: turn off remote access in the desktop app's Pro modal and every session for your license drops within seconds.
• No analytics. No tracking. No third-party SDKs. The app makes exactly three network calls: POST /login at startup (license key in the body), WebSocket connection to relay.bitaxeballer.com for routing your /api/* responses, and the standard system-level calls iOS makes regardless. That's it.

THIS IS NOT (yet)

• A standalone miner controller — the iOS app is a remote viewer. Tuning, scanning your LAN, and adding new devices still happen on the desktop app's LAN dashboard. Remote tuning is on the public roadmap.
• A Bitcoin wallet, exchange, or payment app. We never touch your funds; we only show you the hashrate your hardware is producing.

REQUIREMENTS

• A Bitaxe Baller Pro subscription ($29/year, 5 device activations) — buy at bitaxeballer.com/pro
• The Bitaxe Baller desktop app (free download from bitaxeballer.com) running on your home network with at least one Bitaxe configured
• Remote access enabled in the desktop app's Pro modal

OPEN SOURCE

The desktop app and the mobile wrapper are MIT-licensed at github.com/465media/bitaxe-baller. The relay protocol is documented in the same repo. If you want to audit the license validation flow or the biometric storage layer, all of it is in plain Python and JavaScript.

Got a question or a bug report? Visit bitaxeballer.com/support or open a GitHub Issue.
```

(~2,950 char — well under the 4,000 limit, leaves room for stat callouts)

### Keywords (100 char max, comma-separated, no spaces after commas)

```
bitaxe,mining,bitcoin,solo mining,asic,hashrate,miner,hobby miner,nerdaxe,gamma,supra,monitor,baller
```

(105 char — trim if rejected. Apple matches keywords exactly so each one counts; spaces inside multi-word entries are fine.)

Trimmed alternate (94 char):

```
bitaxe,mining,bitcoin,solo mining,asic,hashrate,nerdaxe,gamma,supra,monitor,miner
```

### What's New (for v1.0 launch)

```
v1.0 — first public release. Native iOS app for monitoring your Bitaxe miners from anywhere. Face ID gated, live fleet view + per-device detail + Pro long-term history charts. Built on the same relay your desktop Bitaxe Baller already talks to.
```

(241 char — within Apple's 4,000 What's New limit; intentionally short for a clean launch entry.)

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

### Category

- Primary: **Utilities**
- Secondary: **Finance** (Bitcoin is finance-adjacent; this opens a second discovery path)

### Age rating

- **4+** — no objectionable content. The app is a metrics dashboard.
- Apple's questionnaire answers (all "None"): violence, cartoon violence, realistic violence, sexual content, nudity, profanity, alcohol/tobacco/drugs, gambling, horror/fear themes, mature themes, contests, unrestricted web access.
- **Note:** "Unrestricted web access" — answer **No**. The webview only loads `relay.bitaxeballer.com` content. We do not navigate to arbitrary URLs.

### Encryption export compliance

- Already declared in `Info.plist` via `ITSAppUsesNonExemptEncryption = false`. Apple's prompt at submission time will accept this declaration without further documentation.

### Pricing

- **Free** (the app itself; the Pro subscription is purchased separately on the desktop, not as an in-app purchase). This is important — see "in-app purchase" section below.

### In-app purchases

- **None.** Pro is sold on bitaxeballer.com via Stripe. The app does not advertise Pro purchase through the App Store. This is the correct model for our case: the desktop app is the primary purchase surface, the iOS app is a companion that requires existing Pro.
- Apple's guideline 3.1.1 prohibits directing users to external purchase from inside the app for the same digital content sold on iOS. We are safe because: (a) we do not offer Pro as an in-app purchase to compare with, and (b) Pro is for the desktop app's functionality, which is a separate product class (reader rule per guideline 3.1.3(a) applies — informational content / services). If Apple questions this, our defense is documented in the response template at the bottom of this file.

---

## Google Play Store

### Title (30 char max)

```
Bitaxe Baller
```

### Short description (80 char max)

```
Companion viewer — requires the Bitaxe Baller desktop app on home Mac/PC/Umbrel.
```

(79 char. Leads with the prerequisite so users don't install expecting
a standalone tool.)

### Full description (4000 char max)

Use the updated Apple iOS body (see app-store-listing-v2.md) which now
opens with the "IMPORTANT — Bitaxe Baller for iOS is a companion
viewer ..." paragraph. For Google Play, swap "iOS" → "Android" in that
paragraph and keep the rest verbatim. Sample first paragraph:

```
IMPORTANT — Bitaxe Baller for Android is a companion viewer for the Bitaxe Baller desktop application. It does NOT work as a standalone tool. You need to install Bitaxe Baller on your home Mac, Windows PC, or Umbrel server FIRST (free download at bitaxeballer.com), add your Bitaxe miners there, enable Remote Access in the desktop's Pro panel, and then pair this Android app to that desktop install. Without a paired desktop, this app has nothing to show.
```

Then the rest of the Apple body verbatim.

### Category

- **Tools**

### Content rating (IARC questionnaire)

- **Everyone**
- All categories answered No (violence, sex/nudity, profanity, gambling, alcohol/tobacco, fear, controlled substances, sensitive social issues, user-generated content, location sharing, personal info sharing, digital purchase).

### Data safety form

| Question | Answer |
|---|---|
| Does your app collect or share user data? | **Yes** |
| Personal info: name? | No |
| Personal info: email address? | **Yes (collected, not shared)** — license-server email comes back from POST /login |
| Personal info: User IDs (license key)? | **Yes (collected, not shared)** — license key |
| Financial info? | No (Stripe handles all financial data, not the app) |
| Health and fitness? | No |
| Messages? | No |
| Photos and videos? | No |
| Audio files? | No |
| Files and docs? | No |
| Calendar? | No |
| Contacts? | No |
| Location? | No |
| Web browsing? | No |
| App activity? | No |
| App info and performance: crash logs? | **Yes (collected, not shared)** — standard Android crash logs to Google Play Console only |
| App info and performance: diagnostics? | No (we do not run our own analytics) |
| Device or other IDs? | No |
| Is data encrypted in transit? | **Yes** |
| Can users request data deletion? | **Yes** (per our privacy policy) |

### Target audience

- **Ages 18+** — Bitcoin mining is implicitly an adult activity (cost of hardware, electricity, etc.). Selecting 18+ keeps us out of Google's Designed for Families program (which has separate compliance requirements we don't need).

### App access

- "All functionality is available without any access restrictions" — **No**, this app requires a Pro subscription credential to function.
- Provide reviewer credentials: a temporary Pro license key + the bitaxeballer.com URL. Generate a fresh test license before submission; deactivate it after approval.

### Ads

- **No ads.**

### Feature graphic

See `feature-graphic.svg` in this directory. Export to 1024×500 PNG before upload.

---

## In case Apple review pushes back on the "Pro is sold externally" angle

Apple guideline 3.1.1: "If you want to unlock features or functionality within your app (by way of example: subscriptions, in-game currencies, game levels, access to premium content, or unlocking a full version), you must use in-app purchase."

**Response template:**

> Bitaxe Baller iOS is a free companion app to the Bitaxe Baller desktop application — a free open-source utility for monitoring Bitcoin home-mining hardware on a user's local network. The desktop app is downloaded from our website (bitaxeballer.com), is MIT-licensed, and is not distributed through the App Store.
>
> The Pro subscription unlocks features within the desktop app — bulk tuning, auto-tune sweeps, persistent local SQLite history, Discord alerts, and remote access via our self-hosted relay. The iOS app is a thin viewer that consumes data the Pro features expose; it does not unlock any iOS-specific paid functionality.
>
> Per guideline 3.1.3(b) ("Multiplatform Services"), users who have purchased Pro elsewhere can use those features in our iOS app without restriction. We do not advertise the Pro subscription within the iOS app and we do not include external purchase links inside the app per the guideline.
>
> If Apple believes the iOS app should offer an equivalent in-app purchase, we will gladly add one — but this would create user confusion (the same Pro subscription would appear in two places at different prices, since IAP pricing must include Apple's commission). We would prefer to keep the iOS app as a free companion.

If Apple still rejects, the fallback is to add Pro as an iOS IAP at a higher price (around $40/year to net us the same $29 after Apple's 30% cut, dropping to 15% after year 1 of subscriptions per Apple's policy). That's a real product decision — discuss before resubmitting.
