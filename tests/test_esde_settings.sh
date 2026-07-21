#!/usr/bin/env bash
# Verifies the ES-DE es_settings.xml capture/restore contract (the ONLY internal ES-DE file the golden
# carries): the golden's per-system alternative-emulator picks (3DS→Citra, DS→melonDS, PS2→NetherSX2…) and
# other frontend settings must SURVIVE the restore, while ROMDirectory/MediaDirectory get re-pointed at the
# provisioned unit's OWN card. Replicates restore.sh's exact snippet against a temp tree (no device).
# Run: bash tests/test_esde_settings.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
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

# --- esde_home / esde_home_kind / esde_home_for / es_setting_value ------------------------------------
# ES-DE Android's home is a USER PICK: the AYN Thor keeps its whole tree on the SD card, the RP6 on
# internal. CAS used to hardcode the internal path, so an SD-home golden captured nothing at all.
eq(){ [ "$2" = "$3" ] || { echo "FAIL($1): got '$2', want '$3'"; fail=1; }; }

# a Thor-shaped device: ES-DE on the card, nothing internal
sdroot="$tmp/sdunit"; mkdir -p "$sdroot/9C33-6BBD/ES-DE/settings" "$sdroot/emulated/0"
eq "esde_home sd"      "$(esde_home "$sdroot")"                      "$sdroot/9C33-6BBD/ES-DE"
eq "kind sd"           "$(esde_home_kind "$(esde_home "$sdroot")")"  "sd"

# an RP6-shaped device: ES-DE internal, a card present but with no ES-DE tree
inroot="$tmp/inunit"; mkdir -p "$inroot/emulated/0/ES-DE/settings" "$inroot/ABCD-1234"
eq "esde_home internal" "$(esde_home "$inroot")"                      "$inroot/emulated/0/ES-DE"
eq "kind internal"      "$(esde_home_kind "$(esde_home "$inroot")")"  "internal"

# no ES-DE anywhere -> empty output AND non-zero rc
noroot="$tmp/noesde"; mkdir -p "$noroot/emulated/0" "$noroot/ABCD-1234"
eq "esde_home none" "$(esde_home "$noroot")" ""
esde_home "$noroot" >/dev/null 2>&1 && { echo "FAIL: esde_home returned 0 with no ES-DE"; fail=1; }

# restore-side resolution: the golden's KIND + this unit's serial -> this unit's ES-DE dir
eq "for sd"       "$(esde_home_for sd 6ED25E36D25E032F "$tmp/u")" "$tmp/u/6ED25E36D25E032F/ES-DE"
eq "for internal" "$(esde_home_for internal 6ED25E36D25E032F "$tmp/u")" "$tmp/u/emulated/0/ES-DE"
# sd-home golden onto a unit with NO card -> empty + non-zero (restore must warn and skip, not guess)
eq "for sd no card" "$(esde_home_for sd '' "$tmp/u")" ""
esde_home_for sd '' "$tmp/u" >/dev/null 2>&1 && { echo "FAIL: esde_home_for sd returned 0 with no card"; fail=1; }

# es_setting_value reads one key out of an es_settings.xml
esv="$tmp/esv.xml"
printf '%s\n' '<bool name="CustomEventScripts" value="true" />' \
              '<string name="MediaDirectory" value="" />' \
              '<string name="ROMDirectory" value="/storage/GOLD-1234/ROMs" />' > "$esv"
eq "esv bool"   "$(es_setting_value CustomEventScripts "$esv")" "true"
eq "esv empty"  "$(es_setting_value MediaDirectory "$esv")"     ""
eq "esv path"   "$(es_setting_value ROMDirectory "$esv")"       "/storage/GOLD-1234/ROMs"
eq "esv absent" "$(es_setting_value NoSuchKey "$esv")"          ""

[ "$fail" -eq 0 ] && echo "test_esde_settings: ALL PASS" || echo "test_esde_settings: FAILURES"
exit "$fail"
