#!/usr/bin/env bash
# Runs grant-persist.sh against a STUB `magisk` and asserts it issues the right --sqlite writes
# and emits the sentinel. No device.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/../provision/root/grant-persist.sh"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
# stub magisk: log every arg line, and answer the SELECT read-back with policy=2
cat > "$tmp/magisk" <<'STUB'
#!/usr/bin/env bash
echo "$@" >> "$MAGISK_LOG"
case "$*" in
  *"SELECT policy FROM policies WHERE uid=2000"*) echo "policy=2" ;;
esac
STUB
chmod +x "$tmp/magisk"
export MAGISK_LOG="$tmp/log"; : > "$MAGISK_LOG"
out="$(PATH="$tmp:$PATH" sh "$script")"
fail=0
grep -q "REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)" "$MAGISK_LOG" \
  || { echo "[X] missing shell allow-policy write"; fail=1; }
grep -q "REPLACE INTO settings (key,value) VALUES('root_access',3)" "$MAGISK_LOG" \
  || { echo "[X] missing root_access=3 write"; fail=1; }
echo "$out" | grep -q "CAS_GRANT policy=2" \
  || { echo "[X] missing/incorrect CAS_GRANT sentinel: $out"; fail=1; }
[ "$fail" = 0 ] && echo "ok: grant-persist.sh" || exit 1
