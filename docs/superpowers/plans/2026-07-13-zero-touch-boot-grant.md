# Zero-touch boot grant (overlay.d) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the MagiskSU shell grant appear on the device **never** — pre-write the shell=ALLOW policy from a root script baked into the Magisk-patched `init_boot` (overlay.d), so `su` is authorized before adb ever calls it.

**Architecture:** After `boot_patch.sh` produces the Magisk-patched `new-boot.img` on the device, an isolated `magiskboot` pass injects an `overlay.d` init service + shell script into the ramdisk. On every boot `magiskinit` runs that service as root; it writes the same policy `grant-persist.sh` writes. CAS's existing `root()` → `is_root()` check then passes with no dialog; the auto-tap stays as a fallback for units whose magiskinit ignores overlay.d.

**Tech Stack:** Python 3 (`cas/`), device-side POSIX `sh` + Android `init` rc, `magiskboot` (aarch64, already bundled), `unittest` (`tests/test_cas.py`).

## Global Constraints

- **Device-consumed scripts must be LF-only bytes.** `provision/root/overlay/*` are read by the device's `init`/`sh`; a CR breaks them (same class as the CRLF device-manifest bug). Author and commit them LF; never write them at runtime.
- **Shell exit codes are unreliable across these units.** Confirm on-device success via a **stdout sentinel**, never `rc` alone (existing pattern: `CAS_PATCH_OK`). New sentinel: `CAS_INJECT_OK`.
- **Best-effort inject, never a regression.** If the inject fails for any reason, `patch_init_boot_on_device` must fall through with the plain patched `new-boot.img` — root still works via the auto-tap fallback. The inject must never abort a patch that already succeeded.
- **The magisk applet is not on PATH at boot.** Resolve it via `/data/adb/magisk/magisk` (the pattern already in `grant-persist.sh`).
- **On-device magiskboot is invoked as `./magiskboot`** from `DEV_PATCH` (that dir is not on PATH — matches `boot_patch.sh`).
- **Existing constants (reuse, do not redefine):** `DEV_PATCH = "/data/local/tmp/cas_magiskpatch"`; `MAGISK_PATCH = BUNDLE / "provision" / "root" / "magisk-patch"`; test aliases `PV = cas.provision`, `P = cas.profiles`, `Adb`/`Fastboot` from `cas.adb`; `FakeRunner`/`GrantRunner`/`FbRunner` in `tests/test_cas.py`; config tests override the path via `os.environ["CAS_CONFIG"]`.

---

## File Structure

- **Create** `provision/root/overlay/cas-grant.sh` — boot-time root policy writer (LF-only).
- **Create** `provision/root/overlay/init.cas-grant.rc` — init service that runs it at `boot_completed` as root (LF-only).
- **Modify** `cas/config.py` — add `bake_boot_grant()` toggle (default `True`).
- **Modify** `cas/provision.py` — add `OVERLAY_DIR` constant + `_inject_boot_grant()`; call it inside `patch_init_boot_on_device()`; reword the `root()` pre-authorized success log.
- **Modify** `tests/test_cas.py` — LF/content guard for overlay files; config default test; inject argv / fallback / toggle tests; `root()` skip-auto-tap test; `FakeRunner` branch for `CAS_INJECT_OK`.

---

### Task 1: overlay.d payload files + LF/content guard

**Files:**
- Create: `provision/root/overlay/cas-grant.sh`
- Create: `provision/root/overlay/init.cas-grant.rc`
- Test: `tests/test_cas.py` (new `TestOverlayBootGrant` class)

