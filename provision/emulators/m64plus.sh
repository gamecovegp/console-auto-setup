# M64Plus FZ (N64) — CLASS B: legacy storage (pm grant). N64 needs no BIOS -> ES-DE hands the ROM.
PKG=org.mupen64plusae.v3.fzurita
[ "${RESET:-0}" = 1 ] && { log "pm clear (fresh)"; SH "pm clear $PKG"; }
launch_first "$PKG"
grant_legacy "$PKG"
ui_waittap "^ALLOW$" 6 || true        # dismiss the notification-permission dialog if it appears
ok "M64Plus: storage granted; license auto-accepted. Ready for ES-DE launches (N64 needs no BIOS)."
warn "Own-gallery folder (Settings>Library) + control profile are in /data/data -> set on GOLDEN; not"
warn "  needed for ES-DE launching. Some prefs may not carry per unit (this is the one /data/data-only app)."
