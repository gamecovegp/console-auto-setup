# Sequential action-chain runner — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the footer's four one-shot action buttons with per-action checkboxes + a single **Run** that executes the ticked steps in fixed order, per device, devices in parallel, stopping a device's chain on a failed step.

**Architecture:** A pure `_resolve_chain(ticked)` turns the ticked checkboxes into an ordered, validated step list (Save⟂Download/Lock). `_run_chain(steps, serials, save_name)` runs the **unit** chain as **stage-by-stage over carried-forward survivors** — each stage calls the existing `PV.root_all`/`provision_all`/`seal_all` (already parallel across devices, returns `{serial:(status,…)}`); a device that fails a stage drops out of the next stage's set (= per-device stop-on-fail with a stage barrier). The **golden** chain (Save) is single-device: Root stage then `PV.capture_to_pc`.

**Tech Stack:** Python 3 / Tkinter (`cas/gui.py`), `unittest` with mocked `cas.provision` (`tests/test_cas.py`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-29-action-chain-runner-design.md`.
- Steps run in **fixed order**: `root, save, download, lock` (save and download/lock never coexist).
- **Mutual exclusivity:** Save ⟂ {Download, Lock}. Root valid in both chains. ≥1 step must be ticked.
- **Save is single-device:** if Save is ticked with >1 target (ALL or multiple rows), refuse before running — no partial run.
- **Per-device stop-on-fail:** a device that fails a stage is excluded from later stages; other devices continue. A device is "failed" when its `res[serial][0] in ("fail", "error")` (the existing contract in `_run_batch`).
- **One chain-run at a time** behind `self.busy` (reuse `_run_bg`); **Cancel** aborts the running chain (existing `self.cancel_event` / `_cancel_op`); checkboxes+Run disable while busy, Cancel stays live.
- Reuse existing helpers verbatim: `_action_targets`, `_selected_serial`, `_profile_map`, `_run_bg`, `self.cancel_event`, `self.adb_bin`, `self.fb_bin`, `self.profiles_root`, `APPDIR`, `config.es_media_src()`, `_stamp()`, the `Adb`/`Fastboot` constructors, and `PV.root_all`/`provision_all`/`seal_all`/`capture_to_pc`.
- Branch: current branch (`feat/companion-device-owner-lockdown`); commit per task.

---

### Task 1: `_resolve_chain` — ordered, validated step list (pure logic)

**Files:**
- Modify: `cas/gui.py` (add `_resolve_chain` method to the main window class, near `_action_targets`)
- Test: `tests/test_cas.py`

**Interfaces:**
- Produces: `_resolve_chain(ticked: dict[str, bool]) -> tuple[list[str], str | None]`. `ticked` keys are a subset of `{"root","save","download","lock"}`. Returns `(steps, None)` with `steps` in fixed order `["root","save","download","lock"]` filtered to ticked, OR `([], error_message)` when nothing ticked, or Save coexists with Download/Lock. Consumed by Task 2 (test) and Task 3 (GUI Run).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cas.py`:

```python
class TestResolveChain(unittest.TestCase):
    def _r(self, **t):
        from cas.gui import App                          # the main window class
        return App._resolve_chain(None, t)               # pure: no self state used

    def test_orders_unit_chain(self):
        self.assertEqual(self._r(lock=True, root=True, download=True), (["root", "download", "lock"], None))

    def test_golden_chain(self):
        self.assertEqual(self._r(root=True, save=True), (["root", "save"], None))

    def test_save_excludes_download_lock(self):
        steps, err = self._r(save=True, download=True)
        self.assertEqual(steps, [])
        self.assertIn("Save", err)

    def test_nothing_ticked_is_error(self):
        steps, err = self._r()
        self.assertEqual(steps, [])
        self.assertTrue(err)
```

(If the main window class is named other than `App`, use its actual name — grep `class .*(tk\\.|ttk\\.|object)` / the class that defines `_action_targets`.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestResolveChain -q`
Expected: FAIL — `AttributeError: … has no attribute '_resolve_chain'`.

- [ ] **Step 3: Implement**

Add this method to the main window class in `cas/gui.py` (next to `_action_targets`):

```python
    _CHAIN_ORDER = ("root", "save", "download", "lock")

    def _resolve_chain(self, ticked):
        """Turn the ticked action checkboxes into an ordered, validated step list.
        Returns (steps_in_fixed_order, error_or_None). Save is mutually exclusive with Download/Lock."""
        on = [k for k in self._CHAIN_ORDER if ticked.get(k)]
        if not on:
            return [], "Tick at least one action to run."
        if "save" in on and ("download" in on or "lock" in on):
            return [], "Save (golden capture) can't run with Download/Lock — they're opposite directions."
        return on, None
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestResolveChain -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py tests/test_cas.py
git commit -m "feat(gui): _resolve_chain — ordered, validated action-chain steps"
```

---

### Task 2: `_stage` + `_run_chain` — stage-by-stage runner

**Files:**
- Modify: `cas/gui.py` (add `_stage` and `_run_chain`; refactor `_run_batch`'s stage bodies into `_stage`)
- Test: `tests/test_cas.py`

**Interfaces:**
- Consumes: `_resolve_chain` (Task 1); `PV.root_all`/`provision_all`/`seal_all` (return `{serial:(status,…)}`); `PV.capture_to_pc`.
- Produces:
  - `_stage(step, serials, pm, force, cev) -> dict[str, tuple]` — runs ONE unit stage (`"root"`/`"download"`/`"lock"`) across `serials` via the matching `PV.*_all`, returns its result dict.
  - `_run_chain(steps, serials, save_name=None)` — runs the chain under `_run_bg`. Unit chain: survivors carried across stages. Golden chain (`"save"` in steps): single device, optional root stage then `capture_to_pc(serial, save_name)`. Consumed by Task 3.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cas.py` (mock `PV.*_all` so a chosen serial "fails" root and must be dropped from download):

```python
class TestRunChain(unittest.TestCase):
    def _app(self):
        from cas.gui import App
        app = App.__new__(App)                            # bypass Tk __init__
        app.adb_bin = app.fb_bin = None
        app.profiles_root = "."
        app.assigned = {"S1": "p", "S2": "p"}
        app.cancel_event = type("E", (), {"is_set": lambda self: False})()
        app._stage_calls = []
        def fake_stage(step, serials, pm, force, cev):
            app._stage_calls.append((step, list(serials)))
            # S1 fails 'root'; everything else ok
            return {s: (("fail" if (step == "root" and s == "S1") else "ok"),) for s in serials}
        app._stage = fake_stage
        return app

    def test_failed_root_drops_from_download(self):
        app = self._app()
        survivors = app._run_chain_core(["root", "download", "lock"], ["S1", "S2"], None)
        # stages run in order; download/lock only see S2 (S1 dropped after failing root)
        self.assertEqual(app._stage_calls,
                         [("root", ["S1", "S2"]), ("download", ["S2"]), ("lock", ["S2"])])
        self.assertEqual(survivors, ["S2"])
```

(`_run_chain_core(steps, serials, save_name)` is the pure survivor-folding loop extracted from `_run_chain`'s background `work()`, so it's testable without Tk/threads. `_run_chain` wraps it with the confirm + `_run_bg`.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestRunChain -q`
Expected: FAIL — no `_run_chain_core`/`_stage`.

- [ ] **Step 3: Implement**

Add to the main window class in `cas/gui.py`. First extract `_stage` (the per-stage bodies already in `_run_batch`'s `work()`):

```python
    def _stage(self, step, serials, pm, force, cev):
        """Run ONE unit stage across serials via the matching PV.*_all; return its {serial:(status,…)} dict."""
        devs = [(s, "device") for s in serials]
        mk_adb = lambda s: Adb(serial=s, adb=self.adb_bin, cancel=cev)
        mk_fb = lambda s: Fastboot(serial=s, fastboot=self.fb_bin, cancel=cev)
        if step == "download":
            return PV.provision_all(mk_adb, devs, root=self.profiles_root, log=self.log,
                                    profile_map=pm, es_media_src=config.es_media_src())
        if step == "root":
            return PV.root_all(mk_adb, mk_fb, devs, profiles_root=self.profiles_root, appdir=APPDIR,
                               log=self.log, profile_map=pm, force_serials=force,
                               on_critical=self._on_flash_critical)
        return PV.seal_all(mk_adb, mk_fb, devs, profiles_root=self.profiles_root, appdir=APPDIR,
                           log=self.log, profile_map=pm, force_serials=force,
                           on_critical=self._on_flash_critical)

    def _run_chain_core(self, steps, serials, save_name):
        """Pure chain loop (no Tk/threads): fold survivors across stages, return the final survivor list."""
        cev = self.cancel_event
        pm, force = self._profile_map(serials)
        survivors = list(serials)
        for step in steps:
            if cev.is_set():
                break
            if step == "save":
                s = survivors[0]
                ok = PV.capture_to_pc(Adb(serial=s, adb=self.adb_bin, cancel=cev), save_name, _stamp(),
                                      root=self.profiles_root, log=self.log)
                survivors = survivors if ok else []
            else:
                res = self._stage(step, survivors, pm, force, cev)
                survivors = [s for s in survivors if res.get(s, ("error",))[0] not in ("fail", "error")]
            self.log(f"chain: after {step} — {len(survivors)}/{len(serials)} still ok")
        return survivors

    def _run_chain(self, steps, serials, save_name=None):
        """Run the resolved chain on serials (one confirm, then background, per-stage survivor folding)."""
        if "save" in steps and len(serials) != 1:
            messagebox.showinfo("CAS", "Save captures ONE golden device. Select a single device (or untick Save).")
            return
        names = {"root": "Root", "save": "Save", "download": "Download", "lock": "Lock"}
        chain = " → ".join(names[s] for s in steps)
        if not messagebox.askyesno("CAS — Run", f"Run {chain} on {len(serials)} device(s)?\nThey run IN PARALLEL per stage."):
            return
        def work():
            survivors = self._run_chain_core(steps, serials, save_name)
            self.win.after(0, self.refresh_devices)
            self.win.after(0, self.refresh_profiles)
            return {s: ("done",) if s in survivors else ("fail",) for s in serials}
        self._run_bg(work, label=f"Running {chain} on {len(serials)} device(s)")
```

Then point `_run_batch`'s `work()` stage bodies at `_stage` to remove the duplication (replace the inline `if kind == "download": … elif "root": … else:` block with `res = self._stage(kind, serials, pm, force, cev)`), keeping its confirm + retry-ctx behavior for the single-action retry path. (If that refactor risks the existing `_run_batch` tests, leave `_run_batch` as-is — `_stage` is additive; note it for the reviewer.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestRunChain -q`
Expected: PASS. Then full `python3 -m pytest tests/test_cas.py -q` (existing `_run_batch`/seal/provision tests stay green).

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py tests/test_cas.py
git commit -m "feat(gui): _run_chain — stage-by-stage chain over survivors (per-device stop-on-fail)"
```

---

### Task 3: Footer UI — checkboxes + Run + mutual exclusivity

**Files:**
- Modify: `cas/gui.py` (footer `row2`, ~lines 674-706: replace the four buttons with four checkboxes + Run; add `_on_chain_tick`, `run_chain`)

**Interfaces:**
- Consumes: `_resolve_chain` (Task 1), `_run_chain` (Task 2), `_action_targets`, `_selected_serial`.

- [ ] **Step 1: Replace the four action buttons with checkboxes + Run**

In `cas/gui.py`, replace the `for text, cmd, tip in (…): … self.btns.append(b)` button loop (the four-button block in the footer) with:

```python
        self.chain_vars = {}                              # action key -> BooleanVar
        self.chain_cbs = {}                               # action key -> the Checkbutton (for enable/disable)
        for key, label, tip in (
            ("root", "⓪ Root", "Root the target(s): flash the profile's Magisk-patched init_boot + install Magisk from the PC."),
            ("save", "① Save → profile", "Capture ONE selected device into a profile (golden). Mutually exclusive with Download/Lock."),
            ("download", "② Download", "Install each device's assigned profile (apps + saves/BIOS/settings/grants/homescreen)."),
            ("lock", "③ Lock", "Retail-seal verified unit(s): hide Dev options, un-root, disable USB debugging."),
        ):
            v = tk.BooleanVar(value=False)
            self.chain_vars[key] = v
            cb = ttk.Checkbutton(row2, text=label, variable=v, command=self._on_chain_tick)
            cb.pack(side="left", padx=4, pady=4)
            _tip(cb, tip)
            self.chain_cbs[key] = cb
        self.run_btn = ttk.Button(row2, text="▶ Run", command=self.run_chain)
        self.run_btn.pack(side="left", padx=8, pady=4)
        _tip(self.run_btn, "Run the ticked actions in order (Root → Download → Lock, or Root → Save), per device, in parallel.")
        self.btns = list(self.chain_cbs.values()) + [self.run_btn]   # disabled together while busy
```

(Keep the Cancel button block immediately after, unchanged.)

- [ ] **Step 2: Mutual-exclusivity handler**

Add these methods near `_on_batch_toggle`:

```python
    def _on_chain_tick(self):
        """Save ⟂ Download/Lock: when Save is on, disable+clear Download/Lock; when either of those is on,
        disable+clear Save. Root stays available in both chains."""
        save_on = self.chain_vars["save"].get()
        unit_on = self.chain_vars["download"].get() or self.chain_vars["lock"].get()
        for k in ("download", "lock"):
            self.chain_cbs[k].configure(state="disabled" if save_on else "normal")
            if save_on:
                self.chain_vars[k].set(False)
        self.chain_cbs["save"].configure(state="disabled" if unit_on else "normal")
        if unit_on:
            self.chain_vars["save"].set(False)

    def run_chain(self):
        steps, err = self._resolve_chain({k: v.get() for k, v in self.chain_vars.items()})
        if err:
            messagebox.showinfo("CAS", err)
            return
        if "save" in steps:
            serial = self._selected_serial()
            if not serial:
                messagebox.showinfo("CAS", "Select ONE golden device for Save.")
                return
            name = simpledialog.askstring("Save → profile", "Profile name to capture into:",
                                          initialvalue=self.prof_var.get())
            if not name:
                return
            self._run_chain(steps, [serial], save_name=name)
        else:
            t = self._action_targets()
            if t:
                self._run_chain(steps, t)
```

(Delete the now-unused `provision_selected`/`root_device`/`seal_device` button handlers if nothing else references them — grep first; `capture_update` may be reused or removed. If any is still bound elsewhere, KEEP it.)

- [ ] **Step 3: Verify dispatch wiring (headless) + suite green**

Run:
```bash
cd "$(git rev-parse --show-toplevel)"
python3 -m py_compile cas/gui.py && echo "gui.py OK"
python3 -m pytest tests/test_cas.py -q
grep -nE 'self\.chain_vars\[|self\.run_btn|_on_chain_tick|def run_chain' cas/gui.py | head
```
Expected: compiles; suite green; greps show the new wiring. `[VERIFY in app]`: launch CAS — the footer shows four checkboxes + ▶ Run; ticking Save greys Download/Lock and vice-versa; ticking Root+Download+Lock and Run chains them per device.

- [ ] **Step 4: Commit**

```bash
git add cas/gui.py
git commit -m "feat(gui): footer checkboxes + Run — sequential action chain with Save⟂Download/Lock"
```

---

## Self-Review

**Spec coverage** (against `2026-06-29-action-chain-runner-design.md`):
- §3 UI: four checkboxes + Run, mutual exclusivity, Cancel/ALL toggle kept → Task 3. ✓
- §4 dispatch: `_run_chain` stage-by-stage survivors; golden single-device Root→Save; busy guard → Task 2. ✓
- §5 error handling: per-device stop-on-fail (survivor fold), Save+multi refused, Cancel via cancel_event → Tasks 2-3. ✓
- §6 reporting: per-stage survivor log line + refresh → Task 2. ✓ (per-device per-step detail comes from the existing `PV.*_all` logging.)
- §7 testing: `_resolve_chain` (Task 1), `_run_chain_core` ordering/stop-on-fail (Task 2), `[VERIFY in app]` for Tk (Task 3). ✓

**Placeholder scan:** every code step has real code + exact commands. The one conditional ("if the refactor risks existing tests, leave `_run_batch` as-is") is a guarded fallback, not a placeholder — `_stage` is additive either way.

**Type/name consistency:** `_resolve_chain(ticked)->(steps,err)`, `_stage(step,serials,pm,force,cev)->res`, `_run_chain_core(steps,serials,save_name)->survivors`, `_run_chain(steps,serials,save_name=None)`, `chain_vars`/`chain_cbs`/`run_btn` are used identically across Tasks 1-3. ✓
