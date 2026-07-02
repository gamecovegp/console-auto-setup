# Always-Install Set Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global, operator-configurable "always-install" package set (default `{Steam Link, GameCove Companion}`) that pre-ticks APK-on in the Save dialog and auto-ticks APK-on in the Download dialog for every profile.

**Architecture:** One new `cas-config.json` key (`always_install`) with getter/setter in `cas/config.py`. The set is threaded as an optional argument into three existing pure helpers in `cas/profiles.py` (`default_capture_selection`, `initial_capture_selection`, `download_rows`), which force the APK bit on for members. `cas/gui.py` reads the config set and passes it to those helpers. No on-device or modal-layout changes.

**Tech Stack:** Python 3 stdlib, `unittest` (existing `tests/test_cas.py`), Tkinter GUI (unchanged surface).

**Spec:** `docs/superpowers/specs/2026-07-02-always-install-set-design.md`
**Branch:** `feat/always-install-set` (already created off `main`; the spec commit `13856b5` is its tip).

## Global Constraints

- **APK axis only.** Always-install membership forces the **APK** default on; the **Config** default is never changed by this feature (it keeps the existing per-app policy / operator choice). Copied from spec §2, §4.
- **Default set** (config key absent): `("com.valvesoftware.steamlink", "com.gamecove.gamecove_companion")`. An explicit list overrides it; a stored empty list `[]` disables the feature. Spec §3.
- **Pure helpers stay pure.** `default_capture_selection` / `initial_capture_selection` / `download_rows` take `always_install` as an argument (default `None` → empty set); they never import `config`. The GUI layer reads config and passes the set in. Spec §1, §4.
- **Always-install wins** over the emulator / `CONFIG_ONLY_PKGS` policy and over a stale saved manifest (its reassert runs *after* those). Spec §4.1, §4.2.
- **Back-compat:** every new parameter defaults to `None`; existing call sites and the 208-test `main` baseline stay green until wired in Task 5.
- **Setter mirrors `set_library`:** a falsy argument clears the override (→ default); a stored `[]` (via direct config write) disables. Spec §3.

---

### Task 1: Config accessors (`always_install_pkgs` / `set_always_install_pkgs`)

**Files:**
- Modify: `cas/config.py` — add after `set_library()` (currently ends ~line 58, before `history_dir`)
- Test: `tests/test_cas.py` — add a test method in the config-accessor test class (the class containing `test_config_library_wins_over_default`, ~line 637)

**Interfaces:**
- Produces: `config.always_install_pkgs() -> frozenset[str]`; `config.set_always_install_pkgs(pkgs) -> frozenset[str]`; module constant `config._DEFAULT_ALWAYS_INSTALL: tuple[str, str]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cas.py` inside the config test class:

```python
    def test_always_install_default_override_and_clear(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            # key absent -> default set
            self.assertEqual(
                C.always_install_pkgs(),
                frozenset({"com.valvesoftware.steamlink", "com.gamecove.gamecove_companion"}))
            # explicit override wins and is persisted sorted
            self.assertEqual(C.set_always_install_pkgs(["com.foo", "com.bar"]),
                             frozenset({"com.foo", "com.bar"}))
            self.assertEqual(C.load_config().get("always_install"), ["com.bar", "com.foo"])
            # a stored empty list DISABLES the feature (getter honors [])
            C.save_config({"always_install": []})
            self.assertEqual(C.always_install_pkgs(), frozenset())
            # setter with a falsy value CLEARS the override -> back to default (mirrors set_library)
            self.assertEqual(
                C.set_always_install_pkgs(None),
                frozenset({"com.valvesoftware.steamlink", "com.gamecove.gamecove_companion"}))
            self.assertNotIn("always_install", C.load_config())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest "tests/test_cas.py::TestConfig::test_always_install_default_override_and_clear" -v`
Expected: FAIL with `AttributeError: module 'cas.config' has no attribute 'always_install_pkgs'`.

- [ ] **Step 3: Write minimal implementation**

In `cas/config.py`, immediately after the `set_library()` function:

```python
_DEFAULT_ALWAYS_INSTALL = ("com.valvesoftware.steamlink", "com.gamecove.gamecove_companion")


def always_install_pkgs():
    """The global 'always-install' package set (frozenset) — apps pre-ticked APK-on in the Save dialog
    and auto-ticked APK-on in the Download dialog for every profile. An explicit 'always_install' list in
    cas-config.json overrides the default; a stored empty list disables the feature."""
    v = load_config().get("always_install")
    if isinstance(v, list):
        return frozenset(str(p) for p in v)
    return frozenset(_DEFAULT_ALWAYS_INSTALL)


def set_always_install_pkgs(pkgs):
    """Persist the always-install set (iterable of pkg ids, stored sorted), or clear the override with a
    falsy value so it falls back to the default set (mirrors set_library). Returns always_install_pkgs()."""
    cfg = load_config()
    if pkgs:
        cfg["always_install"] = sorted({str(p) for p in pkgs})
    else:
        cfg.pop("always_install", None)
    save_config(cfg)
    return always_install_pkgs()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest "tests/test_cas.py::TestConfig::test_always_install_default_override_and_clear" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(config): always_install package set accessors (default steamlink+companion)"
```

---

### Task 2: `default_capture_selection` forces APK-on for members

**Files:**
- Modify: `cas/profiles.py:545-560` (`default_capture_selection`)
- Test: `tests/test_cas.py` — add near the existing `test_default_capture_checks_emulators_and_game_launcher`

**Interfaces:**
- Consumes: nothing new.
- Produces: `default_capture_selection(device_apps, game_launcher=None, home_launcher=None, always_install=None) -> dict[str, tuple[bool, bool]]` — members on the device get APK forced on, Config left to policy.

- [ ] **Step 1: Write the failing test**

```python
    def test_default_capture_always_install_forces_apk_on(self):
        from cas import profiles as P
        apps = ["com.valvesoftware.steamlink", "com.github.stenzek.duckstation", "com.random.app"]
        ai = frozenset({"com.valvesoftware.steamlink"})
        sel = P.default_capture_selection(apps, always_install=ai)
        self.assertEqual(sel["com.valvesoftware.steamlink"], (True, False))   # non-emulator member: APK on, Config policy-off
        self.assertEqual(sel["com.github.stenzek.duckstation"], (True, True)) # emulator unchanged
        self.assertEqual(sel["com.random.app"], (False, False))              # non-member unchanged
        # a member that is ALSO config-only (sideloaded) still gets APK on (always-install wins)
        sel2 = P.default_capture_selection(["xyz.aethersx2.tturnip"],
                                           always_install=frozenset({"xyz.aethersx2.tturnip"}))
        self.assertEqual(sel2["xyz.aethersx2.tturnip"], (True, True))
        # back-compat: no always_install arg == today's behavior
        self.assertEqual(P.default_capture_selection(["com.random.app"]), {"com.random.app": (False, False)})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest "tests/test_cas.py" -k always_install_forces_apk_on -v`
Expected: FAIL with `TypeError: default_capture_selection() got an unexpected keyword argument 'always_install'`.

- [ ] **Step 3: Write minimal implementation**

Replace `default_capture_selection` in `cas/profiles.py` with:

```python
def default_capture_selection(device_apps, game_launcher=None, home_launcher=None, always_install=None):
    """The default Save-list check state: {pkg: (apk_on, config_on)}. Emulators (EMULATOR_PKGS) -> both axes,
    EXCEPT CONFIG_ONLY_PKGS (APK sideloaded externally) -> config-only; the game/HOME launcher -> config-only
    (APK is system firmware, but their state — emulator picks / homescreen — is worth keeping, so config
    defaults ON); every other device app -> off. Finally, any device app in `always_install` (the global
    always-install set) has its APK bit forced ON (Config left to the above policy) — these are apps the
    operator wants installed on every unit."""
    ai = always_install or frozenset()
    sel = {}
    for pkg in device_apps:
        if pkg in CONFIG_ONLY_PKGS:
            sel[pkg] = (False, True)                 # APK is provided externally -> capture config/BIOS only
        else:
            on = pkg in EMULATOR_PKGS
            sel[pkg] = (on, on)
    for lp in (game_launcher, home_launcher):
        if lp:
            sel[lp] = (False, True)                  # config-on by default (@gamelauncher / @homescreen)
    for pkg in device_apps:                          # always-install: force APK on, keep the Config default
        if pkg in ai and pkg in sel:
            sel[pkg] = (True, sel[pkg][1])
    return sel
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest "tests/test_cas.py" -k "always_install_forces_apk_on or default_capture" -v`
Expected: PASS (new test + the existing `test_default_capture_checks_emulators_and_game_launcher` regression).

