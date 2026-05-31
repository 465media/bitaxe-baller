# iOS v1.1 — Pairing flow + free-tier relay + IAP unlock

## Why

Apple rejected iOS v1.0 build 2 under guideline 3.1.1 (Business: Payments).
The license-key sign-in screen + Pro-gated relay = "app accesses paid
content purchased outside the app". Reader-app exemption (3.1.3(b)) does
not apply unless IAP is also offered.

Strategy (per Nathan, 2026-05-30): make iOS free + functional for everyone
via QR pairing with desktop. Free desktop users get 1-miner stream; Pro
desktops stream the full fleet. Add an IAP unlock (`$4.99/mo` or
`$39/yr`) as a third sign-in path — satisfies 3.1.3(b) "must also be
available via IAP" clause. Most Pro customers will still enter their
existing license key (free path) or upgrade on web ($29/yr cheaper); the
IAP exists primarily as compliance theater that nets ~$50/yr if anyone
takes it.

## Architecture changes across 4 components

### 1. site-server (`bitaxe-baller-site/server/index.js`)

**New tables (idempotent migrations):**

```sql
CREATE TABLE IF NOT EXISTS pair_tokens (
  token              TEXT PRIMARY KEY,                -- random 22-char base64url
  install_uuid       TEXT NOT NULL,                   -- desktop's install_uuid
  license_key_hash   TEXT,                            -- nullable; Pro only
  tier               TEXT NOT NULL CHECK (tier IN ('free','pro')),
  display_label      TEXT,                            -- "Nathan's Mac mini" etc.
  created_at         INTEGER NOT NULL,                -- unix sec
  expires_at         INTEGER NOT NULL,                -- created_at + 60s
  redeemed_at        INTEGER,                         -- null until iOS exchanges it
  redeemed_device_id TEXT                             -- the device_token row that won it
);

CREATE TABLE IF NOT EXISTS device_tokens (
  id                  TEXT PRIMARY KEY,               -- random 26-char base64url
  install_uuid_paired TEXT NOT NULL,                  -- the desktop this device pairs to
  tier_at_pair        TEXT NOT NULL,                  -- 'free' or 'pro' snapshot
  device_label        TEXT,                           -- e.g. "iPhone 16 Pro"
  platform            TEXT,                           -- 'ios' or 'android'
  created_at          INTEGER NOT NULL,
  last_seen_at        INTEGER,                        -- updated each /pair-status hit
  revoked_at          INTEGER                         -- null until user revokes from desktop
);
CREATE INDEX IF NOT EXISTS idx_devtok_install ON device_tokens(install_uuid_paired);
```

**New endpoints:**

```
POST /api/relay/pair-init
  body: { install_uuid, license_key?, display_label? }
  → { pair_token, expires_in: 60, pair_url: "https://..." }
  Desktop hits this when user clicks "Pair iPhone". License-key validated
  if present (sets tier=pro), else tier=free. Token written to pair_tokens.

POST /api/relay/pair-redeem
  body: { pair_token, device_label?, platform: 'ios'|'android' }
  → { device_token, install_uuid_paired, tier }
  iOS hits this after scanning QR. Validates token unredeemed + not
  expired. Creates device_tokens row. Marks pair_token.redeemed_at.
  Token is a JWT-shaped (or plain) opaque ID; signature unnecessary if
  the relay does DB lookup each time (acceptable for our scale).

GET  /api/relay/device-info
  Authorization: Bearer <device_token>
  → { install_uuid_paired, tier_at_pair, label, last_seen_at }
  Relay calls this on iOS WS connect to know who to route to + tier.

POST /api/relay/device-revoke
  body: { install_uuid, device_token_id, license_key? }
  → { ok: true }
  Desktop calls this from Pro modal (or new "Paired Devices" panel) to
  revoke a paired iPhone.

GET  /api/relay/devices
  body: { install_uuid, license_key? }
  → { devices: [{id, label, platform, created_at, last_seen_at}, ...] }
  Desktop lists currently-paired iOS/Android devices.
```

**Feature flag:** `BBR_RELAY_PAIRING_ENABLED=1` — env var on the VPS. When
unset, all four endpoints return 503. Lets us deploy the code without
exposing it until the relay + desktop UI catch up.

### 2. relay (`/opt/bitaxe-baller-relay/main.py`)

**Changes to `/ws/app` (desktop side):**

