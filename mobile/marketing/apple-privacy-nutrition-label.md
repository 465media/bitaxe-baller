# Apple App Privacy — pre-staged answers

Paste-ready answers for the App Store Connect "App Privacy" section. Every answer here is tied to actual app behavior (Capacitor 8 wrapper, no third-party SDKs, all traffic to `relay.bitaxeballer.com`). When in doubt: minimum disclosure consistent with truth.

Source files for verification:
- `mobile/www/index.html` — single-WebView SPA
- `mobile/package.json` — only `@capacitor/preferences` and `@aparajita/capacitor-biometric-auth`
- `bitaxeballer.com/privacy.html` — canonical disclosures
- `mobile/marketing/app-store-listing.md` — feature/positioning

---

## TL;DR — click these answers, in this order

1. **Privacy Policy URL** → `https://bitaxeballer.com/privacy.html`
2. **Do you or your third-party partners collect data from this app?** → **Yes**
3. Tick **Identifiers → User ID** (the license key). Purpose: **App Functionality**. Linked to user: **Yes**. Used for tracking: **No**.
4. Everything else in every other data category → **Not Collected**.
5. **Do you use data for tracking purposes (across apps/sites owned by other companies)?** → **No**
6. Save. Submit.

Total clicks: ~6. Done.

---

## Preamble Apple shows users at the top of the privacy section

> The developer, 465 Media, indicated that the app's privacy practices may include handling of data as described below. For more information, see the developer's privacy policy.

(No action — Apple writes this automatically based on your answers.)

---

## Question 1 — Privacy Policy URL

| Field | Answer |
|---|---|
| Privacy Policy URL | `https://bitaxeballer.com/privacy.html` |

---

## Question 2 — Does this app collect data?

| Field | Answer | Justification |
|---|---|---|
| Do you or your third-party partners collect data from this app? | **Yes** | We store the user's Pro license key on-device and send it to our relay for session establishment. Apple counts on-device storage of identifiers as "collection" when the data also leaves the device, even just to our own server. |

---

## Question 3 — For each data type, declare collection

Apple lists 14 categories. For each: collected Y/N. If Y, then for each linked sub-type: purposes + linked-to-user + used-for-tracking.

### Contact Info

| Sub-type | Collected? | Notes |
|---|---|---|
| Name | **No** | App never asks for or stores a name. |
| Email Address | **No** | The relay's `/login` response may include the email tied to the license on the server side, but the mobile app never persists, displays, or transmits an email. We do not collect it client-side. |
| Phone Number | **No** | — |
| Physical Address | **No** | — |
| Other User Contact Info | **No** | — |

### Health & Fitness

| Sub-type | Collected? | Notes |
|---|---|---|
| Health | **No** | — |
| Fitness | **No** | — |

### Financial Info

| Sub-type | Collected? | Notes |
|---|---|---|
| Payment Info | **No** | Stripe handles all payment data; the app never touches a card number or billing detail. |
| Credit Info | **No** | — |
| Other Financial Info | **No** | — |

### Location

| Sub-type | Collected? | Notes |
|---|---|---|
| Precise Location | **No** | No CoreLocation usage anywhere. |
| Coarse Location | **No** | — |

### Sensitive Info

| Sub-type | Collected? | Notes |
|---|---|---|
| Sensitive Info | **No** | No race/religion/orientation/political/biometric-identifier collection. Face ID match result is a boolean from `LocalAuthentication`; the biometric template never leaves the Secure Enclave and we never receive it. |

### Contacts

| Sub-type | Collected? | Notes |
|---|---|---|
| Contacts | **No** | No address-book access. |

### User Content

| Sub-type | Collected? | Notes |
|---|---|---|
| Emails or Text Messages | **No** | — |
| Photos or Videos | **No** | — |
| Audio Data | **No** | — |
| Gameplay Content | **No** | — |
| Customer Support | **No** | Support happens via the website, not in-app. |
| Other User Content | **No** | — |

### Browsing History

| Sub-type | Collected? | Notes |
|---|---|---|
| Browsing History | **No** | The WebView only loads our own `relay.bitaxeballer.com` content; we don't log or transmit URL history. |

### Search History

| Sub-type | Collected? | Notes |
|---|---|---|
| Search History | **No** | The app has no search feature. |

### Identifiers

