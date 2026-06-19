# provision/lib.sh — shared engine for the per-emulator provision scripts.
# Runs in TWO contexts with the SAME code:
#   • PC over ADB           (MODE=adb)   — host runs this, drives device via `adb`
#   • On-device via Shizuku (MODE=local) — run inside `rish` (shell-uid shell), e.g. in Termux
# Auto-detects; override with CAS_MODE=adb|local. Optional SERIAL=<adb serial>.
#
# Golden assets live on the SD so file ops are an on-device `cp` in BOTH modes (no adb push needed).
#   PAYLOAD default = $SD/golden_payload   (per-package read-only assets: keys/firmware/bios/config)
#
# Helpers: SH, devcat, exists, detect_sd, launch_first, grant_allfiles, grant_legacy,
#          clone_into, setkey, INPUT, ui_dump, ui_tap, ui_waittap, saf_grant, log, ok, warn

set -u

# ---- mode detection ----
ADB="${ADB:-adb}"; [ -n "${SERIAL:-}" ] && ADB="$ADB -s $SERIAL"
if [ "${CAS_MODE:-auto}" = "local" ]; then MODE=local
elif [ "${CAS_MODE:-auto}" = "adb" ]; then MODE=adb
elif command -v "$ADB" >/dev/null 2>&1 && $ADB get-state >/dev/null 2>&1; then MODE=adb
else MODE=local; fi

log(){  printf '   %s\n' "$*"; }
ok(){   printf '\033[32m ✓ %s\033[0m\n' "$*"; }
warn(){ printf '\033[33m ⚠ %s\033[0m\n' "$*"; }
hdr(){  printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# ---- run a shell command on the device (string arg), strip CR ----
if [ "$MODE" = local ]; then
  SH(){ sh -c "$*" 2>/dev/null | tr -d '\r'; }
  INPUT(){ input "$@"; }
  _UIDUMP(){ uiautomator dump /sdcard/ui.xml >/dev/null 2>&1; cat /sdcard/ui.xml 2>/dev/null | tr -d '\r'; }
else
  SH(){ $ADB shell "$*" </dev/null 2>/dev/null | tr -d '\r'; }
  INPUT(){ $ADB shell input "$@" </dev/null; }
  _UIDUMP(){ $ADB shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1; $ADB shell cat /sdcard/ui.xml 2>/dev/null | tr -d '\r'; }
fi
devcat(){ SH "cat \"$1\" 2>/dev/null"; }
exists(){ [ "$(SH "[ -e \"$1\" ] && echo Y")" = Y ]; }
fg_activity(){ SH 'dumpsys activity activities | grep -m1 topResumedActivity' | sed -E 's/.*u0 //; s/\} .*//'; }

# ---- SD + payload ----
detect_sd(){ SDPATH="$(SH 'for d in /storage/*-*; do [ -d "$d" ] && echo "$d" && break; done')"; SDID="${SDPATH##*/}"; PAYLOAD="${PAYLOAD:-$SDPATH/golden_payload}"; }

# ---- app lifecycle / perms ----
launch_first(){ # $1=pkg : start once so the APP creates its dirs app-owned, then stop.
  SH "monkey -p $1 -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1"
  i=0; while [ "$i" -lt 12 ]; do exists "/sdcard/Android/data/$1" && { sleep 1; break; }; sleep 0.4; i=$((i+1)); done
  SH "am force-stop $1"; }
grant_allfiles(){ SH "appops set $1 MANAGE_EXTERNAL_STORAGE allow"; } # $1=pkg (Class A/some)
grant_legacy(){ # $1=pkg : legacy + A13 granular media (Class B). Harmless if not declared.
  for p in READ_EXTERNAL_STORAGE WRITE_EXTERNAL_STORAGE READ_MEDIA_IMAGES READ_MEDIA_VIDEO READ_MEDIA_AUDIO; do
    SH "pm grant $1 android.permission.$p" >/dev/null 2>&1; done; }

# ---- file ops (on-device cp; assets on SD => works in both modes) ----
clone_into(){ # $1=src (SD path)  $2=dst (device path). Copies CONTENTS of src into dst.
  exists "$1" || { warn "payload missing: $1"; return 1; }
  SH "mkdir -p \"$2\" && cp -r \"$1\"/. \"$2\"/ 2>/dev/null" && log "copied $1 -> $2"; }
setkey(){ # $1=file $2=key $3=value : set key=value in an INI/cfg (handles 'key=' and 'key = ')
  exists "$1" || { warn "config not found: $1"; return 1; }
  SH "sed -i -E 's#^([[:space:]]*$2[[:space:]]*=).*#\\1$3#' \"$1\"" && log "set $2=$3 in ${1##*/}"; }

# ---- UI automation (uiautomator; for standard-Android UIs + system dialogs) ----
# Pure sed/grep/tr — NO python/awk — so it runs in `rish` (on-device Shizuku) and on the host alike.
UIXML="${CAS_TMP:-$([ "$MODE" = local ] && echo /data/local/tmp || echo /tmp)}/cas_ui.xml"
ui_dump(){ _UIDUMP > "$UIXML" 2>/dev/null; }
ui_tap(){ pat="$1"; ui_dump
  case "$pat" in
    '^'*'$') v="${pat#^}"; v="${v%$}"; re="(text|content-desc)=\"$v\"" ;;   # exact attribute value
    *)       re="(text|content-desc)=\"[^\"]*$pat[^\"]*\"" ;;               # substring match
  esac
  line="$(tr '<' '\n' < "$UIXML" 2>/dev/null | grep -iE "$re" | grep -m1 'bounds="\[')"
  [ -n "$line" ] || return 1
  set -- $(echo "$line" | sed -E 's/.*bounds="\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]".*/\1 \2 \3 \4/')
  [ "$#" -eq 4 ] || return 1
  cx=$(( ($1 + $3) / 2 )); cy=$(( ($2 + $4) / 2 ))
  INPUT tap "$cx" "$cy"; log "tap \"$pat\" @ ($cx,$cy)"; return 0; }
ui_waittap(){ # $1=regex $2=max-tries(default 30) — poll as fast as dumps allow, tap when it appears
  i=0; max="${2:-30}"; while [ "$i" -lt "$max" ]; do ui_tap "$1" && return 0; i=$((i+1)); done; warn "timeout: /$1/"; return 1; }

# ---- Class C SAF folder grant (drive the in-app picker via uiautomator) ----
saf_grant(){ # $1=pkg  $2=add-dir-button-regex  (picker opens at the app's folder; grants it)
  SH "monkey -p $1 -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1"
  ui_waittap "$2" 15 || { warn "$1: add-dir button '/$2/' not found"; return 1; }   # polls; no fixed wait
  ui_waittap "USE THIS FOLDER" 12 || { warn "$1: picker not at folder"; return 1; }
  ui_waittap "^ALLOW$" 10 || true
  sleep 1
  if [ "$(SH "dumpsys activity permissions 2>/dev/null | grep -c targetPkg=$1")" -gt 0 ]; then ok "$1 SAF grant persisted"; else warn "$1 grant not confirmed"; fi; }
