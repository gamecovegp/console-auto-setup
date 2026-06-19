# ES-DE (frontend) — config lives on the SD (/storage/<sd>/ES-DE) and rides the card clone.
PKG=org.es_de.frontend
grant_allfiles "$PKG"
# ROMDirectory defaults to %ROMPATH% (= <sd>/ROMs) -> portable across cards. settings/custom_systems/
# gamelists all live under /storage/$SDID/ES-DE and clone with the SD. Nothing to push into the app.
ok "ES-DE: All-Files granted; ES-DE config rides the SD (settings, custom_systems, gamelists)."
warn "If ES-DE shows empty systems on a fresh unit, assign the SD card once (one-time SAF) — All-Files"
warn "  usually covers reads; ES-DE hands ROMs to emulators via its LaunchFileProvider at launch."
