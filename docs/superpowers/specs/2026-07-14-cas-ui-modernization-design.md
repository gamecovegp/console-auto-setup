# CAS UI/UX modernization — design

Date: 2026-07-14
Status: approved (design), not yet implemented

## Problem

The main window is confusing on the bench:

* The right-hand column crams four unrelated things into one panel — the profile
  selector, the golden status, an "ES-DE box art" tab, and the firmware library —
  and it steals half the width from the thing operators actually look at, the
  device list.
* Per-device setup (which profile / which firmware) is done by picking a value in
  a dropdown on the right and then pressing an "Assign → selected" button on the
  left. The link between the two halves is invisible.
* `▶ Run` targets either the selected rows or *every connected device*, depending
  on a checkbox ("Apply to ALL connected devices"). A toggle that silently
  redefines what the main button does is a foot-gun.
* Save asks for the destination profile by typing its name into a free-text box.
* Ctrl/Shift multi-select already works (the tree is `selectmode="extended"`) but
  nothing on screen says so, and nothing reports what is currently selected.
* Visually the app reads as dated: beveled 3D reliefs, ~20px rows, a yellow
  sticky-note tooltip, default gray system buttons, one font size everywhere.

## Goals

1. Full-width device list as the single focus of the main window.
2. Per-device actions (assign profile, assign firmware, run a step, seal, release)
   reachable by right-clicking the selection.
3. Save opens a profile picker instead of a text prompt.
4. Multi-select made visible and reportable.
5. A modern flat look, **with no third-party runtime dependency** (the app ships
   with none today; Pillow is optional and guarded).

## Non-goals

* Rounded corners, drop shadows, ripple animations. Tk cannot draw them on native
  widgets; faking them means a canvas-drawn widget set, which is a separate and
  much larger project.
* Dark mode. The palette is structured so it can be added later, but no dark
  theme ships in this change.
* Any change to `cas.provision`, `cas.adb`, `cas.firmware`, or `cas.profiles`.
  This is a front-end change only.
* Any change to the on-disk config format.

## Design

### 1. Main window

```
┌ File   Settings   ⚠ Warnings (2)   Help ─────────────────────────────┐
│ [⟳ Refresh]  [Profiles…]  [Firmware…]  [Managed APKs…]   Library: … ✓ │  toolbar
├──────────────────────────────────────────────────────────────────────┤
│ serial      model      SD card     profile     firmware      state    │
│ MQ66X01     AIR X      128 GB      air-x-128   air-x-mq66    ● device │  full width
│ RP6A22      Pocket 6   512 GB      rp6-512     (no match)    ● device │
│              Ctrl-click to add · Shift-click for a range              │  hint / empty state
├──────────────────────────────────────────────────────────────────────┤
│ 3 of 5 devices selected                        [Select all] [Clear]   │
│ ⓪ Root   ① Save   ② Download   ③ Warm up   ④ Lock   [▶ Run] [✗ Cancel]│
│ Ready.                                              ▬▬▬▬▬▬▬ progress  │
├──────────────────────────────────────────────────────────────────────┤
│ Log …                                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

Removed from the main window:

| Removed | Replaced by |
|---|---|
| Profile combobox + `New…` / `Delete…` | **Profiles** window (toolbar) + right-click → Assign profile |
| Golden status line | Shown per profile in the Profiles window and in the Save picker |
| "Root images" tab (firmware combobox, Assign, Unset, Add/update, status) | **Firmware** window (toolbar) + right-click → Assign firmware |
| "ES-DE box art" tab | `Settings → ES-DE box art…` dialog |
| `Assign profile → selected` button | right-click → Assign profile |
| `Apply to ALL connected devices` checkbox | selection only; `Select all` button + `Ctrl+A` |

`▶ Run` now always means *the selected rows*. The count line above it
("3 of 5 devices selected") states the blast radius before the operator commits.

Dropping the box-art tab also drops `_sync_media_tab` — the show/hide-the-tab
logic that depended on the selected profile's golden containing ES-DE. Box art is
a bench-wide setting (`config.es_media_src`), not a per-profile one, so it belongs
in Settings.

### 2. Right-click context menu

Selection rule (the file-manager rule; Tk's Treeview does **not** do this by
default, so it is an explicit binding): right-clicking a row that is **not** in
the current selection selects that row alone; right-clicking a row that **is** in
the selection leaves the multi-selection intact.

```
Save “MQ66X01” → profile…            [enabled only when exactly 1 row is selected]
──────────────────────────────
Assign profile   ▸   ● air-x-128     [current assignment is check-marked]
                     ○ rp6-512
                     ──────────────
                     Auto-match (clear override)
                     ＋ New profile…
