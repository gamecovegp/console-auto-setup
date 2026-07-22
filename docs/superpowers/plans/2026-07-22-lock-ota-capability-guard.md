# Lock OTA-Capability Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A unit that CAS roots, provisions and seals can still take its vendor delta OTA — and when that can't be assured, Lock says so instead of shipping silently broken.

**Architecture:** An A/B delta OTA source-verifies every partition by SHA-256 over blocks, so the image Seal flashes must be the unit's *exact-build factory* image. Today Seal trusts whatever `_init_boot_factory` holds, and that store can be poisoned by a capture read from a slot a partial OTA had written. This adds kit **build provenance**, a resolver that prefers a *proven* kit over a contradicting capture, a capture guard that refuses to read a slot while an update is staged, and provenance reporting at Lock.

**Tech Stack:** Python 3 stdlib only. Tests are `unittest` (CI runs `python -m unittest discover -p "test_*.py" -v` from `tests/`) — **no pytest, no third-party imports**.

## Global Constraints

- **Additive only. Shipped RP6/Thor behaviour must not change.** Their kits are a *different build* than the unit (`eng.RP6.20260119` vs kit `RP6_20260115`; `eng.Thor.20260206` vs `Thor_20260112`), so for them the store capture is the only correct source. A kit may override a capture **only** on exact build-fingerprint match.
- **All kit fingerprints are currently `""`** (`version.meta.json` → `fingerprint`). Every new kit-preference path is therefore inert on day one and changes nothing until a fingerprint is explicitly recorded.
- **Never break `resolve_seal_stock`'s existing signature or return type** — it has existing tests. Add alongside it.
- Capture stays **ADDITIVE & NON-FATAL**: any problem logs a warning and returns `False`; `root()` succeeds regardless.
- `adb.su(cmd)` must receive **ONE command** — no `&&`, `||`, `;` or quotes (adb space-joins argv, so the device shell eats them).
- Test runner: `cd tests && python -m unittest <module> -v`.

---

### Task 1: Kit build provenance (record + read a kit version's fingerprint)

A kit becomes authoritative only when it carries a recorded build fingerprint equal to the unit's. `versions/<version>/version.meta.json` already has a `fingerprint` key (currently `""`); this adds public read/write helpers.

**Files:**
- Modify: `cas/firmware.py` (add after `set_gate_fields`, ~line 890)
- Test: `tests/test_kit_provenance.py`

**Interfaces:**
- Consumes: `firmware._version_meta_path(firmware, version)`, `firmware._read_json(p)`, `firmware._write_json(p, obj)`, `firmware.find(firmware_id, root)`
- Produces:
  - `firmware.build_fingerprint(fw, version) -> str | None` — the recorded fingerprint, or `None` when unset/blank
  - `firmware.set_build_fingerprint(firmware_id, root, version, fingerprint) -> str` — records it, returns the stored value; raises `ValueError` on unknown id

- [ ] **Step 1: Write the failing test**

Create `tests/test_kit_provenance.py`:

```python
# tests/test_kit_provenance.py
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import firmware as FW

FP = "MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/eng.hxh.20260507.141302:user/release-keys"


def _make_kit(root, fw_id="air-x", version="20260507-165105", fingerprint=""):
    """Minimal on-disk kit: meta.json + versions/<v>/version.meta.json."""
    p = pathlib.Path(root) / fw_id
    (p / "versions" / version).mkdir(parents=True, exist_ok=True)
    (p / "meta.json").write_text(json.dumps({"id": fw_id, "current": version}))
    (p / "versions" / version / "version.meta.json").write_text(
        json.dumps({"fingerprint": fingerprint}))
    return p


class TestBuildFingerprint(unittest.TestCase):
    def test_blank_fingerprint_reads_as_none(self):
        with tempfile.TemporaryDirectory() as td:
            _make_kit(td, fingerprint="")
            fw = FW.find("air-x", td)
            self.assertIsNone(FW.build_fingerprint(fw, "20260507-165105"))

    def test_whitespace_fingerprint_reads_as_none(self):
        with tempfile.TemporaryDirectory() as td:
            _make_kit(td, fingerprint="   ")
            fw = FW.find("air-x", td)
            self.assertIsNone(FW.build_fingerprint(fw, "20260507-165105"))

    def test_set_then_read_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            _make_kit(td)
            FW.set_build_fingerprint("air-x", td, "20260507-165105", FP)
            fw = FW.find("air-x", td)
            self.assertEqual(FW.build_fingerprint(fw, "20260507-165105"), FP)

    def test_set_preserves_other_version_meta_keys(self):
        with tempfile.TemporaryDirectory() as td:
            p = _make_kit(td)
            vm = p / "versions" / "20260507-165105" / "version.meta.json"
            vm.write_text(json.dumps({"fingerprint": "", "dev_code": "MQ66", "storage": "emmc"}))
            FW.set_build_fingerprint("air-x", td, "20260507-165105", FP)
            meta = json.loads(vm.read_text())
            self.assertEqual(meta["dev_code"], "MQ66")
            self.assertEqual(meta["storage"], "emmc")
            self.assertEqual(meta["fingerprint"], FP)

    def test_unknown_firmware_id_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                FW.set_build_fingerprint("nope", td, "v1", FP)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd tests && python -m unittest test_kit_provenance -v`
