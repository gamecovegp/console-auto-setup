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
# every fixture below plants a real es_settings.xml under settings/ — esde_home() probes for the FILE
# (Important-3 fix), not just the directory, so a bare `mkdir -p .../settings` is not a home.
plant_esde(){ mkdir -p "$1/settings"; printf '%s\n' '<bool name="CustomEventScripts" value="true" />' > "$1/settings/es_settings.xml"; }

# a Thor-shaped device: ES-DE on the card, nothing internal
sdroot="$tmp/sdunit"; mkdir -p "$sdroot/emulated/0"; plant_esde "$sdroot/9C33-6BBD/ES-DE"
eq "esde_home sd"      "$(esde_home "$sdroot")"                      "$sdroot/9C33-6BBD/ES-DE"
eq "kind sd"           "$(esde_home_kind "$(esde_home "$sdroot")")"  "sd"

# an RP6-shaped device: ES-DE internal, a card present but with no ES-DE tree
inroot="$tmp/inunit"; mkdir -p "$inroot/ABCD-1234"; plant_esde "$inroot/emulated/0/ES-DE"
eq "esde_home internal" "$(esde_home "$inroot")"                      "$inroot/emulated/0/ES-DE"
eq "kind internal"      "$(esde_home_kind "$(esde_home "$inroot")")"  "internal"

# no ES-DE anywhere -> empty output AND non-zero rc
noroot="$tmp/noesde"; mkdir -p "$noroot/emulated/0" "$noroot/ABCD-1234"
eq "esde_home none" "$(esde_home "$noroot")" ""
esde_home "$noroot" >/dev/null 2>&1 && { echo "FAIL: esde_home returned 0 with no ES-DE"; fail=1; }

# REGRESSION (Important 3, half 1) — these handhelds ship "android-setup" SD cards preloaded with an EMPTY
# ES-DE/settings/ dir (no es_settings.xml inside). A directory probe would wrongly pick that stale card as
# the home for an RP6 (real ES-DE home = internal), flipping esde_home=sd on the next Save and pointing
# restore at the card instead of internal for every future clone. The FILE probe must see through it.
staleroot="$tmp/staleunit"
mkdir -p "$staleroot/9C33-6BBD/ES-DE/settings"          # settings/ dir present, but EMPTY — no settings file
plant_esde "$staleroot/emulated/0/ES-DE"                 # the unit's REAL (internal) home
eq "stale card dir does not shadow internal home" "$(esde_home "$staleroot")" "$staleroot/emulated/0/ES-DE"
eq "stale card kind resolves internal, not sd"    "$(esde_home_kind "$(esde_home "$staleroot")")" "internal"

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

# esde_home with NO ARGUMENT — the shape capture.sh actually calls in production (every positional-arg
# assertion above passes storage_root explicitly, which never exercises the CAS_STORAGE_ROOT/default path).
noargroot="$tmp/noargunit"; mkdir -p "$noargroot/emulated/0"; plant_esde "$noargroot/9C33-6BBD/ES-DE"
CAS_STORAGE_ROOT="$noargroot"; export CAS_STORAGE_ROOT
eq "esde_home no-arg" "$(esde_home)" "$noargroot/9C33-6BBD/ES-DE"
unset CAS_STORAGE_ROOT

# --- capture side: record the kind (ONLY after settings actually captured), tar the scripts tree -------
# The golden carries the ES-DE/scripts/ tree (the 7 esdecompanion-*.sh hooks) so a unit provisions even
# when its SD image predates the Companion setup. Replicates capture.sh's CURRENT snippet against a temp
# tree — including the Important-3-half-2 ordering: esde_home= is appended ONLY inside the branch where the
# es_settings.xml cp actually SUCCEEDED, never unconditionally right after ESDE_HOME resolves.
gsd="$tmp/golden"; mkdir -p "$gsd/emulated/0"; plant_esde "$gsd/9C33-6BBD/ES-DE"
GH="$gsd/9C33-6BBD/ES-DE"
for ev in game-start game-end game-select system-select \
          screensaver-start screensaver-end screensaver-game-select; do
  mkdir -p "$GH/scripts/$ev"; printf '#!/bin/sh\n' > "$GH/scripts/$ev/esdecompanion-$ev.sh"
