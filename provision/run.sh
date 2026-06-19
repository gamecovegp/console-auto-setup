#!/system/bin/sh
# run.sh — run INSIDE the rish shell (uid=2000). Wrapper that sets the env + runs master.sh, so you
# only type a short line.  Usage (inside rish):
#   sh /storage/<sd>/provision/run.sh            # full provision
#   sh /storage/<sd>/provision/run.sh citra      # one (or several) emulators
#   SEAL=1 sh /storage/<sd>/provision/run.sh      # provision + seal (uninstall tooling) at the end
DIR="$(cd "$(dirname "$0")" && pwd)"
: "${PAYLOAD:=$(ls -d /storage/*-*/golden_payload 2>/dev/null | head -1)}"
echo "run.sh: MODE=local  PAYLOAD=$PAYLOAD"
CAS_MODE=local PAYLOAD="$PAYLOAD" sh "$DIR/master.sh" "$@"
