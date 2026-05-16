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
- **Pool profiles + scheduler** — *(Facebook user request, 2026-05-16.)* Save named pool configs ("BTC — Ocean", "BCH — CKPool", etc.) and switch between them either with a one-click toggle or on a time-based schedule (e.g. 15 h/day BTC, 9 h/day BCH). Bitaxe already supports primary + fallback, so the device-side plumbing exists — this is profile storage, a background scheduler thread, and UI. Open questions: day-of-week schedules vs. flat daily, timezone handling, retry behavior when a device is offline at switch time, and whether the manual toggle / profile save is free-tier (low effort, high goodwill) with only the scheduler gated to Pro. ~1–2 days end-to-end once scoped. Natural first concrete Pro hook beyond auto-update once the LS gate goes live.
- **Bench / burn-in mode** — guided multi-hour stability test that varies voltage/frequency systematically and emits a report. Adjacent to auto-tune.
- **Per-user / per-chip baseline drift detection** — "your Gamma is hashing 4% below its 30-day average" alerts. Builds on long-term history.
- **Smart-plug wall-power monitoring (Matter API)** — *(Competitive watch 2026-05-16: Hashboard ships this as their headline differentiator.)* Pair a Matter-compatible smart plug (TP-Link Tapo P125M, etc.) with each Bitaxe to read true wall-level power draw instead of trusting the firmware's self-reported number. Useful for: (a) accurate J/TH efficiency calcs (firmware-reported is consistently 5–10% optimistic), (b) detecting PSU losses, (c) integrating with home-automation routines. Implementation: Matter library or vendor-specific API; pair plug + miner at the device-detail page; show wall vs. reported delta. Probably Pro-gated. Adjacent to "Per-user baseline drift" — same data pipeline.

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
