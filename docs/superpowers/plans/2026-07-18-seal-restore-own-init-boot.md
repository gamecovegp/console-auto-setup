# Seal Restores Own Factory init_boot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a CAS-sealed unit ship with its **own exact-build factory `init_boot`** so its device OTA applies cleanly (no `code 20 / VALIDATE_SOURCE_HASH_ERROR`).

**Architecture:** At the end of a successful `root()`, capture the pristine factory `init_boot` from the **inactive** A/B slot (the only copy CAS never overwrites) into a per-build store under the shared library. At `seal()`, resolve that captured exact-build image first, falling back to the model-matched library image plus a loud warning when no capture exists. Two guards keep the store clean: the dump must be a valid `ANDROID!` boot image, and must not contain Magisk markers.

**Tech Stack:** Python 3 (stdlib only — `json`, `hashlib`, `pathlib`, `re`, `datetime`), `unittest` + `unittest.mock`. Existing modules: `cas/provision.py`, `cas/firmware.py`, `cas/adb.py`, `cas/cli.py`.

## Global Constraints

- Tests are **`unittest.TestCase`** classes under `tests/`, run with `python -m pytest <file> -v`. Each test file does `sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))` before importing `cas`.
- `cas/initboot_store.py` is **pure** — no `config`, `adb`, or `firmware` imports (avoids cycles; keeps it unit-testable with a tmpdir). Callers pass `store_root` in.
- `adb.su(cmd)` returns `(rc, out, err)`; commands passed to `su` must be a **single** command with **no** `&&`/`||`/`;`/quoted subcommands (adb space-joins argv).
- Capture is **additive & non-fatal**: any failure logs a warning and never changes `root()`'s success.
- Store location: `FW.firmware_root().parent / "_init_boot_factory"` (a sibling of `_firmware`, so captures travel with the shared library across benches).
- Store key: the unit's build fingerprint, `adb.getprop("ro.build.fingerprint")`.

---

### Task 1: `cas/initboot_store.py` — per-build capture store (pure)

**Files:**
- Create: `cas/initboot_store.py`
- Test: `tests/test_initboot_store.py`

**Interfaces:**
- Produces:
  - `looks_like_boot_image(data: bytes) -> bool` — True iff `data` starts with `ANDROID!`.
  - `contains_magisk(data: bytes) -> bool` — True iff any Magisk marker is present.
  - `slug(fingerprint: str) -> str` — filesystem-safe key.
  - `has(store_root, fingerprint) -> bool`
  - `get(store_root, fingerprint) -> pathlib.Path | None` — path to the stored `init_boot.img`, or None.
  - `put(store_root, fingerprint, img_path, meta: dict) -> pathlib.Path` — idempotent; first capture per build wins.

- [ ] **Step 1: Write the failing test**

Create `tests/test_initboot_store.py`:

```python
# tests/test_initboot_store.py
import os
import sys
import pathlib
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import initboot_store as IBS

FP = "qti/kalama/kalama:13/TKQ1.231222.001/eng.RP6.20260119.170007:user/release-keys"


class TestGuards(unittest.TestCase):
    def test_looks_like_boot_image_true(self):
        self.assertTrue(IBS.looks_like_boot_image(b"ANDROID!" + b"\x00" * 100))

    def test_looks_like_boot_image_false_on_zeros(self):
        self.assertFalse(IBS.looks_like_boot_image(b"\x00" * 128))

    def test_contains_magisk_true(self):
        self.assertTrue(IBS.contains_magisk(b"ANDROID!....MAGISKINIT....payload"))

    def test_contains_magisk_false(self):
        self.assertFalse(IBS.contains_magisk(b"ANDROID!" + b"\x00" * 256))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_init_boot_factory"
        self.img = pathlib.Path(self.tmp) / "src.img"
        self.img.write_bytes(b"ANDROID!" + b"\x00" * 1024)

    def test_slug_is_filesystem_safe(self):
        s = IBS.slug(FP)
        self.assertNotIn("/", s)
        self.assertNotIn(":", s)
        self.assertIn("eng.RP6.20260119.170007", s)

    def test_put_then_get_and_has(self):
        self.assertFalse(IBS.has(self.root, FP))
        self.assertIsNone(IBS.get(self.root, FP))
        p = IBS.put(self.root, FP, self.img, {"fingerprint": FP, "sha256": "x", "size": 1032})
        self.assertTrue(p.exists())
        self.assertEqual(p.read_bytes(), self.img.read_bytes())
        self.assertTrue(IBS.has(self.root, FP))
        self.assertEqual(IBS.get(self.root, FP), p)

    def test_put_is_idempotent_first_wins(self):
        IBS.put(self.root, FP, self.img, {"fingerprint": FP})
        other = pathlib.Path(self.tmp) / "other.img"
        other.write_bytes(b"ANDROID!" + b"\xff" * 2048)
        IBS.put(self.root, FP, other, {"fingerprint": FP})
        self.assertEqual(IBS.get(self.root, FP).read_bytes(), self.img.read_bytes())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_initboot_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cas.initboot_store'`.

- [ ] **Step 3: Write minimal implementation**

Create `cas/initboot_store.py`:

```python
"""Per-build store of each unit's OWN factory init_boot, captured at root time and restored at seal.

Pure module: no adb/config/firmware imports. Callers pass `store_root` (a Path), so it stays trivially
unit-testable and free of import cycles. Layout: <store_root>/<slug(fingerprint)>/init_boot.img + meta.json
"""
import json
import pathlib
import re

_MAGISK_MARKERS = (b"MAGISKINIT", b"MAGISKPOLICY", b".magisk")


def looks_like_boot_image(data):
    """True iff `data` is an Android boot image (magic 'ANDROID!' at offset 0). A zeroed/empty inactive
    slot fails this — the reason we never store an unpopulated single-slot-flashed unit's dump."""
    return len(data) >= 8 and data[:8] == b"ANDROID!"


def contains_magisk(data):
    """True iff the image carries Magisk markers (i.e. it's a patched/rooted image, not factory)."""
    return any(m in data for m in _MAGISK_MARKERS)


def slug(fingerprint):
    """Filesystem-safe key from a build fingerprint (keeps alnum and dots, collapses the rest)."""
    return re.sub(r"[^A-Za-z0-9.]+", "_", fingerprint or "").strip("_") or "unknown"


def _dir(store_root, fingerprint):
    return pathlib.Path(store_root) / slug(fingerprint)


def has(store_root, fingerprint):
    return (_dir(store_root, fingerprint) / "init_boot.img").is_file()


def get(store_root, fingerprint):
    p = _dir(store_root, fingerprint) / "init_boot.img"
    return p if p.is_file() else None


def put(store_root, fingerprint, img_path, meta):
    """Store `img_path` as this build's factory init_boot. Idempotent: if one already exists for this
    build, keep it (first clean capture wins) and return it — never overwrite a good capture."""
    d = _dir(store_root, fingerprint)
    dest = d / "init_boot.img"
    if dest.is_file():
        return dest
    d.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(pathlib.Path(img_path).read_bytes())
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dest
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_initboot_store.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add cas/initboot_store.py tests/test_initboot_store.py
git commit -m "feat(seal): per-build factory init_boot store + validity/Magisk guards"
```

---

### Task 2: `capture_factory_init_boot()` — dump the inactive slot at root time

**Files:**
- Modify: `cas/provision.py` (add function near `patch_init_boot_on_device`, ~line 810)
- Test: `tests/test_seal_capture.py`

**Interfaces:**
- Consumes: `cas.initboot_store` (`looks_like_boot_image`, `contains_magisk`, `has`, `put`); `adb.slot_suffix()`, `adb.su()`, `adb.pull()`, `adb.getprop()`.
- Produces: `capture_factory_init_boot(adb, store_root, log=print) -> bool` — True iff a factory image was stored (or already present); False (non-fatal) otherwise.

