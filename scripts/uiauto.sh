#!/usr/bin/env bash
# uiauto.sh — reliable uiautomator-driven UI automation.
# Finds controls by TEXT/content-desc and taps their EXACT input-space bounds
# (works regardless of screen rotation; no pixel guessing). Built for provisioning
# the Class-C SAF emulators hands-off: open app -> Add games -> Use this folder -> Allow.
#
# Usage:
#   ./uiauto.sh list                 dump + print every "(cx,cy)  \"label\"" on screen
#   ./uiauto.sh tap "<regex>"        find first control whose text/desc matches regex, tap its center
#   ./uiauto.sh has "<regex>"        exit 0 if a matching control exists, else 1
#   ./uiauto.sh fg                   print foreground activity
# Env: ADB (default "adb"), SERIAL (optional -s serial)
set -u
ADB="${ADB:-adb}"; [ -n "${SERIAL:-}" ] && ADB="$ADB -s $SERIAL"
_dump(){ $ADB shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1; $ADB shell cat /sdcard/ui.xml 2>/dev/null | tr -d '\r' > /tmp/uiauto.xml; }
case "${1:-}" in
  fg) $ADB shell 'dumpsys activity activities | grep -m1 topResumedActivity' 2>/dev/null | tr -d '\r' | sed -E 's/.*u0 //; s/\} .*//' ;;
  list) _dump; python3 - <<'PY'
import re
x=open('/tmp/uiauto.xml').read(); seen=set()
for m in re.finditer(r'<node[^>]*?(?:text|content-desc)="([^"]+)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',x):
    t,a,b,c,d=m.group(1),*map(int,m.groups()[1:]); k=(t,a,b)
    if t.strip() and k not in seen: seen.add(k); print(f'({(a+c)//2},{(b+d)//2})  "{t}"')
PY
  ;;
  has) _dump; python3 - "$2" <<'PY'
import re,sys
ok=bool(re.search(r'(?:text|content-desc)="[^"]*'+sys.argv[1]+r'[^"]*"',open('/tmp/uiauto.xml').read(),re.I))
sys.exit(0 if ok else 1)
PY
  ;;
  tap) _dump; XY=$(python3 - "$2" <<'PY'
import re,sys
pat=sys.argv[1]; x=open('/tmp/uiauto.xml').read()
for m in re.finditer(r'<node[^>]*?(?:text|content-desc)="([^"]+)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',x):
    t,a,b,c,d=m.group(1),*map(int,m.groups()[1:])
    if re.search(pat,t,re.I): print(f'{(a+c)//2} {(b+d)//2}|{t}'); break
PY
)
     if [ -n "$XY" ]; then $ADB shell input tap ${XY%%|*}; echo "tapped (${XY%%|*}) = \"${XY#*|}\""; else echo "NOT FOUND: /$2/"; exit 1; fi ;;
  # waittap: poll UI as fast as dumps allow until the control appears, then tap immediately.
  # No fixed sleeps -> minimum delay. $3 = max tries (default 30, each ~dump time).
  waittap) tgt="$2"; max="${3:-30}"; i=0
     while [ "$i" -lt "$max" ]; do
       _dump; XY=$(python3 - "$tgt" <<'PY'
import re,sys
pat=sys.argv[1]; x=open('/tmp/uiauto.xml').read()
for m in re.finditer(r'<node[^>]*?(?:text|content-desc)="([^"]+)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',x):
    t,a,b,c,d=m.group(1),*map(int,m.groups()[1:])
    if re.search(pat,t,re.I): print(f'{(a+c)//2} {(b+d)//2}|{t}'); break
PY
)
       if [ -n "$XY" ]; then $ADB shell input tap ${XY%%|*}; echo "tapped (${XY%%|*}) = \"${XY#*|}\" (try $((i+1)))"; exit 0; fi
       i=$((i+1))
     done
     echo "TIMEOUT waiting for /$tgt/ after $max tries"; exit 1 ;;
  *) echo "usage: $0 {list|tap <regex>|has <regex>|fg}"; exit 2 ;;
esac
