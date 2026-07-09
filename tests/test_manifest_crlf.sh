#!/usr/bin/env bash
# ROOT-CAUSE REGRESSION — Windows Download: "no APK in payload" for EVERY app while the payload sat on the
# device, complete. CAS generates the device manifest on the PC with pathlib.write_text(), which on Windows
# translates "\n" -> "\r\n". restore.sh reads packages from it with awk, whose default field separator does
# NOT include \r, so every $pkg became "com.flycast.emulator\r" and "$P/$pkg/apk/"*.apk pointed at a path
# with a carriage return in the middle -> glob miss -> "no APK in payload". (Python's str.split() DOES strip
# \r, which is why the PC-side _validate_payload passed on the same manifest — the two sides disagreed.)
# The device-side parsers must therefore tolerate CRLF, whoever wrote the file (incl. legacy capture-manifest
# files already on the library drive, or one saved from Notepad). Run: bash tests/test_manifest_crlf.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# A manifest exactly as Windows CAS writes it: CRLF line endings.
printf '# p (deploy)\r\ncom.flycast.emulator\r\ncom.bar apk\r\ncom.baz config\r\n@settings on\r\n@grants off\r\n' > "$tmp/m"

# 1) package names must come back WITHOUT a trailing \r (else the payload path is wrong on the device).
got="$(manifest_pkgs "$tmp/m" | od -An -c | tr -s ' ')"
case "$got" in
  *'\r'*) echo "FAIL manifest_pkgs leaked a carriage return:$got"; fail=1 ;;
esac
[ "$(manifest_pkgs "$tmp/m" | head -1)" = "com.flycast.emulator" ] \
  || { echo "FAIL manifest_pkgs[1] != com.flycast.emulator"; fail=1; }

# 2) the real failure: the payload path built from $pkg must resolve.
mkdir -p "$tmp/payload/com.flycast.emulator/apk"
: > "$tmp/payload/com.flycast.emulator/apk/base.apk"
pkg="$(manifest_pkgs "$tmp/m" | head -1)"
set -- "$tmp/payload/$pkg/apk/"*.apk
[ -f "$1" ] || { echo "FAIL apk glob missed (CRLF in \$pkg): [$1]"; fail=1; }

# 3) flags must not carry \r ("on\r" != "on" -> restore silently skips @settings).
[ "$(manifest_flag "$tmp/m" settings)" = "on" ]  || { echo "FAIL manifest_flag settings != on"; fail=1; }
[ "$(manifest_flag "$tmp/m" grants)" = "off" ]   || { echo "FAIL manifest_flag grants != off"; fail=1; }

# 4) axes lookup must still match a \r-free pkg name against a CRLF manifest line.
[ "$(manifest_axes "$tmp/m" com.flycast.emulator)" = "apk config" ] || { echo "FAIL axes(flycast)"; fail=1; }
[ "$(manifest_axes "$tmp/m" com.bar)" = "apk" ]                     || { echo "FAIL axes(bar)"; fail=1; }
[ "$(manifest_axes "$tmp/m" com.baz)" = "config" ]                  || { echo "FAIL axes(baz)"; fail=1; }
manifest_wants "$tmp/m" com.flycast.emulator apk || { echo "FAIL wants(flycast,apk)"; fail=1; }

# 5) LF manifests keep working (no regression).
printf '# p\ncom.foo\n@settings on\n' > "$tmp/lf"
[ "$(manifest_pkgs "$tmp/lf")" = "com.foo" ]   || { echo "FAIL LF manifest_pkgs"; fail=1; }
[ "$(manifest_flag "$tmp/lf" settings)" = "on" ] || { echo "FAIL LF manifest_flag"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: manifest CRLF tolerance"; exit 0; } || exit 1
