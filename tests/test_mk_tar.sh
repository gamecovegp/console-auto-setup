#!/usr/bin/env bash
# Guards mk_tar (in lib-root.sh) — the robust exclude-aware tar helper capture.sh uses for every app's
# data.tar/adata.tar. Regression: a CONFIG-ONLY app (manifest "<pkg> config", no apk axis) never hit the
# apk branch's `mkdir -p "$P/$pkg/apk"`, so mk_tar was asked to write "$P/$pkg/data.tar" into a dir that
# did not exist. tar could not create the output file → the archive read as "corrupt" → the whole golden
# capture failed rc=1 (seen live: xyz.aethersx2.android on mangmi-air-x-256). mk_tar must create its
# output's parent dir itself. Run: bash tests/test_mk_tar.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

command -v mk_tar >/dev/null 2>&1 || { echo "FAIL: mk_tar is not defined in lib-root.sh"; exit 1; }

# a small source tree to archive: <src>/pkg/{keep,cache/junk}
mkdir -p "$tmp/src/pkg/cache"
echo keep > "$tmp/src/pkg/keep"
echo junk > "$tmp/src/pkg/cache/junk"

# 1) REGRESSION: output parent dir does NOT exist — mk_tar must create it and produce a readable archive
out="$tmp/out/pkg/data.tar"        # $tmp/out and $tmp/out/pkg do NOT exist yet
mk_tar "$out" "$tmp/src" "pkg" "pkg/cache" >/dev/null 2>&1 \
  || { echo "FAIL(1a): mk_tar returned non-zero writing into a missing parent dir"; fail=1; }
[ -f "$out" ] || { echo "FAIL(1b): mk_tar did not create the output archive"; fail=1; }
tar -tf "$out" >/dev/null 2>&1 || { echo "FAIL(1c): produced archive is not readable"; fail=1; }

# 2) excludes are honoured: pkg/cache must NOT be in the archive
if tar -tf "$out" 2>/dev/null | grep -q 'pkg/cache/junk'; then
  echo "FAIL(2): excluded pkg/cache was captured anyway"; fail=1
fi
tar -tf "$out" 2>/dev/null | grep -q 'pkg/keep' \
  || { echo "FAIL(2b): kept file pkg/keep missing from archive"; fail=1; }

# 3) meta-write invariant: the same missing-parent problem broke `> "$P/$pkg/meta"`. After mk_tar the
#    per-pkg dir exists, so a sibling meta write into it must succeed.
echo "axes=config" > "$tmp/out/pkg/meta" 2>/dev/null \
  || { echo "FAIL(3): could not write meta beside the archive"; fail=1; }

[ "$fail" -eq 0 ] && echo "test_mk_tar: ALL PASS" || echo "test_mk_tar: FAILURES"
exit "$fail"
