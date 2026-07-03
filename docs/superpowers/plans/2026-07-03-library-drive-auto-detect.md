# Library-drive Auto-detect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-detect when the external library drive (re)appears and self-heal the CAS GUI, on both Windows and Linux, without a manual Refresh.

**Architecture:** A single idle poller on the existing tkinter `after()` loop polls `App._lib_reachable()` (a plain `Path.is_dir()` stat — identical on Windows/Linux, no udev/WMI). A pure edge function decides the action; the watcher method dispatches it. On the unreachable→reachable edge it full-refreshes profiles+firmware+devices; deferred while a job runs; on removal it relabels honestly without wiping the Profile selection.

**Tech Stack:** Python 3, tkinter, `unittest` (canonical) / `pytest` (also collects). No new dependencies.

## Global Constraints

- No new third-party dependencies; standard library + tkinter only.
- Cross-platform: no OS-specific device-notification APIs. Reachability is `pathlib.Path(...).is_dir()` only.
- `cas/gui.py` already imports `tkinter as tk` (line 17) and `pathlib` via `from . import profiles as P` (use `P.pathlib.Path`, matching `_lib_reachable`).
- Poll interval: exactly `2000` ms.
- Watcher must never refresh while `self.busy` is `True` (a job is running).
- Tests follow the suite's headless style: module-level functions imported directly; `App` methods exercised via `App.__new__(App)` with only the needed attributes set (see `tests/test_cas.py` `TestRunChain._app`).
- Full-suite gate command: `python3 -m unittest discover -s tests -p 'test_*.py' -t .`

---

### Task 1: Pure edge function `_lib_watch_action`

Adds the module-level decision function that maps a reachability transition to an action. No GUI state — pure and directly unit-testable, mirroring the existing module-level `_profile_library_label` helper.

**Files:**
- Modify: `cas/gui.py` (add module-level function near `_profile_library_label`, ~line 41)
- Test: `tests/test_cas.py` (new `TestLibWatch` class at end of file)

**Interfaces:**
- Consumes: nothing.
- Produces: `_lib_watch_action(was: bool, now: bool, busy: bool) -> str | None` returning one of `"reconnect"`, `"disconnect"`, `"defer"`, or `None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cas.py`:

```python
class TestLibWatch(unittest.TestCase):
    def test_lib_watch_action_edges(self):
        from cas.gui import _lib_watch_action as act
        # no change → None, regardless of busy
        self.assertIsNone(act(True, True, False))
        self.assertIsNone(act(True, True, True))
        self.assertIsNone(act(False, False, False))
        self.assertIsNone(act(False, False, True))
        # unreachable → reachable while idle → full reconnect
        self.assertEqual(act(False, True, False), "reconnect")
        # unreachable → reachable while a job runs → defer (retry next tick)
        self.assertEqual(act(False, True, True), "defer")
        # reachable → unreachable → relabel, regardless of busy
        self.assertEqual(act(True, False, False), "disconnect")
        self.assertEqual(act(True, False, True), "disconnect")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cas.py::TestLibWatch::test_lib_watch_action_edges -q`
Expected: FAIL — `ImportError: cannot import name '_lib_watch_action' from 'cas.gui'`

- [ ] **Step 3: Write minimal implementation**

In `cas/gui.py`, add directly after the `_profile_library_label` function (after ~line 41):

