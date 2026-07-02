# CAS Always-Install Set — Design

- **Date:** 2026-07-02
- **Status:** Approved (design); implementation pending
- **Author:** Donald (CTO) + Claude
- **Scope:** A global, operator-configurable set of "always-install" packages that are pre-ticked APK-on in the **Save** (capture) dialog and auto-ticked APK-on in the **Download** (restore) dialog for *every* profile — so apps the operator always wants on every unit (e.g. Steam Link, GameCove Companion) never have to be re-ticked by hand, and install even when their APK lives only in the server store.

---

## 1. Background / Motivation

The operator always wants a few apps on **every** unit regardless of profile — concretely **Steam Link** (`com.valvesoftware.steamlink`) and **GameCove Companion** (`com.gamecove.gamecove_companion`). Today that intent is not expressible, so those apps must be re-selected by hand each time:

- **Save dialog** (`_pick_capture` → `profiles.default_capture_selection` / `initial_capture_selection`): non-emulator apps default **both axes OFF**. Only apps in the hardcoded `EMULATOR_PKGS` set default APK+Config-on; `CONFIG_ONLY_PKGS` default config-only; the game/home launcher default config-only. So on a fresh profile Steam Link is unticked and easy to forget — and if it isn't ticked, it isn't captured, so it can't install on Download.
- **Download dialog** (`_pick_downloads` → `profiles.download_rows`): a captured golden app auto-ticks APK only when the golden bundled an APK (`has_captured_apk`). A **store-only / managed app** (APK in `_apks/`, never bundled in the golden) defaults `(APK off, Config off+disabled)` — you must opt in every time to push the store build.

The fix is a single global list of package ids that both dialogs consult, defaulting to `{Steam Link, GameCove Companion}`.

### Existing infrastructure this builds on
- **Config accessors** (`cas/config.py`): `load_config()` / `save_config()`, and the `library()` / `set_library()` getter+setter pattern (env/config override with a fallback default). New accessors mirror this exactly.
- **Pure selection helpers** (`cas/profiles.py`): `default_capture_selection`, `initial_capture_selection`, `download_rows` — all pure functions that take their inputs as arguments (device app list, saved axes, `has_apk`/`has_config` maps). The always-install set is threaded in the same way (passed by the GUI layer, which reads config), keeping these functions pure and unit-testable.
- **Hardcoded pkg sets** (`cas/profiles.py`): `EMULATOR_PKGS`, `CONFIG_ONLY_PKGS`. The always-install set is the operator-configurable analog — it changes the **APK default**, layered on top of these.
- **GUI wiring** (`cas/gui.py`): `_pick_capture` (line ~1277) and `_pick_downloads` (line ~1352-1354) already assemble the inputs for the pure helpers; they gain one extra argument.

---

## 2. Goals / Non-goals

**Goals**
- One global, operator-editable set of package ids ("always-install") stored in `cas-config.json`.
- Members default **APK-on** in the Save dialog for every profile, so they are captured into the golden without manual ticking.
- Members auto-tick **APK-on** in the Download dialog — including store-only apps not bundled in the golden, so they install from the server store.
- Ships working out of the box: default set = `{com.valvesoftware.steamlink, com.gamecove.gamecove_companion}` when the config key is absent.
- The operator can still untick a member in either modal for a single run (it re-defaults on the next open).

**Non-goals**
- **Per-profile** always-install flags (global only, per user decision).
- **New GUI** to manage the set (edit `cas-config.json` for now).
- Forcing the **Config** axis. Always-install governs the APK/install axis only; Config keeps the existing per-app default and the operator's manual choice. (Rationale: "always install" = install the app; capturing its data/config is a separate decision.)
- Any change for apps **not** in the set.
- Auto-installing an always-install app that exists **nowhere** (not on the device, not in the golden, not in the store) — it simply has no row to tick; out of scope to synthesize one. (May emit an informational log; see §6.)

---

## 3. Data model

New key in `cas-config.json`:

```json
"always_install": ["com.valvesoftware.steamlink", "com.gamecove.gamecove_companion"]
```

- **Absent key → default** `("com.valvesoftware.steamlink", "com.gamecove.gamecove_companion")` (a module-level constant `_DEFAULT_ALWAYS_INSTALL`). Works on any bench with no config editing.
- **Present key (any list, including `[]`) → override.** Setting `[]` disables the feature entirely.

New accessors in `cas/config.py` (mirroring `library()` / `set_library()`):

```python
_DEFAULT_ALWAYS_INSTALL = ("com.valvesoftware.steamlink", "com.gamecove.gamecove_companion")

def always_install_pkgs():
    """The global 'always-install' package set (frozenset). An explicit 'always_install' list in
    cas-config.json overrides the default (an empty list disables the feature)."""
    v = load_config().get("always_install")
    if isinstance(v, list):
        return frozenset(str(p) for p in v)
    return frozenset(_DEFAULT_ALWAYS_INSTALL)

def set_always_install_pkgs(pkgs):
    """Persist the always-install set (list of pkg ids), or clear the override with a falsy value
    (falls back to the default set). Returns the resolved always_install_pkgs()."""
    cfg = load_config()
    if pkgs:
        cfg["always_install"] = sorted({str(p) for p in pkgs})
    else:
        cfg.pop("always_install", None)
    save_config(cfg)
    return always_install_pkgs()
```

