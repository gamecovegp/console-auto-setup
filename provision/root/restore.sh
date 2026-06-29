#!/system/bin/sh
# restore.sh — run AS ROOT on each fresh UNIT. Clones the golden's emulator state onto this unit.
#   adb shell su -c 'sh /storage/<sd>/provision/root/restore.sh'   (or PC pushes it + sets CAS_PAYLOAD)
# Re-owns to THIS unit's app UIDs, re-applies SELinux, rewrites the golden SD-serial to this unit's,
# and bulk-installs the RetroArch cores. ROMs/ES-DE ride the SD.
# FAILURE CONTRACT: any install/data/grant failure increments FAIL and the script EXITS NON-ZERO, so the
# PC orchestrator treats the unit as NOT provisioned (never silently ships a broken clone).
DIR="$(cd "$(dirname "$0")" && pwd)"; . "$DIR/lib-root.sh"
is_root || { echo "must run as root (su)"; exit 1; }
# self-protect: NEVER run this destructive restore on the GOLDEN, however it was launched.
[ -e /data/adb/.cas_golden ] && { echo "REFUSING: this is the GOLDEN (.cas_golden present)."; exit 1; }
FAIL=0
# relabel helper: restorecon but SURFACE failures instead of silently swallowing them.
relabel(){ restorecon "$@" 2>/dev/null || warn "restorecon failed: $*"; }
command -v restorecon >/dev/null 2>&1 || warn "restorecon NOT found — relabel skipped (path-default labels still apply; verify on an enforcing unit)"

SD="$(detect_sd)"; SERIAL="${SD##*/}"
[ -n "$SERIAL" ] || warn "NO SD CARD detected — ROMs unavailable; serial-rewrite will be SKIPPED (grants keep the golden serial). Insert the matching/cloned SD and re-run."
# payload source: PC-pushed dir ($CAS_PAYLOAD) wins; else today's on-SD payload (back-compat).
P="${CAS_PAYLOAD:-$SD/golden_root_payload}"
[ -d "$P" ] || { echo "no payload at $P (set CAS_PAYLOAD or stage on SD)"; exit 1; }
GSERIAL="$(sed -n 's/^golden_serial=//p' "$P/global.meta")"
# module set: explicit manifest ($CAS_MANIFEST) honored VERBATIM (even if it selects none); else the
# payload's pkglist; else the built-in PKGS.
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then
  RPKGS="$(manifest_pkgs "$CAS_MANIFEST")"
  [ -n "$RPKGS" ] || { warn "manifest selects no apps — nothing to restore"; exit 1; }
else
  RPKGS="$(payload_pkgs "$P")"
fi
# behavior flags from the manifest (@settings/@hardening/@grants) — default ON (full restore) if absent.
FSETTINGS=on; FHARDENING=on; FGRANTS=on
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then
  v="$(manifest_flag "$CAS_MANIFEST" settings)";  [ -n "$v" ] && FSETTINGS="$v"
  v="$(manifest_flag "$CAS_MANIFEST" hardening)"; [ -n "$v" ] && FHARDENING="$v"
  v="$(manifest_flag "$CAS_MANIFEST" grants)";    [ -n "$v" ] && FGRANTS="$v"
fi
# Optional: scope the whole restore to ONE package (canary.sh uses this to prove the live path safely).
[ -n "${ONLY_PKG:-}" ] && RPKGS="$ONLY_PKG"
log "restore: this serial=$SERIAL  golden serial=$GSERIAL  apps=$(echo $RPKGS | wc -w)${ONLY_PKG:+  (SCOPED to $ONLY_PKG)}"

