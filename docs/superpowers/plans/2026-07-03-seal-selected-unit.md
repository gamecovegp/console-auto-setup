# Seal selected unit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single-unit "Seal selected unit (retail lock)…" action to the CAS Settings menu that runs the full retail seal on the one selected device.

**Architecture:** A new `App.seal_selected()` method mirrors the existing `release_selected()` but calls `PV.seal_all` scoped to a one-device list `[(serial, "device")]`. All firmware/EDL/model-match/golden-guard behavior is inherited from `seal_all` — zero new provisioning logic. One Settings menu entry surfaces it.

**Tech Stack:** Python 3, Tkinter, stdlib `unittest`/`unittest.mock`, pytest runner.

## Global Constraints

- Reuse `PV.seal_all` — do NOT add any new provisioning/flashing logic in the GUI.
- Behavior must match the batch ③ Lock exactly (single-device slice); no divergence.
- Mirror `release_selected()` structure (`cas/gui.py:1959`) for the guard + confirm + `_run_bg` pattern.
- GUI tests run headless via `App.__new__(App)` (bypass Tk `__init__`) — never construct a real window.
- Test runner: `python -m pytest tests/test_cas.py`.

---

### Task 1: `seal_selected()` action + Settings menu entry

**Files:**
- Modify: `cas/gui.py` — add `seal_selected()` immediately before `release_selected` (currently line 1959); add one menu line in the Settings menu block (currently line 219-220).
- Test: `tests/test_cas.py` — new `TestSealSelected` class (append near `TestRunChain`, ~line 2423).

**Interfaces:**
- Consumes (all already exist in `cas/gui.py`):
  - `self._selected_serial() -> str | None` (`gui.py:1127`)
  - `self._profile_map(serials) -> (dict[str, Profile|None], set[str])` (`gui.py:1505`)
  - `self._run_bg(fn, label=...)` (`gui.py:756`)
  - `self._on_flash_critical(active)` (`gui.py:838`)
  - `PV.seal_all(make_adb, make_fb, devices, *, profiles_root, appdir, log, profile_map, force_serials, on_critical) -> dict[str, tuple]` (`provision.py:1154`)
  - module-level `Adb`, `Fastboot`, `APPDIR`, `PV`, `messagebox` (imported at `gui.py:18-26`)
- Produces: `self.seal_selected(self) -> None` (Settings menu command; runs the seal in the background, returns nothing).

- [ ] **Step 1: Write the failing tests**

Append this class to `tests/test_cas.py` (after the `TestRunChain` class, before `TestResolveChain`):

```python
class TestSealSelected(unittest.TestCase):
    """Settings ▸ 'Seal selected unit' — single-device slice of ③ Lock via PV.seal_all."""

    def _app(self, serial="S1"):
        from cas.gui import App
        app = App.__new__(App)                    # bypass Tk __init__
        app.adb_bin = app.fb_bin = None
        app.profiles_root = "."
        app.assigned = {"S1": "p"}
        app.assigned_manual = set()               # S1 not hand-assigned → force stays empty
        app.cancel_event = None
        app.log = lambda m: None
        app._on_flash_critical = lambda active: None
        app.refresh_devices = lambda: None
        # win.after(0, cb) must invoke cb (work() calls self.win.after(0, self.refresh_devices))
        app.win = type("W", (), {"after": lambda self, ms, cb=None: (cb() if cb else None)})()
        app._selected_serial = lambda: serial
        # _run_bg: run the work fn synchronously so the seal_all call happens in-test
        app._bg = []
        app._run_bg = lambda fn, label=None: app._bg.append((label, fn()))
        return app

    def test_no_selection_shows_info_and_does_not_seal(self):
        from unittest import mock
        import cas.gui as G
        app = self._app(serial=None)
        with mock.patch.object(G.messagebox, "showinfo") as info, \
             mock.patch.object(G.PV, "seal_all") as seal:
            app.seal_selected()
        info.assert_called_once()
        seal.assert_not_called()

    def test_confirm_no_does_not_seal(self):
        from unittest import mock
        import cas.gui as G
        app = self._app()
        with mock.patch.object(G.messagebox, "askyesno", return_value=False), \
             mock.patch.object(G.PV, "seal_all") as seal:
            app.seal_selected()
        seal.assert_not_called()

    def test_confirm_yes_seals_the_one_selected_unit(self):
        from unittest import mock
        import cas.gui as G
        app = self._app()
        rec = {}
        def fake_seal_all(mk_adb, mk_fb, devices, **kw):
            rec["devices"] = list(devices)
            rec["profile_map"] = kw.get("profile_map")
            rec["force"] = kw.get("force_serials")
            return {"S1": ("ok", "p")}
        with mock.patch.object(G.messagebox, "askyesno", return_value=True), \
             mock.patch.object(G.PV, "seal_all", side_effect=fake_seal_all):
            app.seal_selected()
        self.assertEqual(rec["devices"], [("S1", "device")])   # only the selected unit
        self.assertIn("S1", rec["profile_map"])                # resolved via _profile_map
        self.assertEqual(rec["force"], set())                  # S1 not hand-assigned
        self.assertEqual(app._bg[0][1], {"S1": ("ok", "p")})   # work() returns the report dict
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_cas.py::TestSealSelected -v`
Expected: FAIL — `AttributeError: 'App' object has no attribute 'seal_selected'`

