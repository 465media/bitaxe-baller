# Android release build — signed AAB for Google Play

End-to-end runbook for producing the signed `.aab` (Android App Bundle) that
gets uploaded to Play Console. First-time setup is ~5 minutes (keystore
generation + filling `keystore.properties`). Every release after that is one
gradle command.

The signing config in `mobile/android/app/build.gradle` reads
`mobile/android/keystore.properties`. If that file is missing, release builds
will compile but be unsigned (unusable for Play).

---

## Step 1 — Generate the production keystore (ONE TIME)

**DO NOT let an AI agent run this for you.** Losing this keystore means losing
the ability to update the Play listing forever — Google does not have a
recovery path.

```bash
cd ~/keys  # or wherever you want to store keystores; create the dir if needed
keytool -genkeypair -v \
  -keystore bitaxe-baller-release.jks \
  -alias bitaxe-baller \
  -keyalg RSA \
  -keysize 4096 \
  -validity 9125 \
  -dname "CN=Nathan Baldwin, OU=Bitaxe Baller, O=Bitaxe Baller, L=, ST=, C=US"
```

`keytool` will prompt for:
- Keystore password (use a 20+ char random password from 1Password)
- Key password (just hit RETURN to reuse the keystore password — simpler)

Notes on the params:
- `-keyalg RSA -keysize 4096` — strongest, Play accepts it
- `-validity 9125` — 25 years (Play requires validity through Oct 22, 2033 at
  minimum; 25 years is the recommended cushion)
- `-alias bitaxe-baller` — referenced in `keystore.properties` as `keyAlias`
- `-dname` — distinguished name baked into the cert. CN matters most; the rest
  can stay blank but the field has to be present

Verify the keystore is valid:

```bash
keytool -list -v -keystore ~/keys/bitaxe-baller-release.jks
```

Confirm `Alias name: bitaxe-baller` and `Valid from ... until ...` ~25 years out.

---

## Step 2 — Fill `keystore.properties`

```bash
cd /Users/nbaldwin/development/bitaxe-baller/mobile/android
cp keystore.properties.example keystore.properties
```

Edit `keystore.properties`:

```
storeFile=/Users/nbaldwin/keys/bitaxe-baller-release.jks
storePassword=<the keystore password you set>
keyAlias=bitaxe-baller
keyPassword=<same as storePassword if you hit RETURN earlier>
```

`keystore.properties` is gitignored — confirm with `git status` after saving.

---

## Step 3 — Back up the keystore (DO THIS NOW, not later)

You need TWO independent copies:

1. **1Password** — create an entry "Bitaxe Baller Android signing keystore"
   - Attach the `.jks` file
   - Save the keystore password + key alias as fields
   - Save a copy of the filled `keystore.properties` as a secure note
2. **Offline disk** — copy the `.jks` to an external drive you keep at home

Do NOT rely on iCloud Drive alone. Do NOT commit it. Do NOT email it to
yourself.

---

## Step 4 — Build the signed AAB

From the repo root:

```bash
cd mobile && npx cap sync android && cd android && ./gradlew bundleRelease
```

What this does:
1. `cap sync android` — copies the latest `mobile/www/` web assets into the
   Android project and refreshes plugin config (skip if you haven't touched
   the web layer since the last sync).
2. `./gradlew bundleRelease` — produces a signed AAB.

Output lands at:

```
mobile/android/app/build/outputs/bundle/release/app-release.aab
```

First build is ~3-5 min, incremental builds are ~30 s.

---

## Step 5 — Upload to Play Console

1. Open <https://play.google.com/console> → Bitaxe Baller app
2. **Testing → Internal testing** → Create new release
3. Upload `app-release.aab`
4. Set release notes (steal from the matching tag in `RELEASE_NOTES_*.md` at
   repo root)
5. Save → Review release → Start rollout to internal testing

The internal track propagates in minutes. Promote to closed/open/production
tracks via the Play Console once you've smoke-tested on a real device.

Confirm app metadata is current before promoting to production:
- Listing copy: `mobile/marketing/app-store-listing.md`
- Screenshots: see `mobile/marketing/screenshot-guide.md`
- Feature graphic: `mobile/marketing/feature-graphic.png`

---

## Bumping the version for the next release

Edit `mobile/android/app/build.gradle`:

```groovy
versionCode 2          // increment by 1 for EVERY upload, even hotfixes
versionName "1.0.1"    // semver string shown to users
```

Play rejects uploads with a `versionCode` it has already seen on that track,
so always bump before `bundleRelease`.

---

## Before every release — verify the keystore still works

```bash
keytool -list -v -keystore /Users/nbaldwin/keys/bitaxe-baller-release.jks \
  -alias bitaxe-baller
```

It will prompt for the keystore password. If this command succeeds and shows
the expected fingerprint, the build will succeed. If it fails (wrong path,
forgotten password, corrupted file), STOP and restore from 1Password / offline
backup before doing anything else.

Compare the SHA-256 fingerprint against the one Play Console shows under
**Setup → App signing**. They must match exactly — if they don't, you're about
to upload with the wrong key and Play will reject it.

---

## Recovery: what to do if you ever lose the keystore

1. Don't panic — check both 1Password AND the offline disk first.
2. If both copies are gone: there is no recovery. Google's "App Signing by
   Google Play" enrollment (one-way, can't be undone) lets Google hold the
   upload key and re-sign, but Bitaxe Baller is not enrolled by default. If
   you want that safety net, opt in at first upload — but read the docs first
   because it changes how key rotation works.
3. Worst case: publish a new app under a new package ID
   (`com.bitaxeballer.app2`), tell existing users to migrate. This is why the
   backup step is non-negotiable.