# 1) install each app's APK set from the payload (so it exists + gets a UID on THIS unit).
#    Gotchas proven on the wiped golden (2026-06-16): `pm install-multiple` is "Unknown command" in this
#    su/pm context, AND installing straight off the FUSE exfat SD makes system_server do a cross-context
#    fuse read (avc denied — only "works" on this permissive build; would FAIL on an enforcing unit).
#    Fix: stage the APK(s) to /data/local/tmp (clean read), then `pm install`. Splits -> install session.
for pkg in $RPKGS; do
  if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ] && ! manifest_wants "$CAS_MANIFEST" "$pkg" apk; then
    log "deploy: $pkg APK-axis off — skipping install"; continue
  fi
  set -- "$P/$pkg/apk/"*.apk
  if [ ! -f "$1" ]; then
    # no APK in the payload: config-only (axes=config) is BY DESIGN — the app is provided elsewhere (e.g.
    # the OEM launcher self-installs it). Skip install, don't FAIL; only WARN if the app isn't here yet so
    # its config can't land. Any other missing-APK is a genuine error (today's fail-closed contract).
    AX="$(sed -n 's/^axes=//p' "$P/$pkg/meta" 2>/dev/null)"
    case " $AX " in
      *" config "*)
        if pm path "$pkg" >/dev/null 2>&1; then
          log "config-only: $pkg already installed — applying config, no APK in payload"
        else
          warn "config-only: $pkg NOT installed on this unit — its config can't apply yet (install it, then re-run Update)"
        fi
        continue ;;
      *) warn "no APK in payload for $pkg"; FAIL=$((FAIL+1)); continue ;;
    esac
  fi
  rm -rf /data/local/tmp/_inst; mkdir -p /data/local/tmp/_inst
  cp "$@" /data/local/tmp/_inst/ 2>/dev/null; set -- /data/local/tmp/_inst/*.apk
  if [ "$#" -eq 1 ]; then
    pm install -r -g "$1" >/dev/null 2>&1 || { warn "install failed: $pkg"; FAIL=$((FAIL+1)); }
  else
    SID="$(pm install-create -r -g 2>/dev/null | sed -n 's/.*\[\([0-9]*\)\].*/\1/p')"
    if [ -z "$SID" ]; then
      warn "install-create gave no session: $pkg"; FAIL=$((FAIL+1)); rm -rf /data/local/tmp/_inst; continue
    fi
    n=0; for a in "$@"; do pm install-write "$SID" "s$n" "$a" >/dev/null 2>&1 || warn "install-write failed: $pkg s$n"; n=$((n+1)); done
    pm install-commit "$SID" >/dev/null 2>&1 || { warn "split install failed: $pkg"; pm install-abandon "$SID" >/dev/null 2>&1; FAIL=$((FAIL+1)); }
  fi
  rm -rf /data/local/tmp/_inst
done

