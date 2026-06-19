# root-check - does temporary root work, and can we reach /data/data?  Gates Method B + firmware/keys writes.
root_check
say "temp-root (adb root):  $ROOT   (id -u = $(dev1 'id -u'))"
say "android-data access:   $(data_access)"
if [ -n "$(dev1 'ls /data/data 2>/dev/null | head -1')" ]; then dd=yes; else dd=no; fi
say "/data/data readable:   $dd"
hr
if [ "$ROOT" = yes ]; then
  say "-> ROOT available: Method B (clone SAF grants) + direct /data/data read/write are possible."
  say "   (best case: even SAF-only emulators and /data/data-only apps can be fully cloned)"
else
  say "-> No root. Use file methods where android-data is rw; for /data/data-only apps use"
  say "   backup/restore (./run.sh backup <emu> ; ./run.sh restore <emu>)."
fi
"$ADB" unroot >/dev/null 2>&1
