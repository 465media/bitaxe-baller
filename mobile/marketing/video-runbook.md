# Video runbook — the easy way

The other doc (`video-script.md`) is the full version. This one is the no-edit-needed path: record one continuous 23-second take in the iPhone Simulator, drop in the end card, done. Total time: 30–60 min.

## Tools you need (already installed)

- **Xcode** with iPhone 15 Pro Max Simulator (you already have this)
- **Mac Photos.app** (built into macOS, trims video without re-encoding)
- That's it. No iMovie, no Figma, no CapCut.

## Step 1 — get the Simulator ready (5 min)

1. Open Xcode → run the app on **iPhone 15 Pro Max** simulator (Cmd+R)
2. Have your real Pro license signed in already — the app remembers it; the next launch will hit the Unlock card auto-fire
3. Cold-kill the app inside the Simulator (double-tap home button → swipe up the app card)
4. Position the Simulator window so it fills most of your screen, but keep your other monitor visible so you can read the runbook below
5. In Simulator menu: **Features → Face ID → Enrolled** (toggle on)

## Step 2 — start the recording (1 min)

1. In Simulator menu: **File → Record Screen** (Cmd+R works once focused on the Simulator)
2. Recording starts immediately at native iPhone 15 Pro Max resolution (1290×2796) — the file Apple wants
3. Now **don't touch anything for 2 seconds** so the first frame is clean

## Step 3 — the 23-second flow

Read this aloud as you go. Don't rush; calm clicks read better than fast scrolling.

| Beat | Time | What you do | What's on screen |
|---|---|---|---|
| 1 | 0:00 → 0:02 | Tap the **Bitaxe Baller icon** on the Simulator home screen | App launches |
| 2 | 0:02 → 0:05 | Wait for the Unlock card → in Simulator menu: **Features → Face ID → Matching Face** | Face ID prompt fires, succeeds, fleet view fades in |
| 3 | 0:05 → 0:09 | Sit on the fleet view for 4 seconds | Summary numbers visible (total hashrate, online count, etc.) |
| 4 | 0:09 → 0:11 | Click a device card (any one — pick a good one with green border) | Detail view loads |
| 5 | 0:11 → 0:14 | Scroll down a tiny amount so the recent hashrate chart is visible | The chart line is the hero |
| 6 | 0:14 → 0:17 | Scroll down again to the long-term history section | Pro chip + 24h chart visible |
| 7 | 0:17 → 0:19 | Click **7d** in the range row | Chart redraws to 7-day bucket |
| 8 | 0:19 → 0:20 | Click **30d** | Chart redraws to 30-day bucket |
| 9 | 0:20 → 0:23 | Sit on the 30d view for 3 seconds | Final beat with chart visible |

## Step 4 — stop + trim (5 min)

1. Click the **stop recording button** in the Simulator's title bar (or Cmd+R again)
2. The file saves to your Desktop as `Simulator Screen Recording … .mp4`
3. Open it in **Photos.app**:
   - Drag the .mp4 onto Photos.app's icon, OR
   - Right-click → Open With → Photos
4. **Trim** with the slider at the bottom — if your take was clean, trim to exactly 25 seconds (23 sec of app + 2 sec of end card you'll add)
5. **Export** at the same resolution (Photos → File → Export → choose "Original")

## Step 5 — add the end card (10 min, OPTIONAL)

The simplest approach: don't bother. Apple does NOT require an end card, and your 23-second recording without one is perfectly valid. Skip to step 6.

If you really want one (it does help conversion), the easiest tools:

**Option A: macOS Photos (built-in, basic)**
- Open the trimmed video in Photos
- Click the share button → **Save Video** to a known location
- You can't add the end card from Photos alone — needs option B

**Option B: iMovie (free, ~10 min)**
- Open iMovie → New Movie
- Drag in the trimmed video → drag `end-card.png` onto the end of the timeline
- Make the still 3 seconds long (right-click the photo segment → Show Adjustments → 3 sec)
- Share → File → Export (use "1080p HD" preset; quality "High")
- Total runtime: 26 seconds, perfectly within Apple's 15–30 sec window

## Step 6 — verify before upload

Apple rejects videos for:
- ❌ Voiceover or narration (you weren't talking, so fine)
- ❌ First frame is a logo or splash screen — make sure the FIRST FRAME of your trimmed video shows app UI, not the home screen icon. If it does, trim 0.5 sec from the start.
- ❌ Marketing text overlays ("Best app of 2026") — n/a, we have none
- ❌ Pricing claims — n/a
- ❌ Showing non-iOS (Mac or Android) — n/a

## Common screwups + fixes

- **Recording is way longer than 30 sec** → Photos trim handles it. Aim for 23–26 sec.
- **Face ID prompt didn't fire** → kill the app, restart, **Features → Face ID → Enrolled** must already be on BEFORE you launch. Then re-record from beat 1.
- **Charts are empty / show "—"** → your local desktop app isn't connected to relay, OR you haven't been Pro long enough for the 7d/30d ranges to have data. Workaround: stay on 24h for the entire history beat (no need to click 7d / 30d).
- **Recording is jerky** → close other apps using the GPU (Xcode itself if possible — but the Simulator needs Xcode running). Restart the Mac if it gets bad.

## Time estimate

- Without end card: **20–30 min total**
- With end card (iMovie): **45–60 min total**

## When you're done

Upload the .mp4 to App Store Connect under your app's listing → Media tab → Drag-drop the video into the iPhone 6.7" slot. Apple processes it for ~10–20 min before it shows in the listing preview.

For Google Play, upload the same .mp4 to YouTube (unlisted is fine), then paste the YouTube URL into the Play Console listing.
