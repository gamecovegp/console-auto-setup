# restore <emu> - adb restore the app's data onto a target (Method D).
# IMPORTANT: works only onto a FRESHLY (re)installed app with no data yet - Android skips
# restore onto apps that already have data. So: reinstall/clear the app first, then restore.
emu="$1"; [ -n "$emu" ] || { say "usage: restore <emu>"; exit 1; }
emu_lookup "$emu" || { say "unknown emu: $emu"; exit 1; }
out="$RESULTS/backup/${emu}.ab"
[ -f "$out" ] || { say "no backup at $out - run: ./run.sh backup $emu (on the golden) first"; exit 1; }
pkg="$(resolve_pkg "$EMU_PKGS")"; [ -n "$pkg" ] && dev "am force-stop $pkg" >/dev/null 2>&1
say "On the Odin screen: leave the password EMPTY and tap 'Restore my data'. Wait for the DEVICE to finish."
"$ADB" restore "$out"
say "[restore] done. Launch the emulator / re-inspect to confirm settings + mapping carried."
