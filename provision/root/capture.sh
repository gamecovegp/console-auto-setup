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
# tar a dir tree with EXCLUDES, robustly. On a LIVE filesystem `tar` exits NON-ZERO when a file changes or
# vanishes mid-archive — that's BENIGN here, so success is judged by whether the archive is READABLE
# (tar -tf), NOT by the exit code. (The old code fell back to a NO-exclude tar on ANY non-zero, which on a
# live /data/data silently re-captured cache/cores/etc. every time — the real cause of oversized goldens.)
# Only a genuinely UNREADABLE archive falls back to a no-exclude retry, as a last resort.
# Args: <out.tar> <-C dir> <member> <exclude-path…>   (exclude paths are member-relative, e.g. "$pkg/cache")
mk_tar(){ out="$1"; cdir="$2"; member="$3"; shift 3
  exc=""; for e in "$@"; do exc="$exc --exclude=$e"; done
  tar -cf "$out" -C "$cdir" $exc "$member" 2>/dev/null
  tar -tf "$out" >/dev/null 2>&1 && return 0
  warn "archive unreadable WITH excludes ($member) — retrying without excludes (will be larger)"
  tar -cf "$out" -C "$cdir" "$member" 2>/dev/null
  tar -tf "$out" >/dev/null 2>&1
}
# global.meta: SD serial + the golden's locale/timezone, so restore can CLONE them onto each unit
# (instead of a hardcoded default). getprop is the source of truth for the persisted tz/locale.
{ echo "golden_serial=${SD##*/}"
  echo "golden_tz=$(getprop persist.sys.timezone 2>/dev/null)"
  echo "golden_locale=$(getprop persist.sys.locale 2>/dev/null)"
} > "$P/global.meta"
# the app set we clone: the manifest's pkgs when a selection was passed (SELECTIVE capture), else ALL
# 3rd-party minus host tools. The default launcher (a system app) rides @homescreen below, NOT this loop,
# so it is filtered out of the per-app set even when the manifest lists it.
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then
  manifest_pkgs "$CAS_MANIFEST" | grep -vxF "$(home_launcher 2>/dev/null || echo __no_launcher__)" > "$P/pkglist.txt" 2>/dev/null \
    || manifest_pkgs "$CAS_MANIFEST" > "$P/pkglist.txt"
else
  user_pkgs > "$P/pkglist.txt"
