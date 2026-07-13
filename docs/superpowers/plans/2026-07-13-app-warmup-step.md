# ③ Warm up chain step — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `warmup` step to the CAS action chain, between Download and Lock, that launches every app in the unit's profile manifest once (plus the two frontends, last) so each emulator initializes against its restored settings and indexes its games — removing the manual "open every emulator by hand" pass before Lock.

**Architecture:** Pure PC-side. A new `warmup()` / `warmup_all()` pair in `cas/provision.py` walks the profile's package list, launches each app with `monkey -p <pkg> -c android.intent.category.LAUNCHER 1`, waits for it to reach the foreground, dwells 3 seconds, and moves on — **never force-stopping**, so a backgrounded app keeps indexing. Two thin `Adb` helpers and two `config` getters support it. The GUI/CLI wiring follows the exact pattern of the existing Download step.

**Tech Stack:** Python 3 stdlib only (`unittest`, no third-party runtime deps). Tk for the GUI. `adb` shelled out through the existing `Adb` class.

**Spec:** `docs/superpowers/specs/2026-07-13-app-warmup-step-design.md`

## Global Constraints

- **Stdlib only.** No new runtime dependencies. Tests are `unittest`, run as `python -m unittest test_cas -v` from the `tests/` directory (this is what CI runs — `.github/workflows/build.yml:74`).
- **Pure ASCII in any file Windows consumes** (`.ps1` / `.bat` / `.inf`). This plan touches none of them, but never introduce an em-dash into one.
- **Workers never raise.** Every `*_all` worker returns a `(status, detail)` 2-tuple. Statuses in use: `ok / fail / error / skip / no-profile / cancelled`. A bare or short tuple makes `_report` crash.
- **Warm-up is additive.** An app that fails to launch produces a `[warn]` line and the pass continues. Only a dead device or a cancel makes the unit `fail`. A warm-up miss must never block a seal.
- **Never force-stop an app during the pass.** A `force-stop` after a 3s dwell would kill a scan that had just started — the exact bug this step exists to fix.
- **Lock renumbers to ④.** Warm up takes ③.
- Commit after every task. Push straight to `main` (no PR).

---

### Task 1: Config getters (`warmup_dwell_s`, `warmup_skip_pkgs`)

**Files:**
- Modify: `cas/config.py` (append after `auto_grant_shell()`, ~line 197)
- Test: `tests/test_cas.py` (add to `class TestConfig`, ~line 965)

**Interfaces:**
- Consumes: `load_config()` from `cas/config.py`.
- Produces:
  - `config.warmup_dwell_s() -> float` — seconds to dwell on each app. Default `3.0`.
  - `config.warmup_skip_pkgs() -> frozenset[str]` — packages warm-up never launches. Default `frozenset({"com.topjohnwu.magisk"})`. A stored `[]` means "skip nothing".

- [ ] **Step 1: Write the failing tests**

Add to `class TestConfig` in `tests/test_cas.py`:

```python
    def test_warmup_dwell_default_and_override(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            self.assertEqual(C.warmup_dwell_s(), 3.0)          # key absent -> default
            C.save_config({"warmup_dwell_s": 10})
            self.assertEqual(C.warmup_dwell_s(), 10.0)         # int is coerced to float
            C.save_config({"warmup_dwell_s": "bogus"})         # unparseable -> default, never crash
            self.assertEqual(C.warmup_dwell_s(), 3.0)
            C.save_config({"warmup_dwell_s": -5})              # negative is clamped to 0
            self.assertEqual(C.warmup_dwell_s(), 0.0)

    def test_warmup_skip_pkgs_default_override_and_empty(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            # key absent -> Magisk only (a host tool, never a shipped app)
            self.assertEqual(C.warmup_skip_pkgs(), frozenset({"com.topjohnwu.magisk"}))
            C.save_config({"warmup_skip_pkgs": ["com.foo", "com.bar"]})
            self.assertEqual(C.warmup_skip_pkgs(), frozenset({"com.foo", "com.bar"}))
            C.save_config({"warmup_skip_pkgs": []})            # stored [] -> skip NOTHING
            self.assertEqual(C.warmup_skip_pkgs(), frozenset())
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd tests && python -m unittest test_cas.TestConfig -v
```

Expected: FAIL — `AttributeError: module 'cas.config' has no attribute 'warmup_dwell_s'`.

