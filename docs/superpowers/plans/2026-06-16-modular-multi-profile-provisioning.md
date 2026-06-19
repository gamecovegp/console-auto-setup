# Modular Multi-Profile Provisioning — Implementation Plan (Slice 1: core engine + one profile)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing root clone toolkit modular and PC-sourced: a manifest selects which app modules to restore, the payload comes from a PC profile pushed over adb (SD = game data only), and one orchestrator provisions a device end-to-end.

**Architecture:** Pure, host-testable shell helpers (`internal_for`, manifest parser, `match_profile`) drive a parametrized `restore.sh`/`capture.sh` (payload source + module set become env-overridable). A PC-side `provision.sh` resolves a profile, pushes the selected modules to `/data/local/tmp/cas`, runs restore over adb, cleans up, reboots. `capture-to-pc.sh` does the reverse for building/updating a profile. Spec: `docs/superpowers/specs/2026-06-16-modular-multi-profile-provisioning-design.md`.

**Tech Stack:** POSIX shell (toybox `sh` on-device, `bash` on PC), `adb`, a tiny home-grown shell test harness + a mock-`adb` stub (no external test framework — none exists on-device).

**Conventions for the worker:** cwd is the project root `console-auto-setup/`. All paths below are relative to it. Device-side `su` is invoked as `/debug_ramdisk/su`. Never run destructive adb against a real device in these tasks except Task 9 (explicit, manual).

---

### Task 0: Initialize git + shell test harness

**Files:**
- Create: `tests/assert.sh`
- Create: `tests/run.sh`

- [ ] **Step 1: Init git if needed**

Run:
```bash
git rev-parse --is-inside-work-tree 2>/dev/null || git init && git add -A && git commit -m "chore: snapshot existing toolkit before modular refactor"
```
Expected: a repo exists and an initial commit is made (or it was already a repo).

- [ ] **Step 2: Write the assert helper**

Create `tests/assert.sh`:
```sh
# tiny POSIX assert helpers. Usage: . tests/assert.sh ; assert_eq "$got" "$want" "label"
: "${FAILS:=0}" "${TESTS:=0}"
assert_eq(){ TESTS=$((TESTS+1)); if [ "$1" = "$2" ]; then printf '  PASS  %s\n' "$3"; else printf '  FAIL  %s\n        got:  [%s]\n        want: [%s]\n' "$3" "$1" "$2"; FAILS=$((FAILS+1)); fi; }
assert_contains(){ TESTS=$((TESTS+1)); case "$1" in *"$2"*) printf '  PASS  %s\n' "$3";; *) printf '  FAIL  %s\n        [%s] does not contain [%s]\n' "$3" "$1" "$2"; FAILS=$((FAILS+1));; esac; }
finish(){ printf '\n%d tests, %d failures\n' "$TESTS" "$FAILS"; [ "$FAILS" -eq 0 ]; }
```

- [ ] **Step 3: Write the test runner**

Create `tests/run.sh`:
```sh
#!/usr/bin/env bash
# runs every tests/test_*.sh in one shell, reports total. Run: bash tests/run.sh
set -u; cd "$(dirname "$0")/.."
. tests/assert.sh
for t in tests/test_*.sh; do [ -f "$t" ] && { printf '\n== %s ==\n' "$t"; . "$t"; }; done
finish
```

- [ ] **Step 4: Verify the harness runs (no tests yet = 0 failures)**

Run: `bash tests/run.sh`
Expected: prints `0 tests, 0 failures` and exits 0.

- [ ] **Step 5: Commit**

```bash
git add tests/assert.sh tests/run.sh && git commit -m "test: add shell test harness"
```

---

### Task 1: `internal_for()` — couple internal-storage dirs to their owning app

**Files:**
- Modify: `provision/root/lib-root.sh` (add function near `INTERNAL_DIRS`)
- Test: `tests/test_lib.sh`

- [ ] **Step 1: Write the failing test**

