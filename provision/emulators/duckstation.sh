# DuckStation (PS1) — CLASS C: SAF library + external BIOS. uiauto grants the folder.
PKG=com.github.stenzek.duckstation
F=/sdcard/Android/data/$PKG/files
[ "${RESET:-0}" = 1 ] && { log "pm clear (fresh)"; SH "pm clear $PKG"; }
launch_first "$PKG"
grant_legacy "$PKG"
clone_into "$PAYLOAD/duckstation/bios" "$F/bios"     # PS1 BIOS (read-only, clones fine)
saf_grant "$PKG" "ADD GAME DIRECTORY"                 # grants the PS1 folder (picker opens at psx)
ok "DuckStation: BIOS cloned + PS1 folder granted."
warn "Fast-Boot ON + Controller=Analog(DualShock) live in /data/data settings.ini (NOT pushable) ->"
warn "  set once on the GOLDEN; on a fresh unit they default (games still boot, may need these toggles in-app)."
