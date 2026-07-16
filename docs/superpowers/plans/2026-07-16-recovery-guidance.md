# Disconnect / failure recovery guidance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On any operation failure/disconnect, tell the operator the device's likely mode and the exact recovery steps — in the live log, on the device row, and in one end-of-run summary popup.

**Architecture:** A new pure module `cas/recovery.py` (`DeviceMode` enum + `Recovery` dataclass + `advise()` catalog + `probe_mode()` + `summary_popup()`), hooked into the four batch workers in `provision.py` (probe → advise → log → return a 3-tuple) and surfaced by `gui.py` (aggregated popup + transient per-row hint). No new runtime dependencies; all advice logic is unit-testable off-device.

**Tech Stack:** Python 3 stdlib only (`enum`, `dataclasses`, `os`), `unittest`. Tk for the popup glue (thin; the decision logic is pure and tested without a display).

## Global Constraints

- **Zero runtime dependencies** — stdlib only, like the rest of `cas/` (`theme.py` precedent).
- **OS-aware copy, pure ASCII** in any string that could reach a Windows-consumed script path; recovery strings are shown in the GUI/log only (not device-shell-consumed), but keep them ASCII to match the repo's guard culture. The Windows-vs-POSIX branch keys on a `_is_windows()` helper (monkeypatchable), never a bare `os.name` inline, so tests can flip it.
- **GUI decisions live in pure functions** tested without a display (`tests/test_ui.py` philosophy); Tk calls (`messagebox`, tree render) are thin wrappers over pure helpers.
- **Failures stay isolated** — a probe/advise error must never mask or replace the original operation failure (wrap every probe in try/except).
- **Lock's by-design adb disconnect is not a failure** — a seal that logs its "SEALED" completion marker before adb drops resolves to success/`SEALED_OK`, never the attention popup.
- Results are `{serial: (status, detail)}` today; this plan widens failing entries to `(status, detail, Recovery|None)`. `log_run` (`provision.py:1088-1090`) and `_report` (`gui.py:799-802`) already index only `[0]`/`[1]`, so 3-tuples are backward-compatible — verify, don't re-plumb.

---

### Task 1: `cas/recovery.py` — DeviceMode, Recovery, advise(), summary_popup()

**Files:**
- Create: `cas/recovery.py`
- Test: `tests/test_recovery.py`

**Interfaces:**
- Produces:
  - `class DeviceMode(enum.Enum)` with members `BOOTED_ADB, ADB_OFFLINE, FASTBOOT, FASTBOOTD, EDL_9008, ABSENT, SEALED_OK`.
  - `@dataclass class Recovery` with fields `state_label: str`, `steps: list[str]`, `operation: str`, `needs_attention: bool = True`; methods `log_block() -> str`, `row_hint() -> str`, `popup_line(serial: str) -> str`.
  - `advise(operation: str, phase: str, mode: DeviceMode) -> Recovery`. `operation ∈ {"root","save","download","warmup","lock"}`.
  - `summary_popup(recs: dict, action: str) -> str | None` — `recs` is `{serial: Recovery|None}`; returns the multi-line popup text, or `None` when nothing needs attention.
  - `_is_windows() -> bool` (module-level, monkeypatchable).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_recovery.py`:

```python
"""Tests for the recovery-guidance catalog (cas/recovery.py). Pure — no device, no Tk."""
import sys, pathlib, unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from cas import recovery as R
from cas.recovery import DeviceMode as M

OPS = ["root", "save", "download", "warmup", "lock"]


