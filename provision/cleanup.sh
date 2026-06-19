# cleanup.sh — SEAL a finished unit: remove ALL provisioning tooling so the retail device is clean.
# Deletes the provision scripts + golden payload/backups/apks from the SD, then uninstalls the two
# bootstrap apps (Termux, then Shizuku LAST — uninstalling Shizuku ends the rish session).
# KEEPS: ROMs, ES-DE config, Bios, game saves, the emulators themselves.
# Run this as the final step (or via `SEAL=1 master.sh`). Idempotent.
hdr "cleanup / seal  (removing provisioning tooling — keeping ROMs/ES-DE/emulators)"

# 1. delete provisioning files from the SD (NOT ROMs/ES-DE/Bios/games)
for p in provision golden_payload eden_payload apks; do
  SH "rm -rf \"$SDPATH/$p\"" && log "removed $SDPATH/$p"
done
SH "rm -rf \"$SDPATH\"/golden_backup_* 2>/dev/null" && log "removed golden_backup_*"

# 2. uninstall the bootstrap apps (Termux first; Shizuku last because it kills this session)
if [ -n "$(SH 'pm path com.termux 2>/dev/null')" ]; then SH "pm uninstall com.termux" >/dev/null 2>&1 && log "uninstalled Termux"; fi
if [ "${KEEP_SHIZUKU:-0}" = 1 ]; then
  warn "KEEP_SHIZUKU=1 -> leaving Shizuku installed (re-provisioning later)."
else
  warn "uninstalling Shizuku now — the rish session ends here. The retail unit is sealed."
  SH "pm uninstall moe.shizuku.privileged.api" >/dev/null 2>&1
fi
ok "SEAL complete. Reboot the unit; verify ROMs boot from ES-DE and no provisioning apps remain."