Expected: FAIL — `AttributeError: module 'cas.firmware' has no attribute 'build_fingerprint'`

- [ ] **Step 3: Write minimal implementation**

Add to `cas/firmware.py`, immediately after `set_gate_fields`:

```python
def build_fingerprint(firmware, version=None):
    """The build fingerprint RECORDED for this kit version, or None when unset/blank.

    Provenance gate: a kit is only authoritative as "this unit's factory image" when it carries a
    fingerprint EQUAL to the unit's. Every kit currently records "" (unset), so callers treating
    None as "unproven" leave today's behaviour untouched — which is the point: RP6/Thor kits are a
    DIFFERENT build than their units, and preferring them would flash a wrong-build image."""
    if firmware is None:
        return None
    v = version or firmware.current
    if not v:
        return None
    fp = str(_read_json(_version_meta_path(firmware, v)).get("fingerprint") or "").strip()
    return fp or None


def set_build_fingerprint(firmware_id, root, version, fingerprint):
    """Record the build fingerprint for one kit version. Only that key is touched, so gate fields and
    ingest metadata already in version.meta.json survive. Raises ValueError on an unknown id."""
    fw = find(firmware_id, root)
    if fw is None:
        raise ValueError(f"no firmware '{firmware_id}' in {root}")
    p = _version_meta_path(fw, version)
    meta = _read_json(p)
    meta["fingerprint"] = str(fingerprint or "").strip()
    _write_json(p, meta)
    return meta["fingerprint"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd tests && python -m unittest test_kit_provenance -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_kit_provenance.py
git commit -m "feat(firmware): record and read a kit version's build fingerprint

A kit is only authoritative as a unit's factory image on EXACT build match.
Every kit records \"\" today, so build_fingerprint() returns None everywhere and
no existing behaviour changes."
```

---

### Task 2: Quarantine a contradicted capture

When a proven kit disagrees with a stored capture, the capture is wrong and must not be reused on the next unit of that build. Move it aside with the reason recorded.

**Files:**
- Modify: `cas/initboot_store.py` (add after `put`)
- Test: `tests/test_initboot_quarantine.py`

**Interfaces:**
- Consumes: `initboot_store._dir(store_root, fingerprint)`, `initboot_store.get(store_root, fingerprint)`
- Produces: `initboot_store.quarantine(store_root, fingerprint, reason) -> pathlib.Path | None` — the quarantine dir, or `None` when there was nothing to quarantine

- [ ] **Step 1: Write the failing test**

Create `tests/test_initboot_quarantine.py`:

