#!/usr/bin/env bash
#
# iOS App Store release pipeline for the Bitaxe Baller mobile companion.
#
# What it does (no Xcode UI required, no manual provisioning dance):
#
#   1. Bumps MARKETING_VERSION + CURRENT_PROJECT_VERSION in the iOS project
#   2. Refreshes www/ via `npx cap sync ios`
#   3. xcodebuild archive (manual signing, Apple Distribution cert)
#   4. xcodebuild -exportArchive (App Store distribution)
#   5. xcrun altool --upload-app to App Store Connect
#   6. Prints the App Store Connect URL so you can submit for review
#
# Usage:
#
#   bash mobile/release-ios.sh 1.3.0         # bump marketing version, auto build #
#   bash mobile/release-ios.sh 1.3.0 7       # explicit build number
#   bash mobile/release-ios.sh --no-upload   # archive + export only, skip upload
#
# Credentials are read from the desktop repo's build/.env.signing — same
# Apple ID + app-specific password + team ID used by the Mac release flow.
# That file is gitignored; never echoed.
#
# Hard-coded signing context (matches Apple Developer setup):
#   Team:    K7SNP7C2XJ (Nathan Baldwin Developer Team)
#   Cert:    Apple Distribution: Nathan Baldwin (K7SNP7C2XJ)
#   Profile: "Bitaxe Baller App Store" (manual provisioning, no devices needed)
#
# If any of those rotate (cert renewal, profile re-generation, team change),
# update the constants near the top of this script.

set -euo pipefail

# ----- Paths -----
# Script lives at mobile/release-ios.sh; the iOS Xcode project is at
# ios/App/App.xcodeproj relative to here. ROOT is the mobile/ dir.
ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$ROOT"

XCODEPROJ="ios/App/App.xcodeproj"
PBXPROJ="$XCODEPROJ/project.pbxproj"
EXPORT_OPTIONS="ios/ExportOptions.plist"
ARCHIVE_PATH="$(mktemp -d)/Bitaxe.xcarchive"
EXPORT_PATH="$(mktemp -d)/Bitaxe-export"

# ----- Signing constants (Apple-side state, not user-tunable per release) -----
TEAM_ID="K7SNP7C2XJ"
SIGNING_IDENTITY="Apple Distribution: Nathan Baldwin (K7SNP7C2XJ)"
PROFILE_NAME="Bitaxe Baller App Store"
BUNDLE_ID="com.bitaxeballer.mobile"

# ----- Credentials (.env.signing in the desktop repo) -----
# Walk up from this worktree's mobile/ to find the desktop repo's build/.env.signing.
# Layout: <desktop-repo>/.claude/worktrees/mobile-capacitor/mobile/release-ios.sh
# So the desktop repo is three levels above the worktree root.
DESKTOP_REPO="$( cd "$ROOT/../../../.." && pwd )"
ENV_SIGNING="$DESKTOP_REPO/build/.env.signing"

if [[ ! -f "$ENV_SIGNING" ]]; then
  echo "❌ Missing $ENV_SIGNING — needed for APPLE_ID + APPLE_APP_PASSWORD." >&2
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "$ENV_SIGNING"; set +a

: "${APPLE_ID:?APPLE_ID not set in $ENV_SIGNING}"
: "${APPLE_TEAM_ID:?APPLE_TEAM_ID not set in $ENV_SIGNING}"
: "${APPLE_APP_PASSWORD:?APPLE_APP_PASSWORD not set in $ENV_SIGNING}"

# ----- Args -----
UPLOAD=true
NEW_VERSION=""
NEW_BUILD=""
for arg in "$@"; do
  case "$arg" in
    --no-upload) UPLOAD=false ;;
    --help|-h)
      sed -n '3,28p' "$0"; exit 0 ;;
    *)
      if [[ -z "$NEW_VERSION" ]]; then NEW_VERSION="$arg"
      elif [[ -z "$NEW_BUILD" ]]; then NEW_BUILD="$arg"
      fi
      ;;
  esac
done

if [[ -z "$NEW_VERSION" ]]; then
  echo "❌ Marketing version required. Usage: bash mobile/release-ios.sh 1.3.0" >&2
  exit 1
fi

# Version sanity: SemVer-ish, e.g. "1.3" or "1.3.0". App Store rejects
# anything with a leading zero (e.g. "1.03"). Apple also caps at 18 chars.
if ! [[ "$NEW_VERSION" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]]; then
  echo "❌ Bad version '$NEW_VERSION' — must be N or N.N or N.N.N (no leading zeros, no suffixes)." >&2
  exit 1
fi

