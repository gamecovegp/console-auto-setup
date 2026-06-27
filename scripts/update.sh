#!/usr/bin/env bash
# ============================================================================
#  update.sh  -  pull the latest CAS and rebuild for THIS OS (Linux or macOS)
#
#  Run whenever a new version has been pushed to GitHub:
#     1) git pull (latest source + device scripts)
#     2) build-linux.sh / build-macos.sh  (regenerates dist/cas)
#     3) symlink the runtime dirs into dist/cas — no multi-GB copy
#
#  Runtime dirs (data/profiles, data/retroarch-cores, data/ES-DE/downloaded_media,
#  provision/root/firmware, data/Apps) are NOT in git; they live in THIS folder and are
#  linked into the freshly built dist/cas. data/Apps/gamecove-companion.apk is the GameCove
#  Companion app installed on every unit during provisioning (install_companion).
#  The golden library is normally the
#  NAS, so a local profiles/ is optional. (windows-kit holds Windows adb.exe —
#  on Linux/macOS adb comes from PATH or a platform-tools/ dir, so it's skipped.)
# ============================================================================
set -e
cd "$(dirname "$0")/.."   # script lives in scripts/; operate from the repo root
ROOT="$(pwd)"

echo "=== [1/3] git pull (latest source) ==="
command -v git >/dev/null || { echo "ERROR: git not found"; exit 1; }
git pull --ff-only || { echo "ERROR: git pull failed (local edits? run 'git status')"; exit 1; }

echo "=== [2/3] build ==="
case "$(uname -s)" in
  Linux)  scripts/build-linux.sh ;;
  Darwin) scripts/build-macos.sh ;;
  *) echo "ERROR: unsupported OS $(uname -s)"; exit 1 ;;
esac

DEST="dist/cas"
[ -d "$DEST" ] || { echo "ERROR: $DEST not produced by the build"; exit 1; }

echo "=== [3/3] link runtime dirs into $DEST (symlinks; skips any not present) ==="
link() {  # link <relpath>
  if [ -e "$1" ]; then
    mkdir -p "$DEST/$(dirname "$1")"
    ln -sfn "$ROOT/$1" "$DEST/$1" && echo "  linked $1"
  fi
}
link platform-tools                 # linux/mac adb+fastboot, if you keep them here
link data/retroarch-cores
link data/profiles
link provision/root/firmware
link "data/ES-DE/downloaded_media"
link data/Apps                      # gamecove-companion.apk -> installed on every unit (install_companion)

echo "=== DONE — updated + rebuilt.  Run:  $DEST/cas-gui ==="
echo "  (golden library = NAS when mounted; else local profiles/)"