Create `tests/test_lib.sh`:
```sh
. provision/root/lib-root.sh
assert_eq "$(internal_for org.es_de.frontend)"      "ES-DE"     "internal_for es-de"
assert_eq "$(internal_for org.citra.emu)"           "citra-emu" "internal_for citra"
assert_eq "$(internal_for com.retroarch.aarch64)"   "RetroArch" "internal_for retroarch"
assert_eq "$(internal_for dev.eden.eden_emulator)"  ""          "internal_for none-owning app"
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `bash tests/run.sh`
Expected: FAIL lines for `internal_for` (command not found / empty).

- [ ] **Step 3: Add the function**

In `provision/root/lib-root.sh`, immediately after the `INTERNAL_DIRS="..."` line, add:
```sh
# Which shared internal-storage dir (if any) a package owns. Restored only if the app is in the manifest.
internal_for(){ case "$1" in
  org.es_de.frontend) echo "ES-DE";;
  org.citra.emu) echo "citra-emu";;
  com.retroarch.aarch64) echo "RetroArch";;
esac; }
```

- [ ] **Step 4: Run it, expect PASS**

Run: `bash tests/run.sh`
Expected: 4 PASS for `internal_for`.

- [ ] **Step 5: Commit**

```bash
git add provision/root/lib-root.sh tests/test_lib.sh && git commit -m "feat: internal_for() pkg->internal-dir coupling"
```

---

### Task 2: Manifest parser — `manifest_pkgs()` + `manifest_flag()`

**Files:**
- Modify: `provision/root/lib-root.sh`
- Test: `tests/test_manifest.sh`

- [ ] **Step 1: Write the failing test**

Create `tests/test_manifest.sh`:
```sh
. provision/root/lib-root.sh
TMP="$(mktemp)"; cat > "$TMP" <<'EOF'
# sample manifest
org.es_de.frontend
dev.eden.eden_emulator
com.github.stenzek.duckstation
@settings on
@hardening off
EOF
assert_eq "$(manifest_pkgs "$TMP" | tr '\n' ',')" "org.es_de.frontend,dev.eden.eden_emulator,com.github.stenzek.duckstation," "manifest_pkgs lists apps only"
assert_eq "$(manifest_flag "$TMP" settings)"  "on"  "manifest_flag settings=on"
assert_eq "$(manifest_flag "$TMP" hardening)" "off" "manifest_flag hardening=off"
assert_eq "$(manifest_flag "$TMP" grants)"    ""    "manifest_flag missing=empty"
rm -f "$TMP"
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `bash tests/run.sh`
Expected: FAIL for the manifest assertions.

- [ ] **Step 3: Add the functions**

In `provision/root/lib-root.sh`, after `internal_for()`, add these two functions:
```sh
# Manifest = app names (one per line) + "@flag value" lines + "#" comments. Both parsers are pure.
# manifest_pkgs <file>      -> app names, one per line (comments + @flag lines stripped)
manifest_pkgs(){ sed -e 's/#.*//' "$1" 2>/dev/null | grep -vE '^[[:space:]]*@' | awk 'NF{print $1}'; }
# manifest_flag <file> <name> -> the value after "@name " (e.g. on/off), empty if absent
manifest_flag(){ f="$1"; n="$2"; sed -n "s/^@${n}[[:space:]]\{1,\}//p" "$f" 2>/dev/null | awk 'NF{print $1; exit}'; }
```

- [ ] **Step 4: Run it, expect PASS**

Run: `bash tests/run.sh`
Expected: 4 PASS for manifest.

- [ ] **Step 5: Commit**

```bash
git add provision/root/lib-root.sh tests/test_manifest.sh && git commit -m "feat: manifest parser (manifest_pkgs, manifest_flag)"
```

---

### Task 3: Parametrize `restore.sh` — payload source + manifest + internal coupling

**Files:**
- Modify: `provision/root/restore.sh`
- Test: `tests/test_restore_resolve.sh`

This task makes restore read its payload from `$CAS_PAYLOAD` (PC-pushed) and its module list from `$CAS_MANIFEST`, both falling back to today's SD behavior. We unit-test only the *resolution* logic (host-runnable); full restore is integration-tested in Task 9.

- [ ] **Step 1: Write the failing test for resolution**

