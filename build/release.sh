#!/usr/bin/env bash
#
# One-shot release orchestrator for Bitaxe Baller.
#
# Runs the entire multi-channel release we used to do by hand: version bumps,
# changelog + release notes, tag + CI, Mac notarized build, appcast merge,
# Umbrel digest, community-store sync, site deploy, and the Discord post.
#
# Run it on the Mac (it needs the Apple signing creds in build/.env.signing +
# keychain). You look at ONE diff, type "yes", and it drives the rest.
#
#   1. Write release notes → build/release-notes/v<version>.md  (plain markdown;
#      line 1 is the title, then '- ' bullets). A template is scaffolded for you
#      if it's missing.
#   2. bash build/release.sh <version>
#
# Flags:
#   --dry-run       print the ordered plan + the commands it would run; touch nothing
#   --yes           skip the one confirmation prompt (for cron/CI use)
#   --from <step>   resume at a step after a mid-run failure. Steps, in order:
#                   prep push release waitci macbuild appcast umbrel store site discord
#   --notes <file>  override the notes path
#
# Ordering fixes baked in (both bit us on the 1.20.0/1.20.1 hand-runs):
#   * waits for Windows + Docker CI BEFORE the Mac appcast merge (no race)
#   * does all prep in one commit so branch/rebase state can't drift
#
set -euo pipefail

# ---- config ------------------------------------------------------------------
ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
SITE_REPO="/Volumes/WDBlack/Home/Development/bitaxe-baller-site"
STORE_REMOTE="https://github.com/465media/umbrel-bitaxe-baller-store.git"
STORE_APP_DIR="bitaxeballer-app"
SITE_SSH="bitaxeballer"
SITE_DEPLOY_CMD="sudo bash /var/www/bitaxeballer/deploy/deploy.sh"
IMAGE="ghcr.io/465media/bitaxe-baller"
CI_WORKFLOWS_REQUIRED=("Build Windows installer" "Build Docker image")
CI_TIMEOUT=1200   # seconds to wait for CI
# -----------------------------------------------------------------------------

DRY=0; YES=0; FROM=""; NOTES=""
VERSION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --yes) YES=1; shift ;;
    --from) FROM="${2:-}"; shift 2 ;;
    --notes) NOTES="${2:-}"; shift 2 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) VERSION="$1"; shift ;;
  esac
done

if [[ -z "$VERSION" ]]; then echo "usage: bash build/release.sh <version> [--dry-run] [--yes] [--from <step>]" >&2; exit 2; fi
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then echo "version must be X.Y.Z (got '$VERSION')" >&2; exit 2; fi
TAG="v$VERSION"
NOTES="${NOTES:-$ROOT/build/release-notes/$TAG.md}"

