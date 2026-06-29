# Golden-driven package set for diag/verify scripts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CAS's diag/verify/audit scripts read the golden's captured package set instead of a static list, so they follow the golden when an app's build is swapped (e.g. NetherSX2 `.tturnip` → `.android`).

**Architecture:** Add one helper, `payload_pkgs`, to `provision/root/lib-root.sh` that returns the golden's captured `pkglist.txt` (newline-separated), falling back to the static `$PKGS` only when no payload is on hand. Repoint the diag/verify scripts at it (or at the live `user_pkgs` set where they run on the golden). The capture/restore clone path is already golden-driven and is left semantically unchanged.

**Tech Stack:** POSIX shell (`/system/bin/sh` on-device; bash-runnable on the dev host for the helper test). Python `pytest` for the existing suite regression check.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-29-golden-driven-package-set-design.md`.
- All touched scripts are read-only diagnostics EXCEPT `restore.sh`, whose change MUST be behavior-preserving.
- `PKGS` value in `lib-root.sh` is **unchanged** (only its comment is demoted to "fallback"). Do not delete or reorder its packages.
- `payload_pkgs` must be pure file IO — no `adb`, no root, no device calls at parse/source time — so it stays locally testable.
- Device-side script bodies that cannot run on the dev host carry a `[VERIFY on device]` note per the repo's existing convention; do not fabricate device test runs.
- Branch: work on the current branch (`feat/companion-device-owner-lockdown`); plain `git commit` per task.

---

### Task 1: `payload_pkgs` helper in `lib-root.sh` (TDD)

**Files:**
- Modify: `provision/root/lib-root.sh` (comment at line 2; add helper after the `PKGS` block, ~line 5)
- Create: `tests/test_payload_pkgs.sh`

**Interfaces:**
- Produces: `payload_pkgs [payload_dir]` — shell function. Echoes one package per line. Reads `<payload_dir>/pkglist.txt` when that file exists and is non-empty; else echoes `$PKGS` (newline-separated). `payload_dir` defaults to `${CAS_OUT:-$(detect_sd)/golden_root_payload}`. Consumed by Tasks 2, 3, 4.

- [ ] **Step 1: Write the failing test**

Create `tests/test_payload_pkgs.sh`:

```bash
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test_payload_pkgs.sh`
Expected: FAIL — `payload_pkgs: command not found` (or non-zero exit), because the helper does not exist yet.

- [ ] **Step 3: Demote the `PKGS` comment and add the helper**

In `provision/root/lib-root.sh`, replace this comment line (line 2):

```sh
# The 11 emulator/frontend packages whose state we clone (settings, key binds, cores, grants, BIOS, keys):
```

with:

```sh
# FALLBACK default only — the golden's captured pkglist.txt is authoritative (see payload_pkgs below); this
# static list is used solely when no payload is on hand. The emulator/frontend packages whose state we clone:
```

Then, immediately AFTER the `PKGS="…"` assignment block (the line ending in `org.es_de.frontend gamehub.lite"`), insert:

```sh
# payload_pkgs [payload_dir] — the authoritative cloned package set: the golden's captured pkglist.txt
# (one pkg per line) when present and non-empty, else the static $PKGS fallback. Pure file IO (no adb/root),
# so it is locally testable. payload_dir defaults to the capture/restore payload location.
payload_pkgs(){
  pdir="${1:-${CAS_OUT:-$(detect_sd)/golden_root_payload}}"
  if [ -s "$pdir/pkglist.txt" ]; then
    cat "$pdir/pkglist.txt"
  else
    printf '%s\n' $PKGS
  fi
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test_payload_pkgs.sh`
Expected: `PASS: payload_pkgs` and exit 0.

- [ ] **Step 5: Commit**

```bash
git add provision/root/lib-root.sh tests/test_payload_pkgs.sh
git commit -m "feat(root): payload_pkgs helper — golden's pkglist is authoritative, PKGS is fallback"
```

---

### Task 2: Repoint the diag scripts (`serial_audit.sh`, `dryrun.sh`, `bios_audit.sh`)

**Files:**
- Modify: `provision/root/diag/serial_audit.sh:6`
- Modify: `provision/root/diag/dryrun.sh:16`
- Modify: `provision/root/diag/bios_audit.sh` (§3 loop, ~line 19)

**Interfaces:**
- Consumes: `payload_pkgs` and the existing `user_pkgs` from `lib-root.sh`.

- [ ] **Step 1: `serial_audit.sh` → live golden set**

`serial_audit.sh` runs ON the golden against live `/data/data`, so use the live installed set. Replace:

```sh
for pkg in $PKGS; do
```

with:

