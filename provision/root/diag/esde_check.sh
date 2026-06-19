#!/system/bin/sh
. "$(dirname "$0")/../lib-root.sh"; is_root || { echo need root; exit 1; }
SD="$(detect_sd)"
echo "=== ES-DE /data/data layout ==="
ls -la /data/data/org.es_de.frontend 2>/dev/null
echo; echo "=== ES-DE shared_prefs (where dir choices usually live) ==="
for f in /data/data/org.es_de.frontend/shared_prefs/*.xml; do echo "--- $f ---"; cat "$f" 2>/dev/null; done
echo; echo "=== where ES-DE looks: ROM/media dir refs anywhere in its data ==="
grep -rhiE 'ROMDirectory|MediaDirectory|romDir|ApplicationDataDirectory|/ES-DE|/ROMs|content://' /data/data/org.es_de.frontend 2>/dev/null | head
echo; echo "=== ES-DE home dir on SD (its real config home) ==="
ls /storage/emulated/0/ES-DE 2>/dev/null; echo "--- SD ES-DE ---"; ls "$SD/ES-DE" 2>/dev/null | head
echo; echo "=== es_settings.xml ROMDirectory value (if present) ==="
find "$SD/ES-DE" /storage/emulated/0/ES-DE -name 'es_settings.xml' 2>/dev/null | while read -r f; do echo "$f:"; grep -iE 'ROMDirectory|MediaDirectory' "$f" 2>/dev/null; done
