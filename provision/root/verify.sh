#!/system/bin/sh
# verify.sh — post-restore correctness check on a provisioned unit. Read-only.
. "$(dirname "$0")/lib-root.sh"; is_root || { echo need root; exit 1; }
SD="$(detect_sd)"; SERIAL="${SD##*/}"
echo "===== apps installed vs payload set ====="
echo "installed 3rd-party: $(pm list packages -3 | grep -cvE 'magisk|termux|shizuku')   payload set: $(grep -c . $SD/golden_root_payload/pkglist.txt)"

echo; echo "===== SAF grants present (this unit) ====="
abx2xml /data/system/urigrants.xml - 2>/dev/null | grep -oE 'targetPkg="[^"]+"' | sort -u | sed 's/targetPkg=//'
echo "grant count: $(abx2xml /data/system/urigrants.xml - 2>/dev/null | grep -c uri-grant)   serial in grants: $(abx2xml /data/system/urigrants.xml - 2>/dev/null | grep -oE '[0-9A-F]{4}-[0-9A-F]{4}' | head -1)  (this SD: $SERIAL)"

echo; echo "===== Android/data ownership (THE fix) — should be <appuid>:1078 ====="
for p in dev.eden.eden_emulator com.github.stenzek.duckstation xyz.aethersx2.tturnip; do
  u=$(stat -c %u /data/data/$p 2>/dev/null)
  own=$(stat -c '%u:%g' /data/media/0/Android/data/$p 2>/dev/null)
  echo "  $p  app_uid=$u   Android/data=$own"
done

echo; echo "===== key/BIOS files present + readable ====="
ls -l /data/media/0/Android/data/dev.eden.eden_emulator/files/keys/prod.keys 2>/dev/null | awk '{print "  eden prod.keys:", $1, $3":"$4, $5"b"}'
ls /data/media/0/Android/data/com.github.stenzek.duckstation/files/bios/ 2>/dev/null | sed 's/^/   duckstation bios: /'
ls /data/media/0/Android/data/xyz.aethersx2.tturnip/files/bios/ 2>/dev/null | sed 's/^/  nethersx2 bios: /'

echo; echo "===== cores / citra-internal / settings applied ====="
echo "  retroarch cores: $(ls /data/data/com.retroarch.aarch64/cores/*.so 2>/dev/null | grep -c .)"
echo "  citra-emu internal: $([ -d /storage/emulated/0/citra-emu/sysdata ] && echo present || echo MISSING)"
echo "  screen_off_timeout=$(settings get system screen_off_timeout)  anim=$(settings get global window_animation_scale)"
echo "  deviceidle whitelisted emulators: $(dumpsys deviceidle whitelist 2>/dev/null | grep -cE 'eden|retroarch|duckstation|citra|dolphin|flycast|melonds|ppsspp|mupen64|aethersx2')"
echo "  ota app com.odin.fota state: $(pm list packages -d | grep -c com.odin.fota) (1=disabled)"
