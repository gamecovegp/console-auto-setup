#!/system/bin/sh
# serial_audit.sh — find every file under /data/data/<app> that contains the SD serial, and flag the
# ones the text-only rewrite (grep -rIl) would MISS (binary files). Read-only.
. "$(dirname "$0")/../lib-root.sh"; is_root || { echo need root; exit 1; }
SER="${1:-9C33-6BBD}"
for pkg in $(user_pkgs); do
  [ -d "/data/data/$pkg" ] || continue
  all="$(grep -rl  "$SER" "/data/data/$pkg" 2>/dev/null)"      # any file (incl binary)
  txt="$(grep -rIl "$SER" "/data/data/$pkg" 2>/dev/null)"      # text only (what restore rewrites today)
  na=$(printf '%s\n' "$all" | grep -c .); nt=$(printf '%s\n' "$txt" | grep -c .)
  [ "$na" -eq 0 ] && continue
  echo "== $pkg : serial in $na file(s), text-rewritable $nt =="
  # the binary-missed files = in $all but not $txt
  printf '%s\n' "$all" | while IFS= read -r f; do
    [ -n "$f" ] || continue
    printf '%s\n' "$txt" | grep -qxF "$f" || echo "   BINARY (missed): $f"
  done
done
