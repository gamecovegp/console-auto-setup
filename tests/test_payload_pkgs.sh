#!/usr/bin/env bash
# Local smoke test for payload_pkgs (pure file IO — no device). Run: bash tests/test_payload_pkgs.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# 1) reads pkglist.txt verbatim when present and non-empty
printf 'com.foo\ncom.bar\n' > "$tmp/pkglist.txt"
got="$(payload_pkgs "$tmp")"
[ "$got" = "$(printf 'com.foo\ncom.bar')" ] || { echo "FAIL(1): pkglist read got [$got]"; fail=1; }

# 2) falls back to $PKGS (newline-separated) when pkglist.txt is missing
rm -f "$tmp/pkglist.txt"
got="$(payload_pkgs "$tmp")"
echo "$got" | grep -qx 'org.es_de.frontend'   || { echo "FAIL(2a): fallback missing es-de"; fail=1; }
[ "$(echo "$got" | wc -l)" -ge 2 ]            || { echo "FAIL(2b): fallback not multiline"; fail=1; }

# 3) empty pkglist.txt also falls back
: > "$tmp/pkglist.txt"
got="$(payload_pkgs "$tmp")"
echo "$got" | grep -qx 'org.es_de.frontend'   || { echo "FAIL(3): empty-file fallback"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: payload_pkgs"; exit 0; } || exit 1
