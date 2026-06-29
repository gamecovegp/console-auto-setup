# Two-stage app/config selection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give CAS two independent per-profile selections — what to capture into the golden (Save, chosen from the connected device's apps) and what to deploy onto a unit (Download, chosen from the golden's apps) — each honoring APK and Config independently, with emulator/all defaults and select/deselect-all.

**Architecture:** Add a second per-profile manifest `capture-manifest` (Save) beside the existing `manifest` (deploy). `capture_to_pc()` pushes and passes the capture selection to the already-axis-aware `capture.sh`. `restore.sh` gains per-app axis enforcement. The GUI grows two labeled lists — a device-scanned Save list (emulators + game launcher pre-checked) and a golden Download list (all on) — each with Select/Deselect all. Launcher rows map to the existing `@gamelauncher`/`@homescreen` flags.

**Tech Stack:** Python 3 stdlib + Tkinter (`cas/`), POSIX sh (`provision/root/`), `unittest` + bash smoke tests.

## Global Constraints

- Python stdlib only; on-device scripts POSIX `sh`; no new dependency.
- Two manifests: **`capture-manifest`** = Save selection; **`manifest`** = deploy selection (existing; `provision()` already reads it — do not rename).
- Tokened format unchanged: `pkg` / `pkg apk` / `pkg config` + `@flags`. Bare line = both axes (back-compat).
- **Launcher rows are not package lines:** the game launcher → `@gamelauncher` flag, the HOME launcher → `@homescreen` flag (the launcher is deliberately excluded from the per-app capture loop).
- **Save defaults:** detected emulators (`EMULATOR_PKGS`) + the game launcher checked; everything else off. **Download defaults:** all golden apps on.
- **Back-compat:** no `capture-manifest` → `capture_to_pc` captures-all as today; a manifest app with no axis tokens → both axes (unchanged).
- `capture.sh` is already axis-aware (honors `CAS_MANIFEST`) — DO NOT modify it.

---

## Phase A — Save side

### Task A1: `EMULATOR_PKGS` + `Profile` capture-manifest accessors

**Files:**
- Modify: `cas/profiles.py` (add `EMULATOR_PKGS` near the top constants; add methods to `Profile`)
- Test: `tests/test_cas.py` (`TestProfiles` or `TestManifestAxes`)

**Interfaces:**
- Produces: `EMULATOR_PKGS: set[str]`; `Profile.capture_pkgs() -> list[str]`, `Profile.capture_axes() -> dict[str,(bool,bool)]`, `Profile.capture_flags() -> dict[str,str]` — all reading `<profile>/capture-manifest` via the existing parsers.

- [ ] **Step 1: Write the failing test**

```python
    def test_capture_manifest_accessors_and_emulator_set(self):
        import tempfile, pathlib
        from cas import profiles as P
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "profile.meta").write_text("frontend=es-de\n")
        (d / "capture-manifest").write_text("# cap\ncom.foo\nbar.app config\n@gamelauncher on\n")
        prof = P.Profile(d)
        self.assertEqual(prof.capture_pkgs(), ["com.foo", "bar.app"])
        self.assertEqual(prof.capture_axes(), {"com.foo": (True, True), "bar.app": (False, True)})
        self.assertEqual(prof.capture_flags().get("gamelauncher"), "on")
        self.assertIn("com.retroarch.aarch64", P.EMULATOR_PKGS)        # the known emulator set exists
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_cas.TestProfiles.test_capture_manifest_accessors_and_emulator_set -v`
Expected: FAIL — `AttributeError: 'Profile' object has no attribute 'capture_pkgs'` (and `EMULATOR_PKGS` missing).

- [ ] **Step 3: Implement** — in `cas/profiles.py`

Add the constant near the other module constants (after the imports / `_CAP_MIN_GB` area):

```python
# Emulator/frontend packages = the GAMING payload, auto-checked in the Save (capture) list.
# Keep in sync with provision/root/lib-root.sh PKGS.
EMULATOR_PKGS = {
    "dev.eden.eden_emulator", "com.retroarch.aarch64", "org.dolphinemu.dolphinemu",
    "com.flycast.emulator", "com.github.stenzek.duckstation", "xyz.aethersx2.android",
    "me.magnum.melonds.nightly", "org.citra.emu", "org.ppsspp.ppsspp",
    "org.mupen64plusae.v3.fzurita", "org.es_de.frontend", "gamehub.lite",
}
```