# 2) per-app: restore data -> rewrite serial -> chown to THIS unit's uid -> restorecon
for pkg in $RPKGS; do
  if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ] && ! manifest_wants "$CAS_MANIFEST" "$pkg" config; then
    log "deploy: $pkg Config-axis off — skipping data restore"; continue
  fi
  [ -f "$P/$pkg/data.tar" ] || continue
  pm path "$pkg" >/dev/null 2>&1 || { warn "$pkg not installed — skip data restore"; FAIL=$((FAIL+1)); continue; }
  TUID="$(app_uid "$pkg")"                                  # this unit's uid for the app
  [ -n "$TUID" ] || { warn "$pkg: could not resolve uid (install failed?) — skip data restore"; FAIL=$((FAIL+1)); continue; }
  # validate the tar BEFORE destroying the fresh data dir (a truncated tar must not leave the app empty).
  tar -tf "$P/$pkg/data.tar" >/dev/null 2>&1 || { warn "BAD data.tar (corrupt/truncated): $pkg — skip"; FAIL=$((FAIL+1)); continue; }
  am force-stop "$pkg" 2>/dev/null
  rm -rf "/data/data/$pkg/"* "/data/data/$pkg/".[!.]* 2>/dev/null
  tar -xf "$P/$pkg/data.tar" -C /data/data 2>/dev/null || { warn "data extract failed: $pkg"; FAIL=$((FAIL+1)); }
  # an OLD payload (captured before IDENTITY_EXCLUDES existed) may still carry per-install identity — strip
  # it so THIS unit mints its own device-id + starts with empty analytics. New captures already exclude these.
  for idf in $IDENTITY_EXCLUDES; do case "$idf" in "$pkg"/*) rm -f "/data/data/$idf" 2>/dev/null;; esac; done
  # external app data (firmware/BIOS/keys/nand) -> internal-storage BACKING store, then chown to THIS
  # unit's app uid:ext_data_rw(1078). FUSE reflects REAL ownership (proven: a shell-owned push locked the
  # app out -> black screen), so leaving the golden's uids/root here would break key/BIOS loading + saves.
  if [ -f "$P/$pkg/adata.tar" ]; then
    if tar -tf "$P/$pkg/adata.tar" >/dev/null 2>&1; then
      AD=/data/media/0/Android/data; mkdir -p "$AD"; rm -rf "$AD/$pkg"
      tar -xf "$P/$pkg/adata.tar" -C "$AD" 2>/dev/null || { warn "adata extract failed: $pkg"; FAIL=$((FAIL+1)); }
      chown -R "$TUID:1078" "$AD/$pkg" 2>/dev/null
      relabel -R "$AD/$pkg"
    else warn "BAD adata.tar (corrupt/truncated): $pkg"; FAIL=$((FAIL+1)); fi
  fi
  # OBB (large game expansion files) -> internal-storage backing, same ownership scheme as adata.
  if [ -f "$P/$pkg/obb.tar" ] && tar -tf "$P/$pkg/obb.tar" >/dev/null 2>&1; then
    OD=/data/media/0/Android/obb; mkdir -p "$OD"; rm -rf "$OD/$pkg"
    tar -xf "$P/$pkg/obb.tar" -C "$OD" 2>/dev/null || { warn "obb extract failed: $pkg"; FAIL=$((FAIL+1)); }
    chown -R "$TUID:1078" "$OD/$pkg" 2>/dev/null
    relabel -R "$OD/$pkg"
  fi
  # rewrite the golden SD serial -> this unit's serial in any config that references it
  if [ -n "$SERIAL" ] && [ -n "$GSERIAL" ] && [ "$GSERIAL" != "$SERIAL" ]; then
    # text configs (the SAF content URIs live here): same-length in-place rewrite.
    grep -rIl "$GSERIAL" "/data/data/$pkg" 2>/dev/null | while IFS= read -r f; do sed -i "s/$GSERIAL/$SERIAL/g" "$f"; done
    # binary files holding the old serial (missed by -I): drop regenerable CACHES (e.g. Citra's
    # databases/icons.db) so they rebuild clean. A binary NON-cache config is flagged + a marker dropped
    # (the post-loop check turns it into a hard failure — it would mean a broken different-serial clone).
    grep -rl "$GSERIAL" "/data/data/$pkg" 2>/dev/null | while IFS= read -r f; do
      grep -Iq "$GSERIAL" "$f" 2>/dev/null && continue            # text -> already rewritten above
      case "$f" in
        */cache/*|*/code_cache/*|*/databases/*) rm -f "$f"; warn "dropped stale binary cache (serial): ${f##*/} — regenerates";;
        *) warn "serial in BINARY non-cache config NOT rewritten: $f — broken on a different-serial SD"; : > /data/local/tmp/.cas_serial_fail;;
      esac
    done
  fi
  chown -R "$TUID:$TUID" "/data/data/$pkg" 2>/dev/null
  relabel -R "/data/data/$pkg"
  # Special appops `pm install -g` does NOT grant (it only covers runtime perms) — "All files access"
  # (MANAGE_EXTERNAL_STORAGE: ES-DE/Eden/GameHub read the ES-DE & ROMs dirs, else ES-DE re-prompts) and
  # "Install unknown apps" (REQUEST_INSTALL_PACKAGES: the Companion self-installs emulators / app updates
  # without the unknown-sources prompt). Granted declaration-driven + verified by grant_special_appops;
  # a declared-but-unconfirmed grant bumps FAIL so a silent grant failure surfaces. See lib-root.sh.
  grant_special_appops "$pkg" || FAIL=$((FAIL+1))
  ok "restored $pkg (uid $TUID, $( [ -f "$P/$pkg/adata.tar" ] && echo 'incl Android/data keys/BIOS' || echo 'internal only'))"
