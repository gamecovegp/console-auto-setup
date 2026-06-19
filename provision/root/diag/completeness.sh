#!/system/bin/sh
# completeness.sh — confirm every provisioned app has install + /data/data + Android/data. Read-only.
. "$(dirname "$0")/../lib-root.sh"; is_root || { echo need root; exit 1; }
for p in $(pm list packages -3 | sed 's/package://' | grep -vE 'magisk|termux|shizuku' | sort); do
  i=N; pm path "$p" >/dev/null 2>&1 && i=Y
  d=N; [ -d "/data/data/$p" ] && d=Y
  a="-"; [ -d "/data/media/0/Android/data/$p" ] && a="$(du -sh "/data/media/0/Android/data/$p" 2>/dev/null | cut -f1)"
  printf '%-34s installed=%s data=%s adata=%s\n' "$p" "$i" "$d" "$a"
done