- [ ] **Step 1: Write the failing test**

Create `tests/test_seal_capture.py`:

```python
# tests/test_seal_capture.py
import os
import sys
import pathlib
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import provision as PV
from cas import initboot_store as IBS

FP = "qti/kalama/kalama:13/TKQ1.231222.001/eng.RP6.20260119.170007:user/release-keys"


def _fake_adb(slot="_a", su_rc=0, pull_bytes=b"ANDROID!" + b"\x00" * 1024, fp=FP):
    adb = mock.Mock()
    adb.slot_suffix.return_value = slot
    adb.getprop.return_value = fp
    adb.su.return_value = (su_rc, "", "")

    def _pull(src, dst):
        if pull_bytes is None:
            return False
        pathlib.Path(dst).write_bytes(pull_bytes)
        return True

    adb.pull.side_effect = _pull
    return adb


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = pathlib.Path(self.tmp) / "_init_boot_factory"

    def test_captures_inactive_slot_b_when_active_a(self):
        adb = _fake_adb(slot="_a")
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertTrue(ok)
        self.assertTrue(IBS.has(self.store, FP))
        # dumped the INACTIVE slot (_b) since active is _a
        ddcmd = adb.su.call_args[0][0]
        self.assertIn("init_boot_b", ddcmd)

    def test_rejects_empty_inactive_slot(self):
        adb = _fake_adb(pull_bytes=b"\x00" * 4096)
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertFalse(ok)
        self.assertFalse(IBS.has(self.store, FP))

    def test_rejects_magisk_patched_image(self):
        adb = _fake_adb(pull_bytes=b"ANDROID!....MAGISKINIT....")
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertFalse(ok)
        self.assertFalse(IBS.has(self.store, FP))

    def test_non_fatal_on_pull_failure(self):
        adb = _fake_adb(pull_bytes=None)
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertFalse(ok)  # returns False, does not raise

    def test_skips_when_no_inactive_slot(self):
        adb = _fake_adb(slot="")  # A-only device: no inactive slot
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertFalse(ok)
        adb.su.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_seal_capture.py -v`
Expected: FAIL — `AttributeError: module 'cas.provision' has no attribute 'capture_factory_init_boot'`.

- [ ] **Step 3: Write minimal implementation**

In `cas/provision.py`, add the import near the other `from . import` lines at the top of the module:

```python
from . import initboot_store as _ibs
```

Add this function immediately after `patch_init_boot_on_device` (after its `return ok` around line 810):

```python
def capture_factory_init_boot(adb, store_root, log=print):
    """Capture this unit's OWN factory init_boot into the per-build store, for seal() to restore later so
    the unit's device OTA still source-verifies. Read the INACTIVE A/B slot — CAS only ever flashes the
    ACTIVE slot, so the inactive one still holds the pristine factory image. ADDITIVE & NON-FATAL: any
    problem logs a warning and returns False; root() succeeds regardless. Returns True iff a valid factory
    image is now stored for this build."""
    slot = (adb.slot_suffix() or "").strip()
    if slot not in ("_a", "_b"):
        log("  factory init_boot capture skipped: no distinct inactive A/B slot on this unit.")
        return False
    inactive = "_b" if slot == "_a" else "_a"
    fp = adb.getprop("ro.build.fingerprint")
    if _ibs.has(store_root, fp):
        return True                                     # already captured for this build
    dev = "/data/local/tmp/cas_factory_ib.img"
    rc, _out, err = adb.su(f"dd if=/dev/block/by-name/init_boot{inactive} of={dev}")
    if rc != 0:
        log(f"  factory init_boot capture skipped: could not read init_boot{inactive} ({err.strip()}).")
        return False
    with tempfile.TemporaryDirectory() as td:
        local = str(pathlib.Path(td) / "factory_init_boot.img")
        pulled = adb.pull(dev, local)
        adb.su(f"rm -f {dev}")
        if not pulled:
            log("  factory init_boot capture skipped: could not pull the dumped image off the device.")
            return False
        data = pathlib.Path(local).read_bytes()
        if not _ibs.looks_like_boot_image(data):
            log(f"  factory init_boot capture skipped: init_boot{inactive} is not a valid boot image "
                "(empty/unpopulated inactive slot) — not storing.")
            return False
        if _ibs.contains_magisk(data):
            log(f"  factory init_boot capture skipped: init_boot{inactive} carries Magisk markers "
                "(not a factory image) — not storing.")
            return False
        meta = {
            "fingerprint": fp,
            "incremental": adb.getprop("ro.build.version.incremental"),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "source_serial": adb.serial,
            "captured_utc": datetime.datetime.utcnow().isoformat() + "Z",
        }
        _ibs.put(store_root, fp, local, meta)
    log(f"  ✓ captured this unit's factory init_boot for build {fp} (seal will restore it → OTA stays "
        "healthy).")
    return True
```

