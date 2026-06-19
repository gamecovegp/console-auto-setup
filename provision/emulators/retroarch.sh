# RetroArch (retro/arcade) — CLASS A config (PLAIN) + the OVERLAY-OFF / ROM-DIR settings.
# CORES are the exception: they live in internal /data/data (noexec on /sdcard) -> can't be cloned.
PKG=com.retroarch.aarch64
F=/sdcard/Android/data/$PKG/files
CFG=$F/retroarch.cfg
[ "${RESET:-0}" = 1 ] && { log "pm clear (fresh)"; SH "pm clear $PKG"; }
launch_first "$PKG"
grant_allfiles "$PKG"; grant_legacy "$PKG"
clone_into "$PAYLOAD/retroarch" "$F"        # retroarch.cfg (+ playlists) — carries input binds/hotkeys
# SETTINGS (RetroArch uses QUOTED values:  key = "value").  The cfg clone carries input binds,
# video_driver=gl, etc.; these setkeys ENFORCE the handheld-specific overrides + the per-unit ROM dir:
setkey "$CFG" "input_overlay_enable"  '"false"'                       # on-screen touch overlay OFF
setkey "$CFG" "input_overlay_hide_when_gamepad_connected" '"true"'    # (belt + suspenders for built-in pad)
setkey "$CFG" "input_overlay_opacity" '"0.000000"'
setkey "$CFG" "rgui_browser_directory" "\"/storage/$SDID/ROMs\""      # ROM dir, serial-correct for THIS unit
ok "RetroArch: cfg cloned (binds, gl driver) + overlay disabled + ROM dir = /storage/$SDID/ROMs."
warn "CORES are NOT clonable (internal /data/data). MANUAL: Online Updater > Core Downloader (GL-UI),"
warn "  OR route GC/Wii/3DS/DS to standalone emulators instead. ES-DE commands point at /data/data cores."
