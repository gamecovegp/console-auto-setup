#!/usr/bin/env bash
# Local smoke test for homescreen self-containment helpers (no device). Sources lib-root.sh,
# stubs device binaries, uses scratch trees. Run: bash tests/test_homescreen_apps.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# === homescreen_apps: extract component=/package= tokens, dedup, launcher NOT special-cased here =====
ld="$tmp/launcher_data/com.launch.home"; mkdir -p "$ld/databases"
# a favorites DB stored as plaintext intents (Launcher3-family shape): two icons for com.foo (dedup),
# one for com.bar via a package= column, plus the launcher's own component.
printf '%s\n' \
  '#Intent;component=com.foo/.MainActivity;end' \
  '#Intent;component=com.foo/.OtherActivity;end' \
  'itemType=1;package=com.bar;' \
  '#Intent;component=com.launch.home/.Home;end' > "$ld/databases/launcher.db"
got="$(homescreen_apps "$ld" | tr '\n' ' ')"
[ "$got" = "com.bar com.foo com.launch.home " ] || { echo "FAIL(extract+dedup): [$got]"; fail=1; }

# a launcher whose DB holds no such tokens -> empty result, rc 0 (degrade gracefully)
ld2="$tmp/blob/com.blob"; mkdir -p "$ld2"
printf '\x00\x01\x02binaryjunkno-tokens-here\xff' > "$ld2/launcher.db"
got2="$(homescreen_apps "$ld2")"; rc2=$?
[ -z "$got2" ] && [ "$rc2" -eq 0 ] || { echo "FAIL(blob): [$got2] rc=$rc2"; fail=1; }

# a missing dir -> empty, rc 0
got3="$(homescreen_apps "$tmp/does-not-exist")"; rc3=$?
[ -z "$got3" ] && [ "$rc3" -eq 0 ] || { echo "FAIL(missing dir): [$got3] rc=$rc3"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: homescreen_apps"; exit 0; } || exit 1