Ensure `hashlib`, `datetime`, and `tempfile` are imported at the top of `cas/provision.py` (add any that are missing — `tempfile` is already used by `root()`). `adb.serial` is the device serial (`Adb.__init__` sets `self.serial`, `cas/adb.py:215`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_seal_capture.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_seal_capture.py
git commit -m "feat(seal): capture the unit's own factory init_boot from the inactive slot at root time"
```

---

### Task 3: Restore the captured image at seal (resolver + `seal_all` wiring)

**Files:**
- Modify: `cas/provision.py` — add `resolve_seal_stock()` (near `seal()`), and wire it into the `seal_all` worker at ~1938-1961.
- Test: `tests/test_seal_capture.py` (add a class)

**Interfaces:**
- Consumes: `cas.initboot_store.get`.
- Produces: `resolve_seal_stock(library_stock, capture_path, fingerprint, log=print) -> str` — the path `seal()` should flash.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_seal_capture.py`:

```python
class TestSealResolve(unittest.TestCase):
    def test_prefers_capture_over_library(self):
        logs = []
        out = PV.resolve_seal_stock("/lib/kit_init_boot.img", "/store/cap.img", FP, log=logs.append)
        self.assertEqual(out, "/store/cap.img")
        self.assertTrue(any("captured factory init_boot" in m for m in logs))

    def test_falls_back_to_library_with_warning(self):
        logs = []
        out = PV.resolve_seal_stock("/lib/kit_init_boot.img", None, FP, log=logs.append)
        self.assertEqual(out, "/lib/kit_init_boot.img")
        self.assertTrue(any("OTA may fail" in m for m in logs))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_seal_capture.py::TestSealResolve -v`
Expected: FAIL — `AttributeError: module 'cas.provision' has no attribute 'resolve_seal_stock'`.

- [ ] **Step 3: Write minimal implementation**

In `cas/provision.py`, add just above `def seal(`:

```python
def resolve_seal_stock(library_stock, capture_path, fingerprint, log=print):
    """Pick the init_boot seal() will flash to un-root: prefer this unit's OWN captured factory image
    (exact-build → its device OTA source-verifies), else fall back to the model-matched library image
    with a LOUD warning that the unit's OTA may break until it's re-captured."""
    if capture_path:
        log(f"  un-root: restoring this unit's own captured factory init_boot for build {fingerprint} "
            "(keeps its device OTA healthy).")
        return str(capture_path)
    log(f"  ⚠ no factory init_boot captured for build {fingerprint} — sealing with the library image; "
        "this unit's device OTA may fail (code 20) until it's rooted from a clean state to capture it.")
    return library_stock
```

Then in the `seal_all` worker, right after the firmware block that sets `stock_path` from `fw.stock_boot_image()` (immediately after the `except Exception as e:` clause ending ~line 1971, before `if flasher is None:`), insert:

```python
            # Prefer this unit's OWN captured factory init_boot (exact build) over the model-matched
            # library image, so the sealed unit's device OTA source-verifies. Falls back + warns.
            from . import firmware as FW
            store_root = FW.firmware_root().parent / "_init_boot_factory"
            _fp = adb.getprop("ro.build.fingerprint")
            stock_path = resolve_seal_stock(stock_path, _ibs.get(store_root, _fp), _fp, log=_wlog)
```

(The `seal_all` worker already imports `from . import firmware as FW` inside its `try`; the extra import here is harmless and guarantees `FW` is in scope at this point regardless of whether the `try` ran.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_seal_capture.py -v`
Expected: PASS (all classes).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python -m pytest tests/test_cas.py tests/test_firmware.py -v`
Expected: PASS (existing seal/root tests unaffected — `seal()` signature and flash mechanics unchanged).

- [ ] **Step 6: Commit**

```bash
git add cas/provision.py tests/test_seal_capture.py
git commit -m "feat(seal): restore the unit's own captured factory init_boot, fall back to library + warn"
```

---

### Task 4: Invoke capture at the end of a successful root (root + callers)

**Files:**
- Modify: `cas/provision.py` — `root()` gains `capture_store=None`; call capture before each success `return True` (~lines 1624, 1630); `root_all` worker resolves `store_root` and passes it (~line 1875).
- Modify: `cas/cli.py:108` — pass `capture_store` to the single-device `PV.root(...)`.
- Test: `tests/test_seal_capture.py` (add a class)

**Interfaces:**
- Consumes: `capture_factory_init_boot`.
- Produces: `root(..., capture_store=None)` — when `capture_store` is set and root succeeds, the factory init_boot is captured.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_seal_capture.py`:

```python
class TestRootCaptureWiring(unittest.TestCase):
    def test_root_calls_capture_on_success(self):
        calls = []

        def fake_capture(adb, store_root, log=print):
            calls.append(store_root)
            return True

        adb = mock.Mock()
        # Drive root() straight to its success return: booted + granted, no boot-grant bake.
        adb.wait_boot.return_value = True
        adb.is_root.return_value = True
        adb.getprop.return_value = "init_boot"  # any nonempty; kernel-size guard is bypassed below

        with mock.patch.object(PV, "capture_factory_init_boot", side_effect=fake_capture), \
             mock.patch.object(PV, "_await_boot_grant", return_value=True), \
             mock.patch.object(PV, "patch_init_boot_on_device", return_value=True), \
             mock.patch.object(PV, "_img_kernel_size", return_value=0), \
             mock.patch.object(PV.pathlib.Path, "exists", return_value=True), \
             mock.patch("cas.config.bake_boot_grant", return_value=False), \
             mock.patch("cas.config.auto_grant_shell", return_value=False):
            adb.boot_flash_target.return_value = "init_boot_a"
            adb.is_golden.return_value = False
            ok = PV.root(adb, mock.Mock(), "/lib/stock_init_boot.img",
                         magisk_apk=None, log=lambda *a: None,
                         flasher=lambda *a, **k: True, capture_store="/store")
        self.assertTrue(ok)
        self.assertEqual(calls, ["/store"])
```

> If `root()`'s internal guards make this hard to drive end-to-end, narrow the test to assert that `root(...)` forwards `capture_store` by patching `capture_factory_init_boot` and asserting it was called once with `"/store"`; keep the mocks minimal and adjust to the real `root()` control flow you see in the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_seal_capture.py::TestRootCaptureWiring -v`
Expected: FAIL — `root()` has no `capture_store` param (`TypeError: unexpected keyword argument 'capture_store'`).

- [ ] **Step 3: Wire capture into `root()` and callers**

3a. Change the `root()` signature (~line 1511):

```python
def root(adb, fastboot, stock_init_boot, magisk_apk=None, log=print, wait=True, model_match=None,
         force=False, flasher=None, capture_store=None):
```

3b. In `root()`'s success paths, capture just before returning True. At the `if granted:` block (~line 1624):

```python
    if granted:
        if capture_store:
            capture_factory_init_boot(adb, capture_store, log=log)
        log("✓ ROOTED — shell pre-authorized at boot (zero-touch, no Magisk prompt). "
            "Ready to '② Download to selected device'.")
        return True
```

And in the auto-grant success (~line 1630):

```python
        if grant_shell_root(adb, log=log):
            if capture_store:
                capture_factory_init_boot(adb, capture_store, log=log)
            log("✓ ROOTED — shell auto-granted. Ready to '② Download'.")
            return True
```

3c. In the `root_all` worker, resolve the store and pass it. Just before the `ok = root(adb, fb, stock_path, ...)` call (~line 1875), add:

```python
            capture_store = FW.firmware_root().parent / "_init_boot_factory"
```

and add the kwarg to the call:

```python
            ok = root(adb, fb, stock_path,
                      magisk_apk=_kit_apk(MAGISK_PKG, prof, appdir, magisk_rel),
                      log=_wlog,
                      model_match=prof.meta.get("model_match"), force=(serial in force_serials),
                      flasher=flasher, capture_store=capture_store)
```

(`FW` is imported in the worker as `from . import firmware as FW` at ~line 1838 — in scope here.)

3d. In `cas/cli.py:108`, pass the store to the single-device root:

```python
        from cas import firmware as _FW
        _cap = _FW.firmware_root().parent / "_init_boot_factory"
        return 0 if PV.root(adb, fb, P.resolve_asset(prof, APPDIR, stock_rel),
                            ..., capture_store=_cap) else 1
```

(Keep the existing positional/keyword args exactly as they are; only add `capture_store=_cap`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_seal_capture.py::TestRootCaptureWiring -v`
Expected: PASS.

- [ ] **Step 5: Full suite (no regressions)**

Run: `python -m pytest tests/ -v`
Expected: PASS — existing root/seal/firmware/UI tests unaffected.

- [ ] **Step 6: Commit**

```bash
git add cas/provision.py cas/cli.py tests/test_seal_capture.py
git commit -m "feat(seal): capture factory init_boot at the end of a successful root (GUI + CLI paths)"
```

---

## Manual bench verification (after all tasks, on a real unit)

Not automatable — record the result in the PR/commit:

1. On a **fresh** RP6 (both slots on shipped build), run CAS **Root** → confirm the log shows `✓ captured this unit's factory init_boot for build …` and that `<library>/_init_boot_factory/<build>/init_boot.img` exists, sha256 recorded.
2. Provision, then **Lock/Seal** → confirm the log shows `restoring this unit's own captured factory init_boot`.
3. On the sealed unit: Settings → System Update → **the OTA applies** (no `code 20`).
4. Negative check: seal a unit with **no** capture for its build → confirm the loud `OTA may fail (code 20)` warning fires and it still seals.

## Self-review notes

- **Spec coverage:** capture store (Task 1) ✓; capture-at-root from inactive slot + validity + Magisk guards (Task 2) ✓; seal prefers capture, falls back + warns (Task 3) ✓; per-build key in library, GUI+CLI paths (Task 4) ✓; non-fatal capture ✓; documented OTA'd-unit limitation carried from spec (manual verification note). 
- **Types:** `store_root: Path`, `fingerprint: str`, `capture_factory_init_boot(adb, store_root, log)->bool`, `resolve_seal_stock(library_stock, capture_path, fingerprint, log)->str`, `capture_store` kwarg on `root()` — consistent across tasks.
- **Line numbers are approximate** — the implementer must anchor edits to the surrounding code shown, not the exact line.