`set_*` is provided for completeness/testability and a future GUI; no caller is required in this change.

---

## 4. Behavior changes (pure helpers)

All three helpers gain an `always_install` parameter defaulting to `None` (treated as the empty set) so existing call sites and tests are unaffected until wired.

### 4.1 `default_capture_selection(device_apps, game_launcher=None, home_launcher=None, always_install=None)`
After the existing per-app policy is applied, for each `pkg` on the device that is in `always_install`, force the **APK bit on**, leaving the Config bit at whatever the policy chose:

```python
ai = always_install or frozenset()
for pkg in device_apps:
    if pkg in ai:
        apk, cfg = sel[pkg]
        sel[pkg] = (True, cfg)
```

This runs **after** the `CONFIG_ONLY_PKGS` branch, so a member that is also a sideloaded/config-only pkg still gets APK-on (always-install wins — the operator explicitly wants it installed). Launchers are unaffected (never members in practice).

### 4.2 `initial_capture_selection(device_apps, saved_axes, saved_flags, game_launcher=None, home_launcher=None, always_install=None)`
Threads `always_install` into `default_capture_selection`, then **re-asserts APK-on after the saved-manifest overlay** so a stale saved manifest (APK previously unticked) cannot suppress an always-install member:

```python
sel = default_capture_selection(device_apps, game_launcher, home_launcher, always_install)
# ... existing saved_axes overlay + CONFIG_ONLY_PKGS reassert + launcher seeding ...
for pkg in (always_install or frozenset()):
    if pkg in sel:
        sel[pkg] = (True, sel[pkg][1])
```

Ordering note: the always-install reassert comes **after** the `CONFIG_ONLY_PKGS` reassert, so if a package were in both sets, APK-on wins (matches §4.1). The operator can still untick in the modal for that single run; the modal's returned axes are authoritative for that Save.

### 4.3 `download_rows(own_pkgs, store_pkgs, has_apk, has_config, always_install=None)`
For always-install members, default the **APK checkbox on**, for both golden apps and store-only apps:

```python
ai = always_install or frozenset()
# own_pkgs loop: apk = bool(has_apk.get(pkg, True)) or (pkg in ai)
# store_pkgs loop: rows[pkg] = ((pkg in ai), False); cfg_disabled.add(pkg)
```

Config behavior is unchanged: a store-only member still has no captured config, so its Config box stays off + disabled. A golden member's Config default still follows `has_config`.

---

## 5. GUI wiring (`cas/gui.py`)

- `_pick_capture`: pass `always_install=config.always_install_pkgs()` into `initial_capture_selection`.
- `_pick_downloads`: pass `always_install=config.always_install_pkgs()` into `download_rows`. `store_pkgs` already surfaces store apps as rows, so an always-install store member now appears **and** defaults APK-on.

No modal-layout changes; only default tick state changes.

---

## 6. Edge cases

- **GameCove Companion** is already force-installed post-restore by `install_companion` whenever `COMPANION_PKG` is in the manifest. Adding it to always-install guarantees it is always **selected/captured** so it reliably reaches the manifest; the two mechanisms are complementary, not conflicting.
- **Member not present anywhere** (not on device, not in golden, not in store): no row is produced (rows derive from `device_apps` / `own_pkgs` / `store_pkgs`), so nothing happens. Optional: `_pick_downloads` may log an informational note naming always-install members with no available APK, so a silent no-op isn't mistaken for "installed." (Nice-to-have; may be dropped in the plan.)
- **Operator override for one profile:** unticking a member in the modal applies to that run; it re-defaults on the next open. Permanent, global exclusion = remove it from `always_install` in `cas-config.json`.

---

## 7. Testing

Unit tests (pure functions — no device), added to `tests/test_cas.py`:

- `config.always_install_pkgs()`: default when key absent; override honored; `[]` disables; `set_*` round-trips and clears.
- `default_capture_selection`: a member on the device defaults APK-on even when it isn't an emulator; a non-member is unchanged; a member that is also a `CONFIG_ONLY_PKGS` pkg still gets APK-on.
- `initial_capture_selection`: a member with a stale saved manifest (APK off) is re-asserted APK-on; Config axis is left to policy/saved; a non-member honors the saved manifest.
- `download_rows`: a store-only member defaults `(APK on, Config off)` and appears as a row; a golden member with `has_apk=False` (config-only capture) still gets APK-on; non-members unchanged (regression guard for existing rows).

Full suite must stay green. Baseline is **208 tests on `main`** (this feature branches off `main`; the payload-axis fix that brings it to 211 lives on its own branch and is independent of this work).

---

## 8. Back-compat / rollout

- Additive `always_install` config key; absent → sensible default. No migration.
- Existing profiles unchanged except the two default members now pre-tick — which is the intended behavior.
- Feature is fully disable-able (`"always_install": []`).
- No file-format or on-device changes; `restore.sh` / capture unaffected (the change is purely which boxes are pre-ticked in the PC-side modals).

---

## 9. Out of scope / future

- A GUI control to manage the set (the `set_always_install_pkgs` accessor is provided to make this a small follow-up).
- Per-profile always-install overrides.
- Always-install driving the Config axis.
