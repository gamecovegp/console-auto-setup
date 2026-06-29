# NetherSX2 (PS2) — CLASS C: SAF library + external BIOS. uiauto grants the folder.
# PKG = our NetherSX2-Turnip APK repackaged under the stock AetherSX2 application id, so OEM game
# launchers (which hardwire PS2 -> xyz.aethersx2.android and ignore /sdcard/ES-DE config) launch it.
# Turnip is still bundled in that repackaged APK. The .tturnip rename is no longer used.
PKG=xyz.aethersx2.android
F=/sdcard/Android/data/$PKG/files
[ "${RESET:-0}" = 1 ] && { log "pm clear (fresh)"; SH "pm clear $PKG"; }
launch_first "$PKG"
grant_legacy "$PKG"
clone_into "$PAYLOAD/nethersx2/bios" "$F/bios"       # PS2 BIOS (.bin/.mec/.nvm)
saf_grant "$PKG" "ADD GAME DIRECTORY"                 # grants the PS2 folder (picker opens at ps2)
ok "NetherSX2: BIOS cloned + PS2 folder granted."
warn "Renderer=Vulkan/Turnip + controller mapping live in /data/data PCSX2.ini (NOT pushable) ->"
warn "  set once on the GOLDEN; verify in-app on a fresh unit. (Turnip is bundled in the APK.)"
