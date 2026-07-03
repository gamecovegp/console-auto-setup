#!/system/bin/sh
# grant-persist.sh — run AS ROOT (via su) right after the FIRST shell grant. Makes the MagiskSU
# shell grant PERMANENT and sets Magisk's global root access, so no unit ever re-prompts.
# Exit codes are unreliable on these units, so success is a stdout sentinel (like boot_patch.sh).
#
# Resolve the magisk applet. CAS runs this via `su -c sh <script>`, where the applet dir is NOT on
# PATH — a bare `magisk` fails "inaccessible or not found" and the policy below is never written, so
# the unit re-prompts for root after a reboot. Try an explicit override, then PATH (dev/test), then
# the standard install location.
MAGISK=magisk
for _c in "${CAS_MAGISK:-}" magisk /data/adb/magisk/magisk; do
  [ -n "$_c" ] && command -v "$_c" >/dev/null 2>&1 && { MAGISK="$_c"; break; }
done
# shell uid 2000 = ALLOW (policy 2), forever (until 0), no logging/notification. All-numeric
# VALUES -> no inner quoting to fight through adb/su.
"$MAGISK" --sqlite "REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)"
# Global: apps AND adb may hold root (root_access 3). Best-effort; the policy row above is the
# load-bearing guarantee for the adb shell.
"$MAGISK" --sqlite "REPLACE INTO settings (key,value) VALUES('root_access',3)"
# Read the shell policy back so the PC can confirm it stuck; emit the sentinel + the read-back.
echo "CAS_GRANT $("$MAGISK" --sqlite "SELECT policy FROM policies WHERE uid=2000")"