- [ ] **Step 3: Write the implementation**

Append to `cas/config.py`, directly after `auto_grant_shell()`:

```python
_DEFAULT_WARMUP_DWELL_S = 3.0
_DEFAULT_WARMUP_SKIP = ("com.topjohnwu.magisk",)


def warmup_dwell_s():
    """Seconds the ③ Warm up step leaves each app in the foreground before launching the next (default
    3.0). Apps are never force-stopped, so this bounds how long we WATCH an app, not how long it gets to
    index — a backgrounded app keeps scanning. Raise it (cas-config.json "warmup_dwell_s") if a unit still
    ships with an unindexed emulator. A garbage/negative value falls back to the default / 0."""
    try:
        return max(0.0, float(load_config().get("warmup_dwell_s", _DEFAULT_WARMUP_DWELL_S)))
    except (TypeError, ValueError):
        return _DEFAULT_WARMUP_DWELL_S


def warmup_skip_pkgs():
    """Packages ③ Warm up never launches (frozenset). Default: Magisk ONLY — it's a host tool, not a
    shipped app, so opening it does nothing for the unit. EVERYTHING else warms (Companion, Steam Link,
    every emulator): at 3s an app, an unnecessary launch costs 3 seconds, and a blanket rule is cheaper to
    reason about than a curated list. A stored list overrides; a stored EMPTY list skips nothing."""
    v = load_config().get("warmup_skip_pkgs")
    if isinstance(v, list):
        return frozenset(str(p) for p in v)
    return frozenset(_DEFAULT_WARMUP_SKIP)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd tests && python -m unittest test_cas.TestConfig -v
```

Expected: PASS (all TestConfig tests, including the pre-existing ones).

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(warmup): config getters for the warm-up dwell + skip-list"
```

---

### Task 2: `Adb.launch()` and `Adb.go_home()`

**Files:**
- Modify: `cas/adb.py` (add both methods to `class Adb`, next to `reboot()` at ~line 311)
- Test: `tests/test_cas.py` (new `class TestAdbLaunch`, place it immediately before `class TestConfig`)

**Interfaces:**
- Consumes: `Adb.shell(cmd, timeout=None) -> (rc, out, err)`; the `FakeRunner` test double.
- Produces:
  - `Adb.launch(pkg) -> bool` — start `pkg`'s LAUNCHER activity via `monkey`. True when monkey reports the event was injected.
  - `Adb.go_home() -> bool` — send the unit back to its launcher.
  - `Adb.pkg_installed(pkg) -> bool` — True when `pm path <pkg>` resolves (the presence guard).

`monkey` is used rather than `am start -n <pkg>/<activity>` because it resolves the launcher activity itself — CAS does not know each emulator's entry-point class, and they differ per app.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cas.py`:

```python
class TestAdbLaunch(unittest.TestCase):
    """Adb.launch / go_home / pkg_installed — the three primitives ③ Warm up is built from."""

    def test_launch_uses_monkey_launcher_intent(self):
        r = FakeRunner()
        a = Adb(runner=r)
        self.assertTrue(a.launch("org.ppsspp.ppsspp"))
        cmd = r.calls[-1][-1]
        self.assertIn("monkey -p org.ppsspp.ppsspp", cmd)
        self.assertIn("android.intent.category.LAUNCHER", cmd)

    def test_launch_false_when_monkey_finds_no_activity(self):
        class NoActivity(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                self.calls.append(list(args))
                if "monkey" in args[-1]:
                    # real monkey text when a package has no LAUNCHER activity: rc 0, but nothing injected
                    return 0, "** No activities found to run, monkey aborted.\n", ""
                return 0, "", ""
        a = Adb(runner=NoActivity())
        self.assertFalse(a.launch("com.no.ui.app"))

    def test_go_home_sends_home_intent(self):
        r = FakeRunner()
        self.assertTrue(Adb(runner=r).go_home())
        self.assertIn("android.intent.category.HOME", r.calls[-1][-1])

    def test_pkg_installed_reflects_pm_path(self):
        class PmPath(FakeRunner):
            def __init__(self, present):
                super().__init__()
                self.present = present
            def __call__(self, args, input_text=None, timeout=900):
                self.calls.append(list(args))
                if args[-1].startswith("pm path "):
                    return (0, "package:/data/app/base.apk\n", "") if self.present else (1, "", "")
                return 0, "", ""
        self.assertTrue(Adb(runner=PmPath(True)).pkg_installed("com.foo"))
        self.assertFalse(Adb(runner=PmPath(False)).pkg_installed("com.foo"))
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd tests && python -m unittest test_cas.TestAdbLaunch -v
```