fi
[ -n "$HEAVY_EXCLUDES" ] && log "app-only capture (heavy runtime dirs excluded): $HEAVY_EXCLUDES"
ok "cloning $(grep -c . "$P/pkglist.txt") apps: $(tr '\n' ' ' < "$P/pkglist.txt")"
for pkg in $(cat "$P/pkglist.txt"); do
  [ -d "/data/data/$pkg" ] || { warn "$pkg not installed — skip"; continue; }
  log "capturing $pkg…"   # announce BEFORE the tar so a big app (save states/mods) isn't a silent gap
  # capture axes for THIS pkg from the manifest (else BOTH — back-compat). apk -> bundle the installer;
  # config -> bundle its data/settings/BIOS. config-only (no apk) = the app is installed elsewhere (e.g. the
  # OEM launcher) and we only carry its config; apk-only = a clean install with no golden saves.
  AX="apk config"; [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ] && AX="$(manifest_axes "$CAS_MANIFEST" "$pkg")"
  case " $AX " in *" apk "*) CAP_APK=1;; *) CAP_APK=0;; esac
  case " $AX " in *" config "*) CAP_CFG=1;; *) CAP_CFG=0;; esac
  if [ "$CAP_APK" = 1 ]; then
    mkdir -p "$P/$pkg/apk"
    # bundle the app's EXACT installed APK into the payload (self-contained root clone — version-exact,
    # portable, no dependency on the SD's /apps folder). pm path = base + any splits.
    for ap in $(pm path "$pkg" 2>/dev/null | sed 's/^package://'); do cp "$ap" "$P/$pkg/apk/" 2>/dev/null; done
  fi
  if [ "$CAP_CFG" = 1 ]; then
    # internal app data — KEEP settings/key binds/mappings/saves/states/grants-references. DROP regenerable or
    # PC-/APK-sourced bulk: cache+code_cache (regenerate), cores (RetroArch — the PC repushes the full curated
    # set on restore, so shipping them twice just bloats the golden), app_flutter (Flutter re-extracts these
    # from the APK on first run — e.g. the Companion's 76 MB kernel_blob), plus any per-app HEAVY dirs (e.g.
    # GameHub's ~5 GB Wine container — app ships, runtime is set up on-request).
    # extra per-app excludes for THIS pkg: HEAVY regenerable bulk + per-INSTALL IDENTITY files (device-id /
    # analytics) that must stay unique per unit and never ride the golden (see lib-root.sh).
    heavy=""; for h in $HEAVY_EXCLUDES $IDENTITY_EXCLUDES; do case "$h" in "$pkg"/*) heavy="$heavy $h";; esac; done
    mk_tar "$P/$pkg/data.tar" /data/data "$pkg" "$pkg/cache" "$pkg/code_cache" "$pkg/cores" "$pkg/app_flutter" $heavy \
      || { warn "data.tar looks corrupt: $pkg"; CFAIL=$((CFAIL+1)); }
    # external app data — KEEP firmware/BIOS/keys/nand/driver. DROP regenerable GPU shader caches (rebuilt on
    # first run — e.g. Eden's ~400 MB files/shader) plus caches/logs.
    if [ -d "/sdcard/Android/data/$pkg" ]; then
      mk_tar "$P/$pkg/adata.tar" /sdcard/Android/data "$pkg" "$pkg/cache" "$pkg/files/shader" "$pkg/files/log" "$pkg/files/logs" \
        || { warn "adata.tar looks corrupt: $pkg"; CFAIL=$((CFAIL+1)); }
    fi
    # large native-game expansion files (OBB) — some GameHub PC-game ports need them
    [ -d "/sdcard/Android/obb/$pkg" ] && tar -cf "$P/$pkg/obb.tar" -C /sdcard/Android/obb "$pkg" 2>/dev/null
  fi
  { echo "golden_uid=$(app_uid "$pkg")"; echo "axes=$AX"; } > "$P/$pkg/meta"
  ok "captured $pkg ($(du -sh "$P/$pkg" 2>/dev/null | cut -f1))"   # size so the operator can see what's big
done
# shared internal-storage dirs (Citra/RetroArch keep state here, OUTSIDE app-private dirs; a factory
# reset wipes internal storage, so the self-contained payload must carry them).
for d in $INTERNAL_DIRS; do
  # skip if absent OR empty (e.g. ES-DE before its home is actually moved to internal — avoids a
  # stale/empty capture that would create a broken empty dir on restore).
  [ -d "/storage/emulated/0/$d" ] && [ -n "$(ls -A "/storage/emulated/0/$d" 2>/dev/null)" ] || continue
  # EXCLUDES — MUST be path-anchored "$d/sub", NOT a bare "sub". toybox tar matches excludes with
  # fnmatch(pattern, member, FNM_LEADING_DIR), which anchors to the START of the member name: a bare
  # "downloaded_media" does NOT match "ES-DE/downloaded_media/…", so it was SILENTLY NOT excluded and the
  # ~12 GB ES-DE box art shipped in every golden — the real cause of the slow, oversized captures.
  # "$d/…" matches correctly on toybox AND GNU tar (verified). We drop:
  #   downloaded_media — ES-DE box art (SHARED layer the PC pushes via push_es_media; not per-golden)
  #   logs             — runtime logs, never golden state
  # RetroArch thumbnails are INTENTIONALLY KEPT (per decision) so units ship with RA box art OFFLINE —
  # do NOT add --exclude="$d/thumbnails" here; it would strip that offline box art.
  # announce BEFORE the tar (a big NAND/thumbnails dir is otherwise a silent gap) and show the RAW source
  # size so the operator sees raw-vs-captured and knows the wait is real work, not a hang.
  log "archiving internal:$d ($(du -sh "/storage/emulated/0/$d" 2>/dev/null | cut -f1) raw; ES-DE box art + logs excluded)…"
  tar -cf "$P/internal_$d.tar" --exclude="$d/downloaded_media" --exclude="$d/logs" \
        -C /storage/emulated/0 "$d" 2>/dev/null \
    && ok "captured internal:$d config ($(du -sh "$P/internal_$d.tar" 2>/dev/null | cut -f1))"
done
# device-experience settings: full dumps for reference; restore applies the safe allowlist (lib-root.sh).
# Gated by @settings in the capture-manifest (default on; off = this golden carries no display settings).
FCSET=on; [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ] && { v="$(manifest_flag "$CAS_MANIFEST" settings)"; [ -n "$v" ] && FCSET="$v"; }
if [ "$FCSET" = off ]; then
  log "settings: capture skipped (@settings off)"
else
  mkdir -p "$P/settings"
  for ns in system secure global; do settings list "$ns" > "$P/settings/$ns.txt" 2>/dev/null; done
  ok "captured settings dumps (system/secure/global)"
fi
# persisted SAF folder grants (the system-side record; path can vary by Android build — verify on first root).
# Gated by @grants in the capture-manifest (default on; off = this golden carries no SAF grants).
FCGR=on; [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ] && { v="$(manifest_flag "$CAS_MANIFEST" grants)"; [ -n "$v" ] && FCGR="$v"; }
if [ "$FCGR" = off ]; then
  log "grants: capture skipped (@grants off)"
else
  for g in /data/system/urigrants.xml /data/system_de/0/urigrants.xml; do
    [ -f "$g" ] && { cp "$g" "$P/urigrants.xml"; ok "captured SAF grants from $g"; break; }
  done
fi
# HOMESCREEN layout — the launcher's own state (icon/folder/dock arrangement), plus wallpaper + the
# appwidget map. This is ADDITIVE (problems WARN, never fail the golden): restore applies it LAST, after
# every app is installed, so each icon's component resolves and nothing shows as "missing". Capture this
# AFTER arranging the homescreen on the golden (emulators foldered, ES-DE/launcher outside).
# Gated by @homescreen (default on); "@homescreen off" skips it (the Save modal's HOME-launcher row).
FHS=on
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then
  [ "$(manifest_flag "$CAS_MANIFEST" homescreen)" = off ] && FHS=off
fi
if [ "$FHS" = off ]; then
  log "homescreen: capture skipped (@homescreen off)"
else
  HS="$P/homescreen"; mkdir -p "$HS"
  LP="$(home_launcher)"
  if [ -n "$LP" ] && [ -d "/data/data/$LP" ]; then
    { echo "launcher_pkg=$LP"; echo "launcher_uid=$(app_uid "$LP")"; } > "$HS/meta"
    # the launcher's private data (favorites DB = folder/icon/dock layout + grid prefs); skip caches.
    tar -cf "$HS/launcher_data.tar" -C /data/data --exclude="$LP/cache" --exclude="$LP/code_cache" "$LP" 2>/dev/null \
      || tar -cf "$HS/launcher_data.tar" -C /data/data "$LP" 2>/dev/null
    if tar -tf "$HS/launcher_data.tar" >/dev/null 2>&1; then ok "captured homescreen launcher: $LP"
    else warn "homescreen launcher_data.tar looks corrupt ($LP) — homescreen will be skipped on restore"; rm -f "$HS/launcher_data.tar"; fi
    # SELF-CONTAINED LAYOUT: bundle installers for the apps placed on the homescreen so every icon resolves
    # on ANY unit model (a placed app absent on the unit is installed on restore, then the favorites DB is
    # applied). Additive — never bumps CFAIL. Only when the layout was actually captured.
    if [ -f "$HS/launcher_data.tar" ]; then
      _hs_n="$(homescreen_bundle_apps "/data/data/$LP" "$P" "$LP")"
      [ "${_hs_n:-0}" -gt 0 ] 2>/dev/null && ok "homescreen: bundled $_hs_n placed-app installer(s) so icons resolve on any model"
    fi
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
fi
# GAME LAUNCHER emulator picks — capture ONLY the portable DataStore/prefs (NOT GAME_INFO; that is SD-bound +
# scan-rebuilt). Auto-detected frontend, independent of the HOME launcher above. Additive (never bumps CFAIL).
# Gated by @gamelauncher (default on); "@gamelauncher off" disables; "@gamelauncher <pkg>" pins the frontend.
FGLC=on; OVLC=""
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then
  v="$(manifest_flag "$CAS_MANIFEST" gamelauncher)"
  case "$v" in "") : ;; off) FGLC=off ;; *.*) OVLC="$v" ;; esac
fi
if [ "$FGLC" = off ]; then
  log "game launcher: capture skipped (@gamelauncher off)"
else
  GL="$(game_launcher "$OVLC")"
  if [ -n "$GL" ]; then gl_capture "$P" "$GL"; else warn "game launcher: none detected — nothing to capture"; fi
fi
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
ok "GOLDEN captured -> $P  (serial ${SD##*/}, $(ls "$P" | grep -c .) entries, $(du -sh "$P" 2>/dev/null | cut -f1) total)"
