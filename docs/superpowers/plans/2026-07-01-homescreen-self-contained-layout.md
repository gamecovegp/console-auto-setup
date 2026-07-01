# Self-contained Homescreen Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an `@homescreen` golden self-contained — bundle the installer for every app the layout references and install the absent ones on restore before re-applying the favorites DB — so a placed app (e.g. the game launcher in an "Emulators" tab) resolves on any target model.

**Architecture:** Four small POSIX-sh helpers in `provision/root/lib-root.sh` (`homescreen_apps`, `install_apks`, `homescreen_bundle_apps`, `homescreen_install_missing`); thin glue calls in `capture.sh` (bundle during the `@homescreen` block) and `restore.sh` (refactor step-1 install to reuse `install_apks`; install-if-absent before the step-8 favorites-DB extract); a copy-only tip update in `cas/gui.py`. Additive throughout — matches how `@homescreen` behaves today.

**Tech Stack:** POSIX sh (device runs these under `su`/toybox), Python/Tkinter GUI (copy only), bash-driven shell unit tests that source `lib-root.sh` with stubbed device binaries.

## Global Constraints

- **Shell dialect:** `lib-root.sh`, `capture.sh`, `restore.sh` run under the device's `su`/toybox POSIX sh. **No bashisms** in those files (no `[[ ]]`, arrays, `local`). Test harnesses are `#!/usr/bin/env bash` but exercise the sh functions unchanged (mirror `tests/test_game_launcher.sh`).
- **Additive failure contract:** `@homescreen` capture problems **WARN, never bump `CFAIL`**; restore problems **WARN, never bump `FAIL`**. A clone whose layout only partly resolved is still functionally clean.
- **No new GUI toggle.** Self-containment is an implementation detail of the existing `@homescreen` flag; only tip copy changes.
- **Dedup:** never bundle an APK the per-app loop already captured (`$P/<pkg>/apk` exists) and never bundle the HOME launcher itself.
- **Staging seam:** `install_apks` stages to `${CAS_INST_DIR:-/data/local/tmp/_inst}`. The default is byte-identical to today's on-device path; the env var exists **only** so off-device tests can point it at scratch.
- **Local var hygiene:** helper-internal variables are prefixed (`_ha_`, `_ia_`, `_hb_`, `_hm_`) so they never clobber caller-scope vars (`pkg`, `FAIL`, `P`, `LP`).
- **Running shell tests:** `bash tests/<file>.sh`; success prints a final `PASS: <name>` line and exits 0. There is no central runner to register new files in.
- **Commit messages:** end every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Payload transport:** the golden payload directory is synced opaquely by the PC side; `homescreen/apps/` needs **no** Python/manifest change (the sibling `homescreen/` dir already rides along).

---

### Task 1: `homescreen_apps` — derive the packages a layout references

**Files:**
- Modify: `provision/root/lib-root.sh` (insert a new function just above the `# The GAME FRONTEND …` comment, ~line 125)
- Test: `tests/test_homescreen_apps.sh` (create)

**Interfaces:**
- Consumes: nothing (pure text extraction over a directory).
- Produces: `homescreen_apps <launcher_data_dir>` → prints the deduped set of referenced package names (one per line, `sort -u` order) to stdout; `rc 0` always (empty output when the dir is missing or holds no tokens). No dependency on `pm` or device state.

- [ ] **Step 1: Write the failing test**

Create `tests/test_homescreen_apps.sh`:

```bash
#!/usr/bin/env bash
# Local smoke test for homescreen self-containment helpers (no device). Sources lib-root.sh,
# stubs device binaries, uses scratch trees. Run: bash tests/test_homescreen_apps.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# === homescreen_apps: extract component=/package= tokens, dedup, launcher NOT special-cased here =====
ld="$tmp/launcher_data/com.launch.home"; mkdir -p "$ld/databases"
# a favorites DB stored as plaintext intents (Launcher3-family shape): two icons for com.foo (dedup),
# one for com.bar via a package= column, plus the launcher's own component.
printf '%s\n' \
  '#Intent;component=com.foo/.MainActivity;end' \
  '#Intent;component=com.foo/.OtherActivity;end' \
  'itemType=1;package=com.bar;' \
  '#Intent;component=com.launch.home/.Home;end' > "$ld/databases/launcher.db"
got="$(homescreen_apps "$ld" | tr '\n' ' ')"
[ "$got" = "com.bar com.foo com.launch.home " ] || { echo "FAIL(extract+dedup): [$got]"; fail=1; }

# a launcher whose DB holds no such tokens -> empty result, rc 0 (degrade gracefully)
ld2="$tmp/blob/com.blob"; mkdir -p "$ld2"
printf '\x00\x01\x02binaryjunkno-tokens-here\xff' > "$ld2/launcher.db"
got2="$(homescreen_apps "$ld2")"; rc2=$?
[ -z "$got2" ] && [ "$rc2" -eq 0 ] || { echo "FAIL(blob): [$got2] rc=$rc2"; fail=1; }

# a missing dir -> empty, rc 0
got3="$(homescreen_apps "$tmp/does-not-exist")"; rc3=$?
[ -z "$got3" ] && [ "$rc3" -eq 0 ] || { echo "FAIL(missing dir): [$got3] rc=$rc3"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: homescreen_apps"; exit 0; } || exit 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test_homescreen_apps.sh`
Expected: FAIL — `homescreen_apps: command not found` / non-`PASS` output, exit 1.

- [ ] **Step 3: Write minimal implementation**

In `provision/root/lib-root.sh`, insert **directly above** the line
`# The GAME FRONTEND (holds per-system emulator picks) — DISTINCT from the Android HOME app (home_launcher).`:

```sh
# The set of packages a homescreen layout REFERENCES — for self-containment, so every placed icon can
# resolve on any unit model. Intent strings in the Launcher3-family favorites DB are stored as plaintext
# (component=<pkg>/<cls> and package=<pkg>), so a launcher-agnostic token scan works without sqlite3 and
# degrades to empty on an exotic binary blob. PURE: no pm/device dependency (caller filters by pm path).
# homescreen_apps <launcher_data_dir> -> deduped pkg names on stdout (launcher itself NOT excluded here).
homescreen_apps(){
  _ha_dir="$1"; [ -d "$_ha_dir" ] || return 0
  { grep -rahoE 'component=[A-Za-z0-9._]+/' "$_ha_dir" 2>/dev/null | sed 's/^component=//; s#/.*##'
    grep -rahoE 'package=[A-Za-z0-9._]+'     "$_ha_dir" 2>/dev/null | sed 's/^package=//'
  } | sort -u
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test_homescreen_apps.sh`
Expected: `PASS: homescreen_apps`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add provision/root/lib-root.sh tests/test_homescreen_apps.sh
git commit -m "feat(provision): homescreen_apps — derive packages a layout references

Launcher-agnostic plaintext token scan (component=/package=) over the launcher
data dir; pure/deduped, no pm dependency. First helper for self-contained
homescreen layouts.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `install_apks` helper + refactor restore step 1 to use it

**Files:**
- Modify: `provision/root/lib-root.sh` (append `install_apks` at end of file)
- Modify: `provision/root/restore.sh:68-80` (replace the inline staged-install block with a call)
- Test: `tests/test_install_apks.sh` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `install_apks <apk_source_dir> <pkg_label>` → stages `<apk_source_dir>/*.apk` to `${CAS_INST_DIR:-/data/local/tmp/_inst}` and installs (single → `pm install`; splits → `install-create`/`write`/`commit`). Returns `0` on success, non-zero on any failure (missing APK, failed install, no session). Warns on failure; the **caller** owns FAIL accounting.

- [ ] **Step 1: Write the failing test**

Create `tests/test_install_apks.sh`:

```bash
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test_install_apks.sh`
Expected: FAIL — `install_apks: command not found`, exit 1.

- [ ] **Step 3: Write minimal implementation**

Append to the **end** of `provision/root/lib-root.sh`:

