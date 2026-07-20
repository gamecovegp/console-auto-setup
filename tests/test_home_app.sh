#!/usr/bin/env bash
# Local smoke test for home_component / set_home_component (no device). Stubs cmd/pm.
# Run: bash tests/test_home_app.sh
#
# WHY these exist: capture.sh already records launcher_pkg, but restore.sh only ever used it as a GATE
# ("unit's launcher != golden's -> SKIP") and never SET the home app. Since the wallpaper restore lives
# inside that same @homescreen block, a golden whose launcher differs from the unit's stock launcher
# silently lost BOTH the layout and the wallpaper. Setting the home activity first makes the gate pass.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

CMD_LOG="$tmp/cmdlog"; : > "$CMD_LOG"
RESOLVE_OUT=""                                  # what `cmd package resolve-activity --brief` prints
INSTALLED=""                                    # space-separated pkgs pm should report as installed

cmd(){
  echo "$*" >> "$CMD_LOG"
  case "$*" in
    *resolve-activity*) [ -n "$RESOLVE_OUT" ] && echo "$RESOLVE_OUT" ;;
    *set-home-activity*) echo "Success" ;;
  esac
  return 0
}
APK_DIR="/data/app"                             # where the stubbed `pm path` claims the APK lives
pm(){
  case "$1" in
    path) case " $INSTALLED " in
            *" $2 "*) echo "package:$APK_DIR/~~ab==/$2-cd==/base.apk"; return 0 ;;
            *) return 1 ;;
          esac ;;
    *) return 0 ;;
  esac
}

# === home_component: full pkg/cls, not just the package =========================================
# set-home-activity needs a COMPONENT; launcher_pkg alone can't drive it.
# NOTE the SHORT class form (pkg/.Cls) -- that is what `resolve-activity --brief` really prints, verified
# on an AYN Thor 2026-07-20, and `set-home-activity` accepts it verbatim. Modelling the full class name
# here would be an unfaithful stub (the same trap that hid the magisk "policy=policy=2" bug).
RESOLVE_OUT="xyz.blacksheep.mjolnir/.HomeActivity"
got="$(home_component)"
[ "$got" = "xyz.blacksheep.mjolnir/.HomeActivity" ] \
  || { echo "FAIL(1 home_component): [$got]"; fail=1; }

# home_launcher must still return only the package (unchanged contract).
got="$(home_launcher)"
[ "$got" = "xyz.blacksheep.mjolnir" ] || { echo "FAIL(1b home_launcher pkg): [$got]"; fail=1; }

# === set_home_component: applies when the target package IS installed ===========================
: > "$CMD_LOG"; INSTALLED="xyz.blacksheep.mjolnir"
set_home_component "xyz.blacksheep.mjolnir/.HomeActivity" || { echo "FAIL(2 rc)"; fail=1; }
grep -q "package set-home-activity xyz.blacksheep.mjolnir/.HomeActivity" "$CMD_LOG" \
  || { echo "FAIL(2 set-home-activity not issued): $(cat "$CMD_LOG")"; fail=1; }

# === THE CRITICAL GUARD: refuse when the package is NOT installed ===============================
# A unit with no valid HOME app is unusable, so this must never fire blind.
: > "$CMD_LOG"; INSTALLED="com.android.launcher3"
if set_home_component "xyz.blacksheep.mjolnir/.HomeActivity"; then
  echo "FAIL(3): set_home_component succeeded for a package that is not installed"; fail=1
fi
grep -q "set-home-activity" "$CMD_LOG" \
  && { echo "FAIL(3 issued the call anyway): $(cat "$CMD_LOG")"; fail=1; }

# === refuse empty / malformed components ========================================================
: > "$CMD_LOG"; INSTALLED="xyz.blacksheep.mjolnir"
if set_home_component ""; then echo "FAIL(4 empty accepted)"; fail=1; fi
# no slash => not a component
if set_home_component "xyz.blacksheep.mjolnir"; then echo "FAIL(4b bare pkg accepted)"; fail=1; fi
grep -q "set-home-activity" "$CMD_LOG" \
  && { echo "FAIL(4c issued a call for a malformed component): $(cat "$CMD_LOG")"; fail=1; }

# === is_user_app: /data/app => user-installed, anything else => system firmware =================
INSTALLED="xyz.blacksheep.mjolnir"
APK_DIR="/data/app"
is_user_app "xyz.blacksheep.mjolnir" || { echo "FAIL(8): /data/app package must read as user-installed"; fail=1; }
APK_DIR="/system/priv-app"
if is_user_app "xyz.blacksheep.mjolnir"; then echo "FAIL(8b): /system package must NOT read as user-installed"; fail=1; fi
APK_DIR="/product/app"
if is_user_app "xyz.blacksheep.mjolnir"; then echo "FAIL(8c): /product package must NOT read as user-installed"; fail=1; fi
APK_DIR="/data/app"
if is_user_app "com.not.installed"; then echo "FAIL(8d): an absent package must NOT read as user-installed"; fail=1; fi

# === capture.sh must NOT filter a USER-INSTALLED launcher out of pkglist.txt ====================
# capture.sh built pkglist.txt as `manifest_pkgs | grep -vxF "$(home_launcher)"`, dropping the HOME
# launcher "even when the manifest lists it". Right for com.android.launcher3 (firmware); wrong for the
# AYN Thor's xyz.blacksheep.mjolnir -- it was ticked in the Save modal, written to the capture-manifest,
# and then silently stripped, so golden_root_payload/xyz.blacksheep.mjolnir/ was never created.
grep -q 'is_user_app' "$ROOT/provision/root/capture.sh" \
  || { echo "FAIL(9): capture.sh still filters the HOME launcher unconditionally"; fail=1; }

# === wiring: capture records the component, restore applies it BEFORE the gate ==================
# Folded into @homescreen (no separate flag) -- it's the same concern: the launcher and the layout it
# owns travel together, and the wallpaper restore already lives in that block.
grep -q 'launcher_component=' "$ROOT/provision/root/capture.sh" \
  || { echo "FAIL(5): capture.sh does not record launcher_component in homescreen/meta"; fail=1; }
grep -q 'set_home_component' "$ROOT/provision/root/restore.sh" \
  || { echo "FAIL(6): restore.sh never sets the default home app"; fail=1; }

# ORDER MATTERS: setting home must happen BEFORE the "launcher != golden's -> SKIP" gate, otherwise the
# gate still skips the block (and with it the wallpaper) before the launcher is ever switched.
set_line="$(grep -n 'set_home_component' "$ROOT/provision/root/restore.sh" | head -1 | cut -d: -f1)"
gate_line="$(grep -n 'would not apply, SKIP' "$ROOT/provision/root/restore.sh" | head -1 | cut -d: -f1)"
if [ -n "$set_line" ] && [ -n "$gate_line" ]; then
  [ "$set_line" -lt "$gate_line" ] \
    || { echo "FAIL(7): set_home_component (line $set_line) must come BEFORE the skip gate (line $gate_line)"; fail=1; }
else
  echo "FAIL(7): could not locate set_home_component and/or the skip gate in restore.sh"; fail=1
fi

[ "$fail" = 0 ] && echo "ok: home_component + set_home_component (guarded) + capture/restore wiring" || exit 1
