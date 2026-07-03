# Move the "Always" control from the pick modals into the Managed APKs window

**Date:** 2026-07-03
**Status:** Design — approved
**Area:** GUI (cas/gui.py)

## Problem

The global always-install set is edited via a per-row **"Always"** checkbox in the Save/Download
app-pick modals (`_app_pick_modal`). The operator wants that checkbox **out** of those pickers and
instead managed per-APK in the **Managed APKs (server store)** window (`_open_apk_store`), where
managed apps already live.

## Key fact (why this is clean)

The always-install set is passed **twice** in each picker caller, for two independent purposes:

1. To the **row computation** — `P.initial_capture_selection(...)` (Save) and `P.download_rows(...)`
   (Download) — which force the **APK bit pre-ticked ON** for always-apps. This is the *enforcement*.
2. To `_app_pick_modal(..., always_install=...)` — which renders the **Always column + APK lock** and
   returns the ticked set. This is the *editing UI*.

Removing (2) does **not** touch (1): always-apps stay APK-pre-ticked in the modals; only the ability
to change set membership moves.

## Design

### 1. Pick modals — remove the Always column

- `_app_pick_modal`: drop the `always_install` parameter and everything it drives — `ai`,
  `always_vars`, `_apk_locked`, the per-row "Always" `Checkbutton` + `_lock` closure, and
  `result["always"]`. The Select/Deselect-all buttons drop the `_apk_locked()` union (no more locked
  rows). Return value becomes **`(axes, flags)`** (was `(axes, flags, always)`).
- `_pick_capture` (Save): remove `always_install=config.always_install_pkgs()` from the
  `_app_pick_modal(...)` call (KEEP it on `initial_capture_selection`); change
  `axes, modal_flags, always_ticked = res` → `axes, modal_flags = res`; delete the
  `self._persist_always_install(...)` line.
- `_pick_downloads` (Download): remove `always_install=...` from the `_app_pick_modal(...)` call
  (KEEP it on `download_rows`); `axes, fl, always_ticked = res` → `axes, fl = res`; delete the
  `self._persist_always_install(...)` line.
- Delete the now-unused `_persist_always_install` method (only those two callers referenced it).

### 2. Managed APKs window — add an ALWAYS toggle

In `_open_apk_store`:
- Treeview columns become `("pkg", "label", "files", "always")`; the `ALWAYS` column shows `●` for
  members of `config.always_install_pkgs()`, blank otherwise. `refresh()` reads the set once and sets
  each row's `always` cell.
- New `toggle_always()`: on the selected row's pkg, read `set(config.always_install_pkgs() or [])`,
  add if absent / discard if present, `config.set_always_install_pkgs(sorted(new))`, log it, refresh.
  (Empty result disables the feature per `set_always_install_pkgs`'s existing contract — acceptable;
  it matches today's "empty = off" semantics.)
- Wire it to a **"Toggle Always"** button in the button bar and to **double-click** on a row
  (`tree.bind("<Double-1>", ...)`).

### Scope / semantics

"Always" is now a property of **store-managed APKs** — an app must be in the store to be marked
always-install (which is consistent: an always-installed app needs a store build to install from
everywhere). The default set (steamlink + companion) are store apps, so they appear there. Golden-only
apps that are not in the store are out of scope (not markable in the GUI); the config key still honors
any pre-existing entries and continues to force APK-on in the modals.

### Non-goals

- No change to enforcement (always-apps still pre-tick APK in the modals via the row-computation).
- No change to `config.always_install_pkgs`/`set_always_install_pkgs`, `P.merge_always_install`, or any
  provision-side behavior.

## Testing

- **Update** the existing `TestPickCapture`/`TestPickDownloads` always-persist tests: `_app_pick_modal`
  no longer takes/returns `always`, and `_persist_always_install` is gone — drop the always-path
  assertions (or the whole test if it only covered that path), keep the axes/flags coverage.
- **Add** a test for the store-window toggle logic: isolating `CAS_CONFIG` to a temp file, exercise the
  toggle operation (add then remove a pkg) and assert `config.always_install_pkgs()` reflects it. If the
  toggle body is a closure inside `_open_apk_store`, factor the membership flip into a tiny testable
  helper (e.g. module-level `toggle_membership(current, pkg) -> set`) and test that + the config write.
- Full suite (`tests.test_cas tests.test_firmware tests.test_warnings tests.test_uiauto`) stays green.
