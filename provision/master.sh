#!/usr/bin/env sh
# provision/master.sh — orchestrate the per-emulator provision scripts in fixed order.
# Runs identically over ADB (PC) or via rish (on-device Shizuku); see lib.sh for MODE detection.
#
# Usage:
#   ./master.sh                 # provision all emulators in order
#   ./master.sh eden retroarch  # only the named ones
#   CAS_MODE=local ./master.sh  # force on-device (Shizuku/rish) mode
#   RESET=1 ./master.sh eden    # pm clear first (simulate a fresh unit) — destructive
#   PAYLOAD=/storage/XXXX-XXXX/golden_payload ./master.sh   # override asset location
#
# Asset payload layout on the SD (read-only golden assets, captured once):
#   $PAYLOAD/eden/{keys,nand/system,gpu_drivers,config}
#   $PAYLOAD/dolphin/Config   $PAYLOAD/duckstation/bios   $PAYLOAD/nethersx2/bios
#   $PAYLOAD/retroarch/retroarch.cfg   $PAYLOAD/citra-emu/...   (melonds DS BIOS, ppsspp, etc.)
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/lib.sh"
detect_sd
[ -n "${SDPATH:-}" ] || { warn "no SD card detected"; exit 1; }
hdr "console-auto-setup provision  (MODE=$MODE  SD=$SDID  PAYLOAD=$PAYLOAD)"

# STEP 1 (fresh device): install emulator APKs from the SD.  INSTALL_APKS=1 default; 0 to skip on re-runs.
if [ "${INSTALL_APKS:-1}" = 1 ]; then hdr "install APKs"; ( . "$DIR/apks.sh" ) || warn "apks step error"; fi

# STEP 2: per-emulator provisioning (assets + grants + settings).
ORDER="esde retroarch eden citra dolphin duckstation nethersx2 flycast melonds ppsspp m64plus"
LIST="${*:-$ORDER}"
for e in $LIST; do
  s="$DIR/emulators/$e.sh"
  [ -f "$s" ] || { warn "no script for '$e' (skipping)"; continue; }
  hdr "$e"
  ( . "$s" ) || warn "$e: script reported an error"
done

# STEP 3 (production): seal the unit — uninstall Shizuku+Termux + delete scripts.  SEAL=1 to enable.
if [ "${SEAL:-0}" = 1 ]; then ( . "$DIR/cleanup.sh" ); fi

hdr "DONE"
log "Class A/B = automated. Class C SAF grants = automated via uiauto."
warn "MANUAL (GL-UI / in-app, can't be scripted): RetroArch core download; PPSSPP memstick GL prompts;"
warn "  melonDS DS BIOS pick; per-emulator in-app graphics/controls not stored in external config."