Add to `class Profile` (beside `pkgs()`/`axes()`/`flags()`):

```python
    @property
    def capture_manifest_path(self):
        return self.path / "capture-manifest"

    def capture_pkgs(self):
        return manifest_pkgs(self.capture_manifest_path)

    def capture_axes(self):
        return manifest_axes(self.capture_manifest_path)

    def capture_flags(self):
        return manifest_flags(self.capture_manifest_path)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_cas.TestProfiles.test_capture_manifest_accessors_and_emulator_set -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(profiles): capture-manifest accessors + EMULATOR_PKGS set"
```

---

### Task A2: `capture_to_pc()` passes the capture selection to `capture.sh`

**Files:**
- Modify: `cas/provision.py` (`capture_to_pc`, the script-push block ~525-529 and the `su_stream` call ~546)
- Test: `tests/test_cas.py` (`TestProvision`)

**Interfaces:**
- Consumes: `capture-manifest` at `<root>/<name>/capture-manifest`.
- Produces: when that file exists, capture runs with `CAS_MANIFEST=/data/local/tmp/cas_scripts/capture-manifest`; when absent, runs as today (capture-all). Back-compat preserved.

- [ ] **Step 1: Write the failing tests** — add to `TestProvision`

```python
    def test_capture_to_pc_passes_capture_manifest_when_present(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            pdir = pathlib.Path(t) / "newprof"; pdir.mkdir()
            (pdir / "capture-manifest").write_text("# cap\ncom.foo\n")
            PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t, log=lambda m: None, dry_pull=True)
            joined = "\n".join(r.cmds())
            self.assertIn("CAS_MANIFEST=/data/local/tmp/cas_scripts/capture-manifest", joined)

    def test_capture_to_pc_no_manifest_captures_all(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t, log=lambda m: None, dry_pull=True)
            self.assertNotIn("CAS_MANIFEST=", "\n".join(r.cmds()))   # back-compat: capture-all
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_cas.TestProvision.test_capture_to_pc_passes_capture_manifest_when_present tests.test_cas.TestProvision.test_capture_to_pc_no_manifest_captures_all -v`
Expected: `test_capture_to_pc_passes_capture_manifest_when_present` FAILS (no `CAS_MANIFEST=` today); the no-manifest test passes already.

- [ ] **Step 3: Implement** — in `cas/provision.py` `capture_to_pc`

Inside `if not dry_pull:`, after the `CAPTURE`/`LIBROOT` push, add the capture-manifest push:

```python
        cap_man = pdir / "capture-manifest"
        if cap_man.exists() and not adb.push(cap_man, "/data/local/tmp/cas_scripts/capture-manifest"):
            log("failed to push capture-manifest — aborting (existing profile untouched).")
            return False
```

Replace the capture run line:

```python
    rc = adb.su_stream(f"CAS_OUT={TMPCAP} sh /data/local/tmp/cas_scripts/capture.sh", log)
```