Add a new query param: `install_uuid`. If both `key` (license) and
`install_uuid` are provided, use license (existing Pro path). If only
`install_uuid`, accept as free-tier — skip the licensing.validate() call,
register AppConn with `tier='free'`. AppConn dataclass grows `tier` and
`install_uuid` fields.

**Changes to iOS auth path:**

Currently iOS does `POST /login` with license key → gets session token →
opens `/ws/browser?token=...`. Add an alternative:

```
WebSocket /ws/browser
  Headers: Authorization: Bearer <device_token>
  (existing: ?token=<session_token>)
```

On device_token connect: relay calls `site.bitaxeballer.com/api/relay/device-info`
→ gets install_uuid + tier → finds matching AppConn by install_uuid → routes.

**Tier-limit enforcement:**

When AppConn streams device data to its browser/iOS sockets, if
`AppConn.tier == 'free'`:
- Strip all but the first device (by config order) from `/api/devices`
  responses before forwarding
- Reject `/api/device/<ip>` for any IP that isn't the first device
- Reject all mutation endpoints (already true for browser side per
  protocol allow-list)

### 3. desktop (`bitaxe-baller/templates/dashboard.html` + `static/common.js`)

**Pro modal: new "Pair iPhone" section** (visible to free + Pro):

```
Paired devices
─────────────────────────────
[ Pair a new iPhone or Android ]   ← button

(if devices exist:)
iPhone 16 Pro       paired 2 days ago    [ revoke ]
Pixel 8a            paired 5 hours ago   [ revoke ]
```

Button click:
1. POST `/api/relay/pair-init` with current install_uuid + license_key
2. Modal shows QR code (using `qrcode-svg` or `qrcode.js` — tiny lib)
3. QR encodes: `bitaxeballer://pair?token=ABC123` (or just `ABC123`)
4. 60s countdown; auto-dismiss + grey out when expired
5. On successful redemption (poll `/api/relay/pair-status?token=...`),
   modal shows green "✓ paired with iPhone" and refreshes the devices list

**Desktop also needs:** when relay is enabled, send `install_uuid` along
with `license_key` on WS connect to relay (so free-tier desktops can
connect).

### 4. iOS (`mobile/www/index.html` + `mobile/www/plugins.js`)

**Replace license-key login screen with pairing screen:**

```
[before: license-key input]
[after:]
  Pair this iPhone with your desktop install.

  1. Open Bitaxe Baller on your Mac, Windows, or Umbrel
  2. Click your profile → Pair iPhone
  3. Point your phone at the QR code below

  [ Scan QR code ]   ← button → triggers Capacitor barcode scanner
```

Scanner reads `bitaxeballer://pair?token=ABC123`, extracts token, calls
`POST /api/relay/pair-redeem`, stores returned `device_token` in keychain.

WebSocket connect changes to use `Authorization: Bearer <device_token>`.

**Unlock-more screen** (when relay reports tier=free):

```
You're viewing 1 of N miners
─────────────────────────────
See your whole fleet on iPhone:

[ Have a Pro license? Enter key ]
[ Subscribe — $4.99/mo or $39/yr ]
```

The "Enter key" path: prompts for license key, calls existing
`/api/license/activate` flow (Pro upgrade), then re-pairs.

The "Subscribe" path: triggers Apple IAP via RevenueCat. On purchase
success, backend issues a Pro license key tied to the user's email, then
re-pairs with that license.

## Phasing

- **Phase 1a** (tonight): site-server schema + endpoints behind feature flag
- **Phase 1b** (tomorrow): relay changes (free-tier WS + tier-limit) + desktop "Pair iPhone" UI
- **Phase 1c** (tomorrow PM): iOS QR scanner + paired-auth replaces license-key screen
- **Phase 2** (next): IAP integration (RevenueCat + StoreKit), unlock-more flow

## Apple resubmission strategy

Submit after Phase 1c. App Review notes:
> "Bitaxe Baller iOS is a free companion app for our open-source desktop
> tool. Pair via QR code from the desktop install. The demo desktop we've
> set up (credentials below) has multiple miners; you'll see them all
> after pairing. Pro/free tier exists on the desktop side; the iOS app
> shows whatever the paired desktop streams. No payment required to
> use the iOS app."

If Apple rejects citing the tier limit ("you stream more devices for Pro
users"), ship Phase 2 (IAP) and resubmit with both paths visible.
