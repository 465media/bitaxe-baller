#!/usr/bin/env bash
#
# Mac build pipeline for Bitaxe Baller.
#
#   1. Run PyInstaller → produces dist/Bitaxe Baller.app
#   2. Code-sign the .app with a Developer ID Application cert
#   3. Submit to Apple's notary service via notarytool
#   4. Staple the notarization ticket to the .app
#   5. Wrap the .app in a drag-to-Applications .dmg
#
# Required env (loaded from build/.env.signing if present, gitignored):
#
#   APPLE_ID           your Apple Developer account email
#   APPLE_TEAM_ID      10-char Team ID (e.g. 1A2B3C4D5E)
#   APPLE_APP_PASSWORD app-specific password (xxxx-xxxx-xxxx-xxxx)
#   APPLE_SIGNING_ID   common name of the Developer ID Application cert in
#                      Keychain. List with:
#                        security find-identity -v -p codesigning
#                      and copy the "Developer ID Application: ..." line.
#
# Skip signing/notarization by passing --no-sign (useful for local smoke tests):
#   bash build/build-mac.sh --no-sign

set -euo pipefail

ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$ROOT"

APP_NAME="Bitaxe Baller"
APP_PATH="dist/${APP_NAME}.app"
DMG_PATH="dist/Bitaxe-Baller-Mac.dmg"
SPEC="build/bitaxe-baller.spec"
ENT="build/entitlements.plist"

SIGN=true
for arg in "$@"; do
  case "$arg" in
    --no-sign) SIGN=false ;;
  esac
done

# Load signing credentials if available
if [[ -f build/.env.signing ]]; then
  # shellcheck disable=SC1091
  set -a; source build/.env.signing; set +a
fi

echo "==> cleaning dist/ and build/ artifacts"
rm -rf dist build/build "${APP_NAME}.app" Bitaxe-Baller-Mac.dmg

echo "==> activating venv"
if [[ ! -d venv ]]; then
  python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install -q -r requirements.txt
pip install -q "pyinstaller>=6.0"

echo "==> running PyInstaller"
pyinstaller --noconfirm --clean --workpath build/build --distpath dist "$SPEC"

if [[ ! -d "$APP_PATH" ]]; then
  echo "ERROR: PyInstaller did not produce $APP_PATH" >&2
  exit 1
fi

echo "==> .app produced at $APP_PATH ($(du -sh "$APP_PATH" | cut -f1))"

if ! $SIGN; then
  echo
  echo "[--no-sign] skipping codesign + notarization."
  echo "App is at: $APP_PATH"
  echo "To launch: open '$APP_PATH'"
  echo "(macOS will warn about an unidentified developer; right-click → Open works)"
  exit 0
fi

# Sanity check signing creds
for v in APPLE_ID APPLE_TEAM_ID APPLE_APP_PASSWORD APPLE_SIGNING_ID; do
  if [[ -z "${!v:-}" ]]; then
    echo "ERROR: $v is not set. Add it to build/.env.signing or pass --no-sign." >&2
    exit 1
  fi
done

echo "==> code-signing $APP_PATH"
# Sign every embedded binary first (deep), then the wrapping .app.
# --options runtime enables Hardened Runtime, required for notarization.
codesign --force --deep --options runtime --timestamp \
  --entitlements "$ENT" \
  --sign "$APPLE_SIGNING_ID" \
  "$APP_PATH"

# Verify the signature
codesign --verify --deep --strict --verbose=2 "$APP_PATH"

echo "==> packaging temporary zip for notary submission"
NOTARY_ZIP="dist/notary-input.zip"
ditto -c -k --keepParent "$APP_PATH" "$NOTARY_ZIP"

echo "==> submitting to Apple notary (this typically takes 30-90s)"
xcrun notarytool submit "$NOTARY_ZIP" \
  --apple-id "$APPLE_ID" \
  --team-id "$APPLE_TEAM_ID" \
  --password "$APPLE_APP_PASSWORD" \
  --wait

echo "==> stapling ticket to the .app"
xcrun stapler staple "$APP_PATH"
xcrun stapler validate "$APP_PATH"

rm -f "$NOTARY_ZIP"

echo "==> wrapping into drag-to-Applications .dmg"
# Tiny temp folder containing the signed .app + an Applications shortcut for UX
DMG_STAGE=$(mktemp -d)
cp -R "$APP_PATH" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"

hdiutil create -volname "Bitaxe Baller" \
  -srcfolder "$DMG_STAGE" \
  -ov -format UDZO \
  "$DMG_PATH"

rm -rf "$DMG_STAGE"

echo "==> signing the .dmg too"
codesign --force --sign "$APPLE_SIGNING_ID" --timestamp "$DMG_PATH"

echo "==> notarizing the .dmg"
xcrun notarytool submit "$DMG_PATH" \
  --apple-id "$APPLE_ID" \
  --team-id "$APPLE_TEAM_ID" \
  --password "$APPLE_APP_PASSWORD" \
  --wait

xcrun stapler staple "$DMG_PATH"
xcrun stapler validate "$DMG_PATH"

# Ed25519-sign the .dmg for the auto-update feed. Refuses if no signing key
# has been generated yet — run `python build/gen-update-keypair.py` once
# to bootstrap. The appcast.xml the app polls is updated in-place under
# dist/ so we accumulate entries across mac + windows builds.
if [[ -f build/.update-signing-key || -n "${UPDATE_SIGNING_KEY:-}" ]]; then
  echo "==> generating auto-update appcast entry"
  APP_VERSION=$(/usr/bin/python3 -c "import re,sys; m=re.search(r'APP_VERSION\s*=\s*\"([^\"]+)\"', open('app.py').read()); print(m.group(1))")
  RELEASE_NOTES_URL="https://github.com/465media/bitaxe-baller/releases/tag/v${APP_VERSION}"
  python build/build-appcast.py \
    --version "$APP_VERSION" \
    --mac-dmg "$DMG_PATH" \
    --release-notes-url "$RELEASE_NOTES_URL" \
    --out dist/appcast.xml
  echo "==> appcast at dist/appcast.xml — 'bash build/release-mac.sh vX.Y.Z' merges + uploads it to the GitHub release (the feed the app actually polls: /releases/latest/download/appcast.xml)"
else
  echo
  echo "[!] No auto-update signing key found at build/.update-signing-key."
  echo "    Skipping appcast generation. Run:"
  echo "      python build/gen-update-keypair.py"
  echo "    once, back up the private key to 1Password, paste the public key into app.py,"
  echo "    then rerun this build."
  echo
fi

echo
echo "✓ Done. Outputs:"
echo "    $APP_PATH"
echo "    $DMG_PATH ($(du -sh "$DMG_PATH" | cut -f1))"
if [[ -f dist/appcast.xml ]]; then
  echo "    dist/appcast.xml"
fi
echo
if [[ -f dist/appcast.xml ]]; then
  echo "Release flow (the appcast.xml is what enables auto-update for shipped clients):"
  echo "  gh release create vX.Y.Z --title \"Bitaxe Baller vX.Y.Z\" \\"
  echo "      \"$DMG_PATH\" dist/appcast.xml"
  echo
  echo "Or if the release already exists (e.g. Windows CI created it on tag push):"
  echo "  gh release upload vX.Y.Z \"$DMG_PATH\" dist/appcast.xml --clobber"
else
  echo "Next: gh release create vX.Y.Z --title \"Bitaxe Baller vX.Y.Z\" $DMG_PATH"
fi
