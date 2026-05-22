# App preview video — script + storyboard

Both stores accept short video previews. They autoplay (muted) at the top of the listing, so they're high-leverage: a good 25-second clip can convert as well as a full-page description.

## Specs

| Platform | Format | Resolution (portrait) | Length | Audio |
|---|---|---|---|---|
| Apple | H.264 MOV or MP4 | **1080×1920** (or 886×1920 for older sizes) | 15-30 sec | Allowed but autoplay-muted; on-screen UX only — no voiceover |
| Google | MP4 link via YouTube | any | up to 2 min (we'll do 30 sec) | Allowed, plays with sound if user unmutes |

Apple's stricter — they reject videos with voiceover, marketing-y intros, or anything that isn't the actual app UI on a real device. Google is lenient. We'll cut for Apple and reuse the same MP4 on YouTube for Google.

## 30-second target

Most app preview videos are too long. People will not watch past 5 seconds without an immediate hook. Below is a 28-second cut with five beats, each 4-7 seconds.

## Storyboard

| Time | What's on screen | What the viewer learns |
|---|---|---|
| 0:00–0:04 | iPhone home screen → tap Bitaxe Baller icon → Face ID prompt appears, looks at face, succeeds, fleet view fades in | "This is a real native iOS app. Face ID gated." |
| 0:04–0:09 | Fleet view: 3+ device cards, summary stats animating in. Total hashrate counts up. | "Multiple miners. Live data. At-a-glance health." |
| 0:09–0:14 | Tap into one device card → smooth transition → detail view loads. Live metrics fill in. | "Drill in for everything." |
| 0:14–0:21 | Scroll down to the long-term history section. Tap **24h → 7d → 30d**, watching the chart redraw each time. | "90 days of history. Performance over time." |
| 0:21–0:25 | Tap back → swipe to lock the phone → unlock it again with Face ID (different angle, very brief) → fleet still there | "Reach it anywhere. Lock it down." |
| 0:25–0:28 | End card: BITAXE.BALLER wordmark, "Free with Pro · bitaxeballer.com" | CTA |

## Capture flow

1. **Set up**: phone on a tripod or leaning stable, in landscape orientation for shooting (we'll rotate in edit). Or use QuickTime screen recording from a connected iPhone — cleanest result, no need for camera.
2. **QuickTime screen recording from iPhone (recommended)**:
   - Plug iPhone into Mac
   - QuickTime Player → File → New Movie Recording
   - Click the dropdown arrow next to the record button → select your iPhone as the source
   - Record button → perform the storyboard on the phone → stop
   - Output: clean 1170×2532 (iPhone 14 native) MP4 with no camera artifacts
3. **Edit in iMovie or CapCut** (free):
   - Trim to the 28-second beats
   - Add the end card as a 3-second still (1080×1920 background image with the logo + CTA)
   - Export at **1080×1920, H.264, MP4**
4. **Inspect the export** before upload:
   - First frame should already show useful product UI, NOT a logo or splash screen (Apple specifically calls this out)
   - No system camera UI, no controls visible, no notifications popping in

## End card

Single-frame image to drop into the last 3 seconds:

```
[ pickaxe icon, accent green ]

BITAXE . BALLER

Free with Pro · bitaxeballer.com
```

Dark background `#0a0d0c`. Wordmark in JetBrains Mono bold, accent green. Subtitle in JetBrains Mono regular, dim gray (`#5a6b66`).

I can generate this end-card PNG as part of the marketing assets when we get to it — just say the word.

## Things Apple will reject the video for

- Voiceover or narration (any human speech). Music is fine if it's autoplay-muted, since they always autoplay muted anyway.
- Marketing text overlays that aren't system UI ("Get yours now!" "Best app of 2026").
- Anything that doesn't appear in the actual app (mockup screens, promotional graphics over the device, etc.).
- Showing a Bitaxe Baller install on a non-iOS device (no Android, no Mac).
- Pricing claims ($29/year shown in the video gets flagged because Apple wants pricing in their own surfaces only).

## Things that are fine

- Subtle background music
- Cinematic camera motion (zoom into the device, slow pan across the fleet view)
- Slow-motion of the Face ID prompt firing
- Two short cuts between the home view and the detail view
- The end card (Apple permits the last 3-5 seconds being a logo + URL)

## Estimated time

- Setup + recording: 30-45 minutes (will need 3-4 takes to get a clean 28 seconds)
- Editing: 60-90 minutes for someone not doing this regularly
- Total: ~2 hours for v1