| Sub-type | Collected? | Linked? | Tracking? | Purposes | Justification |
|---|---|---|---|---|---|
| User ID | **Yes** | **Yes** | **No** | **App Functionality** | The Pro license key (a UUID issued by Stripe purchase flow) is stored locally via `@capacitor/preferences` and sent to `relay.bitaxeballer.com/login` once per session to obtain a 24-hour token. It identifies the paying user. Linked because the key maps to the user's account on our license server. Not tracking because it's never shared with third parties or used to track across other apps/sites. |
| Device ID | **No** | — | — | — | We do not read IDFA, IDFV, or any hardware identifier. No `AppTrackingTransparency` calls. |

### Purchases

| Sub-type | Collected? | Notes |
|---|---|---|
| Purchase History | **No** | Pro is purchased on the website via Stripe, not in-app. No IAP. The app receives no purchase-history data. |

### Usage Data

| Sub-type | Collected? | Notes |
|---|---|---|
| Product Interaction | **No** | No analytics SDK. No event tracking. No screen-view logging. |
| Advertising Data | **No** | — |
| Other Usage Data | **No** | — |

### Diagnostics

| Sub-type | Collected? | Notes |
|---|---|---|
| Crash Data | **No** | Capacitor ships no crash reporter. We have not integrated Sentry, Firebase Crashlytics, Bugsnag, or any equivalent. Apple's automatic crash collection (via the user opting in at iOS setup time) goes to Apple, not us — that doesn't count as developer collection. |
| Performance Data | **No** | — |
| Other Diagnostic Data | **No** | — |

### Other Data

| Sub-type | Collected? | Notes |
|---|---|---|
| Other Data Types | **No** | Hashrate, temperature, pool config, and other Bitaxe telemetry routed through our relay is the user's OWN hardware data, not user data about the user in the privacy-questionnaire sense. We do not store it on our servers (the relay is dumb routing, no payload logging). Not disclosed here per Apple's guidance that "data" refers to data about the user, not data the user views. |

---

## Question 4 — Tracking declaration

| Field | Answer | Justification |
|---|---|---|
| Do you or your third-party partners use data from this app for tracking purposes? | **No** | "Tracking" per Apple's definition means linking user/device data with data from other companies' apps/sites for targeted advertising or measurement, or sharing user/device data with data brokers. We do neither. No IDFA usage, no third-party SDKs, no ad networks, no data brokers, no analytics. The license key stays between the user, our relay, and our license server. |

(Because the answer is No, ATT prompt is NOT required and we do not need to declare a `NSUserTrackingUsageDescription` in `Info.plist`.)

---

## Question 5 — Data Collection Optional Disclosure

Apple shows users a "Data Not Linked to You" section when applicable. We have nothing in that bucket because the one thing we collect (license key / User ID) is linked. Leave empty.

---

## Edge cases / flags

- **Biometric data (Face ID / Touch ID):** The biometric template lives in the Secure Enclave. `LocalAuthentication` returns only a success/failure boolean to the app. We do not "collect" biometric data — Apple's documentation explicitly says biometric authentication results that stay on-device are not collection. Do NOT tick "Sensitive Info → Biometric." Apple's reviewer guidance is unambiguous here.
- **License key as Identifier — User ID vs Other Identifiers:** Apple's "User ID" is defined as "a unique identifier (other than Device ID) used to identify a specific user within the developer's app." The Pro license key fits this exactly. Choose **User ID**, not "Other Identifiers" (which is a vaguer bucket).
- **Email from relay `/login` response:** The relay returns the email tied to the license key for display in some flows. Confirm whether the mobile app actually persists or displays the email — if it's read once and discarded for purely cosmetic display (e.g. "Logged in as nathan@…"), most reviewers do not require disclosure. If the app stores it in Preferences or sends it onward, disclose under Contact Info → Email Address with purpose App Functionality, linked Yes, tracking No. // FLAG: verify in `mobile/www/index.html` whether the email from `/login` is persisted; if yes, tick Email Address.
- **Bitaxe telemetry routed through the relay (hashrate, temps):** This is data about the user's hardware, not the user. The relay does not log payloads. Per Apple's "Data collected refers to data about the user" guidance, not disclosed. If a reviewer pushes back, the response is: "This is the user's own hardware metrics, transiently routed, never persisted server-side. Comparable to a remote desktop session displaying the user's screen."

---

## After submission — what Apple shows in the App Store listing

With the answers above, the listing will show:

> **Data Linked to You**
> The following data may be collected and linked to your identity:
> · Identifiers

That's it. Clean. One bullet. Reflects reality.