class TestAdvise(unittest.TestCase):
    def test_every_operation_x_mode_gives_nonempty_ordered_steps(self):
        for op in OPS:
            for mode in M:
                rec = R.advise(op, "", mode)
                self.assertEqual(rec.operation, op)
                self.assertTrue(rec.steps, f"{op}/{mode} has no steps")
                self.assertTrue(rec.state_label, f"{op}/{mode} has no state label")

    def test_edl_says_hold_power_and_names_the_operation(self):
        rec = R.advise("root", "edl_flash", M.EDL_9008)
        blob = " ".join(rec.steps).lower()
        self.assertIn("hold", blob)
        self.assertIn("power", blob)
        self.assertIn("12", blob)                       # ~12s hold
        self.assertIn("root", " ".join(rec.steps).lower())   # retry verb names the op

    def test_fastboot_says_fastboot_reboot(self):
        rec = R.advise("root", "fastboot_flash", M.FASTBOOT)
        self.assertIn("fastboot reboot", " ".join(rec.steps).lower())

    def test_fastbootd_uses_the_same_reboot_advice_as_fastboot(self):
        self.assertIn("fastboot reboot", " ".join(R.advise("root", "", M.FASTBOOTD).steps).lower())

    def test_offline_says_replug_data_cable(self):
        rec = R.advise("download", "push", M.ADB_OFFLINE)
        blob = " ".join(rec.steps).lower()
        self.assertIn("replug", blob)
        self.assertIn("data", blob)                     # data cable, not charge-only

    def test_absent_with_edl_phase_gives_edl_advice(self):
        # tiebreaker: vanished during an EDL write -> almost certainly dark in 9008
        rec = R.advise("root", "edl_flash", M.ABSENT)
        self.assertIn("12", " ".join(rec.steps))        # EDL hold-power advice, not the generic absent one
        self.assertEqual(rec.state_label, R.advise("root", "", M.EDL_9008).state_label)

    def test_absent_with_fastboot_phase_gives_fastboot_advice(self):
        rec = R.advise("root", "fastboot_flash", M.ABSENT)
        self.assertIn("fastboot reboot", " ".join(rec.steps).lower())

    def test_sealed_ok_is_not_attention(self):
        rec = R.advise("lock", "done", M.SEALED_OK)
        self.assertFalse(rec.needs_attention)

    def test_windows_branch_points_at_setup_windows_bat(self):
        with mock.patch.object(R, "_is_windows", lambda: True):
            blob = " ".join(R.advise("root", "fastboot_flash", M.FASTBOOT).steps)
        self.assertIn("setup-windows.bat", blob)

    def test_posix_branch_points_at_udev_not_bat(self):
        with mock.patch.object(R, "_is_windows", lambda: False):
            blob = " ".join(R.advise("root", "fastboot_flash", M.FASTBOOT).steps).lower()
        self.assertIn("udev", blob)
        self.assertNotIn("setup-windows.bat", blob)

    def test_operation_safety_note_present(self):
        self.assertIn("untouched", " ".join(R.advise("save", "capture", M.ADB_OFFLINE).steps).lower())
        self.assertIn("idempotent", " ".join(R.advise("download", "push", M.ADB_OFFLINE).steps).lower())


class TestRenderers(unittest.TestCase):
    def test_row_hint_is_one_line(self):
        rec = R.advise("root", "edl_flash", M.EDL_9008)
        self.assertNotIn("\n", rec.row_hint())
        self.assertIn(rec.state_label.split(" ")[0][:3].lower(), rec.row_hint().lower())

    def test_popup_line_carries_the_serial(self):
        rec = R.advise("root", "edl_flash", M.EDL_9008)
        self.assertIn("MQ66", rec.popup_line("MQ66123"))

    def test_log_block_is_multiline_with_state_and_steps(self):
        block = R.advise("root", "edl_flash", M.EDL_9008).log_block()
        self.assertIn("\n", block)
        self.assertIn("EDL", block)


class TestSummaryPopup(unittest.TestCase):
    def test_none_when_nothing_needs_attention(self):
        recs = {"A": R.advise("lock", "done", M.SEALED_OK), "B": None}
        self.assertIsNone(R.summary_popup(recs, "Lock"))

    def test_lists_each_attention_device_once(self):
        recs = {
            "MQ66A": R.advise("root", "edl_flash", M.EDL_9008),
            "RP6B": R.advise("root", "fastboot_flash", M.FASTBOOT),
            "OK1": R.advise("lock", "done", M.SEALED_OK),   # excluded
        }
        text = R.summary_popup(recs, "Root")
        self.assertIn("MQ66A", text)
        self.assertIn("RP6B", text)
        self.assertNotIn("OK1", text)
        self.assertIn("2", text)                            # "2 device(s) need attention"


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "$(git rev-parse --show-toplevel)" && python -m unittest tests.test_recovery -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cas.recovery'`.

- [ ] **Step 3: Write `cas/recovery.py`**