- [ ] **Step 3: Implement `seal_selected()`**

In `cas/gui.py`, insert this method immediately **before** `def release_selected(self):` (currently line 1959):

```python
    def seal_selected(self):
        """Operator-only: retail-SEAL the one selected unit on demand (the single-device slice of ③ Lock),
        paired with 'Release selected unit'. Runs the full seal via PV.seal_all([one device]) so firmware /
        EDL flasher / model-match brick-guard / golden-guard all behave exactly as the batch Lock."""
        serial = self._selected_serial()
        if not serial:
            messagebox.showinfo("CAS", "Select ONE device in the list first.")
            return
        if not messagebox.askyesno(
                "CAS — seal (retail-lock) unit?",
                f"Retail-seal {serial}?\n\n"
                "This un-roots the unit (flashes stock init_boot, ~2-3 min), hides Developer options, "
                "and disables USB debugging — adb WILL disconnect. The golden is skipped.\n\n"
                "Use for a one-off / re-seal outside the ③ Lock batch. Assumes the unit is VERIFIED."):
            return
        pm, force = self._profile_map([serial])
        def work():
            cev = self.cancel_event
            res = PV.seal_all(
                lambda s: Adb(serial=s, adb=self.adb_bin, cancel=cev),
                lambda s: Fastboot(serial=s, fastboot=self.fb_bin, cancel=cev),
                [(serial, "device")],
                profiles_root=self.profiles_root, appdir=APPDIR, log=self.log,
                profile_map=pm, force_serials=force, on_critical=self._on_flash_critical)
            self.win.after(0, self.refresh_devices)
            return res
        self._run_bg(work, label=f"Sealing {serial}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cas.py::TestSealSelected -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Add the Settings menu entry**

In `cas/gui.py`, in the Settings menu block, add the Seal line directly **above** the existing Release line (currently line 220). Change:

```python
        setm.add_separator()
        setm.add_command(label="Release selected unit (un-provision)…", command=self.release_selected)
        bar.add_cascade(label="Settings", menu=setm)
```

to:

```python
        setm.add_separator()
        setm.add_command(label="Seal selected unit (retail lock)…", command=self.seal_selected)
        setm.add_command(label="Release selected unit (un-provision)…", command=self.release_selected)
        bar.add_cascade(label="Settings", menu=setm)
```

- [ ] **Step 6: Run the full suite to verify nothing regressed**

Run: `python -m pytest tests/test_cas.py -q`
Expected: PASS — all tests green (the prior 292 + 3 new = 295), 0 failures.

- [ ] **Step 7: Commit**

```bash
git add cas/gui.py tests/test_cas.py
git commit -m "feat(gui): Seal selected unit (retail lock) — single-unit action in Settings"
```

---

## Self-Review

**Spec coverage:**
- Menu entry above Release → Step 5. ✓
- `seal_selected()` reusing `seal_all` on `[(serial,"device")]` with `_profile_map` pm/force → Step 3. ✓
- Strong single-device confirm (un-roots, disables USB debugging, golden skipped) → Step 3 confirm text. ✓
- Returns the report dict for `_report` → Step 3 `return res`; asserted in Step 1 (`app._bg[0][1]`). ✓
- Edge cases (golden/no-profile/EDL/model-mismatch/cancel/busy) → all inherited from `seal_all`/`_run_bg`, no new code needed; not separately tested here because they are already covered by the existing `seal_all`/`seal` tests (`test_seal_*`, `test_seal_all_*`). ✓
- Three tests (no selection, confirm=no, confirm=yes with device/pm/force assertions) → Step 1. ✓

**Placeholder scan:** none — all code blocks are complete.

**Type consistency:** `seal_selected` uses `_selected_serial()` (→ str|None), `_profile_map([serial])` (→ (pm, force)), `PV.seal_all(..., profile_map=pm, force_serials=force, ...)` (dict return), `_run_bg(work, label=...)` — all match the real signatures verified in `cas/gui.py` and `cas/provision.py`.
