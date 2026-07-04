#!/system/bin/sh
# run.sh — run INSIDE the rish shell (uid=2000). Wrapper that sets the env + runs master.sh, so you
# only type a short line.  Usage (inside rish):
#   sh /storage/<sd>/provision/run.sh            # full provision
#   sh /storage/<sd>/provision/run.sh citra      # one (or several) emulators
#   SEAL=1 sh /storage/<sd>/provision/run.sh      # provision + seal (uninstall tooling) at the end
DIR="$(cd "$(dirname "$0")" && pwd)"
# Find golden_payload on the SD card, in ANY volume-id format (skip internal 'emulated'/'self',
# never assume a hyphenated id — a big exFAT ROM card mounts hyphen-LESS).
if [ -z "${PAYLOAD:-}" ]; then
  for d in /storage/*/golden_payload; do
    case "$d" in */emulated/golden_payload|*/self/golden_payload) continue ;; esac
    [ -e "$d" ] && { PAYLOAD="$d"; break; }
  done
fi
echo "run.sh: MODE=local  PAYLOAD=$PAYLOAD"
CAS_MODE=local PAYLOAD="$PAYLOAD" sh "$DIR/master.sh" "$@"
