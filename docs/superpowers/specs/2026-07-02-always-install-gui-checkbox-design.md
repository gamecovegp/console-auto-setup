# Always-Install GUI Checkbox — Design

- **Date:** 2026-07-02
- **Status:** Approved (design); implementation pending
- **Author:** Donald (CTO) + Claude
- **Scope:** Make the global always-install set operator-editable from the app-pick modal — an "Always" checkbox per app row, in BOTH the Save (capture) and Download (restore) pickers — instead of only via editing `cas-config.json`. Extends the config-backed always-install feature (`2026-07-02-always-install-set-design.md`).

---

## 1. Background / Motivation

The always-install set (spec `2026-07-02-always-install-set-design.md`, implemented on `feat/always-install-set`) is config-only: `cas-config.json` → `always_install` list, default `{Steam Link, GameCove Companion}`. There is no UI to manage it, so it isn't discoverable — the operator went looking for an "always install" control in the Managed-APKs and app-pick dialogs and found none. This increment adds that control as an **"Always" checkbox** in the app-pick modal, next to the existing APK / Config checkboxes.

### Existing pieces this builds on
- `cas/gui.py:_app_pick_modal` (~line 1183) — the shared modal used by BOTH pickers; builds one row per app with `APK` + `Config` `ttk.Checkbutton`s bound to `tk.BooleanVar`s, and returns `(axes, flags)` on Run / `None` on Cancel. Its only two callers are `_pick_capture` (Save) and `_pick_downloads` (Download).
- `cas/config.py` — `always_install_pkgs()` (getter, returns `frozenset[str]`; absent key → default, stored `[]` → disabled) and `set_always_install_pkgs()` (setter). This increment REFINES the setter's empty semantics (see §5).
- `cas/profiles.py` — `default_capture_selection` / `initial_capture_selection` / `download_rows` already force APK-on for members; unchanged here.

---

## 2. Goals / Non-goals

