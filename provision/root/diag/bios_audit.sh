#!/system/bin/sh
# bios_audit.sh — verify every emulator's BIOS / keys / firmware is covered by EITHER the payload
# (Android/data -> adata.tar) OR the SD card (rides the SD clone). Read-only.
. "$(dirname "$0")/../lib-root.sh"; is_root || { echo need root; exit 1; }
SD="$(detect_sd)"; P="$SD/golden_root_payload"
echo "===== 1) BIOS/keys/firmware on the SD CARD (rides the SD clone) ====="
find "$SD" -maxdepth 4 -type d \( -iname bios -o -iname keys -o -iname firmware -o -iname system \) 2>/dev/null | grep -vi golden_root_payload | grep -viE 'provision' | head -40
echo "--- key BIOS files on SD ---"
find "$SD/Bios" "$SD/bios" 2>/dev/null \( -iname '*.bin' -o -iname '*.keys' -o -iname '*.nca' -o -iname '*.nvm' -o -iname '*.mec' -o -iname '*.rom0' \) 2>/dev/null | head -40

echo; echo "===== 2) per-emulator BIOS/keys in Android/data (these are inside each adata.tar) ====="
for p in $(cat "$P/pkglist.txt" 2>/dev/null); do
  d="/sdcard/Android/data/$p"
  hits=$(find "$d" \( -ipath '*bios*' -o -ipath '*keys*' -o -ipath '*firmware*' -o -ipath '*registered*' -o -iname '*.nca' \) -type f 2>/dev/null | head -6)
  [ -n "$hits" ] && { echo "-- $p --"; echo "$hits"; }
done

echo; echo "===== 3) CONFIRM those are actually inside the captured payload tars ====="
for p in dev.eden.eden_emulator com.github.stenzek.duckstation xyz.aethersx2.tturnip com.flycast.emulator me.magnum.melonds.nightly; do
  echo "-- $p adata.tar --"
  tar -tf "$P/$p/adata.tar" 2>/dev/null | grep -iE 'bios|/keys/|prod.keys|title.keys|firmware|registered|\.nca$|\.bin$' | head -6
done