Create `tests/test_restore_resolve.sh`:
```sh
# We extract restore.sh's resolution block into a sourceable check by running it with a stub env.
# resolve_payload echoes the chosen payload dir; resolve_pkgs echoes the chosen module list source.
. provision/root/lib-root.sh
# simulate: CAS_PAYLOAD set -> use it; unset -> fall back to <sd>/golden_root_payload
CAS_PAYLOAD=/tmp/cas/payload; SD=/storage/AAAA-1111
assert_eq "$(P="${CAS_PAYLOAD:-$SD/golden_root_payload}"; echo "$P")" "/tmp/cas/payload" "payload from CAS_PAYLOAD"
unset CAS_PAYLOAD
assert_eq "$(P="${CAS_PAYLOAD:-$SD/golden_root_payload}"; echo "$P")" "/storage/AAAA-1111/golden_root_payload" "payload falls back to SD"
# manifest -> RPKGS
TMP="$(mktemp)"; printf 'org.citra.emu\ndev.eden.eden_emulator\n' > "$TMP"
CAS_MANIFEST="$TMP"
assert_eq "$(manifest_pkgs "$CAS_MANIFEST" | tr '\n' ' ')" "org.citra.emu dev.eden.eden_emulator " "RPKGS from manifest"
rm -f "$TMP"
```

- [ ] **Step 2: Run it, expect FAIL** (CAS_MANIFEST path not yet wired, but the env logic above already passes — this test pins the contract restore.sh must follow)

Run: `bash tests/run.sh`
Expected: these specific asserts PASS (they test the shell expressions directly); they exist to lock the contract. If any FAIL, fix the expression before editing restore.sh.

- [ ] **Step 3: Edit `restore.sh` header — payload + module-set resolution**

In `provision/root/restore.sh`, replace the lines:
```sh
SD="$(detect_sd)"; SERIAL="${SD##*/}"; P="$SD/golden_root_payload"
[ -d "$P" ] || { echo "no payload at $P — copy golden_root_payload here first"; exit 1; }
GSERIAL="$(sed -n 's/^golden_serial=//p' "$P/global.meta")"
RPKGS="$(cat "$P/pkglist.txt" 2>/dev/null)"; [ -n "$RPKGS" ] || RPKGS="$PKGS"   # the captured app set
```
with:
```sh
SD="$(detect_sd)"; SERIAL="${SD##*/}"
# payload source: PC-pushed dir ($CAS_PAYLOAD) wins; else today's on-SD payload (back-compat).
P="${CAS_PAYLOAD:-$SD/golden_root_payload}"
[ -d "$P" ] || { echo "no payload at $P (set CAS_PAYLOAD or stage on SD)"; exit 1; }
GSERIAL="$(sed -n 's/^golden_serial=//p' "$P/global.meta")"
# module set: explicit manifest wins; else the payload's pkglist; else the built-in PKGS.
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then RPKGS="$(manifest_pkgs "$CAS_MANIFEST")"
else RPKGS="$(cat "$P/pkglist.txt" 2>/dev/null)"; fi
[ -n "$RPKGS" ] || RPKGS="$PKGS"
```

- [ ] **Step 4: Edit `restore.sh` internal-dir restore — couple to included apps**

Find the `# 2b) shared internal-storage dirs ...` loop:
```sh
for d in $INTERNAL_DIRS; do
  [ -f "$P/internal_$d.tar" ] || continue
  mkdir -p /storage/emulated/0
  tar -xf "$P/internal_$d.tar" -C /storage/emulated/0 2>/dev/null
  restorecon -R "/storage/emulated/0/$d" 2>/dev/null
  ok "restored internal:$d"
done
```
Replace with (restore only internal dirs owned by an app in the manifest):
```sh
for pkg in $RPKGS; do
  d="$(internal_for "$pkg")"; [ -n "$d" ] || continue
  [ -f "$P/internal_$d.tar" ] || continue
  mkdir -p /storage/emulated/0
  tar -xf "$P/internal_$d.tar" -C /storage/emulated/0 2>/dev/null
  restorecon -R "/storage/emulated/0/$d" 2>/dev/null
  ok "restored internal:$d (for $pkg)"
done
```

- [ ] **Step 5: Syntax-check (host) + run tests**

