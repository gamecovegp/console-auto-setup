#!/system/bin/sh
# capture.sh — run AS ROOT on the GOLDEN, once. Captures every emulator's state to the SD.
#   adb shell su -c 'sh /storage/<sd>/provision/root/capture.sh'
# Output: <sd>/golden_root_payload/  (per-app tarballs + grants + serial). tar preserves owner/perms
# (the SD/FUSE layer would otherwise strip them); SELinux contexts are re-applied on restore.
DIR="$(cd "$(dirname "$0")" && pwd)"; . "$DIR/lib-root.sh"
is_root || { echo "must run as root (su)"; exit 1; }
SD="$(detect_sd)"
# capture target: $CAS_OUT (internal temp, for PC pull) wins; else on-SD (back-compat).
P="${CAS_OUT:-$SD/golden_root_payload}"
mkdir -p "$P"
CFAIL=0   # count capture problems; exit non-zero at the end so a corrupt/empty payload is never trusted
# global.meta: SD serial + the golden's locale/timezone, so restore can CLONE them onto each unit
# (instead of a hardcoded default). getprop is the source of truth for the persisted tz/locale.
{ echo "golden_serial=${SD##*/}"
  echo "golden_tz=$(getprop persist.sys.timezone 2>/dev/null)"
  echo "golden_locale=$(getprop persist.sys.locale 2>/dev/null)"
} > "$P/global.meta"
user_pkgs > "$P/pkglist.txt"                      # exact app set we clone: ALL 3rd-party minus host tools
ok "cloning $(grep -c . "$P/pkglist.txt") apps: $(tr '\n' ' ' < "$P/pkglist.txt")"
for pkg in $(cat "$P/pkglist.txt"); do
  [ -d "/data/data/$pkg" ] || { warn "$pkg not installed — skip"; continue; }
  mkdir -p "$P/$pkg/apk"
  # bundle the app's EXACT installed APK into the payload (self-contained root clone — version-exact,
  # portable, no dependency on the SD's /apps folder). pm path = base + any splits.
  for ap in $(pm path "$pkg" 2>/dev/null | sed 's/^package://'); do cp "$ap" "$P/$pkg/apk/" 2>/dev/null; done
  # internal app data (skip caches): settings, key binds, mappings, cores (RetroArch), grants-references
  tar -cf "$P/$pkg/data.tar" -C /data/data --exclude="$pkg/cache" --exclude="$pkg/code_cache" "$pkg" 2>/dev/null \
    || tar -cf "$P/$pkg/data.tar" -C /data/data "$pkg" 2>/dev/null    # fallback if --exclude unsupported
  tar -tf "$P/$pkg/data.tar" >/dev/null 2>&1 || { warn "data.tar looks corrupt: $pkg"; CFAIL=$((CFAIL+1)); }
  # external app data: firmware, BIOS, keys, driver
  if [ -d "/sdcard/Android/data/$pkg" ]; then
    tar -cf "$P/$pkg/adata.tar" -C /sdcard/Android/data "$pkg" 2>/dev/null
    tar -tf "$P/$pkg/adata.tar" >/dev/null 2>&1 || { warn "adata.tar looks corrupt: $pkg"; CFAIL=$((CFAIL+1)); }
  fi
  # large native-game expansion files (OBB) — some GameHub PC-game ports need them
  [ -d "/sdcard/Android/obb/$pkg" ] && tar -cf "$P/$pkg/obb.tar" -C /sdcard/Android/obb "$pkg" 2>/dev/null
  echo "golden_uid=$(app_uid "$pkg")" > "$P/$pkg/meta"
  ok "captured $pkg"
done
# shared internal-storage dirs (Citra/RetroArch keep state here, OUTSIDE app-private dirs; a factory
# reset wipes internal storage, so the self-contained payload must carry them).
for d in $INTERNAL_DIRS; do
  # skip if absent OR empty (e.g. ES-DE before its home is actually moved to internal — avoids a
  # stale/empty capture that would create a broken empty dir on restore).
  [ -d "/storage/emulated/0/$d" ] && [ -n "$(ls -A "/storage/emulated/0/$d" 2>/dev/null)" ] \
    && tar -cf "$P/internal_$d.tar" -C /storage/emulated/0 "$d" 2>/dev/null \
    && ok "captured internal:$d ($(du -sh /storage/emulated/0/$d 2>/dev/null | cut -f1))"
