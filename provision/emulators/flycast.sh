# Flycast (Dreamcast) — emu.cfg keys are PLAIN/editable; content path is SAF (ES-DE handoff covers launch).
PKG=com.flycast.emulator
F=/sdcard/Android/data/$PKG/files
[ "${RESET:-0}" = 1 ] && { log "pm clear (fresh)"; SH "pm clear $PKG"; }
launch_first "$PKG"
grant_legacy "$PKG"
clone_into "$PAYLOAD/flycast" "$F"        # emu.cfg + data (dc_nvmem, vmu saves) + mappings
# SETTINGS (golden values; emu.cfg uses unquoted "key = value"):
setkey "$F/emu.cfg" "pvr.rend" "4"                       # renderer = Vulkan
setkey "$F/emu.cfg" "VirtualGamepadTransparency" "0"     # on-screen gamepad hidden (overlay OFF)
ok "Flycast: emu.cfg cloned + Vulkan renderer + virtual-gamepad hidden."
warn "Dreamcast BIOS dc_boot.bin/dc_flash.bin is MISSING on golden — add to files/data (or point at SD Bios) to boot."
warn "Game path is SAF; ES-DE hands the ROM at launch. If Flycast's own browser is empty, add the folder in-app."