done
PAY="$tmp/pay"; mkdir -p "$PAY"; printf 'golden_serial=9C33-6BBD\n' > "$PAY/global.meta"

ESDE_HOME="$(esde_home "$gsd")"
if [ -n "$ESDE_HOME" ] && [ -f "$ESDE_HOME/settings/es_settings.xml" ]; then
  if cp "$ESDE_HOME/settings/es_settings.xml" "$PAY/es_settings.xml"; then
    echo "esde_home=$(esde_home_kind "$ESDE_HOME")" >> "$PAY/global.meta"
  fi
fi
if [ -n "$ESDE_HOME" ] && [ -d "$ESDE_HOME/scripts" ] && [ -n "$(ls -A "$ESDE_HOME/scripts" 2>/dev/null)" ]; then
  tar -cf "$PAY/es_scripts.tar" -C "$ESDE_HOME" scripts 2>/dev/null
fi

eq "capture kind" "$(sed -n 's/^esde_home=//p' "$PAY/global.meta")" "sd"
[ -f "$PAY/es_settings.xml" ] || { echo "FAIL: es_settings.xml not captured from the SD home"; fail=1; }
[ -f "$PAY/es_scripts.tar" ] || { echo "FAIL: es_scripts.tar not captured"; fail=1; }
n="$(tar -tf "$PAY/es_scripts.tar" 2>/dev/null | grep -c 'esdecompanion-.*\.sh$')"
eq "captured hooks" "$n" "7"
# the golden_serial line the existing pipeline depends on must survive the append
grep -q '^golden_serial=9C33-6BBD$' "$PAY/global.meta" || { echo "FAIL: global.meta clobbered"; fail=1; }

# REGRESSION (Important 3, half 2) — a home that RESOLVES (its settings file exists) but whose cp INTO the
# payload fails (full/read-only destination) must NOT leave an esde_home= key behind: recording a kind for
# a golden that captured nothing usable would make restore trust a location with no es_settings.xml at it.
# Skipped when running as root — chmod can't block root's writes, so the failure this test injects wouldn't
# actually happen and the assertions below would be testing nothing.
if [ "$(id -u 2>/dev/null)" != 0 ]; then
  gsd2="$tmp/golden2"; mkdir -p "$gsd2/emulated/0"; plant_esde "$gsd2/9C33-6BBD/ES-DE"
  PAY2="$tmp/pay2"; mkdir -p "$PAY2"; printf 'golden_serial=9C33-6BBD\n' > "$PAY2/global.meta"
  chmod 555 "$PAY2"                                       # read-only: cp INTO it must fail
  ESDE_HOME2="$(esde_home "$gsd2")"
  if [ -n "$ESDE_HOME2" ] && [ -f "$ESDE_HOME2/settings/es_settings.xml" ]; then
    if cp "$ESDE_HOME2/settings/es_settings.xml" "$PAY2/es_settings.xml" 2>/dev/null; then
      echo "esde_home=$(esde_home_kind "$ESDE_HOME2")" >> "$PAY2/global.meta"
    fi
  fi
  chmod 755 "$PAY2"                                       # restore so the EXIT trap can clean up "$tmp"
  [ -f "$PAY2/es_settings.xml" ] && { echo "FAIL: test setup bug — cp into a read-only dir should have failed"; fail=1; }
  grep -q '^esde_home=' "$PAY2/global.meta" && { echo "FAIL: esde_home= recorded even though es_settings.xml capture failed"; fail=1; }
fi