```python
# tests/test_initboot_quarantine.py
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import initboot_store as IBS

FP = "MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/eng.hxh.20260507.141302:user/release-keys"
IMG = b"ANDROID!" + b"\x00" * 2048


def _store_with_capture(root):
    d = IBS._dir(root, FP)
    d.mkdir(parents=True, exist_ok=True)
    (d / "init_boot.img").write_bytes(IMG)
    (d / "meta.json").write_text(json.dumps({"fingerprint": FP, "size": len(IMG)}))
    return d


class TestQuarantine(unittest.TestCase):
    def test_moves_capture_aside_and_get_misses(self):
        with tempfile.TemporaryDirectory() as td:
            _store_with_capture(td)
            self.assertIsNotNone(IBS.get(td, FP))
            q = IBS.quarantine(td, FP, "contradicted by proven kit")
            self.assertIsNotNone(q)
            self.assertTrue(pathlib.Path(q).is_dir())
            self.assertIsNone(IBS.get(td, FP), "quarantined capture must read as a MISS")

    def test_records_the_reason(self):
        with tempfile.TemporaryDirectory() as td:
            _store_with_capture(td)
            q = IBS.quarantine(td, FP, "contradicted by proven kit")
            note = json.loads((pathlib.Path(q) / "quarantine.json").read_text())
            self.assertEqual(note["reason"], "contradicted by proven kit")
            self.assertEqual(note["fingerprint"], FP)

    def test_preserves_the_image_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            _store_with_capture(td)
            q = IBS.quarantine(td, FP, "why")
            self.assertEqual((pathlib.Path(q) / "init_boot.img").read_bytes(), IMG)

    def test_nothing_to_quarantine_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(IBS.quarantine(td, FP, "why"))

    def test_second_quarantine_does_not_collide(self):
        with tempfile.TemporaryDirectory() as td:
            _store_with_capture(td)
            q1 = IBS.quarantine(td, FP, "first")
            _store_with_capture(td)
            q2 = IBS.quarantine(td, FP, "second")
            self.assertNotEqual(q1, q2)
            self.assertTrue(pathlib.Path(q1).is_dir())
            self.assertTrue(pathlib.Path(q2).is_dir())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd tests && python -m unittest test_initboot_quarantine -v`
Expected: FAIL — `AttributeError: module 'cas.initboot_store' has no attribute 'quarantine'`

- [ ] **Step 3: Write minimal implementation**

Add to `cas/initboot_store.py`, after `put`:

```python
def quarantine(store_root, fingerprint, reason):
    """Move a capture aside so it can never be served again, recording WHY. Returns the quarantine dir,
    or None when there was nothing stored for this build.

    Used when a PROVEN kit contradicts the capture: the capture is then demonstrably not this build's
    factory image (the AIR X case — a valid, Magisk-free, WRONG image that passed both capture guards).
    Moved rather than deleted so the bad image stays available for diagnosis; the directory is renamed
    with a counter suffix so a repeat quarantine can't collide with an earlier one."""
    d = _dir(store_root, fingerprint)
    if not d.is_dir():
        return None
    base = pathlib.Path(store_root) / f"{slug(fingerprint)}.quarantined"
    dest = base
    n = 1
    while dest.exists():
        dest = pathlib.Path(f"{base}.{n}")
        n += 1
    os.replace(d, dest)
    try:
        (dest / "quarantine.json").write_text(json.dumps({
            "fingerprint": fingerprint,
            "reason": str(reason),
            "quarantined_utc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
                "+00:00", "Z"),
        }, indent=2), encoding="utf-8")
    except OSError:
        pass                      # the MOVE is what matters; the note is best-effort
    return dest
```

Add `import datetime` to the imports at the top of `cas/initboot_store.py` if not already present.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd tests && python -m unittest test_initboot_quarantine -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add cas/initboot_store.py tests/test_initboot_quarantine.py
git commit -m "feat(store): quarantine a capture a proven kit contradicts

Moved, not deleted, with the reason recorded — the bad image stays available
for diagnosis while get() reads it as a MISS."
```

---

### Task 3: The factory-image resolver

One resolver returning `(image, provenance)`. Priority: **proven kit** (exact fingerprint match) > **capture** > **library fallback**. `resolve_seal_stock` stays exactly as it is and delegates, so its existing tests keep passing.

**Files:**
- Modify: `cas/provision.py:1957-1967` (add `resolve_factory_init_boot` above `resolve_seal_stock`, rewrite `resolve_seal_stock` as a wrapper)
- Test: `tests/test_factory_image_resolver.py`

**Interfaces:**
- Consumes: `initboot_store.quarantine(store_root, fingerprint, reason)` (Task 2)
- Produces: `provision.resolve_factory_init_boot(library_stock, capture_path, proven_kit_image, fingerprint, log=print, store_root=None) -> (str, str)` where provenance is one of `"proven-kit"`, `"captured"`, `"unverified"`

- [ ] **Step 1: Write the failing test**

Create `tests/test_factory_image_resolver.py`:

```python
# tests/test_factory_image_resolver.py
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import provision as PV
from cas import initboot_store as IBS

