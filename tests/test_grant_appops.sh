#!/usr/bin/env bash
# Local smoke test for grant_special_appops (pure logic — no device). Stubs `dumpsys`/`appops` as shell
# functions so we exercise the declaration-driven grant + verify branches off-device.
# Run: bash tests/test_grant_appops.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
STATEF="$(mktemp)"; trap 'rm -f "$STATEF"' EXIT

# Stubs (override the device binaries grant_special_appops shells out to):
#   DECL  = what `dumpsys package` reports the app declares.
#   STICK = whether an `appops set` actually persists (1) or silently fails (0).
dumpsys(){ printf '%s\n' "$DECL"; }
appops(){ case "$1" in
    set) [ "${STICK:-1}" = 1 ] && echo "$2|$3=allow" >> "$STATEF";;
    get) grep -q "$2|$3=allow" "$STATEF" 2>/dev/null && echo "$3: allow" || echo "$3: default";;
  esac; }

# 1) app declares BOTH special appops and they stick -> rc 0, both granted
: > "$STATEF"; DECL="MANAGE_EXTERNAL_STORAGE REQUEST_INSTALL_PACKAGES"; STICK=1
grant_special_appops com.foo >/dev/null; rc=$?
[ "$rc" -eq 0 ]                                   || { echo "FAIL(1a): rc=$rc (want 0)"; fail=1; }
grep -q 'com.foo|MANAGE_EXTERNAL_STORAGE=allow'  "$STATEF" || { echo "FAIL(1b): all-files not granted"; fail=1; }
grep -q 'com.foo|REQUEST_INSTALL_PACKAGES=allow' "$STATEF" || { echo "FAIL(1c): install-unknown not granted"; fail=1; }

# 2) app declares NEITHER -> rc 0, nothing granted (no-op success; never grants what isn't declared)
: > "$STATEF"; DECL="android.permission.INTERNET"; STICK=1
grant_special_appops com.bar >/dev/null; rc=$?
[ "$rc" -eq 0 ]                  || { echo "FAIL(2a): rc=$rc (want 0)"; fail=1; }
[ ! -s "$STATEF" ]               || { echo "FAIL(2b): granted an undeclared appop"; fail=1; }

# 3) declares REQUEST_INSTALL_PACKAGES but the grant does NOT stick -> rc 1 (surfaces a silent failure)
: > "$STATEF"; DECL="REQUEST_INSTALL_PACKAGES"; STICK=0
out="$(grant_special_appops com.baz)"; rc=$?
[ "$rc" -eq 1 ]                                  || { echo "FAIL(3a): rc=$rc (want 1)"; fail=1; }
echo "$out" | grep -q 'REQUEST_INSTALL_PACKAGES NOT granted' || { echo "FAIL(3b): no warn on unverified grant"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: grant_special_appops"; exit 0; } || exit 1
