# Device Warnings Menu + Pre-Flight Gating — Design

**Date:** 2026-06-29 · **Status:** approved (design) · **Owner:** Donald (CTO)

## 1. Goal
Surface every **actionable** condition that blocks or risks a device operation in one place — a live
`⚠ Warnings (N)` menu in the menu bar — and stop the obvious foot-guns at action time. The motivating case:
when a device's firmware/identity **can't be auto-detected**, say so loudly ("cannot root this device")
instead of the current subtle `(no match)` / `⚠` in the firmware column.

Today the only warning signals are a `⚠` glyph in the firmware column and a one-line firmware status label.
`FW.resolve()` / `FW.logic_check()` already produce the firmware warning strings; adb already reports device
state. Nothing aggregates them, and nothing gates the destructive ⓪ Root / ③ Lock flashes.

## 2. Scope
**In scope:**
- A pure `cas/warnings.py` module: catalog + `evaluate()` + `gate()` (no adb/Tk; unit-testable off-device).
- A best-effort bootloader-lock detector in `cas/adb.py` (`bootloader_state()` + pure `_parse_bootloader_state`).
- A live `⚠ Warnings (N)` top-level menu, rebuilt each `refresh_devices`, with per-warning click-to-select
  and an "Open warnings report…" dialog.
- Pre-flight gating (`_preflight`) in front of both action choke points — `_run_batch` and `_run_chain` —
  that hard-blocks unsafe devices and soft-confirms risky ones, then runs only the survivors.
- `tests/test_warnings.py` + parse tests for the detector.

**Out of scope:**
- New device-side capabilities (we only *read* state; no unlock automation, no flashing changes).
- Auto-fixing warnings (we explain the fix; the operator acts).
- Per-warning colored text inside the Tk menu (Tk can't portably color menu entries — emoji severity in the
  menu; colors live in the report dialog).

## 3. Warning catalog
Each warning declares which actions it **gates** and how: `block` (skip the device, not overridable) or
`confirm` (ask "proceed anyway?"). No gate ⇒ `info` (listed, never blocks). Severity shown = the strongest
gate any action gets (`block` ✗ > `confirm` ⚠ > `info` ℹ).

### Per-device — connection
| Code | Fires when | Root | Save | Download | Lock |
|---|---|---|---|---|---|
| `unauthorized` | adb state = `unauthorized` | block | block | block | block |
| `offline` | adb state ∈ {offline, recovery, sideload, no permissions} | block | block | block | block |

