# backup <emu> - adb backup the app's full data incl /data/data (Method D, no root).
# Use for emulators whose settings live in /data/data (e.g. m64plusfz, sometimes aethersx2).
emu="$1"; [ -n "$emu" ] || { say "usage: backup <emu>"; exit 1; }
emu_lookup "$emu" || { say "unknown emu: $emu"; exit 1; }
pkg="$(resolve_pkg "$EMU_PKGS")"; [ -n "$pkg" ] || { say "$emu not installed"; exit 1; }
mkdir -p "$RESULTS/backup"
out="$RESULTS/backup/${emu}.ab"
say "On the Odin screen: leave the password EMPTY and tap 'Back up my data'."
"$ADB" backup -f "$out" "$pkg"
if [ -f "$out" ]; then
  sz=$(wc -c < "$out" | tr -d ' ')
  say "[backup] $out  ($sz bytes)"
  [ "$sz" -lt 5000 ] 2>/dev/null && say "[!] tiny file - this Android build likely strips app data from adb backup (Method D blocked here)."
else
  say "[X] no backup produced - adb backup may be disabled on this firmware."
fi