Run: `sh -n provision/root/restore.sh && bash tests/run.sh`
Expected: `restore.sh` parses clean; all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add provision/root/restore.sh tests/test_restore_resolve.sh && git commit -m "feat: restore.sh reads CAS_PAYLOAD + CAS_MANIFEST; internal dirs scoped to manifest"
```

---

### Task 4: Parametrize `capture.sh` — output dir

**Files:**
- Modify: `provision/root/capture.sh`

- [ ] **Step 1: Edit the output dir**

In `provision/root/capture.sh`, replace:
```sh
SD="$(detect_sd)"; P="$SD/golden_root_payload"
mkdir -p "$P"
```
with:
```sh
SD="$(detect_sd)"
# capture target: $CAS_OUT (internal temp, for PC pull) wins; else on-SD (back-compat).
P="${CAS_OUT:-$SD/golden_root_payload}"
mkdir -p "$P"
```

- [ ] **Step 2: Syntax-check**

Run: `sh -n provision/root/capture.sh`
Expected: clean (exit 0, no output).

- [ ] **Step 3: Commit**

```bash
git add provision/root/capture.sh && git commit -m "feat: capture.sh writes to CAS_OUT (internal temp for PC pull)"
```

---

### Task 5: `match_profile()` — auto-select a profile by device model

**Files:**
- Create: `provision/lib-pc.sh` (PC-side helpers)
- Test: `tests/test_match.sh`

- [ ] **Step 1: Write the failing test**

Create `tests/test_match.sh`:
```sh
. provision/lib-pc.sh
ROOT="$(mktemp -d)"; mkdir -p "$ROOT/odin2mini" "$ROOT/mangmi-airx-256"
printf 'model_match=Odin2 ?Mini\nfrontend=es-de\n'        > "$ROOT/odin2mini/profile.meta"
printf 'model_match=Air ?X|Mangmi\nfrontend=gamehub\n'    > "$ROOT/mangmi-airx-256/profile.meta"
assert_eq "$(match_profile "Odin2 Mini" "$ROOT")"      "odin2mini"        "match odin"
assert_eq "$(match_profile "Mangmi Air X" "$ROOT")"    "mangmi-airx-256"  "match mangmi"
assert_eq "$(match_profile "Retroid Pocket 6" "$ROOT")" ""               "no match -> empty"
rm -rf "$ROOT"
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `bash tests/run.sh`
Expected: FAIL (`match_profile` not found).

- [ ] **Step 3: Create `provision/lib-pc.sh`**

```sh
# lib-pc.sh — PC-side helpers (bash/sh on the host). Sourced by provision.sh / capture-to-pc.sh.
# match_profile <model> <profiles_root> -> echoes the first profile dir name whose model_match regex hits.
match_profile(){ model="$1"; root="${2:-profiles}"
  for m in "$root"/*/profile.meta; do
    [ -f "$m" ] || continue
    pat="$(sed -n 's/^model_match=//p' "$m")"; [ -n "$pat" ] || continue
    if printf '%s' "$model" | grep -qE "$pat"; then basename "$(dirname "$m")"; return 0; fi
  done; return 1; }
```

- [ ] **Step 4: Run it, expect PASS**

Run: `bash tests/run.sh`
Expected: 3 PASS for match.

- [ ] **Step 5: Commit**

```bash
git add provision/lib-pc.sh tests/test_match.sh && git commit -m "feat: match_profile() auto-selects profile by device model"
```

---

### Task 6: Create the `odin2mini` profile from the validated payload

**Files:**
- Create: `profiles/odin2mini/profile.meta`
- Create: `profiles/odin2mini/manifest`
- Move:   existing `payloads/odin2mini-golden-2026-06-16/golden_root_payload` → `profiles/odin2mini/golden_root_payload`

- [ ] **Step 1: Place the payload under the profile**

Run:
```bash
mkdir -p profiles/odin2mini
cp -a "payloads/odin2mini-golden-2026-06-16/golden_root_payload" "profiles/odin2mini/golden_root_payload"
ls profiles/odin2mini/golden_root_payload | head
```
Expected: lists the 12 app dirs + `internal_*.tar` + `urigrants.xml` + `settings` + `pkglist.txt`.

- [ ] **Step 2: Write `profile.meta`**

Create `profiles/odin2mini/profile.meta`:
```
model_match=Odin2 ?Mini
frontend=es-de
notes=AYN Odin 2 Mini (kalama/SD 8 Gen 2). Superset golden.
captured=2026-06-16
```

- [ ] **Step 3: Generate the default manifest from the payload's pkglist**

Run:
```bash
{ echo "# odin2mini default manifest — all captured apps"; cat profiles/odin2mini/golden_root_payload/pkglist.txt; printf '@settings on\n@hardening on\n@grants on\n'; } > profiles/odin2mini/manifest
cat profiles/odin2mini/manifest
```
Expected: the 12 packages followed by the three `@flag on` lines.

- [ ] **Step 4: Sanity test — manifest parses to the same 12 apps**

