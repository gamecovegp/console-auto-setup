#!/system/bin/sh
# saf_probe.sh — does a placed/edited urigrants.xml survive a reboot (vs AMS overwriting it)?
# NON-DESTRUCTIVE build phase: backs up the live store, builds an ADDITIVE test grant in /data/local/tmp.
# Phase 2 (place + reboot) and phase 3 (check + restore original) are run as separate explicit commands.
. "$(dirname "$0")/../lib-root.sh"
is_root || { echo "need root"; exit 1; }
L=/data/system/urigrants.xml; T=/data/local/tmp
case "$1" in
  build)
    cp "$L" "$T/uri.live.bak"                                  # backup the real store (also in PC payload)
    abx2xml "$L" "$T/uri.xml" 2>/dev/null
    before=$(grep -c uri-grant "$T/uri.xml")
    # additive throwaway grant: give duckstation a 2nd grant to a REAL folder (ROMs/ps2) it doesn't have
    LINE=$(grep duckstation "$T/uri.xml" | head -1 | sed 's#%2Fpsx#%2Fps2#')
    awk -v line="$LINE" '/<\/uri-grants>/{print line} {print}' "$T/uri.xml" > "$T/uri.test.xml"
    xml2abx "$T/uri.test.xml" "$T/uri.test.abx" 2>/dev/null
    abx2xml "$T/uri.test.abx" "$T/uri.check.xml" 2>/dev/null    # re-decode to prove it's valid ABX
    after=$(grep -c uri-grant "$T/uri.check.xml"); dg=$(grep -c duckstation "$T/uri.check.xml")
    ok "live grants=$before -> test-file grants=$after ; duckstation grants in test=$dg (want 2)"
    log "backup at $T/uri.live.bak ; test file at $T/uri.test.abx (NOT placed yet)" ;;
  check)
    abx2xml "$L" "$T/uri.now.xml" 2>/dev/null
    dg=$(grep -c duckstation "$T/uri.now.xml"); total=$(grep -c uri-grant "$T/uri.now.xml")
    ps2=$(grep duckstation "$T/uri.now.xml" | grep -c '%2Fps2')
    ok "AFTER REBOOT: total grants=$total ; duckstation grants=$dg ; the test (ps2) grant present=$ps2"
    [ "$ps2" -ge 1 ] && ok "==> PLACED urigrants.xml SURVIVED the reboot — fast restore path works" \
                     || warn "==> test grant GONE — AMS overwrote on shutdown; use uiauto saf_grant fallback" ;;
  restore)
    cp "$T/uri.live.bak" "$L"; chown system:system "$L"; restorecon "$L" 2>/dev/null
    ok "original urigrants.xml restored from backup (reboot to apply)" ;;
  *) echo "usage: saf_probe.sh build|check|restore" ;;
esac
