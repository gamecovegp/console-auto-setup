# Disconnect / failure recovery guidance — design

**Date:** 2026-07-16
**Status:** approved, ready for implementation plan

## Problem

When a device disconnects or something unexpected happens *during* an operation
(Root / Save / Download / Warm up / Lock), CAS reports that the device failed but
rarely tells the operator **what state the unit is now in** or **what to do next**.
A unit left in EDL/9008 shows a black screen; one left in fastboot sits in the
bootloader; one that dropped mid-push is simply "offline". The operator has to
know, per-mode, that EDL wants a ~12 s power-hold, fastboot wants `fastboot reboot`,
etc.

Some of this guidance already exists but is **scattered and ad-hoc**:
`fastboot_missing_help()` (hold Power ~10 s), the EDL flasher's "hold power ~12 s if
the screen stays black". There is no systematic, per-operation "device state → next
steps" surfaced consistently across every operation.

**Goal:** one systematic recovery layer that, on any failure/disconnect, names the
device's likely mode and gives ordered, OS-aware recovery steps — surfaced in the
live log, on the device row, and in one end-of-run summary.

## Non-goals

- Automatic recovery (auto-rebooting the unit out of EDL etc.). Guidance only; the
  existing EDL auto-reset retry stays as-is.
- Changing how operations detect success/failure. This layer only *explains* a
  failure that the existing code already decided.
- A new persistent UI panel. Reuses the existing log, device tree, and dialog.

## Architecture

New pure module **`cas/recovery.py`** — no device I/O baked into the advice logic,
so the whole catalog is unit-testable off-device.

### `DeviceMode` (enum)

| Value | Meaning |
|-------|---------|
| `BOOTED_ADB` | reachable in adb, booted |
| `ADB_OFFLINE` | serial present but offline/unauthorized, or mid-reboot |
| `FASTBOOT` | in bootloader fastboot |
| `FASTBOOTD` | in userspace fastbootd |
| `EDL_9008` | Qualcomm EDL / 9008 (black screen) |
| `ABSENT` | not visible in adb, fastboot, or EDL |
| `SEALED_OK` | Lock finished and adb vanished **by design** — not a failure |

### `probe_mode(adb, fb, serial) -> DeviceMode`

Best-effort, short-timeout, each probe wrapped in try/except so a probe error never
masks the original failure. Order: `adb get-state`/boot check → `fastboot devices`
→ EDL port scan (reuse the existing detectors in `adb.py`). This is the
"confirm by probe" half of state detection.

### `advise(operation, phase, mode) -> Recovery`

The "infer from the failed step" half. `operation ∈ {root, save, download, warmup,
lock}`. `phase` is a coarse hint the worker knows cheaply — notably `edl_flash` vs
`fastboot_flash`, and `wait_boot` — used as the tiebreaker when the probe is
inconclusive (e.g. `mode=ABSENT` + `phase=edl_flash` → advise EDL recovery, because
a unit that vanished during an EDL write is almost certainly sitting dark in 9008).

`Recovery` is a small dataclass:

```
Recovery(
  state_label: str,      # e.g. "EDL / 9008 (black screen)"
  steps: list[str],      # ordered, OS-aware recovery actions
  operation: str,        # for the retry verb
  needs_attention: bool, # False for SEALED_OK
)
```

with three renderers: `log_block()` (multi-line, for the live log), `row_hint()`
(one line: `state_label — first action`, for the device row), `popup_line(serial)`
(one line for the end-of-run summary).

Guidance is **OS-aware** (mirrors `fastboot_missing_help()`): the "hold Power" step
is universal; the driver step branches on `os.name` — Windows points at
`setup-windows.bat` / `install-edl-host-tools.ps1`, POSIX at android-udev.

## Recovery catalog

Keyed on the resolved `DeviceMode`; the operation supplies the retry verb and a
safety note.

| Mode | Steps |
|------|-------|
| `EDL_9008` | Hold **Power ~12 s** to boot to Android. *(Windows: needs QDLoader/usbser + QPST — run `install-edl-host-tools.ps1`.)* Replug, re-run *{op}*. |
| `FASTBOOT` / `FASTBOOTD` | Run `fastboot reboot`. If `fastboot devices` is empty on Windows the bootloader driver is missing → `setup-windows.bat` (Admin); Linux → android-udev. Re-run *{op}*. |
| `ADB_OFFLINE` | Wait ~30 s for it to reappear; if not, replug a **data** cable (not charge-only) and re-run *{op}*. If it returns "unauthorized", unlock the screen and tap "Allow USB debugging". |
| `ABSENT` | Hold **Power ~10–12 s** to force a reboot, watch for the logo, replug, re-run *{op}*. Try another cable/port if it never shows. |
| `SEALED_OK` | *(Lock only)* Not a failure — "Unit SEALED; adb disconnects by design." Suppressed from the attention list. |

