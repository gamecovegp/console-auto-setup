#!/usr/bin/env bash
# Local smoke test for game_launcher / gl_capture / gl_restore (no device). Stubs pm/am/chown/restorecon
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

# (1b) probe also matches the databases/GAME_INFO signature
mkdir -p "$tmp/data_gi/com.oem.gi/databases"
: > "$tmp/data_gi/com.oem.gi/databases/GAME_INFO"
got="$(DATA_ROOT="$tmp/data_gi" game_launcher)"
[ "$got" = "com.oem.gi" ] || { echo "FAIL(1b GAME_INFO probe): [$got]"; fail=1; }

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

# === gl_capture: portable subtrees only, GAME_INFO excluded =====================================
src="$tmp/data/com.handheld.launcher"
mkdir -p "$src/files/datastore" "$src/databases" "$src/cache"
printf 'psx_select_emulator' > "$src/files/datastore/GameLauncher.preferences_pb"
: > "$src/databases/GAME_INFO"
: > "$src/cache/junk"
out="$tmp/out"; mkdir -p "$out"
DATA_ROOT="$tmp/data" gl_capture "$out" "com.handheld.launcher" >/dev/null || { echo "FAIL(cap rc)"; fail=1; }
tar -tf "$out/gamelauncher/config.tar" 2>/dev/null | grep -q 'files/datastore/GameLauncher.preferences_pb' \
  || { echo "FAIL(cap: datastore missing)"; fail=1; }
tar -tf "$out/gamelauncher/config.tar" 2>/dev/null | grep -q 'GAME_INFO' \
  && { echo "FAIL(cap: GAME_INFO leaked)"; fail=1; }
grep -q '^pkg=com.handheld.launcher$' "$out/gamelauncher/meta" || { echo "FAIL(cap: meta pkg)"; fail=1; }

# === gl_restore: extracts + verifies a preferences_pb under the target data dir ==================
dst="$tmp/restore"; mkdir -p "$dst/com.handheld.launcher"     # target app data dir exists (installed)
DATA_ROOT="$dst" gl_restore "$out" "com.handheld.launcher" >/dev/null || { echo "FAIL(res rc)"; fail=1; }
[ -f "$dst/com.handheld.launcher/files/datastore/GameLauncher.preferences_pb" ] \
  || { echo "FAIL(res: preferences_pb not written)"; fail=1; }

# gl_capture with a RELATIVE out_dir resolves correctly regardless of cwd (regression: Task 2 review Important)
mkdir -p "$tmp/relout"
( cd "$tmp" && DATA_ROOT="$tmp/data" gl_capture "relout" "com.handheld.launcher" >/dev/null )
tar -tf "$tmp/relout/gamelauncher/config.tar" >/dev/null 2>&1 || { echo "FAIL(cap: relative out misdirected)"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: game_launcher"; exit 0; } || exit 1