- [ ] **Step 5: Commit**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(capture): always_install forces APK-on default in default_capture_selection"
```

---

### Task 3: `initial_capture_selection` re-asserts APK-on after the saved overlay

**Files:**
- Modify: `cas/profiles.py:563-583` (`initial_capture_selection`)
- Test: `tests/test_cas.py` — near Task 2's test

**Interfaces:**
- Consumes: `default_capture_selection(..., always_install=...)` from Task 2.
- Produces: `initial_capture_selection(device_apps, saved_axes, saved_flags, game_launcher=None, home_launcher=None, always_install=None) -> dict[str, tuple[bool, bool]]`.

- [ ] **Step 1: Write the failing test**

```python
    def test_initial_capture_always_install_overrides_stale_manifest(self):
        from cas import profiles as P
        apps = ["com.valvesoftware.steamlink", "com.random.app"]
        saved = {"com.valvesoftware.steamlink": (False, False),   # stale: APK previously unticked
                 "com.random.app": (True, True)}
        ai = frozenset({"com.valvesoftware.steamlink"})
        sel = P.initial_capture_selection(apps, saved, {}, always_install=ai)
        self.assertEqual(sel["com.valvesoftware.steamlink"], (True, False))  # APK re-asserted on; Config from saved (False)
        self.assertEqual(sel["com.random.app"], (True, True))               # non-member honors saved manifest
        # back-compat: no always_install arg == today's behavior (saved manifest wins)
        sel2 = P.initial_capture_selection(apps, saved, {})
        self.assertEqual(sel2["com.valvesoftware.steamlink"], (False, False))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest "tests/test_cas.py" -k always_install_overrides_stale -v`
Expected: FAIL with `TypeError: initial_capture_selection() got an unexpected keyword argument 'always_install'`.

- [ ] **Step 3: Write minimal implementation**

Replace `initial_capture_selection` in `cas/profiles.py` with:

```python
def initial_capture_selection(device_apps, saved_axes, saved_flags, game_launcher=None, home_launcher=None,
                              always_install=None):
    """The Save-list initial check state: default_capture_selection, overlaid by a saved capture-manifest's
    package axes, then the launcher rows seeded from the saved @gamelauncher/@homescreen flags. Members of
    `always_install` have their APK bit re-asserted ON *after* the saved overlay, so a stale saved manifest
    (APK previously unticked) can't suppress an always-install app. Pure — no I/O."""
    ai = always_install or frozenset()
    sel = default_capture_selection(device_apps, game_launcher, home_launcher, ai)
    # A saved manifest only OVERRIDES the axes of apps that are actually on this device — it never ADDS a
    # row. Capturing into a profile whose golden came from another unit must not surface apps this device
    # doesn't have (e.g. AetherSX2 on a Retroid that only ships NetherSX2). The scan is authoritative.
    for pkg, axes_pair in (saved_axes or {}).items():
        if pkg in sel:
            sel[pkg] = axes_pair
    # Sideloaded builds: the config-only policy WINS over a stale saved manifest that had APK on — the
    # operator never bundles their externally-installed APK (e.g. PS2) by accident. Config choice is kept.
    for pkg in CONFIG_ONLY_PKGS:
        if pkg in sel:
            sel[pkg] = (False, sel[pkg][1])
    # Always-install WINS over both the saved overlay and the config-only reassert above: force APK on.
    for pkg in ai:
        if pkg in sel:
            sel[pkg] = (True, sel[pkg][1])
    if game_launcher and game_launcher in sel:
        sel[game_launcher] = (False, (saved_flags or {}).get("gamelauncher", "on") == "on")
    if home_launcher and home_launcher in sel:
        sel[home_launcher] = (False, (saved_flags or {}).get("homescreen", "on") == "on")
    return sel
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest "tests/test_cas.py" -k "always_install_overrides_stale or initial_capture" -v`
Expected: PASS (new test + existing `initial_capture` regressions).

- [ ] **Step 5: Commit**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(capture): initial_capture_selection re-asserts always_install APK-on"
```

