# Sequential action-chain runner (footer)

- **Date:** 2026-06-29
- **Status:** Design — awaiting review
- **Area:** `cas/gui.py` (action footer + run dispatch), `tests/test_cas.py`

## 1. Background

The footer has four one-shot action buttons — **⓪ Root → ① Save device→profile → ② Download → ③ Lock** — plus an "Apply to ALL connected devices" toggle and a Cancel button. Each click runs ONE action behind a single `self.busy` flag, so the operator must click an action, **wait for it to finish, then click the next** (Root, wait; Download, wait; Lock). For a unit you almost always run Root→Download→Lock in a row, so this is three click-and-wait cycles per unit.

Two structural facts shape the redesign:
- **Root / Download / Lock are per-unit, PC→device, and run across the targets IN PARALLEL** (`_run_batch(kind, serials)` fans out one action over the selected rows or ALL connected).
- **Save is one-device, device→PC** (golden capture). It does not fit a multi-device parallel chain and is the opposite direction of Download.

This spec replaces the four buttons with **per-action checkboxes + a single Run button** that executes the ticked steps in fixed order, per device, with devices in parallel — so the operator picks the steps once and walks away.

## 2. Goals / Non-goals

**Goals**
- Tick which steps to run (Root / Save / Download / Lock) and run them in one click, in fixed left-to-right order.
- Run the **unit chain** (Root→Download→Lock subset) across the selected rows OR ALL connected, **devices in parallel, in the background**.
- **Stop a device's chain on a failed step** (never Download an un-rooted unit, never Lock a failed Download); other devices keep going.
- **Save⟂Download/Lock** mutual exclusivity in the checkbox set; Save forces single-device.
- Preserve the existing Apply-to-ALL toggle, Cancel (aborts the whole running chain), and per-device/per-step status + logging.

**Non-goals**
- No change to what each action *does* (`root_device`/`capture_update`/`provision_selected`/`seal_device` bodies are reused as step bodies).
- No arbitrary re-ordering (order is fixed Root→Save / Root→Download→Lock).
- No new per-device parallelism model — reuse the existing parallel fan-out.

## 3. UI

Footer `row2` becomes:

```
☐ ⓪ Root    ☐ ① Save    ☐ ② Download    ☐ ③ Lock        [ ▶ Run ]        [ ✗ Cancel ]
```

- **Four checkboxes** (`tk.BooleanVar` each), one **▶ Run** button, plus the existing **✗ Cancel** (right-aligned, live only while busy). The "Apply to ALL connected" toggle and the status/progress bar are unchanged.
- **Mutual exclusivity** (live, via the checkbox commands): ticking **Save** disables + clears Download and Lock; ticking **Download or Lock** disables + clears Save. **Root** is always enabled (valid in both chains). Net valid picks: `{Root?, Save}` or `{Root?, Download?, Lock?}`.
- **Run is enabled only when ≥1 step is ticked** and not busy. Tooltip reflects the resolved chain (e.g. "Run Root → Download → Lock on 3 device(s)").
- Single action = tick one + Run (the old one-click flow, one extra tick).

## 4. Run dispatch

A new `_run_chain(steps, serials)` generalizes today's `_run_batch(kind, serials)`:

- `steps` = the ticked actions in fixed order, as a list of step kinds (`"root"`, `"download"`, `"lock"`, or the single `"save"`).
- **Unit chain** (`steps ⊆ {root, download, lock}`): for **each device in `serials`, in parallel** (same executor/threading as `_run_batch`), run the steps **in sequence**; if a step returns failure for that device, **skip its remaining steps** and mark the device failed. Devices are independent.
- **Golden chain** (`steps` contains `save`): `serials` must resolve to exactly one device (Save is single-device). Run Root (if ticked) then Save on that one device. If Apply-to-ALL is on or multiple rows are selected with Save ticked, **refuse with a clear message** (Save is one device).
- Runs under the existing `self.busy` guard (one chain-run at a time); the checkboxes + Run disable while busy, Cancel stays live.

The four existing handlers are refactored so their per-device work is callable as a step that returns success/failure for one serial (today `_run_batch` already maps a `kind` to per-device work — extend that mapping; the public button handlers become "tick this one + run").

## 5. Error handling

- **Per-device stop-on-fail:** a failed step (`root`/`download`/`lock`) halts only that device's chain; the runner records it and continues other devices. Final summary reports per device which steps ran and where it stopped (reuse `_run_batch`'s existing failed-set + the "retry failed" affordance).
- **Save guard:** Save with >1 target → refuse before starting, no partial run.
- **Cancel:** aborts the in-flight chain — the current step (honoring the existing init_boot-write brick-warning) plus all not-yet-started steps across all devices; leaves devices in whatever state the cancelled step left them (same semantics as cancelling a single action today).
- **Golden safety:** Lock already skips the golden; unchanged. Save never targets a unit chain.

## 6. Reporting

The log/status shows per-device, per-step progress, e.g.:
```
MQ66142509130541: Root ✓  →  Download ✓  →  Lock …
RP6-xxxx:          Root ✗ (flash failed) — chain stopped
```
The activity bar/status line shows the aggregate (e.g. "Running Root→Download→Lock on 3 device(s)…").

## 7. Testing

Python suite (`tests/test_cas.py`, mocked adb):
- `_run_chain` runs steps in fixed order per device; a failing step skips that device's remaining steps but not other devices' (assert the recorded call order per serial).
- Mutual exclusivity: ticking Save clears/disables Download+Lock and vice-versa (pure state logic — factor the exclusivity into a testable method, e.g. `_resolve_chain(ticked) -> ordered steps | error`).
- Save + multi-target → refused (no step bodies invoked).
- Single-step Run reproduces today's single-action behavior.
- All existing footer/seal/provision tests stay green.

GUI rendering (the actual Tk checkboxes/Run) carries a `[VERIFY in app]` marker; the dispatch/exclusivity logic is unit-tested headless.

## 8. Out of scope / future

- Saved/named pipelines, custom ordering, or scheduling.
- Auto-advancing without an explicit Run.
