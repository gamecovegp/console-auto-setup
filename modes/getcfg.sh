# getcfg <emu> - dump the emulator's config file(s) (read-only). Use to find exact setting keys.
emu="$1"; [ -n "$emu" ] || { say "usage: getcfg <emu>"; exit 1; }
emu_lookup "$emu" || { say "unknown emu: $emu (see lib/common.sh emu_table)"; exit 1; }
detect_sd
pkg="$(resolve_pkg "$EMU_PKGS")"; [ -n "$pkg" ] || { say "$emu not installed"; exit 1; }
found=0
for rel in $EMU_CFGS; do
  case "$EMU_WHERE" in ADATA) p="$ADATA/$pkg/$rel";; SD) p="$SDPATH/$rel";; DATADATA) p="/data/data/$pkg/$rel";; *) p="$rel";; esac
  if devexists "$p"; then
    found=1; hr; say "== $emu :: $p =="; devcat "$p"
  fi
done
[ "$found" = 1 ] || say "no config file found for $emu (where=$EMU_WHERE  cfgs=$EMU_CFGS). Open the app + set it up first."
