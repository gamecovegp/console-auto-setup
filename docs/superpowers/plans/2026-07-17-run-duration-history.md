# Run Duration in History — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record and display how long each `root` / `lock` / `warmup` run takes, so a bench day can be planned from real numbers.

**Architecture:** Mirror the pattern Download already uses (`cas/provision.py:846-848`): the caller measures with `time.monotonic()` and passes `elapsed` to the logger. `log_run()` gains an optional trailing param and writes `total_secs`; `_fmt_run()` renders it. No new abstractions, no new modules.

**Tech Stack:** Python 3, `unittest` (pytest is only the runner), no new dependencies.

## Global Constraints

- **A concurrent session is working in this same checkout.** `cas/provision.py` must receive a MINIMAL diff — touch only the exact lines specified. Do not reformat, refactor, or tidy anything nearby.
- **`_img_kernel_size()` in `cas/provision.py` is ABSOLUTELY OFF LIMITS.** It is the guard that refuses a kernel-less image being flashed to a `boot` partition — the check standing between an operator and a bricked device. Do not touch, move, or reference it.
- **Do NOT touch `cas/gui.py`, `cas/profiles.py`, or `tests/test_cas.py`** — the concurrent session owns those.
- `total_secs` is **BATCH wall-clock**, deliberately. `root`/`lock`/`warmup` fan out across devices in PARALLEL by design (root is reboot-dominated). Four units rooting together for ten minutes logs `600`, not `2400`. Do not "fix" this into a per-unit figure — that is Phase 2 and needs its own design.
- The key is **OMITTED entirely** when `elapsed is None` — never written as `null`. Absence means "not recorded", which is what old records mean.
- Backward compatibility is free and must stay free: `_secs(None)` already returns `"—s"`, so pre-existing history records render without migration. No migration step exists or is wanted.
- Field name is `total_secs`, matching `download` (also a batch action). `save`'s per-device field stays `secs`. That inconsistency predates this work; do not "harmonize" it.
- Tests are `unittest.TestCase`. `log_run`'s existing tests live in **`tests/test_ui.py`** (class at ~line 1114), NOT `tests/test_cas.py`.
- `import time` already exists at module level in `cas/provision.py` (used at `:846` and `:1309`). Do not add a duplicate import.

---

### Task 1: `log_run()` records `total_secs`

**Files:**
- Modify: `cas/provision.py:1151` (`log_run`)
- Test: `tests/test_ui.py` (the existing `log_run` test class at ~line 1114)

**Interfaces:**
- Consumes: nothing.
- Produces: `log_run(root, action, results, log=print, elapsed=None)`. When `elapsed` is not None the written record gains `"total_secs": round(elapsed, 1)`. When None the key is absent. Task 2 renders it; Task 3 passes it. All existing positional callers keep working — the param is optional and TRAILING.

- [ ] **Step 1: Write the failing tests**

Add to the existing `log_run` test class in `tests/test_ui.py` (the one containing `test_log_run_writes_status_and_error_only_on_failure`). Match its existing style for reading back the written jsonl:

```python
    def test_log_run_records_total_secs_when_elapsed_given(self):
        with tempfile.TemporaryDirectory() as td:
            PV.log_run(td, "root", {"S1": ("ok", "p")}, log=lambda m: None, elapsed=612.34)
            rec = self._read_run_record(td, "root")
            self.assertEqual(rec["total_secs"], 612.3)          # rounded to 1dp

    def test_log_run_omits_total_secs_when_elapsed_is_none(self):
        # ABSENT, not null — absence is what every pre-existing record means.
        with tempfile.TemporaryDirectory() as td:
            PV.log_run(td, "root", {"S1": ("ok", "p")}, log=lambda m: None)
            rec = self._read_run_record(td, "root")
            self.assertNotIn("total_secs", rec)

    def test_log_run_elapsed_is_optional_and_trailing(self):
        # Every existing caller passes (root, action, results, log) positionally and must keep working.
        with tempfile.TemporaryDirectory() as td:
            PV.log_run(td, "lock", {"S1": ("ok", "p")}, lambda m: None)
            self.assertNotIn("total_secs", self._read_run_record(td, "lock"))

    def test_log_run_records_zero_elapsed(self):
        # 0.0 is a real measurement, not "unknown" — `if elapsed is not None`, never `if elapsed`.
        with tempfile.TemporaryDirectory() as td:
            PV.log_run(td, "warmup", {"S1": ("ok", "p")}, log=lambda m: None, elapsed=0.0)
            self.assertEqual(self._read_run_record(td, "warmup")["total_secs"], 0.0)
```

