# PPSSPP (PSP) — CLASS B storage, but GL-UI memstick prompt. Storage is scriptable; the memstick
# prompt's first OK + final OK are GL-rendered (synthetic taps unreliable) — the picker in between is
# system UI (uiauto-able). N.B. PPSSPP renders its whole UI in OpenGL.
PKG=org.ppsspp.ppsspp
[ "${RESET:-0}" = 1 ] && { log "pm clear (fresh)"; SH "pm clear $PKG"; }
launch_first "$PKG"
grant_legacy "$PKG"; grant_allfiles "$PKG"
warn "PPSSPP memstick = GL-UI. On the device: tap 'OK' with 'Create or Choose a PSP folder' -> the"
warn "  SYSTEM picker opens (uiauto can finish it). Then run:  ui_waittap 'USE THIS FOLDER'; ui_waittap '^ALLOW$'"
# Attempt the system-picker half automatically IF the picker is already open:
if [ "$(fg_activity | grep -c documentsui)" -gt 0 ]; then
  ui_waittap "USE THIS FOLDER" 10; ui_waittap "^ALLOW$" 10
fi
# Once memstick = /storage/$SDID/ROMs/psp, ppsspp.ini becomes editable (graphics/controls scriptable):
PSPINI="/storage/$SDID/ROMs/psp/PSP/SYSTEM/ppsspp.ini"
# Examples (uncomment/adjust to your golden): overlay/touch controls + buttons live here.
# setkey "$PSPINI" "ShowTouchControls" "False"
# setkey "$PSPINI" "iShowFPSCounter" "0"
ok "PPSSPP: storage granted. Memstick=ROMs/psp via the system picker; ppsspp.ini settings scriptable after."
warn "Final memstick 'OK' confirm is GL-UI -> human tap (1 sec)."
