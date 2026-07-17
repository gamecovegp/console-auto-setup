# Chip Gate Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a same-silicon SoC SKU difference from falsely rejecting a device, and make `backfill` tell the operator the truth instead of going silent for 91 minutes.

**Architecture:** Two independent changes, both in `cas/firmware.py`. Part 1 restructures one loop in `gate_check()`. Part 2 gives `backfill()` a `log` callback and a `(filled, skipped)` return, adds one shared helper, and teaches `_no_match_reasons()` to use it. No new modules, no new abstractions.

**Tech Stack:** Python 3, `unittest` (pytest is only the runner), no new dependencies.

## Global Constraints

- **`_img_kernel_size()` in `cas/provision.py` is OFF LIMITS** — it refuses a kernel-less image being flashed to a `boot` partition, the check standing between an operator and a bricked device. Do not touch, move, or reference it. Matching is a heuristic; that check is physics.
- **A concurrent session shares this checkout.** Do NOT touch `cas/gui.py`, `cas/profiles.py`, `cas/provision.py`, or `tests/test_cas.py`. Run `python3 -m pytest tests/test_firmware.py -q` ONLY — the other files carry in-flight edits and produce PHANTOM failures that are not yours.
- **`gate_check`'s core rule is unchanged:** reject only on a KNOWN CONFLICT, never on missing data. An axis gates only when the same field is populated on BOTH sides and the values differ; absence abstains.
- **`gate_check`'s `agreed` contract is unchanged:** only CHIP axes (`board_platform`, `soc`) affirm; android and storage reject on conflict but never contribute to `agreed`. `agreed` is 0 whenever `ok` is False.
- **Never compare `board_platform` against `soc`** — `kalama` and `SM8550` name the same silicon. Each prop compares only against its own counterpart.
- **A WRONG chip is worse than NO chip.** A wrong chip gates and false-rejects forever; a missing chip abstains and is safe. Nothing in this plan may guess a chip.
- **The per-needle rescan is OUT OF SCOPE.** `detect_build` calls `_grep_value` once per needle, which is why 19.4 GB takes ~91 min. Progress output makes that legible, not shorter. Do not "fix" it here.
- Tests are `unittest.TestCase` and live in `tests/test_firmware.py`. Never `pop()` `CAS_CONFIG` bare in a tearDown — save and RESTORE. Do not weaken `setUpModule`/`tearDownModule`.
- **Never touch the operator's real library** (an external drive that `firmware_root()` resolves to). Every test isolates `CAS_PROFILES` and `CAS_CONFIG` to a temp dir. The real library has been mutated once already during this project; do not repeat it.
- Real values to use verbatim in fixtures (measured from a live RP6, serial `caecc295`): `board_platform=kalama`, `soc=QCS8550`, `android_release=13`, `bootdevice=1d84000.ufshc`, `device=kalama`, `brand=Moorechip`. A generic kalama build's super image records `soc=SM8550`.

---

### Task 1: `board_platform` agreement outranks a `soc` conflict

**Files:**
- Modify: `cas/firmware.py` — `gate_check()` (the `for key in ("board_platform", "soc")` loop)
- Test: `tests/test_firmware.py` — `TestGateCheck`

**Interfaces:**
- Consumes: nothing.
- Produces: `gate_check(firmware, identity_dict) -> (ok, reason, agreed)` — signature UNCHANGED. Only the soc-conflict behavior changes. Task 3 consumes `reason`, which keeps its `"chip …"` prefix for chip-axis rejections.

> **Why:** the live RP6 reports `soc=QCS8550`; a generic kalama build's super image records `soc=SM8550`. Both are Snapdragon 8 Gen 2 — `QCS` is the IoT SKU — and `board_platform` is `kalama` on both sides. Today the soc conflict rejects anyway, so the RP6 would be refused a build that fits it, and `_no_match_reasons` would blame *"this chip (kalama)"* when kalama agreed perfectly.
>
> **The principle:** `board_platform` is the PLATFORM; `soc` is the SKU. Same platform + different SKU = same ramdisk.

- [ ] **Step 1: Write the failing tests**

Add to `TestGateCheck` in `tests/test_firmware.py`. Its existing helpers are `self._fw(fid=..., storage=..., **rules)` and `self._rp6(**over)` — read them first and match. Note `_rp6()` defaults `soc="SM8550"`; pass `soc="QCS8550"` explicitly to model the real device.