Expected: FAIL — `AttributeError: 'Adb' object has no attribute 'launch'`.

- [ ] **Step 3: Write the implementation**

Add to `class Adb` in `cas/adb.py`, immediately after `reboot()`:

```python
    def pkg_installed(self, pkg):
        """True when `pkg` is present on the device (`pm path` resolves it). The presence guard for
        ③ Warm up: launching a package the unit doesn't carry is a no-op we'd rather log as a skip."""
        rc, out, _ = self.shell(f"pm path {pkg}", timeout=30)
        return rc == 0 and "package:" in out

    def launch(self, pkg):
        """Start `pkg`'s LAUNCHER activity. True when monkey actually injected the start event.

        `monkey` (not `am start -n pkg/activity`) because CAS does not know each emulator's entry-point
        class and they all differ — monkey resolves the LAUNCHER activity itself from the package id.
        Note monkey exits 0 even when a package has NO launcher activity, printing 'No activities found';
        that string is the real failure signal, so we match on it rather than on rc."""
        rc, out, err = self.shell(
            f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1", timeout=60)
        return rc == 0 and "No activities found" not in (out or "") + (err or "")

    def go_home(self):
        """Send the unit back to its launcher (ends a warm-up pass so a unit is never left sitting inside
        an emulator). Backgrounds the foreground app WITHOUT killing it — it keeps indexing."""
        return self.shell(
            "am start -a android.intent.action.MAIN -c android.intent.category.HOME", timeout=60)[0] == 0
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd tests && python -m unittest test_cas.TestAdbLaunch -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add cas/adb.py tests/test_cas.py
git commit -m "feat(warmup): Adb.launch / go_home / pkg_installed primitives"
```

---

### Task 3: `warmup()` + `warmup_all()` in provision.py

**Files:**
- Modify: `cas/provision.py` (add `WARMUP_FRONTENDS`, `_warmup_order()`, `warmup()`, `warmup_all()` — place them after `provision_all()` ends, ~line 738, before `_append_history`)
- Test: `tests/test_cas.py` (new `class TestWarmup`, place it immediately after `class TestProvision`)

**Interfaces:**
- Consumes: `Adb.launch/go_home/pkg_installed` (Task 2); `config.warmup_dwell_s()` / `config.warmup_skip_pkgs()` (Task 1); `uiauto.foreground(adb)`; `P.Profile.pkgs()`; `_each_device(devices, worker, parallel)`.
- Produces:
  - `provision.WARMUP_FRONTENDS = ("org.es_de.frontend", "com.handheld.launcher")`
  - `provision.WARMUP_FOREGROUND_TIMEOUT = 15`
  - `provision._warmup_order(pkgs, skip) -> list[str]` — **pure**; manifest apps (minus skip, minus frontends) then the frontends, deduped, order-preserving.
  - `provision.warmup(adb, profile, log=print, dwell=None, skip=None) -> bool`
  - `provision.warmup_all(make_adb, devices, root="profiles", log=print, profile=None, profile_map=None, parallel=True, dwell=None, skip=None) -> {serial: (status, detail)}`

`dwell` / `skip` default to `None` on both, meaning "read `cas-config.json`". They are explicit parameters so tests can pass `dwell=0` and never actually sleep.

**Why the frontends go last:** they are the thing being warmed *for* — each must open *after* every emulator has initialized, so it indexes against a warm set. They are an explicit constant rather than manifest-derived because `com.handheld.launcher` is a **system** app on MANGMI units; `user_pkgs()` (`lib-root.sh:104`) lists only `-3` packages, so the launcher never appears in a golden's manifest.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cas.py`:

```python
class TestWarmup(unittest.TestCase):
    """③ Warm up — launch every manifest app once (frontends last), never force-stop, never block a seal."""

    class WarmRunner(FakeRunner):
        """FakeRunner that records launches/foreground polls. `absent` = pkgs `pm path` won't resolve;
        `never_fg` = pkgs that launch but never become the resumed activity."""

        def __init__(self, absent=(), never_fg=(), **kw):
            super().__init__(**kw)
            self.absent, self.never_fg = set(absent), set(never_fg)
            self.launched = []          # pkgs launched, in order
            self.fg = ""                # the pkg currently "resumed" on the fake device

        def __call__(self, args, input_text=None, timeout=900):
            if "shell" in args:
                tail = args[-1]
                if tail.startswith("pm path "):
                    pkg = tail.split()[-1]
                    self.calls.append(list(args))
                    return (1, "", "") if pkg in self.absent else (0, f"package:/data/app/{pkg}.apk\n", "")
                if tail.startswith("monkey -p "):
                    pkg = tail.split()[2]
                    self.launched.append(pkg)
                    self.fg = "" if pkg in self.never_fg else pkg
                    self.calls.append(list(args))
                    return 0, "Events injected: 1\n", ""
                if "topResumedActivity" in tail:
                    self.calls.append(list(args))
                    return 0, (f"topResumedActivity=ActivityRecord{{u0 {self.fg}/.Main}}\n"
                               if self.fg else "topResumedActivity=null\n"), ""
            return super().__call__(args, input_text=input_text, timeout=timeout)

    def _warm(self, prof, runner, **kw):
        """Run warmup() with a zero dwell so tests never actually sleep."""
        logs = []
        ok = PV.warmup(Adb(runner=runner), prof, log=logs.append, dwell=0, **kw)
        return ok, logs

    def test_launches_every_manifest_app_frontends_last(self):
        with tempfile.TemporaryDirectory() as t:
            # ES-DE is in the manifest; com.handheld.launcher is a SYSTEM app (never in a manifest) but
            # is present on the unit — both must warm, and both must come AFTER the emulators.
            prof = make_profile(t, apps=["org.es_de.frontend", "org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner()
            ok, _ = self._warm(prof, r)
            self.assertTrue(ok)
            self.assertEqual(r.launched, ["org.ppsspp.ppsspp", "org.citra.emu",
                                          "org.es_de.frontend", "com.handheld.launcher"])

    def test_never_force_stops(self):
        """A force-stop after a 3s dwell would kill a scan mid-flight — the bug this step exists to fix."""
        with tempfile.TemporaryDirectory() as t:
            r = self.WarmRunner()
            self._warm(make_profile(t, apps=["org.ppsspp.ppsspp"]), r)
            self.assertNotIn("force-stop", "\n".join(" ".join(c) for c in r.calls))

    def test_skip_list_pkg_is_never_launched(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["com.topjohnwu.magisk", "org.ppsspp.ppsspp"])
            r = self.WarmRunner()
            self._warm(prof, r, skip=frozenset({"com.topjohnwu.magisk"}))
            self.assertNotIn("com.topjohnwu.magisk", r.launched)
            self.assertIn("org.ppsspp.ppsspp", r.launched)

    def test_absent_pkg_is_skipped_not_launched(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner(absent={"org.citra.emu", "com.handheld.launcher", "org.es_de.frontend"})
            ok, logs = self._warm(prof, r)
            self.assertTrue(ok)                                   # an absent app is a skip, not a failure
            self.assertEqual(r.launched, ["org.ppsspp.ppsspp"])
            self.assertIn("org.citra.emu", "\n".join(logs))       # …and it's reported

    def test_app_that_never_foregrounds_warns_and_continues(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner(never_fg={"org.ppsspp.ppsspp"})
            ok, logs = self._warm(prof, r)
            self.assertTrue(ok)                                   # additive: a miss never blocks the seal
            joined = "\n".join(logs)
            self.assertIn("[warn]", joined)
            self.assertIn("org.ppsspp.ppsspp", joined)
            self.assertIn("org.citra.emu", r.launched)            # the pass carried on to the next app

    def test_pass_ends_at_home(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.WarmRunner()
            self._warm(make_profile(t, apps=["org.ppsspp.ppsspp"]), r)
            self.assertIn("android.intent.category.HOME", " ".join(r.calls[-1]))

    def test_cancel_stops_the_pass_between_apps(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner()
            ev = threading.Event()
            ev.set()                                              # already cancelled -> launch nothing
            ok = PV.warmup(Adb(runner=r, cancel=ev), prof, log=lambda m: None, dwell=0)
            self.assertFalse(ok)
            self.assertEqual(r.launched, [])

    def test_warmup_all_reports_ok_per_device(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            res = PV.warmup_all(lambda s: Adb(serial=s, runner=self.WarmRunner()),
                                [("S1", "device"), ("S2", "device")],
                                log=lambda m: None, profile=prof, parallel=False, dwell=0)
            self.assertEqual({k: v[0] for k, v in res.items()}, {"S1": "ok", "S2": "ok"})
```

