# Default-launcher propagation + Companion-grant robustness + Update mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Set a configurable default HOME launcher (default ES-DE) and make the All-Files-Access grant reliably stick, both deliverable to already-provisioned units via a new non-destructive `CAS_MODE=update`.

**Architecture:** Two on-device shell helpers (`set_home_launcher`, `grant_all_files`) in `lib-root.sh`; `restore.sh` gains a `CAS_MODE` switch that gates its destructive phases and runs a new set-default-HOME step + an extracted grant pass; `provision.py` gains a `mode` parameter that, in `update`, drops the SD guard, pushes a reduced payload, and passes `CAS_MODE=update`; the GUI gets an "Update" button.

**Tech Stack:** Python 3 (stdlib `unittest`, mocks adb), POSIX `sh` (Android toybox), tkinter GUI.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-27-default-launcher-and-update-mode-design.md`.
- Run tests with **unittest, not pytest** (pytest chokes on the `[07]` brackets in the repo path): `python -m unittest tests.test_cas -v`.
- Home-launcher determination order: `@home <pkg>` manifest flag → else `org.es_de.frontend` if in manifest → else captured `launcher_pkg`/`launcher_component`. **Never read the OEM launcher.**
- ES-DE home component is `org.es_de.frontend/org.es_de.frontend.MainActivityHomeApp` (declares `CATEGORY_HOME`); role-holder is package-only so no component is needed for the ES-DE path.
- Failure contract unchanged: counted failure → `restore.sh` exits non-zero. **All-Files grant failure = FAIL; default-launcher set failure = WARN** (additive).
- Update mode RUNS: set default HOME, All-Files grants, `@settings`, `@hardening`. Update mode SKIPS: app (re)install, per-app data restore, internal-dir restore, favorites-DB clone, SAF-urigrants overwrite, OOBE/locale, cores.
- All 106 existing tests must stay green.
- Shell logic that needs a real device carries a `[VERIFY on device]` note; do not attempt to run `restore.sh` end-to-end in tests (it touches absolute `/data/...` paths).

---

### Task 1: `lib-root.sh` helpers — `home_launcher_component`, `set_home_launcher`, `grant_all_files`

**Files:**
- Modify: `provision/root/lib-root.sh` (add three functions near the existing `home_launcher`, ~line 47-53)
- Test: `tests/test_cas.py` (new class `TestRootHelpers`, subprocess-driven shell tests with PATH stubs)

**Interfaces:**
- Produces (shell):
  - `home_launcher_component` → echoes the resolved HOME `pkg/cls`, empty if none.
  - `set_home_launcher <pkg> [component]` → role-holder first (package-only), `set-home-activity <component>` fallback; returns 0 iff `home_launcher` == `<pkg>` afterward.
  - `grant_all_files <pkg>` → returns 0 if the app does not declare `MANAGE_EXTERNAL_STORAGE` (no-op) or if the grant verifies; non-zero if declared but unverifiable.

- [ ] **Step 1: Write the failing tests**

Add to the top of `tests/test_cas.py` after the existing imports (around line 15):

```python
import subprocess
import stat as _stat

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
LIBROOT_SH = ROOT_DIR / "provision" / "root" / "lib-root.sh"


def _write_stub(bindir, name, body):
    p = pathlib.Path(bindir) / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)


def run_libroot(call, stubs, env=None):
    """Source lib-root.sh under a stub PATH and run `call`; return (rc, stdout)."""
    with tempfile.TemporaryDirectory() as bd:
        for name, body in stubs.items():
            _write_stub(bd, name, body)
        e = dict(os.environ)
        e["PATH"] = bd + os.pathsep + e["PATH"]
        if env:
            e.update(env)
        script = f'. "{LIBROOT_SH}"; {call}; echo "RC=$?"'
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=e)
        return out.stdout