```python
    # --- board_platform outranks a soc SKU conflict (measured: live RP6 reports QCS8550) ---
    def test_platform_agreement_outranks_a_soc_conflict(self):
        # A generic kalama build records soc=SM8550; the real RP6 reports soc=QCS8550. Same silicon
        # (QCS is the IoT SKU), and board_platform agrees on both sides -> must NOT reject.
        fw = self._fw(board_platform="kalama", soc="SM8550")
        ok, reason, agreed = FW.gate_check(fw, self._rp6(soc="QCS8550", storage_off=None))
        self.assertTrue(ok, f"platform agreed but the soc SKU rejected: {reason}")
        self.assertIsNone(reason)
        self.assertEqual(agreed, 1)     # platform affirmed; the conflicting soc adds nothing

    def test_soc_conflict_still_rejects_when_firmware_has_no_board_platform(self):
        # No platform to outrank it -> soc remains the fallback chip axis and must still reject.
        fw = self._fw(soc="SM8550")
        ok, reason, agreed = FW.gate_check(fw, self._rp6(soc="QCS8550"))
        self.assertFalse(ok)
        self.assertIn("QCS8550", reason)
        self.assertEqual(agreed, 0)

    def test_soc_conflict_still_rejects_when_device_reports_no_board_platform(self):
        fw = self._fw(board_platform="kalama", soc="SM8550")
        ok, reason, agreed = FW.gate_check(fw, self._rp6(board_platform="", soc="QCS8550"))
        self.assertFalse(ok)
        self.assertEqual(agreed, 0)

    def test_board_platform_conflict_still_rejects_regardless_of_soc(self):
        # A platform conflict is unconditional — an agreeing soc must not rescue it.
        fw = self._fw(board_platform="sun", soc="QCS8550")
        ok, reason, agreed = FW.gate_check(fw, self._rp6(soc="QCS8550"))
        self.assertFalse(ok)
        self.assertIn("kalama", reason)
        self.assertEqual(agreed, 0)

    def test_platform_and_soc_both_agree_still_affirms_twice(self):
        fw = self._fw(board_platform="kalama", soc="QCS8550")
        ok, _reason, agreed = FW.gate_check(fw, self._rp6(soc="QCS8550"))
        self.assertTrue(ok)
        self.assertEqual(agreed, 2)
```

`self._fw()` defaults `storage="ufs"` and `_rp6()` defaults a `ufshc` bootdevice, so the storage axis agrees silently — that is fine here (storage never counts into `agreed`). If a test needs the storage axis to abstain, pass `storage=""` to `_fw()` and `bootdevice=""` to `_rp6()`. Drop the `storage_off=None` kwarg above if `_rp6()` has no such parameter — it is illustrative; read the helper and use its real signature.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_firmware.py -q -k GateCheck`
Expected: FAIL — `test_platform_agreement_outranks_a_soc_conflict` fails, because the soc conflict currently rejects (`ok` is False).

- [ ] **Step 3: Write minimal implementation**

In `gate_check()`, replace the `for key in ("board_platform", "soc")` loop with:

```python
    # board_platform is the PLATFORM; soc is the SKU. Same platform + different SKU = the same
    # silicon and the same ramdisk: a live RP6 reports soc=QCS8550 (the IoT SKU) where a generic
    # kalama build's super image records SM8550, and board_platform is 'kalama' on both sides. So
    # platform agreement OUTRANKS a soc conflict — otherwise that build falsely rejects the RP6 and
    # _no_match_reasons blames "this chip (kalama)" when kalama agreed perfectly.
    # A platform CONFLICT still rejects unconditionally, and soc still rejects when no platform
    # compared (it remains the fallback chip axis).
    platform_agreed = False
    want, live = r.get("board_platform"), identity_dict.get("board_platform")
    if want and live:
        if want.strip().lower() != live.strip().lower():
            return (False, f"chip {live} != firmware {want}", 0)
        agreed += 1
        platform_agreed = True

    want, live = r.get("soc"), identity_dict.get("soc")
    if want and live:
        if want.strip().lower() != live.strip().lower():
            if not platform_agreed:
                return (False, f"chip {live} != firmware {want}", 0)
            # else: the platform already agreed — a differing SKU neither rejects nor affirms.
        else:
            agreed += 1
