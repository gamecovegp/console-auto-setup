#!/system/bin/sh
. "$(dirname "$0")/../lib-root.sh"; is_root || { echo need root; exit 1; }
SD="$(detect_sd)"
echo "=== ES-DE MANAGE_EXTERNAL_STORAGE (All files access) state ==="
appops get org.es_de.frontend MANAGE_EXTERNAL_STORAGE 2>/dev/null
echo
echo "=== payload apps that DECLARE MANAGE_EXTERNAL_STORAGE ==="
for p in $(cat "$SD/golden_root_payload/pkglist.txt"); do
  dumpsys package "$p" 2>/dev/null | grep -q MANAGE_EXTERNAL_STORAGE && echo "  $p"
done
echo
echo "=== their current appop state (allow/deny) ==="
for p in $(cat "$SD/golden_root_payload/pkglist.txt"); do
  dumpsys package "$p" 2>/dev/null | grep -q MANAGE_EXTERNAL_STORAGE || continue
  st=$(appops get "$p" MANAGE_EXTERNAL_STORAGE 2>/dev/null | head -1)
  echo "  $p : $st"
done