# --- restore side: resolve THIS unit's home from the golden's kind ------------------------------------
# "Follow the golden": restore never probes the unit. An SD-home golden lands on the unit's OWN card
# (different volume id), an internal-home golden lands on internal, and a golden with NO esde_home key
# reads as internal — that back-compat default is what keeps every pre-existing RP6 golden working.
uroot="$tmp/unit"; mkdir -p "$uroot/AAAA-BBBB" "$uroot/emulated/0"
resolve(){ # $1 = payload dir, $2 = this unit's card serial -> echoes ES_HOME ("" if unresolvable)
  _k="$(sed -n 's/^esde_home=//p' "$1/global.meta" 2>/dev/null)"; [ -n "$_k" ] || _k=internal
  esde_home_for "$_k" "$2" "$uroot"
}
mkdir -p "$tmp/p_sd" "$tmp/p_int" "$tmp/p_old"
printf 'esde_home=sd\n'       > "$tmp/p_sd/global.meta"
printf 'esde_home=internal\n' > "$tmp/p_int/global.meta"
printf 'golden_serial=X\n'    > "$tmp/p_old/global.meta"        # pre-2026-07-21 golden: NO esde_home key
eq "resolve sd"       "$(resolve "$tmp/p_sd"  AAAA-BBBB)" "$uroot/AAAA-BBBB/ES-DE"
eq "resolve internal" "$(resolve "$tmp/p_int" AAAA-BBBB)" "$uroot/emulated/0/ES-DE"
eq "resolve legacy"   "$(resolve "$tmp/p_old" AAAA-BBBB)" "$uroot/emulated/0/ES-DE"
eq "resolve sd nocard" "$(resolve "$tmp/p_sd" '')"        ""

# restoring both artefacts into the resolved home
ES_HOME="$(resolve "$tmp/p_sd" AAAA-BBBB)"
cp "$PAY/es_settings.xml" "$tmp/es_src.xml"                      # reuse Task 2's captured payload
mkdir -p "$ES_HOME/settings"
cp "$tmp/es_src.xml" "$ES_HOME/settings/es_settings.xml"
tar -xf "$PAY/es_scripts.tar" -C "$ES_HOME" 2>/dev/null
[ -f "$ES_HOME/settings/es_settings.xml" ] || { echo "FAIL: settings not restored into the SD home"; fail=1; }
[ -x "$ES_HOME/scripts/game-start/esdecompanion-game-start.sh" ] \
  || [ -f "$ES_HOME/scripts/game-start/esdecompanion-game-start.sh" ] \
  || { echo "FAIL: game-start hook not restored"; fail=1; }
m="$(find "$ES_HOME/scripts" -name 'esdecompanion-*.sh' 2>/dev/null | wc -l | tr -d ' ')"
eq "restored hooks" "$m" "7"

# --- MediaDirectory / ROMDirectory: rewrite rule by home kind -----------------------------------------
# internal-home  -> always force to THIS unit's card (unchanged behaviour; the RP6 path must not move).
# sd-home, empty -> LEAVE EMPTY. ES-DE resolves it inside its own home, which IS this card, so an empty
#                   value is portable across cards; an absolute one would carry a foreign volume id.
# sd-home, set   -> re-point, because the value carries the GOLDEN's volume id.
rewrite(){ # $1 = file, $2 = kind, $3 = this unit's serial, $4 = key, $5 = replacement value
  if [ "$2" = internal ] || [ -n "$(es_setting_value "$4" "$1")" ]; then
    sed "/name=\"$4\"/d" "$1" > "$1.cas" && mv "$1.cas" "$1"
    [ -s "$1" ] && [ -n "$(tail -c1 "$1" 2>/dev/null)" ] && printf '\n' >> "$1"
    printf '<string name="%s" value="%s" />\n' "$4" "$5" >> "$1"
  fi
}
U=6ED25E36D25E032F

f="$tmp/r_sd_empty.xml"; printf '%s\n' '<string name="MediaDirectory" value="" />' > "$f"
rewrite "$f" sd "$U" MediaDirectory "/storage/$U/ES-DE/downloaded_media"
eq "sd+empty stays empty" "$(es_setting_value MediaDirectory "$f")" ""

f="$tmp/r_sd_set.xml"; printf '%s\n' '<string name="MediaDirectory" value="/storage/GOLD-1234/ES-DE/downloaded_media" />' > "$f"
rewrite "$f" sd "$U" MediaDirectory "/storage/$U/ES-DE/downloaded_media"
eq "sd+set re-pointed" "$(es_setting_value MediaDirectory "$f")" "/storage/$U/ES-DE/downloaded_media"
grep -qF GOLD-1234 "$f" && { echo "FAIL: golden volume id survived an sd-home rewrite"; fail=1; }

