#!/usr/bin/env bash
# fetch-cores.sh — download ALL RetroArch cores (arm64-v8a) from the libretro buildbot, unzipped.
# Runs on the PC. Output: <outdir>/*.so. Then push to the SD and (rooted) cp into RetroArch's dir.
#   ./fetch-cores.sh [outdir]          (default ./retroarch-cores)
# Resumable: re-run to fetch only what's missing.
set -u
URL="https://buildbot.libretro.com/nightly/android/latest/arm64-v8a"
OUT="${1:-./retroarch-cores}"
mkdir -p "$OUT"
echo "Listing cores from $URL ..."
LIST=$(curl -sL --max-time 60 "$URL/" | grep -oE '[A-Za-z0-9_.-]+_libretro_android\.so\.zip' | sort -u)
n=$(printf '%s\n' "$LIST" | grep -c .)
[ "$n" -gt 0 ] || { echo "no cores found (network?)"; exit 1; }
echo "Downloading $n cores -> $OUT"
i=0; ok=0; fail=""
for z in $LIST; do
  i=$((i+1)); so="${z%.zip}"
  if [ -f "$OUT/$so" ]; then ok=$((ok+1)); continue; fi
  if curl -sfL --max-time 180 -o "$OUT/$z" "$URL/$z" && unzip -oq "$OUT/$z" -d "$OUT" 2>/dev/null; then
    rm -f "$OUT/$z"; ok=$((ok+1)); printf '  [%d/%d] %s\n' "$i" "$n" "$so"
  else
    rm -f "$OUT/$z"; fail="$fail $so"; printf '  [%d/%d] FAILED %s\n' "$i" "$n" "$z"
  fi
done
echo "Done: $ok/$n cores in $OUT ($(du -sh "$OUT" 2>/dev/null | cut -f1))."
[ -n "$fail" ] && echo "Failed (re-run to retry):$fail"
echo "Next: adb push \"$OUT\" /storage/<sd>/retroarch-cores  ; then (rooted) cp into /data/data/com.retroarch.aarch64/cores/"