```sh
# install_apks <apk_source_dir> <pkg_label> — stage the dir's *.apk to a clean tmp and install (single ->
# pm install; splits -> install session). Returns 0 on success, non-zero on any failure. Proven gotchas
# (wiped golden, 2026-06-16): `pm install-multiple` is "Unknown command" in this su/pm context, and
# installing straight off the FUSE exfat SD triggers a cross-context avc denial — so ALWAYS stage first.
# CAS_INST_DIR overrides the staging path for off-device tests (default is the on-device path, unchanged).
install_apks(){
  _ia_src="$1"; _ia_pkg="$2"; _ia_stage="${CAS_INST_DIR:-/data/local/tmp/_inst}"
  set -- "$_ia_src"/*.apk
  [ -f "$1" ] || { warn "install_apks: no APK in $_ia_src ($_ia_pkg)"; return 1; }
  rm -rf "$_ia_stage"; mkdir -p "$_ia_stage"
  cp "$@" "$_ia_stage/" 2>/dev/null; set -- "$_ia_stage"/*.apk
  _ia_rc=0
  if [ "$#" -eq 1 ]; then
    pm install -r -g "$1" >/dev/null 2>&1 || { warn "install failed: $_ia_pkg"; _ia_rc=1; }
  else
    _ia_sid="$(pm install-create -r -g 2>/dev/null | sed -n 's/.*\[\([0-9]*\)\].*/\1/p')"
    if [ -z "$_ia_sid" ]; then warn "install-create gave no session: $_ia_pkg"; rm -rf "$_ia_stage"; return 1; fi
    _ia_i=0; for _ia_a in "$@"; do pm install-write "$_ia_sid" "s$_ia_i" "$_ia_a" >/dev/null 2>&1 || warn "install-write failed: $_ia_pkg s$_ia_i"; _ia_i=$((_ia_i+1)); done
    pm install-commit "$_ia_sid" >/dev/null 2>&1 || { warn "split install failed: $_ia_pkg"; pm install-abandon "$_ia_sid" >/dev/null 2>&1; _ia_rc=1; }
  fi
  rm -rf "$_ia_stage"
  return $_ia_rc
}
```

Then in `provision/root/restore.sh`, replace the inline staged-install block (currently lines 68-80):

```sh
  rm -rf /data/local/tmp/_inst; mkdir -p /data/local/tmp/_inst
  cp "$@" /data/local/tmp/_inst/ 2>/dev/null; set -- /data/local/tmp/_inst/*.apk
  if [ "$#" -eq 1 ]; then
    pm install -r -g "$1" >/dev/null 2>&1 || { warn "install failed: $pkg"; FAIL=$((FAIL+1)); }
  else
    SID="$(pm install-create -r -g 2>/dev/null | sed -n 's/.*\[\([0-9]*\)\].*/\1/p')"
    if [ -z "$SID" ]; then
      warn "install-create gave no session: $pkg"; FAIL=$((FAIL+1)); rm -rf /data/local/tmp/_inst; continue
    fi
    n=0; for a in "$@"; do pm install-write "$SID" "s$n" "$a" >/dev/null 2>&1 || warn "install-write failed: $pkg s$n"; n=$((n+1)); done
    pm install-commit "$SID" >/dev/null 2>&1 || { warn "split install failed: $pkg"; pm install-abandon "$SID" >/dev/null 2>&1; FAIL=$((FAIL+1)); }
  fi
  rm -rf /data/local/tmp/_inst
```

with the single-line call (the surrounding `set -- "$P/$pkg/apk/"*.apk` at line 51 and the config-only guard above it stay unchanged):

```sh
  install_apks "$P/$pkg/apk" "$pkg" || FAIL=$((FAIL+1))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test_install_apks.sh`
Expected: `PASS: install_apks`, exit 0.

- [ ] **Step 5: Verify no regression in existing shell tests**

Run: `bash tests/test_game_launcher.sh && bash tests/test_manifest_axes.sh && bash tests/test_payload_pkgs.sh && bash tests/test_homescreen_apps.sh`
Expected: four `PASS:` lines, exit 0. (Also sanity-read `restore.sh` around line 68 to confirm the config-only branch and `set --` at line 51 are intact.)

