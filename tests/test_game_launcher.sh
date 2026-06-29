#!/usr/bin/env bash
# Local smoke test for game_launcher / gl_capture / gl_restore (no device). Stubs pm/am/chown/restorecon/stat
# and points DATA_ROOT at scratch trees. Run: bash tests/test_game_launcher.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# --- stubs for device binaries the helpers shell out to -----------------------------------------
INSTALLED=""                                   # space-separated pkgs pm should report as installed
pm(){ case "$1" in path) case " $INSTALLED " in *" $2 "*) return 0;; *) return 1;; esac;; *) return 0;; esac; }
am(){ :; }                                     # force-stop no-op
chown(){ :; }                                  # ownership no-op off-device
restorecon(){ :; }                             # relabel no-op off-device

# === game_launcher resolution order =============================================================
# (1) probe: a data dir with the DataStore signature wins over the list
mkdir -p "$tmp/data/com.handheld.launcher/files/datastore"
: > "$tmp/data/com.handheld.launcher/files/datastore/GameLauncher.preferences_pb"
got="$(DATA_ROOT="$tmp/data" game_launcher)"
[ "$got" = "com.handheld.launcher" ] || { echo "FAIL(1 probe): [$got]"; fail=1; }

# (2) override wins even when a different pkg would probe-hit
INSTALLED="com.oem.frontend"
got="$(DATA_ROOT="$tmp/data" game_launcher com.oem.frontend)"
[ "$got" = "com.oem.frontend" ] || { echo "FAIL(2 override): [$got]"; fail=1; }

# (3) no probe hit -> curated list fallback (pm-installed)
empty="$tmp/empty"; mkdir -p "$empty"
INSTALLED="com.handheld.launcher"
got="$(DATA_ROOT="$empty" game_launcher)"
[ "$got" = "com.handheld.launcher" ] || { echo "FAIL(3 list): [$got]"; fail=1; }

# (4) nothing matches -> empty
INSTALLED=""
got="$(DATA_ROOT="$empty" game_launcher)"
[ -z "$got" ] || { echo "FAIL(4 none): [$got]"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: game_launcher"; exit 0; } || exit 1
