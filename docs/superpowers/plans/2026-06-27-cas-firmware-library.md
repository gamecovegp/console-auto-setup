# CAS Firmware Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a labeled, versioned **device-root-firmware** library on the NAS that auto-suggests firmware per connected device (serial-prefix/props + logic-check), lets the operator override/select, and supports drop-in version updates with history — library-only, CAS never flashes.

**Architecture:** A new `cas/firmware.py` mirrors `cas/profiles.py` (directory-backed library, JSON meta). `cas/config.py` gains `firmware_root()` + per-serial `device_firmware` persistence (mirrors `device_profiles`). `cas/adb.py` gains a one-shot `identity()`. CLI + GUI surface the suggestion/override. All matching/ingest logic lives in `firmware.py` so it is unit-testable without a device.

**Tech Stack:** Python 3 stdlib only (json, pathlib, shutil, re), Tkinter (existing GUI), `unittest` (existing test harness, injectable runner).

## Global Constraints

- **Firmware = DEVICE ROOT firmware only** (handheld OS/boot images). Never emulator/app BIOS. Ingest must reject non-device-firmware layouts.
- **CAS never flashes.** Library stores + advises; exposes paths + flash targets only.
- **No symlinks** on the library (CIFS/Windows-safe): `current` is a key in `meta.json`.
- **NAS layout:** `library_root()/_firmware/<id>/versions/<version>/payload/`.
- **Assignment version resolution:** non-pinned assignments use the firmware's `current` at read time (updates propagate); a persisted `version` only exists when the operator pins a rollback.
- **Confirmed labels:** serial `MQ66…`→`mangmi-air-x-mq66` (non-I2C, init_boot/emmc); `MQ65…`→`mangmi-air-x-mq65` (I2C, init_boot/emmc); `ro.product.device=Pocket_Max`→`mangmi-pocket-max` (boot/ufs); AYN→`ayn-m0`/`ayn-m2` (boot/ufs, serial prefixes TBD-deferred).
- **Run tests with:** `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v` (run from repo root).
- **Commits:** conventional commits (`feat(firmware): …`), end body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

- **Create `cas/firmware.py`** — `Firmware` class, `list_firmware`, `find`, `match`, `logic_check`, `ingest`, `resolve`, JSON/version/history helpers. One responsibility: the firmware library + matching.
- **Modify `cas/config.py`** — add `firmware_root()`, `get_device_firmware()`, `set_device_firmware()` (next to the existing `device_profiles` block).
- **Modify `cas/adb.py`** — add `Adb.identity()` (next to `boot_flash_target`).
- **Modify `cas/cli.py`** — add a `firmware` subcommand group (list / ingest / show / assign).
- **Modify `cas/gui.py`** — add a Firmware panel (suggestion + logic-check + override dropdown + Add/update button). UI integration; logic delegated to `firmware.py`.
- **Create `tests/test_firmware.py`** — unit tests for adb.identity, config, match, logic_check, ingest, resolve.

---

### Task 1: `Adb.identity()` — one-shot device identity

**Files:**
- Modify: `cas/adb.py` (add method to `Adb`, after `boot_flash_target`, ~line 215)
- Test: `tests/test_firmware.py`

**Interfaces:**
- Consumes: existing `Adb.getprop`, `Adb.slot_suffix`, `Adb.boot_flash_target`.
- Produces: `Adb.identity() -> dict` with keys `serial, device, model, brand, soc, dev_code, first_api, slot, flash_target` (all str).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_firmware.py
import os, sys, json, pathlib, tempfile, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from cas.adb import Adb


class IdRunner:
    """getprop runner returning a fixed prop table; everything else returns empty ok."""
    def __init__(self, props):
        self.props = props
    def __call__(self, args, input_text=None, timeout=900):
        if "shell" in args and args[-1].startswith("getprop"):
            return 0, (self.props.get(args[-1].split()[-1], "") + "\n"), ""
        return 0, "", ""


AIRX_PROPS = {
    "ro.serialno": "MQ66142509130541", "ro.product.device": "AIR_X",
    "ro.product.model": "AIR X", "ro.product.manufacturer": "MANGMI",
    "ro.soc.model": "SM6115", "ro.mangmi.dev.code": "MQ66",
    "ro.product.first_api_level": "33", "ro.boot.slot_suffix": "_b",
}


class TestIdentity(unittest.TestCase):
    def test_identity_airx(self):
        idn = Adb(runner=IdRunner(AIRX_PROPS)).identity()
        self.assertEqual(idn["serial"], "MQ66142509130541")
        self.assertEqual(idn["device"], "AIR_X")
        self.assertEqual(idn["soc"], "SM6115")
        self.assertEqual(idn["flash_target"], "init_boot_b")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: FAIL — `AttributeError: 'Adb' object has no attribute 'identity'`

- [ ] **Step 3: Add the method to `cas/adb.py`** (inside class `Adb`, after `boot_flash_target`)

```python
    def identity(self):
        """One-shot device identity for firmware/profile auto-assign (getprop, no root). Read while adb is
        up. serial falls back to ro.serialno when this Adb isn't serial-scoped."""
        g = self.getprop
        return {
            "serial": self.serial or g("ro.serialno"),
            "device": g("ro.product.device"),
            "model": g("ro.product.model"),
            "brand": g("ro.product.manufacturer"),
            "soc": g("ro.soc.model"),
            "dev_code": g("ro.mangmi.dev.code"),
            "first_api": g("ro.product.first_api_level"),
            "slot": self.slot_suffix(),
            "flash_target": self.boot_flash_target(),
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/adb.py tests/test_firmware.py
git commit -m "feat(firmware): Adb.identity() one-shot device identity"
```

