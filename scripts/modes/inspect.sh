# inspect - READ-ONLY. For each installed emulator, find HOW it stores its game folder and
# classify it, so we know which mapping METHOD can work. This is the core diagnosis.
# Usage (via run.sh): inspect [emu]
WANT="${1:-all}"
REPORT="$RESULTS/inspect_$(ts).txt"

detect_sd
_acc="$(data_access)"
{
  say "###### console-auto-setup : INSPECT ######"
  say "model: $(dev1 'getprop ro.product.model')  device: $(dev1 'getprop ro.product.device')  android: $(dev1 'getprop ro.build.version.release')"
  say "gpu:   $(dev1 "dumpsys SurfaceFlinger 2>/dev/null | grep -m1 -iE 'GLES|Adreno|Mali'")"
  say "sd:    ${SDPATH:-NONE}  (serial ${SDID:-NONE})"
  say "android-data-access: $_acc   [rw=file methods work | ro/none=need root or backup/restore]"
  hr
  say "Per-emulator mapping (TYPE drives which METHOD to use):"
  say ""

  emu_table | while IFS= read -r row; do
    [ -n "$row" ] || continue
    name="${row%%;*}"
    [ "$WANT" = "all" ] || [ "$WANT" = "$name" ] || continue
    emu_lookup "$name" || continue
    pkg="$(resolve_pkg "$EMU_PKGS")"
    if [ -z "$pkg" ]; then
      printf '  %-12s [not installed]\n' "$name"
      continue
    fi

    cfg_path_for "$EMU_WHERE" "$EMU_CFGS" "$pkg"
    if [ -z "$CFG_PATH" ]; then
      case "$EMU_WHERE" in
        DATADATA) typ="DATA-DATA (root-only)"; rec="Method D (backup/restore) or temp-root" ;;
        SD)       typ="SD-config NOT FOUND";   rec="set the game folder in-app once, then re-inspect" ;;
        *)        typ="NO CONFIG YET";         rec="open the app + add the game folder, then re-inspect" ;;
      esac
      printf '  %-12s pkg=%s\n      TYPE: %s\n      -> %s\n' "$name" "$pkg" "$typ" "$rec"
      continue
    fi

    # Classify off the GAME-FOLDER KEY line specifically. (Don't just scan for the
    # first /storage/ line: emulators like Eden list internal nand/sdmc /storage dirs
    # BEFORE the real gamedir, which is a content:// URI further down -> mis-classified
    # PLAIN. The key regex pins the actual game-folder line.)
    keyre="${EMU_KEYRE:-zzz_nokey}"
    keyline="$(devcat "$CFG_PATH" | grep -iE "$keyre" | grep -iE 'content://|/storage/' | head -1)"
    if printf '%s' "$keyline" | grep -qi 'content://'; then
      typ="SAF content:// URI"
      rec="Method A won't stick. Launch via ES-DE (hands ROM directly) or Method B (root) / C (re-pick)."
    elif printf '%s' "$keyline" | grep -qiE "/storage/[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}|/storage/emulated|/storage/"; then
      typ="PLAIN /storage path"
      rec="Method A works: clone the config + rewrite the SD serial. Best case."
    else
      typ="config present, game-folder line not obvious"
      rec="run: ./run.sh getcfg $name   (and tell me what you see)"
    fi
    # path-bearing lines for display context (key line first if we found one)
    lines="$(devcat "$CFG_PATH" | grep -iE "content://|/storage/|$keyre" | head -6)"
    [ -n "$keyline" ] && lines="$keyline
$lines"
    printf '  %-12s pkg=%s\n      cfg:  %s\n      TYPE: %s\n      -> %s\n' "$name" "$pkg" "${CFG_PATH#"$ADATA"/}" "$typ" "$rec"
    [ -n "$lines" ] && printf '%s\n' "$lines" | sed 's/^/        | /'
  done

  hr
  say "Legend of methods (details in README.md):"
  say "  A plain-path + All-Files-Access  | B SAF-grant clone (root) | C re-pick in-app (manual)"
  say "  D adb backup/restore (/data/data)| SD-resident configs ride your card clone"
} | tee "$REPORT"
say ""
say "[saved] $REPORT"
