# lib-root.sh — shared bits for the ROOT capture/restore toolkit. Everything here runs AS ROOT (su).
# FALLBACK default only — the golden's captured pkglist.txt is authoritative (see payload_pkgs below); this
# static list is used solely when no payload is on hand. The emulator/frontend packages whose state we clone:
PKGS="dev.eden.eden_emulator com.retroarch.aarch64 org.dolphinemu.dolphinemu com.flycast.emulator \
com.github.stenzek.duckstation xyz.aethersx2.android xyz.aethersx2.tturnip me.magnum.melonds.nightly \
org.citra.emu org.ppsspp.ppsspp org.mupen64plusae.v3.fzurita org.es_de.frontend gamehub.lite"
# CRLF guard. The manifest is GENERATED ON THE PC (pathlib.write_text translates "\n" -> "\r\n" on Windows)
# and a pkglist can be hand-edited in Notepad. awk's default field separator does NOT include \r, so a bare
# package line yields "$pkg" = "com.foo\r" and "$P/$pkg/apk/" names a path that cannot exist -> restore warned
# "no APK in payload" for EVERY app while the payload sat on the device, complete. (Python's str.split() DOES
# strip \r, so the PC-side validation passed on the same file — the two sides disagreed.) Strip \r wherever we
# parse a PC-writable list. Same class as the *.sh eol=lf rule in .gitattributes.
strip_cr(){ awk '{ sub(/\r$/, ""); print }'; }
# payload_pkgs [payload_dir] — the authoritative cloned package set: the golden's captured pkglist.txt
# (one pkg per line) when present and non-empty, else the static $PKGS fallback. Pure file IO (no adb/root),
# so it is locally testable. payload_dir defaults to the capture/restore payload location.
payload_pkgs(){
  pdir="${1:-${CAS_OUT:-$(detect_sd)/golden_root_payload}}"
  if [ -s "$pdir/pkglist.txt" ]; then
    strip_cr < "$pdir/pkglist.txt"
  else
    printf '%s\n' $PKGS
  fi
}
# Shared dirs on INTERNAL storage (/storage/emulated/0) that hold emulator state OUTSIDE any app-private
# dir — wiped by a factory reset, so the payload MUST carry them. Citra MMJ keeps its whole state here
# (nand/saves/sysdata/config); RetroArch keeps system/saves/playlists/config here. These are shared media
# storage (not app-UID-owned), so restore is a plain tar extract — FUSE handles ownership.
# Shared internal-storage dirs (/storage/emulated/0/<dir>) captured WHOLESALE — app config/state OUTSIDE
# the app's private dirs; a factory reset wipes internal storage, so the golden must carry them. PSP =
# PPSSPP's memstick (SYSTEM/ppsspp.ini + controls.ini + saves/states); it lives here, NOT in /data/data or
# /sdcard/Android/data, so without it PPSSPP settings never transfer.
# NOTE: ES-DE is deliberately NOT here. Its frontend tree (gamelists/themes/box art) is multi-GB and rides
# the SD card by default; the ONLY per-unit ES-DE state that must travel is es_settings.xml (the per-system
# alternative-emulator picks: 3DS→Citra, DS→melonDS, PS2→NetherSX2…), captured/restored as a single file.
INTERNAL_DIRS="citra-emu RetroArch PSP"
# WHERE ES-DE LIVES. ES-DE Android's home directory is a USER PICK, not a fixed path: the AYN Thor keeps
# its whole tree (settings/scripts/downloaded_media) on the SD card, while the RP6 keeps it on internal
# storage. This used to be a hardcoded /storage/emulated/0 constant, so on an SD-home unit capture's
# `[ -f ]` test silently failed and the golden shipped with NO es_settings.xml — the ES-DE Companion's
# event-script toggles were lost and every provisioned unit needed hand setup.
# CAS_STORAGE_ROOT overrides /storage for off-device tests (same idiom as CAS_INST_DIR in install_apks).
# esde_home [storage_root] — print this device's ES-DE dir; empty + rc 1 when there is none. External
# volumes are probed FIRST (a handheld normally holds ES-DE on its card), internal last.
esde_home(){
  _eh_root="${1:-${CAS_STORAGE_ROOT:-/storage}}"
  for _eh_d in "$_eh_root"/*/; do
    _eh_d="${_eh_d%/}"; _eh_n="${_eh_d##*/}"
    case "$_eh_n" in emulated|self|'*') continue;; esac
    [ -d "$_eh_d/ES-DE/settings" ] && { echo "$_eh_d/ES-DE"; return 0; }
  done
  [ -d "$_eh_root/emulated/0/ES-DE/settings" ] && { echo "$_eh_root/emulated/0/ES-DE"; return 0; }
  return 1
}
# esde_home_kind <dir> — which KIND of home that path is. Recorded in the golden's global.meta so restore
# can resolve the same kind of location on a unit whose card has a DIFFERENT volume id.
esde_home_kind(){ case "$1" in */emulated/0|*/emulated/0/*) echo internal;; *) echo sd;; esac; }
# esde_home_for <kind> <sd_serial> [storage_root] — the ES-DE dir on THIS unit for a golden captured with
# <kind>. RESTORE NEVER PROBES: it obeys the kind the golden recorded ("follow the golden"). Empty + rc 1
# when the golden was SD-home and this unit has no card — the caller must warn and skip, never guess.
esde_home_for(){
  _ef_root="${3:-${CAS_STORAGE_ROOT:-/storage}}"
  case "$1" in
    internal) echo "$_ef_root/emulated/0/ES-DE"; return 0;;
    sd) if [ -n "$2" ]; then echo "$_ef_root/$2/ES-DE"; return 0; fi; return 1;;
  esac
  return 1
}
# es_setting_value <key> <es_settings.xml> — the value="…" of one setting line (empty if absent OR empty).
# es_settings.xml is a FLAT list of <type name=".." value=".." /> lines with no root wrapper.
es_setting_value(){ sed -n "s/.*name=\"$1\"[[:space:]]*value=\"\([^\"]*\)\".*/\1/p" "$2" 2>/dev/null | head -1; }
# Which shared internal-storage dir (if any) a package owns — restored only if the app is in the manifest.
internal_for(){ case "$1" in
  org.citra.emu) echo "citra-emu";;
  com.retroarch.aarch64) echo "RetroArch";;
  org.ppsspp.ppsspp) echo "PSP";;      # PPSSPP memstick (config + saves) in shared storage
  # ES-DE is handled separately (es_settings.xml only) — see esde_home()/esde_home_for(), capture.sh, restore.sh.