---

### Task 4: `download_rows` auto-ticks APK for members (golden + store-only)

**Files:**
- Modify: `cas/profiles.py:233-254` (`download_rows`)
- Test: `tests/test_cas.py` — near existing `test_download_rows_golden_drives_defaults`

**Interfaces:**
- Consumes: nothing new.
- Produces: `download_rows(own_pkgs, store_pkgs, has_apk, has_config, always_install=None) -> (rows: dict[str,tuple[bool,bool]], cfg_disabled: set[str])`.

- [ ] **Step 1: Write the failing test**

```python
    def test_download_rows_always_install_auto_ticks_apk(self):
        from cas import profiles as P
        own = ["com.github.stenzek.duckstation", "com.cfgonly"]
        store = ["com.valvesoftware.steamlink"]     # store-only, not in the golden
        has_apk = {"com.github.stenzek.duckstation": True, "com.cfgonly": False}  # cfgonly: config-only capture, no bundled apk
        has_cfg = {"com.github.stenzek.duckstation": True, "com.cfgonly": True}
        ai = frozenset({"com.valvesoftware.steamlink", "com.cfgonly"})
        rows, disabled = P.download_rows(own, store, has_apk, has_cfg, always_install=ai)
        self.assertEqual(rows["com.valvesoftware.steamlink"], (True, False))     # store member auto-ticks APK
        self.assertIn("com.valvesoftware.steamlink", disabled)                   # no captured config -> disabled
        self.assertEqual(rows["com.cfgonly"], (True, True))                      # golden member, has_apk False -> APK forced on
        self.assertEqual(rows["com.github.stenzek.duckstation"], (True, True))   # non-member unchanged
        # regression: a store-only NON-member stays OFF
        rows2, _ = P.download_rows([], ["com.other"], {}, {}, always_install=ai)
        self.assertEqual(rows2["com.other"], (False, False))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest "tests/test_cas.py" -k always_install_auto_ticks -v`
Expected: FAIL with `TypeError: download_rows() got an unexpected keyword argument 'always_install'`.

- [ ] **Step 3: Write minimal implementation**

Replace `download_rows` in `cas/profiles.py` with:

```python
def download_rows(own_pkgs, store_pkgs, has_apk, has_config, always_install=None):
    """Golden-driven defaults for the Download app-pick modal. Returns (rows, cfg_disabled):
      * rows: ordered {pkg: (apk_default, cfg_default)}. A captured golden app defaults APK-ON only when the
        golden actually bundled an APK for it (has_apk[pkg]) — a config-only capture (APK sideloaded) defaults
        APK-OFF. Its Config defaults ON only when the golden captured config for it (has_config[pkg]) — an
        apk-only capture defaults Config-OFF. A store-only (managed) app — NOT in the golden — defaults
        APK-OFF (you opt in to push the store build) and has no captured config. FINALLY, any app in
        `always_install` (the global always-install set) has its APK default forced ON — for golden apps and
        for store-only apps alike — so operator-always-wanted apps install without re-ticking.
      * cfg_disabled: the set of pkgs whose Config checkbox the modal must DISABLE — you can't restore
        config that was never captured.
    Pure — the caller derives has_apk/has_config ({pkg: bool}) from the payload (Profile.has_captured_*)."""
    ai = always_install or frozenset()
    rows, cfg_disabled = {}, set()
    for pkg in own_pkgs:
        apk = bool(has_apk.get(pkg, True)) or (pkg in ai)   # always-install forces APK on
        cfg = bool(has_config.get(pkg))
        rows[pkg] = (apk, cfg)
        if not cfg:
            cfg_disabled.add(pkg)
    for pkg in store_pkgs:
        if pkg not in rows:
            rows[pkg] = ((pkg in ai), False)                # store-only member auto-ticks APK; else off
            cfg_disabled.add(pkg)
    return rows, cfg_disabled
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest "tests/test_cas.py" -k "always_install_auto_ticks or download_rows" -v`
Expected: PASS (new test + existing `download_rows` regressions).

