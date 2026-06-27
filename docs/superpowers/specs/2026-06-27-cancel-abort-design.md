# Cancel / Abort In-Flight Operations — Design

- **Date:** 2026-06-27
- **Status:** Approved (design); implementation pending
- **Scope:** Add a **Cancel** button that aborts a running Download / Save / Root / Lock by stopping the active subprocess. **Abort-only** (no undo of what already happened). Always cancelable, but the brief partition-write window prompts a brick-warning confirm. Cancel never bricks and never strands the unit in fastboot/EDL.

---

## 1. Background

CAS runs each long operation on a background daemon thread via `App._run_bg(fn, label)` (`cas/gui.py`): it sets `self.busy`, disables the action buttons, animates the progress bar, runs `fn()`, then `done()` reports. There is currently **no way to stop a running op** — the operator must wait for it to finish or fail (the screenshot showed a multi-minute Download failing after 3 push retries with no abort).

The actual work happens in subprocesses driven from `cas/adb.py`:
- **blocking:** `subprocess_runner` (subprocess.run) — getprop, su, `fastboot flash`, `fh_loader`, `adb install`.
- **streaming:** `subprocess_stream`, `pull_with_progress`, `push_stream`/`pull_stream` — the multi-GB `adb push`/`pull` and on-device `restore.sh`/`capture.sh` (the minutes-long phases).
- **wait loops:** `Adb.wait_boot`, `Fastboot.wait`, `Edl.find_port` — `time.sleep` polling loops.

## 2. Goals / Non-goals

**Goals**
1. A Cancel button that aborts the in-flight op within ~1 s by stopping the active child process.
2. Honor cancel at every layer: blocking runner, streaming helpers, and wait loops.
3. Never strand the unit — on cancel during fastboot/EDL, still reboot it back to the OS.
4. Brick-safety: pressing Cancel during the ~5–10 s partition write prompts a confirm first.

