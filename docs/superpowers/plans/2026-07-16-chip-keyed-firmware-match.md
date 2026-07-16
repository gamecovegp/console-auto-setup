# Chip-Keyed Firmware Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Match a device to a firmware by a `chip + android + storage` boolean gate evaluated *before* scoring, so same-chip units auto-resolve and the always-true device-inequality warning can be deleted.

**Architecture:** `cas/firmware.py` gains a pure `gate_check()` that rejects a firmware only on a **known conflict** (same field populated on both sides, values differ) and never on missing data. `match()` filters through the gate, then scores survivors with the existing soft rules. `identity()` and `detect_build()` grow the fields the gate compares. Everything is pure and table-tested; the only `provision.py` change is one call added at an existing success branch.

**Tech Stack:** Python 3, `unittest` (not pytest), no new dependencies.

## Global Constraints

- **Core rule — reject only on a KNOWN CONFLICT, never on missing data.** An axis gates only when the same field is populated on **both** sides and the values differ. Absence on either side means the axis abstains. This is what lets today's chip-less `meta.json` entries keep resolving unchanged.
- **Never compare `ro.board.platform` against `ro.soc.model`.** `kalama` and `SM8550` name the same silicon; a cross-prop compare reads as a conflict and would disqualify the entire library. Compare `board_platform` to `board_platform` and `soc` to `soc`, independently.
- **`_img_kernel_size()` in `cas/provision.py:1449` must not be touched, moved, or referenced by this logic.** Matching is a heuristic; the kernel-size check is physics. They must not share a failure mode. Its existing tests must pass unmodified.
- **Tests are `unittest`**, run via `python3 -m pytest tests/test_firmware.py -v` (pytest is the runner; the tests themselves are `unittest.TestCase`).
- **`CAS_CONFIG` isolation is mandatory** for any test touching `resolve()` / `set_device_firmware()`. `tests/test_firmware.py` has a module-level `setUpModule` backstop — do not remove it, and do not `pop()` `CAS_CONFIG` bare in a `tearDown` (that re-opens the bug commit 1492ce8 closed: the suite writing the operator's real, gitignored `cas-config.json`).
- Android compares **major only**: `13` vs `13.1` are equal; `13` vs `14` conflict.
- Existing helpers to reuse, not reinvent: `_read_json`, `_write_json`, `_grep_value`, `log_event`.

---

### Task 1: `identity()` carries the gate props

**Files:**
- Modify: `cas/firmware.py` — `identity()` (~line 40)
- Test: `tests/test_firmware.py` — `TestIdentity`

**Interfaces:**
- Consumes: nothing.
- Produces: `identity()` dict gains three string keys — `board_platform`, `android_release`, `bootdevice`. Existing keys (`serial`, `device`, `model`, `brand`, `soc`, `dev_code`, `first_api`, `slot`, `flash_target`) are unchanged. Every later task reads these three.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_firmware.py`. Note `AIRX_PROPS` (module level, ~line 58) must gain the three new props — add them there:

```python
AIRX_PROPS = {
    "ro.serialno": "MQ66142509130541", "ro.product.device": "AIR_X",
    "ro.product.model": "AIR X", "ro.product.manufacturer": "MANGMI",
    "ro.soc.model": "SM6115", "ro.mangmi.dev.code": "MQ66",
    "ro.product.first_api_level": "33", "ro.boot.slot_suffix": "_b",
    "ro.board.platform": "bengal", "ro.build.version.release": "14",
    "ro.boot.bootdevice": "4804000.sdhci",
}
```

Then add to `class TestIdentity`:

```python
    def test_identity_carries_gate_props(self):
        idn = FW.identity(Adb(runner=IdRunner(AIRX_PROPS)))
        self.assertEqual(idn["board_platform"], "bengal")
        self.assertEqual(idn["android_release"], "14")
        self.assertEqual(idn["bootdevice"], "4804000.sdhci")
        self.assertEqual(idn["soc"], "SM6115")      # unchanged

    def test_identity_gate_props_absent_are_empty_not_missing(self):
        # A device that doesn't report them must yield '' (abstain), never a KeyError.
        idn = FW.identity(Adb(runner=IdRunner({"ro.serialno": "X"})))
        self.assertEqual(idn["board_platform"], "")
        self.assertEqual(idn["android_release"], "")
        self.assertEqual(idn["bootdevice"], "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestIdentity -v`
Expected: FAIL with `KeyError: 'board_platform'`

- [ ] **Step 3: Write minimal implementation**

In `cas/firmware.py`, `identity()` — add three lines to the returned dict:

```python
def identity(adb):
    """One-shot device identity for auto-assign (getprop, no root). Calls adb.slot_suffix()/
    adb.boot_flash_target() which already exist on Adb.

    board_platform/android_release/bootdevice feed gate_check(). They are best-effort: a device that
    doesn't report one yields '', which makes that gate axis ABSTAIN rather than reject."""
    g = adb.getprop
    return {
        "serial": adb.serial or g("ro.serialno"),
        "device": g("ro.product.device"),
        "model": g("ro.product.model"),
        "brand": g("ro.product.manufacturer"),
        "soc": g("ro.soc.model"),
        "board_platform": g("ro.board.platform"),
        "android_release": g("ro.build.version.release"),
        "bootdevice": g("ro.boot.bootdevice"),
        "dev_code": g("ro.mangmi.dev.code"),
        "first_api": g("ro.product.first_api_level"),
        "slot": adb.slot_suffix(),
        "flash_target": adb.boot_flash_target(),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -v`
Expected: PASS, all existing tests still green.

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): identity() carries board_platform/android_release/bootdevice"
```

---

### Task 2: `_storage_from_bootdevice()` — the unverified axis, isolated

**Files:**
- Modify: `cas/firmware.py` — new module-level helper, place directly above `match()`
- Test: `tests/test_firmware.py` — new `TestStorageProbe`

**Interfaces:**
- Consumes: nothing.
- Produces: `_storage_from_bootdevice(bootdevice: str) -> str` returning `"ufs"` | `"emmc"` | `""`. `""` means unrecognized → the storage axis abstains. Task 5's `gate_check()` calls it.

> **Reviewer note:** this mapping is the one **unverified** part of the design — `ro.boot.bootdevice` has not been read off a real RP6 or AIR X. It is isolated in its own function and returns `""` on anything unrecognized, so a wrong guess degrades to "storage doesn't gate" (legacy behavior), never to a wrong flash.

- [ ] **Step 1: Write the failing test**

```python
class TestStorageProbe(unittest.TestCase):
    """ro.boot.bootdevice -> 'ufs'|'emmc'|''. UNVERIFIED against real hardware: the '' fallback is what
    makes a wrong guess safe (unrecognized -> axis abstains -> legacy behavior, never a wrong flash)."""

    def test_ufs_controller(self):
        self.assertEqual(FW._storage_from_bootdevice("1d84000.ufshc"), "ufs")

    def test_emmc_sdhci_controller(self):
        self.assertEqual(FW._storage_from_bootdevice("4804000.sdhci"), "emmc")

    def test_emmc_mmc_controller(self):
        self.assertEqual(FW._storage_from_bootdevice("7c4000.mmc0"), "emmc")

    def test_case_insensitive(self):
        self.assertEqual(FW._storage_from_bootdevice("1D84000.UFSHC"), "ufs")

    def test_unknown_returns_empty_so_axis_abstains(self):
        self.assertEqual(FW._storage_from_bootdevice("something.weird"), "")

    def test_none_and_empty_return_empty(self):
        self.assertEqual(FW._storage_from_bootdevice(None), "")
        self.assertEqual(FW._storage_from_bootdevice(""), "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestStorageProbe -v`
Expected: FAIL with `AttributeError: module 'cas.firmware' has no attribute '_storage_from_bootdevice'`

- [ ] **Step 3: Write minimal implementation**

```python
def _storage_from_bootdevice(bootdevice):
    """'ufs' | 'emmc' | '' from ro.boot.bootdevice (e.g. '1d84000.ufshc' -> 'ufs', '4804000.sdhci' ->
    'emmc'). '' = unrecognized, which makes the storage gate axis ABSTAIN.

    UNVERIFIED against real hardware — the '' fallback is deliberate: a wrong guess here degrades to
    'storage does not gate' (legacy behavior), never to a wrong flash."""
    b = (bootdevice or "").strip().lower()
    if "ufs" in b:
        return "ufs"
    if "sdhci" in b or "mmc" in b:
        return "emmc"
    return ""


def _android_major(release):
    """'13.1' -> '13'; '' -> ''. Android gates on MAJOR only."""
    return str(release or "").strip().split(".")[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_firmware.py::TestStorageProbe -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): _storage_from_bootdevice + _android_major gate helpers"
```

---

### Task 3: `detect_build()` extracts chip + android from the build

**Files:**
- Modify: `cas/firmware.py` — `detect_build()` (~line 400)
- Test: `tests/test_firmware.py` — extend `fake_build()` helper + `TestIngest`

**Interfaces:**
- Consumes: nothing.
- Produces: `detect_build()` dict gains `board_platform`, `soc`, `android_release` (all `str`, `""` when undetectable). Task 4 seeds them into `match{}`.

- [ ] **Step 1: Write the failing test**

First extend the `fake_build()` helper (~line 76) so its `super_1.img` carries the new props:

```python
def fake_build(tmp, name, storage="emmc", with_init_boot=True, device="AIR_X",
               dev_code="MQ66", os_version="1.1.6", board_platform="bengal",
               soc="SM6115", android="14"):
    """A minimal device-firmware tree: <name>/<storage>/{rawprogram1.xml, super_1.img}.
    board_platform/soc/android are written into super_1.img so detect_build() can grep them; pass '' to
    simulate a build whose props are undetectable (the legacy-entry case)."""
    d = pathlib.Path(tmp) / name
    p = d / storage
    p.mkdir(parents=True)
    parts = '<program label="boot_a" /><program label="init_boot_a" />' if with_init_boot \
        else '<program label="boot_a" /><program label="boot_b" />'
    (p / "rawprogram1.xml").write_text(f"<data>{parts}</data>")
    props = (f"ro.product.system.device={device}\nro.mangmi.dev.code={dev_code}\n"
             f"ro.mangmi.os.version={os_version}\n")
    if board_platform:
        props += f"ro.board.platform={board_platform}\n"
    if soc:
        props += f"ro.soc.model={soc}\n"
    if android:
        props += f"ro.build.version.release={android}\n"
    (p / "super_1.img").write_text(props)
    return d
```

Then add to `class TestIngest`:

```python
    def test_detect_build_extracts_gate_fields(self):
        src = fake_build(self.tmp, "b-20260507.165105", board_platform="kalama",
                         soc="SM8550", android="13")
        info = FW.detect_build(src)
        self.assertEqual(info["board_platform"], "kalama")
        self.assertEqual(info["soc"], "SM8550")
        self.assertEqual(info["android_release"], "13")

    def test_detect_build_undetectable_gate_fields_are_empty(self):
        # A build whose props don't grep out must yield '' — the legacy entry stays legacy, no raise.
        src = fake_build(self.tmp, "c-20260507.165105", board_platform="", soc="", android="")
        info = FW.detect_build(src)
        self.assertEqual(info["board_platform"], "")
        self.assertEqual(info["soc"], "")
        self.assertEqual(info["android_release"], "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestIngest -v`
Expected: FAIL with `KeyError: 'board_platform'`

- [ ] **Step 3: Write minimal implementation**

In `detect_build()`, add three entries to the returned dict, reusing the existing `_grep_value(imgs, ...)` pattern already used for `ro.build.fingerprint=`:

```python
    return {
        "storage": storage,
        "flash_target": flash_target,
        "flash_method": "edl" if is_edl else "fastboot",
        "version": version,
        "device": _grep_value(imgs, "ro.product.system.device="),
        "dev_code": _grep_value(imgs, "ro.mangmi.dev.code="),
        "os_version": _grep_value(imgs, "ro.mangmi.os.version="),
        "fingerprint": _grep_value(imgs, "ro.build.fingerprint="),
        # Gate fields. Both chip spellings are captured: gate_check() compares board_platform to
        # board_platform and soc to soc, never across ('kalama' != 'SM8550' would false-conflict).
        "board_platform": _grep_value(imgs, "ro.board.platform="),
        "soc": _grep_value(imgs, "ro.soc.model="),
        "android_release": _grep_value(imgs, "ro.build.version.release="),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -v`
Expected: PASS, all existing tests still green.

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): detect_build() extracts board_platform/soc/android_release"
```

---

### Task 4: `ingest()` seeds the gate fields — the zero-knowledge operator path

**Files:**
- Modify: `cas/firmware.py` — `ingest()` (~line 440)
- Test: `tests/test_firmware.py` — `TestIngest`

**Interfaces:**
- Consumes: `detect_build()`'s `board_platform` / `soc` / `android_release` (Task 3).
- Produces: `meta.json` `match{}` gains optional keys `board_platform`, `soc`, `android_release`; `version.meta.json` records the same three. Task 5's `gate_check()` reads them via `Firmware.match_rules()`.

> This is the task that answers "how do I set it for a new chip?" — **the operator never types a chip codename.** The build self-describes on ingest, exactly as it already does for `device` / `storage` / `flash_target`.

- [ ] **Step 1: Write the failing test**

```python
    def test_ingest_seeds_gate_fields_with_no_caller_input(self):
        # The zero-knowledge operator path: ingest a build, its chip rules populate themselves.
        src = fake_build(self.tmp, "odin2-20260507.165105", board_platform="kalama",
                         soc="SM8550", android="13")
        fw = FW.ingest(src, self.root, firmware_id="ayn-odin2")
        r = fw.match_rules()
        self.assertEqual(r["board_platform"], "kalama")
        self.assertEqual(r["soc"], "SM8550")
        self.assertEqual(r["android_release"], "13")

    def test_ingest_undetectable_chip_leaves_entry_legacy_and_does_not_raise(self):
        src = fake_build(self.tmp, "legacy-20260507.165105", board_platform="", soc="", android="")
        fw = FW.ingest(src, self.root, firmware_id="legacy-fw")
        r = fw.match_rules()
        self.assertNotIn("board_platform", r)
        self.assertNotIn("android_release", r)

    def test_ingest_does_not_clobber_caller_supplied_match_rules(self):
        src = fake_build(self.tmp, "odin2b-20260507.165105", board_platform="kalama",
                         soc="SM8550", android="13")
        fw = FW.ingest(src, self.root, firmware_id="ayn-odin2b",
                       match={"serial_prefix": ["AYN"]})
        r = fw.match_rules()
        self.assertEqual(r["serial_prefix"], ["AYN"])       # caller's rule survives
        self.assertEqual(r["board_platform"], "kalama")     # detection fills the rest
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestIngest -v`
Expected: FAIL with `KeyError: 'board_platform'`

- [ ] **Step 3: Write minimal implementation**

In `ingest()`, extend the `version.meta.json` write:

```python
    _write_json(vdir / "version.meta.json", {
        "fingerprint": info["fingerprint"],
        "dev_code": info["dev_code"],
        "os_version": info["os_version"],
        "storage": info["storage"],
        "flash_target": info["flash_target"],
        "flash_method": info["flash_method"],
        "board_platform": info["board_platform"],
        "soc": info["soc"],
        "android_release": info["android_release"],
        "source": str(src),
    })
```

Then extend the existing match-seeding block (which currently only fills `device`):

```python
    # Seed match rules so a freshly-ingested firmware auto-matches immediately: start from the caller's
    # rules (e.g. serial_prefix for the MQ65/MQ66 split — both report device AIR_X), then fill from
    # detection whatever the caller didn't set. Without this a GUI ingest produced an empty match{} and
    # nothing ever auto-matched.
    #
    # The gate fields land here too — this is the zero-knowledge operator path: the build self-describes
    # its chip/android, so adding a NEW chip (e.g. the Odin 3 'sun' build) needs no operator input at
    # all. A field detection couldn't read stays absent, which makes that gate axis abstain (the core
    # rule: missing data never rejects, and never promotes).
    m = dict(match) if match else dict(meta.get("match") or {})
    for key in ("device", "board_platform", "soc", "android_release"):
        if info.get(key) and not m.get(key):
            m[key] = info[key]
    meta["match"] = m
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -v`
Expected: PASS, all existing tests still green.

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): ingest() seeds gate fields from the build (no operator input)"
```

---

### Task 5: `gate_check()` — the core rule

**Files:**
- Modify: `cas/firmware.py` — new function, place directly above `match()` (below `_serial_prefix_hit`)
- Test: `tests/test_firmware.py` — new `TestGateCheck`

**Interfaces:**
- Consumes: `_storage_from_bootdevice()`, `_android_major()` (Task 2); `Firmware.match_rules()`, `Firmware.storage`.
- Produces: `gate_check(firmware, identity_dict) -> (ok: bool, reason: str|None, agreed: int)`. `agreed` counts axes that **compared and agreed**. Task 6's `match()` and Task 8's `resolve()` both consume all three values.

> **`agreed` is the subtle part and the whole feature hinges on it.** An RP6 matched against the Odin 2 build scores **zero** on the soft rules (no serial hit, `device` differs, `brand` differs). Without `agreed`, `match()`'s `if score > 0` would discard it and the feature would fail on its motivating case. `agreed > 0` = the gate positively affirmed compatibility → candidate at score 0. `agreed == 0` = every axis abstained (legacy entry) → still needs a positive score, exactly as today.

- [ ] **Step 1: Write the failing test**

```python
class TestGateCheck(unittest.TestCase):
    """CORE RULE: reject only on a KNOWN CONFLICT (same field populated both sides, values differ);
    never on missing data."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        self.root.mkdir(parents=True)

    def _fw(self, fid="ayn-odin2", storage="ufs", **rules):
        return make_fw(self.root, fid, device="odin2", storage=storage, match=rules)

    def _rp6(self, **over):
        idn = {"serial": "RP6x", "device": "RP6", "brand": "Retroid", "board_platform": "kalama",
               "soc": "SM8550", "android_release": "13", "bootdevice": "1d84000.ufshc"}
        idn.update(over)
        return idn

    # --- the motivating case ---------------------------------------------------------------------
    def test_proven_cross_brand_pair_passes_and_is_affirmed(self):
        # RP6 on the Odin 2 build: known to boot. Must PASS and must be AFFIRMED (agreed>0), or
        # match() would discard it at score 0.
        fw = self._fw(board_platform="kalama", soc="SM8550", android_release="13")
        ok, reason, agreed = FW.gate_check(fw, self._rp6())
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertGreater(agreed, 0)

    # --- known conflicts reject ------------------------------------------------------------------
    def test_chip_conflict_rejects(self):
        fw = self._fw(board_platform="sun")                      # Odin 3 build
        ok, reason, agreed = FW.gate_check(fw, self._rp6())      # kalama unit
        self.assertFalse(ok)
        self.assertIn("kalama", reason)

    def test_soc_conflict_rejects(self):
        fw = self._fw(soc="SM8750")
        ok, reason, _ = FW.gate_check(fw, self._rp6())
        self.assertFalse(ok)
        self.assertIn("SM8550", reason)

    def test_android_major_conflict_rejects(self):
        fw = self._fw(board_platform="kalama", android_release="15")
        ok, reason, _ = FW.gate_check(fw, self._rp6())
        self.assertFalse(ok)
        self.assertIn("android", reason)

    def test_storage_conflict_rejects(self):
        fw = self._fw(storage="emmc", board_platform="kalama")   # ufs unit
        ok, reason, _ = FW.gate_check(fw, self._rp6())
        self.assertFalse(ok)
        self.assertIn("storage", reason)

    # --- missing data ABSTAINS (never rejects) ----------------------------------------------------
    def test_legacy_entry_with_no_chip_abstains_vacuously(self):
        fw = self._fw(storage="")                                # today's meta.json: no gate fields
        ok, reason, agreed = FW.gate_check(fw, self._rp6())
        self.assertTrue(ok)
        self.assertEqual(agreed, 0)                              # vacuous: affirmed nothing

    def test_device_not_reporting_props_abstains(self):
        fw = self._fw(board_platform="kalama", android_release="13")
        ok, _, agreed = FW.gate_check(fw, self._rp6(board_platform="", android_release="",
                                                    soc="", bootdevice=""))
        self.assertTrue(ok)
        self.assertEqual(agreed, 0)

    def test_unrecognized_bootdevice_makes_storage_abstain_not_reject(self):
        fw = self._fw(storage="emmc", board_platform="kalama")
        ok, _, _ = FW.gate_check(fw, self._rp6(bootdevice="something.weird"))
        self.assertTrue(ok)                                      # storage abstained, chip agreed

    # --- the cross-prop trap ----------------------------------------------------------------------
    def test_never_compares_board_platform_against_soc(self):
        # fw records only soc; device reports only board_platform. 'kalama' vs 'SM8550' must NOT
        # be read as a conflict — they name the same silicon.
        fw = self._fw(soc="SM8550")
        ok, _, agreed = FW.gate_check(fw, self._rp6(soc=""))
        self.assertTrue(ok)
        self.assertEqual(agreed, 0)

    # --- comparison semantics ---------------------------------------------------------------------
    def test_android_minor_does_not_conflict(self):
        fw = self._fw(board_platform="kalama", android_release="13")
        ok, _, _ = FW.gate_check(fw, self._rp6(android_release="13.1"))
        self.assertTrue(ok)

    def test_chip_compare_is_case_insensitive(self):
        fw = self._fw(board_platform="KALAMA")
        ok, _, agreed = FW.gate_check(fw, self._rp6())
        self.assertTrue(ok)
        self.assertGreater(agreed, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestGateCheck -v`
Expected: FAIL with `AttributeError: module 'cas.firmware' has no attribute 'gate_check'`

- [ ] **Step 3: Write minimal implementation**

```python
def gate_check(firmware, identity_dict):
    """Hard compatibility gate, evaluated BEFORE scoring. Returns (ok, reason, agreed).

    CORE RULE: reject only on a KNOWN CONFLICT — never on missing data. An axis gates only when the
    SAME field is populated on BOTH sides and the values differ; absence on either side abstains. That
    is what lets today's chip-less meta.json entries keep resolving exactly as they always have.

    NEVER compare ro.board.platform against ro.soc.model. 'kalama' and 'SM8550' name the same silicon,
    so a cross-prop compare would read as a conflict and disqualify the whole library. Each chip prop is
    compared only against its own counterpart.

    `agreed` = how many axes actually COMPARED AND AGREED (as opposed to abstaining). agreed>0 is a
    positive affirmation of compatibility and makes a firmware a candidate even at score 0 — which is
    what makes cross-model reuse work at all (an RP6 on the Odin 2 build scores zero on every soft
    rule). agreed==0 is a vacuous pass: the gate affirmed nothing, so match() still requires a positive
    score, preserving today's behavior for un-backfilled entries.
    """
    r = firmware.match_rules()
    agreed = 0

    for key in ("board_platform", "soc"):
        want, live = r.get(key), identity_dict.get(key)
        if want and live:
            if want.strip().lower() != live.strip().lower():
                return (False, f"chip {live} != firmware {want}", agreed)
            agreed += 1

    want_a, live_a = r.get("android_release"), identity_dict.get("android_release")
    if want_a and live_a:
        if _android_major(want_a) != _android_major(live_a):
            return (False, f"android {live_a} != firmware {want_a}", agreed)
        agreed += 1

    want_s = firmware.storage
    live_s = _storage_from_bootdevice(identity_dict.get("bootdevice"))
    if want_s and live_s:
        if want_s.strip().lower() != live_s:
            return (False, f"storage {live_s} != firmware {want_s}", agreed)
        agreed += 1

    return (True, None, agreed)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_firmware.py::TestGateCheck -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): gate_check() — reject only on known conflict, never on missing data"
```

---

### Task 6: `match()` gates before scoring; retire `soc` from the score

**Files:**
- Modify: `cas/firmware.py` — `match()` (~line 280)
- Test: `tests/test_firmware.py` — `TestMatch`

**Interfaces:**
- Consumes: `gate_check()` (Task 5).
- Produces: `match(identity_dict, root)` signature unchanged — still returns `(Firmware, current_version)` or `None`. Task 8's `resolve()` is unaffected.

> Fixes the latent bug: today `serial_prefix=3` outvotes `soc=1`, so a stale serial rule can carry a wrong-chip build to the top and win. After this, a gate-rejected firmware can never be promoted by any soft rule.

- [ ] **Step 1: Write the failing test**

```python
    def test_gate_rejected_firmware_cannot_be_promoted_by_serial_prefix(self):
        # THE LATENT BUG: serial_prefix (3) used to outvote soc (1). A stale serial rule must no
        # longer be able to carry a wrong-chip build to the top.
        make_fw(self.root, "wrong-chip-but-serial-hit",
                match={"serial_prefix": ["MQ66"], "board_platform": "sun"})
        m = FW.match({"serial": "MQ66999", "device": "AIR_X", "board_platform": "bengal",
                      "soc": "SM6115", "brand": "MANGMI"}, self.root)
        self.assertNotEqual(getattr(m and m[0], "id", None), "wrong-chip-but-serial-hit")

    def test_affirmed_gate_pass_is_a_candidate_at_score_zero(self):
        # THE MOTIVATING CASE: an RP6 on the Odin 2 build hits no serial prefix, and its device and
        # brand both differ -> score 0. The affirmed gate pass alone must carry it.
        root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        root.mkdir(parents=True)
        make_fw(root, "ayn-odin2", device="odin2", storage="ufs",
                match={"device": "odin2", "board_platform": "kalama", "android_release": "13"})
        m = FW.match({"serial": "RP6x", "device": "RP6", "brand": "Retroid",
                      "board_platform": "kalama", "soc": "SM8550", "android_release": "13",
                      "bootdevice": "1d84000.ufshc"}, root)
        self.assertIsNotNone(m)
        self.assertEqual(m[0].id, "ayn-odin2")

    def test_vacuous_gate_pass_at_score_zero_is_not_a_candidate(self):
        # A legacy chip-less entry affirms nothing. It must still need a positive score — today's
        # behavior, preserved. Otherwise every legacy entry would tie at 0 and matching would break.
        root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        root.mkdir(parents=True)
        make_fw(root, "legacy", device="whatever", storage="", match={})
        self.assertIsNone(FW.match({"serial": "RP6x", "device": "RP6",
                                    "board_platform": "kalama"}, root))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestMatch -v`
Expected: FAIL — `test_affirmed_gate_pass_is_a_candidate_at_score_zero` returns `None`; `test_gate_rejected_firmware_cannot_be_promoted_by_serial_prefix` picks the wrong-chip build.

- [ ] **Step 3: Write minimal implementation**

Replace `match()` entirely:

```python
def match(identity_dict, root):
    """Suggest a Firmware for a device identity. TWO STAGES:

      1. gate_check() — a hard compatibility gate (chip/android/storage). A rejected firmware is not a
         candidate at all; no soft rule can promote it. This is what fixes the latent bug where
         serial_prefix (3) outvoted soc (1) and could carry a wrong-chip build to the top.
      2. score, among survivors only — serial_prefix=3, device=2, brand=1. `soc` is NOT scored: chip is
         a gate now, not a tiebreaker.

    Candidacy: score>0, OR an AFFIRMED gate pass (agreed>0). The affirmed case is essential — an RP6 on
    the Odin 2 build scores zero on every soft rule, and the gate's affirmation is the only evidence
    there is. A VACUOUS pass (agreed==0, a legacy chip-less entry) affirms nothing and still requires a
    positive score, preserving today's behavior.

    The unique highest score wins. Tie -> None (operator selects). Returns (Firmware, version) or None.
    """
    serial = identity_dict.get("serial") or ""
    scored = []
    for fw in list_firmware(root):
        ok, _reason, agreed = gate_check(fw, identity_dict)
        if not ok:
            continue
        r = fw.match_rules()
        score = 0
        if _serial_prefix_hit(r, serial):
            score += 3
        if r.get("device") and r["device"] == identity_dict.get("device"):
            score += 2
        if r.get("brand") and r["brand"].lower() == (identity_dict.get("brand") or "").lower():
            score += 1
        if score > 0 or agreed > 0:
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -v`
Expected: PASS. The pre-existing `TestMatch` tests must still be green — their fixtures carry `device` rules, so scoring still decides them, and their identities omit `board_platform`/`bootdevice`, so the gate abstains.

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): match() gates before scoring; soc retired from the score"
```

---

### Task 7: delete the always-true device-inequality warning

**Files:**
- Modify: `cas/firmware.py` — `logic_check()` (~line 330)
- Test: `tests/test_firmware.py` — `TestLogicCheck` (an existing test **must** be updated)

**Interfaces:**
- Consumes: nothing.
- Produces: `logic_check(firmware, identity_dict) -> (ok, [warnings])` — signature unchanged. The `firmware device 'X' != device 'Y'` warning no longer appears. `flash_target` and `serial_prefix` warnings are unchanged.

> **Existing test `test_warns_on_serial_and_device_mismatch` asserts `len(warns) == 2` and will fail.** That is correct — it is asserting the bug. Update it to expect 1.

- [ ] **Step 1: Update the existing test and add the regression test**

Replace `test_warns_on_serial_and_device_mismatch` in `TestLogicCheck`:

```python
    def test_warns_on_serial_mismatch_only(self):
        # Was 2 warnings; the device-inequality warning is deleted. A firmware's human label
        # ('Odin2 (kalama)') never equals a live ro.product.device, so that warning was always true
        # and never meaningful — which is exactly what trained operators to click through warnings.
        ok, warns = FW.logic_check(self.fw, {"serial": "MQ65x", "device": "Pocket_Max",
                                             "flash_target": "init_boot_b"})
        self.assertFalse(ok)
        self.assertEqual(len(warns), 1)
        self.assertIn("MQ65x", warns[0])

    def test_no_device_inequality_warning_on_proven_cross_brand_pair(self):
        # RP6 rooted from the Odin 2 build: proven to boot, must be SILENT.
        fw = make_fw(self.root, "ayn-odin2", device="odin2", flash="init_boot",
                     match={"device": "odin2", "board_platform": "kalama"})
        ok, warns = FW.logic_check(fw, {"serial": "RP6x", "device": "RP6",
                                        "flash_target": "init_boot_a"})
        self.assertTrue(ok)
        self.assertEqual(warns, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestLogicCheck -v`
Expected: FAIL — both new tests see the device warning (`len(warns) == 2`, and `warns != []`).

- [ ] **Step 3: Write minimal implementation**

In `logic_check()`, delete the device-inequality block:

```python
def logic_check(firmware, identity_dict):
    """Validate a (suggested or chosen) firmware against the LIVE device. Returns (ok, [warnings]).
    A warned firmware is still selectable — the operator just sees why (brick-guard).

    NOTE: there is deliberately NO 'firmware device != device' warning. A firmware's human device label
    ('Odin2 (kalama)') never equals a live ro.product.device codename, so that warning fired on every
    legitimate cross-brand match (the RP6-on-the-Odin-2-build pair is PROVEN to boot) — always true,
    never meaningful, and the reason operators learned to click through warnings. Chip compatibility is
    enforced by gate_check() instead, which rejects rather than warns.
    """
    warns = []
    live_base = _strip_slot(identity_dict.get("flash_target") or "")
    if firmware.flash_target and live_base and firmware.flash_target != live_base:
        warns.append(
            f"firmware expects '{firmware.flash_target}' but device exposes '{live_base}'"
        )
    prefixes = firmware.match_rules().get("serial_prefix") or []
    serial = identity_dict.get("serial") or ""
    if prefixes and serial and not any(serial.startswith(p) for p in prefixes):
        warns.append(f"serial '{serial}' matches none of {prefixes}")
    return (not warns, warns)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "fix(firmware): delete the always-true device-inequality warning"
```

---

### Task 8: `(no match)` explains itself

**Files:**
- Modify: `cas/firmware.py` — `resolve()` (~line 520), new helper `_no_match_reasons()`
- Test: `tests/test_firmware.py` — new `TestNoMatchReasons`

**Interfaces:**
- Consumes: `gate_check()` (Task 5).
- Produces: `_no_match_reasons(identity_dict, root) -> [str]`. `resolve()`'s existing `{"warnings": [...]}` field carries them — no new plumbing, no signature change.

> We are deleting a warning that was noisy and false. We must not replace it with a dead end that is quiet and uninformative. "Ingest a build for this chip" and "run backfill" are different operator actions, so they must be different messages.

- [ ] **Step 1: Write the failing test**

```python
class TestNoMatchReasons(unittest.TestCase):
    def setUp(self):
        self._saved_cas_config = os.environ.get("CAS_CONFIG")
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    def _rp6(self):
        return {"serial": "RP6x", "device": "RP6", "brand": "Retroid", "board_platform": "kalama",
                "soc": "SM8550", "android_release": "13", "bootdevice": "1d84000.ufshc"}

    def test_reason_names_the_chip_when_all_entries_were_rejected(self):
        make_fw(self.root, "ayn-odin3", device="odin3", storage="ufs",
                match={"board_platform": "sun"})
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertTrue(any("kalama" in w for w in r["warnings"]),
                        f"expected the chip named in {r['warnings']}")

    def test_reason_says_run_backfill_when_entries_have_no_chip(self):
        make_fw(self.root, "legacy-a", device="x", storage="", match={})
        make_fw(self.root, "legacy-b", device="y", storage="", match={})
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertTrue(any("backfill" in w for w in r["warnings"]),
                        f"expected a backfill hint in {r['warnings']}")

    def test_empty_library_says_neither(self):
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertTrue(any("no match" in w for w in r["warnings"]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestNoMatchReasons -v`
Expected: FAIL — `warnings` is the bare `["no match — select manually"]`.

- [ ] **Step 3: Write minimal implementation**

Add the helper above `resolve()`:

```python
def _no_match_reasons(identity_dict, root):
    """Why did nothing match? Distinguishes the two situations, because they imply DIFFERENT operator
    actions: 'the library has no build for this silicon' (ingest one) vs 'entries exist but record no
    chip' (run backfill). A bare 'no match' leaves the operator with no next step — and this spec
    deletes a warning for being uninformative, so it must not add one."""
    rejected, legacy = 0, 0
    for fw in list_firmware(root):
        ok, _reason, agreed = gate_check(fw, identity_dict)
        if not ok:
            rejected += 1
        elif agreed == 0 and not fw.match_rules().get("board_platform"):
            legacy += 1
    out = []
    chip = identity_dict.get("board_platform") or identity_dict.get("soc") or "unknown"
    if rejected:
        out.append(f"no firmware matches this chip ({chip}) — {rejected} rejected by the gate; "
                   f"ingest a build for it")
    if legacy:
        out.append(f"{legacy} firmware(s) record no chip — run 'python3 -m cas.firmware backfill'")
    if not out:
        out.append("no match — select manually")
    return out
```

Then in `resolve()`, replace the no-candidate return:

```python
    if fw is None:
        return {
            "firmware_id": None,
            "version": None,
            "manual": False,
            "suggested": None,
            "ok": False,
            "warnings": _no_match_reasons(identity_dict, root),
            "firmware": None,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): (no match) distinguishes wrong-chip from needs-backfill"
```

---

### Task 9: `set` subcommand — the escape hatch

**Files:**
- Modify: `cas/firmware.py` — new `set_gate_fields()` + `main()` subparser (~line 570)
- Test: `tests/test_firmware.py` — new `TestSetGateFields`

**Interfaces:**
- Consumes: `find()`, `_read_json`, `_write_json`.
- Produces: `set_gate_fields(firmware_id, root, chip=None, soc=None, android=None, storage=None) -> Firmware`. CLI: `python3 -m cas.firmware set <id> [--chip X] [--soc Y] [--android Z] [--storage emmc|ufs]`.

> For when `detect_build()`'s grep comes up empty. Today the only recourse is hand-editing `meta.json`.

- [ ] **Step 1: Write the failing test**

```python
class TestSetGateFields(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        self.root.mkdir(parents=True)
        make_fw(self.root, "legacy-fw", device="x", storage="", match={})

    def test_set_writes_gate_fields(self):
        fw = FW.set_gate_fields("legacy-fw", self.root, chip="kalama", soc="SM8550",
                                android="13", storage="ufs")
        r = fw.match_rules()
        self.assertEqual(r["board_platform"], "kalama")
        self.assertEqual(r["soc"], "SM8550")
        self.assertEqual(r["android_release"], "13")
        self.assertEqual(fw.storage, "ufs")

    def test_set_is_idempotent(self):
        FW.set_gate_fields("legacy-fw", self.root, chip="kalama")
        fw = FW.set_gate_fields("legacy-fw", self.root, chip="kalama")
        self.assertEqual(fw.match_rules()["board_platform"], "kalama")

    def test_set_only_touches_named_fields(self):
        FW.set_gate_fields("legacy-fw", self.root, chip="kalama")
        fw = FW.set_gate_fields("legacy-fw", self.root, android="13")
        self.assertEqual(fw.match_rules()["board_platform"], "kalama")   # survives
        self.assertEqual(fw.match_rules()["android_release"], "13")

    def test_set_unknown_id_raises(self):
        with self.assertRaises(ValueError):
            FW.set_gate_fields("nope", self.root, chip="kalama")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestSetGateFields -v`
Expected: FAIL with `AttributeError: module 'cas.firmware' has no attribute 'set_gate_fields'`

- [ ] **Step 3: Write minimal implementation**

Add below `ingest()`:

```python
def set_gate_fields(firmware_id, root, chip=None, soc=None, android=None, storage=None):
    """Write gate fields on an existing firmware without re-ingesting — the escape hatch for a build
    whose props detect_build() can't grep out of its super image. Only the named fields are touched.
    Raises ValueError on an unknown id. Returns the Firmware."""
    fw = find(firmware_id, root)
    if fw is None:
        raise ValueError(f"no firmware '{firmware_id}' in {root}")
    meta = _read_json(fw.path / "meta.json")
    m = dict(meta.get("match") or {})
    for key, val in (("board_platform", chip), ("soc", soc), ("android_release", android)):
        if val:
            m[key] = str(val)
    meta["match"] = m
    if storage:
        meta["storage"] = str(storage)
    _write_json(fw.path / "meta.json", meta)
    return Firmware(fw.path)
```

In `main()`, add the subparser after the `assign` parser:

```python
    st = sub.add_parser("set", help="write gate fields (chip/android/storage) on a firmware")
    st.add_argument("id")
    st.add_argument("--chip", help="ro.board.platform, e.g. kalama")
    st.add_argument("--soc", help="ro.soc.model, e.g. SM8550")
    st.add_argument("--android", help="major Android release, e.g. 13")
    st.add_argument("--storage", choices=["emmc", "ufs"])
```

and the dispatch branch before the `("show", "assign")` branch:

```python
    elif args.cmd == "set":
        fw = set_gate_fields(args.id, root, chip=args.chip, soc=args.soc,
                             android=args.android, storage=args.storage)
        print(f"{fw.id}: match={fw.match_rules()} storage={fw.storage}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): 'set' subcommand — gate-field escape hatch"
```

---

### Task 10: `backfill` subcommand — migration without a flag day

**Files:**
- Modify: `cas/firmware.py` — new `backfill()` + `main()` subparser
- Test: `tests/test_firmware.py` — new `TestBackfill`

**Interfaces:**
- Consumes: `detect_build()` (Task 3), `set_gate_fields()` (Task 9), `Firmware.payload_dir()`.
- Produces: `backfill(root) -> [(firmware_id, filled: dict)]`, listing only entries actually changed. CLI: `python3 -m cas.firmware backfill`.

> The payload is a verbatim copy of the build tree, so `detect_build(fw.payload_dir())` works unchanged. Backfilling is what upgrades an entry from "legacy/warns" to "silent auto-select" — never overwrite a value an operator set by hand.

- [ ] **Step 1: Write the failing test**

```python
class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def _ingest_then_strip(self, fid, **kw):
        """Ingest a build, then strip its gate fields to simulate a pre-existing legacy entry."""
        src = fake_build(self.tmp, f"{fid}-20260507.165105", **kw)
        fw = FW.ingest(src, self.root, firmware_id=fid)
        meta = FW._read_json(fw.path / "meta.json")
        meta["match"] = {k: v for k, v in (meta.get("match") or {}).items()
                         if k not in ("board_platform", "soc", "android_release")}
        FW._write_json(fw.path / "meta.json", meta)
        return FW.Firmware(fw.path)

    def test_backfill_fills_gate_fields_from_the_payload(self):
        self._ingest_then_strip("ayn-odin2", board_platform="kalama", soc="SM8550", android="13")
        filled = FW.backfill(self.root)
        self.assertEqual([fid for fid, _ in filled], ["ayn-odin2"])
        fw = FW.find("ayn-odin2", self.root)
        self.assertEqual(fw.match_rules()["board_platform"], "kalama")
        self.assertEqual(fw.match_rules()["android_release"], "13")

    def test_backfill_is_idempotent_and_reports_nothing_second_time(self):
        self._ingest_then_strip("ayn-odin2", board_platform="kalama", soc="SM8550", android="13")
        FW.backfill(self.root)
        self.assertEqual(FW.backfill(self.root), [])

    def test_backfill_never_overwrites_an_operator_set_value(self):
        self._ingest_then_strip("ayn-odin2", board_platform="kalama", soc="SM8550", android="13")
        FW.set_gate_fields("ayn-odin2", self.root, chip="kalama-hand-set")
        FW.backfill(self.root)
        self.assertEqual(FW.find("ayn-odin2", self.root).match_rules()["board_platform"],
                         "kalama-hand-set")

    def test_backfill_skips_undetectable_entry_without_raising(self):
        self._ingest_then_strip("legacy", board_platform="", soc="", android="")
        self.assertEqual(FW.backfill(self.root), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestBackfill -v`
Expected: FAIL with `AttributeError: module 'cas.firmware' has no attribute 'backfill'`

- [ ] **Step 3: Write minimal implementation**

Add below `set_gate_fields()`:

```python
def backfill(root):
    """Re-run detect_build() over every firmware's CURRENT version payload and fill the gate fields it
    is MISSING. The payload is a verbatim copy of the build tree, so detect_build() works on it as-is.

    Never overwrites an existing value — an operator's `set` wins over detection. Best-effort per entry:
    an unreadable or undetectable payload is skipped, never raised. Returns [(firmware_id, filled)] for
    entries actually changed, so the CLI can report what moved."""
    out = []
    for fw in list_firmware(root):
        pd = fw.payload_dir()
        if not pd or not pd.is_dir():
            continue
        try:
            info = detect_build(pd)
        except Exception:
            continue
        r = fw.match_rules()
        filled = {}
        for meta_key, info_key in (("board_platform", "board_platform"), ("soc", "soc"),
                                   ("android_release", "android_release")):
            if info.get(info_key) and not r.get(meta_key):
                filled[meta_key] = info[info_key]
        if not filled:
            continue
        set_gate_fields(fw.id, root, chip=filled.get("board_platform"),
                        soc=filled.get("soc"), android=filled.get("android_release"))
        out.append((fw.id, filled))
    return out
```

In `main()`, add the subparser:

```python
    sub.add_parser("backfill", help="fill gate fields on existing firmware from their payloads")
```

and the dispatch branch:

```python
    elif args.cmd == "backfill":
        rows = backfill(root)
        for fid, filled in rows:
            print(f"{fid}: filled {filled}")
        print(f"{len(rows)} firmware backfilled")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): 'backfill' subcommand — migration without a flag day"
```

---

### Task 11: proven pairs — evidence, not a gate

**Files:**
- Modify: `cas/firmware.py` — new `log_proven_pair()`
- Modify: `cas/provision.py:1782-1823` — `root_all()`'s worker
- Test: `tests/test_firmware.py` — new `TestProvenPair`

**Interfaces:**
- Consumes: `config.history_dir()`, `config.history_filename()` (the `log_event()` pattern).
- Produces: `log_proven_pair(identity_dict, firmware_id, version, when=None) -> None`. Best-effort; never raises.

> **This never gates anything.** It records what actually booted, so "RP6 ≡ Odin 2 works" stops being tribal knowledge and becomes data a future maintainer can act on.

- [ ] **Step 1: Write the failing test**

```python
class TestProvenPair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = os.environ.get("CAS_PROFILES")
        os.environ["CAS_PROFILES"] = self.tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CAS_PROFILES", None)
        else:
            os.environ["CAS_PROFILES"] = self._saved

    def _idn(self):
        return {"serial": "RP6x", "model": "RP6", "board_platform": "kalama", "soc": "SM8550",
                "android_release": "13", "bootdevice": "1d84000.ufshc"}

    def test_logs_the_tuple(self):
        FW.log_proven_pair(self._idn(), "ayn-odin2", "20260507-165105", when="2026-07-16 10:00")
        p = pathlib.Path(C.history_dir()) / C.history_filename("firmware-proven")
        rec = json.loads(p.read_text().strip().splitlines()[-1])
        self.assertEqual(rec["chip"], "kalama")
        self.assertEqual(rec["android"], "13")
        self.assertEqual(rec["storage"], "ufs")
        self.assertEqual(rec["model"], "RP6")
        self.assertEqual(rec["firmware_id"], "ayn-odin2")
        self.assertEqual(rec["version"], "20260507-165105")

    def test_never_raises_on_a_bad_identity(self):
        FW.log_proven_pair(None, None, None)        # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_firmware.py::TestProvenPair -v`
Expected: FAIL with `AttributeError: module 'cas.firmware' has no attribute 'log_proven_pair'`

- [ ] **Step 3: Write minimal implementation**

In `cas/firmware.py`, add below `log_event()`:

```python
def log_proven_pair(identity_dict, firmware_id, version, when=None):
    """Record a (chip, android, storage, model, firmware_id, version) tuple that ACTUALLY BOOTED.

    EVIDENCE, NOT A GATE — nothing reads this to allow or block a flash. It exists so a proven
    cross-model pair (an RP6 rooted from the Odin 2 build) stops being knowledge in one person's head
    and becomes data in the library. Best-effort; never raises."""
    try:
        if when is None:
            import datetime
            when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        idn = identity_dict or {}
        rec = {
            "when": when,
            "serial": idn.get("serial"),
            "model": idn.get("model"),
            "chip": idn.get("board_platform") or idn.get("soc"),
            "android": _android_major(idn.get("android_release")),
            "storage": _storage_from_bootdevice(idn.get("bootdevice")),
            "firmware_id": firmware_id,
            "version": version,
        }
        p = pathlib.Path(config.history_dir()) / config.history_filename("firmware-proven")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
```

In `cas/provision.py`, `root_all()`'s worker: `idn` and `fwres` are assigned **inside** the `try` at line 1782 and are not in scope if it raised, so capture them into a pre-declared variable. Before the `try` (line 1780), alongside `flasher = None`:

```python
            flasher = None
            proven = None                     # (identity, firmware_id, version) once firmware resolves
            phase = "fastboot_flash"          # coarse recovery hint; the EDL branch below flips it
```

Inside the `try`, immediately after `fw = fwres.get("firmware")` (line 1786):

```python
                fw = fwres.get("firmware")
                proven = (idn, fwres.get("firmware_id"), fwres.get("version"))
```

Then at the success branch (line 1822):

```python
            if ok:
                if proven and proven[1]:
                    # Root returned ok, which means the unit BOOTED — record the combination that
                    # worked. Evidence only; nothing gates on it.
                    FW.log_proven_pair(*proven)
                return ("ok", prof.name)
```

Note `FW` is imported inside the `try` (line 1783) as `from . import firmware as FW`. Move that import to the top of the worker, before `flasher = None`, so it is in scope at the success branch:

```python
            from . import firmware as FW
            flasher = None
            proven = None
            phase = "fastboot_flash"
```

and delete the now-duplicate `from . import firmware as FW` inside the `try`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS — the whole suite, including every `provision.py` test, green.

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py cas/provision.py tests/test_firmware.py
git commit -m "feat(firmware): log proven (chip, android, storage, model, fw) pairs after boot"
```

---

## Self-Review

**Spec coverage** — every spec section maps to a task:

| Spec section | Task |
|---|---|
| Stage 1 gate (chip/android/storage) | 5 |
| Affirmed vs vacuous passes (candidacy at 0) | 5, 6 |
| Stage 2 score, soc retired | 6 |
| `serial_prefix`-outvotes-chip latent bug | 6 |
| Delete device-inequality warning | 7 |
| Proven pairs | 11 |
| `_img_kernel_size` untouched | Global Constraints (enforced by Task 11's full-suite run) |
| Data-model changes (`identity`/`detect_build`/`ingest`/`meta.json`) | 1, 3, 4 |
| Operator workflow — new chip needs no input | 4 |
| Escape hatch (`set`) | 9 |
| `(no match)` explains itself | 8 |
| Migration / backfill | 10 |
| Storage probe is the unverified axis | 2 |

**Type consistency** — `gate_check()` returns the 3-tuple `(ok, reason, agreed)` in Tasks 5, 6, and 8. `_storage_from_bootdevice()` and `_android_major()` are defined in Task 2 and consumed in 5 and 11. `set_gate_fields()` is defined in Task 9 and consumed by `backfill()` in Task 10 — so **Task 9 must land before Task 10**. `match()`'s signature and return type are unchanged, so `resolve()` needs no edit for Task 6.

**Placeholder scan** — none: every code step carries complete, runnable code.

## Open verification items (carried from the spec — not blockers)

1. **`ro.boot.bootdevice` is unverified on real hardware.** Task 2 isolates it and returns `""` on anything unrecognized → the storage axis abstains → legacy behavior, never a wrong flash. Confirm on an RP6 and an AIR X before trusting the storage axis.
2. **Confirm the proven pair passes its own gate.** If a live RP6 and the Odin 2 build disagree on any axis, `gate_check()` is wrong — that pair is known to boot. Highest-value bench check; `Task 5 :: test_proven_cross_brand_pair_passes_and_is_affirmed` encodes the intent, but only real props confirm it.
3. **Whether any existing library build fails to yield `ro.board.platform=`** from its `super_*.img` — those stay legacy until `set` supplies the chip by hand. `backfill`'s output tells you which.
