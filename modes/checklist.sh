# checklist <emu> - per-emulator FULL-SETUP verification (keys, firmware, drivers, settings, mapping).
# Reads recipes/<emu>.sh (defines recipe_items) and checks each item PASS/MISSING + the fix method.
emu="$1"; [ -n "$emu" ] || { say "usage: checklist <emu>   (have: $(ls "$DIR/recipes" 2>/dev/null | sed 's/\.txt$//' | tr '\n' ' '))"; exit 1; }
RF="$DIR/recipes/$emu.txt"
[ -f "$RF" ] || { say "no recipe for '$emu'. have: $(ls "$DIR/recipes" 2>/dev/null | sed 's/\.txt$//' | tr '\n' ' ')"; exit 1; }
emu_lookup "$emu" || { say "no registry row for $emu in lib/common.sh"; exit 1; }
detect_sd
pkg="$(resolve_pkg "$EMU_PKGS")"; [ -n "$pkg" ] || { say "$emu not installed on this unit"; exit 1; }
REPORT="$RESULTS/checklist_${emu}_$(ts).txt"
{
  say "###### SETUP CHECKLIST: $emu ($pkg) ######"
  say "android-data: $(data_access)    sd: ${SDPATH:-NONE}"
  hr
  # Fields are ';'-delimited:  label ; kind ; target ; hint
  cat "$RF" | while IFS=';' read -r label kind target hint; do
    [ -n "$label" ] || continue
    [ "$target" = "-" ] && target=""
    st="?"
    case "$kind" in
      file)
        case "$target" in /*) p="$target";; *) p="$ADATA/$pkg/$target";; esac
        if devexists "$p"; then st="PASS"; else st="MISSING"; fi ;;
      filedir)
        case "$target" in /*) p="$target";; *) p="$ADATA/$pkg/$target";; esac
        n="$(dev1 "ls \"$p\" 2>/dev/null | grep -c .")"
        if [ "${n:-0}" -gt 0 ] 2>/dev/null; then st="PASS ($n items)"; else st="MISSING"; fi ;;
      map)
        if cfg_path_for "$EMU_WHERE" "$EMU_CFGS" "$pkg"; then
          v="$(devcat "$CFG_PATH" | grep -iE "$target" | grep -iE 'content://|/storage/' | head -1)"
          if printf '%s' "$v" | grep -qi 'content://'; then st="SET (SAF - re-pick or grant per unit)"
          elif [ -n "$v" ]; then st="SET (plain - clones cleanly)"
          else st="NOT MAPPED"; fi
        else st="no cfg"; fi ;;
      show)
        cr="${target%%::*}"; pat="${target#*::}"
        case "$cr" in /*) p="$cr";; *) p="$ADATA/$pkg/$cr";; esac
        v="$(devcat "$p" | grep -iE "$pat" | head -3 | tr '\n' '~')"
        if [ -n "$v" ]; then st="now: $v"; else st="key not found"; fi ;;
      setting)
        cr="${target%%::*}"; rest="${target#*::}"; k="${rest%%::*}"; exp="${rest#*::}"
        case "$cr" in /*) p="$cr";; *) p="$ADATA/$pkg/$cr";; esac
        cur="$(devcat "$p" | grep -iE "^[[:space:]]*$k[[:space:]]*=" | head -1)"
        if printf '%s' "$cur" | grep -qiF "$exp"; then st="PASS"
        elif [ -n "$cur" ]; then st="WRONG: $cur"
        else st="not set"; fi ;;
      perm) st="run: ./run.sh grant $emu" ;;
      manual) st="MANUAL" ;;
    esac
    printf '  [ %-26s ] %s\n' "$st" "$label"
    [ -n "$hint" ] && printf '          -> %s\n' "$hint"
  done
} | tee "$REPORT"
say ""; say "[saved] $REPORT"
