# Screenshot capture guide

Both Apple and Google show screenshots as the dominant visual element of the listing — most people decide whether to install based on these three to five frames, not the description. Take them with real data on a real device.

## Required dimensions

| Platform | Device size | Resolution | Min count | Max count |
|---|---|---|---|---|
| Apple | 6.7" iPhone (Pro Max) | **1290×2796** | 2 | 10 |
| Apple | 6.5" iPhone (XS Max, 11) | 1284×2778 | optional | 10 |
| Google | Phone | **9:16 aspect, min 320 px, max 3840 px** | 2 | 8 |
| Google | 7" tablet | optional | 0 | 8 |
| Google | 10" tablet | optional | 0 | 8 |

Apple's 6.7" iPhone size is the one they show on most phones in the App Store. If you only supply one size, supply this one — Apple downscales it for everything smaller. **Take all screenshots on your iPhone 14 (which is 6.1") and then upscale via Apple's screenshot tools, or shoot on an iPhone 15 Pro Max in the Simulator (which is 6.7" natively).** Simulator is easier; let me know if you want me to walk you through Simulator screenshot capture.

Google's requirement is less strict — anything 9:16 phone-shaped works. We'll reuse the iPhone screenshots cropped to 9:16 for Google. No re-capture needed.

## Setup before capturing

1. **Have at least three Bitaxe devices configured** in your desktop app, with their hashrate, temps, and shares actively populating. If you only have one Bitaxe, fake two more by adding their IPs (192.0.2.10 and 192.0.2.11) — they'll show as offline but at least the home view has multiple cards.
2. **Activate Pro** so the long-term history charts have real data. If you've only had Pro for a day, the 7d/30d/90d ranges may be sparse — that's fine, the screenshot reads as "history available," not "long history available."
3. **Disable phone notifications** (Do Not Disturb on) so a Slack ping doesn't land in the screenshot.
4. **Set status bar to a clean look** — full battery icon, full wifi icon, 9:41 (Apple's preferred time per their HIG; you can fake this in the Simulator or just take it when your real clock happens to read close to it).
5. **Disable theme auto-follow if you have it on** — pick dark mode and stay in dark mode for consistency across frames. The dark mode is the brand's primary look.

## Six frames to capture

Each one is meant to "sell" a specific feature in three seconds of scanning. Annotate with a single short caption (overlay text added in Figma or Photos markup after capture).

### Frame 1 — Hero shot: fleet view, multiple devices

**What's on screen:**
- Header: BITAXE.BALLER + REMOTE · PREVIEW tag + green connected dot
- Summary cells: total hashrate, total power, avg efficiency, online count (all populated with real numbers)
- 3-4 device cards visible, mixed severity (one green, one yellow if possible)

**Caption overlay (above the screenshot):**
> Your whole fleet,
> from anywhere.

**How to capture:**
- App is on the fleet view
- Scroll position: top of the list

### Frame 2 — Per-device detail with live charts

**What's on screen:**
- Detail view header (device label, IP, severity dot)
- Live metrics grid with hashrate, temps, power populated
- Recent hashrate chart visible — looking for a populated chart with the green stroke + gradient fill

**Caption:**
> Live hashrate, temps,
> shares — five-second refresh.

**How to capture:**
- Tap a card → detail view
- Scroll just enough that the hashrate chart is fully visible

### Frame 3 — Pro long-term history sweep

**What's on screen:**
- Long-term history section header with the PRO chip
- Range buttons row with **30d** active (this looks more impressive than 24h)
- The three stacked charts: hashrate, temps, efficiency — populated

**Caption:**
> 90 days of history.
> Spot drift before it costs you.

**How to capture:**
- Scroll the detail view to the long-history section
- Tap "30d" so the active range is visible

### Frame 4 — Face ID unlock prompt

**What's on screen:**
- The Bitaxe Baller Unlock card visible behind the Face ID system prompt overlay
- The Face ID prompt itself ("Bitaxe Baller wants to use Face ID")

**Caption:**
> Face ID gated.
> Your license never sees a webview.

**How to capture:**
- Cold-kill the app (App Switcher → swipe up)
- Tap the icon to relaunch
- The Face ID prompt should auto-fire — quickly tap the volume-up + side button to screenshot before it succeeds or fails. (You'll likely need to take this multiple times to get the timing right.)
- Alternative if too hard to time: ask Nathan to manually trigger by canceling first and re-tapping Unlock; the screenshot is the system prompt regardless of trigger origin.

### Frame 5 — Severity in action (yellow or red card)

**What's on screen:**
- Fleet view, but zoomed/cropped to show one or two cards with non-green severity
- Or: the home view filtered to a device showing a real recommendation (overheating, HW errors, offline)

**Caption:**
> Yellow means look.
> Red means act now.

**How to capture:**
- If your real Bitaxes are well-behaved, simulate by changing a recommendation rule threshold temporarily (e.g. raise the "ASIC warn" temp threshold to a value below your current temp) — set it back after the screenshot.
- Or use Frame 1 if you don't want to mess with thresholds; this frame is nice-to-have, not required.

### Frame 6 — Both modes side-by-side (or remote-access toggle in Pro modal)

Pick ONE of these depending on what reads stronger:

**Option A — light + dark side by side**

Either two stacked half-screenshots or two thumbnails. Useful if reviewers want to see the app respects system theme.

**Caption:**
> Built for day + night.

**Option B — remote-access toggle in the Pro modal of the desktop app**

This isn't on the phone, so skip if you want only-phone screenshots. But it's useful proof-of-control in the listing.

**Caption:**
> One toggle. Instant cut-off.

## Annotation style

Match the brand palette:

- Background tint above the screenshot: solid dark `#0a0d0c` (the app's primary background)
- Caption text: `#00ff9c` (the accent green) at ~36-42pt, regular weight, JetBrains Mono. Two lines max, second line slightly dimmer.
- Caption position: above the device frame, centered, with ~12% of the canvas above as headroom

Apple shows these screenshots at ~250px wide in the listing, so caption text needs to be readable at that size. Avoid thin or italic fonts.

## Tooling

- **iPhone 15 Pro Max Simulator** for 1290×2796 capture: Xcode → Window → Devices and Simulators → iPhone 15 Pro Max → run app → Cmd+S inside Simulator to take a 1:1 screenshot.
- **Figma** for adding the caption + dark-background banner — free, has Bitaxe Baller's font (JetBrains Mono) available via Google Fonts. Use a 1290×2796 frame.
- **Photos.app on Mac** as fallback if you want to add captions without Figma — supports text annotations natively, less control over font but quick.

## Time estimate

Realistic budget for the full set: about 60-90 minutes once your real fleet is connected. ~10 min per frame for capture + annotation + export, plus 15 min of cleanup at the end.