```

Then add this paragraph to the docstring, after the "NEVER compare ro.board.platform against ro.soc.model" one:

```
    PLATFORM OUTRANKS SKU: when board_platform is populated on both sides and AGREES, a differing
    ro.soc.model does NOT reject (and does not affirm). board_platform names the platform (kalama);
    soc names the SKU (SM8550 vs QCS8550 — the IoT variant of the same Snapdragon 8 Gen 2). Same
    platform means the same ramdisk. soc still rejects on conflict when board_platform did not
    compare, so it remains the fallback chip axis for a device or build that reports only soc.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -q`
Expected: PASS — including every pre-existing `TestGateCheck` and `TestMatch` test, unmodified.

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "fix(firmware): board_platform agreement outranks a soc SKU conflict"
```

---

### Task 2: `backfill` reports — progress, and every skip with a reason

**Files:**
- Modify: `cas/firmware.py` — new `_payload_has_build_images()` helper (place directly above `backfill`), `backfill()`, and `main()`'s `backfill` dispatch branch (~line 890)
- Test: `tests/test_firmware.py` — `TestBackfill`, `TestMainBackfillSubcommand`

**Interfaces:**
- Consumes: `detect_build()`, `set_gate_fields()`, `Firmware.payload_dir()`.
- Produces:
  - `_payload_has_build_images(firmware, version=None) -> bool` — True when the payload holds a `super_*.img`/`system_*.img` for `detect_build()` to grep. **Task 3 consumes this.**
  - `backfill(root, log=print) -> (filled: [(id, dict)], skipped: [(id, reason)])` — return shape CHANGED from `[(id, dict)]`. Existing callers must be updated (the CLI is the only one).

> **Why:** measured on the real library — 91 minutes, **zero output** until it returned, and it silently skipped the three entries it can never help (`odin2-default`, `odin3`, `retroid-pocket-5` have bare `init_boot.img`/`boot.img` payloads with no super image). An entry backfill can *never* fix looked identical to one it merely had nothing to add to.

> **SPEC CORRECTION — do not implement `"already has a chip"` as a skip.** The spec lists it, but it is wrong: `backfill` fills ANY missing gate field, not just the chip. Skipping a chip-having entry would silently prevent its `android_release`/`soc` from ever being filled (e.g. `retroid-pocket-6` has `board_platform` + `android_release` but no `soc`). `"nothing new detected"` covers that case correctly and honestly. The spec's own principle — no silent skips — is what governs.

- [ ] **Step 1: Write the failing tests**

```python
class TestPayloadHasBuildImages(unittest.TestCase):
    """Distinguishes 'backfill can never help this' from 'backfill had nothing to add'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def test_true_when_payload_has_a_super_image(self):
        src = fake_build(self.tmp, "hasimg-20260507.165105")
        fw = FW.ingest(src, self.root, firmware_id="hasimg")
        self.assertTrue(FW._payload_has_build_images(fw))

    def test_false_when_payload_has_no_super_or_system_image(self):
        # The real shape of odin2-default / odin3 / retroid-pocket-5: a bare init_boot.img payload.
        fw = make_fw(self.root, "bare", storage="")
        (fw.payload_dir() / "init_boot.img").write_bytes(b"x")
        self.assertFalse(FW._payload_has_build_images(fw))

    def test_false_when_there_is_no_payload_at_all(self):
        d = self.root / "nopayload"
        (d / "versions").mkdir(parents=True)
        FW._write_json(d / "meta.json", {"id": "nopayload", "current": "v1", "match": {}})
        self.assertFalse(FW._payload_has_build_images(FW.Firmware(d)))


class TestBackfillReporting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)
        self.lines = []

    def _log(self, m):
        self.lines.append(str(m))

    def _bare(self, fid):
        """An entry with no super image — backfill can NEVER detect its chip."""
        fw = make_fw(self.root, fid, storage="")
        (fw.payload_dir() / "init_boot.img").write_bytes(b"x")
        return fw

    def test_returns_filled_and_skipped(self):
        src = fake_build(self.tmp, "good-20260507.165105", board_platform="kalama",
                         soc="QCS8550", android="13")
        FW.ingest(src, self.root, firmware_id="good")
        meta = FW._read_json(self.root / "good" / "meta.json")
        meta["match"] = {}
        FW._write_json(self.root / "good" / "meta.json", meta)
        self._bare("bare")

        filled, skipped = FW.backfill(self.root, log=self._log)
        self.assertEqual([fid for fid, _ in filled], ["good"])
        self.assertIn("bare", [fid for fid, _ in skipped])

    def test_no_super_image_skip_names_the_reason_and_the_fix(self):
        self._bare("bare")
        _filled, skipped = FW.backfill(self.root, log=self._log)
        reason = dict(skipped)["bare"]
        self.assertIn("no super", reason.lower())
        self.assertIn("set --chip", reason)

    def test_nothing_new_detected_is_reported_not_silent(self):
        src = fake_build(self.tmp, "done-20260507.165105", board_platform="kalama",
                         soc="QCS8550", android="13")
        FW.ingest(src, self.root, firmware_id="done")      # ingest already seeded every gate field
        _filled, skipped = FW.backfill(self.root, log=self._log)
        self.assertIn("done", [fid for fid, _ in skipped])
        self.assertIn("nothing new", dict(skipped)["done"].lower())

    def test_progress_is_emitted_for_every_entry_including_skipped_ones(self):
        # A skipped entry that prints nothing is exactly the bug being fixed.
        self._bare("bare")
        FW.backfill(self.root, log=self._log)
        self.assertTrue(any("bare" in l for l in self.lines),
                        f"no progress line mentioned the skipped entry: {self.lines}")
        self.assertTrue(any("1/1" in l or "[1/" in l for l in self.lines),
                        f"no [i/n] progress counter emitted: {self.lines}")

    def test_corrupt_meta_is_reported_not_silently_skipped(self):
        d = self.root / "corrupt"; d.mkdir()
        (d / "meta.json").write_text("null")
        _filled, skipped = FW.backfill(self.root, log=self._log)
        self.assertIn("corrupt", [fid for fid, _ in skipped])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_firmware.py -q -k "PayloadHasBuildImages or BackfillReporting"`