- [ ] **Step 6: Commit**

```bash
git add provision/root/lib-root.sh provision/root/restore.sh tests/test_install_apks.sh
git commit -m "refactor(provision): factor staged split-install into install_apks

Extract restore.sh step-1's proven stage-to-tmp + single/split install into a
reusable lib-root helper (CAS_INST_DIR seam for off-device tests). Behavior
unchanged; reused by the homescreen install-if-absent step next.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `homescreen_bundle_apps` helper + capture-side bundling

**Files:**
- Modify: `provision/root/lib-root.sh` (append `homescreen_bundle_apps` at end of file)
- Modify: `provision/root/capture.sh` (inside the `@homescreen` block, after the `launcher_data.tar` validation, ~line 144)
- Test: `tests/test_homescreen_apps.sh` (extend)

**Interfaces:**
- Consumes: `homescreen_apps` (Task 1); device `pm path`.
- Produces: `homescreen_bundle_apps <launcher_data_dir> <payload_dir> <launcher_pkg>` → for each referenced pkg that is **not** the launcher and **not** already captured at `<payload_dir>/<pkg>/apk`, copies `pm path` APKs (base+splits) into `<payload_dir>/homescreen/apps/<pkg>/`; prints the count of bundled apps to stdout; `rc 0`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_homescreen_apps.sh`, **before** the final `PASS`/exit line:

```bash
# === homescreen_bundle_apps: bundle referenced apps' APKs, skip launcher + already-captured ==========
apkroot="$tmp/apks"; mkdir -p "$apkroot"
: > "$apkroot/foo-base.apk"; : > "$apkroot/bar-base.apk"; : > "$apkroot/launch-base.apk"
# pm path stub: map each pkg to a scratch APK file that exists
pm(){ case "$1" in
        path) case "$2" in
                com.foo)         echo "package:$apkroot/foo-base.apk";;
                com.bar)         echo "package:$apkroot/bar-base.apk";;
                com.launch.home) echo "package:$apkroot/launch-base.apk";;
                *) return 1;; esac;;
        *) return 0;; esac; }

bld="$tmp/bld/com.launch.home"; mkdir -p "$bld/databases"
printf '%s\n' \
  '#Intent;component=com.foo/.Main;end' \
  'package=com.bar;' \
  '#Intent;component=com.launch.home/.Home;end' \
  '#Intent;component=com.nopath/.X;end' > "$bld/databases/launcher.db"   # com.nopath: pm path fails -> skipped
pay="$tmp/payload"; mkdir -p "$pay/com.bar/apk"                          # com.bar already captured -> skipped
n="$(homescreen_bundle_apps "$bld" "$pay" "com.launch.home")"
[ "$n" = "1" ] || { echo "FAIL(bundle count): [$n]"; fail=1; }
[ -f "$pay/homescreen/apps/com.foo/foo-base.apk" ] || { echo "FAIL(bundle: com.foo not bundled)"; fail=1; }
[ ! -d "$pay/homescreen/apps/com.bar" ]         || { echo "FAIL(bundle: com.bar should be skipped, already captured)"; fail=1; }
[ ! -d "$pay/homescreen/apps/com.launch.home" ] || { echo "FAIL(bundle: launcher should be skipped)"; fail=1; }
[ ! -d "$pay/homescreen/apps/com.nopath" ]      || { echo "FAIL(bundle: no-path pkg should be skipped)"; fail=1; }
unset -f pm
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test_homescreen_apps.sh`
Expected: FAIL — `homescreen_bundle_apps: command not found`, exit 1.

- [ ] **Step 3: Write minimal implementation**

Append to the **end** of `provision/root/lib-root.sh`:

```sh
# homescreen_bundle_apps <launcher_data_dir> <payload_dir> <launcher_pkg> — SELF-CONTAINED LAYOUT: bundle
# the installer for every app the layout references so each icon resolves on ANY target model. Skips the
# launcher itself and apps the per-app loop already captured ($payload/<pkg>/apk) — no duplicate APKs.
# Copies base+splits from `pm path`. Prints the count of bundled apps. Additive: a copy miss is silent.
homescreen_bundle_apps(){
  _hb_ldir="$1"; _hb_pd="$2"; _hb_lp="$3"; _hb_hsa="$_hb_pd/homescreen/apps"; _hb_n=0
  for _hb_p in $(homescreen_apps "$_hb_ldir"); do
    [ "$_hb_p" = "$_hb_lp" ] && continue
    [ -d "$_hb_pd/$_hb_p/apk" ] && continue
    _hb_paths="$(pm path "$_hb_p" 2>/dev/null | sed 's/^package://')"
    [ -n "$_hb_paths" ] || continue
    mkdir -p "$_hb_hsa/$_hb_p"
    for _hb_ap in $_hb_paths; do cp "$_hb_ap" "$_hb_hsa/$_hb_p/" 2>/dev/null; done
    if [ -n "$(ls -A "$_hb_hsa/$_hb_p" 2>/dev/null)" ]; then _hb_n=$((_hb_n+1)); else rmdir "$_hb_hsa/$_hb_p" 2>/dev/null; fi
  done
  echo "$_hb_n"
}
```

Then in `provision/root/capture.sh`, inside the `@homescreen` block, replace:

```sh
    if tar -tf "$HS/launcher_data.tar" >/dev/null 2>&1; then ok "captured homescreen launcher: $LP"
    else warn "homescreen launcher_data.tar looks corrupt ($LP) — homescreen will be skipped on restore"; rm -f "$HS/launcher_data.tar"; fi
```

with (adds the bundling only when the layout was actually captured):

```sh
    if tar -tf "$HS/launcher_data.tar" >/dev/null 2>&1; then ok "captured homescreen launcher: $LP"
    else warn "homescreen launcher_data.tar looks corrupt ($LP) — homescreen will be skipped on restore"; rm -f "$HS/launcher_data.tar"; fi
    # SELF-CONTAINED LAYOUT: bundle installers for the apps placed on the homescreen so every icon resolves
    # on ANY unit model (a placed app absent on the unit is installed on restore, then the favorites DB is
    # applied). Additive — never bumps CFAIL. Only when the layout was actually captured.
    if [ -f "$HS/launcher_data.tar" ]; then
      _hs_n="$(homescreen_bundle_apps "/data/data/$LP" "$P" "$LP")"
      [ "${_hs_n:-0}" -gt 0 ] 2>/dev/null && ok "homescreen: bundled $_hs_n placed-app installer(s) so icons resolve on any model"
    fi
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test_homescreen_apps.sh`
Expected: `PASS: homescreen_apps`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add provision/root/lib-root.sh provision/root/capture.sh tests/test_homescreen_apps.sh
git commit -m "feat(provision): bundle placed-app installers into the golden (@homescreen)

homescreen_bundle_apps copies base+splits for every app the layout references
(skipping the launcher + already-captured apps) into homescreen/apps/<pkg>/;
capture.sh runs it when the layout is captured. Additive, dedup-safe.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `homescreen_install_missing` helper + restore-side install-if-absent

**Files:**
- Modify: `provision/root/lib-root.sh` (append `homescreen_install_missing` at end of file)
- Modify: `provision/root/restore.sh` (step 8, before the favorites-DB extract, ~line 327)
- Test: `tests/test_homescreen_apps.sh` (extend)

**Interfaces:**
- Consumes: `install_apks` (Task 2); device `pm path`.
- Produces: `homescreen_install_missing <payload_dir>` → for each `<payload_dir>/homescreen/apps/<pkg>/` whose pkg is **absent** on the unit (`pm path` fails), calls `install_apks` and WARNs on failure; skips packages already present. Always `rc 0` (additive).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_homescreen_apps.sh`, **before** the final `PASS`/exit line:

```bash
# === homescreen_install_missing: install only ABSENT placed apps (additive) =========================
pay2="$tmp/payload2"; mkdir -p "$pay2/homescreen/apps/com.present" "$pay2/homescreen/apps/com.absent"
: > "$pay2/homescreen/apps/com.present/base.apk"; : > "$pay2/homescreen/apps/com.absent/base.apk"
pm(){ case "$1" in
        path) case "$2" in com.present) return 0;; *) return 1;; esac;;   # only com.present is installed
        *) return 0;; esac; }