esac; }
# Manifest = app names (one per line) + "@flag value" lines + "#" comments. Both parsers are pure.
manifest_pkgs(){ sed -e 's/#.*//' "$1" 2>/dev/null | grep -vE '^[[:space:]]*@' | awk '{sub(/\r$/,"")} NF{print $1}'; }
manifest_flag(){ f="$1"; n="$2"; sed -n "s/^@${n}[[:space:]]\{1,\}//p" "$f" 2>/dev/null | awk '{sub(/\r$/,"")} NF{print $1; exit}'; }
# manifest_axes <manifest> <pkg> — echoes the capture axes for a pkg: "apk config" (bare/default),
# "apk", "config", or empty if the pkg isn't listed. Tokens after the pkg name narrow it.
manifest_axes(){
  line="$(sed -e 's/#.*//' "$1" 2>/dev/null | grep -vE '^[[:space:]]*@' | awk -v p="$2" '{sub(/\r$/,"")} NF && $1==p {print; exit}')"
  [ -n "$line" ] || return 0
  rest="$(echo "$line" | cut -s -d' ' -f2-)"
  case "$rest" in
    "") echo "apk config" ;;                                  # bare = both
    *apk*config*|*config*apk*) echo "apk config" ;;
    *apk*) echo "apk" ;;
    *config*) echo "config" ;;
    *) echo "apk config" ;;                                   # unknown token -> both (back-compat)
  esac
}
# manifest_wants <manifest> <pkg> <apk|config> — rc 0 if that capture/deploy axis is ON for the pkg
# (bare line = both axes on; pkg absent = off). Pure text; used by restore.sh to gate install/data.
manifest_wants(){
  _mw="$(manifest_axes "$1" "$2")"          # "apk config" | "apk" | "config" | ""
  case " $_mw " in *" $3 "*) return 0 ;; *) return 1 ;; esac
}
# Host/provisioning tools that must NOT be cloned onto shipped units (seal removes them anyway), plus
# Magisk (root provides it per-unit via init_boot). Everything else third-party gets cloned.
EXCLUDE_PKGS="com.termux moe.shizuku.privileged.api com.topjohnwu.magisk"
# Per-app HEAVY data dirs excluded from the captured data.tar — the app + its small config still ship (so it
# stays a tickable option in the list), but a regenerable/reconfigurable bulk payload does NOT bloat the
# golden. GameHub's Wine/Box64 container (files/usr, ~5 GB) is set up on-request per unit, so the golden
# ships GameHub APP-ONLY. Format: "pkg/reldir pkg/reldir …" (member-relative — matches mk_tar's exclude form).
HEAVY_EXCLUDES="gamehub.lite/files/usr gamehub.lite/files/xj_winemu"
# Per-INSTALL identity/state files that must NEVER be cloned from the golden — each unit must mint its own,
# else every device shares the golden's "unique" id and shows the golden's local analytics (recent searches).
# The Companion app self-heals (it binds its device-id to ANDROID_ID and resets analytics on new hardware),
# but ANDROID_ID can be empty on some builds — then the app can't tell it was cloned, so we ALSO strip these
# at the provisioning layer. capture excludes them; restore deletes any an OLD payload still carries.
# Same member-relative form as HEAVY_EXCLUDES ("pkg/reldir-or-file" — matches mk_tar's exclude + the restore rm).
IDENTITY_EXCLUDES="com.gamecove.gamecove_companion/files/device_id.txt com.gamecove.gamecove_companion/files/analytics.json"
# Ship-clean scrub (run at Lock, while rooted, BEFORE un-root). Member-relative "pkg/reldir-or-file",
# same form as IDENTITY_EXCLUDES. USAGE_TRACES = recent-ROM/MRU/search history; SAVE_STATES = savestates +
# in-game saves so a unit ships with zero progress. [VERIFY on device] — exact paths confirmed on the AIR X
# during rollout; this seeds the known emulator set.
USAGE_TRACES="com.retroarch.aarch64/content_history.lpl com.retroarch.aarch64/content_image_history.lpl com.retroarch.aarch64/content_music_history.lpl"
SAVE_STATES="com.github.stenzek.duckstation/savestates xyz.aethersx2.android/files/sstates"
# scrub_members <data_root> <member…> — rm -rf each member under data_root (WARN on failure, never abort).
scrub_members(){ dr="$1"; shift; for m in "$@"; do [ -e "$dr/$m" ] && { rm -rf "$dr/$m" 2>/dev/null || warn "scrub: could not remove $m"; }; done; }
# scrub_traces — the Lock-time entry point. Clears usage traces + saved game states for INSTALLED pkgs, plus
# the Android recent-tasks list. DATA_ROOT/ADATA_ROOT are overridable for local testing.
scrub_traces(){
  DR="${DATA_ROOT:-/data/data}"; AR="${ADATA_ROOT:-/sdcard/Android/data}"
  for m in $USAGE_TRACES $SAVE_STATES; do
    p="${m%%/*}"; pm path "$p" >/dev/null 2>&1 || continue          # only installed pkgs
    scrub_members "$DR" "$m"; scrub_members "$AR" "$m"              # member may live in either root
  done
  rm -rf /data/system_ce/0/recent_tasks/* /data/system/recent_tasks/* 2>/dev/null || warn "scrub: recents"
  ok "scrub_traces: usage traces + saved game states cleared"
}
# Every user-installed app on the golden, minus the host tools — this is the set capture clones.
user_pkgs(){
  for p in $(pm list packages -3 2>/dev/null | sed 's/^package://'); do
    skip=0; for e in $EXCLUDE_PKGS; do [ "$p" = "$e" ] && skip=1; done
    [ "$skip" = 0 ] && echo "$p"
  done
}
# Device-experience settings to clone (safe subset — NOT identity/provisioning/wifi/account keys).
SET_SYSTEM="screen_off_timeout screen_brightness screen_brightness_mode accelerometer_rotation font_scale haptic_feedback_enabled sound_effects_enabled peak_refresh_rate min_refresh_rate"
SET_GLOBAL="window_animation_scale transition_animation_scale animator_duration_scale stay_on_while_plugged_in"

log(){  printf '   %s\n' "$*"; }
ok(){   printf ' [ok]   %s\n' "$*"; }
warn(){ printf ' [warn] %s\n' "$*"; }
is_root(){ id 2>/dev/null | grep -q 'uid=0'; }
# First external /storage volume, in ANY volume-id format (FAT 'XXXX-XXXX', exFAT 16-hex, OTG UUID) —
# list every mounted volume and skip only the internal 'emulated'/'self', never assume a hyphen.
detect_sd(){ for d in /storage/*/; do d=${d%/}; n=${d##*/}; [ "$n" = emulated ] || [ "$n" = self ] || { [ -d "$d" ] && { echo "$d"; return; }; }; done; }
app_uid(){ stat -c %u "/data/data/$1" 2>/dev/null; }   # the app's uid on THIS device (differs per unit)

# pb_rewrite_serial <file> <old_serial> <new_serial>
# LENGTH-CORRECT SD-serial rewrite for a Jetpack DataStore Preferences protobuf (*.preferences_pb, e.g.
# ES-DE's settings, the OEM launcher's GameLauncher). These files are NUL-free, so `grep -I` MISREADS
# them as text and a plain `sed` rewrites the serial in-place — desyncing the protobuf length varints
# (a 9-char FAT id -> a 16-char exFAT id shifts every field). The app then dies on launch with
# "Unable to parse preferences proto". We instead decode the flat PreferenceMap, substitute the serial
# inside string/bytes values, and RE-ENCODE every length from the actual content, so it is correct for
# ANY serial length. On ANY parse/verify failure the file is LEFT UNCHANGED (still a valid proto -> the
# app starts, just with the golden-serial path) and we return non-zero. Needs awk + od (present here).
pb_rewrite_serial(){
  _pb_f="$1"; _pb_old="$2"; _pb_new="$3"
  [ -f "$_pb_f" ] || return 0
  command -v awk >/dev/null 2>&1 && command -v od >/dev/null 2>&1 || { warn "pb-rewrite: no awk/od — ${_pb_f##*/} left unchanged"; return 1; }
  grep -q "$_pb_old" "$_pb_f" 2>/dev/null || return 0            # serial not present -> nothing to do
  _pb_ob="$(printf '%s' "$_pb_old" | od -An -v -tu1 | tr '\n' ' ')"
  _pb_nb="$(printf '%s' "$_pb_new" | od -An -v -tu1 | tr '\n' ' ')"
  _pb_tmp="$_pb_f.casnew"
  od -An -v -tu1 "$_pb_f" | LC_ALL=C awk -v oldb="$_pb_ob" -v newb="$_pb_nb" '
    function rdvint(idx,   v,sh,b){ v=0; sh=0
      while(1){ b=B[idx++]; v += (b%128)*(2^sh); if(b<128) break; sh+=7 } NEXTP=idx; return v }
    function appv(n){  while(n>=128){ RV[++RN]=(n%128)+128;  n=int(n/128) } RV[++RN]=n }
    function appv2(n){ while(n>=128){ OUT[++O]=(n%128)+128;  n=int(n/128) } OUT[++O]=n }
    function vbytes(n,   c){ c=1; while(n>=128){ n=int(n/128); c++ } return c }
    function subst(s,e,   i,j){ SN=0; i=s
      while(i<e){ if(i+no-1<e){ j=0; while(j<no && B[i+j]==OA[j+1]) j++
          if(j==no){ for(j=1;j<=nn;j++) SS[++SN]=NA[j]; i+=no; continue } }
        SS[++SN]=B[i]; i++ } }
    function rewrite_value(s,e,   p,ftag,plen,k){ RN=0; p=s; ftag=B[p]
      if(ftag==42 || ftag==66){ p++; plen=rdvint(p); p=NEXTP; subst(p,p+plen)
        RV[++RN]=ftag; appv(SN); for(k=1;k<=SN;k++) RV[++RN]=SS[k] }
      else { for(k=s;k<e;k++) RV[++RN]=B[k] } }
    BEGIN{ no=split(oldb,OA," "); nn=split(newb,NA," ") }
    { for(i=1;i<=NF;i++) B[++N]=$i+0 }
    END{ P=1; O=0
      while(P<=N){
        if(B[P]!=10) exit 3;  P++; elen=rdvint(P); P=NEXTP; eend=P+elen
        if(B[P]!=10) exit 3;  P++; klen=rdvint(P); P=NEXTP; kstart=P; P+=klen
        if(B[P]!=18) exit 3;  P++; vlen=rdvint(P); P=NEXTP; vstart=P
        rewrite_value(vstart, vstart+vlen)
        elen2 = 1 + vbytes(klen) + klen + 1 + vbytes(RN) + RN
        OUT[++O]=10; appv2(elen2)
        OUT[++O]=10; appv2(klen); for(i=0;i<klen;i++) OUT[++O]=B[kstart+i]
        OUT[++O]=18; appv2(RN);   for(i=1;i<=RN;i++) OUT[++O]=RV[i]
        P=eend }
      for(i=1;i<=O;i++) printf "%c", OUT[i] }
  ' > "$_pb_tmp" 2>/dev/null
  _pb_rc=$?
  if [ "$_pb_rc" -ne 0 ] || [ ! -s "$_pb_tmp" ] || grep -q "$_pb_old" "$_pb_tmp" 2>/dev/null; then
    rm -f "$_pb_tmp"; warn "pb-rewrite: could not safely rewrite ${_pb_f##*/} (exit=$_pb_rc) — LEFT UNCHANGED"; return 1
  fi
  cat "$_pb_tmp" > "$_pb_f" && rm -f "$_pb_tmp"   # overwrite in place; pkg dir is re-chowned/relabeled after
  ok "pb-rewrite: ${_pb_f##*/} serial $_pb_old -> $_pb_new"
  return 0
}
# Special appops that `pm install -g` does NOT grant — they are "special access" (Settings → Special app
# access), not runtime permissions. We grant each one the app DECLARES, then VERIFY it stuck, so a silent
# grant failure surfaces. Declaration-driven (keyed off the manifest), so no package is hardcoded:
#   MANAGE_EXTERNAL_STORAGE  = "All files access" — ES-DE/Eden/GameHub read the ES-DE & ROM dirs.
#   REQUEST_INSTALL_PACKAGES = "Install unknown apps" — the Companion self-installs emulators / app updates
#                              without the end user hitting the unknown-sources prompt.
SPECIAL_APPOPS="MANAGE_EXTERNAL_STORAGE REQUEST_INSTALL_PACKAGES"
grant_special_appops(){ _p="$1"; _rc=0; _d="$(dumpsys package "$_p" 2>/dev/null)"
  for _op in $SPECIAL_APPOPS; do
    printf '%s' "$_d" | grep -q "$_op" || continue          # only grant what the app actually declares
    appops set "$_p" "$_op" allow 2>/dev/null
    appops get "$_p" "$_op" 2>/dev/null | grep -q allow || { warn "$_op NOT granted: $_p"; _rc=1; }
  done
  return $_rc; }
# The package that owns the HOME screen (the launcher). Its private data holds the icon/folder/dock
# layout we clone. Try the brief component form first (pkg/cls), fall back to the packageName= field.
home_launcher(){
  c="$(cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME 2>/dev/null | grep '/' | tail -1)"
  [ -n "$c" ] && { echo "${c%%/*}"; return; }
  cmd package resolve-activity -a android.intent.action.MAIN -c android.intent.category.HOME 2>/dev/null \
    | sed -n 's/.*packageName=\([^ }]*\).*/\1/p' | head -1
}
# Wall-clock seconds, for attributing a run's time to phases. A Download reports one total, and at the
# observed ~16 MB/s link speed the bytes account for only ~a fifth of it -- the rest is device-side work
# nobody could attribute without splitting it. Always prints digits (0 if `date` is unavailable) so the
# caller can do arithmetic on it unguarded.
now_s(){ date +%s 2>/dev/null || echo 0; }
# Is <pkg> USER-INSTALLED (an APK under /data/app) rather than system firmware (/system, /product,
# /vendor)? Decides whether a launcher is a real app that must be captured and installed on the target
# unit, or firmware that only rides @homescreen. Non-zero for an absent package too.
is_user_app(){
  case "$(pm path "$1" 2>/dev/null | head -1)" in
    package:/data/app/*) return 0 ;;
    *) return 1 ;;
  esac
}
# The FULL home component (pkg/cls). home_launcher returns only the package -- enough to GATE on, but
# `cmd package set-home-activity` needs the component, so capture records this alongside launcher_pkg.
home_component(){
  cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME 2>/dev/null \
    | grep '/' | tail -1
}
# Make <component> the default HOME app, so a golden's launcher choice actually lands on the unit.
# HARD GUARD: a unit with no valid home app is unusable, so refuse anything that isn't a real pkg/cls
# whose package is installed, and issue NO call in that case. Non-zero return = not applied.
# Callers rely on this being safe to attempt unconditionally.
set_home_component(){
  _c="$1"
  case "$_c" in *"/"*) ;; *) return 1;; esac          # must be a component, not a bare package
  [ -n "${_c%%/*}" ] && [ -n "${_c#*/}" ] || return 1 # both halves non-empty
  pm path "${_c%%/*}" >/dev/null 2>&1 || return 1     # never point HOME at a package that isn't there
  cmd package set-home-activity "$_c" >/dev/null 2>&1 || return 1
}
# The set of packages a homescreen layout REFERENCES — for self-containment, so every placed icon can
# resolve on any unit model. Intent strings in the Launcher3-family favorites DB are stored as plaintext
# (component=<pkg>/<cls> and package=<pkg>), so a launcher-agnostic token scan works without sqlite3 and
# degrades to empty on an exotic binary blob. PURE: no pm/device dependency (caller filters by pm path).
# homescreen_apps <launcher_data_dir> -> deduped pkg names on stdout (launcher itself NOT excluded here).
homescreen_apps(){
  _ha_dir="$1"; [ -d "$_ha_dir" ] || return 0
  { grep -rahoE 'component=[A-Za-z0-9._]+/' "$_ha_dir" 2>/dev/null | sed 's/^component=//; s#/.*##'
    grep -rahoE 'package=[A-Za-z0-9._]+'     "$_ha_dir" 2>/dev/null | sed 's/^package=//'
  } | sort -u
}
# The GAME FRONTEND (holds per-system emulator picks) — DISTINCT from the Android HOME app (home_launcher).
# Curated fallback list; the probe below handles OEM rebrands that keep the ES-DE-fork data shape.
GAME_LAUNCHERS="com.handheld.launcher"
_gl_installed(){ pm path "$1" >/dev/null 2>&1; }
# game_launcher [override_pkg] — resolve the frontend. Order: override (if installed) -> data-dir signature
# probe (databases/GAME_INFO or files/datastore/GameLauncher.preferences_pb) -> curated list. Echoes the
# package or nothing. DATA_ROOT overrides the probe root (default /data/data) so this is testable off-device.
game_launcher(){
  _gl_ov="$1"
  if [ -n "$_gl_ov" ] && _gl_installed "$_gl_ov"; then echo "$_gl_ov"; return 0; fi
  _gl_dr="${DATA_ROOT:-/data/data}"
  for _gl_d in "$_gl_dr"/*; do
    [ -d "$_gl_d" ] || continue
    if [ -f "$_gl_d/databases/GAME_INFO" ] || [ -f "$_gl_d/files/datastore/GameLauncher.preferences_pb" ]; then
      echo "${_gl_d##*/}"; return 0
    fi
  done
  for _gl_p in $GAME_LAUNCHERS; do _gl_installed "$_gl_p" && { echo "$_gl_p"; return 0; }; done
  return 0
}
# gl_capture <out_dir> <pkg> — capture ONLY the launcher's portable config (DataStore + shared_prefs);
# NEVER GAME_INFO (SD-bound + scan-rebuilt) or caches. DATA_ROOT overridable for tests.
gl_capture(){
  _gl_out="$1"; _gl_pkg="$2"; _gl_dr="${DATA_ROOT:-/data/data}"; _gl_src="$_gl_dr/$_gl_pkg"
  [ -d "$_gl_src" ] || { warn "gamelauncher: $_gl_pkg has no data dir — skip"; return 1; }
  mkdir -p "$_gl_out/gamelauncher"
  _gl_gld="$(cd "$_gl_out/gamelauncher" 2>/dev/null && pwd)" || { warn "gamelauncher: out dir $_gl_out invalid — skip"; return 1; }
  echo "pkg=$_gl_pkg" > "$_gl_gld/meta"   # only pkg is portable + used by restore (target uid is per-device)
  ( cd "$_gl_src" 2>/dev/null && tar -cf "$_gl_gld/config.tar" \
      --exclude='files/datastore/*-shm' --exclude='files/datastore/*.tmp' \
      files/datastore shared_prefs 2>/dev/null )
  if tar -tf "$_gl_gld/config.tar" 2>/dev/null | grep -q .; then
    ok "captured game launcher config: $_gl_pkg"
    # Also bundle the launcher's INSTALLER, so a fresh unit runs the GOLDEN's launcher version instead of
    # the stock /product build (updating the launcher was otherwise a manual per-unit sideload). It overlays
    # the system app on restore (same OEM signature). Best-effort — a copy miss just keeps the stock build.
    _gl_ap="$(pm path "$_gl_pkg" 2>/dev/null | sed 's/^package://')"
    if [ -n "$_gl_ap" ]; then
      mkdir -p "$_gl_gld/apk"
      for _gl_a in $_gl_ap; do cp "$_gl_a" "$_gl_gld/apk/" 2>/dev/null; done
      ls "$_gl_gld/apk/"*.apk >/dev/null 2>&1 && ok "captured launcher APK: $_gl_pkg" || rmdir "$_gl_gld/apk" 2>/dev/null
    fi
    return 0
  fi
  warn "gamelauncher: no portable config for $_gl_pkg (no datastore/shared_prefs?) — skip"
  rm -rf "$_gl_out/gamelauncher"; return 1
}
# gl_restore <payload_dir> <pkg> — write the captured config back as a SYSTEM app: force-stop -> extract ->
# chown system:system -> restorecon -> verify a preferences_pb exists. DATA_ROOT overridable for tests.
gl_restore(){
  _gl_pd="$1"; _gl_pkg="$2"; _gl_dr="${DATA_ROOT:-/data/data}"; _gl_tgt="$_gl_dr/$_gl_pkg"
  tar -tf "$_gl_pd/gamelauncher/config.tar" >/dev/null 2>&1 || { warn "gamelauncher: config.tar missing/corrupt — skip"; return 1; }
  # First bring the launcher to the GOLDEN's version if its APK was captured (overlays the stock /product
  # build; needs the same OEM signature). Additive — a miss keeps the unit's current build. This is what
  # makes the launcher auto-update on Download instead of a manual per-unit sideload.
  if ls "$_gl_pd/gamelauncher/apk/"*.apk >/dev/null 2>&1; then
    install_apks "$_gl_pd/gamelauncher/apk" "$_gl_pkg" \
      && ok "launcher updated to golden version: $_gl_pkg" \
      || warn "launcher: golden APK not installed (older than installed / signature mismatch?) — keeping current build"
  fi
  [ -d "$_gl_tgt" ] || { warn "gamelauncher: $_gl_pkg not installed here — skip"; return 1; }
  am force-stop "$_gl_pkg" 2>/dev/null
  mkdir -p "$_gl_tgt/files/datastore"
  tar -xf "$_gl_pd/gamelauncher/config.tar" -C "$_gl_tgt" 2>/dev/null || { warn "gamelauncher: extract failed: $_gl_pkg"; return 1; }
  chown -R system:system "$_gl_tgt/files/datastore" 2>/dev/null
  [ -d "$_gl_tgt/shared_prefs" ] && chown -R system:system "$_gl_tgt/shared_prefs" 2>/dev/null
  restorecon -R "$_gl_tgt/files/datastore" 2>/dev/null || warn "gamelauncher: restorecon failed (verify on enforcing unit)"
  if [ -d "$_gl_tgt/shared_prefs" ]; then
    restorecon -R "$_gl_tgt/shared_prefs" 2>/dev/null || warn "gamelauncher: restorecon shared_prefs failed (verify on enforcing unit)"
  fi
  if ls "$_gl_tgt"/files/datastore/*.preferences_pb >/dev/null 2>&1; then
    ok "game launcher config applied: $_gl_pkg"; return 0
  fi
  warn "gamelauncher: write-back unverified (no preferences_pb) for $_gl_pkg"; return 1
}
# install_apks <apk_source_dir> <pkg_label> — stage the dir's *.apk to a clean tmp and install (single ->
# pm install; splits -> install session). Returns 0 on success, non-zero on any failure. Proven gotchas
# (wiped golden, 2026-06-16): `pm install-multiple` is "Unknown command" in this su/pm context, and
# installing straight off the FUSE exfat SD triggers a cross-context avc denial — so ALWAYS stage first.
# CAS_INST_DIR overrides the staging path for off-device tests (default is the on-device path, unchanged).
install_apks(){
  _ia_src="$1"; _ia_pkg="$2"; _ia_stage="${CAS_INST_DIR:-/data/local/tmp/_inst}"
  set -- "$_ia_src"/*.apk
  [ -f "$1" ] || { warn "install_apks: no APK in $_ia_src ($_ia_pkg)"; return 1; }
  rm -rf "$_ia_stage"; mkdir -p "$_ia_stage"
  cp "$@" "$_ia_stage/" 2>/dev/null; set -- "$_ia_stage"/*.apk
  _ia_rc=0
  if [ "$#" -eq 1 ]; then
    pm install -r -g "$1" >/dev/null 2>&1 || { warn "install failed: $_ia_pkg"; _ia_rc=1; }
  else
    _ia_sid="$(pm install-create -r -g 2>/dev/null | sed -n 's/.*\[\([0-9]*\)\].*/\1/p')"
    if [ -z "$_ia_sid" ]; then warn "install-create gave no session: $_ia_pkg"; rm -rf "$_ia_stage"; return 1; fi
    _ia_i=0; for _ia_a in "$@"; do pm install-write "$_ia_sid" "s$_ia_i" "$_ia_a" >/dev/null 2>&1 || warn "install-write failed: $_ia_pkg s$_ia_i"; _ia_i=$((_ia_i+1)); done
    pm install-commit "$_ia_sid" >/dev/null 2>&1 || { warn "split install failed: $_ia_pkg"; pm install-abandon "$_ia_sid" >/dev/null 2>&1; _ia_rc=1; }
  fi
  rm -rf "$_ia_stage"
  return $_ia_rc
}
# homescreen_bundle_apps <launcher_data_dir> <payload_dir> <launcher_pkg> — SELF-CONTAINED LAYOUT: bundle
# the installer for every app the layout references so each icon resolves on ANY target model. Skips the
# launcher itself and apps the per-app loop already captured ($payload/<pkg>/apk) — no duplicate APKs.
# Copies base+splits from `pm path`. Prints the count of bundled apps. Additive: a copy miss is silent.
homescreen_bundle_apps(){
  _hb_ldir="$1"; _hb_pd="$2"; _hb_lp="$3"; _hb_hsa="$_hb_pd/homescreen/apps"; _hb_n=0
  for _hb_p in $(homescreen_apps "$_hb_ldir"); do
    [ "$_hb_p" = "$_hb_lp" ] && continue
    [ -d "$_hb_pd/$_hb_p/apk" ] && continue
    _hb_paths="$(pm path "$_hb_p" 2>/dev/null | sed 's/^package://')"
    [ -n "$_hb_paths" ] || continue
    mkdir -p "$_hb_hsa/$_hb_p"
    for _hb_ap in $_hb_paths; do cp "$_hb_ap" "$_hb_hsa/$_hb_p/" 2>/dev/null; done
    if [ -n "$(ls -A "$_hb_hsa/$_hb_p" 2>/dev/null)" ]; then _hb_n=$((_hb_n+1)); else rmdir "$_hb_hsa/$_hb_p" 2>/dev/null; fi
  done
  echo "$_hb_n"
}
# homescreen_install_missing <payload_dir> — install any placed app that is ABSENT on THIS unit, so its
# icon resolves when the favorites DB is applied (a wiped unit / different model may lack the game launcher
# or other placed apps). Skips apps already present. Additive: a miss WARNs, never fails the restore.
homescreen_install_missing(){
  _hm_pd="$1"; _hm_hsa="$_hm_pd/homescreen/apps"
  [ -d "$_hm_hsa" ] || return 0
  for _hm_d in "$_hm_hsa"/*/; do
    [ -d "$_hm_d" ] || continue
    _hm_p="$(basename "$_hm_d")"
    if pm path "$_hm_p" >/dev/null 2>&1; then
      log "homescreen: $_hm_p already present — no install needed"
    else
      install_apks "$_hm_d" "$_hm_p" \
        || warn "homescreen: could not install $_hm_p — its icon may not resolve (platform-signed system app on a foreign key?)"
    fi
  done
  return 0
}
# ---- WiFi provisioning (gated by @wifi, DEFAULT ON) --------------------------------------------------
# Clone the golden's saved WiFi so a fresh unit is ONLINE during provisioning (to pull app/emulator
# updates), then STRIP it at Lock so no unit ever ships carrying the shop's network/PSK. On these OEM
# builds the PreSharedKey is stored PLAINTEXT, so the store is portable across identical units; an
# encrypted build (EncryptedData/IV tags) would need a re-add instead — capture_wifi flags that case.
# The store moved to the APEX data dir on Android 12+; older builds keep it under /data/misc/wifi.
# WIFI_ROOT is prepended to every path so the whole set is testable off-device.
wifi_store_path(){
  for _wp in "${WIFI_ROOT:-}/data/misc/apexdata/com.android.wifi/WifiConfigStore.xml" \
             "${WIFI_ROOT:-}/data/misc/wifi/WifiConfigStore.xml"; do
    [ -f "$_wp" ] && { echo "$_wp"; return 0; }
  done
  echo "${WIFI_ROOT:-}/data/misc/apexdata/com.android.wifi/WifiConfigStore.xml"   # default target (may not exist yet)
}
# capture_wifi <out_dir> — copy the golden's saved-network store into <out_dir>/wifi/. rc 1 (WARN, never
# fatal) when there is no store or no saved network. Flags an encrypted-PSK build (won't clone portably).
capture_wifi(){
  _cw_src="$(wifi_store_path)"
  [ -f "$_cw_src" ] || { warn "wifi: no WifiConfigStore.xml on the golden — nothing to capture"; return 1; }
  grep -q 'name="SSID"' "$_cw_src" 2>/dev/null || { warn "wifi: golden has no saved network — skip"; return 1; }
  if grep -q 'name="EncryptedData"' "$_cw_src" 2>/dev/null; then
    warn "wifi: golden PSK is ENCRYPTED (device-bound key) — cloning it won't connect on another unit; skip"
    return 1
  fi
  mkdir -p "$1/wifi"
  cp "$_cw_src" "$1/wifi/WifiConfigStore.xml" 2>/dev/null || { warn "wifi: capture copy failed"; return 1; }
  ok "captured wifi ($(grep -c 'name="SSID"' "$_cw_src" 2>/dev/null) saved network(s))"
}
# restore_wifi <payload_dir> — clone the golden's store onto THIS unit (system:system 0600 + SELinux) so it
# auto-joins on the NEXT reboot (the framework only reads the store at boot; the provision flow reboots
# after restore). rc 1 (WARN) when the payload carries no wifi or the target apex dir is missing.
restore_wifi(){
  _rw_src="$1/wifi/WifiConfigStore.xml"
  [ -f "$_rw_src" ] || { log "wifi: no wifi in payload — skip"; return 1; }
  _rw_dst="$(wifi_store_path)"; _rw_dir="$(dirname "$_rw_dst")"
  [ -d "$_rw_dir" ] || { warn "wifi: target dir $_rw_dir missing (wifi apex not initialized?) — skip"; return 1; }
  cp "$_rw_src" "$_rw_dst" 2>/dev/null || { warn "wifi: restore copy failed"; return 1; }
  chown system:system "$_rw_dst" 2>/dev/null
  chmod 600 "$_rw_dst" 2>/dev/null
  restorecon "$_rw_dst" 2>/dev/null || warn "wifi: restorecon failed (verify on an enforcing unit)"
  ok "wifi cloned from golden — auto-joins on the next reboot"
}
# strip_wifi — Lock-time: leave NO saved network on a shipped unit. forget-network clears the framework's
# IN-MEMORY config first (so the shutdown flush can't rewrite the network back), THEN the on-disk store is
# deleted. Verify via the framework view when `cmd` exists, else by store absence. rc 1 (WARN) if anything
# remains. Always safe to run (a no-op when no wifi was provisioned).
strip_wifi(){
  _sw_apex="${WIFI_ROOT:-}/data/misc/apexdata/com.android.wifi/WifiConfigStore.xml"
  _sw_misc="${WIFI_ROOT:-}/data/misc/wifi/WifiConfigStore.xml"
  if command -v cmd >/dev/null 2>&1; then
    for _sw_id in $(cmd wifi list-networks 2>/dev/null | awk 'NR>1 && $1 ~ /^[0-9]+$/ {print $1}'); do
      cmd wifi forget-network "$_sw_id" >/dev/null 2>&1
    done
  fi
  rm -f "$_sw_apex" "$_sw_misc" 2>/dev/null
  if command -v cmd >/dev/null 2>&1; then
    _sw_left="$(cmd wifi list-networks 2>/dev/null | awk 'NR>1 && $1 ~ /^[0-9]+$/' | grep -c .)"
  else
    { [ -f "$_sw_apex" ] || [ -f "$_sw_misc" ]; } && _sw_left=1 || _sw_left=0
  fi
  if [ "${_sw_left:-0}" -gt 0 ] 2>/dev/null; then
    warn "wifi: saved network(s) still present after strip — unit may ship with wifi!"; return 1
  fi
  ok "wifi stripped — unit ships with no saved network"
}