```python
def _lib_watch_action(was, now, busy):
    """Edge decision for the idle library-drive watcher. Given the previously-seen
    reachability (`was`), the current reachability (`now`), and whether a job is running
    (`busy`), return the action to take: 'reconnect' (drive came back — full refresh),
    'disconnect' (drive removed — relabel), 'defer' (came back mid-job — retry later),
    or None (no change)."""
    if now == was:
        return None
    if now:                                  # unreachable → reachable
        return "defer" if busy else "reconnect"
    return "disconnect"                       # reachable → unreachable
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_cas.py::TestLibWatch::test_lib_watch_action_edges -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py tests/test_cas.py
git commit -m "feat(gui): pure edge fn for library-drive watcher

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `App._lib_watch` watcher + startup wiring + re-baseline

Adds the idle poller method, starts it in `__init__`, and re-baselines it when the library path is changed via the menu so a manual path change is not seen as a drive edge.

**Files:**
- Modify: `cas/gui.py`
  - add `App._lib_watch` method after `_update_lib_label` (~line 1969)
  - seed baseline + start loop in `App.__init__` after the startup refreshes (~line 186)
  - re-baseline inside `choose_library._applied` (~line 394)
- Test: `tests/test_cas.py` (extend `TestLibWatch`)

**Interfaces:**
- Consumes: `_lib_watch_action` (Task 1); existing `App._lib_reachable`, `App.refresh_profiles`, `App.refresh_firmware`, `App.refresh_devices`, `App._update_lib_label`, `App.log`, `App.win`, `App.busy`.
- Produces: `App._lib_watch(self) -> None` (self-rescheduling); attribute `App._lib_last_reachable: bool`.

- [ ] **Step 1: Write the failing tests**

Add to the `TestLibWatch` class in `tests/test_cas.py`:

```python
    def _watch_app(self, was, now, busy):
        from cas.gui import App
        app = App.__new__(App)                 # bypass Tk __init__
        app._lib_last_reachable = was
        app.busy = busy
        app._lib_reachable = lambda: now
        calls = []
        for name in ("refresh_profiles", "refresh_firmware",
                     "refresh_devices", "_update_lib_label"):
            setattr(app, name, lambda n=name: calls.append(n))
        app.log = lambda m: None
        after = []
        app.win = type("W", (), {"after": lambda self, ms, fn: after.append(ms)})()
        app._calls, app._after = calls, after
        return app

    def test_watch_reconnect_idle_full_refresh(self):
        app = self._watch_app(was=False, now=True, busy=False)
        app._lib_watch()
        self.assertTrue(app._lib_last_reachable)
        self.assertIn("refresh_profiles", app._calls)
        self.assertIn("refresh_firmware", app._calls)
        self.assertIn("refresh_devices", app._calls)
        self.assertEqual(app._after, [2000])           # rescheduled once

    def test_watch_reconnect_busy_defers(self):
        app = self._watch_app(was=False, now=True, busy=True)
        app._lib_watch()
        self.assertFalse(app._lib_last_reachable)      # baseline unchanged
        self.assertNotIn("refresh_profiles", app._calls)
        self.assertNotIn("refresh_devices", app._calls)
        self.assertEqual(app._after, [2000])           # still rescheduled

    def test_watch_disconnect_relabels_keeps_profiles(self):
        app = self._watch_app(was=True, now=False, busy=False)
        app._lib_watch()
        self.assertFalse(app._lib_last_reachable)
        self.assertIn("_update_lib_label", app._calls)
        self.assertIn("refresh_firmware", app._calls)
        self.assertNotIn("refresh_profiles", app._calls)   # selection preserved
        self.assertEqual(app._after, [2000])

    def test_watch_no_change_noop(self):
        app = self._watch_app(was=True, now=True, busy=False)
        app._lib_watch()
        self.assertEqual(app._calls, [])               # nothing but the reschedule
        self.assertEqual(app._after, [2000])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest "tests/test_cas.py::TestLibWatch" -q`
Expected: the four new `test_watch_*` FAIL with `AttributeError: 'App' object has no attribute '_lib_watch'` (the Task-1 edge test still passes).

- [ ] **Step 3: Write the watcher method**

In `cas/gui.py`, add this method immediately after `_update_lib_label` (after ~line 1969):

```python
    def _lib_watch(self):
        """Idle poll (every 2s): when the library drive (re)appears, self-heal the UI so
        the operator need not click Refresh. On the unreachable→reachable edge (while idle)
        re-resolve profiles, firmware and devices; if a job is running, defer to the next
        tick. On removal, relabel honestly WITHOUT calling refresh_profiles, so a transient
        USB drop does not wipe the operator's Profile selection. Reschedules itself; stops
        quietly once the window is destroyed."""
        try:
            now = self._lib_reachable()
            action = _lib_watch_action(self._lib_last_reachable, now, self.busy)
            if action == "reconnect":
                self._lib_last_reachable = True
                self.log("library drive detected — refreshed")
                self.refresh_profiles()
                self.refresh_firmware()
                self.refresh_devices()
            elif action == "disconnect":
                self._lib_last_reachable = False
                self.log("library drive removed")
                self._update_lib_label()
                self.refresh_firmware()
            # action in (None, "defer") → leave the baseline untouched
        except tk.TclError:
            return                            # window gone — stop rescheduling
        self.win.after(2000, self._lib_watch)