done
# a binary non-cache serial config was found -> the different-serial clone is broken; count it once.
[ -e /data/local/tmp/.cas_serial_fail ] && { FAIL=$((FAIL+1)); rm -f /data/local/tmp/.cas_serial_fail; }

# ---- GLOBAL steps below run on a FULL restore only (skipped when ONLY_PKG scopes to one app) ----
if [ -z "${ONLY_PKG:-}" ]; then
# 2b) shared internal-storage dirs (Citra/RetroArch state) -> /storage/emulated/0. These are shared media
# storage (not app-UID-owned), so a plain extract works; FUSE assigns ownership, apps read via storage perm.
# restore an internal dir only if its owning app is in the manifest (coupling via internal_for).
for pkg in $RPKGS; do
  d="$(internal_for "$pkg")"; [ -n "$d" ] || continue
  [ -f "$P/internal_$d.tar" ] || continue
  tar -tf "$P/internal_$d.tar" >/dev/null 2>&1 || { warn "BAD internal_$d.tar"; FAIL=$((FAIL+1)); continue; }
  mkdir -p /storage/emulated/0
  tar -xf "$P/internal_$d.tar" -C /storage/emulated/0 2>/dev/null || { warn "internal:$d extract failed"; FAIL=$((FAIL+1)); }
  relabel -R "/storage/emulated/0/$d"
  ok "restored internal:$d (for $pkg)"
done

# 2c) ES-DE box-art location — point MediaDirectory at where the art actually is for THIS unit:
#   CAS_ES_MEDIA=sd (default) -> the unit's OWN SD card (/storage/$SERIAL/ES-DE/downloaded_media). Box art
#       rides the SD image; nothing is pushed to internal. Skip if no SD (art absent, like ROMs).
#   CAS_ES_MEDIA=internal     -> internal default; the PC pushes downloaded_media there AFTER restore.
# es_settings.xml is ES-DE's own flat list of <type name=".." value=".." /> lines (no root wrapper), so we
# DELETE any existing MediaDirectory line (robust to spacing) and append a clean one. [VERIFY the element
# format against the live ES-DE build on first run.]
ES_SET="/storage/emulated/0/ES-DE/settings/es_settings.xml"
if echo "$RPKGS" | grep -q org.es_de.frontend && [ -f "$ES_SET" ]; then
  case "${CAS_ES_MEDIA:-sd}" in
    internal) MEDIA_DIR="/storage/emulated/0/ES-DE/downloaded_media";;
    *)        [ -n "$SERIAL" ] && MEDIA_DIR="/storage/$SERIAL/ES-DE/downloaded_media" || MEDIA_DIR="";;
  esac
  if [ -n "$MEDIA_DIR" ]; then
    sed -i '/name="MediaDirectory"/d' "$ES_SET" 2>/dev/null   # drop any existing line (avoids duplicates)
    [ -s "$ES_SET" ] && [ -n "$(tail -c1 "$ES_SET" 2>/dev/null)" ] && printf '\n' >> "$ES_SET"  # ensure EOL
    printf '<string name="MediaDirectory" value="%s" />\n' "$MEDIA_DIR" >> "$ES_SET"
    relabel "$ES_SET" 2>/dev/null
    ok "ES-DE MediaDirectory -> $MEDIA_DIR (${CAS_ES_MEDIA:-sd})"
  else
    warn "ES-DE box art: SD mode but no SD serial — MediaDirectory left at default (box art may be absent)."
  fi
fi