```python
"""Post-failure recovery guidance: given an operation, a coarse phase, and the device's probed mode,
produce ordered, OS-aware 'here is the state, here is what to do next' steps. Pure (advise/summary) +
a best-effort device probe. Stdlib only. Surfaced by provision.py (per worker) and gui.py (popup + row
hint). See docs/superpowers/specs/2026-07-16-recovery-guidance-design.md."""
import dataclasses
import enum
import os

from . import adb as _adb


class DeviceMode(enum.Enum):
    BOOTED_ADB = "booted"          # reachable in adb
    ADB_OFFLINE = "offline"        # present but offline/unauthorized, or mid-reboot
    FASTBOOT = "fastboot"          # bootloader fastboot
    FASTBOOTD = "fastbootd"        # userspace fastbootd (advised identically to FASTBOOT)
    EDL_9008 = "edl"               # Qualcomm EDL / 9008 (black screen)
    ABSENT = "absent"              # not in adb, fastboot, or EDL
    SEALED_OK = "sealed"           # Lock finished, adb gone BY DESIGN — not a failure


def _is_windows():
    """Monkeypatchable OS check (tests flip this instead of os.name)."""
    return os.name == "nt"


_OP_NAME = {"root": "Root", "save": "Save", "download": "Download",
            "warmup": "Warm up", "lock": "Lock"}

# Per-operation safety note appended to every attention block so the operator knows retry is safe.
_OP_SAFETY = {
    "root": "The unit is unharmed — a failed root leaves it bootable; nothing was sealed.",
    "save": "Your existing profile was left untouched — a failed Save never overwrites the good golden.",
    "download": "Download is idempotent — re-running re-pushes the payload cleanly.",
    "warmup": "Warm-up changes nothing persistent — safe to re-run once the unit is booted.",
    "lock": "The unit may be partially sealed — re-run Lock to finish (safe to repeat).",
}


def _driver_hint_fastboot():
    if _is_windows():
        return ("If `fastboot devices` is empty, the bootloader USB driver is missing — "
                "run scripts\\setup-windows.bat (Administrator), then replug.")
    return "If `fastboot devices` is empty, install the android-udev rules, then replug."


def _driver_hint_edl():
    if _is_windows():
        return ("Windows needs the QDLoader 9008 driver + QPST host tools — "
                "run scripts\\install-edl-host-tools.ps1 if EDL tooling is missing.")
    return "On Linux the /dev/ttyUSB port needs access — scripts/setup-linux.sh installs the udev rule."


@dataclasses.dataclass
class Recovery:
    state_label: str
    steps: list
    operation: str
    needs_attention: bool = True

    def log_block(self):
        lines = [f"  STATE: {self.state_label}", "  DO NEXT:"]
        lines += [f"    {i}. {s}" for i, s in enumerate(self.steps, 1)]
        return "\n".join(lines)

    def row_hint(self):
        first = self.steps[0] if self.steps else ""
        return f"{self.state_label} — {first}".replace("\n", " ")

    def popup_line(self, serial):
        first = self.steps[0] if self.steps else ""
        return f"  {serial}  {self.state_label} — {first}".replace("\n", " ")


def _effective_mode(phase, mode):
    """When the device is ABSENT (nothing visible), fall back to the phase to guess the mode: a unit that
    vanished during an EDL write is dark in 9008; during a fastboot write it's in the bootloader."""
    if mode is DeviceMode.ABSENT:
        if phase == "edl_flash":
            return DeviceMode.EDL_9008
        if phase == "fastboot_flash":
            return DeviceMode.FASTBOOT
    return mode


def advise(operation, phase, mode):
    op_verb = _OP_NAME.get(operation, operation)
    eff = _effective_mode(phase, mode)
    safety = _OP_SAFETY.get(operation, "")

    if eff is DeviceMode.SEALED_OK:
        return Recovery("SEALED (adb disconnects by design)",
                        ["Nothing to do — the unit sealed and adb went away as expected."],
                        operation, needs_attention=False)

    if eff is DeviceMode.EDL_9008:
        steps = [f"Hold Power ~12s to boot back to Android, then replug and re-run {op_verb}.",
                 _driver_hint_edl(), safety]
        return Recovery("EDL / 9008 (black screen)", [s for s in steps if s], operation)

    if eff in (DeviceMode.FASTBOOT, DeviceMode.FASTBOOTD):
        steps = [f"Run `fastboot reboot` to return to Android, then re-run {op_verb}.",
                 _driver_hint_fastboot(), safety]
        return Recovery("fastboot / bootloader", [s for s in steps if s], operation)

    if eff is DeviceMode.ADB_OFFLINE:
        steps = ["Wait ~30s for the unit to reappear; if it returns 'unauthorized', unlock the screen "
                 "and tap 'Allow USB debugging'.",
                 f"If it stays gone, replug a DATA cable (not charge-only) and re-run {op_verb}.", safety]
        return Recovery("offline / rebooting", [s for s in steps if s], operation)

    if eff is DeviceMode.ABSENT:
        steps = [f"Hold Power ~10-12s to force a reboot, watch for the boot logo, replug, re-run {op_verb}.",
                 "If it never shows in adb/fastboot/EDL, try a different cable or USB port.", safety]
        return Recovery("not visible (black screen / cable?)", [s for s in steps if s], operation)

    # BOOTED_ADB — still online, so the failure is operational, not a mode problem.
    online = {
        "root": "Root reported a failure but the unit is still online — check the log above; safe to re-run Root.",
        "save": "Not rooted? run Root first. Otherwise the capture hit an error — check the log; re-run Save.",
        "download": "Restore reported an error — check the log above; the unit is still online, re-run Download.",
        "warmup": "An app failed to launch (maybe not installed) — run Download first, then re-run Warm up.",
        "lock": "Lock reported a failure but the unit is still online — check the log; re-run Lock.",
    }
    return Recovery("still online", [online.get(operation, f"Re-run {op_verb}."), safety], operation)


def _fastboot_present(fb):
    """True iff a device is listed in `fastboot devices` (any non-empty line)."""
    try:
        out = fb.devices() or ""
    except Exception:
        return False
    return any(ln.strip() for ln in out.splitlines())


def probe_mode(adb, fb, edl_ports=None):
    """Best-effort current mode for this device. adb: Adb; fb: Fastboot; edl_ports: callable()->list
    (defaults to adb._edl_ports). Every probe is wrapped so a probe error never raises into the caller."""
    edl_ports = edl_ports or _adb._edl_ports
    try:
        st = adb.state()
    except Exception:
        st = ""
    if st == "device":
        return DeviceMode.BOOTED_ADB
    if st in ("offline", "unauthorized"):
        return DeviceMode.ADB_OFFLINE
    # st == "" -> not in adb; check fastboot, then EDL.
    if _fastboot_present(fb):
        return DeviceMode.FASTBOOT
    try:
        if edl_ports():
            return DeviceMode.EDL_9008
    except Exception:
        pass
    return DeviceMode.ABSENT


def summary_popup(recs, action):
    """One end-of-run dialog body listing every device that needs attention, or None if none do.
    `recs` is {serial: Recovery|None}."""
    hot = [(s, r) for s, r in recs.items() if r is not None and r.needs_attention]
    if not hot:
        return None
    head = f"{len(hot)} device(s) need attention after {action}:\n"
    return head + "\n".join(r.popup_line(s) for s, r in hot)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_recovery -v`