f="$tmp/r_int_empty.xml"; printf '%s\n' '<string name="MediaDirectory" value="" />' > "$f"
rewrite "$f" internal "$U" MediaDirectory "/storage/$U/ES-DE/downloaded_media"
eq "internal+empty forced" "$(es_setting_value MediaDirectory "$f")" "/storage/$U/ES-DE/downloaded_media"
eq "internal no dupes" "$(grep -c 'name="MediaDirectory"' "$f")" "1"

f="$tmp/r_sd_rom.xml"; printf '%s\n' '<string name="ROMDirectory" value="" />' > "$f"
rewrite "$f" sd "$U" ROMDirectory "/storage/$U/ROMs"
eq "sd+empty ROMDirectory stays empty" "$(es_setting_value ROMDirectory "$f")" ""

# --- REGRESSION (Important 2): CAS_ES_MEDIA=internal must not be defeated by an SD-home golden's empty
# MediaDirectory --------------------------------------------------------------------------------------
# "empty is portable" only holds when the target IS this card (CAS_ES_MEDIA=sd, the default): in internal
# mode the PC pushes box art to internal storage regardless of where ES-DE's home is, so on an SD-home
# golden (exactly the live Thor) an empty value must be FORCED, same as an internal-home golden's is.
# Mirrors restore.sh's real condition — a SEPARATE helper from rewrite() above (not shared) because
# ROMDirectory has no media-mode concept and must not gain a CAS_ES_MEDIA check it was never given.
media_rewrite(){ # $1=file $2=EHK(golden's home kind) $3=CAS_ES_MEDIA $4=replacement value
  if [ "$2" = internal ] || [ "${3:-sd}" != sd ] || [ -n "$(es_setting_value MediaDirectory "$1")" ]; then
    sed '/name="MediaDirectory"/d' "$1" > "$1.cas" && mv "$1.cas" "$1"
    [ -s "$1" ] && [ -n "$(tail -c1 "$1" 2>/dev/null)" ] && printf '\n' >> "$1"
    printf '<string name="MediaDirectory" value="%s" />\n' "$4" >> "$1"
  fi
}
f="$tmp/r_sd_empty_media_sd.xml"; printf '%s\n' '<string name="MediaDirectory" value="" />' > "$f"
media_rewrite "$f" sd sd "/storage/$U/ES-DE/downloaded_media"
eq "sd-home, CAS_ES_MEDIA=sd, empty stays empty" "$(es_setting_value MediaDirectory "$f")" ""

f="$tmp/r_sd_empty_media_internal.xml"; printf '%s\n' '<string name="MediaDirectory" value="" />' > "$f"
media_rewrite "$f" sd internal "/storage/emulated/0/ES-DE/downloaded_media"
eq "sd-home, CAS_ES_MEDIA=internal, empty gets FORCED (not left empty)" \
   "$(es_setting_value MediaDirectory "$f")" "/storage/emulated/0/ES-DE/downloaded_media"

# --- guard: Companion restored, but the golden never enabled its event scripts ------------------------
companion_guard(){ # $1 = es_settings.xml, $2 = RPKGS -> echoes the warning text, or nothing
  echo "$2" | grep -q com.esde.companion || return 0
  [ "$(es_setting_value CustomEventScripts "$1")" = true ] && return 0
  echo "WARN"
}
f="$tmp/g_off.xml"; printf '%s\n' '<bool name="CustomEventScripts" value="false" />' > "$f"
eq "guard fires when off"  "$(companion_guard "$f" 'org.es_de.frontend com.esde.companion')" "WARN"
f="$tmp/g_on.xml";  printf '%s\n' '<bool name="CustomEventScripts" value="true" />'  > "$f"
eq "guard silent when on"  "$(companion_guard "$f" 'org.es_de.frontend com.esde.companion')" ""
eq "guard silent w/o companion" "$(companion_guard "$f" 'org.es_de.frontend')" ""
f="$tmp/g_absent.xml"; : > "$f"
eq "guard fires when key absent" "$(companion_guard "$f" 'com.esde.companion')" "WARN"

[ "$fail" -eq 0 ] && echo "test_esde_settings: ALL PASS" || echo "test_esde_settings: FAILURES"
exit "$fail"