FP = "MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/eng.hxh.20260507.141302:user/release-keys"


def _f(td, name, data=b"ANDROID!" + b"\x00" * 64):
    p = pathlib.Path(td) / name
    p.write_bytes(data)
    return str(p)


class TestResolver(unittest.TestCase):
    def test_capture_wins_when_no_kit_is_proven(self):
        """RP6/Thor REGRESSION GUARD: their kits are a different build and are never proven."""
        with tempfile.TemporaryDirectory() as td:
            lib, cap = _f(td, "lib.img"), _f(td, "cap.img", b"ANDROID!" + b"\x11" * 64)
            path, prov = PV.resolve_factory_init_boot(lib, cap, None, FP, log=lambda m: None)
            self.assertEqual(path, cap)
            self.assertEqual(prov, "captured")

    def test_proven_kit_wins_over_a_disagreeing_capture(self):
        with tempfile.TemporaryDirectory() as td:
            lib = _f(td, "lib.img")
            cap = _f(td, "cap.img", b"ANDROID!" + b"\x11" * 64)
            kit = _f(td, "kit.img", b"ANDROID!" + b"\x22" * 64)
            path, prov = PV.resolve_factory_init_boot(lib, cap, kit, FP, log=lambda m: None)
            self.assertEqual(path, kit)
            self.assertEqual(prov, "proven-kit")

    def test_agreeing_capture_is_not_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            same = b"ANDROID!" + b"\x33" * 64
            lib = _f(td, "lib.img")
            cap = _f(td, "cap.img", same)
            kit = _f(td, "kit.img", same)
            store = pathlib.Path(td) / "store"
            (IBS._dir(store, FP)).mkdir(parents=True, exist_ok=True)
            PV.resolve_factory_init_boot(lib, cap, kit, FP, log=lambda m: None, store_root=store)
            self.assertTrue(IBS._dir(store, FP).is_dir(), "identical capture must be kept")

    def test_disagreeing_capture_is_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            lib = _f(td, "lib.img")
            cap = _f(td, "cap.img", b"ANDROID!" + b"\x11" * 64)
            kit = _f(td, "kit.img", b"ANDROID!" + b"\x22" * 64)
            store = pathlib.Path(td) / "store"
            (IBS._dir(store, FP)).mkdir(parents=True, exist_ok=True)
            PV.resolve_factory_init_boot(lib, cap, kit, FP, log=lambda m: None, store_root=store)
            self.assertFalse(IBS._dir(store, FP).is_dir(), "contradicted capture must be moved aside")

    def test_neither_kit_nor_capture_is_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            lib = _f(td, "lib.img")
            path, prov = PV.resolve_factory_init_boot(lib, None, None, FP, log=lambda m: None)
            self.assertEqual(path, lib)
            self.assertEqual(prov, "unverified")

    def test_resolve_seal_stock_signature_unchanged(self):
        """Existing callers/tests must keep working: same args, same single return value."""
        with tempfile.TemporaryDirectory() as td:
            lib, cap = _f(td, "lib.img"), _f(td, "cap.img")
            self.assertEqual(PV.resolve_seal_stock(lib, cap, FP, log=lambda m: None), cap)
            self.assertEqual(PV.resolve_seal_stock(lib, None, FP, log=lambda m: None), lib)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd tests && python -m unittest test_factory_image_resolver -v`
Expected: FAIL — `AttributeError: module 'cas.provision' has no attribute 'resolve_factory_init_boot'`

- [ ] **Step 3: Write minimal implementation**

Replace `cas/provision.py:1957-1967` (the whole existing `resolve_seal_stock`) with:

```python
def resolve_factory_init_boot(library_stock, capture_path, proven_kit_image, fingerprint,
                              log=print, store_root=None):
    """Pick the init_boot seal() flashes to un-root, and say WHERE it came from.

    Returns (path, provenance) with provenance one of:
      'proven-kit' — a kit whose RECORDED build fingerprint equals this unit's. Authoritative: it is
                     the vendor's factory image for this exact build.
      'captured'   — this unit's own captured factory image. The default, and the ONLY correct source
                     when no kit is proven for the build (RP6/Thor: their kits are a DIFFERENT build,
                     so preferring a kit there would flash a wrong-build image and break their OTA).
      'unverified' — neither available; falls back to the model-matched library image and warns.

    When a proven kit and a capture DISAGREE the capture is demonstrably not this build's factory
    image, so it is quarantined: that is the AIR X failure, where a valid, Magisk-free, wrong image
    passed both capture guards and Seal flashed it on every unit of that build."""
    if proven_kit_image:
        if capture_path:
            try:
                same = pathlib.Path(proven_kit_image).read_bytes() == pathlib.Path(capture_path).read_bytes()
            except OSError:
                same = True                       # unreadable => don't destroy evidence on a guess
            if not same:
                log(f"  ⚠ the captured factory init_boot for build {fingerprint} CONTRADICTS the proven "
                    "kit image — quarantining the capture and sealing with the kit.")
                if store_root is not None:
                    try:
                        _ibs.quarantine(store_root, fingerprint, "contradicted by proven kit image")
                    except OSError as e:
                        log(f"  (could not quarantine the capture: {e})")
        log(f"  un-root: restoring the PROVEN factory init_boot for build {fingerprint} "
            "(keeps its device OTA healthy).")
        return str(proven_kit_image), "proven-kit"
    if capture_path:
        log(f"  un-root: restoring this unit's own captured factory init_boot for build {fingerprint} "
            "(keeps its device OTA healthy).")
        return str(capture_path), "captured"
    log(f"  ⚠ no factory init_boot captured for build {fingerprint} — sealing with the library image; "
        "this unit's device OTA may fail (code 20) until it's rooted from a clean state to capture it.")
    return library_stock, "unverified"


