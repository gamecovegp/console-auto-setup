#!/usr/bin/env bash
# Runs grant-persist.sh against a STUB `magisk` and asserts it issues the right --sqlite writes and
# emits the sentinel. No device. Covers BOTH resolution paths:
#   A) magisk on PATH               — a normal shell / dev box.
#   B) magisk NOT on PATH, via override — models `su -c sh <script>`, where the Magisk applet dir is
#      NOT on PATH, so a bare `magisk` fails "inaccessible or not found" (the real-unit bug this guards).
set -u
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/../provision/root/grant-persist.sh"

make_stub() {  # $1 = path to create the magisk stub at
  cat > "$1" <<'STUB'
#!/usr/bin/env bash
echo "$@" >> "$MAGISK_LOG"
case "$*" in
  *"SELECT policy FROM policies WHERE uid=2000"*) echo "policy=2" ;;
esac
STUB
  chmod +x "$1"
}

check() {  # $1 = scenario label; asserts against $MAGISK_LOG and $out
  fail=0
  grep -q "REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)" "$MAGISK_LOG" \
    || { echo "[X] $1: missing shell allow-policy write"; fail=1; }
  grep -q "REPLACE INTO settings (key,value) VALUES('root_access',3)" "$MAGISK_LOG" \
    || { echo "[X] $1: missing root_access=3 write"; fail=1; }
  echo "$out" | grep -q "CAS_GRANT policy=2" \
    || { echo "[X] $1: missing/incorrect CAS_GRANT sentinel: $out"; fail=1; }
  return $fail
}

rc=0

# Scenario A: magisk on PATH.
tmpA="$(mktemp -d)"; make_stub "$tmpA/magisk"
export MAGISK_LOG="$tmpA/log"; : > "$MAGISK_LOG"
out="$(PATH="$tmpA:$PATH" sh "$script")"
check "on-PATH" || rc=1
rm -rf "$tmpA"

# Scenario B: magisk NOT on PATH ($tmpB is NOT added to PATH); resolved only via CAS_MAGISK.
tmpB="$(mktemp -d)"; make_stub "$tmpB/magisk"
export MAGISK_LOG="$tmpB/log"; : > "$MAGISK_LOG"
out="$(CAS_MAGISK="$tmpB/magisk" sh "$script")"
check "off-PATH-fallback" || rc=1
rm -rf "$tmpB"

[ "$rc" = 0 ] && echo "ok: grant-persist.sh (on-PATH + fallback)" || exit 1
