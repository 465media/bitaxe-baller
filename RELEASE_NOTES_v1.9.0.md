# Bitaxe Baller v1.9.0

**Headline: remote access.** Pro users can now reach their Bitaxe Baller dashboard from anywhere — no port forwarding, no VPN, no fixed IP. The desktop app opens an outbound WebSocket to `relay.bitaxeballer.com`; remote browsers hit the same relay and get routed to your local app. Read-only home view for v0; tuning over relay coming in v1.9.x.

## New (Pro)

### Remote access via relay.bitaxeballer.com

- Outbound-only — the desktop app connects out, nothing inbound to your network.
- License key is the credential. Activating Pro automatically unlocks remote access; you opt in with a one-click toggle in the Pro modal.
- Session tokens are HMAC-SHA256 signed by a relay-side secret, 24h TTL. The token is opaque to your browser — no license key sits in URLs or `localStorage` beyond the token itself.
- All safety bounds (frequency 400–700 MHz, voltage 1000–1300 mV, etc.) apply identically over the relay — the security perimeter is the relay's license check + path/method allow-list + message-size cap. Local app stays the source of truth for everything.
- Disable remote access in the Pro modal at any time and the connection drops immediately.

To use it:
1. Update to v1.9.0.
2. Open the Pro modal (the ★ button in the header).
3. In the **Remote access** section, click **Turn on**.
4. From anywhere, visit https://relay.bitaxeballer.com/, sign in with your license key, and you're in.

The remote dashboard in v1.9.0 is intentionally minimal: device cards with summary stats (total hashrate, total power, avg efficiency, online/total), live polling, severity-colored borders. Tuning + scanning + adding devices stay on the LAN dashboard for v1.9.0 — those land in a follow-up.

### Bonus: the `/api/remote/enable` URL-preservation fix

The plumbing under the hood also fixed a small bug where toggling remote access off → on used to silently reset the relay URL to the production default, clobbering any custom URL you'd configured. Toggling now preserves whatever URL you'd set.

## Free tier — unchanged

The LAN dashboard, mDNS publishing, network scanner, recommendation engine, tuning controls, fan control, light/dark theme, daily CSV logs — all unchanged. Remote access is the only Pro-gated thing in this release.

## Upgrading

- **Mac (Pro users):** v1.8.2's auto-update infrastructure delivers v1.9.0 in-place. You'll get the banner on next launch, click "install & restart," and you're on the new version with auto-update enabled going forward.
- **Mac (free users):** same as before — banner with a one-click download link.
- **Windows:** same banner-with-download flow as v1.8.2. Authenticode-signed by Azure Trusted Signing; SmartScreen shouldn't bother you after the first run of v1.8.2.
- **From source:** `git pull && pip install -r requirements.txt` (picks up the new `websockets>=12.0` dep) and re-run.

## Under the hood — for developers

- New `relay/` directory: FastAPI WebSocket router, HMAC session tokens, in-memory registry, idle-disconnect loop. ~825 LOC, no external state.
- New `relay_client.py`: app-side connector. Outbound WSS, loopback HTTP dispatch to the existing Flask `/api/*` routes — Flask sees a remote request as identical to a LAN browser tab.
- New `relay/web/index.html`: single-file SPA hosted by the relay. Login form, polling dashboard, no external assets.
- New `relay/deploy/`: systemd unit + nginx config + env template + deployment runbook for self-hosters.
- WebSocket protocol preserves client-allocated request IDs end-to-end, so concurrent in-flight requests work cleanly.

Full PR: https://github.com/465media/bitaxe-baller/pull/2

Scoping doc: https://www.notion.so/3629cef8928f8192bd6bfc2d81ddaaa7

## Compatibility

- The desktop app's polling, tuning, and config persistence are unchanged from v1.8.x. Existing `config.json` files keep working — `remote_access` is just a new optional key.
- The relay validates against the existing license server at `bitaxeballer.com/api/license` (the v1.8.3 pivot), so no new credentials or accounts are needed.
- The auto-update appcast format is unchanged; v1.8.2 Pro users get this release through Sparkle as expected.

## Known limitations in v1.9.0

- Remote SPA is read-only: no add-device, no tuning, no per-device detail page. Coming in v1.9.x.
- Single-relay-instance design — if you self-host, plan for a brief outage during restarts. State is in-memory and clients reconnect automatically.
- The relay's idle-disconnect closes app sockets after 1h of no client traffic to save bandwidth; the desktop app reconnects automatically on the next session.

---

Thanks to everyone who asked for this on Discord and Facebook. Remote-access was the #1 backlog item — and it's the same pipe mobile apps will eventually run over.
