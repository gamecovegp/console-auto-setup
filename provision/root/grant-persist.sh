#!/system/bin/sh
# grant-persist.sh — run AS ROOT (via su) right after the FIRST shell grant. Makes the MagiskSU
# shell grant PERMANENT and sets Magisk's global root access, so no unit ever re-prompts.
# Exit codes are unreliable on these units, so success is a stdout sentinel (like boot_patch.sh).
#
# shell uid 2000 = ALLOW (policy 2), forever (until 0), no logging/notification. All-numeric
# VALUES -> no inner quoting to fight through adb/su.
magisk --sqlite "REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)"
# Global: apps AND adb may hold root (root_access 3). Best-effort; the policy row above is the
# load-bearing guarantee for the adb shell.
magisk --sqlite "REPLACE INTO settings (key,value) VALUES('root_access',3)"
# Read the shell policy back so the PC can confirm it stuck; emit the sentinel + the read-back.
echo "CAS_GRANT $(magisk --sqlite "SELECT policy FROM policies WHERE uid=2000")"
