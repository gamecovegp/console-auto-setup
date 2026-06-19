# Eden (Switch) — CLASS A: assets in own files/, ROM via ES-DE handoff (no folder grant needed).
PKG=dev.eden.eden_emulator
F=/sdcard/Android/data/$PKG/files
[ "${RESET:-0}" = 1 ] && { log "pm clear (fresh)"; SH "pm clear $PKG"; }
launch_first "$PKG"                         # app creates its dirs app-owned
grant_allfiles "$PKG"; grant_legacy "$PKG"
clone_into "$PAYLOAD/eden/keys"        "$F/keys"          # prod.keys (+ title.keys)
clone_into "$PAYLOAD/eden/nand/system" "$F/nand/system"   # firmware (registered NCAs)
clone_into "$PAYLOAD/eden/gpu_drivers" "$F/gpu_drivers"   # Turnip driver
clone_into "$PAYLOAD/eden/config"      "$F/config"        # config.ini carries: driver_path, gamedirs(SAF), settings
# SETTINGS: Eden stores each key as a PAIR  (key=value  +  key\default=true|false). Editing only the
# value can be IGNORED when \default=true, so the cloned config.ini (above) is AUTHORITATIVE — it carries
# the golden values correctly. Golden values (for reference): renderer Vulkan · resolution_setup=3 ·
# use_vsync=2 · aspect_ratio=0 · nvdec_emulation=2 · use_disk_shader_cache=true · driver_path=Turnip.
# The "disable on-screen overlay" toggle is an Android-pref (not in config.ini) -> set on the GOLDEN.
ok "Eden provisioned (keys, firmware, Turnip driver, config w/ golden settings). Launch Switch via ES-DE."
warn "Eden overlay-off + GPU-driver SELECTED are in-app prefs — confirm on the golden capture."