def resolve_seal_stock(library_stock, capture_path, fingerprint, log=print):
    """Back-compat wrapper: the pre-provenance two-source resolution, returning just the path.
    Kept so existing callers and tests are untouched; new code should call
    resolve_factory_init_boot() and use the provenance it reports."""
    path, _prov = resolve_factory_init_boot(library_stock, capture_path, None, fingerprint, log=log)
    return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd tests && python -m unittest test_factory_image_resolver -v`
Expected: PASS (6 tests)

Then confirm nothing regressed:

Run: `cd tests && python -m unittest test_seal_capture -v`
Expected: PASS (all existing tests)

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_factory_image_resolver.py
git commit -m "feat(seal): factory-image resolver reports provenance

Priority: proven kit > capture > library. A kit only wins on exact recorded
build-fingerprint match, so RP6/Thor (different-build kits, unproven) keep
using their capture exactly as today. A contradicted capture is quarantined.
resolve_seal_stock keeps its signature and delegates."
```

---

### Task 4: Refuse to capture from a slot while an update is staged

This is the guard that stops a repeat poisoning at source. The AIR X capture was read from slot B minutes after a day of partial OTA writes to it.

**Files:**
- Modify: `cas/provision.py:965-1017` (`capture_factory_init_boot`; add `_update_is_staged` above it)
- Test: `tests/test_capture_staged_update.py`

**Interfaces:**
- Consumes: `adb.su(cmd)` → `(rc, out, err)`, `adb.slot_suffix()`
- Produces: `provision._update_is_staged(adb) -> bool | None` — `True` staged, `False` clean, `None` undeterminable

- [ ] **Step 1: Write the failing test**

Create `tests/test_capture_staged_update.py`:

```python
# tests/test_capture_staged_update.py
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import provision as PV

FP = "MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/eng.hxh.20260507.141302:user/release-keys"


def _adb_with_su(responses, slot="_a"):
    """responses: dict mapping a substring of the command -> (rc, out, err)."""
    adb = mock.Mock()
    adb.slot_suffix.return_value = slot
    adb.boot_partition.return_value = "init_boot"
    adb.getprop.return_value = FP
    adb.serial = "TESTSERIAL"

    def _su(cmd, timeout=900):
        for key, resp in responses.items():
            if key in cmd:
                return resp
        return (1, "", "unknown command")

    adb.su.side_effect = _su
    return adb


class TestUpdateStagedProbe(unittest.TestCase):
    def test_idle_status_is_not_staged(self):
        adb = _adb_with_su({"update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_IDLE", "")})
        self.assertIs(PV._update_is_staged(adb), False)

    def test_need_reboot_status_is_staged(self):
        adb = _adb_with_su({"update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_UPDATED_NEED_REBOOT", "")})
        self.assertIs(PV._update_is_staged(adb), True)

    def test_downloading_status_is_staged(self):
        adb = _adb_with_su({"update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_DOWNLOADING", "")})
        self.assertIs(PV._update_is_staged(adb), True)

    def test_falls_back_to_bootctl_when_update_engine_unavailable(self):
        adb = _adb_with_su({
            "update_engine_client": (127, "", "not found"),
            "bootctl": (0, "0", ""),          # inactive slot NOT marked successful -> staged
        })
        self.assertIs(PV._update_is_staged(adb), True)

    def test_bootctl_successful_slot_is_not_staged(self):
        adb = _adb_with_su({
            "update_engine_client": (127, "", "not found"),
            "bootctl": (0, "1", ""),
        })
        self.assertIs(PV._update_is_staged(adb), False)

    def test_neither_probe_readable_is_undeterminable(self):
        adb = _adb_with_su({})
        self.assertIsNone(PV._update_is_staged(adb))

    def test_su_commands_contain_no_shell_operators(self):
        """adb space-joins argv, so `su -c` must receive ONE command with no &&/||/;."""
        seen = []
        adb = _adb_with_su({"update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_IDLE", "")})
        orig = adb.su.side_effect

        def _record(cmd, timeout=900):
            seen.append(cmd)
            return orig(cmd, timeout)

        adb.su.side_effect = _record
        PV._update_is_staged(adb)
        for cmd in seen:
            for op in ("&&", "||", ";", '"', "'"):
                self.assertNotIn(op, cmd, f"{op!r} in su command {cmd!r}")


class TestCaptureRefusesStagedUpdate(unittest.TestCase):
    def test_capture_skipped_when_update_staged(self):
        adb = _adb_with_su({
            "update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_UPDATED_NEED_REBOOT", ""),
            "dd": (0, "", ""),
        })
        with tempfile.TemporaryDirectory() as td:
            msgs = []
            self.assertFalse(PV.capture_factory_init_boot(adb, td, log=msgs.append))
            self.assertTrue(any("staged" in m or "update" in m for m in msgs), msgs)

    def test_capture_skipped_when_undeterminable(self):
        adb = _adb_with_su({"dd": (0, "", "")})
        with tempfile.TemporaryDirectory() as td:
            msgs = []
            self.assertFalse(PV.capture_factory_init_boot(adb, td, log=msgs.append))

    def test_no_dd_is_issued_when_refused(self):
        adb = _adb_with_su({
            "update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_UPDATED_NEED_REBOOT", ""),
        })
        with tempfile.TemporaryDirectory() as td:
            PV.capture_factory_init_boot(adb, td, log=lambda m: None)
        issued = [c.args[0] for c in adb.su.call_args_list]
        self.assertFalse(any(c.startswith("dd ") for c in issued), issued)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd tests && python -m unittest test_capture_staged_update -v`
Expected: FAIL — `AttributeError: module 'cas.provision' has no attribute '_update_is_staged'`

- [ ] **Step 3: Write minimal implementation**

Add to `cas/provision.py` immediately above `capture_factory_init_boot`:

```python
def _update_is_staged(adb):
    """True when an OTA is in progress or awaiting reboot, False when demonstrably idle, None when
    neither probe can be read.

    The inactive A/B slot is only a source of the FACTORY image while no update has written to it. A
    staged or half-applied payload writes the TARGET slot, so a capture taken then stores the wrong
    image under this build's fingerprint — exactly how the AIR X store was poisoned (captured 18:05,
    minutes after a day of partial OTA writes to slot B).

    Both probes are ONE command each: adb space-joins argv, so `su -c` must never receive `&&`."""
    rc, out, _err = adb.su("update_engine_client --status")
    blob = (out or "").upper()
    if rc == 0 and "UPDATE_STATUS_" in blob:
        return "UPDATE_STATUS_IDLE" not in blob
    slot = (adb.slot_suffix() or "").strip()
    if slot in ("_a", "_b"):
        other = "1" if slot == "_a" else "0"
        rc, out, _err = adb.su(f"bootctl is-slot-marked-successful {other}")
        if rc == 0 and (out or "").strip() in ("0", "1"):
            return (out or "").strip() != "1"
    return None
```