C_B=$'\033[1m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_0=$'\033[0m'
say()  { echo "${C_B}▸ $*${C_0}"; }
ok()   { echo "${C_G}✓ $*${C_0}"; }
warn() { echo "${C_Y}! $*${C_0}"; }
die()  { echo "${C_R}✗ $*${C_0}" >&2; exit 1; }

# run: echoes a command; runs it unless --dry-run
run() { echo "  \$ $*"; if [[ $DRY -eq 0 ]]; then "$@"; fi; }

# step gating for --from
STEPS=(prep push release waitci macbuild appcast umbrel store site discord)
_started=1
if [[ -n "$FROM" ]]; then
  _started=0
  printf '%s\n' "${STEPS[@]}" | grep -qx "$FROM" || die "--from '$FROM' is not a valid step (${STEPS[*]})"
fi
todo() {  # returns 0 (do it) if we've reached the --from step
  local s="$1"
  if [[ $_started -eq 1 ]]; then return 0; fi
  if [[ "$s" == "$FROM" ]]; then _started=1; return 0; fi
  return 1
}

trap 'die "failed at step: ${CUR:-?}. Fix it, then resume with:  bash build/release.sh $VERSION --from ${CUR:-prep}"' ERR

# ---- preflight ---------------------------------------------------------------
CUR=preflight
say "Bitaxe Baller release $TAG  (dry-run=$DRY, from=${FROM:-start})"
cd "$ROOT"
[[ -f "$NOTES" ]] || {
  mkdir -p "$(dirname "$NOTES")"
  cat > "$NOTES" <<EOF
# short title here (e.g. "fix: DigiByte solo-block odds")

- **Headline:** what changed and why it matters.
- Second point, plain prose or **bold** and \`code\` allowed.
EOF
  die "no notes file — scaffolded one at $NOTES. Fill it in and re-run."
}
command -v gh >/dev/null   || die "gh CLI not found"
command -v docker >/dev/null || die "docker not found"
gh auth status >/dev/null 2>&1 || die "gh not authenticated (gh auth login)"
[[ "$(git branch --show-current)" == "main" ]] || die "not on main"
git fetch -q origin main
[[ -z "$(git status --porcelain)" ]] || warn "working tree not clean — prep will add to it"
if git ls-remote --tags origin "refs/tags/$TAG" | grep -q "$TAG"; then
  [[ -n "$FROM" ]] || die "tag $TAG already exists on origin — pass --from <step> to resume, or bump the version"
fi
ok "preflight passed  (notes: $NOTES)"

# ---- prep: version bumps + notes --------------------------------------------
if todo prep; then CUR=prep; say "prep — version bumps + changelog/release notes"
  run git pull --rebase origin main
  run python3 build/release_prep.py bump "$VERSION" --root "$ROOT"
  run python3 build/release_prep.py notes "$VERSION" --notes "$NOTES" --root "$ROOT" --site "$SITE_REPO"
  if [[ $DRY -eq 0 ]]; then
    echo; echo "${C_B}----- main repo diff -----${C_0}"; git --no-pager diff --stat
    echo "${C_B}----- site changelog diff -----${C_0}"; (cd "$SITE_REPO" && git --no-pager diff --stat)
    if [[ $YES -eq 0 ]]; then
      read -r -p $'\nProceed with the full release? this pushes tags, builds, and deploys. [y/N] ' a
      [[ "$a" == "y" || "$a" == "Y" ]] || die "aborted by user (nothing pushed; run 'git checkout .' in both repos to discard bumps)"
    fi
  fi
  ok "prep done"
fi

# ---- push main + create release ---------------------------------------------
if todo push; then CUR=push; say "commit + push main"
  run git add -A
  run git commit -m "release $TAG"
  run git push origin main
  ok "main pushed"
fi

if todo release; then CUR=release; say "create GitHub release $TAG (fires Windows/Docker/Discord CI)"
  run gh release create "$TAG" --target main --title "$TAG — $(python3 build/release_prep.py title "$VERSION" --notes "$NOTES")" --notes-file "$NOTES"
  ok "release created"
fi

# ---- wait for CI -------------------------------------------------------------
if todo waitci; then CUR=waitci; say "waiting for CI (Windows + Docker) on $TAG"
  if [[ $DRY -eq 0 ]]; then
    deadline=$(( $(date +%s) + CI_TIMEOUT ))
    while :; do
      done_ok=1
      for wf in "${CI_WORKFLOWS_REQUIRED[@]}"; do
        line=$(gh run list --json name,status,conclusion,headBranch \
                 --jq "[.[] | select(.headBranch==\"$TAG\" and .name==\"$wf\")] | first" 2>/dev/null || echo "")
        st=$(echo "$line" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
        cc=$(echo "$line" | sed -n 's/.*"conclusion":"\([^"]*\)".*/\1/p')
        if [[ "$st" != "completed" ]]; then done_ok=0; printf '   %s: %s\n' "$wf" "${st:-queued}"; fi
        if [[ "$st" == "completed" && "$cc" != "success" ]]; then die "$wf finished with conclusion=$cc — check 'gh run list'"; fi
      done
      [[ $done_ok -eq 1 ]] && break
      [[ $(date +%s) -gt $deadline ]] && die "CI wait timed out after ${CI_TIMEOUT}s"
      sleep 20
    done
  fi
  ok "CI green (Windows + Docker built)"
fi

# ---- Mac build + appcast merge ----------------------------------------------
if todo macbuild; then CUR=macbuild; say "Mac build (PyInstaller → codesign → notarize → dmg)"
  run bash build/build-mac.sh
  ok "Mac DMG + appcast built"
fi

if todo appcast; then CUR=appcast; say "merge appcast (Mac + Windows) and upload to release"
  run bash build/release-mac.sh "$TAG"
  if [[ $DRY -eq 0 ]]; then
    sleep 3
    got=$(curl -sL "https://github.com/465media/bitaxe-baller/releases/download/$TAG/appcast.xml" | grep -oE 'sparkle:os="[a-z]+"' | sort -u | tr '\n' ' ')
    echo "   appcast entries: $got"
    [[ "$got" == *macos* && "$got" == *windows* ]] || die "appcast missing a platform ($got) — re-run: bash build/release-mac.sh $TAG"
  fi
  ok "appcast has both macOS + Windows"
fi

# ---- Umbrel: repin digest ----------------------------------------------------
if todo umbrel; then CUR=umbrel; say "pin Umbrel image digest from CI build"
  if [[ $DRY -eq 0 ]]; then
    digest=$(docker buildx imagetools inspect "$IMAGE:$VERSION" --format '{{.Manifest.Digest}}')
    [[ "$digest" == sha256:* ]] || die "bad digest: $digest"
    echo "   digest: $digest"
    python3 - "$ROOT/umbrel/docker-compose.yml" "$IMAGE" "$digest" <<'PY'
import re, sys
path, image, digest = sys.argv[1], sys.argv[2], sys.argv[3]
t = open(path).read()
new = re.sub(re.escape(image) + r':latest@sha256:[0-9a-f]{64}', f'{image}:latest@{digest}', t)
assert new != t and digest.split(":")[1] in new, "digest not updated"
open(path, "w").write(new)
print("   docker-compose.yml repinned")
PY
    run git add umbrel/docker-compose.yml
    run git commit -m "umbrel: pin $TAG image digest ($digest)"
    run git push origin main
  else
    echo "  \$ docker buildx imagetools inspect $IMAGE:$VERSION  → repin docker-compose.yml → commit + push"
  fi
  ok "Umbrel digest pinned"
fi

# ---- community store sync ----------------------------------------------------
if todo store; then CUR=store; say "sync community-store repo"
  tmp=$(mktemp -d)
  run git clone -q "$STORE_REMOTE" "$tmp/store"
  if [[ $DRY -eq 0 ]]; then
    cp "$ROOT/umbrel/docker-compose.yml" "$tmp/store/$STORE_APP_DIR/docker-compose.yml"
    cp "$ROOT/umbrel/umbrel-app.yml"     "$tmp/store/$STORE_APP_DIR/umbrel-app.yml"
    ( cd "$tmp/store"
      if [[ -z "$(git status --porcelain)" ]]; then echo "   store already in sync"; else
        git add "$STORE_APP_DIR"/docker-compose.yml "$STORE_APP_DIR"/umbrel-app.yml
        git commit -q -m "bitaxe-baller $TAG"
        git push -q origin main
      fi )
  fi
  rm -rf "$tmp"
  ok "store synced"
fi

# ---- site deploy -------------------------------------------------------------
if todo site; then CUR=site; say "push + deploy site (changelog goes live)"
  ( cd "$SITE_REPO"
    run git add public/changelog.html
    run git commit -m "changelog: $TAG"
    run git push origin main )
  run ssh "$SITE_SSH" "$SITE_DEPLOY_CMD"
  ok "site deployed"
fi

# ---- Discord -----------------------------------------------------------------
if todo discord; then CUR=discord; say "post changelog to Discord"
  run bash -c "cd '$SITE_REPO' && python3 scripts/notify-changelog.py"
  ok "Discord posted"
fi

CUR=done
echo
ok "$TAG shipped — desktop (Mac+Win auto-update), Docker, Umbrel + store, site, Discord."
echo "  Reminder: your running app checks for updates at startup + 1h cache — relaunch to see the banner."
