# Pro tier roadmap

Running list of features destined for the paid tier. The free tier stays fully functional; Pro is additive — bulk operations, automation, alerts, persistent history.

This doc is the source of truth as features get scoped, designed, or implemented. Add items as they come up. Strike them through when shipped.

---

## Shipped in v1.8.0 (2026-05-14)

### ~~Bulk tuning across selected devices~~ ✅
- Multi-select device cards on the home page (per-card checkbox, "select all" toolbar).
- Apply a preset or custom freq / voltage / fan to every selected device in one click.
- `POST /api/devices/bulk_tune` — server-side bounded, parallel fan-out via ThreadPoolExecutor, max 64 IPs.

### ~~Auto-tune sweeps with HW-error guardrails~~ ✅ (v1: frequency only)
- Frequency-only probe (voltage is **not** touched during a sweep — v1 safety scope).
- +25 MHz per 90 s observation window, capped at 8 steps.
- Hard abort + baseline restore at VR ≥ 65 °C, ASIC ≥ 65 °C, or HW error rate ≥ 5 %.
- Records the highest stable frequency it found and applies it.
- **v2 enhancement (not yet done):** voltage probing for additional headroom. Frequency-only is the conservative first pass; voltage tuning lands once we've seen freq-sweep behave in the wild.

### ~~Long-term history~~ ✅
- Persistent local SQLite (`history.db` in user data dir), 90-day retention.
- Bucketed read endpoint with 24h / 7d / 30d / 90d ranges.
- Chart UI added to every device detail page; Pro-gated (free users see a teaser).
- Free tier keeps its in-memory 1h rolling window + daily CSV logs unchanged.

### ~~Discord alerts~~ ✅
- Three triggers: offline > N min, VR temp ≥ X °C, ASIC temp ≥ X °C.
- 30-minute cooldown per (device, trigger) pair.
- Test button on the config UI.
- **Still to do in v1.8.x:** SMTP / email channel, Telegram channel, HW-error-rate-sustained trigger.

### ~~License activation + Pro modal~~ ✅
- Activation via Lemon Squeezy customer portal API. Five machine activations per license; deactivate to free a slot.
- Dev override (`BITAXE_BALLER_DEV_PRO=1`) for development work.

---

## Shipped in v1.22.0 (2026-08-27)

### ~~Card drag-to-reorder~~ ✅
- *(Nate request, 2026-08-27.)* Drag device cards on the home dashboard into whatever order you want, within each coin section. Grip handle on every card, next to the bulk-tuning checkbox; Pointer Events (not HTML5 drag-and-drop, so it works on touch too).
- `POST /api/devices/reorder` persists the order to `config.json` — Pro-gated write, but the order itself isn't tied to license state, so it doesn't revert if Pro lapses.
- **Mobile lines up for free.** The iOS/Android app renders `/api/devices` straight through with no client-side sort, so a Pro user's custom order shows up on their phone automatically over the relay — no mobile app update needed.

---

## Still in the v1.0 Pro launch list (not yet shipped)

### Auto-updates (in-place / "Chrome-style")
- Real Sparkle (Mac) + WinSparkle (Windows) integration. App downloads new version in the background, prompts on next launch, replaces itself, restarts.
- Free tier ships the lighter [v1.7 update banner](app.py) — *tells* users when an update exists, click to manually re-install. Pro tier ships the real auto-install flow.
- **Hard dependency:** Windows code-signing certificate ($120-400/yr). Without it every auto-update fires SmartScreen, defeating the point.
- **Hard dependency:** appcast.xml hosted on bitaxeballer.com with Ed25519 signatures on each release. Update channel = remote code execution if signatures aren't enforced.
- Failure recovery: if the new binary crashes on launch, roll back to the previous version automatically. Don't brick paid users.

---

## Ideas under consideration

Validate before scoping into the v1.0 list. Some may belong to a later paid tier or stay free.