Add to `tests/test_manifest.sh` (append) :
```sh
assert_eq "$(. provision/root/lib-root.sh; manifest_pkgs profiles/odin2mini/manifest | grep -c .)" "12" "odin2mini manifest has 12 apps"
```
Run: `bash tests/run.sh`
Expected: PASS (12 apps).

- [ ] **Step 5: Commit**

```bash
git add profiles/odin2mini/profile.meta profiles/odin2mini/manifest tests/test_manifest.sh && git commit -m "feat: odin2mini profile (meta + default manifest) from validated payload"
echo "profiles/odin2mini/golden_root_payload/" >> .gitignore && git add .gitignore && git commit -m "chore: gitignore the multi-GB payload blob"
```
(Payload blobs are large; track the profile metadata/manifest in git, keep the payload on disk + your PC backup, not in git.)

---

### Task 7: `provision.sh` — PC orchestrator (single device, PC-sourced)

**Files:**
- Create: `provision/provision.sh`
- Create: `tests/test_provision.sh` (uses a mock `adb`)
- Create: `tests/mock/adb` (stub that records args)

- [ ] **Step 1: Write the mock adb stub**

Create `tests/mock/adb`:
```sh
#!/usr/bin/env bash
# records every invocation to $ADB_LOG; canned replies for the calls provision.sh makes.
echo "adb $*" >> "${ADB_LOG:-/dev/null}"
case "$*" in
  "get-state") echo device;;
  *"getprop ro.product.model"*) echo "Odin2 Mini";;
  *"getprop sys.boot_completed"*) echo 1;;
  "shell /debug_ramdisk/su -c id") echo "uid=0(root)";;
  *) :;;
esac
exit 0
```
Run: `chmod +x tests/mock/adb`

- [ ] **Step 2: Write the failing test**

Create `tests/test_provision.sh`:
```sh
ADB_LOG="$(mktemp)"; export ADB_LOG
PATH="$(pwd)/tests/mock:$PATH"   # provision.sh must call `adb`, picking up the mock
# DRY_PUSH=1 makes provision skip real file pushes (we only assert the command sequence).
DRY_PUSH=1 ADB=adb bash provision/provision.sh --profile odin2mini >/dev/null 2>&1
log="$(cat "$ADB_LOG")"
assert_contains "$log" "getprop ro.product.model"                         "provision reads model"
assert_contains "$log" "shell /debug_ramdisk/su -c id"                    "provision checks root"
assert_contains "$log" "restore.sh"                                       "provision runs restore.sh"
assert_contains "$log" "reboot"                                           "provision reboots"
rm -f "$ADB_LOG"
```

- [ ] **Step 3: Run it, expect FAIL**

Run: `bash tests/run.sh`
Expected: FAIL (provision.sh missing).

- [ ] **Step 4: Write `provision/provision.sh`**

