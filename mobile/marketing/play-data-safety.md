# Google Play Data Safety — pre-staged answers

Paste-ready answers for the Play Console "Data safety" form. Parallel to `apple-privacy-nutrition-label.md` but adapted to Google's ontology (which is different from Apple's in subtle, annoying ways).

Source files for verification:
- `mobile/www/index.html` — single-WebView SPA
- `mobile/package.json` — only `@capacitor/preferences` + `@aparajita/capacitor-biometric-auth`
- `bitaxeballer.com/privacy.html` — canonical disclosures
- `mobile/marketing/app-store-listing.md` — has an older draft table; this file supersedes it

---

## TL;DR — click these answers, in this order

1. **Does your app collect or share any of the required user data types?** → **Yes**
2. **Is all of the user data collected by your app encrypted in transit?** → **Yes**
3. **Do you provide a way for users to request that their data is deleted?** → **Yes** (link to `https://bitaxeballer.com/privacy.html#your-rights`)
4. In the data-type matrix, tick only:
   - **Personal info → User IDs** → Collected: Yes. Shared: No. Optional: No (required for app function). Purpose: **App functionality, Account management**. Ephemeral: No.
5. Everything else → **No**.
6. **Privacy Policy URL** → `https://bitaxeballer.com/privacy.html`
7. Save. Submit.

Total clicks: ~8.

---

## Section 1 — Data collection and security (top-level)

| Question | Answer | Justification |
|---|---|---|
| Does your app collect or share any of the required user data types? | **Yes** | We collect the Pro license key (a User ID). |
| Is all of the user data collected by your app encrypted in transit? | **Yes** | All traffic is HTTPS / WSS to `relay.bitaxeballer.com`. App Transport Security equivalent on Android via network-security-config (cleartext disabled). |
| Do you provide a way for users to request that their data is deleted? | **Yes** | Privacy policy `#your-rights` section documents the process. Also: user can deactivate their license self-serve at `bitaxeballer.com/account`. |

---

## Section 2 — Data types (the big matrix)

Google groups types into categories. For each that is collected/shared, declare: collected vs shared, optional vs required, purposes, ephemeral.

### Personal info

| Sub-type | Collected? | Shared? | Optional? | Ephemeral? | Purposes | Notes |
|---|---|---|---|---|---|---|
| Name | **No** | — | — | — | — | App never collects. |
| Email address | **No** | — | — | — | — | The license server has the buyer's email, but the mobile app does not collect/persist it. // FLAG: confirm `/login` response handling doesn't persist email — see Apple notes. |
| User IDs | **Yes** | **No** | **Required** | **No** | **App functionality, Account management** | The Pro license key. Stored locally via `@capacitor/preferences` (Android EncryptedSharedPreferences). Sent to our relay for session establishment. Not shared with any third party. |
| Address | **No** | — | — | — | — | — |
| Phone number | **No** | — | — | — | — | — |
| Race and ethnicity | **No** | — | — | — | — | — |
| Political or religious beliefs | **No** | — | — | — | — | — |
| Sexual orientation | **No** | — | — | — | — | — |
| Other info | **No** | — | — | — | — | — |

### Financial info

| Sub-type | Collected? | Notes |
|---|---|---|
| User payment info | **No** | Stripe handles purchase off-app. |
| Purchase history | **No** | No IAP, no purchase history in the app. |
| Credit score | **No** | — |
| Other financial info | **No** | — |

### Health and fitness

| Sub-type | Collected? | Notes |
|---|---|---|
| Health info | **No** | — |
| Fitness info | **No** | — |

### Messages

| Sub-type | Collected? | Notes |
|---|---|---|
| Emails | **No** | — |
| SMS or MMS | **No** | — |
| Other in-app messages | **No** | — |

### Photos and videos

All **No**.

### Audio files

All **No**.

### Files and docs

| Sub-type | Collected? | Notes |
|---|---|---|
| Files and docs | **No** | — |

### Calendar

All **No**.

### Contacts

All **No**.

### Location

| Sub-type | Collected? | Notes |
|---|---|---|
| Approximate location | **No** | — |
| Precise location | **No** | — |

### Web browsing

| Sub-type | Collected? | Notes |
|---|---|---|
| Web browsing history | **No** | The WebView only loads our own `relay.bitaxeballer.com` content; we do not log it. |