# 3) RetroArch cores: bulk-copy the arm64 .so set into the internal (exec-able) cores dir.
#    Source order: $CAS_CORES (pushed from the PC) > $SD/retroarch-cores (legacy, SD). The app's own
#    cores already arrive inside data.tar; this step tops the set up to the full curated library.
CORES="${CAS_CORES:-$SD/retroarch-cores}"; RAUID="$(app_uid com.retroarch.aarch64)"
if [ -d "$CORES" ] && [ -n "$RAUID" ]; then
  mkdir -p /data/data/com.retroarch.aarch64/cores
  cp "$CORES"/*.so /data/data/com.retroarch.aarch64/cores/ 2>/dev/null
  chown -R "$RAUID:$RAUID" /data/data/com.retroarch.aarch64/cores
  relabel -R /data/data/com.retroarch.aarch64/cores
  ok "installed $(ls "$CORES"/*.so 2>/dev/null | grep -c .) cores"
fi

# 4) SAF folder grants. /data/system/urigrants.xml is ABX; each <uri-grant> embeds the SD serial,
#    keyed by targetPkg, userId=0, NO app-UID dependency. decode -> serial-rewrite -> re-encode -> place.
#    We re-encode to a TEMP, verify it round-trips, and only THEN overwrite the live store.
GR="$P/urigrants.xml"
if [ "$FGRANTS" != on ]; then
  log "SAF grants: skipped (@grants off)"
elif [ -f "$GR" ] && command -v abx2xml >/dev/null 2>&1 && command -v xml2abx >/dev/null 2>&1; then
  TX=/data/local/tmp/urigrants.txt
  abx2xml "$GR" "$TX" 2>/dev/null
  if [ -n "$SERIAL" ] && [ -n "$GSERIAL" ] && [ "$GSERIAL" != "$SERIAL" ]; then sed -i "s/$GSERIAL/$SERIAL/g" "$TX"; fi
  # NOTE: overwrite is fine on a fresh provisioned unit (no other important SAF grants). AMS reads this at BOOT.
  if xml2abx "$TX" "$TX.abx" 2>/dev/null && abx2xml "$TX.abx" - 2>/dev/null | grep -q uri-grant; then
    cp "$TX.abx" /data/system/urigrants.xml
    chown system:system /data/system/urigrants.xml 2>/dev/null
    chmod 600 /data/system/urigrants.xml 2>/dev/null
    relabel /data/system/urigrants.xml
    ok "SAF grants rebuilt (serial $GSERIAL -> $SERIAL) — active after the reboot below"
  else
    warn "SAF grant re-encode failed verification — left existing grants untouched"; FAIL=$((FAIL+1))
  fi
  rm -f "$TX" "$TX.abx"
  # If AMS rewrites our file over on shutdown, fall back to the proven no-root uiauto saf_grant.
else
  warn "no urigrants.xml/abx tools — use provision uiauto saf_grant for the SAF emulators instead"
fi

# 5) device-experience settings (safe allowlist from lib-root.sh — never identity/wifi/account keys)
apply_settings(){ ns="$1"; shift; f="$P/settings/$ns.txt"; [ -f "$f" ] || return
  for k in "$@"; do v="$(sed -n "s/^$k=//p" "$f" | head -1)"; [ -n "$v" ] && settings put "$ns" "$k" "$v" 2>/dev/null; done; }
if [ "$FSETTINGS" = on ]; then
  apply_settings system $SET_SYSTEM
  apply_settings global $SET_GLOBAL
  ok "applied device-experience settings (display/animations/timeout)"
else log "settings: skipped (@settings off)"; fi

# 6) gaming/stability hardening (universal across Android handhelds):
#    a) keep every emulator out of Doze/battery-optimization so it's never throttled or killed
if [ "$FHARDENING" = on ]; then
  for pkg in $RPKGS; do
    dumpsys deviceidle whitelist +"$pkg" >/dev/null 2>&1               # Doze whitelist (RUNTIME — lost on reboot)
    cmd appops set "$pkg" RUN_ANY_IN_BACKGROUND allow >/dev/null 2>&1  # background-run exemption (PERSISTS reboot)
  done
  ok "battery-optimization exemption applied to $(echo $RPKGS | wc -w) apps"
  #  b) stop OTA updates — on a ROOTED unit an OTA can bootloop or strip root. Disable Google's auto path
  #     plus any vendor FOTA/updater app (com.odin.fota, Retroid's equivalent, etc.). Keep configupdater.
  settings put global ota_disable_automatic_update 1 2>/dev/null
  for u in $(pm list packages 2>/dev/null | sed 's/^package://' | grep -iE 'fota|\.ota$|systemupdate|softwareupdate|firmwareupdate' | grep -viE 'configupdater'); do
    pm disable-user --user 0 "$u" >/dev/null 2>&1 && ok "disabled OTA app $u (protects root)"
  done
else log "hardening: skipped (@hardening off)"; fi

# 7) OOBE skip + first-boot experience — replaces the manual WiFi/timezone/language setup wizard.
#    The wizard asks 3 things; we skip ALL of them — WiFi is NOT provisioned here (not needed for setup;
#    the unit runs off the SD + local emulators, so it ships offline and WiFi is joined later if ever):
#      • WiFi      -> simply skipped. Marking the device provisioned dismisses the prompt; no network saved.
#      • language  -> golden ROM is already en-US (ro.product.locale); skipping the wizard keeps it.
#      • timezone  -> pinned via prop below (offline-safe); auto_time_zone refines it later if WiFi is added.
# Mark the device provisioned + user setup complete so a fresh unit boots straight to the launcher.
settings put global device_provisioned 1 2>/dev/null
settings put secure user_setup_complete 1 2>/dev/null
pm disable-user --user 0 com.odin.setupwizard >/dev/null 2>&1 && ok "Odin setup wizard disabled (WiFi/lang/TZ prompts skipped)"
# locale + timezone: CLONE from the GOLDEN (captured in global.meta), NOT a hardcoded default. Priority:
#   CAS_LOCALE/CAS_TZ env override  >  the golden's captured value  >  a last-resort fallback.
GTZ="$(sed -n 's/^golden_tz=//p' "$P/global.meta" 2>/dev/null)"
GLOC="$(sed -n 's/^golden_locale=//p' "$P/global.meta" 2>/dev/null)"
TZ_SET="${CAS_TZ:-${GTZ:-Asia/Manila}}"
LOC_SET="${CAS_LOCALE:-${GLOC:-en-US}}"
setprop persist.sys.locale   "$LOC_SET" 2>/dev/null
setprop persist.sys.timezone "$TZ_SET"  2>/dev/null
# auto_time_zone OFF so the cloned timezone STICKS on an offline unit (with it ON and no network, Android
# can't resolve a zone and may ignore the manual one). auto_time (clock) stays ON to sync if WiFi is added.
settings put global auto_time 1 2>/dev/null
settings put global auto_time_zone 0 2>/dev/null
[ -n "$GTZ" ] || warn "golden_tz not in payload (old capture) — using fallback tz=$TZ_SET; re-capture the golden to clone its real timezone"
ok "OOBE skipped; locale=$LOC_SET tz=$TZ_SET cloned from golden (applies on reboot)"

# 8) HOMESCREEN layout (apply LAST — every app from step 1 is now installed, so each icon's component
#    resolves and nothing shows as "missing"). Restores the launcher's favorites DB (folder/icon/dock
#    arrangement) + grid prefs, the wallpaper, and the appwidget map (best-effort). Gated by @homescreen
#    (default ON if the payload carries it). ADDITIVE: problems WARN but do NOT bump FAIL — the clone is
#    still functionally clean, only the icon arrangement didn't apply. SAME-FAMILY ONLY: the launcher pkg
#    must match the golden's (identical hardware); a different-launcher unit (e.g. Mangmi) skips itself.
#    Takes effect on the reboot the provision flow performs after restore (launcher/wallpaper/widgets all
#    reload at cold boot).
FHOME=on
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then v="$(manifest_flag "$CAS_MANIFEST" homescreen)"; [ -n "$v" ] && FHOME="$v"; fi
HS="$P/homescreen"
if [ "$FHOME" != on ]; then
  log "homescreen: skipped (@homescreen off)"
elif [ ! -f "$HS/launcher_data.tar" ]; then
  log "homescreen: no layout in payload (capture a golden with the homescreen arranged to enable)"
else
  LP="$(sed -n 's/^launcher_pkg=//p' "$HS/meta" 2>/dev/null)"
  CUR="$(home_launcher)"
  if [ -z "$LP" ]; then
    warn "homescreen: payload has no launcher_pkg — skip"
  elif [ -n "$CUR" ] && [ "$CUR" != "$LP" ]; then
    warn "homescreen: this unit's launcher ($CUR) != golden's ($LP) — would not apply, SKIP (different family?)"
  elif ! pm path "$LP" >/dev/null 2>&1; then
    warn "homescreen: launcher $LP not present on this unit — skip"
  elif ! tar -tf "$HS/launcher_data.tar" >/dev/null 2>&1; then
    warn "homescreen: launcher_data.tar corrupt — skip"
  else
    LUID="$(app_uid "$LP")"
    if [ -z "$LUID" ]; then
      warn "homescreen: cannot resolve $LP uid — skip"
    else
      am force-stop "$LP" 2>/dev/null
      rm -rf "/data/data/$LP/"* "/data/data/$LP/".[!.]* 2>/dev/null
      if tar -xf "$HS/launcher_data.tar" -C /data/data 2>/dev/null; then
        chown -R "$LUID:$LUID" "/data/data/$LP" 2>/dev/null
        relabel -R "/data/data/$LP"
        ok "homescreen: launcher layout restored for $LP (uid $LUID)"
      else warn "homescreen: launcher extract failed — arrangement not applied"; fi
      # wallpaper -> per-user system dir (system:system; restorecon sets the wallpaper_file label)
      WPDIR=/data/system/users/0; WPN=0
      for w in wallpaper wallpaper_orig wallpaper_info.xml wallpaper_lock wallpaper_lock_orig; do
        [ -f "$HS/$w" ] && cp "$HS/$w" "$WPDIR/$w" 2>/dev/null && { chown system:system "$WPDIR/$w" 2>/dev/null; relabel "$WPDIR/$w"; WPN=$((WPN+1)); }
      done
      [ "$WPN" -gt 0 ] && ok "homescreen: wallpaper restored ($WPN file(s))"
      # appwidget map (BEST-EFFORT — the system may renumber appWidget ids on first boot)
      if [ -f "$HS/appwidgets.xml" ]; then
        cp "$HS/appwidgets.xml" "$WPDIR/appwidgets.xml" 2>/dev/null && { chown system:system "$WPDIR/appwidgets.xml" 2>/dev/null; relabel "$WPDIR/appwidgets.xml"; }
        warn "homescreen: appwidget map placed (BEST-EFFORT — widgets may show empty if ids were reallocated)"
      fi
    fi
  fi
fi

# GAME LAUNCHER emulator picks (DataStore) — ADDITIVE (WARN, never FAIL), like @homescreen. Auto-detect THIS
# unit's frontend and write back the captured portable config. Default ON when the payload carries it;
# "@gamelauncher off" disables; "@gamelauncher <pkg>" pins/overrides the target frontend.
FGL=on; OVL=""
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then
  v="$(manifest_flag "$CAS_MANIFEST" gamelauncher)"
  case "$v" in "") : ;; off) FGL=off ;; *.*) OVL="$v" ;; *) FGL="$v" ;; esac
fi
if [ ! -d "$P/gamelauncher" ]; then
  : # back-compat: golden carried no game-launcher config — nothing to do
elif [ "$FGL" = off ]; then
  log "game launcher: skipped (@gamelauncher off)"
else
  GLPKG="$(sed -n 's/^pkg=//p' "$P/gamelauncher/meta" 2>/dev/null)"
  TGL="$(game_launcher "$OVL")"
  if [ -z "$TGL" ]; then
    warn "game launcher: none detected on this unit — skip"
  elif [ -n "$GLPKG" ] && [ "$TGL" != "$GLPKG" ]; then
    warn "game launcher: this unit ($TGL) != golden's ($GLPKG) — skip (different family?)"
  else
    gl_restore "$P" "$TGL" || true        # additive: a write-back miss must not fail the restore
  fi
fi

fi   # ---- end GLOBAL steps ----

if [ "$FAIL" -gt 0 ]; then
  warn "RESTORE finished with $FAIL failure(s) — this unit is NOT a clean clone. Do NOT seal/ship it."
  exit 1
fi
ok "RESTORE complete. Rebooting recommended; then verify games boot from ES-DE."
