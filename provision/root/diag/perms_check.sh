#!/system/bin/sh
. "$(dirname "$0")/../lib-root.sh"; is_root || { echo need root; exit 1; }
SD="$(detect_sd)"
# Report the SPECIAL appops restore.sh grants declaration-driven (lib-root.sh: $SPECIAL_APPOPS) —
# "All files access" (MANAGE_EXTERNAL_STORAGE) and "Install unknown apps" (REQUEST_INSTALL_PACKAGES).
for op in $SPECIAL_APPOPS; do
  echo "=== payload apps that DECLARE $op and their appop state (allow/deny) ==="
  for p in $(cat "$SD/golden_root_payload/pkglist.txt"); do
    dumpsys package "$p" 2>/dev/null | grep -q "$op" || continue
    st=$(appops get "$p" "$op" 2>/dev/null | head -1)
    echo "  $p : ${st:-<no state>}"
  done
  echo
done
