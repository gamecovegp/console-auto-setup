# common.sh - shared helpers for console-auto-setup
# Odin storage-mapping diagnostics. Runs on the HOST (Linux/Mac/WSL) and drives the
# Odin over adb. Read-heavy and debug-first: small composable probes, no big engine.
# POSIX sh.

ADB="${ADB:-adb}"
if ! command -v "$ADB" >/dev/null 2>&1; then
  # repo root is $DIR/.. (this lib lives in scripts/lib/, sourced by scripts/run.sh which sets DIR).
  for _c in "${DIR:-.}/../../odin-provisioning/platform-tools/adb" "${DIR:-.}/../platform-tools/adb" "/usr/sbin/adb" "/usr/bin/adb"; do
    [ -x "$_c" ] && ADB="$_c" && break
  done
fi

ADATA="/sdcard/Android/data"
RESULTS="${RESULTS:-results}"

say(){ printf '%s\n' "$*"; }
hr(){  say "------------------------------------------------------------"; }

need_device(){
  command -v "$ADB" >/dev/null 2>&1 || { say "[X] adb not found. Set ADB=/path/to/adb and retry."; exit 1; }
  "$ADB" get-state >/dev/null 2>&1 || { say "[X] No device. Plug in the Odin, enable USB debugging, tap Allow."; exit 1; }
}

# --- device shell helpers (strip the \r adb appends) ---
# NOTE: every adb call reads from /dev/null. Without this, an `adb shell` invoked
# inside a `... | while read` loop swallows the rest of the piped stdin, so the
# loop silently dies after one iteration (this is why `inspect` only ever showed
# the first emulator). Keep </dev/null on ALL adb calls used inside loops.
dev(){  "$ADB" shell "$@" </dev/null; }
dev1(){ "$ADB" shell "$@" </dev/null 2>/dev/null | tr -d '\r'; }
devcat(){ "$ADB" shell "cat \"$1\" 2>/dev/null" </dev/null | tr -d '\r'; }
devexists(){ [ "$(dev1 "[ -e \"$1\" ] && echo Y")" = "Y" ]; }

detect_sd(){
  SDPATH="$(dev1 'for d in /storage/*-*; do [ -d "$d" ] && echo "$d" && break; done')"
  SDID="${SDPATH##*/}"
}

# ADATA access level: rw | ro | none
data_access(){
  if [ -n "$(dev1 "ls $ADATA 2>/dev/null")" ]; then
    if [ "$(dev1 "echo ok > $ADATA/.cas_probe 2>/dev/null && rm -f $ADATA/.cas_probe 2>/dev/null && echo rw")" = "rw" ]; then
      echo rw
    else echo ro; fi
  else echo none; fi
}

# Does temporary root work? sets ROOT=yes|no (also leaves adbd as-is)
root_check(){
  "$ADB" root >/dev/null 2>&1; "$ADB" wait-for-device >/dev/null 2>&1
  if [ "$(dev1 'id -u')" = "0" ]; then ROOT=yes; else ROOT=no; fi
}

# First installed package id from a space-separated candidate list ($1). echoes "" if none.
resolve_pkg(){
  for _c in $1; do "$ADB" shell pm path "$_c" </dev/null >/dev/null 2>&1 && { echo "$_c"; return 0; }; done
  echo ""; return 1
}

# Emulator registry.  Fields are ';'-delimited (so '|' is free for key-regex alternation):
#   name ; pkg-candidates(space-sep) ; where(ADATA|SD|DATADATA) ; cfg-rel-paths(space-sep) ; gamedir-key-regex
# where = where the game-folder setting lives:
#   ADATA    -> /sdcard/Android/data/<pkg>/<cfg>     (clonable over adb if access=rw)
#   SD       -> <sd-card>/<cfg>                      (rides your SD-card clone)
#   DATADATA -> /data/data/<pkg>/...                 (root-only; use backup/restore)
emu_table(){ cat "${DIR:-.}/lib/emulators.txt" 2>/dev/null; }

# Look up one emulator's registry row -> sets EMU_PKGS EMU_WHERE EMU_CFGS EMU_KEYRE. Returns 1 if unknown.
emu_lookup(){
  _hit="$(emu_table | grep -i "^$1;")"
  [ -n "$_hit" ] || return 1
  EMU_NAME="${_hit%%;*}"; _rest="${_hit#*;}"
  EMU_PKGS="${_rest%%;*}"; _rest="${_rest#*;}"
  EMU_WHERE="${_rest%%;*}"; _rest="${_rest#*;}"
  EMU_CFGS="${_rest%%;*}"; EMU_KEYRE="${_rest#*;}"
  [ "$EMU_CFGS" = "-" ] && EMU_CFGS=""
  [ "$EMU_KEYRE" = "-" ] && EMU_KEYRE=""
  return 0
}

# Resolve the on-device config file path for an emulator (sets CFG_PATH; "" if none found).
# Needs PKG (resolved) and SDPATH (for SD-resident configs).
cfg_path_for(){  # $1=where $2=cfgs $3=pkg
  CFG_PATH=""
  for _rel in $2; do
    case "$1" in
      ADATA)    _p="$ADATA/$3/$_rel" ;;
      SD)       _p="$SDPATH/$_rel" ;;
      DATADATA) _p="/data/data/$3/$_rel" ;;
      *)        _p="$_rel" ;;
    esac
    if devexists "$_p"; then CFG_PATH="$_p"; return 0; fi
  done
  return 1
}

ts(){ date +%Y-%m-%d_%H%M%S 2>/dev/null || echo run; }
