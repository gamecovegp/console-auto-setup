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

# ORDER MATTERS: setting home must happen BEFORE the "no launcher_pkg -> skip" gate chain, so HOME gets
# switched to the golden's choice regardless of whether the layout-owner gate below skips the layout.
# NOTE: there is no more "launcher != golden's -> SKIP" arm (that equality gate was the bug); the
# surviving chain starts at "payload has no launcher_pkg — skip".
set_line="$(grep -n 'set_home_component' "$ROOT/provision/root/restore.sh" | head -1 | cut -d: -f1)"
gate_line="$(grep -n 'payload has no launcher_pkg' "$ROOT/provision/root/restore.sh" | head -1 | cut -d: -f1)"
if [ -n "$set_line" ] && [ -n "$gate_line" ]; then
  [ "$set_line" -lt "$gate_line" ] \
    || { echo "FAIL(7): set_home_component (line $set_line) must come BEFORE the layout gate (line $gate_line)"; fail=1; }
else
  echo "FAIL(7): could not locate set_home_component and/or the layout gate in restore.sh"; fail=1
fi
# THE ACTUAL FIX: the launcher-equality skip arm must be GONE, with NO replacement equality check.
grep -q 'would not apply, SKIP' "$ROOT/provision/root/restore.sh" \
  && { echo "FAIL(7b): the CUR != LP equality skip arm is still present in restore.sh"; fail=1; }

# === restore.sh: the layout goes to the LAYOUT OWNER, whoever happens to be HOME ==================
# Replicates restore.sh's homescreen gate. The old chain skipped the layout unless the unit's CURRENT
# home app equalled the golden's launcher_pkg. Once those are different roles that test is wrong by
# construction: on a Thor the unit's HOME becomes Mjolnir while the layout owner is launcher3, so the
# layout AND the wallpaper (restored inside the same block) were skipped every time.
INSTALLED="com.android.launcher3 xyz.blacksheep.mjolnir"
pm(){ case "$1" in path) case " $INSTALLED " in *" $2 "*) return 0;; *) return 1;; esac;; *) return 0;; esac; }

# decide(): the gate as it must now behave. $1=launcher_pkg $2=launcher_component $3=this unit's HOME
decide(){ _lp="$1"; _lc="$2"; _cur="$3"; _lhome="${_lc%%/*}"
  _sethome=no
  [ -n "$_lc" ] && [ -n "$_lhome" ] && [ -n "$_cur" ] && [ "$_cur" != "$_lhome" ] && _sethome=yes
  if [ -z "$_lp" ]; then echo "$_sethome/skip-no-pkg"; return; fi
  if ! pm path "$_lp" >/dev/null 2>&1; then echo "$_sethome/skip-absent"; return; fi
  echo "$_sethome/restore-$_lp"; }

# THE THOR CASE: golden's layout owner is launcher3, its HOME is Mjolnir, the fresh unit boots launcher3.
# HOME must be switched to Mjolnir AND the layout must still land in launcher3.
[ "$(decide com.android.launcher3 xyz.blacksheep.mjolnir/.HomeActivity com.android.launcher3)" \
  = "yes/restore-com.android.launcher3" ] || { echo "FAIL(thor): $(decide com.android.launcher3 xyz.blacksheep.mjolnir/.HomeActivity com.android.launcher3)"; fail=1; }

# …and once HOME is already Mjolnir, the layout STILL restores (the old CUR != LP test broke exactly here)
[ "$(decide com.android.launcher3 xyz.blacksheep.mjolnir/.HomeActivity xyz.blacksheep.mjolnir)" \
  = "no/restore-com.android.launcher3" ] || { echo "FAIL(thor 2nd run): $(decide com.android.launcher3 xyz.blacksheep.mjolnir/.HomeActivity xyz.blacksheep.mjolnir)"; fail=1; }

# BACK-COMPAT (the four working goldens): HOME and layout owner are the same package
[ "$(decide com.android.launcher3 com.android.launcher3/.uioverrides.QuickstepLauncher com.android.launcher3)" \
  = "no/restore-com.android.launcher3" ] || { echo "FAIL(rp6 back-compat)"; fail=1; }

# a golden with no launcher_component at all (captured before that key existed) still restores its layout
[ "$(decide com.android.launcher3 '' com.android.launcher3)" = "no/restore-com.android.launcher3" ] \
  || { echo "FAIL(legacy no-component golden)"; fail=1; }

# layout owner absent from this unit -> skip (nothing to extract into)
INSTALLED="xyz.blacksheep.mjolnir"
[ "$(decide com.android.launcher3 xyz.blacksheep.mjolnir/.HomeActivity xyz.blacksheep.mjolnir)" \
  = "no/skip-absent" ] || { echo "FAIL(absent layout owner)"; fail=1; }
INSTALLED="com.android.launcher3 xyz.blacksheep.mjolnir"

# payload with no launcher_pkg -> skip
[ "$(decide '' xyz.blacksheep.mjolnir/.HomeActivity com.android.launcher3)" = "yes/skip-no-pkg" ] \
  || { echo "FAIL(no launcher_pkg)"; fail=1; }

[ "$fail" = 0 ] && echo "ok: home_component + set_home_component (guarded) + capture/restore wiring" || exit 1
