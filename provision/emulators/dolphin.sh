# Dolphin (GameCube/Wii) — CLASS C: SAF library, needs its own folder grant (uiauto macro).
# SETTINGS are in external files/Config and ride the clone: GFX.ini (graphics), GCPadNew.ini
# (BUTTON MAPPING), Hotkeys.ini, Dolphin.ini (ISOPath). No on-screen overlay in Dolphin.
PKG=org.dolphinemu.dolphinemu
F=/sdcard/Android/data/$PKG/files
[ "${RESET:-0}" = 1 ] && { log "pm clear (fresh)"; SH "pm clear $PKG"; }
launch_first "$PKG"
grant_legacy "$PKG"
clone_into "$PAYLOAD/dolphin/Config" "$F/Config"     # graphics + button mapping + hotkeys + ISOPath
saf_grant "$PKG" "Add Games"                          # grants the GameCube folder (picker opens at gc)
ok "Dolphin: Config cloned (graphics+buttons+hotkeys); GC folder granted."
warn "Wii: run a 2nd folder add for ROMs/wii (Add Games -> navigate to wii -> Use this folder -> Allow)."
