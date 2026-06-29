#!/system/bin/sh
# dryrun.sh — NON-DESTRUCTIVE validation of the root restore pipeline against the golden's OWN payload.
# Writes ONLY to /data/local/tmp/restore_dryrun. NEVER touches /data/data/<pkg> or /data/system.
# Simulates a target unit whose SD serial differs from the golden's, to exercise the serial rewrite.
DIR="$(cd "$(dirname "$0")" && pwd)"; . "$DIR/lib-root.sh"
is_root || { echo "must run as root (su)"; exit 1; }
SD="$(detect_sd)"; SERIAL="${SD##*/}"; P="$SD/golden_root_payload"
[ -d "$P" ] || { echo "no payload at $P"; exit 1; }
FAKE="AAAA-1111"                                   # pretend the target unit's SD serial
GSERIAL="$(sed -n 's/^golden_serial=//p' "$P/global.meta")"
W=/data/local/tmp/restore_dryrun; rm -rf "$W"; mkdir -p "$W/data"
log "SD=$SD  this(real)=$SERIAL  golden=$GSERIAL  simulated-target=$FAKE"
SMALL="org.dolphinemu.dolphinemu com.github.stenzek.duckstation org.citra.emu me.magnum.melonds.nightly com.flycast.emulator dev.eden.eden_emulator"

echo; echo "== 1. payload integrity: tar -tf every app (lists without extracting) =="
for pkg in $(payload_pkgs "$P"); do
  t="$P/$pkg/data.tar"; [ -f "$t" ] || { warn "$pkg: NO data.tar"; continue; }
  n=$(tar -tf "$t" 2>/dev/null | grep -c .)
  a=""; [ -f "$P/$pkg/adata.tar" ] && a=" + adata $(tar -tf "$P/$pkg/adata.tar" 2>/dev/null | grep -c .)"
  ok "$pkg: data $n entries$a"
done

echo; echo "== 2. fresh-unit prerequisites on SD =="
ls "$SD"/apks/*.apk >/dev/null 2>&1 && ok "APKs: $(ls "$SD"/apks/*.apk 2>/dev/null | grep -c .) on SD" \
  || warn "NO apks/ on SD — restore can't install apps on a factory-reset unit"
ls "$SD"/retroarch-cores/*.so >/dev/null 2>&1 && ok "cores: $(ls "$SD"/retroarch-cores/*.so 2>/dev/null | grep -c .) on SD" \
  || warn "NO retroarch-cores/ on SD — restore can't bulk-install cores"

echo; echo "== 3. serial rewrite + chown + relabel on SCRATCH copies (small apps only) =="
for pkg in $SMALL; do
  [ -f "$P/$pkg/data.tar" ] || continue
  tar -xf "$P/$pkg/data.tar" -C "$W/data" 2>/dev/null
  before=$(grep -rIl "$GSERIAL" "$W/data/$pkg" 2>/dev/null | grep -c .)
  grep -rIl "$GSERIAL" "$W/data/$pkg" 2>/dev/null | while IFS= read -r f; do sed -i "s/$GSERIAL/$FAKE/g" "$f"; done
  leftover=$(grep -rIl "$GSERIAL" "$W/data/$pkg" 2>/dev/null | grep -c .)
  newrefs=$(grep -rIl "$FAKE" "$W/data/$pkg" 2>/dev/null | grep -c .)
  realctx=$(ls -dZ "/data/data/$pkg" 2>/dev/null | awk '{print $1}')
  log "$pkg: serial-files $before -> leftover $leftover, new-serial files $newrefs ; real SELinux ctx=$realctx"
done
# prove chown+restorecon mechanics on one scratch tree (uses golden's own uid as a stand-in target uid)
TP=org.dolphinemu.dolphinemu; TUID="$(app_uid "$TP")"
chown -R "$TUID:$TUID" "$W/data/$TP" 2>/dev/null && ok "chown -R $TUID:$TUID OK (scratch)" || warn "chown FAILED"
restorecon -RF "$W/data/$TP" 2>/dev/null && ok "restorecon ran OK (scratch path gets tmp label; real /data/data gets app label)" || warn "restorecon FAILED"

echo; echo "== 4. ABX urigrants.xml: decode -> serial rewrite -> re-encode -> re-decode (round-trip) =="
GR="$P/urigrants.xml"
if abx2xml "$GR" "$W/uri.xml" 2>/dev/null; then
  ok "abx2xml decode OK ($(grep -c 'uri-grant' "$W/uri.xml") grants, $(wc -c < "$GR") bytes ABX)"
  sed -i "s/$GSERIAL/$FAKE/g" "$W/uri.xml"
  if xml2abx "$W/uri.xml" "$W/uri.abx" 2>/dev/null; then
    ok "xml2abx re-encode OK ($(wc -c < "$W/uri.abx") bytes)"
    abx2xml "$W/uri.abx" "$W/uri_check.xml" 2>/dev/null
    old=$(grep -c "$GSERIAL" "$W/uri_check.xml"); new=$(grep -c "$FAKE" "$W/uri_check.xml")
    ok "re-decode valid: golden-serial refs=$old (want 0)  target-serial refs=$new (want >0)"
    echo "   sample grants after rewrite:"
    grep -oE 'targetPkg="[^"]+"[^>]*tree/[^"]+' "$W/uri_check.xml" | sed -E 's/targetPkg="([^"]+)".*tree\/(.*)/     \1  ->  \2/' | head -6
  else warn "xml2abx FAILED"; fi
else warn "abx2xml FAILED — fall back to uiauto saf_grant"; fi

echo; echo "== DONE. Scratch at $W ($(du -sh "$W" 2>/dev/null | cut -f1)); golden data/grants untouched. =="