Assign firmware  ▸   ● air-x-mq66
                     ○ (default kit)
                     ──────────────
                     Clear override (auto-match)
──────────────────────────────
Run              ▸   ⓪ Root   ② Download   ③ Warm up   ④ Lock
──────────────────────────────
Seal (retail lock)…                  [1 row only]
Release (un-provision)…              [1 row only]
Copy serial
```

* Right-click on empty space: `Refresh devices`, `Select all`.
* `Shift+F10` / the Menu key opens the menu at the focused row.
* Double-click a row opens the profile picker for that row (keeps today's fast
  manual-override path, which was double-click = assign the dropdown profile).
* `Run ▸ <step>` calls the existing `_run_chain([step], serials)` — same preflight
  gating, same mini-report, same retry prompt. No second execution path.
* Assigning a profile from the submenu is a **manual** assignment: sticky,
  remembered in `cas-config.json`, tinted in the list, and it joins `force_serials`
  exactly as `assign_profile()` does today. The existing model-mismatch confirm is
  preserved.

### 3. Multi-select

`selectmode="extended"` already gives Ctrl-click (toggle one) and Shift-click
(range). This change makes it visible, it does not rebuild it:

* Hint line under the list: `Ctrl-click to add · Shift-click for a range`.
* Footer counter: `N of M devices selected` (and `No devices selected` at zero).
* `Select all` / `Clear` buttons, plus a `Ctrl+A` binding on the tree.
* `▶ Run` disables itself when the selection is empty, instead of popping a
  "select one or more device rows" messagebox after the click.

### 4. Save — two-step picker

Both Save entry points — the footer's `① Save` chain tick and the context menu's
`Save “<serial>” → profile…` — go through the same two steps. The free-text
`simpledialog.askstring` in `run_chain()` is replaced by step 1.

Step 1 (new modal, `dialogs.ProfilePicker`):

```
┌─ Save — which profile? ───────────────┐
│ ● air-x-128    AIR X    golden 3.4 GB │
│                         saved 07-11   │
│ ○ rp6-512      Pocket 6 golden 12 GB  │
│ ○ odin2-mini   Odin 2   — empty       │
│ ＋ New profile…                        │
│                                       │
│ ⚠ air-x-128 already has a golden      │
│   (3.4 GB) — saving REPLACES it.      │
│                    [Cancel]  [Next ›] │
└───────────────────────────────────────┘
```

* Pre-selects the device's currently-assigned profile.
* `＋ New profile…` runs the existing `new_profile()` flow and selects the result.
* The overwrite warning appears inline when the highlighted profile already has a
  golden; it is a warning, not a block.
* `Next ›` hands the chosen name to the **existing** `_pick_capture()` app-picker
  modal, unchanged.
* Cancel at either step writes nothing — same as today.

The picker is also reused (without the overwrite warning) for double-click and for
right-click → Assign profile → `＋ New profile…`.

### 5. Profiles and Firmware windows

Both are plain `Toplevel` list windows opened from the toolbar.

**Profiles**: one row per profile — name, `model_match`, golden size, capture date.
Buttons: `New…`, `Delete…` (archives, types-the-name-to-confirm, as today),
`Open folder`. Selecting a row shows its golden status and download-ETA line (the
current `_update_golden_status` text).

**Firmware**: one row per firmware id — id, current version, match rules, payload
path, and the logic-check state. Buttons: `Add / update…` (the existing
`FW.ingest` flow), `Open folder`. The library-location + reachability line that
lives in the tab today moves here verbatim.

Per-device assignment is *not* in these windows — it is on the device row's
context menu. These windows manage the library; the context menu applies it.

### 6. Theme — `cas/theme.py`

One module, applied once at startup, restyling ttk's `clam`. No new dependency.

* **Palette** — a dict, so a dark variant can be added later without touching
  widget code:
  * surface `#FFFFFF`, surface-alt `#F5F6F8`, border `#E5E7EB`
  * text `#1F2024`, muted `#6B7280`
  * accent `#A855F7` (the GameCove purple already used by `_PH_PALETTE`),
    accent-tint `#EDE4FB` for selection
  * ok `#10B981`, warn `#F59E0B`, danger `#EF4444`
