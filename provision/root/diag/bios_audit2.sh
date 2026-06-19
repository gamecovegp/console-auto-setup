#!/system/bin/sh
# bios_audit2.sh — close the loop on Dreamcast (Flycast) + DS (melonDS) BIOS, and Citra keys.
. "$(dirname "$0")/../lib-root.sh"; is_root || { echo need root; exit 1; }
SD="$(detect_sd)"; P="$SD/golden_root_payload"
echo "=== Flycast: Dreamcast BIOS (dc_boot/dc_flash) anywhere? ==="
find /sdcard/Android/data/com.flycast.emulator "$SD/Bios" "$SD/bios" /storage/emulated/0 -iname 'dc_boot.bin' -o -iname 'dc_flash.bin' 2>/dev/null | grep -v golden_ | head
echo "--- what flycast's data dir actually holds (this is its data.tar) ---"
ls -R /sdcard/Android/data/com.flycast.emulator/files 2>/dev/null | head -30
echo "--- flycast config: where it looks for BIOS ---"
grep -rhiE 'bios|dreamcast|dc_boot|image|path' /data/data/com.flycast.emulator/shared_prefs 2>/dev/null | head

echo; echo "=== melonDS: DS BIOS (bios7/bios9/firmware) anywhere? ==="
find /sdcard/Android/data/me.magnum.melonds.nightly "$SD/Bios" "$SD/bios" -iname 'bios7*' -o -iname 'bios9*' -o -iname 'firmware.bin' 2>/dev/null | grep -v golden_ | head
echo "--- melonds config (freebios vs external?) ---"
grep -rhiE 'bios|freebios|firmware|extBios|dsiBios' /data/data/me.magnum.melonds.nightly 2>/dev/null | head

echo; echo "=== Citra: 3DS aes_keys / seeddb (in citra-emu internal, already captured) ==="
find /storage/emulated/0/citra-emu -iname 'aes_keys.txt' -o -iname 'seeddb.bin' -o -ipath '*sysdata*' 2>/dev/null | head
echo "--- in internal_citra-emu.tar payload? ---"
tar -tf "$P/internal_citra-emu.tar" 2>/dev/null | grep -iE 'aes_keys|seeddb|sysdata|nand' | head
