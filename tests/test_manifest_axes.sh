#!/usr/bin/env bash
# Local test for manifest_axes (pure text — no device). Run: bash tests/test_manifest_axes.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
printf '# h\ncom.foo\ncom.bar apk\nxyz.aethersx2.android config\ncom.baz config apk\n@settings on\n' > "$tmp/m"

check(){ got="$(manifest_axes "$tmp/m" "$1")"; [ "$got" = "$2" ] || { echo "FAIL $1: [$got] != [$2]"; fail=1; }; }
check com.foo "apk config"
check com.bar "apk"
check xyz.aethersx2.android "config"
check com.baz "apk config"
check com.absent ""
wants(){ manifest_wants "$tmp/m" "$1" "$2" && echo yes || echo no; }
[ "$(wants com.foo apk)" = yes ]    || { echo "FAIL wants(foo,apk)"; fail=1; }     # bare = both
[ "$(wants com.foo config)" = yes ] || { echo "FAIL wants(foo,config)"; fail=1; }
[ "$(wants com.bar apk)" = yes ]    || { echo "FAIL wants(bar,apk)"; fail=1; }     # 'com.bar apk'
[ "$(wants com.bar config)" = no ]  || { echo "FAIL wants(bar,config)"; fail=1; }
[ "$(wants xyz.aethersx2.android config)" = yes ] || { echo "FAIL wants(aeth,config)"; fail=1; }
[ "$(wants xyz.aethersx2.android apk)" = no ]     || { echo "FAIL wants(aeth,apk)"; fail=1; }
[ "$fail" -eq 0 ] && { echo "PASS: manifest_axes"; exit 0; } || exit 1