---

### Task 2: `config` — firmware_root + per-serial device_firmware

**Files:**
- Modify: `cas/config.py` (add after the `device_profiles` block, ~line 161)
- Test: `tests/test_firmware.py`

**Interfaces:**
- Consumes: existing `config.load_config`, `config.save_config`, `config.library_root`.
- Produces:
  - `config.firmware_root() -> pathlib.Path` (= `library_root()/_firmware`)
  - `config.get_device_firmware() -> {serial: {"firmware_id": str, "version": str|None, "manual": bool}}`
  - `config.set_device_firmware(serial, firmware_id, version=None, manual=True)` (falsy `firmware_id` forgets)

- [ ] **Step 1: Write the failing test** (append to `tests/test_firmware.py`)

```python
from cas import config as C


class TestDeviceFirmware(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        os.environ["CAS_PROFILES"] = self.tmp                       # pins library_root() to tmp
    def tearDown(self):
        os.environ.pop("CAS_CONFIG", None); os.environ.pop("CAS_PROFILES", None)

    def test_firmware_root_under_library(self):
        self.assertEqual(C.firmware_root(), pathlib.Path(self.tmp) / "_firmware")

    def test_set_get_roundtrip_and_forget(self):
        C.set_device_firmware("MQ66x", "mangmi-air-x-mq66", manual=True)
        got = C.get_device_firmware()["MQ66x"]
        self.assertEqual(got["firmware_id"], "mangmi-air-x-mq66")
        self.assertTrue(got["manual"])
        self.assertIsNone(got["version"])
        C.set_device_firmware("MQ66x", None)                        # forget
        self.assertNotIn("MQ66x", C.get_device_firmware())

    def test_pinned_version_persists(self):
        C.set_device_firmware("S", "fw", version="20260507-165105", manual=True)
        self.assertEqual(C.get_device_firmware()["S"]["version"], "20260507-165105")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: FAIL — `AttributeError: module 'cas.config' has no attribute 'firmware_root'`

- [ ] **Step 3: Add to `cas/config.py`** (after `set_device_profile`, before the download-history block)

```python
# --- per-device DEVICE-ROOT-FIRMWARE library + assignment (mirrors device_profiles) -------------
def firmware_root():
    """The device-root-firmware library dir: library_root()/_firmware. (NOT emulator BIOS.)"""
    return library_root() / "_firmware"


def get_device_firmware():
    """{serial: {'firmware_id': str, 'version': str|None, 'manual': bool}} — remembered assignments."""
    raw = load_config().get("device_firmware")
    out = {}
    if isinstance(raw, dict):
        for serial, v in raw.items():
            if isinstance(v, dict) and v.get("firmware_id"):
                out[serial] = {"firmware_id": str(v["firmware_id"]),
                               "version": (str(v["version"]) if v.get("version") else None),
                               "manual": bool(v.get("manual"))}
    return out


def set_device_firmware(serial, firmware_id, version=None, manual=True):
    """Remember (firmware_id truthy) or forget (falsy) a device's firmware assignment. `version` is set
    ONLY for an explicit rollback pin; omit it so the firmware's current version propagates."""
    if not serial:
        return
    cfg = load_config()
    df = cfg.get("device_firmware")
    if not isinstance(df, dict):
        df = {}
    if firmware_id:
        rec = {"firmware_id": str(firmware_id), "manual": bool(manual)}
        if version:
            rec["version"] = str(version)
        df[serial] = rec
    else:
        df.pop(serial, None)
    cfg["device_firmware"] = df
    save_config(cfg)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: PASS (3 tests in TestDeviceFirmware)

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_firmware.py
git commit -m "feat(firmware): config.firmware_root + per-serial device_firmware"
```

---

### Task 3: `firmware.py` — Firmware class + list_firmware + find

**Files:**
- Create: `cas/firmware.py`
- Test: `tests/test_firmware.py`

**Interfaces:**
- Produces:
  - `Firmware(path)` with props `.id`, `.label`, `.device`, `.flash_target`, `.storage`; methods `.match_rules() -> dict`, `.current() -> str|None`, `.versions() -> [str]`, `.payload_dir(version=None) -> pathlib.Path|None`.
  - `list_firmware(root) -> [Firmware]` (dirs with `meta.json`)
  - `find(firmware_id, root) -> Firmware|None`
  - helpers `_read_json`, `_write_json` (used by later tasks)

- [ ] **Step 1: Write the failing test** (append)

```python
from cas import firmware as FW


def make_fw(root, fid, device="AIR_X", flash="init_boot", storage="emmc",
            match=None, current="20260507-165105"):
    d = pathlib.Path(root) / fid
    (d / "versions" / current / "payload").mkdir(parents=True)
    FW._write_json(d / "meta.json", {
        "id": fid, "label": fid, "device": device, "flash_target": flash,
        "storage": storage, "match": match or {}, "current": current, "history": []})
    return FW.Firmware(d)