Expected: FAIL — `AttributeError: module 'cas.firmware' has no attribute '_payload_has_build_images'`, and `ValueError: too many values to unpack` from `backfill` still returning a single list.

- [ ] **Step 3: Write minimal implementation**

Add the helper directly above `backfill`:

```python
# The exact skip reason for an entry backfill can NEVER fix. It carries the next command, because the
# operator's alternative is a 91-minute scan that ends in "0 firmware backfilled" and no explanation.
NO_BUILD_IMAGES = ("no super/system image in payload — backfill can never detect this; "
                   "use 'set --chip'")


def _payload_has_build_images(firmware, version=None):
    """True when the payload holds a super_*/system_*.img for detect_build() to grep. Mirrors
    detect_build()'s own base-directory logic (payload/<emmc|ufs>/ if present, else payload/) so the
    two can never disagree about whether an entry is detectable.

    False means backfill can NEVER fill this entry's chip no matter how long it scans — its payload is
    a bare init_boot.img/boot.img (odin2-default, odin3, retroid-pocket-5 are all this shape). That is
    a different situation from 'scanned and found nothing new', and the operator needs to know which."""
    pd = firmware.payload_dir(version)
    if not pd or not pd.is_dir():
        return False
    storage = "emmc" if (pd / "emmc").is_dir() else ("ufs" if (pd / "ufs").is_dir() else "")
    base = (pd / storage) if storage else pd
    return bool(list(base.glob("super_*.img")) or list(base.glob("system_*.img")))
```

Replace `backfill` entirely:

```python
def backfill(root, log=print):
    """Re-run detect_build() over every firmware's CURRENT version payload and fill the gate fields it
    is MISSING. The payload is a verbatim copy of the build tree, so detect_build() works on it as-is.

    Returns (filled, skipped): filled = [(id, {field: value})] for entries actually changed;
    skipped = [(id, reason)] for every entry that was not. NOTHING IS SKIPPED SILENTLY — a measured run
    on a real library took 91 MINUTES, printed nothing, and quietly passed over the three entries it
    could never help, which is indistinguishable from a hang followed by a shrug. Progress is emitted
    via `log` BEFORE each entry is scanned, for the same reason.

    Never overwrites an existing value — an operator's `set` wins over detection. Best-effort per
    entry: an unreadable or undetectable payload is skipped, never raised.

    CORRUPT-META GUARD: list_firmware() only returns dirs that CONTAIN a meta.json — so if fw.meta is
    empty/falsy, the file exists but did NOT parse (_read_json() swallows the error and returns {}).
    That is a corrupt entry, never a legitimate backfill target: treating {} as "every gate field is
    missing" would call set_gate_fields(), which re-reads the same unparseable file, also gets {}, and
    writes back a meta.json containing almost nothing — silently dropping device/storage/flash_target/
    current/history/label/id. Skip it instead, before touching anything."""
    filled_out, skipped = [], []
    fws = list_firmware(root)
    total = len(fws)
    for i, fw in enumerate(fws, 1):
        head = f"[{i}/{total}] {fw.id}"

        def skip(reason):
            skipped.append((fw.id, reason))
            log(f"{head}: skipped — {reason}")

        if not fw.meta:
            skip("meta.json did not parse — left untouched")
            continue
        if not _payload_has_build_images(fw):
            skip(NO_BUILD_IMAGES)
            continue
        log(f"{head}: scanning payload…")
        try:
            info = detect_build(fw.payload_dir())
        except Exception as e:
            skip(f"payload unreadable ({e})")
            continue
        r = fw.match_rules()
        filled = {}
        for key in ("board_platform", "soc", "android_release"):
            if info.get(key) and not r.get(key):
                filled[key] = info[key]
        if not filled:
            skip("nothing new detected")
            continue
        set_gate_fields(fw.id, root, chip=filled.get("board_platform"),
                        soc=filled.get("soc"), android=filled.get("android_release"))
        filled_out.append((fw.id, filled))
        log(f"{head}: filled {filled}")
    return (filled_out, skipped)
```

