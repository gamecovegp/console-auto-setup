#!/usr/bin/env bash
# Local tests (no device) for the WiFi provisioning helpers: store-path resolution, capture (plaintext vs
# encrypted vs none), restore copy, and the Lock-time strip. Every path is sandboxed via WIFI_ROOT.
# Run: bash tests/test_wifi.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
chk(){ if [ "$1" = "$2" ]; then :; else echo "FAIL: $3 (got '$1' want '$2')"; fail=1; fi; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
APEX="$tmp/data/misc/apexdata/com.android.wifi"
MISC="$tmp/data/misc/wifi"
PLAIN='<WifiConfigStore><Network><string name="SSID">&quot;Shop&quot;</string><string name="PreSharedKey">&quot;secret&quot;</string></Network></WifiConfigStore>'
ENC='<WifiConfigStore><Network><string name="SSID">&quot;Shop&quot;</string><byte-array name="EncryptedData" num="8">aabbccdd</byte-array></Network></WifiConfigStore>'

# --- wifi_store_path resolution ---
mkdir -p "$APEX"; printf '%s' "$PLAIN" > "$APEX/WifiConfigStore.xml"
chk "$(WIFI_ROOT="$tmp" wifi_store_path)" "$APEX/WifiConfigStore.xml" "store_path prefers apexdata"
rm -f "$APEX/WifiConfigStore.xml"; mkdir -p "$MISC"; printf '%s' "$PLAIN" > "$MISC/WifiConfigStore.xml"
chk "$(WIFI_ROOT="$tmp" wifi_store_path)" "$MISC/WifiConfigStore.xml" "store_path falls back to /data/misc/wifi"
rm -f "$MISC/WifiConfigStore.xml"
chk "$(WIFI_ROOT="$tmp" wifi_store_path)" "$APEX/WifiConfigStore.xml" "store_path default target when none exist"

# --- capture_wifi ---
out="$tmp/out"; mkdir -p "$APEX"; printf '%s' "$PLAIN" > "$APEX/WifiConfigStore.xml"
WIFI_ROOT="$tmp" capture_wifi "$out" >/dev/null 2>&1; chk "$?" "0" "capture plaintext store -> ok"
[ -f "$out/wifi/WifiConfigStore.xml" ] || { echo "FAIL: capture did not stage the store"; fail=1; }
printf '%s' "$ENC" > "$APEX/WifiConfigStore.xml"; rm -rf "$out"
WIFI_ROOT="$tmp" capture_wifi "$out" >/dev/null 2>&1; chk "$?" "1" "capture refuses encrypted PSK"
printf '<WifiConfigStore></WifiConfigStore>' > "$APEX/WifiConfigStore.xml"; rm -rf "$out"
WIFI_ROOT="$tmp" capture_wifi "$out" >/dev/null 2>&1; chk "$?" "1" "capture skips when no saved network"

# --- restore_wifi ---
pd="$tmp/payload"; mkdir -p "$pd/wifi"; printf '%s' "$PLAIN" > "$pd/wifi/WifiConfigStore.xml"
mkdir -p "$APEX"; rm -f "$APEX/WifiConfigStore.xml"
WIFI_ROOT="$tmp" restore_wifi "$pd" >/dev/null 2>&1; chk "$?" "0" "restore clones store when target dir exists"
[ -f "$APEX/WifiConfigStore.xml" ] || { echo "FAIL: restore did not place the store"; fail=1; }
rm -rf "$pd/wifi"
WIFI_ROOT="$tmp" restore_wifi "$pd" >/dev/null 2>&1; chk "$?" "1" "restore skips when payload has no wifi"

# --- strip_wifi (cmd absent on host -> file-absence verify) ---
mkdir -p "$APEX" "$MISC"; printf '%s' "$PLAIN" > "$APEX/WifiConfigStore.xml"; printf '%s' "$PLAIN" > "$MISC/WifiConfigStore.xml"
WIFI_ROOT="$tmp" strip_wifi >/dev/null 2>&1; chk "$?" "0" "strip removes stores + verifies clean"
{ [ ! -f "$APEX/WifiConfigStore.xml" ] && [ ! -f "$MISC/WifiConfigStore.xml" ]; } || { echo "FAIL: strip left a store behind"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: wifi helpers"; exit 0; } || exit 1