Per-operation **safety note** (appended so the operator knows retry is safe):

- **root** — unit is unharmed; a failed root leaves it bootable, nothing sealed.
- **save** — existing profile untouched; a failed Save never overwrites the good golden.
- **download** — idempotent; re-running re-pushes cleanly.
- **warmup** — changes nothing persistent; safe to re-run once booted.
- **lock** — may be partially sealed; re-run Lock to finish (safe to repeat).

### Phase → expected mode (fallback when the probe is inconclusive)

| Operation | Phase | Expected mode |
|-----------|-------|---------------|
| root | `fastboot_flash` | FASTBOOT/FASTBOOTD |
| root | `edl_flash` | EDL_9008 |
| root | `patch` / `capture` / `wait_boot` | BOOTED/rebooting |
| save | `capture` / `pull` | BOOTED (never reboots) |
| download | `push` / `restore` | BOOTED |
| download | `reboot` | rebooting |
| warmup | `launch` | BOOTED |
| lock | `edl_flash` | EDL_9008 |
| lock | `fastboot_flash` | FASTBOOT |
| lock | `scrub` | BOOTED |
| lock | `done` | SEALED_OK |

## Integration

### 1. Worker hook — `provision.py` (`root_all` / `provision_all` / `warmup_all` / `seal_all`)

Each worker already returns `(status, detail)` and isolates failures in try/except.
On any `fail` / `error` / exception:

- `mode = recovery.probe_mode(adb, fb, serial)`
- `rec = recovery.advise(operation, phase, mode)` (worker passes `operation` always;
  `phase` when cheaply known — the flasher already knows EDL vs fastboot)
- log `rec.log_block()` **live**
- return `(status, detail, rec)` — a 3-tuple. `log_run` / `_report` / `_chain_result`
  already index `res[0]`/`res[1]`, so variable-length tuples are tolerated; the
  guidance also lands in the **Run History reason** for free.

Guard: in `seal_all`, a unit that finishes the seal and *then* drops adb resolves to
`SEALED_OK` and is **not** added to the attention list — a successful seal must never
raise a scary popup.

### 2. End-of-run summary popup — `gui.py` (`_run_bg.done()`)

After controls are restored, gather every `needs_attention` failure into ONE dialog:

```
⚠ 3 devices need attention after Root:
  MQ66…123  EDL/9008 — hold Power ~12s, replug, re-run
  RP6…ABC   fastboot — run `fastboot reboot`, re-run
  ODIN…X    offline mid-flash — replug USB, re-run Root
```

Shown **before** the existing "retry the failures?" prompt, so the operator reads
what to do, then chooses to retry.

### 3. Device-row hint — `gui.py`

`self._last_fail[serial] = rec.row_hint()` on a failed run; `_populate_devices`
renders it on that serial's row (appended to the state cell, tinted red). Cleared
when the serial next probes healthy (`device` + booted) or a new run starts on it —
a transient "needs attention" flag, not a permanent mark.

## Testing

- **`recovery.advise()`** — table test over every (operation × mode): non-empty
  ordered steps, correct retry verb + safety note, and the **OS branch** (monkeypatch
  `os.name`) — Windows says `setup-windows.bat`, POSIX says udev. Explicit assertions
  for the stated examples: EDL → "hold Power ~12s", fastboot → "fastboot reboot".
- **Lock `SEALED_OK` guard** — a seal that succeeds then drops adb is *not* flagged.
- **`probe_mode()`** — a `FakeRunner` posed in each mode (device / offline / fastboot
  / EDL / absent) resolves to the right `DeviceMode`.
- **Integration** — a worker whose op fails returns a 3-tuple whose guidance reaches
  both the history reason and the aggregated popup text; a mixed run builds the
  correct multi-device summary.

## Risks / notes

- **Concurrent session:** another Claude session is editing `provision.py` / `adb.py`
  / `test_cas.py` on this same checkout (operator chose to proceed on the shared tree).
  Watch `git log` for foreign commits; keep edits tight and re-run the full suite
  before committing.
- Keeping the existing scattered EDL/fastboot messages is fine — they live *inside*
  the low-level flashers; the new layer adds the per-device, per-operation end-of-run
  guidance on top. Consolidating them into `recovery.py` is a nice-to-have, not
  required for this change.