**Goals**
- An "Always" checkbox per installable app row in `_app_pick_modal`, shown in BOTH Save and Download pickers.
- Ticking it persists that app into the global `always_install` set; unticking removes it. Effective across all profiles (global), matching "I always want this app installed."
- The set edited via the modal must MERGE with off-modal members (apps not shown in this device/profile's modal are preserved).
- Unticking every member (empty result) DISABLES the feature (stores `[]`), rather than resurrecting the built-in defaults.
- The pure merge logic is unit-tested; the Tkinter wiring is not (consistent with the repo, which does not unit-test the modal).

**Non-goals**
- A separate management dialog (the checkbox lives inline in the existing modal).
- Per-profile always-install (the set stays global).
- Changing the Config axis behavior, or the emulator/CONFIG_ONLY policy.
- Managing always-install for launcher rows (system firmware, never installed) — they get no "Always" checkbox.
- Transactional coupling of the Always write to the multi-profile Download run (see §6 — Always is a standalone global preference, persisted per modal Run).

---

## 3. UI behavior (`_app_pick_modal`)

- New parameter `always_install=None` (a `frozenset[str]`). When provided, each **non-launcher** row gains a third `ttk.Checkbutton` labelled **"Always"**, initial-checked when `pkg in always_install`. When `None` (no caller passed a set), no Always column appears — back-compat.
- **APK lock interaction:** when a row's **Always** is ticked, its **APK** box is set checked and **disabled** (an always-install app is by definition installed). Unticking Always re-enables the APK box and restores it to the value it held before the lock. Implemented with a per-row Tk trace/command on the Always var. Config is never touched by Always.
- **Select all / Deselect all** continue to operate on APK/Config only (not Always) — Always is a persistent preference, not a per-run bulk toggle.
- Tooltip on the Always box: "Always install <pkg> on every unit (adds it to the global always-install set; APK stays on)."

---

## 4. Modal API change

`_app_pick_modal(...)` return becomes a **3-tuple** `(axes, flags, always_ticked)`:
- `axes` — `{pkg:(apk,cfg)}` for every row (unchanged).
- `flags` — `{key:'on'/'off'}` (unchanged).
- `always_ticked` — the `set[str]` of visible rows whose Always box is checked at Run. Empty set when the Always column wasn't shown (`always_install=None`).

Both callers update their unpack: `_pick_capture` (`axes, modal_flags = res` → `axes, modal_flags, always_ticked = res`) and `_pick_downloads` (`axes, fl = res` → `axes, fl, always_ticked = res`). `res is None` (Cancel) handling is unchanged.

---

## 5. Merge + persistence

**Pure merge helper** (new, in `cas/profiles.py`, keeping `_app_pick_modal` thin and giving a unit-testable core):

```python
def merge_always_install(old, visible, ticked):
    """Delta-merge the app-pick modal's Always choices into the global set. `old` = current global set;
    `visible` = pkgs shown in this modal (its editable scope); `ticked` = the visible pkgs the operator
    marked Always. Members NOT visible in this modal are preserved untouched. Returns the new set (frozenset)."""
    old, visible, ticked = frozenset(old), frozenset(visible), frozenset(ticked)
    return (old - visible) | (ticked & visible)
```

**Refined config setter** (`cas/config.py`) — distinguish "clear override" from "store empty (disable)" so the GUI can persist an exact set, including empty:

```python
def set_always_install_pkgs(pkgs):
    """Persist the always-install set. `pkgs is None` CLEARS the override (getter falls back to the default
    set). A list/iterable (INCLUDING an empty one) is stored verbatim, sorted+deduped — an empty list
    DISABLES the feature. A bare string is treated as a single pkg id. Returns always_install_pkgs()."""
    cfg = load_config()
    if pkgs is None:
        cfg.pop("always_install", None)
    else:
        if isinstance(pkgs, str):
            pkgs = [pkgs]
        cfg["always_install"] = sorted({str(p) for p in pkgs})
    save_config(cfg)
    return always_install_pkgs()
```

This supersedes the prior "any falsy value clears" behavior (the `[]`-disables-via-getter note from commit `cee7a89`): now `set_always_install_pkgs([])` stores `[]` (disabled), and only `None` clears. The GUI relies on this to persist an empty set as "disabled" rather than resurrecting defaults.

**Persistence in the callers, on Run:**

```python
visible = set(rows)                                  # the modal's editable scope (all app rows shown)
new = P.merge_always_install(config.always_install_pkgs(), visible, always_ticked)
if new != config.always_install_pkgs():
    config.set_always_install_pkgs(sorted(new))       # verbatim; empty -> disabled
```

`rows` in both callers already excludes launchers, so `visible` is exactly the installable app rows.

---

## 6. Callers (`cas/gui.py`)

- `_pick_capture` (single modal): pass `always_install=config.always_install_pkgs()`; on Run, merge+persist as in §5, then proceed as today.
- `_pick_downloads` (one modal per distinct profile in a loop): pass the same; on each modal's Run, merge+persist immediately. **Deliberate:** the Always set is a global preference independent of the run's transactional manifest writes, so it persists when that modal is Run even if a later profile's modal is cancelled (which still aborts the download itself, writing no manifests — unchanged). Documented so it isn't mistaken for a violation of the "write manifests only after all confirmed" contract.

Both callers already pass `always_install=config.always_install_pkgs()` into `initial_capture_selection`/`download_rows` (from the base feature); that stays and is what makes the newly-ticked members pre-tick APK on the next open.

---

## 7. Testing

Pure/unit tests (`tests/test_cas.py`):
- `merge_always_install`: adds a visible-ticked pkg; removes a visible-unticked member; PRESERVES an off-visible member; empty `ticked` with all members visible → empty result; `ticked` outside `visible` is ignored.
- `config.set_always_install_pkgs`: add assertions that `set_always_install_pkgs([])` STORES `[]` → getter returns empty (disabled), and `set_always_install_pkgs(None)` clears → default. Non-empty stored sorted; bare string → single pkg. The earlier `test_always_install_default_override_and_clear` and `test_always_install_setter_wraps_bare_string` stay valid as-is (they call `save_config({"always_install": []})` and `set_...(None)`/`set_...("com.solo")`, none of which change behavior); only the setter's docstring/comment from `cee7a89` is rewritten to the refined semantics.

GUI wiring (Always checkbox, APK-lock, 3-tuple unpack, merge+persist call) — verified by inspection + full suite green (Tkinter modal not unit-tested here, consistent with the base feature's Task 5).

---

## 8. Back-compat / rollout

- Additive `always_install=None` modal param → no Always column unless a caller passes the set; both callers do.
- The 3-tuple return is internal (only two callers, both updated in this change).
- Config key format unchanged; only the setter's empty-semantics are refined (on the same unmerged branch, before shipping — no migration).
- No on-device / `restore.sh` / capture changes.

## 9. Out of scope / future
- A dedicated "manage always-install" dialog or a column in the Managed-APKs store window.
- Per-profile overrides.
