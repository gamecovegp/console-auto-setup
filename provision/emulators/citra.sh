# Citra MMJ (3DS) — CLASS B: legacy storage (pm grant) + config lives OUTSIDE app scope.
# Best case: config at /sdcard/citra-emu SURVIVES pm clear -> clone it + grant = done, ZERO UI.
PKG=org.citra.emu
[ "${RESET:-0}" = 1 ] && { log "pm clear (fresh)"; SH "pm clear $PKG"; }
grant_legacy "$PKG"                                   # legacy + A13 READ_MEDIA_* (the key step)
clone_into "$PAYLOAD/citra-emu" "/sdcard/citra-emu"   # config-mmj.ini etc. (graphics API, resolution, game path)
# SETTINGS: graphics API / internal resolution / shaders / overlay all live in config-mmj.ini (cloned
# above, authoritative). game_storage_path is a PLAIN "/storage" path -> portable across cards (no serial
# to rewrite). Enforce it explicitly if you ever need to:
# setkey "/sdcard/citra-emu/config/config-mmj.ini" "game_storage_path" "/storage;"
launch_first "$PKG"                                   # rescans; game list should populate
ok "Citra provisioned (config cloned + legacy/media storage granted). Games list automatically."
