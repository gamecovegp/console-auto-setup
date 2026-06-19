# melonDS (DS) — CLASS C: SAF ROM dir (uiauto grant). DS BIOS is internal -> see warning.
PKG=me.magnum.melonds.nightly
[ "${RESET:-0}" = 1 ] && { log "pm clear (fresh)"; SH "pm clear $PKG"; }
launch_first "$PKG"
grant_legacy "$PKG"
saf_grant "$PKG" "SET ROM DIRECTORY"                  # grants the NDS folder (picker opens at nds)
ok "melonDS: NDS ROM directory granted (game list populates)."
warn "DS BIOS (bios7.bin/bios9.bin/firmware.bin) is internal (wiped by reset). To BOOT games: in melonDS"
warn "  Settings, point BIOS files at the SD 'Bios' folder (SAF pick) OR enable FreeBIOS/HLE (DS mode)."
warn "DS/DSi mode is an in-app setting."