Add this helper to the same class (read the file's existing history-reading code first and match how it locates the per-machine jsonl — `_append_history` writes `<action>-history.<machine>.jsonl`):

```python
    def _read_run_record(self, td, action):
        """The single record log_run just wrote to <action>-history.<machine>.jsonl under td."""
        import glob
        hits = glob.glob(os.path.join(td, "**", f"{action}-history*.jsonl"), recursive=True)
        self.assertTrue(hits, f"no {action}-history jsonl written under {td}")
        return json.loads(pathlib.Path(hits[0]).read_text().strip().splitlines()[-1])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ui.py -q -k log_run`
Expected: FAIL — `test_log_run_records_total_secs_when_elapsed_given` errors with `TypeError: log_run() got an unexpected keyword argument 'elapsed'`.

- [ ] **Step 3: Write minimal implementation**

In `cas/provision.py`, change `log_run`'s signature and add the field. Change ONLY the `def` line, the docstring's final paragraph, and add the two-line `if` before `_append_history`:

```python
def log_run(root, action, results, log=print, elapsed=None):
    """Append ONE per-run record to <action>-history.<machine>.jsonl (action ∈ root/lock/warmup): which
    devices passed, and — the point of this — the ERROR REASON for each that failed. A successful device
    carries only its 'ok' status (no noise); a failed one carries the last line it logged before bailing.
    Best-effort via _append_history (a write failure only warns). `results` is {serial:(status, detail)}.
    Download + Save keep their own byte-carrying history; this covers the actions that had none.

    `elapsed` (seconds) is the action's BATCH WALL-CLOCK — these actions fan out across devices in
    PARALLEL, so four units rooting together for ten minutes is 600, not 2400. That is the number that
    predicts a bench day. Omitted entirely when None (never written as null): absence means 'not
    recorded', which is exactly what every record written before this field existed means."""
```

Then, immediately before the `_append_history(...)` call:

```python
    rec = {"when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "action": action, "ok": ok, "failed": failed, "devices": devs}
    if elapsed is not None:                                  # 0.0 is a real measurement, not "unknown"
        rec["total_secs"] = round(elapsed, 1)
    _append_history(root, f"{action}-history", rec, log,
                    summary=f"{action} run logged: {ok} ok, {failed} failed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ui.py -q`
Expected: PASS — including the pre-existing `log_run` tests, unmodified.

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_ui.py
git commit -m "feat(history): log_run records total_secs (batch wall-clock)"
```

---

### Task 2: `_fmt_run()` renders the duration

**Files:**
- Modify: `cas/history.py` (`_fmt_run`, ~line 49)
- Test: `tests/test_ui.py` (wherever history rendering/formatting is tested — grep for `_fmt_run`, `render(`, or the download history fixture at ~line 1022)

**Interfaces:**
- Consumes: the `total_secs` key Task 1 writes.
- Produces: `_fmt_run(r)` returns `"{ok} ok · {failed} failed · {secs}  ·  {who}"`. Task 3 does not depend on this.

> **Placement matters and is the point of this task.** The duration goes BEFORE the device list, not after. The device list is unbounded — one entry per unit on the bench, each carrying a failure reason — so a duration appended after it gets pushed off the end of the line. `_fmt_download` already places it before; mirroring Download means mirroring the layout.

- [ ] **Step 1: Write the failing tests**

```python
class TestFmtRunDuration(unittest.TestCase):
    """_fmt_run renders BATCH wall-clock before the (unbounded) device list, like _fmt_download."""

    def test_renders_duration(self):
        out = H._fmt_run({"ok": 3, "failed": 0, "total_secs": 612,
                          "devices": [{"serial": "S1", "status": "ok", "profile": "p"}]})
        self.assertIn("612s", out)

    def test_duration_precedes_the_device_list(self):
        out = H._fmt_run({"ok": 1, "failed": 0, "total_secs": 612,
                          "devices": [{"serial": "S1", "status": "ok", "profile": "p"}]})
        self.assertLess(out.index("612s"), out.index("S1"),
                        f"duration must precede the unbounded device list: {out!r}")

    def test_old_record_without_total_secs_degrades_to_dash(self):
        # Every record written before this field existed. Must render, must not raise.
        out = H._fmt_run({"ok": 1, "failed": 0,
                          "devices": [{"serial": "S1", "status": "ok", "profile": "p"}]})
        self.assertIn("—s", out)
        self.assertIn("S1", out)

    def test_still_shows_counts_and_failure_reason(self):
        out = H._fmt_run({"ok": 0, "failed": 1, "total_secs": 5,
                          "devices": [{"serial": "S1", "status": "fail", "error": "boom"}]})
        self.assertIn("0 ok", out)
        self.assertIn("1 failed", out)
        self.assertIn("boom", out)
```

Import `cas.history as H` if the test file does not already; grep first — it may already be imported under another alias.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ui.py -q -k FmtRunDuration`
Expected: FAIL — `test_renders_duration` fails because `612s` is not in the output.

- [ ] **Step 3: Write minimal implementation**

In `cas/history.py`, replace `_fmt_run`:

```python
def _fmt_run(r):
    """root / lock / warmup run: pass/fail counts, BATCH wall-clock, then each device (with the error
    reason on a failure). The duration goes BEFORE the device list — like _fmt_download — because the
    device list is unbounded (one entry per bench unit, each carrying a failure reason) and would push
    the duration off the end of the line. total_secs is absent on records written before it existed;
    _secs() renders those '—s'."""
    who = " | ".join(_dev_line(d) for d in (r.get("devices") or [])) or "—"
    return (f"{r.get('ok', 0)} ok · {r.get('failed', 0)} failed · "
            f"{_secs(r.get('total_secs'))}  ·  {who}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ui.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/history.py tests/test_ui.py
git commit -m "feat(history): _fmt_run renders duration before the device list"
```

---

### Task 3: the three callers actually measure

**Files:**
- Modify: `cas/provision.py` — `warmup_all` (call site at `:1127`), `root_all` (call site at `:1846`), `seal_all` (call site at `:1949`)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `log_run(root, action, results, log=print, elapsed=None)` from Task 1.
- Produces: nothing later tasks depend on. This is the last task.

> **This task is the one that can silently do nothing.** A caller that stops timing still passes every Task-1 and Task-2 test — `log_run` works, `_fmt_run` works, and the record just quietly says `—s` forever. So each call site must be pinned individually.

- [ ] **Step 1: Write the failing test**

```python
class TestRunCallersMeasureElapsed(unittest.TestCase):
    """Each *_all action must pass a measured elapsed to log_run. A caller that silently stops timing
    breaks nothing and fails nothing — it just logs '—s' forever. So pin every call site."""

    def _capture_elapsed(self, fn_name, *args, **kw):
        """Call PV.<fn_name> with log_run stubbed; return the `elapsed` it was handed."""
        seen = {}

        def fake_log_run(root, action, results, log=print, elapsed=None):
            seen["action"] = action
            seen["elapsed"] = elapsed
        with mock.patch.object(PV, "log_run", fake_log_run), \
             mock.patch.object(PV, "_each_device", lambda devices, worker, parallel: {"S1": ("ok", "p")}):
            getattr(PV, fn_name)(*args, **kw)
        return seen

    def test_root_all_passes_measured_elapsed(self):
        seen = self._capture_elapsed("root_all", lambda s: None, lambda s: None, ["S1"],
                                     profiles_root="r", log=lambda m: None)
        self.assertEqual(seen["action"], "root")
        self.assertIsNotNone(seen["elapsed"], "root_all passed no elapsed — it stopped timing")
        self.assertGreaterEqual(seen["elapsed"], 0.0)

    def test_seal_all_passes_measured_elapsed(self):
        seen = self._capture_elapsed("seal_all", lambda s: None, lambda s: None, ["S1"],
                                     profiles_root="r", log=lambda m: None)
        self.assertEqual(seen["action"], "lock")
        self.assertIsNotNone(seen["elapsed"], "seal_all passed no elapsed — it stopped timing")
        self.assertGreaterEqual(seen["elapsed"], 0.0)

    def test_warmup_all_passes_measured_elapsed(self):
        seen = self._capture_elapsed("warmup_all", lambda s: None, ["S1"],
                                     root="r", log=lambda m: None)
        self.assertEqual(seen["action"], "warmup")
        self.assertIsNotNone(seen["elapsed"], "warmup_all passed no elapsed — it stopped timing")
        self.assertGreaterEqual(seen["elapsed"], 0.0)
```

**Read each function's real signature before writing these calls** — `root_all`, `seal_all`, and `warmup_all` do not take identical arguments, and the stub args above are indicative. Patching `_each_device` is what keeps these unit tests from touching hardware; adjust the lambda's parameters to the real `_each_device` signature. If a function needs more mocking to reach its `log_run` call, add it — but do NOT change production code to make a test easier.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ui.py -q -k RunCallersMeasureElapsed`
Expected: FAIL — all three fail with `AssertionError: ... passed no elapsed — it stopped timing` (`elapsed` is None).

- [ ] **Step 3: Write minimal implementation**

For EACH of `root_all`, `seal_all`, `warmup_all`: add `t0 = time.monotonic()` as the **first statement after the function's docstring** (so the span covers the whole action, not just the fan-out — each does real work around it), and pass the delta at its `log_run` call.

`warmup_all` (call site `:1127`):
```python
    results = _each_device(devices, worker, parallel)
    log_run(root, "warmup", results, log, elapsed=time.monotonic() - t0)   # + BATCH wall-clock
```

`root_all` (call site `:1846`):
```python
    results = _each_device(devices, worker, parallel)
    log_run(profiles_root, "root", results, log, elapsed=time.monotonic() - t0)   # + BATCH wall-clock
```

`seal_all` (call site `:1949`):
```python
    results = _each_device(devices, worker, parallel)
    log_run(profiles_root, "lock", results, log, elapsed=time.monotonic() - t0)   # + BATCH wall-clock
```

`import time` already exists at module level (used at `:846` and `:1309`) — do not add another.

That is six changed lines total across the three functions. Keep the diff to exactly those. Do not touch `_img_kernel_size`.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_ui.py -q`
Expected: PASS

Then run the provision suite once, since `provision.py` changed:
Run: `python3 -m pytest tests/test_ui.py tests/test_firmware.py -q`
Expected: PASS. If `tests/test_cas.py` shows failures when you check it, do NOT fix them — the concurrent session owns that file. Report what you see.

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_ui.py
git commit -m "feat(history): root/lock/warmup measure and log their wall-clock"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| `log_run(..., elapsed=None)` writes `total_secs` | 1 |
| Key OMITTED (not null) when None | 1 |
| `_fmt_run` renders it | 2 |
| Rendered BEFORE the device list, like `_fmt_download` | 2 |
| Three callers measure, spanning the whole action | 3 |
| Batch wall-clock semantics (not per-unit) | Global Constraints + Task 1 docstring |
| Backward compat: old records render `—s`, no migration | 2 (`test_old_record_without_total_secs_degrades_to_dash`) |
| Field named `total_secs`, matching download | Global Constraints |
| Existing `log_run` tests pass unmodified | 1 (Step 4) |
| Per-call-site pinning (a caller that stops timing) | 3 |
| `_img_kernel_size` untouched | Global Constraints |
| Phase 2 (per-unit rollup) NOT built | Not in this plan — deliberate |

**Type consistency:** `log_run(root, action, results, log=print, elapsed=None)` is used identically in Tasks 1 and 3. `total_secs` is the field name in Tasks 1, 2, and 3. `_fmt_run(r)` keeps its single-arg signature.

**Placeholder scan:** none — every code step carries runnable code. Task 3's stub arguments are explicitly flagged as indicative with an instruction to read the real signatures, which is a direction to verify, not a placeholder to invent.

## Known risk

`cas/provision.py` is contended — a concurrent session commits to it. Tasks 1 and 3 both touch it. Keep both diffs minimal and expect to rebase. If `provision.py` is dirty with another session's work when a task starts, STOP and report rather than committing around it.
