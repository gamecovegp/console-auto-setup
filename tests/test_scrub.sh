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

# scrub.sh clears the overlay.d boot-grant diagnostic marker so it never ships on a sealed unit.
# (Static check: scrub.sh's is_root gate early-exits on a non-root dev box, so we can't run it here.)
grep -q 'rm -f /data/local/tmp/cas_boot_grant.done' "$ROOT/provision/root/scrub.sh" \
  || { echo "FAIL: scrub.sh does not clear the cas_boot_grant marker"; fail=1; }

# The boot-grant SCRIPT is staged on /data (the ramdisk copy can't survive switch_root), so unlike the
# ramdisk it outlives the seal. seal() reflashes stock init_boot, so no cas_grant service remains to run
# it -- but the file would still ship on a customer unit. Scrub it for the same reason as the marker.
grep -q 'rm -f /data/local/tmp/cas-grant.sh' "$ROOT/provision/root/scrub.sh" \
  || { echo "FAIL: scrub.sh does not clear the staged cas-grant.sh"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: scrub_members"; exit 0; } || exit 1