```sh
for pkg in $(user_pkgs); do
```

(The `[ -d "/data/data/$pkg" ] || continue` guard on the next line stays.)

- [ ] **Step 2: `dryrun.sh` §1 → captured set**

In `dryrun.sh`, the payload-integrity loop (the one whose body references `"$P/$pkg/data.tar"`), replace:

```sh
for pkg in $PKGS; do
```

with:

```sh
for pkg in $(payload_pkgs "$P"); do
```

Leave the `SMALL="…"` variable and the §3 `for pkg in $SMALL` loop untouched (curated fast subset, not variant-affected).

- [ ] **Step 3: `bios_audit.sh` §3 → captured set (+ skip apps with no adata.tar)**

In `bios_audit.sh` §3 ("CONFIRM those are actually inside the captured payload tars"), replace:

```sh
for p in dev.eden.eden_emulator com.github.stenzek.duckstation xyz.aethersx2.tturnip com.flycast.emulator me.magnum.melonds.nightly; do
  echo "-- $p adata.tar --"
  tar -tf "$P/$p/adata.tar" 2>/dev/null | grep -iE 'bios|/keys/|prod.keys|title.keys|firmware|registered|\.nca$|\.bin$' | head -6
done
```

with:

```sh
for p in $(payload_pkgs "$P"); do
  [ -f "$P/$p/adata.tar" ] || continue
  echo "-- $p adata.tar --"
  tar -tf "$P/$p/adata.tar" 2>/dev/null | grep -iE 'bios|/keys/|prod.keys|title.keys|firmware|registered|\.nca$|\.bin$' | head -6
done
```

(§1 and §2 are unchanged — §2 already iterates `cat "$P/pkglist.txt"`.)

- [ ] **Step 4: Static verification of the swaps**

Run:

```bash
cd "$(git rev-parse --show-toplevel)"
grep -n 'for pkg in $(user_pkgs)' provision/root/diag/serial_audit.sh
grep -n 'payload_pkgs "$P"' provision/root/diag/dryrun.sh provision/root/diag/bios_audit.sh
! grep -rn 'for pkg in $PKGS' provision/root/diag/serial_audit.sh provision/root/diag/dryrun.sh
! grep -n 'xyz.aethersx2.tturnip' provision/root/diag/bios_audit.sh
```

Expected: the first two `grep`s each print a match; the two `!`-negated `grep`s find nothing (exit 0). `[VERIFY on device]`: on a real golden, `dryrun.sh` / `bios_audit.sh` / `serial_audit.sh` enumerate the golden's actual cloned packages (and, after a deliberate `.tturnip`→`.android` swap, follow it).

- [ ] **Step 5: Commit**

```bash
git add provision/root/diag/serial_audit.sh provision/root/diag/dryrun.sh provision/root/diag/bios_audit.sh
git commit -m "refactor(diag): drive serial_audit/dryrun/bios_audit off the golden's package set"
```

---

### Task 3: `verify.sh` — ownership loop + variant-resolved PS2 BIOS line

**Files:**
- Modify: `provision/root/verify.sh` (ownership loop ~line 13; key/BIOS block ~lines 20-22)

**Interfaces:**
- Consumes: `payload_pkgs` from `lib-root.sh`. `$SD` is already defined in `verify.sh` as `detect_sd`.

- [ ] **Step 1: Ownership loop → captured set**

Replace:

```sh
for p in dev.eden.eden_emulator com.github.stenzek.duckstation xyz.aethersx2.tturnip; do
  u=$(stat -c %u /data/data/$p 2>/dev/null)
  own=$(stat -c '%u:%g' /data/media/0/Android/data/$p 2>/dev/null)
  echo "  $p  app_uid=$u   Android/data=$own"
done
```

with:

```sh
for p in $(payload_pkgs "$SD/golden_root_payload"); do
  u=$(stat -c %u /data/data/$p 2>/dev/null)
  own=$(stat -c '%u:%g' /data/media/0/Android/data/$p 2>/dev/null)
  echo "  $p  app_uid=$u   Android/data=$own"
done
```

- [ ] **Step 2: Resolve the PS2 runner from the captured set**

In the "key/BIOS files present + readable" block, immediately after this header line:

```sh
echo; echo "===== key/BIOS files present + readable ====="
```

insert:

```sh
PS2="$(payload_pkgs "$SD/golden_root_payload" | grep -m1 aethersx2)"   # .android or .tturnip — follow the golden
```

Then replace the hardcoded PS2 BIOS line:

```sh
ls /data/media/0/Android/data/xyz.aethersx2.tturnip/files/bios/ 2>/dev/null | sed 's/^/  nethersx2 bios: /'
```

with:

```sh
[ -n "$PS2" ] && ls /data/media/0/Android/data/$PS2/files/bios/ 2>/dev/null | sed "s|^|  ps2 ($PS2) bios: |"
```

(The eden `prod.keys` and duckstation `bios` lines stay literal — not variant-prone. The `deviceidle … grep -cE '…aethersx2'` line already matches both variants by substring — leave it.)

- [ ] **Step 3: Static verification**

Run:

```bash
cd "$(git rev-parse --show-toplevel)"
grep -n 'payload_pkgs "$SD/golden_root_payload"' provision/root/verify.sh   # >=2 matches (loop + PS2=)
! grep -n 'xyz.aethersx2.tturnip' provision/root/verify.sh                  # no literal left
grep -n 'aethersx2' provision/root/verify.sh                                # deviceidle substring still present
```

Expected: first `grep` prints two matches; the `!`-negated `grep` finds nothing (exit 0); the last still shows the deviceidle line. `[VERIFY on device]`: on a restored unit, `verify.sh` lists ownership for the golden's actual package set and prints the PS2 BIOS line for whichever aethersx2 variant the golden carries.

- [ ] **Step 4: Commit**

```bash
git add provision/root/verify.sh
git commit -m "refactor(verify): golden-driven ownership loop + variant-resolved PS2 BIOS check"
```

---

### Task 4: `restore.sh` fallback consistency + Python suite regression

**Files:**
- Modify: `provision/root/restore.sh:29`

**Interfaces:**
- Consumes: `payload_pkgs` from `lib-root.sh`. `$P` (payload dir) is already defined in `restore.sh`.

- [ ] **Step 1: Use `payload_pkgs` for the no-manifest fallback (behavior-preserving)**

Replace line 29:

```sh
  RPKGS="$(cat "$P/pkglist.txt" 2>/dev/null)"; [ -n "$RPKGS" ] || RPKGS="$PKGS"
```

with:

```sh
  RPKGS="$(payload_pkgs "$P")"
```

(The manifest branch above it — `RPKGS="$(manifest_pkgs "$CAS_MANIFEST")"` — is unchanged and still takes precedence. `payload_pkgs` reproduces the exact prior semantics: pkglist.txt if present, else `$PKGS`.)

- [ ] **Step 2: Static verification of the swap**

Run:

```bash
cd "$(git rev-parse --show-toplevel)"
grep -n 'RPKGS="$(payload_pkgs "$P")"' provision/root/restore.sh
! grep -n 'RPKGS="$(cat "$P/pkglist.txt"' provision/root/restore.sh
```

Expected: first `grep` prints a match; the `!`-negated `grep` finds nothing (exit 0).

- [ ] **Step 3: Re-run the `payload_pkgs` smoke test (no regression)**

Run: `bash tests/test_payload_pkgs.sh`
Expected: `PASS: payload_pkgs`.

- [ ] **Step 4: Run the Python suite (must stay green)**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py tests/test_firmware.py -q`
Expected: all tests pass (these scripts are not exercised by the Python suite, so this is a no-op confirmation — no failures, no errors).

- [ ] **Step 5: Commit**

```bash
git add provision/root/restore.sh
git commit -m "refactor(restore): use payload_pkgs for the no-manifest fallback (no behavior change)"
```

---

## Self-Review

**Spec coverage** (against `2026-06-29-golden-driven-package-set-design.md`):
- §4.1 `payload_pkgs` + `PKGS` comment demotion → Task 1. ✓
- §4.2 `serial_audit.sh` → `user_pkgs` → Task 2 Step 1. ✓
- §4.3 `dryrun.sh` §1 → `payload_pkgs`; `SMALL` untouched → Task 2 Step 2. ✓
- §4.4 `bios_audit.sh` §3 → `payload_pkgs` → Task 2 Step 3. ✓
- §4.5 `verify.sh` ownership loop + variant-resolved PS2 line; eden/duckstation literal; deviceidle unchanged → Task 3. ✓
- §4.6 `restore.sh:29` → `payload_pkgs` → Task 4 Step 1. ✓
- §7 testing: `payload_pkgs` local smoke test (Task 1) + `[VERIFY on device]` markers (Tasks 2-3) + Python suite green (Task 4). ✓

**Placeholder scan:** No TBD/TODO; every code step shows exact before/after text and exact commands. ✓

**Type/name consistency:** `payload_pkgs` signature (optional `payload_dir`, newline-separated output) is defined in Task 1 and consumed identically in Tasks 2-4. `$P`, `$SD`, `user_pkgs`, `PKGS`, `CAS_OUT`, `detect_sd` all reference existing symbols in the respective scripts. ✓