`tests/test_cas.py` does **not** import `threading` today. Add it to the import block at the top of the file (after `import tempfile`):

```python
import threading
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd tests && python -m unittest test_cas.TestWarmup -v
```

Expected: FAIL — `AttributeError: module 'cas.provision' has no attribute 'warmup'`.

- [ ] **Step 3: Write the implementation**

Add to `cas/provision.py` after `provision_all()` (before `_append_history`).

`uiauto` is imported **inside** `warmup()`, not at module level — that matches how `grant_shell_root()` already does it (`provision.py:1177`, `from . import uiauto`). Follow the existing convention; do not hoist it.

```python
# ③ WARM UP — launch every app the unit just received, once, so it initializes against its restored
# settings and indexes its games. Without this pass an emulator that has NEVER been opened won't launch a
# game from the frontend, and every unit needs a manual "open each emulator" pass before Lock.
WARMUP_FRONTENDS = ("org.es_de.frontend", "com.handheld.launcher")   # warmed LAST — see _warmup_order
WARMUP_FOREGROUND_TIMEOUT = 15    # seconds to wait for a launched app to become the resumed activity


def _warmup_order(pkgs, skip):
    """The launch order for one unit: manifest apps first, then the frontends. PURE (no adb).

    The frontends go LAST because they are what the warm-up is FOR — each must open after every emulator
    has initialized so it indexes against a warm set. They're an explicit constant, not manifest-derived:
    com.handheld.launcher is a SYSTEM app on MANGMI units, and user_pkgs() lists only `-3` packages, so it
    never appears in a golden's manifest. A frontend already in the manifest (ES-DE usually is) is launched
    ONCE, in the frontend slot at the end — not twice."""
    skip = set(skip or ())
    apps = [p for p in pkgs if p not in skip and p not in WARMUP_FRONTENDS]
    return apps + [f for f in WARMUP_FRONTENDS if f not in skip]


def warmup(adb, profile, log=print, dwell=None, skip=None):
    """Warm up one unit: launch each of its apps once, in _warmup_order, and leave them RUNNING.

    Apps are never force-stopped. Launching app B simply backgrounds app A, where it keeps indexing —
    a force-stop after the (short) dwell would kill a scan that had just started, which is the very bug
    this step exists to fix. The Lock reboot cleans the unit up; a standalone warm-up leaves the apps
    running, which is harmless.

    ADDITIVE, like scrub.sh: an app that won't launch or never reaches the foreground is a [warn] naming
    the package (and what WAS foreground, so the log localizes it), never a failure — a warm-up miss must
    not block a seal. Returns False only on cancel. Requires no root."""
    from . import config as _cfg
    from . import uiauto                 # function-local, matching grant_shell_root (provision.py:1177)
    dwell = _cfg.warmup_dwell_s() if dwell is None else dwell
    skip = _cfg.warmup_skip_pkgs() if skip is None else skip
    cancelled = lambda: adb.cancel is not None and adb.cancel.is_set()

    order = _warmup_order(profile.pkgs(), skip)
    log(f"==> warm up: {len(order)} app(s) to open, {dwell:g}s each (they keep indexing in the background)")
    warmed = 0
    for pkg in order:
        if cancelled():
            log("cancelled — stopping the warm-up pass")
            return False
        if not adb.pkg_installed(pkg):
            log(f"   skip {pkg} (not installed on this unit)")
            continue
        if not adb.launch(pkg):
            log(f" [warn] {pkg} would not launch (no launcher activity?) — skipping it")
            continue
        deadline = time.monotonic() + WARMUP_FOREGROUND_TIMEOUT
        while time.monotonic() < deadline:
            if pkg in uiauto.foreground(adb):
                break
            if cancelled():
                log("cancelled — stopping the warm-up pass")
                return False
            time.sleep(1)
        else:
            log(f" [warn] {pkg} never reached the foreground in {WARMUP_FOREGROUND_TIMEOUT}s "
                f"(foreground was: {uiauto.foreground(adb) or 'nothing'}) — moving on")
            continue
        warmed += 1
        log(f" [ok]   {pkg} is up ({warmed}/{len(order)})")
        time.sleep(dwell)
    adb.go_home()                       # never leave a unit sitting inside an emulator
    log(f" [ok]   warm-up done — {warmed} app(s) opened")
    return True


def warmup_all(make_adb, devices, root="profiles", log=print, profile=None, profile_map=None,
               parallel=True, dwell=None, skip=None):
    """Batch WARM UP: open every unit's apps once, in PARALLEL by default. Profile resolution matches
    provision_all: profile_map[serial] > `profile` > auto-match by model. Returns {serial: (status,
    detail)}; failures isolated. A unit only FAILS on a dead device or a cancel — a warm-up miss is a
    [warn] inside warmup(), never a failure, so a seal is never blocked by it."""
    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        try:
            adb = make_adb(serial)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", "")
            if profile_map is not None and serial in profile_map:
                prof = profile_map[serial]
                if prof is None:
                    log(f"[{serial}] no profile assigned — skip")
                    return ("no-profile", "")
            elif profile is not None:
                prof = profile
            else:
                model = adb.getprop("ro.product.model")
                prof = P.match_profile(model, root)
                if not prof:
                    log(f"[{serial}] no profile matches '{model}'")
                    return ("no-profile", model)
            ok = warmup(adb, prof, log=lambda m, s=serial: log(f"[{s}] {m}"), dwell=dwell, skip=skip)
            if ok:
                return ("ok", prof.name)
            return ("cancelled", prof.name)     # warmup() returns False ONLY on cancel
        except Exception as e:                  # isolate: one device fault must not abort the whole batch
            log(f"[{serial}] ERROR: {e}")
            return ("error", str(e))
    return _each_device(devices, worker, parallel)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd tests && python -m unittest test_cas.TestWarmup -v
```

