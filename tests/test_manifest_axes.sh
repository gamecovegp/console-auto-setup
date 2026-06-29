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
[ "$fail" -eq 0 ] && { echo "PASS: manifest_axes"; exit 0; } || exit 1
