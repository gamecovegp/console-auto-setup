#!/usr/bin/env bash
# Local test for pb_rewrite_serial (lib-root.sh): the length-correct SD-serial rewrite for a DataStore
# Preferences protobuf. Guards the regression where restore.sh's naive `sed` corrupted ES-DE's
# settings.preferences_pb (NUL-free -> grep -I calls it text) when the golden serial and the unit's SD
# serial differ in length, crashing ES-DE with "Unable to parse preferences proto". No device needed.
# Run: bash tests/test_pb_serial_rewrite.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# Build a valid 2-entry Preferences proto: startupCounter(int 73) + romDirectory(string path w/ serial).
# Wire format: repeated entry (field1 wt2); entry = {key str(f1), value msg(f2)}; string value = f5 (0x2a).
make_proto(){ # $1 serial, $2 outfile
  s="$1"; out="$2"; path="/storage/$s/ROMs"; plen=${#path}
  vmsg_len=$((2 + plen)); body_len=$((2 + 12 + 2 + vmsg_len))
  {
    printf '\012\024\012\016startupCounter\022\002\030\111'                 # entry A: int 73
    printf '\012'; printf "$(printf '\\%03o' "$body_len")"                  # entry B header
    printf '\012\014romDirectory\022'; printf "$(printf '\\%03o' "$vmsg_len")"
    printf '\052'; printf "$(printf '\\%03o' "$plen")"; printf '%s' "$path" # string value
  } > "$out"
}

OLD="9C33-6BBD"; NEW="6ED25E36D25E032F"    # 9-char FAT id -> 16-char exFAT id (the real bench mismatch)

# --- T1: rewrite succeeds, points at the new serial, old gone, and stays parseable (round-trips) -------
make_proto "$OLD" "$tmp/a.pb"; cp "$tmp/a.pb" "$tmp/a.orig"
pb_rewrite_serial "$tmp/a.pb" "$OLD" "$NEW" >/dev/null 2>&1 || { echo "FAIL(T1 rc)"; fail=1; }
grep -q "/storage/$NEW/ROMs" "$tmp/a.pb" || { echo "FAIL(T1 new serial path missing)"; fail=1; }
grep -q "$OLD" "$tmp/a.pb" && { echo "FAIL(T1 old serial still present)"; fail=1; }
[ "$(wc -c < "$tmp/a.pb")" -eq "$(( $(wc -c < "$tmp/a.orig") + 7 ))" ] || { echo "FAIL(T1 size: +7 expected for 9->16)"; fail=1; }
# round-trip NEW->OLD must reproduce the original bytes exactly (proves lengths re-encode canonically)
pb_rewrite_serial "$tmp/a.pb" "$NEW" "$OLD" >/dev/null 2>&1
cmp -s "$tmp/a.pb" "$tmp/a.orig" || { echo "FAIL(T1 round-trip not byte-identical)"; fail=1; }

# --- T2: same-length serial (9->9) also works -----------------------------------------------------------
make_proto "$OLD" "$tmp/b.pb"
pb_rewrite_serial "$tmp/b.pb" "$OLD" "1234-5678" >/dev/null 2>&1 || { echo "FAIL(T2 rc)"; fail=1; }
grep -q "/storage/1234-5678/ROMs" "$tmp/b.pb" || { echo "FAIL(T2 rewrite)"; fail=1; }

# --- T3: serial absent -> no-op, file untouched ---------------------------------------------------------
make_proto "ZZZZ-ZZZZ" "$tmp/c.pb"; cp "$tmp/c.pb" "$tmp/c.orig"
pb_rewrite_serial "$tmp/c.pb" "$OLD" "$NEW" >/dev/null 2>&1 || { echo "FAIL(T3 rc)"; fail=1; }
cmp -s "$tmp/c.pb" "$tmp/c.orig" || { echo "FAIL(T3 no-op changed the file)"; fail=1; }

# --- T4: malformed proto that still CONTAINS the serial -> abort, file LEFT UNCHANGED, non-zero rc ------
printf 'xxAAAA9C33-6BBDnot-a-protobuf-at-all' > "$tmp/d.pb"; cp "$tmp/d.pb" "$tmp/d.orig"
if pb_rewrite_serial "$tmp/d.pb" "$OLD" "$NEW" >/dev/null 2>&1; then echo "FAIL(T4 should have returned non-zero)"; fail=1; fi
cmp -s "$tmp/d.pb" "$tmp/d.orig" || { echo "FAIL(T4 corrupted a file it could not safely rewrite)"; fail=1; }
[ -f "$tmp/d.pb.casnew" ] && { echo "FAIL(T4 left a temp file behind)"; fail=1; }

[ "$fail" -eq 0 ] && echo "test_pb_serial_rewrite: ALL PASS" || echo "test_pb_serial_rewrite: FAILURES"
exit "$fail"
