# apks.sh — install the emulator APKs on a FRESH device (before per-emulator provisioning).
# APKs are pre-captured to the SD (from the golden via `pm path`/pull). Installs as shell/Shizuku.
# Layout:  $APKDIR/*.apk          single-APK apps  -> pm install
#          $APKDIR/<name>/*.apk   split-APK apps   -> pm install-multiple (base + splits)
# Override: APKDIR=/storage/<sd>/apks   (default $SD/apks)
APKDIR="${APKDIR:-$SDPATH/apks}"
exists "$APKDIR" || { warn "no APK dir at $APKDIR — skipping install (assuming emulators already present)"; return 0; }

# single-APK installs
for f in $(SH "ls \"$APKDIR\"/*.apk 2>/dev/null"); do
  [ -n "$f" ] || continue
  r="$(SH "pm install -r -g \"$f\" 2>&1")"
  case "$r" in *Success*) ok "installed ${f##*/}";; *) warn "install ${f##*/}: $r";; esac
done

# split-APK installs (one subdir per app: base.apk + split_*.apk)
for d in $(SH "ls -d \"$APKDIR\"/*/ 2>/dev/null"); do
  [ -n "$d" ] || continue
  sid="$(SH "pm install-create -r -g 2>/dev/null | sed -E 's/.*\\[([0-9]+)\\].*/\\1/'")"
  [ -n "$sid" ] || { warn "install-create failed for $d"; continue; }
  for a in $(SH "ls \"$d\"*.apk 2>/dev/null"); do
    SH "pm install-write $sid \"${a##*/}\" \"$a\" >/dev/null 2>&1"
  done
  r="$(SH "pm install-commit $sid 2>&1")"
  case "$r" in *Success*) ok "installed split app ${d%/}";; *) warn "commit ${d%/}: $r";; esac
done
ok "APK install pass done (from $APKDIR)."
