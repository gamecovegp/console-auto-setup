#!/usr/bin/env bash
# Guards the shared-internal-storage capture coupling in lib-root.sh: INTERNAL_DIRS (what capture.sh
# archives from /storage/emulated/0) MUST agree with internal_for() (what restore.sh restores per app).
# Regression: PPSSPP keeps its config+saves in the shared-storage memstick /sdcard/PSP — captured by
# NEITHER data.tar (/data/data) nor adata.tar (/sdcard/Android/data) — so a missing PSP mapping meant
# PPSSPP settings never transferred to a provisioned unit. Run: bash tests/test_internal_dirs.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
in_list(){ case " $INTERNAL_DIRS " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# 1) each app whose config lives in shared storage maps to a dir, and that dir is CAPTURED (in INTERNAL_DIRS)
for pair in \
  "org.ppsspp.ppsspp=PSP" \
  "org.citra.emu=citra-emu" \
  "com.retroarch.aarch64=RetroArch"; do
  app="${pair%%=*}"; want="${pair#*=}"
  got="$(internal_for "$app")"
  [ "$got" = "$want" ] || { echo "FAIL: internal_for($app) = '$got', expected '$want'"; fail=1; }
  in_list "$got" || { echo "FAIL: '$got' (for $app) is NOT in INTERNAL_DIRS='$INTERNAL_DIRS' -> captured never, restored never"; fail=1; }
done

# 2) reverse coupling: every dir capture.sh archives must be restorable by SOME app's internal_for,
#    else we capture data no unit ever gets back (silent waste / drift).
for d in $INTERNAL_DIRS; do
  hit=""
  for app in org.ppsspp.ppsspp org.citra.emu com.retroarch.aarch64; do
    [ "$(internal_for "$app")" = "$d" ] && { hit=1; break; }
  done
  [ -n "$hit" ] || { echo "FAIL: INTERNAL_DIRS entry '$d' has no internal_for() app -> captured but never restored"; fail=1; }
done

# 3) ES-DE must NOT be captured wholesale: its multi-GB tree rides the SD card; only es_settings.xml
#    travels (targeted copy in capture.sh/restore.sh, located via esde_home()/esde_home_for() — see
#    test_esde_settings.sh). Guard against a re-add that would double-capture.
[ -z "$(internal_for org.es_de.frontend)" ] || { echo "FAIL: org.es_de.frontend must have NO internal_for() mapping (es_settings.xml is handled targeted)"; fail=1; }
in_list "ES-DE" && { echo "FAIL: 'ES-DE' must NOT be in INTERNAL_DIRS (only es_settings.xml travels)"; fail=1; }

[ "$fail" -eq 0 ] && echo "test_internal_dirs: ALL PASS" || echo "test_internal_dirs: FAILURES"
exit "$fail"