class TestFirmwareClass(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"; self.root.mkdir(parents=True)
    def test_list_and_find_and_props(self):
        make_fw(self.root, "mangmi-air-x-mq66", match={"serial_prefix": ["MQ66"]})
        fws = FW.list_firmware(self.root)
        self.assertEqual([f.id for f in fws], ["mangmi-air-x-mq66"])
        f = FW.find("mangmi-air-x-mq66", self.root)
        self.assertEqual(f.flash_target, "init_boot")
        self.assertEqual(f.current(), "20260507-165105")
        self.assertEqual(f.versions(), ["20260507-165105"])
        self.assertTrue(f.payload_dir().is_dir())
    def test_find_missing(self):
        self.assertIsNone(FW.find("nope", self.root))
    def test_index_json_not_listed_as_firmware(self):
        (self.root / "index.json").write_text("{}")
        make_fw(self.root, "ayn-m0", device="AYN", flash="boot", storage="ufs")
        self.assertEqual([f.id for f in FW.list_firmware(self.root)], ["ayn-m0"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cas.firmware'`

- [ ] **Step 3: Create `cas/firmware.py`**

```python
"""Device-root-firmware library: list / match-by-identity / logic-check / ingest+version / resolve.

A firmware is a directory under `_firmware/<id>/` with:
  meta.json               id, label, device, brand, storage, flash_target, match{}, current, history[]
  versions/<version>/payload/   the firmware tree as-is (emmc|ufs + fh_loader/QSaharaServer/script)
  versions/<version>/version.meta   fingerprint, dev_code, os_version, storage, flash_target, added, source

DEVICE ROOT firmware only (handheld OS/boot images) — never emulator/app BIOS. CAS stores + advises;
it never flashes.
"""
import json
import pathlib
import re
import shutil


def _read_json(p):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return {}


def _write_json(p, obj):
    p = pathlib.Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


class Firmware:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.id = self.path.name
        self.meta = _read_json(self.path / "meta.json")

    @property
    def label(self):
        return self.meta.get("label", self.id)

    @property
    def device(self):
        return self.meta.get("device", "")

    @property
    def flash_target(self):
        return self.meta.get("flash_target", "")

    @property
    def storage(self):
        return self.meta.get("storage", "")

    def match_rules(self):
        m = self.meta.get("match")
        return m if isinstance(m, dict) else {}

    def current(self):
        return self.meta.get("current")

    def versions(self):
        d = self.path / "versions"
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    def payload_dir(self, version=None):
        v = version or self.current()
        if not v:
            return None
        return self.path / "versions" / v / "payload"

    def __repr__(self):
        return f"<Firmware {self.id} device={self.device} flash={self.flash_target}>"


def list_firmware(root):
    """All Firmware under `root` (dirs containing meta.json). index.json + dotfiles ignored."""
    root = pathlib.Path(root)
    if not root.is_dir():
        return []
    return [Firmware(p) for p in sorted(root.iterdir())
            if p.is_dir() and not p.name.startswith(".") and (p / "meta.json").exists()]


def find(firmware_id, root):
    p = pathlib.Path(root) / (firmware_id or "")
    return Firmware(p) if (p / "meta.json").exists() else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): Firmware class + list_firmware + find"
```

---

### Task 4: `firmware.match` — suggestion by identity

**Files:**
- Modify: `cas/firmware.py`
- Test: `tests/test_firmware.py`

**Interfaces:**
- Consumes: `list_firmware`, `Firmware.match_rules`, `Firmware.current`.
- Produces: `match(identity, root) -> (Firmware, version)|None`. Scoring: serial_prefix=3, device=2, brand=1, soc=1; unique top score wins, else `None`.

- [ ] **Step 1: Write the failing test** (append)

```python
class TestMatch(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"; self.root.mkdir(parents=True)
        make_fw(self.root, "mangmi-air-x-mq66",
                match={"serial_prefix": ["MQ66"], "device": "AIR_X", "soc": "SM6115"})
        make_fw(self.root, "mangmi-air-x-mq65",
                match={"serial_prefix": ["MQ65"], "device": "AIR_X", "soc": "SM6115"})
        make_fw(self.root, "mangmi-pocket-max", device="Pocket_Max", flash="boot", storage="ufs",
                match={"device": "Pocket_Max"})

    def test_serial_prefix_splits_airx(self):
        m = FW.match({"serial": "MQ66999", "device": "AIR_X", "soc": "SM6115", "brand": "MANGMI"}, self.root)
        self.assertEqual(m[0].id, "mangmi-air-x-mq66")
        m = FW.match({"serial": "MQ65111", "device": "AIR_X", "soc": "SM6115", "brand": "MANGMI"}, self.root)
        self.assertEqual(m[0].id, "mangmi-air-x-mq65")

    def test_pocket_max_by_device(self):
        m = FW.match({"serial": "PKX1", "device": "Pocket_Max", "brand": "MANGMI"}, self.root)
        self.assertEqual(m[0].id, "mangmi-pocket-max")

    def test_returns_current_version(self):
        m = FW.match({"serial": "MQ66999", "device": "AIR_X", "soc": "SM6115"}, self.root)
        self.assertEqual(m[1], "20260507-165105")

    def test_no_match_returns_none(self):
        self.assertIsNone(FW.match({"serial": "ZZ", "device": "OTHER"}, self.root))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: FAIL — `AttributeError: module 'cas.firmware' has no attribute 'match'`

- [ ] **Step 3: Add to `cas/firmware.py`**

```python
def _serial_prefix_hit(rules, serial):
    return bool(serial) and any(serial.startswith(p) for p in (rules.get("serial_prefix") or []))


def match(identity, root):
    """Suggest a Firmware for a device identity. Score per rule (serial_prefix=3, device=2, brand=1,
    soc=1); the unique highest score wins. Tie or zero -> None (operator selects). Returns
    (Firmware, current_version) or None."""
    serial = identity.get("serial") or ""
    scored = []
    for fw in list_firmware(root):
        r = fw.match_rules()
        score = 0
        if _serial_prefix_hit(r, serial):
            score += 3
        if r.get("device") and r["device"] == identity.get("device"):
            score += 2
        if r.get("brand") and r["brand"].lower() == (identity.get("brand") or "").lower():
            score += 1
        if r.get("soc") and r["soc"] == identity.get("soc"):
            score += 1
        if score > 0:
            scored.append((score, fw))
    if not scored:
        return None
    top = max(s for s, _ in scored)
    winners = [fw for s, fw in scored if s == top]
    if len(winners) != 1:
        return None
    fw = winners[0]
    return (fw, fw.current())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): match() suggestion by device identity"
```

---

### Task 5: `firmware.logic_check` — brick-guard

**Files:**
- Modify: `cas/firmware.py`
- Test: `tests/test_firmware.py`

**Interfaces:**
- Consumes: `Firmware.flash_target`, `Firmware.device`, `Firmware.match_rules`.
- Produces: `logic_check(firmware, identity) -> (ok: bool, warnings: [str])`. Compares partition base of live `flash_target`, device, and serial_prefix.

- [ ] **Step 1: Write the failing test** (append)

```python
class TestLogicCheck(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"; self.root.mkdir(parents=True)
        self.fw = make_fw(self.root, "mangmi-air-x-mq66", device="AIR_X", flash="init_boot",
                          match={"serial_prefix": ["MQ66"], "device": "AIR_X"})

    def test_ok_when_consistent(self):
        ok, warns = FW.logic_check(self.fw, {"serial": "MQ66x", "device": "AIR_X",
                                             "flash_target": "init_boot_b"})
        self.assertTrue(ok); self.assertEqual(warns, [])

    def test_warns_on_partition_mismatch(self):
        ok, warns = FW.logic_check(self.fw, {"serial": "MQ66x", "device": "AIR_X",
                                             "flash_target": "boot_a"})
        self.assertFalse(ok)
        self.assertTrue(any("init_boot" in w and "boot" in w for w in warns))

    def test_warns_on_serial_and_device_mismatch(self):
        ok, warns = FW.logic_check(self.fw, {"serial": "MQ65x", "device": "Pocket_Max",
                                             "flash_target": "init_boot_b"})
        self.assertFalse(ok)
        self.assertEqual(len(warns), 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: FAIL — `AttributeError: module 'cas.firmware' has no attribute 'logic_check'`

- [ ] **Step 3: Add to `cas/firmware.py`**

```python
def _strip_slot(part):
    for suf in ("_a", "_b"):
        if part.endswith(suf):
            return part[:-2]
    return part


def logic_check(firmware, identity):
    """Validate a (suggested or chosen) firmware against the LIVE device. Returns (ok, [warnings]).
    A warned firmware is still selectable — the operator just sees why (brick-guard)."""
    warns = []
    live_base = _strip_slot(identity.get("flash_target") or "")
    if firmware.flash_target and live_base and firmware.flash_target != live_base:
        warns.append(f"firmware expects '{firmware.flash_target}' but device exposes '{live_base}'")
    if firmware.device and identity.get("device") and firmware.device != identity["device"]:
        warns.append(f"firmware device '{firmware.device}' != device '{identity['device']}'")
    prefixes = firmware.match_rules().get("serial_prefix") or []
    serial = identity.get("serial") or ""
    if prefixes and serial and not any(serial.startswith(p) for p in prefixes):
        warns.append(f"serial '{serial}' matches none of {prefixes}")
    return (not warns, warns)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): logic_check brick-guard"
```

---

### Task 6: `firmware.ingest` — detect + version + history + guard

**Files:**
- Modify: `cas/firmware.py`
- Test: `tests/test_firmware.py`

**Interfaces:**
- Consumes: `_read_json`, `_write_json`, `find`.
- Produces:
  - `detect_build(src) -> {storage, flash_target, version, device, dev_code, os_version, fingerprint}`
  - `ingest(src, root, firmware_id=None, label=None, match=None, copy=True) -> Firmware` (raises `ValueError` on device-guard mismatch; idempotent if version already present)

- [ ] **Step 1: Write the failing test** (append)

```python
def fake_build(tmp, name, storage="emmc", with_init_boot=True, device="AIR_X",
               dev_code="MQ66", os_version="1.1.6"):
    """A minimal device-firmware tree: <name>/<storage>/{rawprogram1.xml, super_1.img}."""
    d = pathlib.Path(tmp) / name
    p = d / storage
    p.mkdir(parents=True)
    parts = '<program label="boot_a" /><program label="init_boot_a" />' if with_init_boot \
        else '<program label="boot_a" /><program label="boot_b" />'
    (p / "rawprogram1.xml").write_text(f"<data>{parts}</data>")
    (p / "super_1.img").write_text(
        f"ro.product.system.device={device}\nro.mangmi.dev.code={dev_code}\n"
        f"ro.mangmi.os.version={os_version}\n")
    return d


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"; self.root.mkdir(parents=True)

    def test_detect_build(self):
        src = fake_build(self.tmp, "MANGMI_Vex6115_FlatBuild_TurboX-C6115_xx.xx_la2.0.l.user.20260507.165105")
        d = FW.detect_build(src)
        self.assertEqual(d["storage"], "emmc")
        self.assertEqual(d["flash_target"], "init_boot")
        self.assertEqual(d["version"], "20260507-165105")
        self.assertEqual(d["device"], "AIR_X")
        self.assertEqual(d["dev_code"], "MQ66")
        self.assertEqual(d["os_version"], "1.1.6")

    def test_ingest_creates_version_and_history(self):
        src = fake_build(self.tmp, "MANGMI_Vex6115_FlatBuild_TurboX-C6115_xx.xx_la2.0.l.user.20260507.165105")
        fw = FW.ingest(src, self.root, firmware_id="mangmi-air-x-mq66",
                       match={"serial_prefix": ["MQ66"], "device": "AIR_X"})
        self.assertEqual(fw.current(), "20260507-165105")
        self.assertTrue((fw.payload_dir() / "emmc" / "super_1.img").is_file())
        self.assertEqual(len(fw.meta["history"]), 1)
        self.assertEqual(fw.flash_target, "init_boot")

    def test_ingest_idempotent_same_version(self):
        src = fake_build(self.tmp, "MANGMI_x_la2.0.l.user.20260507.165105")
        FW.ingest(src, self.root, firmware_id="fw")
        fw = FW.ingest(src, self.root, firmware_id="fw")            # no-op re-ingest
        self.assertEqual(len(fw.meta["history"]), 1)

    def test_ingest_second_version_bumps_current_keeps_old(self):
        FW.ingest(fake_build(self.tmp, "a_la2.0.l.user.20260506.000000"), self.root, firmware_id="fw")
        fw = FW.ingest(fake_build(self.tmp, "b_la2.0.l.user.20260507.000000"), self.root, firmware_id="fw")
        self.assertEqual(fw.current(), "20260507-000000")
        self.assertEqual(sorted(fw.versions()), ["20260506-000000", "20260507-000000"])
        self.assertEqual(len(fw.meta["history"]), 2)

    def test_ingest_wrong_device_guard(self):
        FW.ingest(fake_build(self.tmp, "a_la2.0.l.user.20260507.000000", device="AIR_X"),
                  self.root, firmware_id="fw")
        with self.assertRaises(ValueError):
            FW.ingest(fake_build(self.tmp, "b_la2.0.l.user.20260508.000000", device="Pocket_Max"),
                      self.root, firmware_id="fw")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: FAIL — `AttributeError: module 'cas.firmware' has no attribute 'detect_build'`

- [ ] **Step 3: Add to `cas/firmware.py`**

```python
_VERSION_RE = re.compile(r"(\d{8})\.(\d{6})")          # …user.YYYYMMDD.HHMMSS -> groups


def _grep_value(paths, needle, cap=80):
    """First ASCII value after `needle` found scanning files in 1 MiB chunks (best-effort; a match split
    across a chunk boundary is skipped — fine for build.prop-in-image text). '' if not found."""
    nb = needle.encode()
    for p in paths:
        try:
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    i = chunk.find(nb)
                    if i >= 0:
                        j, out = i + len(nb), bytearray()
                        while j < len(chunk) and chunk[j] not in (0, 10, 13) and len(out) < cap:
                            out.append(chunk[j]); j += 1
                        return out.decode("ascii", "ignore").strip()
        except OSError:
            pass
    return ""


def detect_build(src):
    """Inspect a raw firmware build folder and return what we can determine without a network:
    storage (emmc|ufs), flash_target (init_boot|boot), version (YYYYMMDD-HHMMSS), and best-effort
    device / dev_code / os_version / fingerprint from the partition images."""
    src = pathlib.Path(src)
    storage = "emmc" if (src / "emmc").is_dir() else ("ufs" if (src / "ufs").is_dir() else "")
    base = (src / storage) if storage else src
    labels = ""
    for xml in sorted(base.glob("rawprogram*.xml")):
        try:
            labels += xml.read_text(errors="ignore")
        except OSError:
            pass
    flash_target = "init_boot" if "init_boot" in labels else ("boot" if "boot" in labels else "")
    m = _VERSION_RE.search(src.name)
    version = f"{m.group(1)}-{m.group(2)}" if m else src.name
    imgs = sorted(base.glob("super_*.img")) + sorted(base.glob("system_*.img"))
    return {
        "storage": storage,
        "flash_target": flash_target,
        "version": version,
        "device": _grep_value(imgs, "ro.product.system.device="),
        "dev_code": _grep_value(imgs, "ro.mangmi.dev.code="),
        "os_version": _grep_value(imgs, "ro.mangmi.os.version="),
        "fingerprint": _grep_value(imgs, "ro.build.fingerprint="),
    }


def ingest(src, root, firmware_id=None, label=None, match=None, copy=True):
    """Add a raw build folder to the library as a new version. Detects storage/flash_target/version/device,
    copies the tree to versions/<version>/payload, writes version.meta, sets current, appends history.
    Idempotent if the version already exists. Raises ValueError if the detected device contradicts an
    existing firmware id's device (anti-misfile guard). Returns the Firmware."""
    src = pathlib.Path(src)
    root = pathlib.Path(root)
    info = detect_build(src)
    if not info["flash_target"]:
        raise ValueError(f"{src.name}: not a device-firmware build (no boot/init_boot rawprogram labels)")
    fid = firmware_id or f"{(info['device'] or src.name).lower().replace('_', '-')}"
    fw_dir = root / fid
    existing = _read_json(fw_dir / "meta.json")
    if existing.get("device") and info["device"] and existing["device"] != info["device"]:
        raise ValueError(f"device mismatch: id '{fid}' is {existing['device']}, build is {info['device']}")

    version = info["version"]
    vdir = fw_dir / "versions" / version
    if vdir.is_dir():                                   # idempotent
        return Firmware(fw_dir)

    if copy:
        shutil.copytree(src, vdir / "payload")
    else:
        (vdir / "payload").mkdir(parents=True)
    _write_json(vdir / "version.meta" if False else (vdir / "version.meta.json"), {
        "fingerprint": info["fingerprint"], "dev_code": info["dev_code"],
        "os_version": info["os_version"], "storage": info["storage"],
        "flash_target": info["flash_target"], "source": str(src)})

    meta = existing or {"id": fid, "history": []}
    meta.setdefault("history", [])
    meta.update({"id": fid, "label": label or meta.get("label", fid),
                 "device": info["device"] or meta.get("device", ""),
                 "storage": info["storage"] or meta.get("storage", ""),
                 "flash_target": info["flash_target"], "current": version})
    if match:
        meta["match"] = match
    meta.setdefault("match", {})
    meta["history"].append({"version": version, "fingerprint": info["fingerprint"],
                            "os_version": info["os_version"], "source": str(src)})
    _write_json(fw_dir / "meta.json", meta)
    return Firmware(fw_dir)
```

> Note: `version.meta` is written as `version.meta.json` for valid JSON. Update the spec's filename reference accordingly during review.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: PASS (5 tests in TestIngest)

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): ingest with detect/version/history/device-guard"
```

---

### Task 7: `firmware.resolve` — assignment (suggest + override + version)

**Files:**
- Modify: `cas/firmware.py`
- Test: `tests/test_firmware.py`

**Interfaces:**
- Consumes: `cas.config.get_device_firmware`/`set_device_firmware`, `find`, `match`, `logic_check`, `Firmware.current`.
- Produces: `resolve(serial, identity, root) -> {firmware_id, version, manual, suggested, ok, warnings, firmware}`. Manual override wins; else match (and is remembered with manual=False); version = pinned or current.

- [ ] **Step 1: Write the failing test** (append)

```python
class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        self.root = pathlib.Path(self.tmp) / "_firmware"; self.root.mkdir(parents=True)
        make_fw(self.root, "mangmi-air-x-mq66", match={"serial_prefix": ["MQ66"], "device": "AIR_X"})
        make_fw(self.root, "mangmi-air-x-mq65", match={"serial_prefix": ["MQ65"], "device": "AIR_X"})
    def tearDown(self):
        os.environ.pop("CAS_CONFIG", None)

    def _idn(self, serial):
        return {"serial": serial, "device": "AIR_X", "soc": "SM6115",
                "flash_target": "init_boot_b"}

    def test_auto_suggests_and_remembers(self):
        r = FW.resolve("MQ66x", self._idn("MQ66x"), self.root)
        self.assertEqual(r["firmware_id"], "mangmi-air-x-mq66")
        self.assertEqual(r["suggested"], "mangmi-air-x-mq66")
        self.assertFalse(r["manual"]); self.assertTrue(r["ok"])
        self.assertEqual(C.get_device_firmware()["MQ66x"]["firmware_id"], "mangmi-air-x-mq66")

    def test_manual_override_wins(self):
        C.set_device_firmware("MQ66x", "mangmi-air-x-mq65", manual=True)
        r = FW.resolve("MQ66x", self._idn("MQ66x"), self.root)
        self.assertEqual(r["firmware_id"], "mangmi-air-x-mq65")
        self.assertTrue(r["manual"])
        self.assertFalse(r["ok"])                      # logic_check warns: MQ66 serial vs MQ65 firmware

    def test_pinned_version_used(self):
        C.set_device_firmware("S", "mangmi-air-x-mq66", version="20260101-000000", manual=True)
        r = FW.resolve("S", self._idn("S"), self.root)
        self.assertEqual(r["version"], "20260101-000000")

    def test_no_match(self):
        r = FW.resolve("ZZ", {"serial": "ZZ", "device": "OTHER"}, self.root)
        self.assertIsNone(r["firmware_id"]); self.assertFalse(r["ok"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: FAIL — `AttributeError: module 'cas.firmware' has no attribute 'resolve'`

- [ ] **Step 3: Add to `cas/firmware.py`** (top: `from . import config`)

Add the import near the top of the file (after `import shutil`):

```python
from . import config
```

Then append:

```python
def resolve(serial, identity, root):
    """Decide the firmware for a connected device. Manual override (sticky) wins; else match() and
    remember it (manual=False). Version = pinned rollback or the firmware's current. Always runs
    logic_check. Returns a dict the UI/CLI render directly."""
    assigned = config.get_device_firmware().get(serial)
    fw, manual, suggested, pinned = None, False, None, None
    if assigned:
        fw = find(assigned["firmware_id"], root)
        manual = assigned["manual"]
        pinned = assigned.get("version")
    if fw is None:
        m = match(identity, root)
        if m:
            fw, _ = m
            suggested = fw.id
            config.set_device_firmware(serial, fw.id, version=None, manual=False)
    if fw is None:
        return {"firmware_id": None, "version": None, "manual": False, "suggested": None,
                "ok": False, "warnings": ["no match — select manually"], "firmware": None}
    ok, warns = logic_check(fw, identity)
    return {"firmware_id": fw.id, "version": pinned or fw.current(), "manual": manual,
            "suggested": suggested, "ok": ok, "warnings": warns, "firmware": fw}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s tests -p 'test_firmware.py' -t . -v`
Expected: PASS — full suite green.

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): resolve() suggest+override+version assignment"
```

---

### Task 8: CLI — `cas firmware {list,ingest,show,assign}`

**Files:**
- Modify: `cas/cli.py`
- Test: manual (CLI is a thin shell over tested functions)

**Interfaces:**
- Consumes: `cas.config.firmware_root/get_device_firmware/set_device_firmware`, `cas.firmware.list_firmware/ingest/resolve`, `cas.adb.Adb`, `cas.find_adb`.

- [ ] **Step 1: Read `cas/cli.py`** to find how subcommands are registered (the `argparse` subparsers object — likely `sub = parser.add_subparsers(...)`) and how an `Adb` is constructed for the selected device.

- [ ] **Step 2: Add the `firmware` subcommand group.** Register a parser and handler following the file's existing style:

```python
def _cmd_firmware(args):
    from . import config, firmware as fw
    root = config.firmware_root()
    if args.fw_cmd == "list":
        for f in fw.list_firmware(root):
            print(f"{f.id:28} device={f.device:10} flash={f.flash_target:10} current={f.current()}")
    elif args.fw_cmd == "ingest":
        f = fw.ingest(args.src, root, firmware_id=args.id, label=args.label)
        print(f"ingested {f.id} version {f.current()}")
    elif args.fw_cmd in ("show", "assign"):
        a = Adb(serial=args.serial, adb=find_adb("adb"))
        idn = a.identity()
        if args.fw_cmd == "assign":
            config.set_device_firmware(idn["serial"], args.id, manual=True)
        r = fw.resolve(idn["serial"], idn, root)
        print(f"serial={idn['serial']} device={idn['device']} flash_target={idn['flash_target']}")
        print(f"firmware={r['firmware_id']} version={r['version']} manual={r['manual']} ok={r['ok']}")
        for w in r["warnings"]:
            print(f"  ! {w}")
        if r["firmware"]:
            print(f"  payload: {r['firmware'].payload_dir(r['version'])}")
```

Register (adapt names to the file's subparser variable):

```python
    fwp = sub.add_parser("firmware", help="device-root-firmware library (list/ingest/show/assign)")
    fwsub = fwp.add_subparsers(dest="fw_cmd", required=True)
    fwsub.add_parser("list")
    ig = fwsub.add_parser("ingest"); ig.add_argument("src"); ig.add_argument("--id"); ig.add_argument("--label")
    sh = fwsub.add_parser("show"); sh.add_argument("--serial")
    asg = fwsub.add_parser("assign"); asg.add_argument("--serial"); asg.add_argument("id")
    fwp.set_defaults(func=_cmd_firmware)
```

- [ ] **Step 3: Verify manually**

Run: `python3 -m cas firmware list` → prints nothing on an empty library (exit 0).
Run (device connected): `python3 -m cas firmware show` → prints identity + resolved firmware + any warnings.

- [ ] **Step 4: Run the full test suite (no regressions)**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -t . -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add cas/cli.py
git commit -m "feat(firmware): cli firmware list/ingest/show/assign"
```

---

### Task 9: GUI — Firmware panel

**Files:**
- Modify: `cas/gui.py`
- Test: manual (Tkinter; logic already covered by `firmware.py` tests)

**Interfaces:**
- Consumes: `cas.firmware.resolve/list_firmware/ingest`, `cas.config.set_device_firmware`, the GUI's existing selected-device `Adb`/identity.

- [ ] **Step 1: Read `cas/gui.py`** to find the per-device detail area (where model/SD/profile are shown) and the pattern for adding a labeled row + a dropdown (`ttk.Combobox`) + a button.

- [ ] **Step 2: Add a "Firmware" row** to the device detail area that, when a device is selected, calls `firmware.resolve(serial, identity, firmware_root())` and renders:
  - resolved firmware label + version, with a ✓ (ok) or ⚠ + warnings tooltip/label.
  - a `Combobox` listing every `list_firmware(root)` id; selecting one calls `config.set_device_firmware(serial, chosen, manual=True)` then re-resolves and re-renders (sticky override; mirrors the existing profile Assign behavior).
  - the on-disk `payload_dir` path shown as copyable text (operator flashes manually).

```python
# sketch — adapt names to gui.py conventions
from . import firmware as fw, config
def refresh_firmware_row(self):
    idn = self.current_identity()                      # however gui.py exposes the selected device's identity
    r = fw.resolve(idn["serial"], idn, config.firmware_root())
    self.fw_var.set(f"{r['firmware_id'] or '—'}  v{r['version'] or '—'}  "
                    + ("OK" if r["ok"] else "WARN: " + "; ".join(r["warnings"])))
    self.fw_combo["values"] = [f.id for f in fw.list_firmware(config.firmware_root())]
def on_fw_override(self, choice):
    idn = self.current_identity()
    config.set_device_firmware(idn["serial"], choice, manual=True)
    self.refresh_firmware_row()
```

- [ ] **Step 3: Add an "Add / update firmware" button** that opens a folder picker and calls `firmware.ingest(folder, firmware_root())` in a worker thread (reuse the GUI's existing background-task pattern so the multi-GB copy doesn't freeze the UI), then refreshes the row.

- [ ] **Step 4: Verify manually** — launch the GUI, select the connected MQ66 unit, confirm the Firmware row suggests `mangmi-air-x-mq66` with OK, the dropdown lists all firmwares, an override sticks across reselect, and the payload path is shown.

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py
git commit -m "feat(firmware): GUI firmware panel (suggest/override/ingest)"
```

---

### Task 10: Seed the five firmwares into the library

**Files:** none (operator/bench step using Task 8's CLI)

- [ ] **Step 1: Ingest each build** (on a bench with the NAS mounted, or set `CAS_PROFILES` to the library):

```bash
cd "<repo>"
B="device-firmwares"
python3 -m cas firmware ingest "$B/MANGMI_Vex6115_FlatBuild_TurboX-C6115_xx.xx_la2.0.l.user.20260507.165105" \
    --id mangmi-air-x-mq66 --label "MANGMI AIR X (MQ66, non-I2C)"
python3 -m cas firmware ingest "$B/MANGMI_Vex6115_I2C_FlatBuild_TurboX-C6115_xx.xx_la2.0.l.user.20260506.192132" \
    --id mangmi-air-x-mq65 --label "MANGMI AIR X (MQ65, I2C)"
python3 -m cas firmware ingest "$B/MANGMI_VIEGO_07_FlatBuild_TurboX_C2130_xx.xx_LA1.2.l.user.20260316.183818" \
    --id mangmi-pocket-max --label "MANGMI Pocket Max"
python3 -m cas firmware ingest "$B/AYN_ufs_M0_user" --id ayn-m0 --label "AYN (M0)"
python3 -m cas firmware ingest "$B/AYN_ufs_M2_user" --id ayn-m2 --label "AYN (M2)"
```

- [ ] **Step 2: Add match rules** (ingest seeds device/storage/flash_target; set the serial-prefix/device rules each `meta.json` needs). Edit `_firmware/<id>/meta.json` `match`:
  - `mangmi-air-x-mq66`: `{"serial_prefix": ["MQ66"], "device": "AIR_X", "soc": "SM6115"}`
  - `mangmi-air-x-mq65`: `{"serial_prefix": ["MQ65"], "device": "AIR_X", "soc": "SM6115"}`
  - `mangmi-pocket-max`: `{"device": "Pocket_Max"}`
  - `ayn-m0` / `ayn-m2`: `{"brand": "AYN", "serial_prefix": [<TBD M0/M2 prefixes>]}` — **deferred**: capture the real prefixes from an untouched AYN unit, then fill. Until filled, AYN auto-suggest returns None and the operator selects via override (intended fallback).

- [ ] **Step 3: Verify** — `python3 -m cas firmware list` shows all five with correct device/flash/current; with the MQ66 unit connected, `python3 -m cas firmware show` resolves `mangmi-air-x-mq66`, `ok=True`.

---

## Self-Review

**1. Spec coverage:**
- §3 storage layout → Tasks 3, 6 (dirs/meta/versions/payload). ✓
- §4 identity/match/logic_check/resolve → Tasks 1, 4, 5, 7. ✓
- §5 easy update/ingest → Task 6 + Task 10. ✓
- §6 history → Task 6 (meta.history + version.meta) ✓; **assignment audit jsonl log NOT yet a task** — see gap below.
- §7 code touch points → Tasks 1–3, 8, 9. ✓
- §8 testing → Tasks 1–7 each TDD. ✓
- §9 open items → AYN deferred (Task 10 Step 2), root-flow deferred. ✓

**Gap found & resolved:** §6's assignment audit `firmware-history.jsonl` had no task. It is low-risk and depends on `config.history_dir`. **Added as Task 7b below** rather than expanding Task 7.

**2. Placeholder scan:** AYN serial prefixes are an explicit deferred data item (spec §9), not a code placeholder — the matcher ships complete. No code TODOs.

**3. Type consistency:** `identity` dict keys (`serial/device/brand/soc/flash_target`) consistent across Tasks 1,4,5,7. `match()` returns `(Firmware, version)`; `resolve()` returns the documented dict; `ingest()` returns `Firmware`. `version.meta` is written as `version.meta.json` (Task 6 note) — flagged for a one-line spec fix.

---

### Task 7b: Assignment audit log (`firmware-history.jsonl`)

**Files:**
- Modify: `cas/firmware.py`
- Test: `tests/test_firmware.py`

**Interfaces:**
- Consumes: `cas.config.history_dir`.
- Produces: `log_event(serial, firmware_id, version, action, manual, when=None)` appending one JSON line to `history_dir()/firmware-history.jsonl`. Called by `resolve` (action="suggest"/"assign") and `ingest` (action="update").

- [ ] **Step 1: Write the failing test** (append)

```python
class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        os.environ["CAS_PROFILES"] = self.tmp
    def tearDown(self):
        os.environ.pop("CAS_CONFIG", None); os.environ.pop("CAS_PROFILES", None)
    def test_log_event_appends_jsonl(self):
        FW.log_event("S1", "fw", "v1", "assign", True, when="2026-06-27 12:00")
        FW.log_event("S2", "fw2", "v2", "update", False, when="2026-06-27 12:01")
        p = pathlib.Path(C.history_dir()) / "firmware-history.jsonl"
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["serial"], "S1")
        self.assertEqual(lines[1]["action"], "update")
```

- [ ] **Step 2: Run test to verify it fails** → `AttributeError: ... 'log_event'`.

- [ ] **Step 3: Add to `cas/firmware.py`**

```python
def log_event(serial, firmware_id, version, action, manual, when=None):
    """Append one audit line to history_dir()/firmware-history.jsonl. Best-effort (never raises)."""
    try:
        if when is None:
            import datetime
            when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        rec = {"when": when, "serial": serial, "firmware_id": firmware_id,
               "version": version, "action": action, "manual": bool(manual)}
        p = pathlib.Path(config.history_dir()) / "firmware-history.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
```

- [ ] **Step 4: Run the test to verify it passes.**

- [ ] **Step 5: Wire calls + commit.** In `resolve`, after a successful suggest call `log_event(serial, fw.id, fw.current(), "suggest", False)`; on manual path the CLI/GUI `assign` calls `log_event(..., "assign", True)`. In `ingest`, before returning the new version, call `log_event("", fid, version, "update", False)`.

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): assignment/update audit jsonl log"
```

---

## Execution Handoff

(see skill — offered after plan approval)
