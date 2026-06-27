#!/usr/bin/env sh
# console-auto-setup launcher (Odin storage-mapping diagnostics).
#   ./run.sh <mode> [args]
# Modes:
#   inspect [emu]                 read-only: classify each emulator's game-folder mapping
#   getcfg  <emu>                 dump an emulator's config file(s)
#   setpath <emu> <key> <value>   write a config key (Method A building block)
#   grant   <emu>                 grant All-Files-Access (Method A building block)
#   backup  <emu>                 adb backup the app's data        (Method D)
#   restore <emu>                 adb restore it onto a target      (Method D)
#   safscan [emu]                 (root) show persisted SAF folder grants (Method B)
#   root-check                    test temporary root + /data/data access
DIR="$(cd "$(dirname "$0")" && pwd)"; cd "$DIR" || exit 1
. "$DIR/lib/common.sh"

MODE="${1:-inspect}"
[ "$#" -gt 0 ] && shift
if [ ! -f "$DIR/modes/$MODE.sh" ]; then
  say "unknown mode: $MODE"
  say "modes: $(ls "$DIR/modes" 2>/dev/null | sed 's/\.sh$//' | tr '\n' ' ')"
  exit 1
fi
need_device
mkdir -p "$RESULTS"
. "$DIR/modes/$MODE.sh"