- [ ] **Step 5: Commit**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(download): always_install auto-ticks APK for golden + store apps"
```

---

### Task 5: Wire the config set into the Save + Download pickers

**Files:**
- Modify: `cas/gui.py` — the `initial_capture_selection(...)` call in `_pick_capture` (~line 1277) and the `download_rows(...)` call in `_pick_downloads` (~line 1354)
- Test: none new (Tkinter modal is not unit-tested in this repo; behavior is covered by Tasks 2–4). Verification = full suite green + code inspection + on-bench smoke.

**Interfaces:**
- Consumes: `config.always_install_pkgs()` (Task 1); the new `always_install=` params (Tasks 3, 4). `config` is already imported in `cas/gui.py` (used at `config.apk_store_dir()`).
- Produces: nothing new.

- [ ] **Step 1: Wire `_pick_capture`**

Find (in `_pick_capture`):
```python
        sel = P.initial_capture_selection(device_apps, prof.capture_axes(), prof.capture_flags(),
                                          game_launcher=gl, home_launcher=hl)
```
Replace with:
```python
        sel = P.initial_capture_selection(device_apps, prof.capture_axes(), prof.capture_flags(),
                                          game_launcher=gl, home_launcher=hl,
                                          always_install=config.always_install_pkgs())
```

- [ ] **Step 2: Wire `_pick_downloads`**

Find (in `_pick_downloads`):
```python
            rows, cfg_disabled = P.download_rows(own_pkgs, store_pkgs, has_apk, has_cfg)
```
Replace with:
```python
            rows, cfg_disabled = P.download_rows(own_pkgs, store_pkgs, has_apk, has_cfg,
                                                 always_install=config.always_install_pkgs())
```

- [ ] **Step 3: Verify the whole suite is green**

Run: `python3 -m pytest tests/test_cas.py -q`
Expected: PASS, count = 208 baseline + 4 new tests (Tasks 1–4) = **212 passed**.

- [ ] **Step 4: Inspection check (no unit test for the GUI pass-through)**

Confirm `config` is imported at the top of `cas/gui.py` and both call sites now pass `always_install=config.always_install_pkgs()`. Run:
`grep -n "always_install=config.always_install_pkgs()" cas/gui.py`
Expected: exactly 2 hits (one in `_pick_capture`, one in `_pick_downloads`).

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py
git commit -m "feat(gui): pass always_install set into Save + Download pickers"
```

---

## Self-Review

**1. Spec coverage:**
- §3 data model / accessors → Task 1. ✅
- §4.1 `default_capture_selection` APK-on → Task 2. ✅
- §4.2 `initial_capture_selection` re-assert → Task 3. ✅
- §4.3 `download_rows` golden + store APK-on → Task 4. ✅
- §5 GUI wiring → Task 5. ✅
- §7 testing → each task ships its unit test; Task 5 states the full-suite count. ✅
- §6 optional informational log for a member available nowhere → **intentionally dropped** (spec marked it nice-to-have; YAGNI). No task. Noted here so the omission is explicit.
- §8 back-compat → every new param defaults to `None`; verified by the back-compat asserts in Tasks 2 & 3.

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows real assertions. The only "no new test" is Task 5 (GUI pass-through), which is explicitly justified and replaced with a grep + full-suite check.

**3. Type consistency:** `always_install` is a `frozenset[str]` everywhere; `always_install or frozenset()` guards `None` in all three helpers; `config.always_install_pkgs()` returns that frozenset; the getter/setter names match Task 1 → Task 5 usage. Row tuples are `(bool, bool)` throughout. Consistent.

**Task 1 test path:** the config-accessor test class is `TestConfig` (verified in `tests/test_cas.py`, alongside `test_config_library_wins_over_default`).
