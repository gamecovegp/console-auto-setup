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

# === homescreen_bundle_apps: bundle referenced apps' APKs, skip launcher + already-captured ==========
apkroot="$tmp/apks"; mkdir -p "$apkroot"
: > "$apkroot/foo-base.apk"; : > "$apkroot/bar-base.apk"; : > "$apkroot/launch-base.apk"
# pm path stub: map each pkg to a scratch APK file that exists
pm(){ case "$1" in
        path) case "$2" in
                com.foo)         echo "package:$apkroot/foo-base.apk";;
                com.bar)         echo "package:$apkroot/bar-base.apk";;
                com.launch.home) echo "package:$apkroot/launch-base.apk";;
                *) return 1;; esac;;
        *) return 0;; esac; }

bld="$tmp/bld/com.launch.home"; mkdir -p "$bld/databases"
printf '%s\n' \
  '#Intent;component=com.foo/.Main;end' \
  'package=com.bar;' \
  '#Intent;component=com.launch.home/.Home;end' \
  '#Intent;component=com.nopath/.X;end' > "$bld/databases/launcher.db"   # com.nopath: pm path fails -> skipped
pay="$tmp/payload"; mkdir -p "$pay/com.bar/apk"                          # com.bar already captured -> skipped
n="$(homescreen_bundle_apps "$bld" "$pay" "com.launch.home")"
[ "$n" = "1" ] || { echo "FAIL(bundle count): [$n]"; fail=1; }
[ -f "$pay/homescreen/apps/com.foo/foo-base.apk" ] || { echo "FAIL(bundle: com.foo not bundled)"; fail=1; }
[ ! -d "$pay/homescreen/apps/com.bar" ]         || { echo "FAIL(bundle: com.bar should be skipped, already captured)"; fail=1; }
[ ! -d "$pay/homescreen/apps/com.launch.home" ] || { echo "FAIL(bundle: launcher should be skipped)"; fail=1; }
[ ! -d "$pay/homescreen/apps/com.nopath" ]      || { echo "FAIL(bundle: no-path pkg should be skipped)"; fail=1; }
unset -f pm

# === homescreen_install_missing: install only ABSENT placed apps (additive) =========================
pay2="$tmp/payload2"; mkdir -p "$pay2/homescreen/apps/com.present" "$pay2/homescreen/apps/com.absent"
: > "$pay2/homescreen/apps/com.present/base.apk"; : > "$pay2/homescreen/apps/com.absent/base.apk"
pm(){ case "$1" in
        path) case "$2" in com.present) return 0;; *) return 1;; esac;;   # only com.present is installed
        *) return 0;; esac; }
INSTALL_LOG="$tmp/install.log"; : > "$INSTALL_LOG"
install_apks(){ echo "$2" >> "$INSTALL_LOG"; return 0; }                    # stub: record which pkg we install
homescreen_install_missing "$pay2" || { echo "FAIL(install_missing rc)"; fail=1; }
[ "$(cat "$INSTALL_LOG" 2>/dev/null)" = "com.absent" ] || { echo "FAIL(install_missing: wrong set): [$(tr '\n' ' ' < "$INSTALL_LOG")]"; fail=1; }
unset -f pm install_apks

# no homescreen/apps dir -> rc 0, no error
homescreen_install_missing "$tmp/payload" || { echo "FAIL(install_missing: no-apps dir rc)"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: homescreen_apps"; exit 0; } || exit 1