Expected: PASS (all tests in `TestAdvise`, `TestRenderers`, `TestSummaryPopup`).

- [ ] **Step 5: Commit**

```bash
git add cas/recovery.py tests/test_recovery.py
git commit -m "feat(recovery): device-state recovery catalog (advise/summary, pure)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `probe_mode` device-mode detection tests

`probe_mode` is already written in Task 1 (it lives in `recovery.py`). This task adds its device-facing tests with tiny fakes, kept separate because it is tested by posing a fake device in each mode rather than by the pure table.

**Files:**
- Modify: `tests/test_recovery.py` (append `TestProbeMode`)

**Interfaces:**
- Consumes: `recovery.probe_mode(adb, fb, edl_ports=None)`, `recovery.DeviceMode`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recovery.py`:

```python
class _FakeAdb:
    def __init__(self, state):
        self._state = state
    def state(self):
        return self._state

class _FakeFb:
    def __init__(self, present):
        self._present = present
    def devices(self):
        return "SERIAL123\tfastboot\n" if self._present else ""


class TestProbeMode(unittest.TestCase):
    def _probe(self, state, fb_present=False, edl=()):
        return R.probe_mode(_FakeAdb(state), _FakeFb(fb_present), edl_ports=lambda: list(edl))

    def test_device_state_is_booted(self):
        self.assertIs(self._probe("device"), M.BOOTED_ADB)

    def test_offline_is_adb_offline(self):
        self.assertIs(self._probe("offline"), M.ADB_OFFLINE)

    def test_unauthorized_is_adb_offline(self):
        self.assertIs(self._probe("unauthorized"), M.ADB_OFFLINE)

    def test_absent_in_adb_but_in_fastboot_is_fastboot(self):
        self.assertIs(self._probe("", fb_present=True), M.FASTBOOT)

    def test_absent_in_adb_and_fastboot_but_edl_port_present_is_edl(self):
        self.assertIs(self._probe("", fb_present=False, edl=["/dev/ttyUSB0"]), M.EDL_9008)

    def test_nothing_anywhere_is_absent(self):
        self.assertIs(self._probe("", fb_present=False, edl=[]), M.ABSENT)

    def test_a_probe_exception_never_raises(self):
        class Boom:
            def state(self):
                raise RuntimeError("adb died")
        self.assertIs(R.probe_mode(Boom(), _FakeFb(False), edl_ports=lambda: []), M.ABSENT)
```

