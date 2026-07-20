#!/usr/bin/env bash
# Runs cas-grant.sh (the overlay.d boot-grant, run AS ROOT at boot) against a STUB `magisk`, with the
# marker + retry bounds redirected off-device via CAS_GRANT_MARK/TRIES/SLEEP. Asserts it issues the
# right --sqlite writes and leaves the correct diagnostic marker. No device. Covers:
#   A) magisk on PATH, daemon ready           -> writes both policies + "cas-grant ok policy=2" marker
#   B) magisk off PATH via CAS_MAGISK, ready   -> same (models the boot PATH where the applet dir is absent)
#   C) daemon never ready                      -> no policy writes, "cas-grant daemon-not-ready" marker
set -u
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/../provision/root/overlay/cas-grant.sh"

make_stub() {  # $1 = path to create the magisk stub at
  # DAEMON_READY=1 (default): `--sqlite "SELECT 1"` succeeds so the loop proceeds; the policy read-back
  # returns 2. DAEMON_READY=0: `SELECT 1` fails (exit 1), modelling magiskd not up yet.
  cat > "$1" <<'STUB'
#!/usr/bin/env bash
echo "$@" >> "$MAGISK_LOG"
case "$*" in
  *"SELECT 1"*) [ "${DAEMON_READY:-1}" = 1 ] && echo 1 || exit 1 ;;
  # Real `magisk --sqlite` prints key=value, NOT a bare value -- verified on an AYN Thor 2026-07-20:
  #   SELECT policy FROM policies WHERE uid=2000  ->  policy=2
  #   SELECT * FROM policies                      ->  uid=2000|policy=2|until=0|...
  # The stub used to echo a bare `2`, which hid a real double-prefix bug ("ok policy=policy=2").
  *"SELECT policy FROM policies WHERE uid=2000"*) echo "policy=2" ;;
esac
STUB
  chmod +x "$1"
}

check_ok() {  # $1 = scenario label; asserts the granted state against $MAGISK_LOG and $MARK
  fail=0
  grep -q "REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)" "$MAGISK_LOG" \
    || { echo "[X] $1: missing shell allow-policy write"; fail=1; }
  grep -q "REPLACE INTO settings (key,value) VALUES('root_access',3)" "$MAGISK_LOG" \
    || { echo "[X] $1: missing root_access=3 write"; fail=1; }
  # Exact match, not substring: a substring test still passed the malformed "ok policy=policy=2".
  grep -qx "cas-grant ok policy=2" "$MARK" \
    || { echo "[X] $1: missing/incorrect success marker: $(cat "$MARK" 2>/dev/null)"; fail=1; }
  return $fail
}

rc=0

# Scenario A: magisk on PATH, daemon ready.
tmpA="$(mktemp -d)"; make_stub "$tmpA/magisk"
export MAGISK_LOG="$tmpA/log"; : > "$MAGISK_LOG"
export MARK="$tmpA/marker"
CAS_GRANT_MARK="$MARK" PATH="$tmpA:$PATH" sh "$script"
check_ok "on-PATH" || rc=1
rm -rf "$tmpA"

# Scenario B: magisk NOT on PATH ($tmpB is NOT added to PATH); resolved only via CAS_MAGISK.
tmpB="$(mktemp -d)"; make_stub "$tmpB/magisk"
export MAGISK_LOG="$tmpB/log"; : > "$MAGISK_LOG"
export MARK="$tmpB/marker"
CAS_GRANT_MARK="$MARK" CAS_MAGISK="$tmpB/magisk" sh "$script"
check_ok "off-PATH-fallback" || rc=1
rm -rf "$tmpB"

# Scenario C: daemon never ready -> bounded retry exhausts, no policy writes, daemon-not-ready marker.
tmpC="$(mktemp -d)"; make_stub "$tmpC/magisk"
export MAGISK_LOG="$tmpC/log"; : > "$MAGISK_LOG"
export MARK="$tmpC/marker"
CAS_GRANT_MARK="$MARK" CAS_GRANT_TRIES=2 CAS_GRANT_SLEEP=0 DAEMON_READY=0 PATH="$tmpC:$PATH" sh "$script"
if grep -q "cas-grant daemon-not-ready" "$MARK" \
   && ! grep -q "REPLACE INTO" "$MAGISK_LOG"; then
  :
else
  echo "[X] daemon-not-ready: expected daemon-not-ready marker and NO policy writes; marker=$(cat "$MARK" 2>/dev/null)"; rc=1
fi
rm -rf "$tmpC"

# Scenario D: the applet is reachable ONLY via the built-in probe list -- not on PATH, no CAS_MAGISK.
# This is the real on-device shape: at boot `magisk` is not on PATH and /data/adb/magisk/ is EMPTY
# (Magisk v30.7 stashes the applet in the /debug_ramdisk tmpfs). Before the fix the probe list missed
# it entirely, so every --sqlite call failed and the run ended "daemon-not-ready" instead of granting.
tmpD="$(mktemp -d)"; make_stub "$tmpD/magisk"
export MAGISK_LOG="$tmpD/log"; : > "$MAGISK_LOG"
export MARK="$tmpD/marker"
CAS_GRANT_MARK="$MARK" CAS_MAGISK_PATHS="$tmpD/magisk" sh "$script"
check_ok "probe-list-path" || rc=1
rm -rf "$tmpD"

# Scenario E (static): the DEFAULT probe list must name the real on-device applet location. Guards the
# regression directly -- scenario D passes with any overridable list, this pins the shipped default.
grep -q '/debug_ramdisk/magisk' "$script" \
  || { echo "[X] default-probe-list: cas-grant.sh must probe /debug_ramdisk/magisk (Magisk's tmpfs stash)"; rc=1; }

[ "$rc" = 0 ] && echo "ok: cas-grant.sh (on-PATH + fallback + daemon-not-ready + probe-list + default-probe)" || exit 1