INSTALL_LOG="$tmp/install.log"; : > "$INSTALL_LOG"
install_apks(){ echo "$2" >> "$INSTALL_LOG"; return 0; }                    # stub: record which pkg we install
homescreen_install_missing "$pay2" || { echo "FAIL(install_missing rc)"; fail=1; }
[ "$(cat "$INSTALL_LOG" 2>/dev/null)" = "com.absent" ] || { echo "FAIL(install_missing: wrong set): [$(tr '\n' ' ' < "$INSTALL_LOG")]"; fail=1; }
unset -f pm install_apks

# no homescreen/apps dir -> rc 0, no error
homescreen_install_missing "$tmp/payload" || { echo "FAIL(install_missing: no-apps dir rc)"; fail=1; }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test_homescreen_apps.sh`
Expected: FAIL — `homescreen_install_missing: command not found`, exit 1.

- [ ] **Step 3: Write minimal implementation**

Append to the **end** of `provision/root/lib-root.sh`:

```sh
# homescreen_install_missing <payload_dir> — install any placed app that is ABSENT on THIS unit, so its
# icon resolves when the favorites DB is applied (a wiped unit / different model may lack the game launcher
# or other placed apps). Skips apps already present. Additive: a miss WARNs, never fails the restore.
homescreen_install_missing(){
  _hm_pd="$1"; _hm_hsa="$_hm_pd/homescreen/apps"
  [ -d "$_hm_hsa" ] || return 0
  for _hm_d in "$_hm_hsa"/*/; do
    [ -d "$_hm_d" ] || continue
    _hm_p="$(basename "$_hm_d")"
    if pm path "$_hm_p" >/dev/null 2>&1; then
      log "homescreen: $_hm_p already present — no install needed"
    else
      install_apks "$_hm_d" "$_hm_p" \
        || warn "homescreen: could not install $_hm_p — its icon may not resolve (platform-signed system app on a foreign key?)"
    fi
  done
  return 0
}
```

Then in `provision/root/restore.sh`, in step 8's launcher-uid-resolved branch, replace:

```sh
    else
      am force-stop "$LP" 2>/dev/null
      rm -rf "/data/data/$LP/"* "/data/data/$LP/".[!.]* 2>/dev/null
```

with (install absent placed apps BEFORE clearing/extracting the launcher data, so components resolve):

```sh
    else
      # SELF-CONTAINED LAYOUT: install any placed app that's absent on THIS unit BEFORE re-applying the
      # favorites DB, so every icon's component resolves. Runs on the same-family success path only (we're
      # about to apply the layout). Additive — never bumps FAIL.
      homescreen_install_missing "$P"
      am force-stop "$LP" 2>/dev/null
      rm -rf "/data/data/$LP/"* "/data/data/$LP/".[!.]* 2>/dev/null
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test_homescreen_apps.sh`
Expected: `PASS: homescreen_apps`, exit 0.

- [ ] **Step 5: Verify full shell-test suite**

Run: `for t in tests/test_homescreen_apps.sh tests/test_install_apks.sh tests/test_game_launcher.sh tests/test_manifest_axes.sh tests/test_payload_pkgs.sh tests/test_grant_appops.sh tests/test_scrub.sh; do bash "$t" || echo "REGRESSION: $t"; done`
Expected: a `PASS:` line per test, no `REGRESSION:` line.

- [ ] **Step 6: Commit**

```bash
git add provision/root/lib-root.sh provision/root/restore.sh tests/test_homescreen_apps.sh
git commit -m "feat(provision): install absent placed apps before applying the layout

restore.sh step 8 now installs any homescreen/apps/<pkg> missing on the unit
(via install_apks) before extracting the favorites DB, so every icon resolves
on any model. Additive; also unblocks @gamelauncher where the frontend was absent.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Update `@homescreen` tip copy (Save + Download)

**Files:**
- Modify: `cas/gui.py:103-104` (Download modal `_DL_FLAG_TIPS["homescreen"]`)
- Modify: `cas/gui.py:1360-1361` (Save modal `tips["homescreen"]`)

**Interfaces:**
- Consumes/Produces: none — copy-only. No behavior change, no new control.

- [ ] **Step 1: Update the Download-modal tip**

In `cas/gui.py`, replace:

```python
    "homescreen": "Restore the homescreen layout — your app folders, icon/dock arrangement, "
                  "wallpaper (and widgets, best-effort).",
```

with:

```python
    "homescreen": "Restore the homescreen layout — your app folders, icon/dock arrangement, "
                  "wallpaper (and widgets, best-effort). Placed apps missing on the unit are "
                  "installed first so every icon resolves.",
```

- [ ] **Step 2: Update the Save-modal tip**

In `cas/gui.py`, replace:

```python
            "homescreen": "Capture this device's homescreen layout (icon/folder/dock arrangement + "
                          "wallpaper) into the golden (and restore it by default on Download).",
```

with:

```python
            "homescreen": "Capture this device's homescreen layout (icon/folder/dock arrangement + "
                          "wallpaper) — and bundle the installers for the apps you placed so every "
                          "icon resolves on any unit model — into the golden (restored by default on Download).",
```

- [ ] **Step 3: Verify the copy changed and Python tests still pass**

Run: `grep -n "installed first so every icon resolves\|bundle the installers for the apps you placed" cas/gui.py`
Expected: two matching lines (one per tip).

Run: `python -m pytest tests/test_cas.py -q`
Expected: all tests pass (no test pins the homescreen tip text).

- [ ] **Step 4: Commit**

```bash
git add cas/gui.py
git commit -m "docs(gui): @homescreen tip notes placed-app installers are bundled/installed

Copy-only: Save tip says it bundles the placed apps' installers; Download tip
says missing placed apps are installed first so every icon resolves. No new toggle.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage**

| Spec element | Task |
|---|---|
| `homescreen_apps` token scan (`component=`/`package=`, dedup, degrade) | Task 1 |
| Bundle any referenced app with a resolvable APK; skip launcher + already-captured | Task 3 (`homescreen_bundle_apps`) + capture.sh glue |
| Restore installs absent placed apps before the favorites-DB extract; additive WARN | Task 4 (`homescreen_install_missing`) + restore.sh glue |
| Refactor the proven staged split-install into `install_apks`, reused by step 1 + step 8 | Task 2 |
| `@gamelauncher` synergy (frontend now installed) | Emergent from Task 4 ordering (step 8 precedes the game-launcher step at `restore.sh:349`); no separate task needed |
| GUI: no new toggle, tip copy only (both modals) | Task 5 |
| Additive contract, dedup, same-family guard unchanged | Enforced in Tasks 3/4 (never bump CFAIL/FAIL; `[ -d $P/<pkg>/apk ]` skip; step-8 glue sits inside the existing same-family branch) |
| Tests mirror `test_game_launcher.sh`; new `test_homescreen_apps.sh` | Tasks 1/3/4 (+ `test_install_apks.sh` in Task 2) |

No uncovered spec requirement.

**2. Placeholder scan:** No `TBD`/`TODO`/"handle edge cases"/"similar to Task N". Every code step shows complete code; every test step shows real assertions.

**3. Type/name consistency:** `homescreen_apps <dir>`, `install_apks <apk_source_dir> <pkg_label>`, `homescreen_bundle_apps <launcher_data_dir> <payload_dir> <launcher_pkg>`, `homescreen_install_missing <payload_dir>` are used identically in their defining task, their glue, and their tests. Bundle path `homescreen/apps/<pkg>/` matches between capture (Task 3) and restore (Task 4). `CAS_INST_DIR` seam defined and consumed only in Task 2. Caller-scope vars (`pkg`, `FAIL`, `P`, `LP`) are untouched by helper-internal `_ia_`/`_hb_`/`_hm_`/`_ha_` locals.