- [ ] **Step 2: Run tests to verify they pass immediately**

Run: `python -m unittest tests.test_recovery.TestProbeMode -v`
Expected: PASS (implementation already exists from Task 1). If any fail, fix `probe_mode` in `cas/recovery.py` — do NOT change the tests.

- [ ] **Step 3: Commit**

```bash
git add tests/test_recovery.py
git commit -m "test(recovery): probe_mode resolves each device mode + never raises

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire recovery into the four batch workers in `provision.py`

**Files:**
- Modify: `cas/provision.py` — add `from . import recovery as RC` near the top imports; edit the workers in `root_all` (~1725-1740), `provision_all` (~723+), `warmup_all` (~999+), `seal_all` (~1746+).
- Test: `tests/test_cas.py` (append to the provision test class)

**Interfaces:**
- Consumes: `RC.probe_mode(adb, fb)`, `RC.advise(op, phase, mode)`, `RC.DeviceMode`.
- Produces: workers now return `(status, detail, Recovery|None)` on failure; `("ok", name)` unchanged on success (a 2-tuple — consumers tolerate both).

**Helper (add once, module-level in `provision.py`):**

```python
def _fail_with_recovery(operation, phase, adb, fb, status, detail, log):
    """Probe the device, build recovery guidance, log it live, and return the (status, detail, Recovery)
    3-tuple the GUI surfaces. Never raises — a probe error degrades to no guidance."""
    try:
        mode = RC.probe_mode(adb, fb)
        rec = RC.advise(operation, phase, mode)
        log(rec.log_block())
    except Exception as e:                       # guidance is best-effort; never mask the real failure
        log(f"(recovery hint unavailable: {e})")
        rec = None
    return (status, detail, rec)
```

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cas.py` (in the provision test class, near `test_provision_pushes_the_captured_wifi_store`):

```python
    def test_root_all_failure_carries_recovery_guidance(self):
        # A failed Root must return a 3-tuple whose 3rd element is a Recovery, and log the DO-NEXT block.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            logs = []
            # Force root() to fail by giving a runner that reports NOT rooted the whole time.
            r = FakeRunner(root=False)
            res = PV.root_all(lambda s: Adb(runner=r), lambda s: Fastboot(runner=r),
                              [("MQ66TEST", "device")], profiles_root=t, appdir=t,
                              profile=prof, log=logs.append, parallel=False)
            status, detail, *rest = res["MQ66TEST"] if isinstance(res["MQ66TEST"], (list, tuple)) else (res["MQ66TEST"], "")
            self.assertEqual(status, "fail")
            self.assertTrue(rest, "no Recovery element on the failing result")
            rec = rest[0]
            self.assertIsNotNone(rec)
            self.assertTrue(rec.steps)
            self.assertIn("DO NEXT", "\n".join(logs))     # the live log carried the guidance block
```