```

Then add the test class:

```python
class TestRootHelpers(unittest.TestCase):
    # `cmd` stub: role add-role-holder writes the pkg to $CAS_T_STATE (unless CAS_T_ROLE=0);
    # resolve-activity echoes "<pkg>/X.Home" so home_launcher's ${c%%/*} yields the pkg;
    # set-home-activity writes the component's pkg to state.
    CMD_STUB = (
        'st="$CAS_T_STATE"\n'
        'case "$1 $2" in\n'
        '  "role add-role-holder") [ "${CAS_T_ROLE:-1}" = 1 ] && { for a in "$@"; do p="$a"; done; echo "$p" > "$st"; } ;;\n'
        '  "package resolve-activity") [ -s "$st" ] && echo "$(cat "$st")/X.Home" ;;\n'
        '  "package set-home-activity") c="$3"; echo "${c%%/*}" > "$st" ;;\n'
        'esac\n'
        'exit 0\n'
    )
    DUMPSYS_STUB = '[ "${CAS_T_DECL:-1}" = 1 ] && echo "  android.permission.MANAGE_EXTERNAL_STORAGE: granted=false"\nexit 0\n'
    APPOPS_STUB = (
        'st="$CAS_T_OPS"\n'
        'case "$1" in\n'
        '  set) [ "${CAS_T_OK:-1}" = 1 ] && echo allow > "$st" ;;\n'
        '  get) [ -s "$st" ] && cat "$st" ;;\n'
        'esac\nexit 0\n'
    )

    def test_set_home_launcher_via_role_holder(self):
        with tempfile.TemporaryDirectory() as t:
            st = str(pathlib.Path(t) / "home")
            out = run_libroot('set_home_launcher org.es_de.frontend ""',
                              {"cmd": self.CMD_STUB}, {"CAS_T_STATE": st, "CAS_T_ROLE": "1"})
            self.assertIn("RC=0", out)

    def test_set_home_launcher_falls_back_to_set_home_activity(self):
        with tempfile.TemporaryDirectory() as t:
            st = str(pathlib.Path(t) / "home")
            out = run_libroot('set_home_launcher org.es_de.frontend org.es_de.frontend/org.es_de.frontend.MainActivityHomeApp',
                              {"cmd": self.CMD_STUB}, {"CAS_T_STATE": st, "CAS_T_ROLE": "0"})
            self.assertIn("RC=0", out)   # role-holder did nothing; set-home-activity stuck it

    def test_grant_all_files_ok_when_declared_and_grant_sticks(self):
        with tempfile.TemporaryDirectory() as t:
            ops = str(pathlib.Path(t) / "ops")
            out = run_libroot('grant_all_files com.gamecove.gamecove_companion',
                              {"dumpsys": self.DUMPSYS_STUB, "appops": self.APPOPS_STUB},
                              {"CAS_T_OPS": ops, "CAS_T_DECL": "1", "CAS_T_OK": "1"})
            self.assertIn("RC=0", out)

    def test_grant_all_files_noop_when_not_declared(self):
        with tempfile.TemporaryDirectory() as t:
            ops = str(pathlib.Path(t) / "ops")
            out = run_libroot('grant_all_files some.app',
                              {"dumpsys": self.DUMPSYS_STUB, "appops": self.APPOPS_STUB},
                              {"CAS_T_OPS": ops, "CAS_T_DECL": "0", "CAS_T_OK": "1"})
            self.assertIn("RC=0", out)

    def test_grant_all_files_fails_when_declared_but_grant_blocked(self):
        with tempfile.TemporaryDirectory() as t:
            ops = str(pathlib.Path(t) / "ops")
            out = run_libroot('grant_all_files com.gamecove.gamecove_companion',
                              {"dumpsys": self.DUMPSYS_STUB, "appops": self.APPOPS_STUB},
                              {"CAS_T_OPS": ops, "CAS_T_DECL": "1", "CAS_T_OK": "0"})
            self.assertNotIn("RC=0", out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_cas.TestRootHelpers -v`
Expected: FAIL — `set_home_launcher`/`grant_all_files` are not defined yet (RC non-zero / "not found").

- [ ] **Step 3: Add the helpers to `lib-root.sh`**

Insert immediately after the `home_launcher()` function (after its closing `}`, ~line 53):

```sh
# The full HOME component (pkg/cls) — recorded by capture for the no-frontend fallback path.
home_launcher_component(){
  cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME 2>/dev/null \
    | grep '/' | tail -1
}
# Make <pkg> the default HOME. Role-holder first (package-only; Android picks the pkg's HOME activity, e.g.
# ES-DE's MainActivityHomeApp); set-home-activity <component> as a fallback when a component is supplied.
# Returns 0 only if HOME actually became <pkg>.
set_home_launcher(){
  _p="$1"; _c="$2"
  cmd role add-role-holder --user 0 android.app.role.HOME "$_p" >/dev/null 2>&1
  [ "$(home_launcher)" = "$_p" ] && return 0
  [ -n "$_c" ] && cmd package set-home-activity "$_c" >/dev/null 2>&1
  [ "$(home_launcher)" = "$_p" ]
}
# Robust "All files access" (MANAGE_EXTERNAL_STORAGE). No-op success if the app doesn't declare it; else
# grant via several appops forms (they vary across OEM A14 builds), verify, one retry. Returns grant status.
grant_all_files(){
  _p="$1"
  dumpsys package "$_p" 2>/dev/null | grep -q MANAGE_EXTERNAL_STORAGE || return 0
  appops set "$_p" MANAGE_EXTERNAL_STORAGE allow 2>/dev/null
  appops set --user 0 "$_p" MANAGE_EXTERNAL_STORAGE allow 2>/dev/null
  _u="$(app_uid "$_p")"; [ -n "$_u" ] && appops set --uid "$_u" MANAGE_EXTERNAL_STORAGE allow 2>/dev/null
  appops get "$_p" MANAGE_EXTERNAL_STORAGE 2>/dev/null | grep -q allow && return 0
  sleep 1
  appops set "$_p" MANAGE_EXTERNAL_STORAGE allow 2>/dev/null
  appops get "$_p" MANAGE_EXTERNAL_STORAGE 2>/dev/null | grep -q allow
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_cas.TestRootHelpers -v`
Expected: PASS (5 tests). `[VERIFY on device]`: confirm `cmd role add-role-holder HOME` and `appops` forms behave as expected on a real MANGMI AIR X (A14) unit.

- [ ] **Step 5: Commit**

```bash
git add provision/root/lib-root.sh tests/test_cas.py
git commit -m "feat(root): set_home_launcher + grant_all_files + home_launcher_component helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `capture.sh` records `launcher_component`

**Files:**
- Modify: `provision/root/capture.sh:99`
- Test: `tests/test_cas.py` (grep-guard in a new `TestCaptureScript` — capture.sh itself needs a device to run)

**Interfaces:**
- Produces: `homescreen/meta` now contains a `launcher_component=<pkg/cls>` line (used by restore's no-frontend fallback).

- [ ] **Step 1: Write the failing test**

```python
class TestCaptureScript(unittest.TestCase):
    def test_capture_records_launcher_component(self):
        text = (ROOT_DIR / "provision" / "root" / "capture.sh").read_text()
        self.assertIn("launcher_component=", text)
        self.assertIn("home_launcher_component", text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_cas.TestCaptureScript -v`
Expected: FAIL — `launcher_component=` not present yet.

- [ ] **Step 3: Edit `capture.sh`**

Change the meta-writing line (currently line 99):

```sh
  { echo "launcher_pkg=$LP"; echo "launcher_uid=$(app_uid "$LP")"; } > "$HS/meta"
```

to also record the component:

```sh
  { echo "launcher_pkg=$LP"; echo "launcher_uid=$(app_uid "$LP")"; \
    echo "launcher_component=$(home_launcher_component)"; } > "$HS/meta"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_cas.TestCaptureScript -v`
Expected: PASS. `[VERIFY on device]`: capture a golden and confirm `homescreen/meta` has a non-empty `launcher_component=`.

- [ ] **Step 5: Commit**

```bash
git add provision/root/capture.sh tests/test_cas.py
git commit -m "feat(capture): record launcher_component in homescreen/meta

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `restore.sh` — `CAS_MODE`, extracted grant pass, set-default-HOME step, gated destructive phases

**Files:**
- Modify: `provision/root/restore.sh` (multiple surgical edits)
- Test: `tests/test_cas.py` (extend `TestRestoreScript` grep-guards; runtime behavior is `[VERIFY on device]`)

**Interfaces:**
- Consumes: `lib-root.sh` helpers from Task 1; `homescreen/meta` `launcher_pkg`/`launcher_component` from Task 2; `@home` flag via `manifest_flag` (Task 4 writes it, but `manifest_flag` already parses any flag).
- Produces: `restore.sh` honors `CAS_MODE=update|full`; sets the default HOME per the determination order; grants All-Files in both modes.

- [ ] **Step 1: Write the failing grep-guard tests**

```python
class TestRestoreScript(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT_DIR / "provision" / "root" / "restore.sh").read_text()

    def test_reads_cas_mode(self):
        self.assertIn('MODE="${CAS_MODE:-full}"', self.text)

    def test_has_set_home_step(self):
        self.assertIn("set_home_launcher", self.text)
        # resolution order: @home flag, else ES-DE, else captured launcher
        self.assertIn("manifest_flag", self.text)
        self.assertIn("org.es_de.frontend", self.text)

    def test_grant_pass_uses_helper_and_runs_outside_destructive_loop(self):
        self.assertIn("grant_all_files", self.text)

    def test_update_mode_gates_destructive_phases(self):
        # the per-app data wipe + favorites-DB wipe must be guarded so update never wipes them
        self.assertIn('[ "$MODE" != update ]', self.text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_cas.TestRestoreScript -v`
Expected: FAIL — none of these strings exist yet.

- [ ] **Step 3a: Read `CAS_MODE` near the top**

After the flag-parsing block (after line 37, where `FGRANTS` etc. are set), add:

```sh
# CAS_MODE=update -> non-destructive refresh: re-apply launcher + grants + settings + hardening only,
# leaving every app's data (saves/state) and SAF grants untouched. Default 'full' = the complete restore.
MODE="${CAS_MODE:-full}"
```

- [ ] **Step 3b: Gate the APK-install loop (step 1) — full only**

Wrap the install loop (lines 47-63, `for pkg in $RPKGS; do … done` that does `pm install`) so it runs only in full mode:

```sh
if [ "$MODE" != update ]; then
for pkg in $RPKGS; do
  # ... existing install body unchanged ...
done
fi
```

- [ ] **Step 3c: Gate the per-app data-restore loop (step 2) and REMOVE the inline grant**

Wrap the per-app data loop (lines 66-119, `for pkg in $RPKGS; do [ -f "$P/$pkg/data.tar" ] || continue … done`) in `if [ "$MODE" != update ]; then … fi`. Inside that loop, **delete** the inline All-Files block (current lines 111-117):

```sh
  # (DELETE these lines — moved to the standalone grant pass in Step 3d)
  if dumpsys package "$pkg" 2>/dev/null | grep -q MANAGE_EXTERNAL_STORAGE; then
    appops set "$pkg" MANAGE_EXTERNAL_STORAGE allow 2>/dev/null
    appops get "$pkg" MANAGE_EXTERNAL_STORAGE 2>/dev/null | grep -q allow || { warn "All-files-access NOT granted: $pkg"; FAIL=$((FAIL+1)); }
  fi
```

Also wrap the binary-serial-fail check (lines 120-121) in the same `[ "$MODE" != update ]` guard (it belongs to the per-app data phase).

- [ ] **Step 3d: Add the standalone grant pass (runs in BOTH modes)**

Immediately after the per-app data phase's closing `fi` (after the binary-serial check), add:

```sh
# All-Files-Access (MANAGE_EXTERNAL_STORAGE) — a special appop `pm install -g` does NOT grant; ES-DE/Eden/
# GameHub/Companion need it. Robust grant + verify, in FULL and UPDATE mode. Absent app: skip (in full an
# install failure was already counted; in update the app simply isn't here yet).
for pkg in $RPKGS; do
  pm path "$pkg" >/dev/null 2>&1 || { [ "$MODE" = update ] && log "all-files: $pkg absent — skip"; continue; }
  if dumpsys package "$pkg" 2>/dev/null | grep -q MANAGE_EXTERNAL_STORAGE; then
    grant_all_files "$pkg" && ok "all-files-access: $pkg" || { warn "All-files-access NOT granted: $pkg"; FAIL=$((FAIL+1)); }
  fi
done
```

- [ ] **Step 3e: Gate the GLOBAL full-only sub-steps**

The global block starts at `if [ -z "${ONLY_PKG:-}" ]; then` (line 124). Inside it, wrap these in `if [ "$MODE" != update ]; then … fi` so Update skips them: **2b** internal dirs (lines 128-136), **2c** ES-DE MediaDirectory (lines 145-160), **3** cores (lines 165-172), **4** SAF urigrants (lines 177-198), **7** OOBE/locale (lines 232-248). Leave **5** settings (203-207) and **6** hardening (211-223) ungated (they already self-gate on `@settings`/`@hardening` and are non-destructive).

- [ ] **Step 3f: Add the set-default-HOME step (both modes) and guard the favorites-DB clone**

In step 8 (the `FHOME`/homescreen block, lines 258-301), insert the set-HOME logic right after `FHOME` is resolved (after line 259) and BEFORE the `HS="$P/homescreen"` favorites-DB logic:

```sh
# Set the default HOME launcher (the "game launcher"). Determination order (NEVER the OEM launcher):
#   @home <pkg>  ->  org.es_de.frontend if in the manifest  ->  captured launcher from homescreen/meta.
if [ "$FHOME" = on ]; then
  HOME_PKG=""; HOME_COMP=""
  if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then HOME_PKG="$(manifest_flag "$CAS_MANIFEST" home)"; fi
  if [ -z "$HOME_PKG" ] && echo "$RPKGS" | grep -qw org.es_de.frontend; then HOME_PKG="org.es_de.frontend"; fi
  if [ -z "$HOME_PKG" ]; then
    HOME_PKG="$(sed -n 's/^launcher_pkg=//p' "$P/homescreen/meta" 2>/dev/null)"
    HOME_COMP="$(sed -n 's/^launcher_component=//p' "$P/homescreen/meta" 2>/dev/null)"
  fi
  if [ -z "$HOME_PKG" ]; then
    log "default launcher: none resolved (no @home / ES-DE / captured launcher) — skip"
  elif ! pm path "$HOME_PKG" >/dev/null 2>&1; then
    warn "default launcher: $HOME_PKG not installed on this unit — skip"
  elif set_home_launcher "$HOME_PKG" "$HOME_COMP"; then
    ok "default launcher set: $HOME_PKG"
  else
    warn "default launcher NOT set: $HOME_PKG (verify it declares CATEGORY_HOME)"   # WARN: additive
  fi
fi
```

Then guard the favorites-DB clone so it is full-only AND skipped when the launcher is itself a managed app (its data already arrived via per-app restore). Change the favorites-DB branch condition. Currently:

```sh
HS="$P/homescreen"
if [ "$FHOME" != on ]; then
  log "homescreen: skipped (@homescreen off)"
elif [ ! -f "$HS/launcher_data.tar" ]; then
```

to:

```sh
HS="$P/homescreen"
LP_META="$(sed -n 's/^launcher_pkg=//p' "$HS/meta" 2>/dev/null)"
if [ "$FHOME" != on ]; then
  log "homescreen: skipped (@homescreen off)"
elif [ "$MODE" = update ]; then
  log "homescreen layout: skipped (update mode — only the default launcher is (re)set)"
elif [ -n "$LP_META" ] && echo "$RPKGS" | grep -qw "$LP_META"; then
  log "homescreen layout: launcher $LP_META is a managed app — restored via its app data; skip favorites clone"
elif [ ! -f "$HS/launcher_data.tar" ]; then
```

(the rest of the favorites-DB branch is unchanged.)

- [ ] **Step 4: Run the grep-guard tests**

Run: `python -m unittest tests.test_cas.TestRestoreScript -v`
Expected: PASS. Then run the full suite to ensure nothing regressed:
Run: `python -m unittest tests.test_cas -v` → Expected: OK (all green).
`[VERIFY on device]`: on a MANGMI AIR X, run a full Download and confirm ES-DE becomes HOME + Companion has All-Files-Access; then run an Update on a used unit and confirm saves/state survive and the launcher/grant still apply.

- [ ] **Step 5: Commit**

```bash
git add provision/root/restore.sh tests/test_cas.py
git commit -m "feat(restore): CAS_MODE=update, set-default-HOME step, robust grant pass

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Preserve `@home` (value-flags) through the GUI "Save selection"

**Files:**
- Modify: `cas/gui.py` (`save_manifest`, ~lines 995-1003)
- Test: `tests/test_cas.py` (new `TestManifestFlags` — exercises `profiles` round-trip, which is what the GUI fix relies on)

**Interfaces:**
- Consumes: `P.Profile.flags()` / `P.manifest_flags` (already parse `@home <pkg>`).
- Produces: editing only the four checkbox flags no longer drops `@home`.

- [ ] **Step 1: Write the failing test**

```python
class TestManifestFlags(unittest.TestCase):
    def test_save_manifest_preserves_home_flag(self):
        # Simulates the GUI save path: start from existing flags, overlay the 4 checkbox flags.
        with tempfile.TemporaryDirectory() as t:
            mp = pathlib.Path(t) / "manifest"
            P.save_manifest(mp, ["org.es_de.frontend"],
                            {"home": "org.es_de.frontend", "settings": "on"}, header="# x")
            existing = dict(P.manifest_flags(mp))             # what the GUI must start from
            existing.update({"settings": "off", "hardening": "on",
                             "grants": "on", "homescreen": "on"})
            P.save_manifest(mp, ["org.es_de.frontend"], existing, header="# x")
            self.assertEqual(P.manifest_flags(mp).get("home"), "org.es_de.frontend")
            self.assertEqual(P.manifest_flags(mp).get("settings"), "off")
```

- [ ] **Step 2: Run test to verify it fails**

It will actually PASS at the `profiles` layer (round-trip already works) — the real defect is in `gui.save_manifest`, which rebuilds `flags` from only `flag_vars`. Confirm by reading `gui.save_manifest`; the test documents the required GUI behavior. Run:
Run: `python -m unittest tests.test_cas.TestManifestFlags -v`
Expected: PASS (this locks the contract the GUI must follow).

- [ ] **Step 3: Fix `gui.save_manifest` to start from existing flags**

In `cas/gui.py`, change:

```python
        pkgs = [p for p, v in self.pkg_vars.items() if v.get()]
        flags = {fl: ("on" if v.get() else "off") for fl, v in self.flag_vars.items()}
        P.save_manifest(prof.manifest_path, pkgs, flags, header=f"# {name}")
```

to:

```python
        pkgs = [p for p, v in self.pkg_vars.items() if v.get()]
        # Start from the manifest's existing flags so value-flags like @home (the default launcher) survive;
        # overlay only the four behaviour checkboxes the GUI actually edits.
        flags = dict(prof.flags())
        flags.update({fl: ("on" if v.get() else "off") for fl, v in self.flag_vars.items()})
        P.save_manifest(prof.manifest_path, pkgs, flags, header=f"# {name}")
```

- [ ] **Step 4: Run the test + full suite**

Run: `python -m unittest tests.test_cas.TestManifestFlags -v` → PASS
Run: `python -m unittest tests.test_cas -v` → OK (all green)

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py tests/test_cas.py
git commit -m "fix(gui): preserve @home (and other value-flags) on Save selection

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `provision.py` — `mode` parameter, reduced payload, `CAS_MODE` env, SD-guard relax

**Files:**
- Modify: `cas/provision.py` (`provision`, lines 204-311)
- Test: `tests/test_cas.py` (extend `TestProvision`)

**Interfaces:**
- Consumes: `restore.sh` `CAS_MODE` from Task 3.
- Produces: `provision(adb, profile, log=print, dry_push=False, es_media_src=None, mode="full") -> bool`. In `update`: no SD guard; pushes only `manifest`, `pkglist.txt`, `global.meta`, `settings/`, `homescreen/meta`, `restore.sh`, `lib-root.sh`; sends `CAS_MODE=update`.

- [ ] **Step 1: Write the failing tests**

```python
    def test_provision_update_sets_cas_mode_and_skips_app_payload(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            # give the payload a homescreen/meta so update has something to push
            hs = prof.payload / "homescreen"; hs.mkdir()
            (hs / "meta").write_text("launcher_pkg=org.es_de.frontend\n")
            r = FakeRunner()
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, mode="update")  # non-dry
            self.assertTrue(ok)
            cmds = "\n".join(r.cmds())
            self.assertIn("CAS_MODE=update", cmds)
            # per-app payload dirs / data tars must NOT be pushed in update mode
            self.assertNotIn("data.tar", cmds)
            pushes = [c for c in r.calls if "push" in c]
            self.assertFalse(any("org.es_de.frontend" in c[-2] for c in pushes
                                 if c[-2].rstrip("/").endswith("org.es_de.frontend")))

    def test_provision_update_does_not_require_sd(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            (prof.payload / "homescreen").mkdir(); (prof.payload / "homescreen" / "meta").write_text("launcher_pkg=org.es_de.frontend\n")
            r = FakeRunner(sd=False)                         # no SD card present
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, mode="update")
            self.assertTrue(ok)                              # update doesn't need the SD

    def test_provision_full_still_requires_sd(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner(sd=False)
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)  # mode defaults to full
            self.assertFalse(ok)

    def test_provision_full_has_no_update_mode_env(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner()
            PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)
            self.assertNotIn("CAS_MODE=update", "\n".join(r.cmds()))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_cas.TestProvision -v`
Expected: FAIL — `provision()` has no `mode` kwarg (TypeError) / no `CAS_MODE`.

- [ ] **Step 3: Add `mode` to `provision()`**

Change the signature (line 204):

```python
def provision(adb, profile, log=print, dry_push=False, es_media_src=None, mode="full"):
```

In the guard block, make the SD requirement full-only — change (lines 222-225):

```python
    if not adb.has_sd():
        log("REFUSING: no SD card detected. The SD carries ROMs + the volume serial; provisioning "
            "without it produces a unit with no games and risks a bad serial rewrite. Insert it and retry.")
        return False
```

to:

```python
    if mode != "update" and not adb.has_sd():
        log("REFUSING: no SD card detected. The SD carries ROMs + the volume serial; provisioning "
            "without it produces a unit with no games and risks a bad serial rewrite. Insert it and retry.")
        return False
```

- [ ] **Step 4: Branch the push section on `mode`**

Replace the push body inside `if not dry_push:` (lines 243-283) so update pushes only the small config set. Locate:

```python
        for i, pkg in enumerate(pkgs, 1):                  # only the manifest's app modules
            log(f"pushing module {i}/{len(pkgs)}: {pkg}")
            if not push(pay / pkg, f"{DEV}/payload/"):
                return False
        for f in ("global.meta", "pkglist.txt", "urigrants.xml"):
            if (pay / f).exists() and not push(pay / f, f"{DEV}/payload/"):
                return False
        if (pay / "settings").exists() and not push(pay / "settings", f"{DEV}/payload/"):
            return False
        if (pay / "homescreen").exists() and not push(pay / "homescreen", f"{DEV}/payload/"):
            return False                                   # launcher layout + wallpaper + widget map (optional)
        for pkg in pkgs:                                   # internal dirs for included apps only
            d = P.internal_for(pkg)
            tar = pay / f"internal_{d}.tar" if d else None
            if tar and tar.exists() and not push(tar, f"{DEV}/payload/"):
                return False
        for f in (RESTORE, LIBROOT):
            if not push(f, f"{DEV}/"):
                return False
        if not push(profile.manifest_path, f"{DEV}/manifest"):
            return False
        if push_cores:                                     # the full curated core set, FROM THE PC
            log(f"pushing RetroArch cores from PC ({sum(1 for _ in CORES_SRC.glob('*.so'))} cores)...")
            if not push(CORES_SRC, f"{DEV}/cores"):
                return False
```

and replace with:

```python
        if mode == "update":
            # Reduced payload: only what the non-destructive update needs (launcher + grants + settings +
            # hardening). No APKs / data tars / internal tars — fast, and it never carries per-app data.
            for f in ("global.meta", "pkglist.txt"):
                if (pay / f).exists() and not push(pay / f, f"{DEV}/payload/"):
                    return False
            if (pay / "settings").exists() and not push(pay / "settings", f"{DEV}/payload/"):
                return False
            if (pay / "homescreen" / "meta").exists():     # only the meta (launcher pkg/component), not the tars
                adb.shell(f"mkdir -p {DEV}/payload/homescreen")
                if not push(pay / "homescreen" / "meta", f"{DEV}/payload/homescreen/meta"):
                    return False
            for f in (RESTORE, LIBROOT):
                if not push(f, f"{DEV}/"):
                    return False
            if not push(profile.manifest_path, f"{DEV}/manifest"):
                return False
        else:
            for i, pkg in enumerate(pkgs, 1):              # only the manifest's app modules
                log(f"pushing module {i}/{len(pkgs)}: {pkg}")
                if not push(pay / pkg, f"{DEV}/payload/"):
                    return False
            for f in ("global.meta", "pkglist.txt", "urigrants.xml"):
                if (pay / f).exists() and not push(pay / f, f"{DEV}/payload/"):
                    return False
            if (pay / "settings").exists() and not push(pay / "settings", f"{DEV}/payload/"):
                return False
            if (pay / "homescreen").exists() and not push(pay / "homescreen", f"{DEV}/payload/"):
                return False                               # launcher layout + wallpaper + widget map (optional)
            for pkg in pkgs:                              # internal dirs for included apps only
                d = P.internal_for(pkg)
                tar = pay / f"internal_{d}.tar" if d else None
                if tar and tar.exists() and not push(tar, f"{DEV}/payload/"):
                    return False
            for f in (RESTORE, LIBROOT):
                if not push(f, f"{DEV}/"):
                    return False
            if not push(profile.manifest_path, f"{DEV}/manifest"):
                return False
            if push_cores:                                # the full curated core set, FROM THE PC
                log(f"pushing RetroArch cores from PC ({sum(1 for _ in CORES_SRC.glob('*.so'))} cores)...")
                if not push(CORES_SRC, f"{DEV}/cores"):
                    return False
```

- [ ] **Step 5: Pass `CAS_MODE` and keep ES box-art full-only**

Change the env/restore invocation (lines 285-292). Locate:

```python
    cores_env = f"CAS_CORES={DEV}/cores " if (push_cores and not dry_push) else ""
    es_mode = "internal" if es_media_src else "sd"
    es_env = f"CAS_ES_MEDIA={es_mode} " if "org.es_de.frontend" in pkgs else ""
    log("running restore (installs apps, restores data/keys/BIOS/cores/grants/settings)...")
    rc = adb.su_stream(                                      # stream each [ok]/[warn] line LIVE to the log
        f"{es_env}{cores_env}CAS_PAYLOAD={DEV}/payload CAS_MANIFEST={DEV}/manifest sh {DEV}/restore.sh", log)
```

replace with:

```python
    cores_env = f"CAS_CORES={DEV}/cores " if (push_cores and not dry_push and mode != "update") else ""
    es_mode = "internal" if es_media_src else "sd"
    es_env = f"CAS_ES_MEDIA={es_mode} " if ("org.es_de.frontend" in pkgs and mode != "update") else ""
    mode_env = "CAS_MODE=update " if mode == "update" else ""
    log("running restore (update: launcher + grants + settings)..." if mode == "update"
        else "running restore (installs apps, restores data/keys/BIOS/cores/grants/settings)...")
    rc = adb.su_stream(                                      # stream each [ok]/[warn] line LIVE to the log
        f"{mode_env}{es_env}{cores_env}CAS_PAYLOAD={DEV}/payload CAS_MANIFEST={DEV}/manifest sh {DEV}/restore.sh", log)
```

Also make the post-restore ES box-art push full-only — change (line 303):

```python
    if not dry_push and "org.es_de.frontend" in pkgs and es_mode == "internal":
```

to:

```python
    if not dry_push and mode != "update" and "org.es_de.frontend" in pkgs and es_mode == "internal":
```

(The Companion APK refresh at line 305-306 stays as-is — it runs in both modes when the Companion is in the manifest.)

- [ ] **Step 6: Run the tests + full suite**

Run: `python -m unittest tests.test_cas.TestProvision -v` → PASS
Run: `python -m unittest tests.test_cas -v` → OK (all green)

- [ ] **Step 7: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(provision): mode=update — reduced payload, CAS_MODE env, no SD guard

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `provision_all` mode passthrough + GUI "Update" button

**Files:**
- Modify: `cas/provision.py` (`provision_all`, lines 350-391)
- Modify: `cas/gui.py` (action-button list ~line 675-693; `_run_batch` ~line 1204-1247; add `update_selected`)
- Test: `tests/test_cas.py` (extend `TestProvision`; grep-guard for the GUI button)

**Interfaces:**
- Consumes: `provision(..., mode=...)` from Task 5.
- Produces: `provision_all(make_adb, devices, root="profiles", log=print, profile=None, profile_map=None, parallel=True, es_media_src=None, mode="full")`; GUI `_run_batch("update", ...)` and an `update_selected()` handler bound to a new "Update" button.

- [ ] **Step 1: Write the failing tests**

```python
    def test_provision_all_passes_update_mode(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            (prof.payload / "homescreen").mkdir(); (prof.payload / "homescreen" / "meta").write_text("launcher_pkg=org.es_de.frontend\n")
            r = FakeRunner()
            res = PV.provision_all(lambda s: Adb(runner=r), [("ABC123", "device")],
                                   root=t, log=lambda m: None, profile=prof, parallel=False, mode="update")
            self.assertEqual(res["ABC123"][0], "ok")
            self.assertIn("CAS_MODE=update", "\n".join(r.cmds()))


class TestGuiUpdateButton(unittest.TestCase):
    def test_gui_has_update_button_and_handler(self):
        text = (ROOT_DIR / "cas" / "gui.py").read_text()
        self.assertIn("update_selected", text)
        self.assertIn('"update"', text)            # _run_batch kind
        self.assertIn("Update", text)              # button label
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_cas.TestProvision.test_provision_all_passes_update_mode tests.test_cas.TestGuiUpdateButton -v`
Expected: FAIL — `provision_all` has no `mode`; gui.py has no update button.

- [ ] **Step 3: Add `mode` to `provision_all`**

Change the signature (line 350-351):

```python
def provision_all(make_adb, devices, root="profiles", log=print, profile=None, profile_map=None,
                  parallel=True, es_media_src=None, mode="full"):
```

and the inner `provision(...)` call (line 379):

```python
            ok = provision(adb, prof, log=_wlog, es_media_src=es_media_src, mode=mode)
```

- [ ] **Step 4: Add the GUI "Update" button**

In the action-button tuple list (after the `② Download` entry, before `③ Lock for shipping`, ~line 688), insert:

```python
            ("Update (no wipe)", self.update_selected,
             "Re-apply each device's profile SETTINGS, default launcher, and file-access grants to an "
             "ALREADY-set-up unit — WITHOUT wiping app data (saves/state) or SAF grants. Use this to push a "
             "golden change (e.g. the default launcher) to units in the field. Runs on the selected "
             "device(s), or ALL connected if the toggle is on, IN PARALLEL."),
```

- [ ] **Step 5: Teach `_run_batch` the "update" kind and add the handler**

In `_run_batch` (lines 1204-1247), extend the `verb`, `extra`, `kind=="download"` dispatch, and `label` dicts to include `update`. Change the `verb` dict (line 1210):

```python
            verb = {"download": "Download to", "root": "Root", "lock": "Seal (lock)",
                    "update": "Update"}[kind]
```

the `extra` dict (lines 1212-1215) — add an `update` key:

```python
            extra = {"root": "\n\nBootloaders must be UNLOCKED; each device reboots a couple of times.",
                     "lock": "\n\nAssumes each unit is VERIFIED. Hides Developer options, un-roots, and "
                             "disables USB debugging. The golden is skipped.",
                     "download": "",
                     "update": "\n\nNon-destructive: re-applies settings, default launcher, and file-access "
                               "grants only. App data (saves/state) is preserved."}[kind]
```

the dispatch (line 1224, `if kind == "download":`) — handle download and update together:

```python
            if kind in ("download", "update"):
                res = PV.provision_all(lambda s: Adb(serial=s, adb=self.adb_bin), devs,
                                       root=self.profiles_root, log=self.log, profile_map=pm,
                                       es_media_src=config.es_media_src(),
                                       mode=("update" if kind == "update" else "full"))
```

and the `label` dict (line 1245):

```python
        label = {"download": "Downloading", "root": "Rooting", "lock": "Locking",
                 "update": "Updating"}[kind]
```

Then add the handler next to `provision_selected` (after line 1252):

```python
    def update_selected(self):
        t = self._action_targets()
        if t:
            self._run_batch("update", t)
```

- [ ] **Step 6: Run the tests + full suite**

Run: `python -m unittest tests.test_cas.TestProvision.test_provision_all_passes_update_mode tests.test_cas.TestGuiUpdateButton -v` → PASS
Run: `python -m unittest tests.test_cas -v` → OK (all green)
`[VERIFY on device]`: confirm the GUI "Update" button runs on selected/all and that the log shows the update restore path.

- [ ] **Step 7: Commit**

```bash
git add cas/provision.py cas/gui.py tests/test_cas.py
git commit -m "feat(gui): Update button — non-destructive provision_all mode=update

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- §3 determination order (`@home` → ES-DE → captured) — Task 3 Step 3f; `@home` preserved — Task 4. ✓
- §4.1 helpers — Task 1. ✓
- §4.2 capture `launcher_component` — Task 2. ✓
- §4.3 `CAS_MODE`, grant extraction, set-HOME, favorites guard, destructive gating — Task 3. ✓
- §4.4 provision `mode`, reduced payload, SD relax, `CAS_MODE` env — Task 5. ✓
- §4.5 GUI Update button + `save_manifest` preserve — Task 6 + Task 4. ✓
- §6 update run/skip set — Task 3 Steps 3b-3f (settings/hardening ungated; install/data/internal/ES/cores/SAF/OOBE gated; favorites guarded). ✓
- §7 FAIL vs WARN — Task 3 Step 3d (grant FAIL) + Step 3f (launcher WARN). ✓
- §8 tests — Tasks 1-6 each add tests; 106 baseline asserted green. ✓

**Placeholder scan:** none — every step shows exact code/commands.

**Type consistency:** `set_home_launcher <pkg> [component]`, `grant_all_files <pkg>`, `home_launcher_component` used identically across Tasks 1/3; `provision(..., mode=...)` and `provision_all(..., mode=...)` consistent across Tasks 5/6; `_run_batch("update", ...)`/`update_selected` consistent in Task 6.
