# grant <emu> - grant All-Files-Access (MANAGE_EXTERNAL_STORAGE). Method A building block;
# lets emulators that support plain-path (non-SAF) file access read the SD without a folder-picker.
emu="$1"; [ -n "$emu" ] || { say "usage: grant <emu>"; exit 1; }
emu_lookup "$emu" || { say "unknown emu: $emu"; exit 1; }
pkg="$(resolve_pkg "$EMU_PKGS")"; [ -n "$pkg" ] || { say "$emu not installed"; exit 1; }
if dev "appops set $pkg MANAGE_EXTERNAL_STORAGE allow" >/dev/null 2>&1 \
   || dev "cmd appops set $pkg MANAGE_EXTERNAL_STORAGE allow" >/dev/null 2>&1; then
  say "[grant] All-Files-Access allowed -> $pkg"
else
  say "[X] appops grant failed for $pkg"
fi
