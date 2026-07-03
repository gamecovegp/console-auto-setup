# Library-drive auto-detect

**Date:** 2026-07-03
**Status:** design (approved defaults, pending user review of spec)

## Problem

The GUI's "Library … ✗ not reachable" status (profile library and firmware library)
is only recomputed on an explicit **Refresh** (`refresh_devices` / Ctrl+R) or on the
handful of operations that call the refresh methods. If the operator opens CAS with the
external golden drive unplugged, then plugs it in, nothing re-checks on its own — the
Profile dropdown stays empty and both labels stay "not reachable" until a manual refresh.
Worse, clicking **Refresh devices** alone does not repopulate the Profile dropdown
(that list is owned by `refresh_profiles`, which the button does not call), so the operator
has no single obvious action to recover the UI.

## Goal

When the configured library drive (re)appears while the window is idle, CAS should
detect it and self-heal the UI automatically — on **both Windows and Linux** with one
code path. When the drive is removed, the status labels should become honest again
without destroying in-progress selection state.

## Why it can be universal

The reachability test is already a plain filesystem stat:

```python
def _lib_reachable(self):            # cas/gui.py
    return P.pathlib.Path(self.profiles_root).is_dir()
```

`Path.is_dir()` behaves identically on Windows and Linux, so a low-frequency poll of it
needs **no** OS-specific device-notification code (no Linux `udev`, no Windows
`WM_DEVICECHANGE`/WMI). A stat of a mount root is cheap even on a slow external drive
(it does not read the directory), so polling on the Tk main thread is fine.

Instant hardware-event detection is an explicit **non-goal**: it would require separate
Windows/Linux code for a sub-2-second latency improvement that no operator will notice.

## Design

### 1. Pure edge function (module level, `cas/gui.py`)

Unit-testable without Tk, mirroring the existing `_profile_library_label` /
`_app_label` module-level helpers:

```python
def _lib_watch_action(was, now, busy):
    """Edge decision for the library-drive watcher.
    Returns 'reconnect' | 'disconnect' | 'defer' | None."""
    if now == was:
        return None
    if now:                                  # unreachable -> reachable
        return "defer" if busy else "reconnect"
    return "disconnect"                       # reachable -> unreachable
```

- `defer` = the drive came back while a job is running; the caller must **not** advance
  the stored baseline, so the edge is re-evaluated on the next idle tick.

### 2. `App._lib_watch(self)`

Idle poller, rescheduled every **2000 ms** via `self.win.after`. Reschedules itself
unconditionally in a `finally`, and is guarded so a callback that fires after the window
is destroyed (e.g. just after Quit) does not raise `TclError`.

```python
def _lib_watch(self):
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
            self._update_lib_label()      # profile-library status line
            self.refresh_firmware()       # firmware-library status line, honestly
        # action in (None, "defer"): leave baseline untouched
    except tk.TclError:
        return                            # window gone — stop rescheduling
    self.win.after(2000, self._lib_watch)
```

Behaviour per edge:

| Edge | Guard | Effect |
|------|-------|--------|
| unreachable → reachable | idle | Full self-heal: `refresh_profiles` + `refresh_firmware` + `refresh_devices` + labels. Repopulates the empty Profile dropdown and firmware column. |
| unreachable → reachable | busy | Deferred — baseline stays `False`; retried next idle tick. Never refreshes mid-flash. |
| reachable → unreachable | any | Relabel only: `_update_lib_label` + `refresh_firmware`. **Does not** call `refresh_profiles`, so a transient USB drop does not wipe the operator's Profile selection/list. |
| no change | any | Nothing. |

### 3. Startup & wiring

- In `App.__init__`, after the existing initial `refresh_profiles()` / `refresh_devices()`
  (around `gui.py:184-186`): seed the baseline and start the loop:
  ```python
  self._lib_last_reachable = self._lib_reachable()
  self._lib_watch()
  ```
- In `App.choose_library()` (the "Library folder…" menu action that changes
  `self.profiles_root`): reset `self._lib_last_reachable = self._lib_reachable()` after
  the path change, so switching the library path does not emit a spurious
  "detected/removed" log on the next tick.

### Independent of the busy `_tick`

`_lib_watch` is separate from the existing `_tick` (which only runs while a job is
`busy`). They do not interact; `_lib_watch` runs continuously while the window lives.

## Configuration

**Always on — no toggle.** The poll is a single cheap stat every 2 s, skipped while a
job runs. There is no cost or risk that would justify a setting, so per YAGNI it is not
made configurable.

## Non-goals

- Instant OS device-event detection (udev / WMI / `WM_DEVICECHANGE`).
- Auto-detecting a **separately-configured** firmware drive (`firmware_dir`) that is not
  under the library root. The default `library_root/_firmware` layout is covered because
  the reconnect path calls `refresh_firmware`; a distinct firmware drive is out of scope.
- Auto-refreshing while a job is in progress (explicitly deferred).

## Testing

Follows the suite's existing patterns (`tests/test_cas.py`):

1. **Edge table** — call `_lib_watch_action(was, now, busy)` for every combination of
   `was ∈ {True, False}`, `now ∈ {True, False}`, `busy ∈ {True, False}` and assert the
   returned action (`None` / `reconnect` / `disconnect` / `defer`).
2. **Wiring** — build `app = App.__new__(App)`, set `_lib_last_reachable`, `busy`, and
   fakes for `_lib_reachable`, `refresh_profiles`, `refresh_firmware`, `refresh_devices`,
   `_update_lib_label`, `log`, and `win.after` (recording calls). Drive `_lib_watch` and
   assert:
   - reconnect edge (idle) → the three `refresh_*` fire and baseline becomes `True`;
   - reconnect edge (busy) → no `refresh_*`, baseline stays `False` (deferred);
   - disconnect edge → only relabel calls fire, `refresh_profiles` does **not**, baseline
     becomes `False`;
   - no-change → nothing fires;
   - `win.after(2000, …)` is always re-scheduled.

## Files touched

- `cas/gui.py` — new `_lib_watch_action` (module level); new `App._lib_watch`; two lines
  in `__init__`; one line in `choose_library`.
- `tests/test_cas.py` — edge-table + wiring tests.
