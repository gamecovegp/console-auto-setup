# Always-Install GUI Checkbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "Always" checkbox per app row in the shared app-pick modal (both Save and Download pickers) that edits the global always-install set from the UI, with delta-merge persistence and APK auto-lock.

**Architecture:** A pure `merge_always_install` helper in `cas/profiles.py` computes the new global set from the modal's visible-row choices (preserving off-modal members). `cas/config.py`'s setter is refined so `None` clears and a list (incl. `[]`) is stored verbatim. `cas/gui.py`'s `_app_pick_modal` gains an `always_install` param, an "Always" checkbox per non-launcher row (which force-locks APK on), and a 3-tuple return; both callers unpack it and persist the merge on Run.

**Tech Stack:** Python 3 stdlib, `unittest` (`tests/test_cas.py`), Tkinter (GUI, not unit-tested here).

**Spec:** `docs/superpowers/specs/2026-07-02-always-install-gui-checkbox-design.md`
**Branch:** `feat/always-install-set` (worktree `scratchpad/wt-always-install`). Builds on the shipped config-backed always-install feature. Baseline at branch tip: combined suite (`tests/test_cas.py` + `tests/test_firmware.py`) = **244 green**; `tests/test_cas.py` alone = **213**.

## Global Constraints

- **APK axis only.** Ticking Always forces the row's APK on and locks it; unticking releases APK to its prior value. The Config axis is never touched. Spec §3.
- **Global + merge.** The always-install set is global. The modal shows only this device/profile's apps, so persistence MERGES: `new = (old − visible) ∪ (ticked ∩ visible)` — apps not shown are preserved. Spec §5.
- **Empty = disabled.** If the merge yields an empty set, it must be STORED as `[]` (disabled), never clear-to-default (which would resurrect the built-in defaults). Spec §5.
- **Setter semantics (refined).** `set_always_install_pkgs(None)` clears the override (→ default). A list/iterable — including `[]` — is stored verbatim, sorted+deduped. A bare string → single pkg. Spec §5.
- **Modal return** becomes the 3-tuple `(axes, flags, always_ticked)`; the `always_install=None` param default means no Always column (back-compat). `always_ticked` is the `set` of visible rows checked Always (empty set when the column wasn't shown). Spec §4.
- **Persist on Run, per modal.** Always is a standalone global preference, persisted when a modal is Run — deliberately independent of Download's multi-profile transactional manifest writes. Spec §6.
- **Testing:** pure `merge_always_install` + config-setter tests; the Tkinter wiring (checkbox, APK-lock, 3-tuple unpack, merge/persist call) is verified by inspection + suite green, consistent with the base feature. Spec §7.

---

### Task 1: `merge_always_install` pure helper

**Files:**
- Modify: `cas/profiles.py` — add after `initial_capture_selection` (ends ~line 583)
- Test: `tests/test_cas.py` — add near the capture-selection tests

**Interfaces:**
- Produces: `merge_always_install(old, visible, ticked) -> frozenset[str]`.

- [ ] **Step 1: Write the failing test**

```python
    def test_merge_always_install(self):
        from cas import profiles as P
        old = frozenset({"a", "b", "offscreen"})     # 'offscreen' is a member NOT shown in this modal
        visible = {"a", "b", "c"}
        ticked = {"b", "c"}                           # untick a, keep b, add c
        self.assertEqual(P.merge_always_install(old, visible, ticked),
                         frozenset({"b", "c", "offscreen"}))   # a removed, c added, offscreen preserved
        # unticking all visible members with no offscreen member -> empty (disable)
        self.assertEqual(P.merge_always_install({"a", "b"}, {"a", "b"}, set()), frozenset())
        # ticked outside visible is ignored
        self.assertEqual(P.merge_always_install(set(), {"a"}, {"a", "x"}), frozenset({"a"}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest "tests/test_cas.py" -k merge_always_install -v`
Expected: FAIL with `AttributeError: module 'cas.profiles' has no attribute 'merge_always_install'`.

- [ ] **Step 3: Write minimal implementation**

Add to `cas/profiles.py` after `initial_capture_selection`:

```python
def merge_always_install(old, visible, ticked):
    """Delta-merge the app-pick modal's Always choices into the global always-install set. `old` = current
    global set; `visible` = pkgs shown in this modal (its editable scope); `ticked` = the visible pkgs the
    operator marked Always. Members NOT visible in this modal are preserved untouched. Returns a frozenset."""
    old, visible, ticked = frozenset(old), frozenset(visible), frozenset(ticked)
    return (old - visible) | (ticked & visible)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest "tests/test_cas.py" -k merge_always_install -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(always-install): merge_always_install delta-merge helper"
```

---

### Task 2: Refine `set_always_install_pkgs` (None clears, list stored verbatim incl. [])

**Files:**
- Modify: `cas/config.py:72-85` (`set_always_install_pkgs` — current body wraps a bare string, then falsy-clears)
- Test: `tests/test_cas.py` — `TestConfig` class

**Interfaces:**
- Produces: `set_always_install_pkgs(pkgs)` where `pkgs is None` clears (→ default) and any list/iterable (incl. `[]`) is stored verbatim (`[]` disables). Return type unchanged (`frozenset`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cas.py` inside `TestConfig`:

```python
    def test_always_install_setter_none_clears_empty_disables(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            C.set_always_install_pkgs(["com.a", "com.b"])
            # empty list STORES [] -> disabled (getter returns empty, NOT the default set)
            self.assertEqual(C.set_always_install_pkgs([]), frozenset())
            self.assertEqual(C.load_config().get("always_install"), [])
            self.assertEqual(C.always_install_pkgs(), frozenset())
            # None CLEARS the override -> default set returns
            self.assertEqual(
                C.set_always_install_pkgs(None),
                frozenset({"com.valvesoftware.steamlink", "com.gamecove.gamecove_companion"}))
            self.assertNotIn("always_install", C.load_config())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest "tests/test_cas.py::TestConfig::test_always_install_setter_none_clears_empty_disables" -v`
Expected: FAIL — the current setter treats `[]` as falsy and pops the key, so `always_install_pkgs()` returns the DEFAULT set (assert `== frozenset()` fails) and `load_config().get("always_install")` is `None` not `[]`.

- [ ] **Step 3: Write minimal implementation**

Replace `set_always_install_pkgs` in `cas/config.py` with:

```python
def set_always_install_pkgs(pkgs):
    """Persist the always-install set. `pkgs is None` CLEARS the override (getter falls back to the default
    set). A list/iterable — INCLUDING an empty one — is stored verbatim (sorted, deduped): an empty list
    DISABLES the feature. A bare string is treated as a single pkg id. Returns always_install_pkgs()."""
    cfg = load_config()
    if pkgs is None:
        cfg.pop("always_install", None)
    else:
        if isinstance(pkgs, str):
            pkgs = [pkgs]
        cfg["always_install"] = sorted({str(p) for p in pkgs})
    save_config(cfg)
    return always_install_pkgs()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest "tests/test_cas.py::TestConfig" -v`
Expected: PASS — the new test plus the existing `test_always_install_default_override_and_clear` and `test_always_install_setter_wraps_bare_string` (both stay valid: they use `save_config({"always_install": []})`, `set_always_install_pkgs(None)`, and `set_always_install_pkgs("com.solo")`, none of which change behavior).

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "refactor(config): set_always_install_pkgs None-clears; list stored verbatim ([]=disabled)"
```

---

### Task 3: "Always" checkbox in `_app_pick_modal` + both callers persist the merge

**Files:**
- Modify: `cas/gui.py` — `_app_pick_modal` (signature ~1183, `_run` ~1210-1213, row loop ~1229-1250, return ~1259-1261); add a `_persist_always_install` helper method; `_pick_capture` (modal call ~1315, unpack ~1325); `_pick_downloads` (modal call ~1364, unpack ~1374)
- Test: none new (Tkinter modal not unit-tested here; behavior covered by Tasks 1-2 + the base feature). Verification = grep + full suite green + inspection.

**Interfaces:**
- Consumes: `P.merge_always_install` (Task 1); `config.always_install_pkgs()` / `config.set_always_install_pkgs()` (Task 2 / base). `config` and `P` (`profiles`) are already imported in `cas/gui.py`.
- Produces: `_app_pick_modal(..., always_install=None)` returning `(axes, flags, always_ticked)`; new method `self._persist_always_install(visible, ticked)`.

- [ ] **Step 1: Add the `always_install` param + normalize**

In `_app_pick_modal`'s signature (currently):
```python
    def _app_pick_modal(self, title, intro, prof, rows, launchers, flag_specs, labels=None,
                        flags_caption="— behavior —", cfg_disabled=None):
```
change to:
```python
    def _app_pick_modal(self, title, intro, prof, rows, launchers, flag_specs, labels=None,
                        flags_caption="— behavior —", cfg_disabled=None, always_install=None):
```
Then, right after the existing `cfg_disabled = cfg_disabled or set()` line, add:
```python
        ai = None if always_install is None else frozenset(always_install)  # None -> no Always column
        always_vars = {}
```

- [ ] **Step 2: Add the Always checkbox to each non-launcher row**

In the row loop, immediately after the existing `_tip(cfg_cb, cfg_tip).pack(side="left")` line, add:
```python
            if ai is not None and not is_launcher:
                # Always-install: a GLOBAL preference. When on, APK is forced on + locked (the app is,
                # by definition, always installed). Unticking releases APK back to normal.
                always_v = tk.BooleanVar(value=(pkg in ai))
                always_vars[pkg] = always_v

                def _lock(av=always_v, ap=apk_v, cb=apk_cb):
                    if av.get():
                        ap.set(True); cb.configure(state="disabled")
                    else:
                        cb.configure(state="normal")
                acb = ttk.Checkbutton(row, text="Always", variable=always_v, command=_lock)
                _lock()                                    # apply initial lock state for current members
                _tip(acb, f"Always install {pkg} on every unit (adds it to the global always-install "
                          "set; APK stays on).").pack(side="left")
```

- [ ] **Step 3: Return the Always selection from `_run` and the return statement**

In `_run` (currently):
```python
        def _run():
            result["axes"] = {p: (a.get(), c.get()) for p, (a, c) in pick_vars.items()}
            result["flags"] = {k: ("on" if v.get() else "off") for k, v in flag_vars.items()}
            win.destroy()
```
add the `always` line before `win.destroy()`:
```python
            result["always"] = {p for p, v in always_vars.items() if v.get()}
```
And change the final return (currently `return result["axes"], result["flags"]`) to:
```python
        return result["axes"], result["flags"], result.get("always", set())
```

- [ ] **Step 4: Add the `_persist_always_install` helper**

Add this method to the same class, directly above `_app_pick_modal`:
```python
    def _persist_always_install(self, visible, ticked):
        """Merge the app-pick modal's Always choices into the global always-install set and persist it
        (verbatim; an empty result DISABLES rather than resurrecting defaults). No-op when unchanged."""
        old = config.always_install_pkgs()
        new = P.merge_always_install(old, visible, ticked)
        if new != old:
            config.set_always_install_pkgs(sorted(new))
```

- [ ] **Step 5: Wire `_pick_capture` (pass the set, unpack 3, persist)**

In `_pick_capture`, the modal call currently ends:
```python
            prof, rows, set(), flag_specs=flag_specs,
            flags_caption="— behavior (saved with the golden; default on Download) —")
```
change to:
```python
            prof, rows, set(), flag_specs=flag_specs,
            flags_caption="— behavior (saved with the golden; default on Download) —",
            always_install=config.always_install_pkgs())
```
Then change the unpack `axes, modal_flags = res` to:
```python
        axes, modal_flags, always_ticked = res
        self._persist_always_install(set(rows), always_ticked)
```

- [ ] **Step 6: Wire `_pick_downloads` (pass the set, unpack 3, persist)**

In `_pick_downloads`, the modal call currently ends:
```python
                prof, rows, set(), flag_specs, labels=labels, cfg_disabled=cfg_disabled)
```
change to:
```python
                prof, rows, set(), flag_specs, labels=labels, cfg_disabled=cfg_disabled,
                always_install=config.always_install_pkgs())
```
Then change the unpack `axes, fl = res` to:
```python
            axes, fl, always_ticked = res
            self._persist_always_install(set(rows), always_ticked)
```

- [ ] **Step 7: Verify (grep + full suite)**

Run: `grep -n "always_install=config.always_install_pkgs()" cas/gui.py`
Expected: **4** hits — the two pre-existing selection-helper calls (`initial_capture_selection`, `download_rows`) plus the two new modal calls.

Run: `grep -n "_persist_always_install\|always_ticked = res\|return result\[.axes.\], result\[.flags.\], result.get" cas/gui.py`
Expected: the helper def + 2 call sites, 2 `always_ticked = res` unpacks, and the 3-tuple return.

Run: `python3 -m pytest tests/test_cas.py tests/test_firmware.py -q`
Expected: **246 passed** (244 baseline + Task 1 + Task 2), output pristine. (No new test this task.)

- [ ] **Step 8: Commit**

```bash
git add cas/gui.py
git commit -m "feat(gui): Always checkbox in app-pick modal (both pickers) + merge-persist"
```

---

## Self-Review

**1. Spec coverage:**
- §3 UI (Always checkbox, APK-lock, select-all untouched, tooltip) → Task 3 Steps 2-3. ✅ (select-all is left operating on APK/Config only because `_set_all` acts on `pick_vars`, which this change does not add Always vars to — no code change needed.)
- §4 modal 3-tuple return + both callers unpack → Task 3 Steps 3, 5, 6. ✅
- §5 pure merge helper → Task 1; refined setter → Task 2; verbatim/empty-disable persistence → Task 3 Step 4 (`sorted(new)` through the refined setter; empty → `[]` stored). ✅
- §6 callers + persist-on-Run → Task 3 Steps 5-6 (`_pick_downloads` persists per modal Run). ✅
- §7 testing → Tasks 1-2 unit tests; Task 3 grep + suite. ✅
- §2 non-goals (no launcher Always, Config untouched, global not per-profile) → Task 3 Step 2 gates on `not is_launcher` and only sets the APK var. ✅

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; test steps show real assertions; Task 3's "no new test" is explicitly justified with a concrete grep + suite verification.

**3. Type consistency:** `merge_always_install(old, visible, ticked) -> frozenset` used with `set(rows)` (visible) and `always_ticked` (set) in `_persist_always_install`; `config.set_always_install_pkgs(sorted(new))` passes a list (Task 2 accepts list/None). Modal returns `(axes, flags, always_ticked:set)`; both callers unpack three names. `ai` is `frozenset|None`; `always_vars: {pkg: BooleanVar}`. Consistent.

**Note:** `_lock` binds `always_v`/`apk_v`/`apk_cb` as default args so each row's closure captures its own widgets (avoiding the classic closure-in-loop capture bug); it references no loop-scoped names. The Always checkbox is only created for non-launcher rows, so unticking always restores APK to `state="normal"`.
