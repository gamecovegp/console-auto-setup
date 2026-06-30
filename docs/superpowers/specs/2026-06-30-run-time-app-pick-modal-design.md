# Run-time app-pick modal (replace the sidebar Save/Download lists)

**Date:** 2026-06-30
**Status:** Approved — ready for implementation plan
**Area:** `cas/gui.py` (Tkinter front-end only; no changes to capture/restore internals)

## Problem

The right-side **Profile** panel currently carries an "Apps & options" tab holding two
long per-app checklists — **Save (capture from device)** and **Download (restore to
device)** — plus behavior flags and two "Save … selection" buttons. Three issues:

1. **Wrong altitude / too much surface.** Both lists, four select-all buttons, and the
   flag block are always on screen. The user wants the app picking to live in a **modal
   that opens on Run**, not a persistent sidebar.
2. **Detection happens at the wrong time.** The Save list is built at *profile-select*
   time from whatever device is plugged in (e.g. a MANGMI shows up unexpectedly). The
   user expects detection at **Run** time, scoped to the action.
3. **Icons were lost.** A prior refactor (commit `3d65553`) replaced the single
   icon-bearing checkbox with a text-only `ttk.Label`, so app rows show no launcher icon.

## Goal

On **Run**, pop a modal that detects the apps for that action and lets the operator
choose, with icons back on every row:

- **Save** → scan the **connected device's** apps, choose what to capture into the golden.
- **Download** → list the **golden's** apps, choose what to restore.

Remove the persistent sidebar lists entirely.

## Decisions (settled with the user)

| Question | Decision |
|---|---|
| Fate of the sidebar app lists | **Remove entirely.** Right panel keeps Profile selector + golden status, and tabs **ES-DE box art** + **Root images** only. |
| Persistence | **Remember.** Modal pre-fills from the saved manifest (or computed defaults); clicking **Run** rewrites that manifest, so the last choice is the next default. |
| Batch Download (Apply-to-ALL spans multiple profiles) | **One modal per distinct assigned profile**, shown in sequence before the chain runs. Usual case (shared/single profile) = one modal. |

## Architecture

Capture (`PV.capture_to_pc`) and restore (`PV.provision_all`) are **unchanged**: they
read their app list from a manifest **file on disk** (`profile.capture_manifest_path` /
`profile.manifest_path`). The modal's job is exactly today's "Save … selection" buttons —
write that file — but triggered at Run time with run-time-detected rows.

### New unit: `_app_pick_modal(...) -> bool`

A single reusable modal. Inputs:

- `title`, `intro` — window title + one-line explanation.
- `rows`: ordered `{pkg: (apk0, cfg0)}` initial tick state.
- `prof`: the `Profile` (for icon extraction via `_app_icon`).
- `launchers`: set of pkgs whose **APK** checkbox is disabled (system firmware, never
  reinstalled).
- `flag_specs`: ordered list of `(key, label, tip, initial_bool)` for the behavior block.

Renders (top→bottom): intro · **Select all / Deselect all** · scrollable app list (each
row = `_app_name_label` icon+name, then `APK` + `Config` checkboxes; launcher APK
disabled) · `— behavior —` flag checkboxes · pinned **Run** / **Cancel**.

Behavior:
- `transient(self.win)` + `grab_set()` + `wait_window()` — true modal.
- Holds its own `BooleanVar` dicts (`pick_vars`, `flag_vars`) — **no** instance-level
  `pkg_vars`/`cap_vars` anymore. Select/Deselect-all operate on the modal's local dict
  (reuse `_set_all` generalized to take an explicit launcher set).
- **Run** → stash `(axes, flags)` on the modal result and close, return `True`.
- **Cancel** / window-close → return `False`.

Reuses existing helpers: `_scroll_tab` (scroll area), `_app_name_label` (icon row),
`_app_icon` / `_placeholder_icon` (icons).

### Save wrapper — invoked from `run_chain` (save branch)

Order inside `run_chain` when `"save" in steps` (after device select, preflight, and the
profile-name prompt — all unchanged):

1. `serial` = the one selected device; `prof = Profile(profiles_root / name)`.
2. `device_apps = _scan_device_apps(serial)`; `gl, hl = _detect_device_launchers(serial)`.
3. `rows = initial_capture_selection(device_apps, prof.capture_axes(),
   prof.capture_flags(), game_launcher=gl, home_launcher=hl)` (launcher APK forced off).
4. Show `_app_pick_modal("Save — capture <model> into <name>", …, launchers={gl, hl},
   flag_specs=<capture flags>)`. Cancel → abort the whole run (`return`).
5. On Run → write `capture-manifest` using **exactly today's `_save_capture_manifest`
   logic**: launcher rows become `@gamelauncher` / `@homescreen` flags, others become
   package lines with axes. Then proceed to `_run_chain(steps, cleared, save_name=name)`.

