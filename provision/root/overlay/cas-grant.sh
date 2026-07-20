#!/system/bin/sh
# cas-grant.sh -- baked into the Magisk-patched init_boot via overlay.d and started AS ROOT at boot
# (see init.cas-grant.rc). Pre-writes the MagiskSU shell-allow policy so the adb shell's first `su`
# never trips the on-device Grant dialog -- zero-touch, first boot or ever. Mirrors grant-persist.sh,
# but runs from inside the device at boot instead of after a PC-driven grant.
#
# Marker (/data/local/tmp/cas_boot_grant.done) is a bench diagnostic: ABSENT after boot => the
# service never ran (overlay.d not honored on this magiskinit); PRESENT with "daemon-not-ready" =>
# it ran but magiskd wasn't up in time. Exit codes on these units are unreliable, so we never rely
# on rc -- the marker is the signal.
# CAS_GRANT_MARK / CAS_GRANT_TRIES / CAS_GRANT_SLEEP override the device defaults for the shell test
# (tests/test_cas_grant.sh); on-device they are unset, so behaviour is unchanged.
MARK="${CAS_GRANT_MARK:-/data/local/tmp/cas_boot_grant.done}"

# Resolve the magisk applet (not on PATH at boot). CAS_MAGISK / CAS_MAGISK_PATHS override for tests.
# /debug_ramdisk/magisk FIRST: on Magisk v30.7 that tmpfs is where the applet actually lives (alongside
# magiskinit, su -> ./magisk, resetprop) and /data/adb/magisk/ is EMPTY -- verified on an AYN Thor
# 2026-07-20. Probing only PATH + /data/adb/magisk/magisk found nothing, so every --sqlite call failed
# and the run ended "daemon-not-ready" instead of granting. Same path cas/adb.py already uses for su.
MAGISK=magisk
for c in "${CAS_MAGISK:-}" ${CAS_MAGISK_PATHS:-/debug_ramdisk/magisk /data/adb/magisk/magisk} magisk; do
  [ -n "$c" ] && command -v "$c" >/dev/null 2>&1 && { MAGISK="$c"; break; }
done

# magiskd / magisk.db may not be ready the instant we fire -- retry a bounded number of times.
i=0
while [ "$i" -lt "${CAS_GRANT_TRIES:-10}" ]; do
  if "$MAGISK" --sqlite "SELECT 1" >/dev/null 2>&1; then
    # shell uid 2000 = ALLOW (policy 2) forever (until 0), no logging/notification.
    "$MAGISK" --sqlite "REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)"
    # global: apps AND adb may hold root.
    "$MAGISK" --sqlite "REPLACE INTO settings (key,value) VALUES('root_access',3)"
    # `magisk --sqlite` prints key=value ("policy=2"), so interpolating it raw produced the malformed
    # marker "cas-grant ok policy=policy=2". Strip the prefix if present, so this reads the same whether
    # the applet returns "policy=2" or a bare "2".
    POL=$("$MAGISK" --sqlite "SELECT policy FROM policies WHERE uid=2000")
    echo "cas-grant ok policy=${POL#policy=}" > "$MARK"
    exit 0
  fi
  i=$((i + 1))
  sleep "${CAS_GRANT_SLEEP:-2}"
done
echo "cas-grant daemon-not-ready" > "$MARK"
exit 0