Replace `main()`'s `backfill` dispatch branch:

```python
    elif args.cmd == "backfill":
        filled, skipped = backfill(root)
        for fid, fields in filled:
            print(f"{fid}: filled {fields}")
        for fid, reason in skipped:
            print(f"{fid}: skipped — {reason}")
        print(f"{len(filled)} firmware backfilled, {len(skipped)} skipped")
```

Note `backfill`'s default `log=print` means the CLI prints progress live AND the summary at the end; that duplication is intentional — progress is for the 91-minute wait, the summary is for the record.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -q`
Expected: PASS. Pre-existing `TestBackfill` tests will need their return unpacking updated to `filled, skipped = FW.backfill(...)` — that is the intended interface change, NOT a test to weaken. The never-overwrite and only-changed-entries rules must stay pinned (both are currently mutation-verified).

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "feat(firmware): backfill reports progress and every skip with a reason"
```

---

### Task 3: `(no match)` stops recommending backfill for entries it cannot help

**Files:**
- Modify: `cas/firmware.py` — `_no_match_reasons()`
- Test: `tests/test_firmware.py` — `TestNoMatchReasons`

**Interfaces:**
- Consumes: `_payload_has_build_images(firmware, version=None) -> bool` from Task 2.
- Produces: nothing later tasks depend on. Last task.

> **Why:** the dead-end loop, measured. `(no match)` → "run backfill" → **91 minutes** → "0 firmware backfilled" → still `(no match)`. `_no_match_reasons` counts every chip-less entry toward the backfill hint, including the three whose payloads have no super image and can never be filled by scanning.

- [ ] **Step 1: Write the failing tests**

Add to the existing `TestNoMatchReasons` class (it already isolates `CAS_CONFIG` in setUp/tearDown — match that pattern; read it first).

```python
    def _bare_legacy(self, fid):
        """Chip-less AND no super image: backfill can never help it."""
        fw = make_fw(self.root, fid, storage="", match={})
        (fw.payload_dir() / "init_boot.img").write_bytes(b"x")
        return fw

    def test_does_not_recommend_backfill_when_the_entry_has_no_super_image(self):
        # The measured dead end: "run backfill" -> 91 min -> "0 backfilled" -> still (no match).
        self._bare_legacy("bare-legacy")
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertFalse(any("backfill" in w for w in r["warnings"]),
                         f"recommended backfill for an entry it can never fix: {r['warnings']}")
        self.assertTrue(any("set --chip" in w for w in r["warnings"]),
                        f"expected a 'set --chip' hint in {r['warnings']}")

    def test_still_recommends_backfill_when_a_chip_less_entry_has_a_super_image(self):
        src = fake_build(self.tmp, "scan-20260507.165105", board_platform="kalama",
                         soc="QCS8550", android="13")
        FW.ingest(src, self.root, firmware_id="scannable")
        meta = FW._read_json(self.root / "scannable" / "meta.json")
        meta["match"] = {}
        FW._write_json(self.root / "scannable" / "meta.json", meta)
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertTrue(any("backfill" in w for w in r["warnings"]),
                        f"expected a backfill hint in {r['warnings']}")