**Non-goals**
- No undo / reverse of completed work (a cancelled Download stays partially restored; a cancelled Save's partial PC profile is discarded). Operator re-runs.
- No cancel for the trivial short calls (getprop/su id) — not worth it; cancel targets the long phases + the flash.

## 3. Cancel signal

A single `threading.Event` per operation, owned by the GUI. **The UI thread only SETS the event; the background layers POLL it and self-kill their child** — so no cross-thread subprocess-handle juggling. Add to `cas/adb.py`:

```python
CANCELLED = 130     # rc returned by a cancelled child (128 + SIGINT); distinguishes cancel from real failure
def is_cancelled(rc): return rc == CANCELLED
```

## 4. `cas/adb.py` — cancellation at the subprocess layer

- **`subprocess_runner(args, input_text=None, timeout=900, cancel=None)`** — when `cancel` is given, run via `Popen` and loop `proc.wait(timeout=0.3)`; on `TimeoutExpired` check `cancel.is_set()` → `terminate()`, brief wait, `kill()`, return `(CANCELLED, partial_out, "cancelled")`. Also enforce the overall `timeout`. When `cancel is None` → unchanged (`subprocess.run`).
- **`subprocess_stream(args, on_line, input_text=None, cancel=None)`** and **`pull_with_progress(..., cancel=None)`** — in their read/poll loops, if `cancel.is_set()` → `kill()` the Popen, return `CANCELLED` / `False`.
- **`Adb` / `Fastboot` / `Edl`** gain `cancel=None` in `__init__` (stored as `self.cancel`):
  - streaming methods (`su_stream`, `pull_stream`, `pull_with_progress`, `push_stream`) pass `self.cancel` to the helpers.
  - runner calls pass cancel **only to the real runner** (compat with test runners whose signature is fixed):
    `kw = {"cancel": self.cancel} if self.runner is subprocess_runner else {}` then `self.runner(args, input_text=…, timeout=…, **kw)`.
  - wait loops (`Adb.wait_boot`, `Fastboot.wait`, `Edl.find_port`) check `self.cancel.is_set()` each iteration → return early (False/None).

## 5. `cas/provision.py` — op-level abort + flash-critical marker

- The batch workers (`root_all`/`seal_all` worker, and `restore`/`capture` step sequences) check `cancel.is_set()` between steps → return `("cancelled", "")` instead of proceeding. (`cancel` reaches them via the `adb`/`fastboot` objects they already receive — read `adb.cancel`.)
- **Never strand:** if cancel hits after `reboot bootloader`/`reboot edl`, still call `fastboot.reboot()` / `edl.reset()` to return the unit to the OS. `edl_flasher` already always resets; `fastboot_flasher` already reboots on its failure path — extend both to treat a cancelled flash the same.
- **Flash-critical marker:** `fastboot_flasher`/`edl_flasher` accept an optional `on_critical(bool)` callback; call `on_critical(True)` immediately before the partition write (`fastboot flash` / `fh_loader` Firehose write) and `on_critical(False)` immediately after. Default `on_critical=None` (no-op) keeps non-GUI callers unchanged.

## 6. `cas/gui.py` — the Cancel button

- `_run_bg` creates a fresh `self.cancel_event = threading.Event()` at start and clears the reference in `done()`.
- A **Cancel** button in the action row, `state=disabled` normally, `enabled` while `self.busy` (mirror how the action buttons toggle). Pressing it calls `_cancel_op()`.
- All `Adb`/`Fastboot`/`Edl` built for the op are constructed with `cancel=self.cancel_event` (the `make_adb`/`make_fb` factories in `root_all`/`seal_all`, and the direct constructions for Download/Save).
- `self._flash_critical` (bool) is set/cleared by the `on_critical` callback handed to the flashers (marshalled to the UI thread via `win.after`).
- **`_cancel_op()`**: if `self._flash_critical` → `messagebox.askyesno("CAS — cancel during flash?", "Interrupting a flash can BRICK the unit. Cancel anyway?")`; on no, return. Otherwise (or on yes) → `self.cancel_event.set()`, log `⏹ cancelling…`, and disable the Cancel button (so it can't be hit twice). The polling layers stop within ~1 s.
- `_report`/`done` recognize the cancelled status (`is_cancelled` / `("cancelled", …)`) and show `⏹ cancelled` rather than ❌ failed.

## 7. Device-left state (the "abort" choice)

Cancel only stops; it does not undo. After a cancel:
- **Download** — unit is partially restored → NOT sealed/shippable; re-run Download.
- **Save** — partial PC payload is discarded (the worker removes the half-written profile payload dir on cancel).
- **Root/Lock** — if cancelled before the write, nothing changed; if cancelled mid-write (after the confirm), the unit may be unbootable → recover by re-flashing its stock/patched init_boot (the firmware library has it). The confirm is what gates this.

## 8. Testing (`tests/`)

1. `subprocess_runner(cancel=set_event)` → child killed, returns `CANCELLED`; `cancel=None` path unchanged.
2. `subprocess_stream`/`pull_with_progress` with a pre-set cancel → returns cancelled without hanging.
3. `Adb.wait_boot`/`Fastboot.wait`/`Edl.find_port` with a set cancel → return early.
4. A worker (root_all) whose `adb.cancel` is set mid-run → returns `("cancelled", …)` and does not flash.
5. `_cancel_op` flash-critical branch → asks for confirm; non-critical → sets the event without confirm. (logic test, mocked messagebox.)
6. Full existing suite stays green (test runners unaffected — cancel only passed to the real `subprocess_runner`).

## 9. Code touch points

| File | Change |
|---|---|
| `cas/adb.py` | `CANCELLED`/`is_cancelled`; `cancel=` on `subprocess_runner`/`subprocess_stream`/`pull_with_progress`; `cancel=` on `Adb`/`Fastboot`/`Edl` + wait-loop checks |
| `cas/provision.py` | worker cancel checks; never-strand on cancel; `on_critical` callback in `fastboot_flasher`/`edl_flasher` (+ thread through `root`/`root_all`/`seal`/`seal_all`) |
| `cas/gui.py` | Cancel button; per-op `cancel_event`; build device objects with it; `_flash_critical` via `on_critical`; `_cancel_op` confirm logic; cancelled in report |
| `tests/test_cas.py` | cancellation tests (above) |