Then in `capture_factory_init_boot`, insert immediately after the `if _ibs.has(store_root, fp): return True` line and **before** `part = adb.boot_partition()`:

```python
    staged = _update_is_staged(adb)
    if staged is not False:
        why = ("an update is staged/partially applied" if staged
               else "could not prove no update is staged")
        log(f"  factory init_boot capture skipped: {why} — the inactive slot may hold OTA-written "
            "data, not the factory image. Absence of a capture is recoverable; a poisoned one is not.")
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd tests && python -m unittest test_capture_staged_update -v`
Expected: PASS (10 tests)

Existing capture tests now need a clean-status adb. Run them and fix any that fail by adding `"update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_IDLE", "")` to their `su` mock:

Run: `cd tests && python -m unittest test_seal_capture -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_capture_staged_update.py tests/test_seal_capture.py
git commit -m "fix(seal): refuse to capture the inactive slot while an update is staged

The slot only holds the factory image while no payload has written to it. This
is how the AIR X store was poisoned: captured minutes after a day of partial
OTA writes to slot B. Undeterminable also refuses — a missing capture is
recoverable, a poisoned one is not."
```

---

### Task 5: Wire the resolver into Lock and report provenance

**Files:**
- Modify: `cas/provision.py` (seal_all worker, the `elif adb.slot_suffix() in ("_a", "_b"):` branch ~line 2303, and the success return ~line 2313)
- Test: `tests/test_lock_provenance.py`

**Interfaces:**
- Consumes: `provision.resolve_factory_init_boot(...)` (Task 3), `firmware.build_fingerprint(fw, version)` (Task 1)
- Produces: seal_all success detail string carries `· ota:<provenance>`

- [ ] **Step 1: Write the failing test**

Create `tests/test_lock_provenance.py`:

```python
# tests/test_lock_provenance.py
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import provision as PV


class TestProvenanceLabel(unittest.TestCase):
    def test_label_appends_provenance(self):
        self.assertEqual(PV._ota_detail("RP6 256", "captured"), "RP6 256 · ota:captured")

    def test_label_handles_empty_profile_name(self):
        self.assertEqual(PV._ota_detail("", "unverified"), "· ota:unverified")

    def test_waiver_label_for_ships_rooted(self):
        self.assertEqual(PV._ota_detail("RP5", "waived-ships-rooted"),
                         "RP5 · ota:waived-ships-rooted")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd tests && python -m unittest test_lock_provenance -v`
Expected: FAIL — `AttributeError: module 'cas.provision' has no attribute '_ota_detail'`

- [ ] **Step 3: Write minimal implementation**

Add to `cas/provision.py` above the `seal_all` definition:

```python
def _ota_detail(profile_name, provenance):
    """Success detail for run history, carrying how the sealed unit's factory image was resolved.
    Appended to the existing detail string rather than added as a new field, so nothing that reads
    run history has to change."""
    return f"{profile_name} · ota:{provenance}".strip()
```

In the seal_all worker, replace the `ships_rooted` / slot branch (currently ~2300-2307) with:

```python
            ota_provenance = "unverified"
            if ships_rooted:
                ota_provenance = "waived-ships-rooted"
                _wlog("SHIPS-ROOTED build: keeping its declared image (not substituting a captured "
                      "factory image, which would un-root the unit and drop the overclock). This unit "
                      "CANNOT take a vendor delta OTA — that is by design, not a fault.")
            elif adb.slot_suffix() in ("_a", "_b"):
                store_root = _ibs.store_root(FW.firmware_root())
                _fp = adb.getprop("ro.build.fingerprint")
                proven_kit = None
                try:
                    if fw is not None and FW.build_fingerprint(fw, fwres.get("version")) == _fp:
                        sb = fw.stock_boot_image(fwres.get("version"))
                        proven_kit = str(sb) if sb else None
                except Exception as e:                 # provenance is additive — never fail the seal
                    _wlog(f"(kit provenance check skipped: {e})")
                stock_path, ota_provenance = resolve_factory_init_boot(
                    stock_path, _ibs.get(store_root, _fp), proven_kit, _fp,
                    log=_wlog, store_root=store_root)
```

