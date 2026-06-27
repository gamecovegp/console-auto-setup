# setpath <emu> <key> <value> - set "key = value" in the emulator's config (Method A building block).
# Pulls the file, edits locally (keeps a .orig backup), pushes it back. Needs android-data rw.
emu="$1"; key="$2"; val="$3"
[ -n "$emu" ] && [ -n "$key" ] && [ -n "$val" ] || { say "usage: setpath <emu> <key> <value>"; exit 1; }
emu_lookup "$emu" || { say "unknown emu: $emu"; exit 1; }
detect_sd
pkg="$(resolve_pkg "$EMU_PKGS")"; [ -n "$pkg" ] || { say "$emu not installed"; exit 1; }
cfg_path_for "$EMU_WHERE" "$EMU_CFGS" "$pkg" || { say "no config file for $emu (open it + set up once first)"; exit 1; }
mkdir -p "$RESULTS/edit"
orig="$RESULTS/edit/${emu}_$(ts).orig"
new="$RESULTS/edit/${emu}.new"
"$ADB" pull "$CFG_PATH" "$orig" >/dev/null 2>&1 || { say "[X] pull failed (access?)"; exit 1; }
cp "$orig" "$new"
if grep -qE "^[[:space:]]*$key[[:space:]]*=" "$new"; then
  sed -i "s#^[[:space:]]*$key[[:space:]]*=.*#$key = $val#" "$new"
else
  printf '%s = %s\n' "$key" "$val" >> "$new"
fi
"$ADB" push "$new" "$CFG_PATH" >/dev/null 2>&1 || { say "[X] push failed (android-data may be read-only)"; exit 1; }
say "[set] $emu :: $key = $val   ->  ${CFG_PATH}"
say "      original saved: $orig"
say "      relaunch the emulator and check whether the game list populates."