(If `root=False` short-circuits before the worker's flash path in a way that returns a different status, adjust the fake to reach a `fail` — e.g. `FakeRunner(su_blocked=False, root=True)` with a `flash`-failing runner. The assertion that matters: a `fail`/`error` result is a 3-tuple with a non-None `Recovery`, and the log contains `DO NEXT`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_cas.TestProvision.test_root_all_failure_carries_recovery_guidance -v`
Expected: FAIL — result is a 2-tuple `("fail", detail)`, `rest` is empty.

- [ ] **Step 3: Implement — add the import + helper, then edit each worker**

Add near the other `from . import ...` lines at the top of `cas/provision.py`:

```python
from . import recovery as RC
```

Add the `_fail_with_recovery` helper (shown above) at module level.

**`root_all` worker** — replace the failing returns. The worker knows the flasher kind, so set `phase` once:

```python
            phase = "edl_flash" if (fw is not None and getattr(fw, "flash_method", "") == "edl") else "fastboot_flash"
            ...
            ok = root(adb, fb, stock_path, ...)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", prof.name)
            if ok:
                return ("ok", prof.name)
            return _fail_with_recovery("root", phase, adb, fb, "fail",
                                       msgs[-1] if msgs else prof.name, _wlog)
        except Exception as e:
            log(f"[{serial}] ERROR: {e}")
            return _fail_with_recovery("root", "", adb, fb, "error", str(e), log)
```

(For the earlier `("fail", msg)` / `("fail", reason)` early returns in `root_all` — the EDL-no-firmware and EDL-unusable branches — leave them as 2-tuples: those fail BEFORE any reboot, the device is still booted in adb, and their message already IS the actionable fix. Optionally wrap them too with `phase=""`; not required.)

**`provision_all` worker** — its failing return(s) become:

```python
            return _fail_with_recovery("download", "push", adb, fb, "fail", detail, log)
        except Exception as e:
            ...
            return _fail_with_recovery("download", "", adb, fb, "error", str(e), log)
```

(Use the fastboot handle if the worker has one; Download has no fastboot phase, so `fb` may be a fresh `make_fb(serial)` or `None` — `probe_mode` tolerates `fb=None`? No: guard by passing a real Fastboot. If the worker has no `make_fb`, add one, or pass a Fastboot built from the same runner. `_fastboot_present` wraps its call in try/except, so a Fastboot that errors just yields no fastboot signal.)

**`warmup_all` worker** — `phase="launch"`:

```python
            return _fail_with_recovery("warmup", "launch", adb, fb, "fail", detail, log)
```

**`seal_all` worker** — add the SEALED_OK guard (Lock's by-design disconnect). Capture the seal log lines like `root_all` does (`msgs`/`_wlog`), then:

```python
            ok = seal(adb, fb, ...)
            sealed_marker = any("SEALED" in m for m in msgs)   # seal() logs "Device is SEALED" as its last step
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", prof.name)
            if ok:
                return ("ok", prof.name)
            if sealed_marker:
                # The un-root/scrub completed and adb dropped as designed — this is success, not a failure.
                rec = RC.advise("lock", "done", RC.DeviceMode.SEALED_OK)
                return ("ok", "sealed (adb dropped after the seal completed)", rec)
            phase = "edl_flash" if (fw is not None and getattr(fw, "flash_method", "") == "edl") else "fastboot_flash"
            return _fail_with_recovery("lock", phase, adb, fb, "fail",
                                       msgs[-1] if msgs else prof.name, _wlog)
        except Exception as e:
            log(f"[{serial}] ERROR: {e}")
            return _fail_with_recovery("lock", "", adb, fb, "error", str(e), log)
```

(If `seal_all`'s current worker does not already capture `msgs`, add the `_wlog`/`msgs` pattern copied from `root_all` — pass `_wlog` as `seal`'s `log`.)

- [ ] **Step 4: Run the new test + the full provision suite**

Run: `python -m unittest tests.test_cas.TestProvision.test_root_all_failure_carries_recovery_guidance -v`
Expected: PASS.

Run: `python -m unittest tests.test_cas -v 2>&1 | tail -5`
Expected: OK — no regressions (log_run / _report tolerate the 3-tuples).

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(recovery): workers probe + advise on failure, return guidance 3-tuple

root/download/warmup/lock now probe the device on failure and attach a Recovery
(logged live + carried in the run-history reason). Lock's by-design adb drop after
a completed seal resolves to SEALED_OK (success, never an attention popup).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Surface guidance in the GUI — end-of-run popup + transient row hint

**Files:**
- Modify: `cas/gui.py` — `_run_bg` (init `self._last_recs`), `done()` (aggregate → popup + stash `self._last_fail`), `_run_chain_core` (collect per-stage recs), `_populate_devices` (render/clear the row hint), and a pure `_collect_recs` staticmethod + a `_state_cell` hint variant.
- Test: `tests/test_ui.py` (append pure-helper tests)

**Interfaces:**
- Consumes: `recovery.summary_popup(recs, action)`, `Recovery.row_hint()`, and the 3-tuple results from Task 3.
- Produces: `App._collect_recs(result) -> dict` (pure); `App._last_fail: dict[serial,str]`; a `_state_cell_hinted(state, hint)` module helper (pure).

- [ ] **Step 1: Write the failing pure-helper tests**

Append to `tests/test_ui.py`:

```python
class TestRecoverySurfacing(unittest.TestCase):
    def test_collect_recs_pulls_the_third_tuple_element(self):
        from cas.gui import App
        from cas import recovery as R
        rec = R.advise("root", "edl_flash", R.DeviceMode.EDL_9008)
        result = {"A": ("fail", "boom", rec), "B": ("ok", "prof"), "C": ("fail", "x")}
        got = App._collect_recs(result)
        self.assertIs(got["A"], rec)          # A carries a Recovery
        self.assertIsNone(got.get("B"))       # ok device -> no rec
        self.assertIsNone(got.get("C"))       # 2-tuple fail -> no rec (None)

    def test_collect_recs_tolerates_non_dict(self):
        from cas.gui import App
        self.assertEqual(App._collect_recs(True), {})
        self.assertEqual(App._collect_recs(None), {})

    def test_state_cell_hinted_appends_the_hint(self):
        from cas.gui import _state_cell_hinted
        cell = _state_cell_hinted("offline", "EDL / 9008 — Hold Power ~12s")
        self.assertIn("offline", cell)
        self.assertIn("EDL", cell)

    def test_state_cell_hinted_without_hint_matches_plain(self):
        from cas.gui import _state_cell_hinted, _state_cell
        self.assertEqual(_state_cell_hinted("device", None), _state_cell("device"))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_ui.TestRecoverySurfacing -v`
Expected: FAIL — `_collect_recs` / `_state_cell_hinted` don't exist.

- [ ] **Step 3: Implement**

Add the module helper next to `_state_cell` in `cas/gui.py`:

```python
def _state_cell_hinted(state, hint):
    """The state-column text, with a transient failure hint appended (red row already conveys trouble)."""
    base = _state_cell(state)
    return f"{base}  ⚠ {hint}" if hint else base
```

Add the pure collector as a staticmethod on `App`:

```python
    @staticmethod
    def _collect_recs(result):
        """{serial: Recovery} from a batch result dict — the 3rd tuple element where present. Non-dict -> {}."""
        if not isinstance(result, dict):
            return {}
        out = {}
        for s, v in result.items():
            out[s] = v[2] if isinstance(v, (tuple, list)) and len(v) > 2 else None
        return out
```

In `_run_bg`, right after `self.cancel_event = threading.Event()`:

```python
        self._last_recs = {}          # {serial: Recovery} collected during a chain run (single-stage uses the result dict)
```

In `_run_bg.done()`, replace the `self._report(...)` + retry block with (keep the retry logic, insert the recovery surfacing BEFORE it):

```python
            self._report(self._action, result_box.get("r"))
            recs = self._collect_recs(result_box.get("r"))
            recs.update({s: r for s, r in getattr(self, "_last_recs", {}).items() if r is not None})
            self._surface_recovery(self._action, recs)
            # if the op armed a retry ... (unchanged)
```

Add the surfacing method on `App`:

```python
    def _surface_recovery(self, action, recs):
        """One end-of-run popup for the devices that need attention, and stash transient per-row hints."""
        from . import recovery as RC
        self._last_fail = {s: r.row_hint() for s, r in recs.items()
                           if r is not None and r.needs_attention}
        text = RC.summary_popup(recs, action)
        if text:
            messagebox.showwarning(f"CAS — {action}: devices need attention", text)
```

Initialise `self._last_fail = {}` in `App.__init__` (near `self._retry_ctx = None`).

In `_run_chain_core`, capture per-stage recs as devices drop — after the `_stage` call and the `survivors = [...]` filter:

```python
                res = self._stage(step, survivors, pm, force, cev, wait_boot=wb)
                for s in survivors:
                    st = res.get(s)
                    if isinstance(st, (tuple, list)) and st and st[0] in ("fail", "error") \
                            and len(st) > 2 and st[2] is not None:
                        self._last_recs[s] = st[2]
                survivors = [s for s in survivors if res.get(s, ("error",))[0] not in ("fail", "error")]
```

In `_populate_devices`, render the hint and clear it when healthy. Change the `values=` state cell:

```python
            hint = getattr(self, "_last_fail", {}).get(serial)
            if state == "device":
                hint = None
                if hasattr(self, "_last_fail"):
                    self._last_fail.pop(serial, None)   # cleared: the unit is healthy again
            self.dev_tree.insert("", "end", iid=serial, text=serial,
                                 values=(model, sd, _profile_cell(shown, manual),
                                         self._fw_cell(serial), _state_cell_hinted(state, hint)),
                                 tags=tags)
```

- [ ] **Step 4: Run the UI tests + full suite**

Run: `python -m unittest tests.test_ui.TestRecoverySurfacing -v`
Expected: PASS.

Run: `python -m unittest discover -s tests -p "test_*.py" 2>&1 | grep -E "^(Ran|OK|FAILED)"`
Expected: `OK`.

Run (shell suites unaffected, sanity): `bash tests/test_wifi.sh`
Expected: `PASS: wifi helpers`.

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py tests/test_ui.py
git commit -m "feat(recovery): GUI surfaces guidance — end-of-run popup + per-row hint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- `DeviceMode`, `probe_mode`, `advise`, `Recovery` renderers → Task 1 + 2. ✓
- Recovery catalog (every mode + per-op safety note + OS branch) → Task 1. ✓
- Phase→expected-mode fallback → Task 1 (`_effective_mode`). ✓
- Worker hook (probe + advise + live log + 3-tuple + history reason) → Task 3. ✓
- Lock `SEALED_OK` guard → Task 3 (`sealed_marker`). ✓
- End-of-run summary popup → Task 4 (`_surface_recovery` + `summary_popup`). ✓
- Device-row hint (transient, cleared when healthy) → Task 4 (`_last_fail` + `_populate_devices`). ✓
- Testing (advise table, OS branch, SEALED_OK, probe modes, integration, popup aggregation) → Tasks 1-4. ✓

**Placeholder scan:** No TBD/TODO. The one soft spot is Task 3 Step 1's parenthetical about adjusting the fake to force a `fail` — that is guidance for a real-device-shaped test, and the invariant assertion is stated explicitly; acceptable.

**Type consistency:** `Recovery` fields/methods (`state_label`, `steps`, `operation`, `needs_attention`, `log_block`, `row_hint`, `popup_line`) are used identically in Tasks 3-4. `advise(operation, phase, mode)` and `probe_mode(adb, fb, edl_ports=None)` signatures match every call site. `summary_popup(recs, action)` and `_collect_recs(result)` shapes agree. ✓

## Risks

- **Concurrent session** editing `provision.py`/`gui.py`/`test_cas.py` on this shared checkout (operator opted in). Before each task's commit: `git log --oneline -3` for foreign commits, and re-run the full suite. Keep each task's diff tight so a mid-task foreign commit is easy to reconcile.
- `provision_all`/`warmup_all` may not currently build a `Fastboot` per worker. If so, pass `make_fb(serial)` (Download/Warm-up have no flash but `probe_mode` only reads `fastboot devices`, which is harmless). `_fastboot_present` swallows errors, so a missing/again-failing fastboot just yields no fastboot signal.