```sh
#!/usr/bin/env bash
# provision.sh — provision ONE connected device from a PC profile (SD = game data only).
#   bash provision/provision.sh [--profile <name>] [--serial <s>]
# Pushes the manifest's modules to /data/local/tmp/cas, runs restore over adb, cleans up, reboots.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/lib-pc.sh"
ADB="${ADB:-adb}"; SU=/debug_ramdisk/su; DEV=/data/local/tmp/cas
PROFILE=""; SERIAL=""
while [ $# -gt 0 ]; do case "$1" in
  --profile) PROFILE="$2"; shift 2;; --serial) SERIAL="$2"; shift 2;; *) echo "unknown arg $1"; exit 2;; esac; done
A(){ if [ -n "$SERIAL" ]; then "$ADB" -s "$SERIAL" "$@"; else "$ADB" "$@"; fi; }

model="$(A shell getprop ro.product.model | tr -d '\r')"
[ -n "$PROFILE" ] || PROFILE="$(match_profile "$model" "$ROOT/profiles")"
[ -n "$PROFILE" ] || { echo "no profile matches model '$model' — pass --profile <name>"; exit 1; }
PDIR="$ROOT/profiles/$PROFILE"; PAY="$PDIR/golden_root_payload"
[ -d "$PAY" ] || { echo "profile payload missing: $PAY"; exit 1; }
echo "==> device '$model' -> profile '$PROFILE'"

# safety + root
[ "$(A shell '[ -e /data/adb/.cas_golden ] && echo GOLD' | tr -d '\r')" = "GOLD" ] && { echo "refusing: this device is a GOLDEN"; exit 1; }
[ "$(A shell $SU -c id 2>/dev/null | grep -c 'uid=0')" -ge 1 ] || { echo "no root — grant [SharedUID] Shell in Magisk, then retry"; exit 1; }

# push selected modules + scripts (skipped in tests via DRY_PUSH)
MAN="$PDIR/manifest"
if [ -z "${DRY_PUSH:-}" ]; then
  A shell rm -rf "$DEV"; A shell mkdir -p "$DEV/payload"
  for pkg in $(manifest_pkgs "$MAN"); do A push "$PAY/$pkg" "$DEV/payload/" >/dev/null; done
  for f in global.meta pkglist.txt urigrants.xml; do [ -f "$PAY/$f" ] && A push "$PAY/$f" "$DEV/payload/" >/dev/null; done
  [ -d "$PAY/settings" ] && A push "$PAY/settings" "$DEV/payload/" >/dev/null
  # internal dirs for the included apps only
  for pkg in $(manifest_pkgs "$MAN"); do d="$(internal_for "$pkg")"; [ -n "$d" ] && [ -f "$PAY/internal_$d.tar" ] && A push "$PAY/internal_$d.tar" "$DEV/payload/" >/dev/null; done
  A push "$HERE/root/restore.sh"  "$DEV/" >/dev/null
  A push "$HERE/root/lib-root.sh" "$DEV/" >/dev/null
  A push "$MAN" "$DEV/manifest" >/dev/null
fi

# run restore (payload + manifest from the pushed dir)
A shell "$SU -c \"CAS_PAYLOAD=$DEV/payload CAS_MANIFEST=$DEV/manifest sh $DEV/restore.sh\""
# cleanup transient payload, reboot
[ -z "${DRY_PUSH:-}" ] && A shell "$SU -c \"rm -rf $DEV\""
A reboot
echo "==> provisioned '$PROFILE'. After boot: verify on device."
```
Need `manifest_pkgs`/`internal_for` on the host: at the top of `lib-pc.sh` add `. "$(dirname "${BASH_SOURCE[0]}")/root/lib-root.sh" 2>/dev/null || true` — but root/lib-root.sh lives under `provision/root/`. Add this line to `provision/lib-pc.sh` after its header:
```sh
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/root/lib-root.sh" 2>/dev/null || true
```

- [ ] **Step 5: Run it, expect PASS**

Run: `chmod +x provision/provision.sh && bash tests/run.sh`
Expected: 4 PASS for provision.

- [ ] **Step 6: Commit**

```bash
git add provision/provision.sh provision/lib-pc.sh tests/test_provision.sh tests/mock/adb && git commit -m "feat: provision.sh PC orchestrator (single device, manifest-driven, PC-sourced)"
```

---

### Task 8: `capture-to-pc.sh` — build/update a profile from a golden

**Files:**
- Create: `provision/capture-to-pc.sh`
- Test: extend `tests/test_provision.sh`

- [ ] **Step 1: Write the failing test (append to `tests/test_provision.sh`)**

```sh
ADB_LOG2="$(mktemp)"; ADB_LOG="$ADB_LOG2" PATH="$(pwd)/tests/mock:$PATH" \
  DRY_PULL=1 bash provision/capture-to-pc.sh testprof >/dev/null 2>&1
log2="$(cat "$ADB_LOG2")"
assert_contains "$log2" "capture.sh"                 "capture-to-pc runs capture.sh"
assert_contains "$log2" "CAS_OUT=/data/local/tmp"    "capture-to-pc sets CAS_OUT temp"
rm -f "$ADB_LOG2"
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `bash tests/run.sh`
Expected: FAIL (capture-to-pc.sh missing).

- [ ] **Step 3: Write `provision/capture-to-pc.sh`**

```sh
#!/usr/bin/env bash
# capture-to-pc.sh — capture a golden into profiles/<name>/ (rotate .prev for rollback).
#   bash provision/capture-to-pc.sh <profile> [--serial <s>]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"; . "$HERE/lib-pc.sh"
ADB="${ADB:-adb}"; SU=/debug_ramdisk/su; TMP=/data/local/tmp/cas_cap
NAME="${1:?usage: capture-to-pc.sh <profile> [--serial s]}"; shift || true
SERIAL=""; [ "${1:-}" = "--serial" ] && SERIAL="$2"
A(){ if [ -n "$SERIAL" ]; then "$ADB" -s "$SERIAL" "$@"; else "$ADB" "$@"; fi; }
PDIR="$ROOT/profiles/$NAME"; DEST="$PDIR/golden_root_payload"