**Interfaces:**
- Consumes: nothing.
- Produces: two committed device files. `cas-grant.sh` writes marker `/data/local/tmp/cas_boot_grant.done` and the policy rows `policies(2000,2,0,0,0)` + `settings('root_access',3)`. The `.rc` declares service `cas_grant` running `/system/bin/sh /overlay.d/cas-grant.sh` started `on property:sys.boot_completed=1`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cas.py`:

```python
class TestOverlayBootGrant(unittest.TestCase):
    def _overlay(self, name):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return pathlib.Path(repo) / "provision" / "root" / "overlay" / name

    def test_overlay_files_exist_and_are_lf_only(self):
        for name in ("cas-grant.sh", "init.cas-grant.rc"):
            raw = self._overlay(name).read_bytes()
            self.assertNotIn(b"\r", raw, f"{name} must be LF-only (device init/sh consumed)")
            self.assertTrue(raw.endswith(b"\n"), f"{name} must end with a newline")

    def test_cas_grant_writes_the_shell_allow_policy(self):
        sh = self._overlay("cas-grant.sh").read_text()
        # exact policy rows grant-persist.sh writes: shell uid 2000 = allow, adb+apps root
        self.assertIn("policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)", sh)
        self.assertIn("settings (key,value) VALUES('root_access',3)", sh)
        self.assertIn("/data/adb/magisk/magisk", sh)              # applet resolved off-PATH
        self.assertIn("/data/local/tmp/cas_boot_grant.done", sh)  # bench diagnostic marker
        self.assertNotRegex(sh, r"while\s+true")                  # bounded retry, never infinite

    def test_rc_starts_the_service_as_root_at_boot_completed(self):
        rc = self._overlay("init.cas-grant.rc").read_text()
        self.assertIn("service cas_grant /system/bin/sh /overlay.d/cas-grant.sh", rc)
        self.assertIn("user root", rc)
        self.assertIn("seclabel u:r:magisk:s0", rc)
        self.assertIn("oneshot", rc)
        self.assertIn("on property:sys.boot_completed=1", rc)
        self.assertIn("start cas_grant", rc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cas.py::TestOverlayBootGrant -v` (or `python -m unittest tests.test_cas.TestOverlayBootGrant -v`)
Expected: FAIL — files do not exist yet (`FileNotFoundError`).

- [ ] **Step 3: Create `provision/root/overlay/cas-grant.sh`** (write LF-only)

```sh
#!/system/bin/sh
# cas-grant.sh — baked into the Magisk-patched init_boot via overlay.d and started AS ROOT at boot
# (see init.cas-grant.rc). Pre-writes the MagiskSU shell-allow policy so the adb shell's first `su`
# never trips the on-device Grant dialog — zero-touch, first boot or ever. Mirrors grant-persist.sh,
# but runs from inside the device at boot instead of after a PC-driven grant.
#
# Marker (/data/local/tmp/cas_boot_grant.done) is a bench diagnostic: ABSENT after boot => the
# service never ran (overlay.d not honored on this magiskinit); PRESENT with "daemon-not-ready" =>
# it ran but magiskd wasn't up in time. Exit codes on these units are unreliable, so we never rely
# on rc — the marker is the signal.
MARK=/data/local/tmp/cas_boot_grant.done

# Resolve the magisk applet (not on PATH at boot). CAS_MAGISK overrides for tests/odd installs.
MAGISK=magisk
for c in "${CAS_MAGISK:-}" /data/adb/magisk/magisk magisk; do
  [ -n "$c" ] && [ -x "$c" ] && { MAGISK="$c"; break; }
done

# magiskd / magisk.db may not be ready the instant we fire — retry a bounded number of times.
i=0
while [ "$i" -lt 10 ]; do
  if "$MAGISK" --sqlite "SELECT 1" >/dev/null 2>&1; then
    # shell uid 2000 = ALLOW (policy 2) forever (until 0), no logging/notification.
    "$MAGISK" --sqlite "REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)"
    # global: apps AND adb may hold root.
    "$MAGISK" --sqlite "REPLACE INTO settings (key,value) VALUES('root_access',3)"
    echo "cas-grant ok policy=$("$MAGISK" --sqlite "SELECT policy FROM policies WHERE uid=2000")" > "$MARK"
    exit 0
  fi
  i=$((i + 1))
  sleep 2
done
echo "cas-grant daemon-not-ready" > "$MARK"
exit 0
```

- [ ] **Step 4: Create `provision/root/overlay/init.cas-grant.rc`** (write LF-only)

```
# init.cas-grant.rc — injected into the boot ramdisk via overlay.d. Runs cas-grant.sh once as root
# after boot to pre-write the MagiskSU shell-allow policy (zero-touch adb root, no Grant dialog).
# seclabel u:r:magisk:s0 puts the service in Magisk's unconfined domain so it can talk to magiskd.
service cas_grant /system/bin/sh /overlay.d/cas-grant.sh
    user root
    group root
    seclabel u:r:magisk:s0
    oneshot
    disabled

on property:sys.boot_completed=1
    start cas_grant
```

- [ ] **Step 5: Verify LF-only on disk**

Run: `python -c "import pathlib; [print(p, b'\r' in pathlib.Path('provision/root/overlay',p).read_bytes()) for p in ('cas-grant.sh','init.cas-grant.rc')]"`
Expected: both print `... False` (no CR). If your editor added CRLF, re-save as LF.

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_cas.py::TestOverlayBootGrant -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add provision/root/overlay/cas-grant.sh provision/root/overlay/init.cas-grant.rc tests/test_cas.py
git commit -m "feat(root): overlay.d boot-grant payload (cas-grant.sh + init rc)"
```

---

### Task 2: `config.bake_boot_grant()` toggle

**Files:**
- Modify: `cas/config.py` (add after `auto_grant_shell()`, ~line 195)
- Test: `tests/test_cas.py::TestOverlayBootGrant`

**Interfaces:**
- Consumes: `load_config()` (existing).
- Produces: `config.bake_boot_grant() -> bool` (default `True`); read by `patch_init_boot_on_device` in Task 3.

- [ ] **Step 1: Write the failing test** (add to `TestOverlayBootGrant`)

```python
    def test_bake_boot_grant_default_on(self):
        from cas import config
        with tempfile.TemporaryDirectory() as d:
            os.environ["CAS_CONFIG"] = os.path.join(d, "cas-config.json")  # no file -> default
            try:
                self.assertTrue(config.bake_boot_grant())
            finally:
                del os.environ["CAS_CONFIG"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cas.py::TestOverlayBootGrant::test_bake_boot_grant_default_on -v`
Expected: FAIL — `AttributeError: module 'cas.config' has no attribute 'bake_boot_grant'`.

- [ ] **Step 3: Add the toggle to `cas/config.py`** (immediately after `auto_grant_shell`)

```python
def bake_boot_grant():
    """Whether root() bakes the overlay.d boot-grant into the Magisk-patched init_boot so the shell
    su policy is pre-written at boot and no Grant dialog ever appears (default True). Set
    "bake_boot_grant": false in cas-config.json to flash the plain patched image and rely on the
    auto-tap fallback instead."""
    return bool(load_config().get("bake_boot_grant", True))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cas.py::TestOverlayBootGrant::test_bake_boot_grant_default_on -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(config): bake_boot_grant toggle (default on)"
```

---

### Task 3: inject step in `patch_init_boot_on_device`

**Files:**
- Modify: `cas/provision.py` — add `OVERLAY_DIR` near `MAGISK_PATCH` (~line 620); add `_inject_boot_grant()`; wire into `patch_init_boot_on_device()` (~lines 669-674).
- Modify: `tests/test_cas.py` — add a `CAS_INJECT_OK` branch to `FakeRunner.__call__`; add inject tests.

**Interfaces:**
- Consumes: `config.bake_boot_grant()` (Task 2); `OVERLAY_DIR`; `DEV_PATCH`.
- Produces: `_inject_boot_grant(adb, dev_patch, log=print) -> bool`. On True the caller pulls `{dev_patch}/cas-boot.img`; on False it pulls the plain `{dev_patch}/new-boot.img`. `patch_init_boot_on_device` keeps its `(adb, stock_init_boot, dest, log=print) -> bool` signature.

- [ ] **Step 1: Write the failing tests**

First extend `FakeRunner.__call__` so the inject chain "succeeds" (add this branch in the `if "shell" in args:` block, next to the `boot_patch.sh` branch around line 126):

```python
            if "CAS_INJECT_OK" in tail:                 # overlay.d inject chain -> stdout sentinel
                return 0, "- Repacking boot image\nCAS_INJECT_OK\n", ""
```

Then add tests to `tests/test_cas.py` (near the existing patch tests):

```python
    def test_patch_injects_boot_grant_and_pulls_cas_boot(self):
        # With bake_boot_grant on (default), the overlay.d payload is pushed, magiskboot injects it,
        # and the repacked cas-boot.img is what gets pulled to the PC.
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            stock = pathlib.Path(t) / "stock.img"; stock.write_bytes(b"x")
            os.environ["CAS_CONFIG"] = os.path.join(t, "absent.json")  # default -> bake on
            try:
                ok = PV.patch_init_boot_on_device(Adb(runner=r), stock,
                                                  pathlib.Path(t) / "patched.img", log=lambda *_: None)
            finally:
                del os.environ["CAS_CONFIG"]
        self.assertTrue(ok)
        cmds = "\n".join(r.cmds())
        self.assertIn("overlay.d/cas-grant.sh", cmds)      # cpio added the script
        self.assertIn("overlay.d/init.cas-grant.rc", cmds) # cpio added the rc
        self.assertIn("magiskboot repack new-boot.img cas-boot.img", cmds)
        self.assertTrue(any(c[0] == "pull" and c[1].endswith("cas-boot.img") for c in r.calls))

    def test_patch_inject_failure_falls_back_to_plain_image(self):
        # If the inject chain never emits CAS_INJECT_OK, the patch still succeeds by pulling the plain
        # patched new-boot.img (never a regression vs. today).
        class _NoInject(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                if "shell" in args and "CAS_INJECT_OK" in args[-1]:
                    return 1, "- unpack failed\n", "magiskboot: bad ramdisk"
                return super().__call__(args, input_text, timeout)
        r = _NoInject()
        with tempfile.TemporaryDirectory() as t:
            stock = pathlib.Path(t) / "stock.img"; stock.write_bytes(b"x")
            os.environ["CAS_CONFIG"] = os.path.join(t, "absent.json")
            try:
                ok = PV.patch_init_boot_on_device(Adb(runner=r), stock,
                                                  pathlib.Path(t) / "patched.img", log=lambda *_: None)
            finally:
                del os.environ["CAS_CONFIG"]
        self.assertTrue(ok)
        self.assertTrue(any(c[0] == "pull" and c[1].endswith("new-boot.img") for c in r.calls))
        self.assertFalse(any(c[0] == "pull" and c[1].endswith("cas-boot.img") for c in r.calls))

    def test_patch_skips_inject_when_bake_disabled(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            stock = pathlib.Path(t) / "stock.img"; stock.write_bytes(b"x")
            cfg = pathlib.Path(t) / "cas-config.json"
            cfg.write_text('{"bake_boot_grant": false}')
            os.environ["CAS_CONFIG"] = str(cfg)
            try:
                ok = PV.patch_init_boot_on_device(Adb(runner=r), stock,
                                                  pathlib.Path(t) / "patched.img", log=lambda *_: None)
            finally:
                del os.environ["CAS_CONFIG"]
        self.assertTrue(ok)
        cmds = "\n".join(r.cmds())
        self.assertNotIn("cas-grant.sh", cmds)             # nothing injected
        self.assertTrue(any(c[0] == "pull" and c[1].endswith("new-boot.img") for c in r.calls))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cas.py -k "inject or bake_disabled" -v`
Expected: FAIL — `_inject_boot_grant` not defined / `cas-boot.img` never pulled.

- [ ] **Step 3: Add `OVERLAY_DIR` constant to `cas/provision.py`** (next to `MAGISK_PATCH`, ~line 621)

```python
OVERLAY_DIR = BUNDLE / "provision" / "root" / "overlay"   # overlay.d boot-grant payload (rc + cas-grant.sh)
```

- [ ] **Step 4: Add `_inject_boot_grant()` to `cas/provision.py`** (directly above `patch_init_boot_on_device`)

```python
def _inject_boot_grant(adb, dev_patch, log=print):
    """Bake the overlay.d boot-grant (init.cas-grant.rc + cas-grant.sh) into the already-Magisk-
    patched {dev_patch}/new-boot.img, repacked to {dev_patch}/cas-boot.img. This is what makes the
    first `su` prompt-free: at boot magiskinit runs cas-grant.sh as root, which pre-writes the shell
    ALLOW policy. Best-effort — returns True only when the repack sentinel confirms; on False the
    caller flashes the plain new-boot.img (root still works via the auto-tap fallback)."""
    if not OVERLAY_DIR.is_dir():
        log("  ⚠ overlay payload dir missing — skipping boot-grant inject (auto-tap fallback applies).")
        return False
    files = sorted(p for p in OVERLAY_DIR.iterdir() if p.is_file())
    if not files:
        log("  ⚠ overlay payload empty — skipping boot-grant inject.")
        return False
    for f in files:
        if not adb.push(str(f), f"{dev_patch}/{f.name}"):
            log("  ⚠ could not push overlay payload — skipping boot-grant inject.")
            return False
    # Separate magiskboot pass so Magisk's own boot_patch.sh stays untouched. ./magiskboot: DEV_PATCH
    # isn't on PATH. Sentinel (not rc) confirms success — exit codes are unreliable on these units.
    rc, out, err = adb.shell(
        f"cd {dev_patch} && ./magiskboot unpack new-boot.img && "
        f"./magiskboot cpio ramdisk.cpio "
        f"'mkdir 0750 overlay.d' "
        f"'add 0644 overlay.d/init.cas-grant.rc init.cas-grant.rc' "
        f"'add 0755 overlay.d/cas-grant.sh cas-grant.sh' && "
        f"./magiskboot repack new-boot.img cas-boot.img && echo CAS_INJECT_OK")
    if "CAS_INJECT_OK" in out:
        log("  ✓ boot-grant baked into the patched init_boot (overlay.d) — su will be pre-authorized.")
        return True
    log(f"  ⚠ boot-grant inject failed: {((err or out) or '').strip()[:160]} — flashing plain image.")
    return False
```

- [ ] **Step 5: Wire it into `patch_init_boot_on_device()`** — replace the current pull block (the three lines starting `ok = adb.pull(f"{DEV_PATCH}/new-boot.img", str(dest))`, ~line 669) with:

```python
    from . import config as _cfg
    pull_src = f"{DEV_PATCH}/new-boot.img"
    if _cfg.bake_boot_grant() and _inject_boot_grant(adb, DEV_PATCH, log=log):
        pull_src = f"{DEV_PATCH}/cas-boot.img"
    ok = adb.pull(pull_src, str(dest))
    adb.shell(f"rm -rf {DEV_PATCH}")
    if not ok:
        log("ERROR: could not pull the patched init_boot off the device.")
        return False
    log("on-device patch complete — Magisk-patched init_boot pulled to the PC.")
    return True
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_cas.py -k "inject or bake_disabled or patch_init_boot" -v`
Expected: PASS (existing 2 patch tests + 3 new).

- [ ] **Step 7: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(root): inject overlay.d boot-grant into patched init_boot (best-effort)"
```

---

### Task 4: `root()` pre-authorized success wording + skip-auto-tap guard

**Files:**
- Modify: `cas/provision.py` — the `root()` `is_root()` success log (~line 1140).
- Test: `tests/test_cas.py::GrantShellRoot`

**Interfaces:**
- Consumes: `PV.root(...)`, `PV.grant_shell_root`.
- Produces: no signature change — behavior guarantee that a booted-already-root unit returns True without invoking `grant_shell_root`.

- [ ] **Step 1: Write the failing test** (add to `class GrantShellRoot`)

```python
    def test_root_pre_authorized_boot_skips_autotap(self):
        # overlay.d pre-wrote the policy -> the unit boots already-root -> root() succeeds WITHOUT
        # ever invoking the uiautomator auto-tap.
        import tempfile, pathlib
        called = {"autotap": False}
        orig = PV.grant_shell_root
        PV.grant_shell_root = lambda *a, **k: called.__setitem__("autotap", True) or True
        try:
            with tempfile.TemporaryDirectory() as d:
                stock = pathlib.Path(d) / "init_boot.img"; stock.write_bytes(b"x")
                os.environ["CAS_CONFIG"] = str(pathlib.Path(d) / "absent.json")
                try:
                    ok = PV.root(Adb(runner=FakeRunner(root=True)), Fastboot(runner=FbRunner()), stock,
                                 magisk_apk=None, log=lambda *_: None, wait=True,
                                 flasher=lambda adb, target, img, log: True)
                finally:
                    del os.environ["CAS_CONFIG"]
        finally:
            PV.grant_shell_root = orig
        self.assertTrue(ok)
        self.assertFalse(called["autotap"], "auto-tap must not run when su is already pre-authorized")
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python -m pytest "tests/test_cas.py::GrantShellRoot::test_root_pre_authorized_boot_skips_autotap" -v`
Expected: PASS already (the `is_root()` happy path predates this change) — this test **locks in** the guarantee so a future reorder can't regress it. If it FAILS, the ordering is wrong; fix `root()` so `is_root()` is checked before any `grant_shell_root` call.

- [ ] **Step 3: Reword the pre-authorized success log in `root()`** — replace:

```python
    if adb.is_root():
        log("✓ ROOTED — adb shell su works. Ready to '② Download to selected device'.")
        return True
```

with:

```python
    if adb.is_root():
        log("✓ ROOTED — shell pre-authorized at boot (zero-touch, no Magisk prompt). "
            "Ready to '② Download to selected device'.")
        return True
```

- [ ] **Step 4: Run the grant suite to verify it passes**

Run: `python -m pytest tests/test_cas.py::GrantShellRoot -v`
Expected: PASS (all grant tests, including the new one).

- [ ] **Step 5: Run the FULL suite**

Run: `python -m pytest tests/test_cas.py -q`
Expected: all green (prior count + the new tests). If your runner is unittest: `python -m unittest -q`.

- [ ] **Step 6: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(root): name the zero-touch pre-authorized boot; guard auto-tap skip"
```

---

### Task 5: BENCH GATE — prove overlay.d fires on the air-x (manual, Donald)

> **Not a code task.** This is the on-device gate the spec front-loads. Tests cannot prove magiskinit honors overlay.d. Do this on a **MANGMI air-x** before trusting the feature fleet-wide. It uses the *real* injected image built by Tasks 1-3 (no throwaway needed — the marker in `cas-grant.sh` gives the same diagnostic).

- [ ] **Step 1:** From a clean, *stock, un-rooted* air-x, run CAS `⓪ Root` (or the equivalent `PV.root(...)` path) so it patches → injects overlay.d → flashes → reboots.

- [ ] **Step 2:** After boot, from the PC:

```bash
adb shell ls -l /data/local/tmp/cas_boot_grant.done   # marker present?
adb shell cat /data/local/tmp/cas_boot_grant.done      # "cas-grant ok policy=2" hoped-for
adb shell /debug_ramdisk/su -c id                      # expect uid=0(root) with NO dialog on device
```

- [ ] **Step 3: Interpret** (this is why the marker exists):
  - `su id` → `uid=0` **and no dialog appeared** → **GREEN.** overlay.d works; the feature is proven. Proceed to Task 6.
  - Marker present, content `policy=2`, but `su` still prompts → policy write raced or the daemon domain rejected it. Bump the retry count / trigger later; re-flash.
  - Marker **absent** → the service never ran. overlay.d file placement is wrong for this magiskinit. Try relocating the script (change the rc to `/sbin/cas-grant.sh` and inject at `overlay.d/sbin/cas-grant.sh`), re-flash. If still absent after a couple of placements → overlay.d is not honored here: set `"bake_boot_grant": false` for this model and rely on the auto-tap fallback (hardening that becomes the follow-up).

- [ ] **Step 4:** Record the outcome (green / which placement worked / fell back) in a one-line note on the PR or commit so the fleet default is documented.

---

### Task 6: wrap-up — full green + ship

**Files:** none (verification + integration).

- [ ] **Step 1: Full suite green**

Run: `python -m pytest tests/test_cas.py -q` (and the shell tests if the repo runs them: `bash tests/run_sh_tests.sh` or the project's usual command).
Expected: all green.

- [ ] **Step 2:** Confirm the two overlay files ship in the built kit (they live under `provision/root/` which the packaged app already bundles alongside `magisk-patch/`). If the build copies `provision/` selectively, add `provision/root/overlay/` to the copy list and add a CI presence guard mirroring `test_ci_packages_the_drivers_tree_into_the_windows_kit`.

- [ ] **Step 3: Push** per the repo's flow (direct to `main`).

```bash
git push origin main
```

---

## Self-Review

**Spec coverage:**
- overlay.d payload (rc + cas-grant.sh, LF-only, policy SQL, marker) → Task 1. ✓
- Injection as a separate magiskboot pass, best-effort fallback → Task 3. ✓
- `root()` pre-authorized happy path + auto-tap fallback layering → Task 4 (fallback already exists; guarded). ✓
- `bake_boot_grant` config toggle → Task 2. ✓
- LF/CRLF guard → Task 1 test. ✓
- Seal unchanged (stock reflash strips overlay.d) → no task needed; asserted in spec, existing seal un-root check covers it. ✓
- Bench spike on air-x as the gate → Task 5. ✓
- Unit tests (inject argv, fallback, toggle, skip-auto-tap) → Tasks 1-4. ✓

**Placeholder scan:** none — every code/rc/sh block is complete; no TBD/TODO.

**Type consistency:** `_inject_boot_grant(adb, dev_patch, log=print)` defined in Task 3 and called in the same task; `bake_boot_grant()` defined Task 2, consumed Task 3; `CAS_INJECT_OK` sentinel produced by the inject command (Task 3 Step 4) and recognized by the FakeRunner branch (Task 3 Step 1) and inject failure subclass. `pull_src` targets `cas-boot.img` (inject ok) vs `new-boot.img` (fallback) — consistent across code and all three inject tests.
