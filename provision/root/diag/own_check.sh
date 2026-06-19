#!/system/bin/sh
. "$(dirname "$0")/../lib-root.sh"; is_root || { echo need root; exit 1; }
echo "=== FUSE view: /sdcard/Android/data ownership ==="
ls -lan /sdcard/Android/data | grep -iE 'eden|duckstation|aethersx2|citra'
echo
echo "=== backing store: /data/media/0/Android/data ownership ==="
ls -lan /data/media/0/Android/data 2>/dev/null | grep -iE 'eden|duckstation|aethersx2|citra'
echo
echo "=== one level deeper: eden/files backing ownership ==="
ls -lan /data/media/0/Android/data/dev.eden.eden_emulator 2>/dev/null
echo
echo "=== app internal uids ==="
for p in dev.eden.eden_emulator com.github.stenzek.duckstation xyz.aethersx2.tturnip; do
  echo "$p internal_uid=$(stat -c %u /data/data/$p 2>/dev/null)"
done