- **Fleet-across-networks** — cloud relay so users can monitor remote sites (vacation home, friend's basement, rented colo space) without VPN setup. Big infrastructure lift; only do it if there's clear demand. *(Technical prerequisite for the mobile app below — same architecture solves both.)*
- **Mobile apps (iOS + Android)** — native or React Native dashboard for monitoring your fleet from your phone, including when you're off your home network. The hard part isn't the UI (the same JSON API the dashboard already exposes powers everything); it's getting the phone to talk to Bitaxes that live on the user's home LAN. Depends on "Fleet-across-networks" — once that cloud relay exists, the mobile app is mostly UI work. Realistic order: ship the relay first, mobile is a fast follow.
- **API access** — read-only HTTP API key so users can pipe metrics into Grafana / Datadog / personal dashboards. Probably bundled into Pro.
- **Custom rule engine** — user-defined alerts: "if VR temp > 75 °C AND hashrate < 1.1 TH/s for 5 min then page me." Useful but requires careful UI.
- **Pool fee optimization** — recommend pool switches based on observed payout rates. Requires multi-pool data collection; tricky.
- **Pool profiles — manual switch (free tier)** ✅ shipped 2026-06-09 (committed to main, ships with next release). Save named pool configs ("BTC — Public Pool", "BCH — letsmine.it", etc.) and apply with one click to any device. Profile storage in `config.json` under `pool_profiles`, endpoints under `/api/pool-profiles/*`, UI panel on the device detail page above the manual pool form. Passwords not stored in profiles (worker passwords stay write-only — user types them per-device if needed). Time-of-day **scheduler** (the Pro half of the original idea) is still parked — that one is where the multi-device + timezone + retry-on-offline complexity lives.
- **Pool scheduler (Pro)** — *(Facebook user request, 2026-05-16; manual-switch half shipped 2026-06-09.)* Time-based pool switching on top of the manual profiles MVP. Per-device schedule like "BTC profile from 8am to 11pm local, BCH profile from 11pm to 8am, weekends always BTC". Open questions: day-of-week granularity vs. flat daily, timezone handling (system local vs. user-configurable), retry behavior when a device is offline at switch time, whether the cron lives in-process (simpler, dies with the app) or as a separate systemd timer (more reliable but adds install complexity). Builds on the shipped `/api/pool-profiles/<id>/apply` endpoint — just adds a schedule field per device + a background ticker. ~half day end-to-end on top of MVP.
- **Power scheduler + standby (Pro)** — *(Pro user request, 2026-06-15.)* Two halves of the same feature: (1) **standby** — one-click "park" a device, taking it out of active hashing without losing config. AxeOS has no native pause verb, so the practical implementation is "drop to floor frequency + floor voltage + autofan on, remember prior state, restore on wake." Effectively a hibernate / wake toggle. (2) **time-of-day schedule** per device so users on peak/off-peak electricity rates can run Aggressive overnight and Standby during peak hours. Open questions: does standby mean "minimum-hashing-floor" (still earning a trickle, still warm — what we can actually do today) or "true zero-mining off" (AxeOS would need a real off mode — doesn't have one); per-device vs. fleet-wide schedules; how to coexist with the parked Pool scheduler above (same background ticker, same timezone model, same offline-retry behavior — they should ship as one engine with two outputs). Probably belongs as **one combined "Schedule" feature** that switches between {pool profile, tuning preset / standby} per per-device schedule. Strong fit for fleet operators; standby alone is also valuable to single-device users with variable-rate utility plans. ~1 day end-to-end if built on top of the Pool scheduler infrastructure; ~1.5–2 days standalone.
- **Bench / burn-in mode** — guided multi-hour stability test that varies voltage/frequency systematically and emits a report. Adjacent to auto-tune.
- **Per-user / per-chip baseline drift detection** — "your Gamma is hashing 4% below its 30-day average" alerts. Builds on long-term history.
- **Live share feed + top-25 best-shares leaderboard** — *(Competitive parity, 2026-05-21. MinerWatch shipped this and it's reportedly heading into the upstream Bitaxe OS roadmap.)* Real-time view of accepted shares being submitted to the pool, plus a leaderboard across the fleet ranking each device by its best share difficulty (career and session). Firmware already exposes `bestDiff`, `bestSessionDiff`, `sharesAccepted`, `sharesRejected` and we capture them on every 5s poll — this is largely UI: a streaming "shares" tab on the device page, plus a top-shares widget on the home page. ~1–2 days. Free-tier candidate: visible, easy to grasp, high goodwill — Pro hook only if we layer cross-fleet historical analytics on top.
- **Umbrel community app store listing** — *(2026-05-26.)* Package Bitaxe Baller as an Umbrel app and publish via a `465media/umbrel-bitaxe-baller` community repo so Umbrel users can install with one click ("Add Community App Store" → paste repo URL → install). Massive overlap between Bitaxe owners and Umbrel users (both run on the same "I want sovereign-tech in my closet" instinct). Lower barrier than the Mac/Windows DMG / .exe, *and* it complements the relay architecture nicely — if the desktop app runs 24/7 on an Umbrel box instead of a sleeping Mac, the WebSocket relay stays connected without anyone thinking about it. Technical work: Dockerfile (we don't have one yet — the app currently runs from `python app.py`), `umbrel-app.yml` manifest with port 5050 mapped, icon + screenshots, and a community-repo fork-or-host decision. Free-tier installable (Pro features still gate behind license key inside the app). ~2–3 days. Strong fit for Q3.
- **Thermal Guardian** — *(2026-06-09, scoped but not built.)* Persistent background watchdog that keeps each device inside a user-set thermal envelope by adjusting frequency (and optionally voltage) in real time. Differs from auto-tune: auto-tune is a one-shot ceiling finder that stops once it finds the best stable freq; Guardian is a continuous holder that reacts to ambient changes (summer/winter, dusty fan, varying room temp). State machine: ENABLED_HOLD → STEPPED_DOWN_n (after sustained ceiling crossing for ≥30s) → LOCKED (if at hard floor and still hot) → RECOVERY (when temp drops back for ≥90s). Snapshot user's freq+volt on enable; never step UP past snapshot. Hard floor at stock 525 MHz / 1150 mV. Per-device controls: target ASIC ceiling (default 60°C), target VR ceiling (default 75°C — separate per the recent split-threshold fix), opt-in voltage co-tune, min frequency floor. Free tier sees toggle locked behind Pro modal. Differentiator vs MinerWatch Guardian: split ASIC/VR thresholds, opt-in voltage co-tune, fleet-level rollup. ~300 lines / 1 dev-day. Parked because no user signal yet — recommendation engine already surfaces the action with one-click apply, AxeOS overheat_mode is the firmware-level last resort; Guardian's only marginal value is the 30s reaction time when user isn't watching the dashboard. Revisit if a Reddit/Discord user asks "does Baller throttle automatically?" twice in a month, or if heatwave reports come in.
- **Fleet auto-tune campaign** — *(2026-06-08.)* Run auto-tune across multiple selected devices sequentially (never parallel — same thermal-contention reasoning as bulk flash), with a fleet-level rollup view: per-device status, ETA, and a single "campaign complete" event. Useful for the 5+ device user who just got a new batch and wants to dial them all in without babysitting one at a time. Builds on the existing single-device auto-tune machinery in `app.py:1099+`; mostly a wrapper + scheduler + UI rollup. ~1 day. Pro-gated candidate — fleet operators are the audience.
- **Chip-level normalized comparison** — *(2026-06-08.)* GH/s per Watt per MHz across the fleet, surfaced as a leaderboard / scatter on the dashboard. Tells the user which physical board chip is silicon-lottery best/worst — useful when buying new ones, returning a dud, or deciding which device to push hardest. All inputs (`hashRate`, `power`, `frequency`) already polled every 5s; this is a calculation + visualization. Free-tier candidate, low effort (~half day).
- ~~**Fleet ROI overlay**~~ ✅ shipped 2026-06-09 in v1.15.0. Client-side aggregation of per-device blockProbability into a fleet-level "expected days to next block" headline, per chain. Renders one tile per active chain below the existing summary HUD.
- **Bulk firmware + AxeOS flasher** — *(2026-05-27.)* In-app fleet flasher that pushes AxeOS WWW updates (Phase 1) and firmware updates (Phase 2) across the user's whole fleet from one button. AxeOS already exposes `POST /api/system/OTA` (firmware) and `POST /api/system/OTAWWW` (web UI) over LAN; this is the same trust boundary we already use for tuning. Phase 1 = WWW-only (low risk — WWW failure doesn't stop mining); Phase 2 = firmware (~30 days after Phase 1, strict chip-model verification, sequential only, multi-step "type FLASH" confirmation, pre-flight uptime + error-rate checks); Phase 3 = arbitrary user-uploaded binaries (only if demand emerges). Binaries fetched from official `osmu/bitaxe` GitHub releases by tag with SHA256 verification — no untrusted-blob path. Pro-gated (fleet operators are the audience). Sequential flashing only, never parallel — one power blip across a parallel flash could brick the whole fleet. Auto-skip offline / `severity=crit` devices. Real differentiator: MinerWatch doesn't have this, the official AxeOS doesn't have a fleet-flash tool. Pain point is real at 5+ devices. ~3 days Phase 1, ~2 days Phase 2. Risk surface is real but bounded by phasing + safety checks. Memory: `reference_bulk-flasher-idea.md`.

---

## Explicitly NOT Pro (stays free)

These are foundational UX or already promised publicly. Don't gate them.

- All current v1.6+ features: live polling, tooltips, manual tuning, fan control, network scanner, pool config, light/dark theme, recommendation engine, mDNS publishing, CSV logging.
- **Multi-model support** — Gamma (BM1370) is shipping; Supra (BM1368) + Ultra (BM1366) presets are promised in the changelog as a free addition.
- **Update notification banner** (v1.7) — the awareness-layer auto-update lite. Pro gets the real auto-install on top.
- The dashboard itself, the binary, the disclaimer, the open-source code. The app is free.

---

## Pricing / packaging (placeholder — needs decision)

Not committed yet. Options to think through:

- One-time license + N years of updates (e.g. $39 one-time, includes a year of updates and Pro features stay enabled forever)
- Monthly subscription ($5/mo or $50/yr)
- Per-device licensing — N devices per license, more = pay more
- "Donate-supported with a Pro unlock code" — for the community-vibe play

Independent of pricing model, license validation will live in a `/api/license/*` route group on bitaxeballer.com backed by Stripe. The desktop app calls home on launch + once a week to validate; offline grace period (~14 days) so users on bad networks don't get locked out.