```

`TestNoMatchReasons.setUp` may not define `self.tmp` — read it and add one if needed (a `tempfile.mkdtemp()`), keeping its existing `CAS_CONFIG` save/restore intact.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_firmware.py -q -k NoMatchReasons`
Expected: FAIL — `test_does_not_recommend_backfill_when_the_entry_has_no_super_image` fails: the warning still says "run backfill".

- [ ] **Step 3: Write minimal implementation**

In `_no_match_reasons`, split the `legacy` counter in two and emit the right advice for each:

```python
    chip_rejected = 0
    axis_rejections = []
    legacy_scannable = 0        # chip-less, payload HAS a super image -> backfill can fill it
    legacy_unscannable = 0      # chip-less, no super image -> backfill can NEVER fill it
    for fw in list_firmware(root):
        ok, reason, agreed = gate_check(fw, identity_dict)
        if not ok:
            if reason and reason.startswith("chip "):
                chip_rejected += 1
            else:
                axis_rejections.append(reason)
        elif agreed == 0 and not (fw.match_rules().get("board_platform")
                                  or fw.match_rules().get("soc")):
            if _payload_has_build_images(fw):
                legacy_scannable += 1
            else:
                legacy_unscannable += 1
    out = []
    chip = identity_dict.get("board_platform") or identity_dict.get("soc") or "unknown"
    if chip_rejected:
        out.append(f"no firmware matches this chip ({chip}) — {chip_rejected} rejected by the gate; "
                   f"ingest a build for it")
    for reason in axis_rejections:
        out.append(f"a build for this chip exists but was rejected on {reason} — ingest won't help; "
                   f"the mismatch is elsewhere")
    if legacy_scannable:
        out.append(f"{legacy_scannable} firmware(s) record no chip — "
                   f"run 'python3 -m cas.firmware backfill'")
    if legacy_unscannable:
        out.append(f"{legacy_unscannable} firmware(s) record no chip and have no super image — "
                   f"backfill cannot fill them; use 'python3 -m cas.firmware set <id> --chip <name>'")
    if not out:
        out.append("no match — select manually")
    return out
```

Add to the docstring:

```
    NEVER RECOMMEND BACKFILL FOR AN ENTRY IT CANNOT FIX: a chip-less entry whose payload has no
    super/system image (a bare init_boot.img — odin2-default, odin3, retroid-pocket-5) can never be
    filled by scanning, no matter how long. Sending the operator to backfill for those is a measured
    91-MINUTE round trip ending in "0 firmware backfilled" and the same (no match). Those are reported
    with the command that actually works: `set --chip`.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_firmware.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/firmware.py tests/test_firmware.py
git commit -m "fix(firmware): don't send the operator to backfill for entries it can never fix"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Part 1: platform agreement outranks a soc conflict | 1 |
| Part 1: soc still rejects when board_platform didn't compare | 1 |
| Part 1: board_platform conflict always rejects | 1 |
| Part 1: `agreed` semantics unchanged; rejects return 0 | 1 (Global Constraints + tests) |
| Part 2: per-entry progress before each entry | 2 |
| Part 2: `backfill` returns skips with reasons | 2 |
| Part 2: CLI prints skips | 2 |
| Part 2: no device-field fallback (explicitly rejected) | Not implemented — deliberate; Global Constraints forbids guessing a chip |
| Part 2: `_no_match_reasons` stops the dead-end advice | 3 |
| Per-needle rescan out of scope | Global Constraints |
| `_img_kernel_size` untouched | Global Constraints |

**Spec deviation, deliberate:** the spec lists `"already has a chip"` as a skip reason. Dropped — `backfill` fills ANY missing gate field, not just the chip, so skipping a chip-having entry would silently prevent its `android_release`/`soc` from being filled (`retroid-pocket-6` is exactly this shape: `board_platform` + `android_release`, no `soc`). `"nothing new detected"` covers the case correctly. The spec's governing principle — no silent skips — is preserved. **The spec should be amended to match.**

**Type consistency:** `_payload_has_build_images(firmware, version=None) -> bool` is defined in Task 2 and consumed in Task 3 — **Task 2 must land before Task 3.** `backfill(root, log=print) -> (filled, skipped)` is used identically in Task 2's tests and the CLI. `gate_check`'s 3-tuple is unchanged, so Task 3's `ok, reason, agreed` unpacking still holds.

**Placeholder scan:** none — every code step carries runnable code. Task 1's `storage_off=None` kwarg and Task 3's `self.tmp` are explicitly flagged as "read the real helper and adjust", which is a direction to verify against real code, not a blank to invent.