### Download wrapper — invoked from `run_chain` (else branch)

Order when `"download" in steps` (after `_action_targets()` + preflight `cleared`):

1. Build the ordered set of **distinct assigned profiles** among `cleared`
   (`self.assigned[serial]` → `Profile`), skipping `(no match)` / unassigned (those are
   already dropped by `_profile_map` at run).
2. For each distinct profile, in sequence:
   - `rows = {pkg: prof.axes().get(pkg, (True, True))}` over `prof.all_pkgs()`; force the
     `launcher_pkg` APK off.
   - Show `_app_pick_modal("Download — restore <name>", …, launchers={launcher_pkg},
     flag_specs=<5 behavior flags>)`. **Cancel on any → abort the entire run** (`return`)
     with **nothing written**.
   - On Run → **stash** that profile's `(axes, flags)` in a pending dict (do not write yet).
3. Only once **every** profile modal has been confirmed, write all stashed manifests
   (`save_manifest` logic: included if either axis ticked; axes + flags), then
   `_run_chain(steps, cleared)` (Root → Download → Lock as resolved). Root/Lock need no
   app pick. Write-after-all so a late Cancel never mutates an earlier profile's saved
   default.

### Sidebar changes

- **Delete** the "Apps & options" tab: its two save buttons, the `_scroll_tab` for it,
  and the Save/Download list-building block in `on_select_profile`.
- Notebook now adds only **ES-DE box art** and **Root images**.
- `on_select_profile` no longer builds app rows; it keeps: golden status refresh, stock
  init_boot field, and `_sync_media_tab()`.
- Remove now-dead instance state/handlers tied to the sidebar lists: `pkg_vars`,
  `cap_vars`, `_on_app_toggle`, `_sync_selall` (if unused), and the standalone
  `save_manifest` / `_save_capture_manifest` methods (their bodies fold into the Run
  wrappers). `_cap_game_launcher` / `_cap_home_launcher` / `_dl_launcher_pkg` become
  local to the wrappers.

### `_sync_media_tab` rework

Today it shows/hides the **ES-DE box art** tab based on whether ES-DE is ticked in
`pkg_vars`. With the sidebar gone it recomputes from the **selected profile's golden**:
show the tab iff `_ESDE_PKG in prof.all_pkgs()` for the currently-selected profile
(no profile → hide). Called from `on_select_profile` and anywhere it is today.

## Data flow

```
Run ▶ ──"save"──▶ pick device + name + preflight
                   └▶ scan device → initial_capture_selection → MODAL
                        ├ Cancel → abort
                        └ Run → write capture-manifest → PV.capture_to_pc (reads file)

Run ▶ ──"download"──▶ targets + preflight → distinct assigned profiles
                       └▶ for each profile: golden apps + prof.axes() → MODAL
                            ├ Cancel(any) → abort whole run
                            └ Run → write that profile's manifest
                       → _run_chain (Root→Download→Lock; restore reads manifest files)
```

## Error / edge handling

- **No golden yet (new Save profile):** `capture_axes()`/`capture_flags()` empty →
  `initial_capture_selection` falls back to defaults (emulators ∩ device + launcher on).
- **Download target with no golden / no manifest:** `prof.all_pkgs()` may be just the
  pkglist; `prof.axes()` empty → every row defaults `(True, True)`.
- **Unassigned / `(no match)` Download target:** excluded from the distinct-profile loop;
  `_profile_map` already skips it at run, so no modal and no restore for it.
- **Cancel semantics:** Save cancel aborts the run before writing `capture-manifest`.
  Download stashes each confirmed profile's selection and writes **all manifests only
  after every modal passes**, so a Cancel on any profile aborts the whole run with zero
  manifest writes.
- **Pillow-less environment:** icons already degrade to colored placeholder chips
  (`_placeholder_icon`); unchanged.

## Testing

- **Unit (pure, no Tk):** keep/extend coverage of the manifest-writing logic by
  factoring the axes→pkgs/flags transforms into small pure helpers the wrappers call
  (mirrors today's `save_manifest` / `_save_capture_manifest`), tested directly.
- **Manual smoke (Tk):** (a) Save with a device connected → modal lists device apps,
  icons present, launcher APK disabled, Run writes `capture-manifest`; (b) Download with
  a profile assigned → modal lists golden apps, Run writes `manifest`; (c) Cancel aborts;
  (d) batch Download across two profiles → two modals in sequence; (e) ES-DE box-art tab
  shows only when the selected profile's golden contains ES-DE.

## Out of scope (YAGNI)

- No change to capture/restore scripts, manifest file format, or profile API.
- No per-device (rather than per-profile) Download modal.
- No live preview/diff of what changed vs. the saved manifest.