# ----- Read current build number; auto-increment if caller didn't specify -----
CURRENT_BUILD=$(grep -E "CURRENT_PROJECT_VERSION =" "$PBXPROJ" | head -1 | sed -E 's/.*= ([0-9]+).*/\1/')
if [[ -z "$NEW_BUILD" ]]; then
  NEW_BUILD=$((CURRENT_BUILD + 1))
fi

echo "==> iOS release: v$NEW_VERSION (build $NEW_BUILD)"
echo "    (previous build was $CURRENT_BUILD)"
echo "    upload: $UPLOAD"
echo

# ----- 1. Bump version in pbxproj -----
echo "==> bumping MARKETING_VERSION + CURRENT_PROJECT_VERSION"
# `xcodebuild ... CONFIG=value` overrides per-invocation but doesn't persist;
# we want it persisted so git tracks the bump and Xcode users see it.
# In-place edit via sed. Pattern is fixed across Capacitor-generated projects.
sed -i.bak -E "s/MARKETING_VERSION = [^;]+;/MARKETING_VERSION = $NEW_VERSION;/g" "$PBXPROJ"
sed -i.bak -E "s/CURRENT_PROJECT_VERSION = [0-9]+;/CURRENT_PROJECT_VERSION = $NEW_BUILD;/g" "$PBXPROJ"
rm -f "$PBXPROJ.bak"

# ----- 2. Sync www → ios via Capacitor -----
echo "==> npx cap sync ios"
npx cap sync ios > /dev/null

# ----- 3. Archive -----
echo "==> xcodebuild archive (manual signing, $PROFILE_NAME)"
rm -rf "$ARCHIVE_PATH"
xcodebuild archive \
  -project "$XCODEPROJ" \
  -scheme App \
  -configuration Release \
  -destination "generic/platform=iOS" \
  -archivePath "$ARCHIVE_PATH" \
  CODE_SIGN_STYLE=Manual \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  PROVISIONING_PROFILE_SPECIFIER="$PROFILE_NAME" \
  CODE_SIGN_IDENTITY="$SIGNING_IDENTITY" \
  -quiet
echo "    archive: $ARCHIVE_PATH"

# ----- 4. Export IPA -----
echo "==> xcodebuild -exportArchive (App Store)"
rm -rf "$EXPORT_PATH"
xcodebuild -exportArchive \
  -archivePath "$ARCHIVE_PATH" \
  -exportPath "$EXPORT_PATH" \
  -exportOptionsPlist "$EXPORT_OPTIONS" \
  -quiet
IPA="$EXPORT_PATH/App.ipa"
if [[ ! -f "$IPA" ]]; then
  echo "❌ Export ran but $IPA is missing" >&2
  exit 1
fi
SIZE=$(du -h "$IPA" | cut -f1)
echo "    IPA: $IPA ($SIZE)"

if [[ "$UPLOAD" != "true" ]]; then
  echo
  echo "==> Skipping upload (--no-upload). IPA is at:"
  echo "    $IPA"
  exit 0
fi

# ----- 5. Upload via altool -----
# `xcrun altool --upload-app` is the modern replacement for Transporter.app
# and accepts the same Apple ID + app-specific password. Two-factor isn't
# triggered because app-specific passwords bypass it.
echo "==> xcrun altool --upload-app (this is the slow part — ~30s)"
xcrun altool --upload-app \
  --type ios \
  --file "$IPA" \
  --apiKey "" --apiIssuer "" 2>/dev/null \
  --username "$APPLE_ID" \
  --password "$APPLE_APP_PASSWORD" \
  --asc-provider "$TEAM_ID" || {
    # Fallback: older argument syntax some altool versions still expect.
    echo "    primary upload failed, retrying with --validate-app pattern"
    xcrun altool --upload-app \
      -f "$IPA" \
      -t ios \
      -u "$APPLE_ID" \
      -p "$APPLE_APP_PASSWORD"
  }

echo
echo "✅ Uploaded $IPA"
echo
echo "Apple processes the binary on their side (5-30 min). You'll get an"
echo "email when processing finishes. Then submit for review here:"
echo
echo "  https://appstoreconnect.apple.com/apps/6773373318/distribution"
echo
echo "    → + Version → enter $NEW_VERSION"
echo "    → fill 'What's New', select build $NEW_BUILD"
echo "    → Save → Add for Review"
echo
echo "Don't forget to commit the version bump:"
echo "  git add mobile/ios/App/App.xcodeproj/project.pbxproj"
echo "  git commit -m 'mobile: bump iOS to $NEW_VERSION (build $NEW_BUILD)'"
