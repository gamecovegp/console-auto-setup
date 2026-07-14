#!/usr/bin/env bash
# Verifies the ES-DE es_settings.xml capture/restore contract (the ONLY internal ES-DE file the golden
# carries): the golden's per-system alternative-emulator picks (3DS→Citra, DS→melonDS, PS2→NetherSX2…) and
# other frontend settings must SURVIVE the restore, while ROMDirectory/MediaDirectory get re-pointed at the
# provisioned unit's OWN card. Replicates restore.sh's exact snippet against a temp tree (no device).
# Run: bash tests/test_esde_settings.sh
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# --- a golden es_settings.xml as captured from the golden unit (old card serial GOLD-1234) --------------
mkdir -p "$tmp/payload" "$tmp/dev/storage/emulated/0/ES-DE/settings"
GOLD="$tmp/payload/es_settings.xml"
cat > "$GOLD" <<'XML'
<string name="Theme" value="gamecove" />
<string name="AlternativeEmulator.3ds" value="Citra MMJ" />
<string name="AlternativeEmulator.nds" value="melonDS Nightly" />
<string name="AlternativeEmulator.ps2" value="NetherSX2" />
<string name="MediaDirectory" value="/storage/GOLD-1234/ES-DE/downloaded_media" />
<string name="ROMDirectory" value="/storage/GOLD-1234/ROMs" />
XML

# --- replicate restore.sh's es_settings.xml handling for THIS unit (card serial 6ED25E36D25E032F) -------
ES_SET="$tmp/dev/storage/emulated/0/ES-DE/settings/es_settings.xml"
SERIAL="6ED25E36D25E032F"
# 2c-pre) restore the golden file
mkdir -p "${ES_SET%/*}"; cp "$GOLD" "$ES_SET"
# 2c) MediaDirectory -> this unit's card
MEDIA_DIR="/storage/$SERIAL/ES-DE/downloaded_media"
sed '/name="MediaDirectory"/d' "$ES_SET" > "$ES_SET.cas" && mv "$ES_SET.cas" "$ES_SET"   # portable in-place (BSD sed -i differs)
[ -s "$ES_SET" ] && [ -n "$(tail -c1 "$ES_SET" 2>/dev/null)" ] && printf '\n' >> "$ES_SET"
printf '<string name="MediaDirectory" value="%s" />\n' "$MEDIA_DIR" >> "$ES_SET"
# 2d) ROMDirectory -> this unit's card
ROM_DIR="/storage/$SERIAL/ROMs"
sed '/name="ROMDirectory"/d' "$ES_SET" > "$ES_SET.cas" && mv "$ES_SET.cas" "$ES_SET"   # portable in-place (BSD sed -i differs)
[ -s "$ES_SET" ] && [ -n "$(tail -c1 "$ES_SET" 2>/dev/null)" ] && printf '\n' >> "$ES_SET"
printf '<string name="ROMDirectory" value="%s" />\n' "$ROM_DIR" >> "$ES_SET"

# --- assertions ----------------------------------------------------------------------------------------
has(){ grep -qF "$1" "$ES_SET" || { echo "FAIL(missing): $1"; fail=1; }; }
count(){ n="$(grep -cF "$1" "$ES_SET")"; [ "$n" = "$2" ] || { echo "FAIL(count $1 = $n, want $2)"; fail=1; }; }

# the emulator-per-system picks survived
has '<string name="AlternativeEmulator.3ds" value="Citra MMJ" />'
has '<string name="AlternativeEmulator.nds" value="melonDS Nightly" />'
has '<string name="AlternativeEmulator.ps2" value="NetherSX2" />'
has '<string name="Theme" value="gamecove" />'                       # other settings preserved too
# ROM/Media now point at THIS unit's card, exactly once each (no duplicates), old serial gone
has "<string name=\"ROMDirectory\" value=\"/storage/$SERIAL/ROMs\" />"
has "<string name=\"MediaDirectory\" value=\"/storage/$SERIAL/ES-DE/downloaded_media\" />"
count 'name="ROMDirectory"' 1
count 'name="MediaDirectory"' 1
grep -qF "GOLD-1234" "$ES_SET" && { echo "FAIL: old golden card serial still present"; fail=1; }

[ "$fail" -eq 0 ] && echo "test_esde_settings: ALL PASS" || echo "test_esde_settings: FAILURES"
exit "$fail"
