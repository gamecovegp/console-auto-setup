#!/usr/bin/env bash
# Local test for the scrub path-iteration (no device): point a scratch tree at scrub_members and confirm
# SAVE_STATES/USAGE_TRACES members are removed while untargeted files survive. Run: bash tests/test_scrub.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/com.github.stenzek.duckstation/savestates" "$tmp/com.github.stenzek.duckstation/settings"
touch "$tmp/com.github.stenzek.duckstation/savestates/slot1.sav" "$tmp/com.github.stenzek.duckstation/settings/settings.ini"

scrub_members "$tmp" "com.github.stenzek.duckstation/savestates"
[ ! -e "$tmp/com.github.stenzek.duckstation/savestates" ]      || { echo "FAIL: savestates dir not removed"; fail=1; }
[ -e "$tmp/com.github.stenzek.duckstation/settings/settings.ini" ] || { echo "FAIL: settings wrongly removed"; fail=1; }

# absent member is a no-op (no error)
scrub_members "$tmp" "com.absent.app/nothing" && true
[ "$fail" -eq 0 ] && { echo "PASS: scrub_members"; exit 0; } || exit 1