Expected: PASS (8 tests).

- [ ] **Step 5: Run the whole suite — nothing else may regress**

```bash
cd tests && python -m unittest test_cas -v 2>&1 | tail -5
```

Expected: `OK` (the pre-existing count + 12 new tests from Tasks 1–3).

- [ ] **Step 6: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(warmup): warmup() + warmup_all() — open each app once, frontends last, never force-stop"
```

---

### Task 4: Wire the step into the GUI chain

**Files:**
- Modify: `cas/gui.py` — the checkbox loop (~line 657), `_on_chain_tick` (~line 1405), `_CHAIN_ORDER` (~line 1467), `_stage` (~line 1865), the `names` dict in `_run_chain` (~line 1915)
- Modify: `cas/warnings.py:16` — `ACTIONS`
- Test: `tests/test_cas.py` — `class TestRunChain` (~line 2939) and `class TestResolveChain` (~line 3068)

**Interfaces:**
- Consumes: `PV.warmup_all(...)` (Task 3).
- Produces: `"warmup"` as a valid chain step key everywhere a step key is accepted — `App._CHAIN_ORDER`, `App._stage`, `warnings.ACTIONS`.

**The Download reboot needs no new code.** `_run_chain_core` already sets `wait_boot=True` on the Download stage whenever any step follows it (`gui.py:1904` — `wb = step == "download" and bool(steps[i + 1:])`). Inserting `warmup` after `download` turns that on automatically. One of the tests below pins that behavior so it can't silently regress.

- [ ] **Step 1: Write the failing tests**

Add to `class TestRunChain` in `tests/test_cas.py`:

```python
    def test_warmup_runs_between_download_and_lock(self):
        app = self._app()
        survivors = app._run_chain_core(["root", "download", "warmup", "lock"], ["S1", "S2"], None)
        # S1 fails root and is dropped; warm-up sits between Download and Lock. Download gets
        # wait_boot=True because warm-up follows it — a warm-up must never touch a rebooting unit.
        self.assertEqual(app._stage_calls,
                         [("root", ["S1", "S2"], False), ("download", ["S2"], True),
                          ("warmup", ["S2"], False), ("lock", ["S2"], False)])
        self.assertEqual(survivors, ["S2"])

    def test_download_waits_for_boot_when_only_warmup_follows(self):
        """Regression guard: wait_boot keys off 'any step follows', not off Lock specifically."""
        app = self._app()
        app._run_chain_core(["download", "warmup"], ["S2"], None)
        self.assertEqual(app._stage_calls, [("download", ["S2"], True), ("warmup", ["S2"], False)])