### App activity

| Sub-type | Collected? | Notes |
|---|---|---|
| App interactions | **No** | No analytics SDK. No event tracking. |
| In-app search history | **No** | No in-app search. |
| Installed apps | **No** | — |
| Other user-generated content | **No** | — |
| Other actions | **No** | — |

### App info and performance

| Sub-type | Collected? | Notes |
|---|---|---|
| Crash logs | **No** | Capacitor ships no crash reporter. We have not integrated Firebase Crashlytics, Sentry, or any equivalent. Google Play Console's automatic crash collection goes to Google for app-quality vitals; per Play's guidance, automatic Play Console crash data does NOT need to be declared here. |
| Diagnostics | **No** | No performance/diagnostics SDK. |
| Other app performance data | **No** | — |

### Device or other IDs

| Sub-type | Collected? | Notes |
|---|---|---|
| Device or other IDs | **No** | We do not read Advertising ID, Android ID, IMEI, MAC, or any hardware identifier. The license key is a User ID, not a Device ID. |

---

## Section 3 — Security practices

| Question | Answer | Justification |
|---|---|---|
| Is all of the user data collected by your app encrypted in transit? | **Yes** | TLS for HTTPS POST `/login` and WSS for the relay socket. No cleartext. |
| Do you provide a way for users to request that their data be deleted? | **Yes** | Privacy policy outlines email/contact-form deletion request. License can also be self-deactivated at `bitaxeballer.com/account`. |
| Has your app been independently validated against a global security standard? | **No** | We have not commissioned a SOC 2, ISO 27001, or MASA audit. Open-source code at `github.com/465media/bitaxe-baller` is publicly auditable but that does not satisfy Google's specific "independent security review" checkbox (which requires a MASA-listed lab). // FLAG: revisit when revenue justifies a MASA audit; cost is ~$5k. |
| Does your app follow Google Play's Families Policy? | **No / N/A** | App targets 18+ adults (see content rating). Not enrolled in Designed for Families. |

---

## Section 4 — Other answers Google asks during data-safety submission

| Question | Answer | Justification |
|---|---|---|
| Privacy Policy URL | `https://bitaxeballer.com/privacy.html` | Hosted, persistent, plain HTML — meets Google's "must remain available" requirement. |
| Target audience | 18+ | Set in App content → Target audience. |
| Ads | None | No advertising SDKs, no ad placements. |

---

## Edge cases / flags

- **License key classification — Personal info → User IDs:** Google's "User IDs" sub-type explicitly includes "An identifier that relates to an identifiable person. For example, an account ID, account number, or account name." A license key issued at purchase time, tied to a Stripe customer, is exactly this. Do not classify it as Device ID (those are platform-issued hardware identifiers).
- **Biometric data:** Same as Apple — Android BiometricPrompt returns success/failure only. The template lives in the TEE / StrongBox. No collection. Do not tick anything biometric.
- **Bitaxe telemetry through the relay:** User's own hardware data, transient, not persisted server-side. Google's data-safety form is about data about the user, not data the user views. Comparable to a remote-desktop session — not disclosed.
- **Google Play Console's automatic crash data:** Google's published guidance: developers do NOT need to disclose Play-Console-collected crash/ANR data on the data-safety form, because that collection is by Google, not by the developer's app. We don't have any developer-collected crash SDK either. Answer No for Crash logs.
- **Encryption at rest:** Google's form does NOT have a separate "encrypted at rest" toggle (Apple does in the Nutrition Label flow; Play does not). The Preferences plugin uses `EncryptedSharedPreferences` on Android (AES-256 GCM, key in Keystore). Worth mentioning if a reviewer asks, but no field to declare it.

---

## Cross-reference

This file deliberately overrides the older draft table in `app-store-listing.md` lines 184-206, which:
- Incorrectly listed "Email address" as collected (the relay knows the email, the mobile app does not store it),
- Incorrectly listed "Crash logs: Yes (Google Play Console only)" — Google's guidance says this does not need disclosure when there is no developer SDK collecting.

Use THIS file (`play-data-safety.md`) as the source of truth at submission time. Update `app-store-listing.md` to reference this file rather than duplicating the matrix.