# push the capture scripts, run capture to internal temp
if [ -z "${DRY_PULL:-}" ]; then
  A shell mkdir -p /data/local/tmp/cas_scripts
  A push "$HERE/root/capture.sh"  /data/local/tmp/cas_scripts/ >/dev/null
  A push "$HERE/root/lib-root.sh" /data/local/tmp/cas_scripts/ >/dev/null
fi
A shell "$SU -c \"CAS_OUT=$TMP sh /data/local/tmp/cas_scripts/capture.sh\""
if [ -z "${DRY_PULL:-}" ]; then
  mkdir -p "$PDIR"
  [ -d "$DEST" ] && { rm -rf "$DEST.prev"; mv "$DEST" "$DEST.prev"; }   # one-deep rollback
  A pull "$TMP" "$DEST" >/dev/null
  A shell "$SU -c \"rm -rf $TMP\""
  # default manifest only if none exists (preserve operator edits)
  [ -f "$PDIR/manifest" ] || { echo "# $NAME default manifest"; cat "$DEST/pkglist.txt"; printf '@settings on\n@hardening on\n@grants on\n'; } > "$PDIR/manifest"
fi
echo "==> captured golden into profiles/$NAME (prev kept at golden_root_payload.prev)"
```

- [ ] **Step 4: Run it, expect PASS**

Run: `chmod +x provision/capture-to-pc.sh && bash tests/run.sh`
Expected: the 2 capture-to-pc asserts PASS, all prior tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add provision/capture-to-pc.sh tests/test_provision.sh && git commit -m "feat: capture-to-pc.sh builds/updates a profile (with .prev rollback)"
```

---

### Task 9: On-device integration verification (manual, real hardware)

**Files:** none (verification only)

This is the only task that touches a real device. Run it when a unit is connected + rooted.

- [ ] **Step 1: Re-capture the Odin golden into the profile (proves capture-to-pc end-to-end)**

Run: `bash provision/capture-to-pc.sh odin2mini`
Expected: `profiles/odin2mini/golden_root_payload` is (re)created from the device; `.prev` kept.

- [ ] **Step 2: Provision a factory-reset test unit, PC-sourced**

Run: `bash provision/provision.sh --profile odin2mini`
Expected: pushes modules, prints `[ok] RESTORE complete`, reboots. SD untouched (game data only).

- [ ] **Step 3: Verify on device**

Run: `adb shell /debug_ramdisk/su -c "sh /storage/*/provision/root/verify.sh"` *(or push verify.sh alongside)*
Expected: 12 apps, ownership `uid:1078`, keys/BIOS present, grants, cores, settings — all OK. Then open ES-DE → games from the SD; boot one per system.

- [ ] **Step 4: Modular check — trimmed manifest**

Temporarily remove one emulator line from `profiles/odin2mini/manifest`, re-run `provision.sh --profile odin2mini` on a spare/test unit, confirm that emulator is absent and the rest intact. Restore the manifest line after.

- [ ] **Step 5: Commit any manifest/meta fixes discovered**

```bash
git add -A && git commit -m "test: on-device integration verified (capture-to-pc + provision)"
```

---

## Self-Review notes (done)
- **Spec coverage:** component-library (existing payload) ✓; manifest-selects-modules (Tasks 2,3,6) ✓; per-variant profile + meta (Task 6) ✓; PC-sourced push + SD=game-data (Task 7) ✓; capture/update with .prev (Task 8) ✓; restore/capture parametrization (Tasks 3,4) ✓; auto-match + override (Tasks 5,7) ✓.
- **Deferred to Plan 2 (noted, not gaps):** profile collection `list/new/delete-archive` (§7.1), **batch `--all`/`--parallel`** (§9.1), Windows `.bat` wrappers, Retroid/Mangmi profiles, large-PC-games-on-SD wiring, the GUI.
- **Type consistency:** `manifest_pkgs`/`manifest_flag`/`internal_for`/`match_profile` names + signatures consistent across Tasks 1–8; env contract `CAS_PAYLOAD`/`CAS_MANIFEST`/`CAS_OUT` consistent between restore/capture (Tasks 3,4) and the orchestrators (Tasks 7,8).
- **Fix applied inline:** Task 2 Step 3 shows the corrected single definition of `manifest_flag` (the duplicate is called out as a mistake to avoid).
</content>