```

Add to `class TestResolveChain`:

```python
    def test_orders_warmup_between_download_and_lock(self):
        self.assertEqual(self._r(lock=True, warmup=True, download=True, root=True),
                         (["root", "download", "warmup", "lock"], None))

    def test_warmup_alone_is_valid(self):
        self.assertEqual(self._r(warmup=True), (["warmup"], None))

    def test_save_excludes_warmup(self):
        steps, err = self._r(save=True, warmup=True)
        self.assertEqual(steps, [])
        self.assertIn("Save", err)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd tests && python -m unittest test_cas.TestRunChain test_cas.TestResolveChain -v
```

Expected: FAIL — `test_orders_warmup_between_download_and_lock` returns `(["root", "download", "lock"], None)` (warmup is dropped: it isn't in `_CHAIN_ORDER`), and `test_warmup_alone_is_valid` returns `([], "Tick at least one action to run.")`.

- [ ] **Step 3: Write the implementation**

**3a.** `cas/gui.py` — add the checkbox tuple and renumber Lock. Replace the two lines in the `for key, label, tip in (...)` loop:

```python
            ("download", "② Download", "Install each device's assigned profile (apps + saves/BIOS/settings/grants/homescreen)."),
            ("warmup", "③ Warm up", "Open every installed app once (frontends last) so each emulator initializes "
                                    "against its restored settings and indexes its games. Without it, a never-opened "
                                    "emulator won't launch a game from the frontend. Apps are left running to finish "
                                    "indexing; nothing is force-stopped."),
            ("lock", "④ Lock", "Retail-seal verified unit(s): hide Dev options, un-root, disable USB debugging."),
```

**3b.** `cas/gui.py` — `_CHAIN_ORDER`:

```python
    _CHAIN_ORDER = ("root", "save", "download", "warmup", "lock")
```

**3c.** `cas/gui.py` — `_on_chain_tick`. Warm-up is a unit action, mutually exclusive with Save like Download/Lock. Replace the body's first four lines:

```python
        save_on = self.chain_vars["save"].get()
        unit_on = any(self.chain_vars[k].get() for k in ("download", "warmup", "lock"))
        for k in ("download", "warmup", "lock"):
```

Update the docstring's first line to `"""Save ⟂ Download/Warm up/Lock: ..."""`.

**3d.** `cas/gui.py` — `_stage`, add a branch before the final `raise`:

```python
        if step == "warmup":
            return PV.warmup_all(mk_adb, devs, root=self.profiles_root, log=self.log, profile_map=pm,
                                 parallel=True)
```

**3e.** `cas/gui.py` — the `names` dict in `_run_chain`:

```python
        names = {"root": "Root", "save": "Save", "download": "Download", "warmup": "Warm up", "lock": "Lock"}
```

**3f.** `cas/warnings.py:16`:

```python
ACTIONS = ("root", "save", "download", "warmup", "lock")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd tests && python -m unittest test_cas.TestRunChain test_cas.TestResolveChain -v
```

Expected: PASS.

- [ ] **Step 5: Run the whole suite**

```bash
cd tests && python -m unittest test_cas -v 2>&1 | tail -5
```

Expected: `OK`. `warnings.py`'s `_BLOCK_ALL` is derived from `ACTIONS`, so every catalog entry that blocks everything now blocks warm-up too — that is correct (a unit that can't be talked to can't be warmed either) and no existing warning test should change.

- [ ] **Step 6: Commit**

```bash
git add cas/gui.py cas/warnings.py tests/test_cas.py
git commit -m "feat(warmup): ③ Warm up chain step in the GUI (Lock renumbers to ④)"
```

---

### Task 5: CLI subcommands

**Files:**
- Modify: `cas/cli.py` — the module docstring (~line 1), the subparsers (~line 44), the dispatch (~line 66 and ~line 95)
- Test: `tests/test_cas.py` — new `class TestCliWarmup`, place it immediately after `class TestWarmup`

**Interfaces:**
- Consumes: `PV.warmup(adb, profile, log=print)` and `PV.warmup_all(...)` (Task 3); `_resolve_profile(adb, name, proot)` (already in `cli.py`).
- Produces: `cas.cli warmup [--profile NAME] [--serial S]` and `cas.cli warmup-all`. Exit `0` on success, `1` otherwise — matching `provision` / `provision-all`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cas.py`:

```python
class TestCliWarmup(unittest.TestCase):
    """`cas.cli warmup` resolves the profile and calls PV.warmup — the CLI mirror of the ③ checkbox."""

    def test_warmup_calls_provision_warmup_and_exits_zero(self):
        from unittest.mock import patch
        import cas.cli as CLI
        import cas.provision as PV_mod
        seen = {}
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, name="odin2mini")
            def fake_warmup(adb, profile, log=print, **kw):
                seen["profile"] = profile.name
                return True
            with patch.object(PV_mod, "warmup", fake_warmup):
                rc = CLI.main(["--library", t, "--adb", "adb", "warmup", "--profile", "odin2mini"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["profile"], "odin2mini")

    def test_warmup_unknown_profile_exits_one(self):
        import cas.cli as CLI
        with tempfile.TemporaryDirectory() as t:
            rc = CLI.main(["--library", t, "--adb", "adb", "warmup", "--profile", "nope"])
        self.assertEqual(rc, 1)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd tests && python -m unittest test_cas.TestCliWarmup -v
```

Expected: FAIL — argparse exits with `invalid choice: 'warmup'` (raises `SystemExit`).

- [ ] **Step 3: Write the implementation**

**3a.** `cas/cli.py` — add to the module docstring's usage block, after the `provision-all` line:

```
  python -m cas.cli warmup         [--profile NAME] [--serial S]
  python -m cas.cli warmup-all
```

**3b.** `cas/cli.py` — add the subparsers immediately after the `provision-all` parser:

```python
    wp = sub.add_parser("warmup", help="open every app once so emulators index their games (③)")
    wp.add_argument("--profile")
    sub.add_parser("warmup-all", help="warm up every connected device (auto-matched)")
```

**3c.** `cas/cli.py` — add the batch dispatch immediately after the `provision-all` block:

```python
    if a.cmd == "warmup-all":
        res = PV.warmup_all(lambda s: Adb(serial=s, adb=a.adb), list_devices(adb=a.adb), root=proot)
        print("warmup-all:", ", ".join(f"{k}={v[0]}" for k, v in res.items()))
        return 0 if all(v[0] in ("ok", "skip") for v in res.values()) else 1
```

**3d.** `cas/cli.py` — add the single-device dispatch immediately after the `if a.cmd == "provision":` block:

```python
    if a.cmd == "warmup":
        prof = _resolve_profile(adb, a.profile, proot)
        if not prof:
            print("no matching profile — pass --profile NAME"); return 1
        return 0 if PV.warmup(adb, prof) else 1
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd tests && python -m unittest test_cas.TestCliWarmup -v
```

Expected: PASS (2 tests).

- [ ] **Step 5: Run the whole suite plus the shell tests**

```bash
cd tests && python -m unittest test_cas -v 2>&1 | tail -5
cd "$(git rev-parse --show-toplevel)/tests" && for f in test_*.sh; do sh "$f" >/dev/null || echo "FAIL $f"; done; echo "shell tests done"
```

Expected: `OK` from unittest; no `FAIL` lines from the shell tests (this step touches no shell script, so they must be untouched).

- [ ] **Step 6: Commit and push**

```bash
git add cas/cli.py tests/test_cas.py
git commit -m "feat(warmup): cas.cli warmup / warmup-all"
git push origin main
```

---

## Bench verification (after the push)

Not a code task — the on-device gate. On a real unit:

1. `git pull` on the Windows bench, launch CAS.
2. Tick **② Download** + **③ Warm up** (leave Lock off for the first run) → ▶ Run.
3. Watch the log: every app should report `[ok] <pkg> is up (n/N)`. Any `[warn]` line names an app that needs attention — that line is the diagnostic.
4. When it settles, open the frontend on the unit and launch a game **for each emulator**. This is the actual acceptance test: a game that would previously have refused to launch on a fresh unit must now launch.
5. Then run **④ Lock** and re-verify a game still launches after the seal (confirming `scrub_traces` didn't take the warm-up state with it).

If an emulator still won't launch a game, raise `warmup_dwell_s` in `cas-config.json` and re-run — the app was almost certainly still indexing when the pass moved on.
