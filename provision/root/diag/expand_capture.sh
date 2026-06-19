#!/system/bin/sh
# expand_capture.sh — one-off: add the pieces the original capture missed (gamehub launcher +
# internal-storage Citra/RetroArch dirs) into the existing payload, and remove this session's UI-dump junk.
. "$(dirname "$0")/../lib-root.sh"; is_root || { echo "need root"; exit 1; }
SD="$(detect_sd)"; P="$SD/golden_root_payload"
# gamehub.lite (the front-end launcher): apk + data + adata + meta
mkdir -p "$P/gamehub.lite/apk"
for ap in $(pm path gamehub.lite 2>/dev/null | sed 's/^package://'); do cp "$ap" "$P/gamehub.lite/apk/" 2>/dev/null; done
tar -cf "$P/gamehub.lite/data.tar" -C /data/data --exclude=gamehub.lite/cache --exclude=gamehub.lite/code_cache gamehub.lite 2>/dev/null
[ -d /sdcard/Android/data/gamehub.lite ] && tar -cf "$P/gamehub.lite/adata.tar" -C /sdcard/Android/data gamehub.lite 2>/dev/null
echo "golden_uid=$(app_uid gamehub.lite)" > "$P/gamehub.lite/meta"
ok "gamehub.lite: $(ls "$P"/gamehub.lite/apk/*.apk 2>/dev/null | grep -c .) apk, data $(du -sh "$P"/gamehub.lite/data.tar 2>/dev/null | cut -f1)"
# internal-storage shared dirs
for d in $INTERNAL_DIRS; do
  [ -d "/storage/emulated/0/$d" ] && tar -cf "$P/internal_$d.tar" -C /storage/emulated/0 "$d" 2>/dev/null \
    && ok "internal:$d -> $(du -sh "$P/internal_$d.tar" 2>/dev/null | cut -f1)"
done
# clean THIS session's UI-dump leftovers on internal storage (junk)
rm -f /storage/emulated/0/m.xml /storage/emulated/0/mg*.xml /storage/emulated/0/s.xml /storage/emulated/0/s2.xml \
      /storage/emulated/0/s3.xml /storage/emulated/0/su.xml /storage/emulated/0/su2.xml /storage/emulated/0/su_prompt.xml \
      /storage/emulated/0/p.xml /storage/emulated/0/ui.xml /storage/emulated/0/uidump.xml 2>/dev/null
ok "cleaned UI-dump junk; payload now $(du -sh "$P" 2>/dev/null | cut -f1), $(ls "$P" | grep -c .) entries"
