#!/usr/bin/env bash
# Local smoke test for install_apks (no device). Stubs pm, points CAS_INST_DIR at scratch.
# Run: bash tests/test_install_apks.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
export CAS_INST_DIR="$tmp/_inst"

PM_LOG="$tmp/pm.log"; : > "$PM_LOG"
PM_INSTALL_RC=0; PM_COMMIT_RC=0
pm(){
  echo "$*" >> "$PM_LOG"
  case "$1" in
    install)         return "$PM_INSTALL_RC";;
    install-create)  echo "Success: created install session [77]"; return 0;;
    install-write)   return 0;;
    install-commit)  return "$PM_COMMIT_RC";;
    install-abandon) return 0;;
    *)               return 0;;
  esac
}

# single APK -> pm install, rc 0
single="$tmp/single"; mkdir -p "$single"; : > "$single/base.apk"
install_apks "$single" "com.single" >/dev/null || { echo "FAIL(single rc)"; fail=1; }
grep -q '^install -r -g ' "$PM_LOG" || { echo "FAIL(single: pm install not called)"; fail=1; }

# split APKs -> install session, rc 0
: > "$PM_LOG"
split="$tmp/split"; mkdir -p "$split"; : > "$split/base.apk"; : > "$split/split_a.apk"; : > "$split/split_b.apk"
install_apks "$split" "com.split" >/dev/null || { echo "FAIL(split rc)"; fail=1; }
grep -q '^install-create ' "$PM_LOG" || { echo "FAIL(split: no install-create)"; fail=1; }
[ "$(grep -c '^install-write ' "$PM_LOG")" -eq 3 ] || { echo "FAIL(split: wrong write count)"; fail=1; }
grep -q '^install-commit 77' "$PM_LOG" || { echo "FAIL(split: no commit)"; fail=1; }

# no APK in source -> rc 1
empty="$tmp/empty"; mkdir -p "$empty"
if install_apks "$empty" "com.none" >/dev/null 2>&1; then echo "FAIL(empty: returned success)"; fail=1; fi

# pm install fails -> rc 1
: > "$PM_LOG"; PM_INSTALL_RC=1
if install_apks "$single" "com.single" >/dev/null 2>&1; then echo "FAIL(install-fail: returned success)"; fail=1; fi
PM_INSTALL_RC=0

[ "$fail" -eq 0 ] && { echo "PASS: install_apks"; exit 0; } || exit 1
