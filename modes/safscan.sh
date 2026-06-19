# safscan [emu] - (root) show the device's persisted SAF folder grants (Method B diagnosis).
# A SAF content:// mapping only works on a target unit if a matching grant exists here. These
# live in /data/system (root-only), so this needs temp-root to work.
root_check
[ "$ROOT" = yes ] || say "[!] temp-root failed - cannot read /data/system. Method B (grant clone) needs root."
filter="${1:-}"
say "== persisted URI grants (authorized SAF folders) =="
got=0
for f in /data/system/urigrants.xml /data/system/users/0/urigrants.xml /data/system_de/0/urigrants.xml; do
  txt="$(devcat "$f")"
  [ -n "$txt" ] || continue
  got=1; hr; say "-- $f --"
  if [ -n "$filter" ]; then printf '%s\n' "$txt" | grep -iE 'uri-grant|grant ' | grep -i "$filter"
  else printf '%s\n' "$txt" | grep -iE 'uri-grant|grant ' | head -60; fi
done
[ "$got" = 1 ] || say "(no urigrants file readable - need root, or none exist)"
"$ADB" unroot >/dev/null 2>&1
say ""
say "To clone Method B: copy a working grant line for the target's package+tree into the target's"
say "urigrants.xml (root), matching its SD serial. Tell me what shows here and I'll script it."