```

- [ ] **Step 4: Wire startup in `__init__`**

In `cas/gui.py`, the end of `App.__init__` currently reads (around line 183-186):

```python
        self._poll_log()
        self.refresh_profiles()
        self.refresh_devices()
        self._check_updates(manual=False)        # silent startup check; prompts only if newer exists
```

Change it to add the two watcher lines at the end:

```python
        self._poll_log()
        self.refresh_profiles()
        self.refresh_devices()
        self._check_updates(manual=False)        # silent startup check; prompts only if newer exists
        self._lib_last_reachable = self._lib_reachable()   # seed the drive-watcher baseline
        self._lib_watch()                                  # idle poll: auto-refresh when the library drive (re)appears
```

- [ ] **Step 5: Re-baseline on manual library change**

In `cas/gui.py`, the `_applied` inner function of `choose_library` currently reads (around line 393-398):

```python
        def _applied():
            self.profiles_root = str(library_root())
            self._update_lib_label()
            self.refresh_profiles()
            self.refresh_firmware()
            self.refresh_devices()
```

Add one line right after `self.profiles_root = ...`:

```python
        def _applied():
            self.profiles_root = str(library_root())
            self._lib_last_reachable = self._lib_reachable()   # re-baseline: a path change is not a drive edge
            self._update_lib_label()
            self.refresh_profiles()
            self.refresh_firmware()
            self.refresh_devices()
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `python3 -m pytest "tests/test_cas.py::TestLibWatch" -q`
Expected: PASS (5 passed)

- [ ] **Step 7: Run the full suite (regression gate)**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -t .`
Expected: OK — no failures/errors (the prior green count + 5 new tests).

- [ ] **Step 8: Commit**

```bash
git add cas/gui.py tests/test_cas.py
git commit -m "feat(gui): auto-detect library drive (re)connect via idle 2s poll

Idle after()-loop watcher stat-checks _lib_reachable() (portable is_dir(),
no udev/WMI). unreachable→reachable while idle full-refreshes profiles+
firmware+devices; deferred mid-job; removal relabels without wiping the
Profile selection. Started in __init__, re-baselined on manual library change.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Universal `is_dir()` poll, no udev/WMI → Task 1 fn + Task 2 method (`_lib_reachable`). ✓
- Pure edge function, unit-tested → Task 1. ✓
- 2000 ms self-rescheduling watcher, TclError-guarded → Task 2 Step 3. ✓
- Reconnect = full refresh; defer while busy; disconnect = relabel without `refresh_profiles` → Task 2 Steps 1 (tests) + 3 (impl). ✓
- Startup seed + start → Task 2 Step 4. ✓
- Re-baseline in `choose_library` → Task 2 Step 5. ✓
- Non-goals (instant events, separate firmware drive, refresh-while-busy) → honored (not implemented). ✓
- Test plan (edge table + wiring via `App.__new__`) → Task 1 + Task 2 tests. ✓

**Placeholder scan:** none — all steps carry concrete code/commands.

**Type consistency:** `_lib_watch_action(was, now, busy)` returns `"reconnect"|"disconnect"|"defer"|None`; consumed with the exact same string literals in `_lib_watch`. Attribute `_lib_last_reachable` set in `__init__`, `choose_library`, and both branches of `_lib_watch`; read in `_lib_watch`. `win.after(2000, self._lib_watch)` matches the test's recorded `2000`. Consistent.