done
# device-experience settings: full dumps for reference; restore applies the safe allowlist (lib-root.sh).
mkdir -p "$P/settings"
for ns in system secure global; do settings list "$ns" > "$P/settings/$ns.txt" 2>/dev/null; done
ok "captured settings dumps (system/secure/global)"
# persisted SAF folder grants (the system-side record; path can vary by Android build — verify on first root)
for g in /data/system/urigrants.xml /data/system_de/0/urigrants.xml; do
  [ -f "$g" ] && { cp "$g" "$P/urigrants.xml"; ok "captured SAF grants from $g"; break; }
done
# HOMESCREEN layout — the launcher's own state (icon/folder/dock arrangement), plus wallpaper + the
# appwidget map. This is ADDITIVE (problems WARN, never fail the golden): restore applies it LAST, after
# every app is installed, so each icon's component resolves and nothing shows as "missing". Capture this
# AFTER arranging the homescreen on the golden (emulators foldered, ES-DE/launcher outside).
HS="$P/homescreen"; mkdir -p "$HS"
LP="$(home_launcher)"
if [ -n "$LP" ] && [ -d "/data/data/$LP" ]; then
  { echo "launcher_pkg=$LP"; echo "launcher_uid=$(app_uid "$LP")"; } > "$HS/meta"
  # the launcher's private data (favorites DB = folder/icon/dock layout + grid prefs); skip caches.
  tar -cf "$HS/launcher_data.tar" -C /data/data --exclude="$LP/cache" --exclude="$LP/code_cache" "$LP" 2>/dev/null \
    || tar -cf "$HS/launcher_data.tar" -C /data/data "$LP" 2>/dev/null
  if tar -tf "$HS/launcher_data.tar" >/dev/null 2>&1; then ok "captured homescreen launcher: $LP"
  else warn "homescreen launcher_data.tar looks corrupt ($LP) — homescreen will be skipped on restore"; rm -f "$HS/launcher_data.tar"; fi
else
  warn "no home launcher resolved (or it has no data dir) — homescreen layout NOT captured"
fi
# wallpaper (static image + the which-wallpaper xml; lock-screen variants too) — system-owned, per-user.
WPDIR=/data/system/users/0
for w in wallpaper wallpaper_orig wallpaper_info.xml wallpaper_lock wallpaper_lock_orig; do
  [ -f "$WPDIR/$w" ] && cp "$WPDIR/$w" "$HS/$w" 2>/dev/null
done
[ -f "$HS/wallpaper_info.xml" ] && ok "captured wallpaper"
# appwidget bindings (BEST-EFFORT: a wiped unit reallocates appWidget ids, so widgets may not rebind).
[ -f "$WPDIR/appwidgets.xml" ] && { cp "$WPDIR/appwidgets.xml" "$HS/appwidgets.xml" 2>/dev/null; ok "captured appwidget map (best-effort)"; }
# drop the homescreen dir entirely if nothing usable was captured (keeps payloads clean).
[ -n "$(ls -A "$HS" 2>/dev/null)" ] || rmdir "$HS" 2>/dev/null
# NOTE: WiFi is intentionally NOT captured/restored — units ship offline (SD + local emulators), and the
# OOBE WiFi prompt is dismissed by the device_provisioned flag in restore.sh step 7. Nothing to grab here.
# Make the whole payload world-readable so a NON-root `adb pull` (runs as the shell uid) can retrieve EVERY
# entry. urigrants.xml is cloned 0600 root; a single unreadable file makes `adb pull` abort the rest of the
# recursive copy (yet still exit 0 — a silent partial capture). Staging is transient (/data/local/tmp,
# deleted post-pull) and restore.sh re-applies 0600 on the unit, so loosening perms here is safe. On the SD
# default target this is a harmless no-op (exFAT/FUSE ignores Unix perms).
chmod -R a+rX "$P" 2>/dev/null
# completeness gate: a payload with no apps, or with capture corruption, must not be trusted as a golden.
[ "$(grep -c . "$P/pkglist.txt")" -gt 0 ] || { warn "pkglist EMPTY — no 3rd-party apps captured"; CFAIL=$((CFAIL+1)); }
[ -s "$P/global.meta" ] || { warn "global.meta missing/empty"; CFAIL=$((CFAIL+1)); }
if [ "$CFAIL" -gt 0 ]; then
  warn "GOLDEN capture had $CFAIL problem(s) — DO NOT trust this payload until resolved."
  exit 1
fi
ok "GOLDEN captured -> $P  (serial ${SD##*/}, $(ls "$P" | grep -c .) entries)"