with (compute the manifest env from the same `cap_man`, defined just above the `if not dry_pull` so it's in scope for the run line):

```python
    cap_man = pdir / "capture-manifest"
    man_env = "CAS_MANIFEST=/data/local/tmp/cas_scripts/capture-manifest " if cap_man.exists() else ""
    rc = adb.su_stream(f"{man_env}CAS_OUT={TMPCAP} sh /data/local/tmp/cas_scripts/capture.sh", log)
```

(Define `cap_man = pdir / "capture-manifest"` once near the top of the function so both the push block and the run line use it; remove the duplicate definition inside the `if`.)

- [ ] **Step 4: Run to verify they pass + existing capture test still green**

Run: `python -m unittest tests.test_cas.TestProvision -v`
Expected: PASS, including the existing `test_capture_to_pc_invokes_capture`.

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(capture): capture_to_pc passes capture-manifest selection to capture.sh"
```

---

### Task A3: GUI — device-scanned Save list (emulators + game launcher pre-checked)

**Files:**
- Modify: `cas/gui.py` (`on_select_profile` and helpers; add `_scan_device_apps`, `_save_capture_manifest`, select/deselect handlers)
- Test: `tests/test_cas.py` (a pure-logic test of the default-selection helper; Tk widget construction is not unit-tested here)

**Interfaces:**
- Consumes: `Adb.shell("pm list packages -3")`, `_selected_serial()`, `P.EMULATOR_PKGS`, `P.save_manifest`, `home_launcher`/game-launcher detection (run on device via `adb.shell`/`su`).
- Produces: a Save list whose default-checked set = `EMULATOR_PKGS ∩ device apps` + the detected game launcher; `_save_capture_manifest()` writes `capture-manifest` (normal apps as tokened lines, game/HOME launcher as `@gamelauncher`/`@homescreen`).

- [ ] **Step 1: Write the failing test** — a pure helper computing the default-checked set (keep GUI logic testable). Add to `tests/test_cas.py`:

```python
    def test_default_capture_checks_emulators_and_game_launcher(self):
        from cas import profiles as P
        device_apps = ["com.retroarch.aarch64", "org.ppsspp.ppsspp", "com.random.note", "com.foo.bar"]
        checked = P.default_capture_selection(device_apps, game_launcher="com.handheld.launcher")
        # emulators + game launcher on; unrelated apps off
        self.assertEqual(checked["com.retroarch.aarch64"], (True, True))
        self.assertEqual(checked["org.ppsspp.ppsspp"], (True, True))
        self.assertEqual(checked["com.random.note"], (False, False))
        self.assertEqual(checked["com.handheld.launcher"], (False, True))   # launcher = config-only
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_cas.TestProfiles.test_default_capture_checks_emulators_and_game_launcher -v`
Expected: FAIL — `AttributeError: module 'cas.profiles' has no attribute 'default_capture_selection'`.

- [ ] **Step 3: Implement the helper** — in `cas/profiles.py`

```python
def default_capture_selection(device_apps, game_launcher=None, home_launcher=None):
    """The default Save-list check state: {pkg: (apk_on, config_on)}. Emulators (EMULATOR_PKGS) -> both axes;
    the game/HOME launcher -> config-only (APK is system firmware); every other device app -> off."""
    sel = {}
    for pkg in device_apps:
        on = pkg in EMULATOR_PKGS
        sel[pkg] = (on, on)
    for lp in (game_launcher, home_launcher):
        if lp:
            sel[lp] = (False, lp == game_launcher)   # game launcher config-on by default; HOME off
    return sel
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m unittest tests.test_cas.TestProfiles.test_default_capture_checks_emulators_and_game_launcher -v`
Expected: PASS.

- [ ] **Step 5: Commit the helper**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(profiles): default_capture_selection (emulators+game-launcher default-on)"
```

- [ ] **Step 6: Wire the GUI** — in `cas/gui.py`

Add a device-scan helper on the App class:

```python
    def _scan_device_apps(self, serial):
        """Third-party packages on the connected device (pm list -3), sorted. [] if no device/scan fails."""
        if not serial:
            return []
        rc, out, _ = Adb(serial=serial, adb=self.adb_bin).shell("pm list packages -3")
        if rc != 0:
            return []
        return sorted(l.split("package:", 1)[1].strip()
                      for l in out.splitlines() if l.startswith("package:"))

    def _detect_device_launchers(self, serial):
        """(game_launcher, home_launcher) on the device, or (None, None). Best-effort via su."""
        if not serial:
            return (None, None)
        a = Adb(serial=serial, adb=self.adb_bin)
        def _one(cmd):
            rc, out, _ = a.su(f". /data/local/tmp/cas_scripts/lib-root.sh 2>/dev/null; {cmd}")
            line = (out or "").strip().splitlines()
            return line[-1].strip() if rc == 0 and line else None
        return (_one("game_launcher"), _one("home_launcher"))
```

Restructure `on_select_profile` so the manifest area shows TWO labeled lists. Build each with a shared row-builder; keep the existing `_app_label`, `_tip`, and two-axis pattern. The Save list uses the device scan + `default_capture_selection` for its initial check state (overlaid by a saved `capture-manifest` if present via `prof.capture_axes()`); the Download list uses `prof.all_pkgs()` + `prof.axes()` defaulting all-on. Give each list its own `Select all` / `Deselect all` buttons (replace the single `selall_var` toggle with two buttons per list calling a `_set_all(vars_dict, value)` helper).

Add the capture-manifest writer (mirrors `save_manifest`, routing launcher rows to flags):

```python
    def _save_capture_manifest(self):
        prof = P.Profile(pathlib.Path(self.profiles_root) / self.prof_var.get())
        axes = {p: (a.get(), c.get()) for p, (a, c) in self.cap_vars.items()}
        flags = dict(prof.capture_flags())
        # launcher rows -> behavior flags, NOT package lines
        gl, hl = self._cap_game_launcher, self._cap_home_launcher
        pkgs = []
        for p, (a, c) in axes.items():
            if p == gl:
                flags["gamelauncher"] = "on" if c else "off"; continue
            if p == hl:
                flags["homescreen"] = "on" if c else "off"; continue
            if a or c:
                pkgs.append(p)
        P.save_manifest(prof.capture_manifest_path, pkgs, flags,
                        header=f"# {prof.name} capture", axes={p: axes[p] for p in pkgs})
        self.log(f"saved capture selection for {prof.name}: {len(pkgs)} app(s) + flags={flags}")
```

(`self.cap_vars` is the Save list's `{pkg: (apk_var, cfg_var)}`; `self._cap_game_launcher`/`_cap_home_launcher` are stashed during the scan. The Download list keeps the existing `self.pkg_vars` + `save_manifest`.)

- [ ] **Step 7: Smoke-run the GUI logic test + full suite**

Run: `python -m unittest discover -s tests -q`
Expected: OK (new helper test included; no regressions). GUI widget wiring is verified on-device/manually (Tk isn't unit-tested here).

- [ ] **Step 8: Commit**

```bash
git add cas/gui.py
git commit -m "feat(gui): device-scanned Save list (emulators+game-launcher default), capture-manifest writer, select/deselect-all"
```

---

## Phase B — Download side

### Task B1: `restore.sh` honors per-app APK/Config axes

**Files:**
- Modify: `provision/root/lib-root.sh` (add `manifest_wants` helper), `provision/root/restore.sh` (gate install + data restore on the axes)
- Test: `tests/test_manifest_axes.sh` (extend) — pure-text, no device

**Interfaces:**
- Produces: `manifest_wants <manifest> <pkg> <apk|config>` → rc 0 if that axis is on for the pkg (bare line = both on), else 1.

- [ ] **Step 1: Write the failing test** — append to `tests/test_manifest_axes.sh` before its final PASS line

```bash
wants(){ manifest_wants "$tmp/m" "$1" "$2" && echo yes || echo no; }
[ "$(wants com.foo apk)" = yes ]    || { echo "FAIL wants(foo,apk)"; fail=1; }     # bare = both
[ "$(wants com.foo config)" = yes ] || { echo "FAIL wants(foo,config)"; fail=1; }
[ "$(wants com.bar apk)" = yes ]    || { echo "FAIL wants(bar,apk)"; fail=1; }     # 'com.bar apk'
[ "$(wants com.bar config)" = no ]  || { echo "FAIL wants(bar,config)"; fail=1; }
[ "$(wants xyz.aethersx2.android config)" = yes ] || { echo "FAIL wants(aeth,config)"; fail=1; }
[ "$(wants xyz.aethersx2.android apk)" = no ]     || { echo "FAIL wants(aeth,apk)"; fail=1; }
```

(The existing `$tmp/m` in that test already has `com.foo` bare, `com.bar apk`, `xyz.aethersx2.android config`.)

- [ ] **Step 2: Run to verify it fails**

Run: `bash tests/test_manifest_axes.sh`
Expected: FAIL — `manifest_wants: command not found` / non-zero exit.

- [ ] **Step 3: Implement `manifest_wants`** — in `provision/root/lib-root.sh` after `manifest_axes`

```sh
# manifest_wants <manifest> <pkg> <apk|config> — rc 0 if that capture/deploy axis is ON for the pkg
# (bare line = both axes on; pkg absent = off). Pure text; used by restore.sh to gate install/data.
manifest_wants(){
  _mw="$(manifest_axes "$1" "$2")"          # "apk config" | "apk" | "config" | ""
  case " $_mw " in *" $3 "*) return 0 ;; *) return 1 ;; esac
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `bash tests/test_manifest_axes.sh`
Expected: `PASS: manifest_axes`.

- [ ] **Step 5: Gate restore.sh on the axes**

In `provision/root/restore.sh`, in the **APK install loop** (the `for pkg in $RPKGS` at ~line 47), add at the top of the loop body (before the `set -- "$P/$pkg/apk/"*.apk` line):

```sh
  if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ] && ! manifest_wants "$CAS_MANIFEST" "$pkg" apk; then
    log "deploy: $pkg APK-axis off — skipping install"; continue
  fi
```

In the **data restore loop** (the `for pkg in $RPKGS` at ~line 81), add at the top (before `[ -f "$P/$pkg/data.tar" ] || continue`):

```sh
  if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ] && ! manifest_wants "$CAS_MANIFEST" "$pkg" config; then
    log "deploy: $pkg Config-axis off — skipping data restore"; continue
  fi
```

(Bare manifest lines yield both axes on → unchanged behavior. These gates are additive and never bump `FAIL`.)

- [ ] **Step 6: Verify parse + the manifest test + full python suite**

Run: `sh -n provision/root/restore.sh && echo OK && bash tests/test_manifest_axes.sh && python -m unittest discover -s tests -q`
Expected: `OK`, `PASS: manifest_axes`, suite `OK`.

- [ ] **Step 7: Commit**

```bash
git add provision/root/lib-root.sh provision/root/restore.sh tests/test_manifest_axes.sh
git commit -m "feat(restore): honor per-app APK/Config axes at deploy (manifest_wants)"
```

---

### Task B2: GUI — Download list (golden apps, all-on, select/deselect-all)

**Files:**
- Modify: `cas/gui.py` (the Download list built in `on_select_profile`)

This is the second list from Task A3's restructure. It already exists in shape (today's app list = golden apps via `prof.all_pkgs()` + `prof.axes()`); Task B2 confirms it defaults **all-on** when no `manifest` selection is saved, and wires its own Select all / Deselect all using the shared `_set_all` helper, writing `manifest` via the existing `save_manifest`.

- [ ] **Step 1: Confirm default-all-on + select/deselect wiring**

Ensure the Download list's initial check state is: `prof.axes().get(pkg, (True, True))` (all-on default when unset), and its Select all / Deselect all call `_set_all(self.pkg_vars, True/False)`.

- [ ] **Step 2: Full suite + manual GUI smoke**

Run: `python -m unittest discover -s tests -q`
Expected: OK. Manually: pick a profile with a golden → Download list shows all apps ticked; Deselect all clears; Save selection writes `manifest`; a deselected app's APK/Config is skipped on the next Download (verified with Task B1's restore gates).

- [ ] **Step 3: Commit**

```bash
git add cas/gui.py
git commit -m "feat(gui): Download list defaults all-on with select/deselect-all"
```

---

## Self-Review

**Spec coverage:**
- §3 two manifests (`capture-manifest` + `manifest`) → A1 (accessors), A2 (capture uses it), B2 (deploy uses existing manifest). ✓
- §4 Save honors selection + device-driven list + launcher rows→flags + emulator defaults → A2 (capture_to_pc), A3 (scan + default_capture_selection + _save_capture_manifest). ✓
- §5 Download per-axis enforcement → B1 (`manifest_wants` + restore gates); Download list all-on → B2. ✓
- §6 `EMULATOR_PKGS` → A1. ✓
- §7 two labeled lists + select/deselect-all → A3 (restructure + `_set_all`), B2. ✓
- §8 back-compat (no capture-manifest → capture-all; bare line → both axes; no-device fallback) → A2 (no-manifest test), B1 (bare=both), A3 (`_scan_device_apps` returns [] → fallback). ✓
- §9 testing → A1/A2/A3/B1 tests. ✓

**Placeholder scan:** No TBD/TODO; each step has complete code or an exact edit + command. The GUI Tk layout in A3 Step 6 is described against the existing `on_select_profile` structure with complete helper code (widget packing follows the existing two-axis row pattern in that method) — no hand-wavy "add UI" steps.

**Type/name consistency:** `capture-manifest` path via `Profile.capture_manifest_path`; `capture_pkgs/axes/flags`, `EMULATOR_PKGS`, `default_capture_selection(device_apps, game_launcher, home_launcher) -> {pkg:(bool,bool)}`, `_scan_device_apps`, `_save_capture_manifest`, `self.cap_vars` (Save) vs `self.pkg_vars` (Download), `manifest_wants <manifest> <pkg> <axis>` — used consistently across A1→A3 and B1→B2.

## Notes
- `capture.sh` is intentionally untouched (already honors `CAS_MANIFEST` axes + `@gamelauncher`/`@homescreen`).
- Phase A is shippable alone ("choose what to save"); Phase B adds "choose what to deploy, per axis."