* **Type scale** — resolve the best available UI font per OS from a preference
  list (Segoe UI Variable → Inter → Cantarell → the Tk default), and define
  title / body / caption / mono sizes instead of one size everywhere.
* **Widgets** — flat buttons with hover and pressed state layers; a filled accent
  `Accent.TButton` for `▶ Run`; an 8px spacing grid; `Treeview` at ~28px row
  height, flat header, accent-tinted selection, zebra striping; colored state dots
  (green `device`, amber `unauthorized`, red `offline`, blue `fastboot`/`EDL`); a
  thin accent progress bar; slim scrollbars; the tooltip restyled from a yellow
  sticky-note to a dark chip.

What actually reads as dated in the current app is the beveled 3D relief, the
cramped rows, the yellow tooltip, the gray system buttons, and the flat type
scale. Removing those is what buys the modern look — not the corner radius we
cannot draw.

### 7. Files

| File | Change |
|---|---|
| `cas/theme.py` | **new** — palette, font resolution, `apply(root)` |
| `cas/dialogs.py` | **new** — `ProfilePicker`, Profiles window, Firmware window, box-art dialog |
| `cas/gui.py` | right panel, batch toggle and their handlers removed; toolbar, context menu, selection counter added |

The Managed-APKs window stays in `gui.py` and simply inherits the theme — moving
it is churn with no gain.

### 8. Testing

The GUI is tested today without a display: `App.__new__(App)` plus stubs, and pure
module-level helpers. The new decision logic follows the same rule — it lives in
pure functions so no test needs a Tk display:

* `gui._rightclick_selection(clicked_row, current_selection)` → the new selection.
* `gui._context_actions(n_selected, state)` → which menu items are enabled.
* `gui._selection_summary(n_selected, n_total)` → the footer counter text.
* `theme.pick_font(available_families)` → the chosen UI font.
* `dialogs.profile_rows(profiles_root)` → the picker's rows (name, model, golden
  size, capture date).

Two existing tests drive `assign_firmware()` through a stubbed `fw_var`
(`tests/test_cas.py:4538`, `:4563`); the method takes the firmware id as an
argument now, so those two calls become `app.assign_firmware("ayn-m2")`.

### 9. Compatibility and risk

* **No config migration.** `device_profiles`, the firmware overrides,
  `es_media_src` and `always_install` keep their current keys and semantics; an
  operator's existing `cas-config.json` carries over untouched.
* **No packaging change.** No new runtime dependency, so `scripts/cas.spec` and
  `.github/workflows/build.yml` need no new requirement. `cas/theme.py` and
  `cas/dialogs.py` are imported by `cas/gui.py`, so PyInstaller picks them up
  through the existing analysis — no `hiddenimports` entry needed.
* **Behavior preserved.** Preflight warning gates, the mini-report, per-failure
  retry, the cancel path, the flash-critical brick guard, sticky manual
  assignments and `force_serials` all keep working exactly as they do now. The
  only behavioral removal is the `Apply to ALL` toggle, whose job is now done by
  `Select all`.