`fw` and `fwres` are assigned inside the earlier `try:` block. Initialise them before that block so this branch can't raise `NameError` when the firmware lookup failed — add `fw, fwres = None, {}` immediately before `try:` at the firmware-lookup site (~line 2253).

Then change the success return (currently `return ("ok", prof.name)`) to:

```python
            if ok:
                return ("ok", _ota_detail(prof.name, ota_provenance))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd tests && python -m unittest test_lock_provenance -v`
Expected: PASS (3 tests)

Run the full suite:

Run: `cd tests && python -m unittest discover -p "test_*.py" -v`
Expected: PASS — every test, no regressions

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_lock_provenance.py
git commit -m "feat(lock): use the provenance resolver and report OTA-capability

Lock now records how the factory image was resolved (proven-kit / captured /
unverified / waived-ships-rooted) in the run-history detail, so a unit's
OTA-capability is visible after the fact instead of inferred."
```

---

### Task 6 (operator step, no code): Mark the AIR X kit proven

Not a code task — no test cycle. Everything the proven-kit path needs was built in Tasks 1–5; this records the one fingerprint we have hard evidence for, which is what activates that path for the AIR X build and **only** that build.

Provenance is recorded deliberately, never inferred. A version string resembling a build id (`20260507-165105` vs `AIR_X_user_20260507`) is suggestive and is **not** evidence — the hash below is.

- [ ] **Step 1: Verify the kit image really is this build's factory image**

Run from the repo root:

```bash
python3 - <<'EOF'
import hashlib
from cas import config
B = 4096
p = config.firmware_dir() / "air-x/versions/20260507-165105/payload/emmc/init_boot.img"
d = p.read_bytes()
got = hashlib.sha256(d[0:B] + d[492 * B:493 * B]).hexdigest()
want = "2eb657437ef0672d759e04b6d53268bd717010958c9638a172c6ba0fc62354a2"
print("MATCH" if got == want else "MISMATCH — DO NOT mark proven", got)
EOF
```

Expected output: `MATCH 2eb657437ef0672d...`

(Those are the exact source blocks `update_engine` named for `init_boot` operation 0 — blocks 0 and 492 — so a match means the OTA's source verification will pass against this image.)

- [ ] **Step 2: Record the fingerprint — only if Step 1 printed MATCH**

```bash
python3 -c "
from cas import firmware as FW
print(FW.set_build_fingerprint(
    'air-x', FW.firmware_root(), '20260507-165105',
    'MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/eng.hxh.20260507.141302:user/release-keys'))
"
```

Expected output: the fingerprint echoed back.

- [ ] **Step 3: Confirm it reads back as proven**

```bash
python3 -c "
from cas import firmware as FW
fw = FW.find('air-x', FW.firmware_root())
print('air-x  :', FW.build_fingerprint(fw, '20260507-165105'))
for k in ('retroid-pocket-6', 'ayn-thor', 'odin3'):
    f = FW.find(k, FW.firmware_root())
    print(f'{k:16}:', FW.build_fingerprint(f) if f else '(absent)')
"
```

Expected: `air-x` prints the fingerprint; **RP6, Thor and Odin3 all print `None`**.

Leaving those three unset is not an oversight — their kits are a *different build* than their units (`eng.RP6.20260119` vs kit `RP6_20260115`; `eng.Thor.20260206` vs `Thor_20260112`). Marking them proven would make Seal flash a wrong-build image and break the OTA on the two devices that currently work.

---

## Bench gate (not satisfied by tests)

Nothing here is proven until hardware says so. Required before claiming this works:

1. Root → Save → Download → Lock an AIR X; confirm Lock logs `ota:proven-kit`.
2. Run the vendor update on that sealed unit; confirm `update_engine` reports **0 source-hash failures** and `DownloadAction: ErrorCode::kSuccess`.
3. Repeat Root → Lock on the **RP6** and confirm it still logs `ota:captured` and still updates — this is the regression that matters most.