### Per-device — root / brick-guard
| Code | Fires when | Root | Lock |
|---|---|---|---|
| `no_flash_target` | state=device but `identity.flash_target` empty (can't tell init_boot vs boot) | **block** | block |
| `bootloader_locked` | `bootloader_state` confidently = `locked` | **block** | block |
| `fw_flash_mismatch` | `logic_check`: firmware expects init_boot, device exposes boot (or vice-versa) | confirm | confirm |
| `fw_variant_mismatch` | `logic_check`: device-name / serial-prefix mismatch (e.g. MQ65 ↔ MQ66 cross-flash) | confirm | confirm |
| `profile_model_mismatch` | assigned profile `model_match` ≠ device model (bootloop risk) | confirm | confirm |

### Per-device — advisory / info
| Code | Fires when | Gate |
|---|---|---|
| `no_firmware_match` | `FW.resolve` found no library firmware match | info — Root uses the bundled kit / profile image; "verify the right image" |
| `bootloader_unknown` | lock state unreadable | info — **deliberately not a gate** (most units can't report it; never block the working flow) |
| `no_profile` | assigned profile = `(no match)` / none | block ②Download · confirm ⓪Root |
| `no_golden` | profile assigned but no golden saved | block ②Download |
| `identity_incomplete` | state=device but `serial` unreadable | info — sticky per-device assignment won't persist |

### Global
| Code | Fires when | Gate |
|---|---|---|
| `library_unreachable` | `profiles_root` not a reachable dir (NAS unmapped) | block ②Download + ①Save · info ⓪Root/③Lock |
| `firmware_library_empty` | no firmware in the library at all | info |

The two true **"cannot root this device"** hard stops are `no_flash_target` and `bootloader_locked` (plus
connection). Firmware-library codes are softer because Root flashes the *profile's* patched init_boot /
bundled kit, not the library entry.

## 4. Architecture

### 4.1 `cas/warnings.py` (pure)
- `CATALOG: dict[code] -> {title, detail, fix, gates}` where `gates: {action: "block"|"confirm"}`.
  - `title`/`detail`/`fix` may be format strings filled from the snapshot (e.g. the live mismatch text).
- `evaluate(devices, global_state) -> list[Warning]` where:
  - `devices`: list of **DeviceSnapshot** dicts (assembled by the GUI, see 4.3).
  - `global_state`: `{"library_reachable": bool, "firmware_library_empty": bool}`.
  - `Warning = {scope, serial, code, severity, title, detail, fix, gates}` (`scope ∈ {"device","global"}`).
- `gate(warnings, serial, actions) -> {"block": [Warning], "confirm": [Warning]}` — the pre-flight helper;
  for `serial=None` it returns matching global warnings.
- `count_actionable(warnings) -> int` — blockers + advisories (excludes pure `info`); drives the menu `(N)`.

`evaluate` is the only place severity/gating logic lives. The firmware codes are derived by matching the
*strings already in* `fw["warnings"]` (flash-target text vs device/serial-prefix text), so we don't re-run
`logic_check`.

### 4.2 `cas/adb.py` — bootloader detection
- `_parse_bootloader_state(props: dict) -> "locked"|"unlocked"|"unknown"` (pure):
  - prefer `ro.boot.vbmeta.device_state` (`locked`/`unlocked`);
  - else `ro.boot.verifiedbootstate` (`orange` ⇒ unlocked; `green`/`yellow` ⇒ locked);
  - else `unknown`.
- `Adb.bootloader_state()` reads those props (one batched getprop, best-effort, never raises) and returns the
  parsed value. `unknown` on any failure — we never falsely hard-block.

### 4.3 GUI wiring (`cas/gui.py`)
- **Snapshot assembly:** in `refresh_devices()`'s background `work()`, alongside the existing identity + fw
  resolve, add per device: `bootloader_state` (state=device only), `profile_name`, `profile_has_golden`,
  `profile_model_match_ok`. Build a `DeviceSnapshot` list and hand it to `_populate_devices`.
- `_populate_devices(...)` (UI thread): `self.warnings = WARN.evaluate(snaps, global_state)`; then
  `self._rebuild_warnings_menu()`.
- **Menu (`_build_menu`):** add a top-level cascade kept as `self._warn_menu` with its bar index stored.
  - Title via `bar.entryconfig(idx, label=...)`: `✓ Warnings` when none, else `⚠ Warnings (N)`.
  - Submenu rebuilt each refresh: one entry per warning `✗/⚠/ℹ  <serial|ALL> — <title>`; command selects the
    device row (`dev_tree.selection_set` + `see`) and shows a detail messagebox (title/detail/fix). Info
    entries grouped below a separator. Final entry: "Open warnings report…".
  - `_open_warnings_report()`: a `Toplevel` with a `ttk.Treeview` grouped by device, severity tag colors
    (`#b00020` block / `#a06000` confirm / `#666` info) and a "Copy" button (dumps the catalog as text).
- **Pre-flight (`_preflight(actions, serials) -> list[str] | None`):** UI thread, before `_run_bg`:
  1. global `block` for any action ⇒ `messagebox.showerror` listing them, return `None` (abort).
  2. per serial: `g = WARN.gate(self.warnings, s, actions)`; if `g["block"]` ⇒ log "skipped <s>: …", drop it;
     elif `g["confirm"]` ⇒ one `askyesno` listing them, drop on "No"; else keep.
  3. if nothing left ⇒ `messagebox.showinfo("nothing to run")`, return `None`; else return survivors.
  - Wired: `root_device`/`seal_device`/`provision_selected` call `_preflight([kind], targets)` before
    `_run_batch`; `capture_update` calls `_preflight(["save"], [serial])`; `_run_chain` calls
    `_preflight(steps, serials)` before launching, replacing `serials` with survivors.

## 5. Data flow
```
refresh_devices.work()  ──>  per-device snapshot {state, identity, fw, bootloader, profile_*}
                              + global {library_reachable, firmware_library_empty}
        │ (win.after)
        ▼
_populate_devices ──> WARN.evaluate() ──> self.warnings ──> _rebuild_warnings_menu()
                                                  │
press ⓪/②/③ or Run chain ──> _preflight(actions, targets) ──> WARN.gate() per serial
                                                  │  block→skip · confirm→ask · clear→keep
                                                  ▼
                                       _run_batch / _run_chain (survivors only)
```

## 6. Error handling
- `evaluate` is pure and total: a malformed snapshot field degrades to `unknown`/`info`, never raises.
- Snapshot assembly is wrapped per device (an adb hiccup on one unit can't break the refresh of others) —
  matching the existing `fwmap` try/except.
- `bootloader_state` / `_parse_bootloader_state` never raise; `unknown` is the safe fallback.
- A missing `self.warnings` (before first refresh) ⇒ `_preflight` treats it as empty (no gating), so actions
  behave exactly as today until the first refresh populates warnings.

## 7. Testing
- `tests/test_warnings.py` (pytest, off-device like `test_firmware.py`): one test per catalog row — feed a
  synthetic snapshot, assert the expected `code`, `severity`, and `gates`; plus `count_actionable` and `gate`
  (block vs confirm vs clear partitioning, global scope).
- `_parse_bootloader_state`: locked / unlocked / unknown across vbmeta + verifiedbootstate + empty inputs.
- Existing suites must stay green (`tests/test_cas.py`, `tests/test_firmware.py`).

## 8. Files
- **new** `cas/warnings.py`, `tests/test_warnings.py`
- **edit** `cas/adb.py` (detector), `cas/gui.py` (snapshot, menu, pre-flight)
