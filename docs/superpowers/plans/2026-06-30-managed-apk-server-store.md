# Managed APK Server Store — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a central, NAS-hosted APK store where one *current* version of each package deploys, so an operator can attach an APK to a config that lacks one (e.g. cocoon), bump versions in one place, and host kit + third-party APKs on the server.

**Architecture:** A new `library_root()/_apks/<pkg>/` store (resolved by `config.apk_store_dir()`, mirroring `firmware_dir()`). `profiles.py` gains pure store accessors + a resolver (`payload → store → bundle`). `provision.py` splits a manifest's apps into payload apps (captured, unchanged on-device flow) and *managed* apps (installed PC-side via `adb install` from the store); kit APKs (Magisk, Companion) resolve store-first with a bundle fallback. The GUI gets a "Managed APKs" manager and surfaces store apps as tickable rows in the Download picker. The on-device `lib-root.sh`/`restore.sh` are **unchanged**.

**Tech Stack:** Python 3.8+ stdlib only (pathlib, shutil, zipfile, tempfile, json), Tkinter (existing GUI), `unittest`.

**Spec:** `docs/superpowers/specs/2026-06-30-managed-apk-server-store-design.md`

## Global Constraints

- **Stdlib only** — no new third-party dependencies.
- **`provision/root/lib-root.sh` and `restore.sh` MUST stay unchanged** — managed apps install PC-side; the device manifest is filtered to payload apps so the device engine never sees a managed app.
- **Never hard-delete** — replacing/removing a store build retains prior bytes (mirror `archive_profile` / soft-delete convention).
- **Store layout:** `config.apk_store_dir()` → `library_root()/_apks/<pkg>/` containing `meta` (`current=<label>`), `<label>.apk` (single) or `<label>/` (split set), and `_archive/` for displaced bytes.
- **Kit package ids:** Magisk `com.topjohnwu.magisk`; Companion `com.gamecove.gamecove_companion` (`provision.COMPANION_PKG`).
- **Version label** = operator-supplied, default = the source APK's filename stem. No APK parsing.
- **Run tests** from the project root with **module paths** (this env is Python 3.14 and the repo path contains `[07]`, so `unittest discover` errors with "Start directory is not importable" — do NOT use `discover`). Full suite: `python3 -m unittest tests.test_cas tests.test_firmware tests.test_warnings`. Baseline before this feature: **220 tests, OK**. Single test/class: `python3 -m unittest tests.test_cas.TestConfig.test_name -v`. Filtered: `python3 -m unittest tests.test_cas -k pattern -v`.
- **Commit style:** Conventional Commits (`feat(profiles): …`), ending each message with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- Follow the house docstring style: explain **why**, not just what.

---

### Task 1: `config.apk_store_dir` / `set_apk_store`

**Files:**
- Modify: `cas/config.py` (add two functions after `set_firmware_dir`, ~line 139)
- Test: `tests/test_cas.py` (new tests in `class TestConfig`)

**Interfaces:**
- Produces: `config.apk_store_dir() -> pathlib.Path`; `config.set_apk_store(path) -> str|None`

- [ ] **Step 1: Write the failing tests**

Add to `class TestConfig` in `tests/test_cas.py`:

```python
    def test_apk_store_defaults_under_library(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")
            os.environ["CAS_PROFILES"] = str(pathlib.Path(t) / "lib")
            self.assertEqual(C.apk_store_dir(), pathlib.Path(t) / "lib" / "_apks")

    def test_apk_store_override_honored_only_if_exists(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ["CAS_PROFILES"] = str(pathlib.Path(t) / "lib")
            store = pathlib.Path(t) / "store"; store.mkdir()
            C.set_apk_store(str(store))
            self.assertEqual(C.apk_store_dir(), store)                       # exists -> honored
            C.set_apk_store(str(pathlib.Path(t) / "gone"))                   # nonexistent override
            self.assertEqual(C.apk_store_dir(), pathlib.Path(t) / "lib" / "_apks")  # ignored
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas.TestConfig.test_apk_store_defaults_under_library -v`
Expected: FAIL with `AttributeError: module 'cas.config' has no attribute 'apk_store_dir'`

- [ ] **Step 3: Implement**

Add to `cas/config.py` immediately after `set_firmware_dir`:

```python
def apk_store_dir():
    """The managed-APK server store directory. An explicit 'apk_store' override is honored ONLY if its path
    currently exists (so a stale NAS-pinned override on an offline bench is ignored and the store follows the
    discovered library); otherwise library_root()/_apks. Mirrors firmware_dir's rule, so 'on the server by
    default' needs no extra wiring."""
    d = load_config().get("apk_store")
    if d:
        p = pathlib.Path(d)
        try:
            if p.is_dir():
                return p
        except OSError:
            pass
    return library_root() / "_apks"


def set_apk_store(path):
    """Persist (path) or clear (falsy) the managed-APK store directory."""
    cfg = load_config()
    if path:
        cfg["apk_store"] = str(path)
    else:
        cfg.pop("apk_store", None)
    save_config(cfg)
    return load_config().get("apk_store")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cas.TestConfig -v`
Expected: PASS (all TestConfig tests)

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(config): apk_store_dir/set_apk_store (library_root()/_apks, override-if-exists)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: profiles — store read accessors

**Files:**
- Modify: `cas/profiles.py` (new section after `INTERNAL_FOR`/`internal_for`, ~line 31)
- Test: `tests/test_cas.py` (new `_seed_store` helper + tests in the profiles test class — the class containing `test_manifest_parse`, near line 247)

**Interfaces:**
- Consumes: `_read_meta` (existing in profiles.py)
- Produces:
  - `apk_store_pkg_dir(store_dir, pkg) -> pathlib.Path`
  - `store_current_label(store_dir, pkg) -> str|None`
  - `store_apk_files(store_dir, pkg) -> list[pathlib.Path]`
  - `list_store_apks(store_dir) -> list[dict]` (keys: `pkg`, `label`, `nfiles`, `bytes`)

- [ ] **Step 1: Write the failing tests**

Add this module-level helper to `tests/test_cas.py` next to `_mk` (after line 114):

```python
def _seed_store(store, pkg, label, content="apk"):
    """Write a single-APK store entry directly (no put_store_apk) so read-accessor tests are self-contained."""
    d = pathlib.Path(store) / pkg
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{label}.apk").write_text(content)
    (d / "meta").write_text(f"current={label}\n")
```

Add to the profiles test class (the one with `test_manifest_parse`):

```python
    def test_store_read_accessors(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.cocoon.app", "cocoon-1.5.0", content="bytes")
            self.assertEqual(P.store_current_label(store, "org.cocoon.app"), "cocoon-1.5.0")
            files = P.store_apk_files(store, "org.cocoon.app")
            self.assertEqual([f.name for f in files], ["cocoon-1.5.0.apk"])
            self.assertEqual(P.list_store_apks(store),
                             [{"pkg": "org.cocoon.app", "label": "cocoon-1.5.0",
                               "nfiles": 1, "bytes": len("bytes")}])

    def test_store_split_label_returns_all_apks(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"; d = store / "com.split" / "v2"; d.mkdir(parents=True)
            (d / "base.apk").write_text("a"); (d / "split_config.apk").write_text("b")
            (store / "com.split" / "meta").write_text("current=v2\n")
            self.assertEqual(sorted(f.name for f in P.store_apk_files(store, "com.split")),
                             ["base.apk", "split_config.apk"])

    def test_store_empty_and_missing(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            self.assertEqual(P.list_store_apks(store), [])                 # missing dir -> []
            self.assertIsNone(P.store_current_label(store, "nope"))
            self.assertEqual(P.store_apk_files(store, "nope"), [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas -k store_read -v`
Expected: FAIL with `AttributeError: module 'cas.profiles' has no attribute 'store_current_label'`

- [ ] **Step 3: Implement**

Add to `cas/profiles.py` after `internal_for` (~line 31), before `_dir_bytes`:

```python
# --- managed-APK server store -------------------------------------------------------------------
# A central, library-side APK store (config.apk_store_dir(), default library_root()/_apks): ONE current
# version of each package deploys, and every config that lists the app (apk axis) installs it. Captured
# golden APKs (golden_root_payload/<pkg>/apk) are SEPARATE and unchanged — the resolver prefers them.
def apk_store_pkg_dir(store_dir, pkg):
    """The store directory for one package: <store_dir>/<pkg>."""
    return pathlib.Path(store_dir) / pkg


def store_current_label(store_dir, pkg):
    """The 'current=' label from <store>/<pkg>/meta, or None when the package has no current build (never
    added, or soft-removed)."""
    return _read_meta(apk_store_pkg_dir(store_dir, pkg) / "meta").get("current") or None


def store_apk_files(store_dir, pkg):
    """APK file(s) for the package's CURRENT label: [<label>.apk] for a single build, or every *.apk under
    <label>/ (sorted) for a split build. [] if there's no current label or its file(s) are missing."""
    label = store_current_label(store_dir, pkg)
    if not label:
        return []
    d = apk_store_pkg_dir(store_dir, pkg)
    single = d / f"{label}.apk"
    if single.is_file():
        return [single]
    split = d / label
    if split.is_dir():
        return sorted(split.glob("*.apk"))
    return []


def list_store_apks(store_dir):
    """Every package in the store WITH a current build: [{'pkg','label','nfiles','bytes'}], sorted by pkg.
    Soft-removed packages (no current) and bookkeeping dirs (names starting with '_') are omitted."""
    root = pathlib.Path(store_dir)
    out = []
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        entries = []
    for d in entries:
        if not d.is_dir() or d.name.startswith("_"):
            continue
        files = store_apk_files(store_dir, d.name)
        if not files:
            continue
        out.append({"pkg": d.name, "label": store_current_label(store_dir, d.name),
                    "nfiles": len(files), "bytes": sum(f.stat().st_size for f in files)})
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cas -k store -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(profiles): APK-store read accessors (current label, files, listing)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: profiles — `put_store_apk` / `remove_store_apk`

**Files:**
- Modify: `cas/profiles.py` (after `list_store_apks`)
- Test: `tests/test_cas.py` (profiles test class)

**Interfaces:**
- Consumes: `set_meta_key`, `apk_store_pkg_dir`, `store_current_label` (existing/Task 2); `shutil` (already imported in profiles.py)
- Produces:
  - `put_store_apk(store_dir, pkg, src, label=None) -> str` (the label)
  - `remove_store_apk(store_dir, pkg) -> None`

- [ ] **Step 1: Write the failing tests**

Add to the profiles test class:

```python
    def test_put_defaults_label_and_sets_current(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            src = pathlib.Path(t) / "cocoon-1.5.0.apk"; src.write_text("v15")
            label = P.put_store_apk(store, "org.cocoon.app", src)
            self.assertEqual(label, "cocoon-1.5.0")                          # default label = filename stem
            self.assertEqual(P.store_current_label(store, "org.cocoon.app"), "cocoon-1.5.0")
            self.assertEqual((store / "org.cocoon.app" / "cocoon-1.5.0.apk").read_text(), "v15")

    def test_second_put_repoints_current_and_retains_prior(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            a = pathlib.Path(t) / "cocoon-1.4.0.apk"; a.write_text("v14")
            b = pathlib.Path(t) / "cocoon-1.5.0.apk"; b.write_text("v15")
            P.put_store_apk(store, "org.cocoon.app", a)
            P.put_store_apk(store, "org.cocoon.app", b)
            self.assertEqual(P.store_current_label(store, "org.cocoon.app"), "cocoon-1.5.0")
            self.assertTrue((store / "org.cocoon.app" / "cocoon-1.4.0.apk").is_file())   # prior label kept

    def test_reused_label_archives_prior_bytes(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            old = pathlib.Path(t) / "old.apk"; old.write_text("old")
            new = pathlib.Path(t) / "new.apk"; new.write_text("new")
            P.put_store_apk(store, "p", old, label="v1")
            P.put_store_apk(store, "p", new, label="v1")                     # re-use label
            self.assertEqual((store / "p" / "v1.apk").read_text(), "new")
            arch = list((store / "p" / "_archive").glob("v1.apk*"))
            self.assertEqual([a.read_text() for a in arch], ["old"])

    def test_remove_clears_current_but_keeps_files(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            src = pathlib.Path(t) / "cocoon-1.5.0.apk"; src.write_text("v15")
            P.put_store_apk(store, "org.cocoon.app", src)
            P.remove_store_apk(store, "org.cocoon.app")
            self.assertIsNone(P.store_current_label(store, "org.cocoon.app"))
            self.assertTrue((store / "org.cocoon.app" / "cocoon-1.5.0.apk").is_file())   # bytes retained
            self.assertEqual(P.list_store_apks(store), [])                               # not listed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas -k "put_ or reused_label or remove_clears" -v`
Expected: FAIL with `AttributeError: module 'cas.profiles' has no attribute 'put_store_apk'`

- [ ] **Step 3: Implement**

Add to `cas/profiles.py` after `list_store_apks`:

```python
def _archive_if_exists(target, pkgdir):
    """Move an existing store target (file or dir) into <pkgdir>/_archive/ under a non-colliding name, so a
    re-used label never hard-deletes the prior bytes."""
    target = pathlib.Path(target)
    if not target.exists():
        return
    arch = pathlib.Path(pkgdir) / "_archive"
    arch.mkdir(parents=True, exist_ok=True)
    dest, n = arch / target.name, 1
    while dest.exists():
        dest = arch / f"{target.name}.{n}"
        n += 1
    shutil.move(str(target), str(dest))


def put_store_apk(store_dir, pkg, src, label=None):
    """Add/replace the CURRENT build of `pkg`. `src` is a single .apk file OR a directory of split APKs.
    Copies it under <store>/<pkg>/<label>(.apk|/) and repoints meta 'current=<label>'. `label` defaults to
    the source's filename stem (its dir name for a split set). Any PRIOR bytes occupying the same label
    target are archived first (never hard-deleted). Backs BOTH the GUI's Add and Update. Returns the label."""
    src = pathlib.Path(src)
    d = apk_store_pkg_dir(store_dir, pkg)
    d.mkdir(parents=True, exist_ok=True)
    label = label or (src.stem if src.is_file() else src.name)
    if src.is_dir():
        target = d / label
        _archive_if_exists(target, d)
        shutil.copytree(src, target)
    else:
        target = d / f"{label}.apk"
        _archive_if_exists(target, d)
        shutil.copy2(src, target)
    set_meta_key(d / "meta", "current", label)
    return label


def remove_store_apk(store_dir, pkg):
    """Soft-remove: clear meta 'current' so `pkg` stops deploying everywhere, while RETAINING every label
    file in place (re-running put_store_apk restores it). No-op if the package isn't in the store."""
    meta_path = apk_store_pkg_dir(store_dir, pkg) / "meta"
    if meta_path.exists():
        set_meta_key(meta_path, "current", "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cas -k "put_ or reused_label or remove_clears or store" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(profiles): put_store_apk/remove_store_apk (soft-archive, current pointer)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: profiles — `resolve_app_apk` + `download_rows`

**Files:**
- Modify: `cas/profiles.py` (after `remove_store_apk`)
- Test: `tests/test_cas.py` (profiles test class)

**Interfaces:**
- Consumes: `store_apk_files` (Task 2), a `Profile` (`.payload`)
- Produces:
  - `resolve_app_apk(pkg, prof, store_dir, bundle_fallback=None) -> list[pathlib.Path]|None`
  - `download_rows(all_pkgs, store_pkgs, saved) -> dict[str, tuple[bool, bool]]`

- [ ] **Step 1: Write the failing tests**

Add to the profiles test class:

```python
    def test_resolve_prefers_payload_then_store_then_bundle(self):
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "p", apps=["com.captured"])                  # captured app has a payload apk
            store = pathlib.Path(t) / "store"
            self.assertEqual([f.name for f in P.resolve_app_apk("com.captured", prof, store)], ["base.apk"])
            _seed_store(store, "org.cocoon.app", "v1")
            self.assertEqual([f.name for f in P.resolve_app_apk("org.cocoon.app", prof, store)], ["v1.apk"])
            b = pathlib.Path(t) / "kit.apk"; b.write_text("x")
            self.assertEqual(P.resolve_app_apk("com.kit", prof, store, bundle_fallback=b), [b])
            self.assertIsNone(P.resolve_app_apk("com.absent", prof, store))

    def test_resolve_split_store_returns_list(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"; d = store / "com.split" / "v2"; d.mkdir(parents=True)
            (d / "base.apk").write_text("a"); (d / "split_config.apk").write_text("b")
            (store / "com.split" / "meta").write_text("current=v2\n")
            files = P.resolve_app_apk("com.split", None, store)
            self.assertEqual(sorted(f.name for f in files), ["base.apk", "split_config.apk"])

    def test_download_rows_appends_store_only_apk_on_config_off(self):
        rows = P.download_rows(["a", "b"], ["b", "store1"], saved={"a": (True, False)})
        self.assertEqual(rows, {"a": (True, False), "b": (True, True), "store1": (True, False)})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas -k "resolve_ or download_rows" -v`
Expected: FAIL with `AttributeError: module 'cas.profiles' has no attribute 'resolve_app_apk'`

- [ ] **Step 3: Implement**

Add to `cas/profiles.py` after `remove_store_apk`:

```python
def resolve_app_apk(pkg, prof, store_dir, bundle_fallback=None):
    """The APK file(s) to install for `pkg`, in priority order, or None if nothing is available:
      1. the profile's CAPTURED module — golden_root_payload/<pkg>/apk/*.apk (unchanged behaviour),
      2. else the server store's CURRENT build (store_apk_files),
      3. else `bundle_fallback` — a path or list of paths shipped in the CAS bundle (kit apps only).
    Returns a list of file paths (the installer uses install-multiple when len > 1)."""
    if prof is not None:
        apkdir = pathlib.Path(prof.payload) / pkg / "apk"
        cap = sorted(apkdir.glob("*.apk")) if apkdir.is_dir() else []
        if cap:
            return cap
    files = store_apk_files(store_dir, pkg)
    if files:
        return files
    if bundle_fallback:
        cand = ([bundle_fallback] if isinstance(bundle_fallback, (str, pathlib.Path)) else bundle_fallback)
        fb = [pathlib.Path(p) for p in cand if pathlib.Path(p).is_file()]
        if fb:
            return fb
    return None


def download_rows(all_pkgs, store_pkgs, saved):
    """Ordered {pkg: (apk_bool, cfg_bool)} for the Download app-pick modal. The profile's own apps come
    first (each defaulting to BOTH axes), then store-only apps are appended (defaulting to (True, False) —
    APK on, no captured config to restore). A `saved` axis for any pkg overrides the default. Pure — no I/O."""
    rows = {}
    for pkg in all_pkgs:
        rows[pkg] = saved.get(pkg, (True, True))
    for pkg in store_pkgs:
        if pkg not in rows:
            rows[pkg] = saved.get(pkg, (True, False))
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cas -k "resolve_ or download_rows" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(profiles): resolve_app_apk (payload->store->bundle) + download_rows helper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: provision — deploy managed apps PC-side (split + filtered manifest + install)

**Files:**
- Modify: `cas/provision.py` — add `_split_manifest_apps` + `_install_apk` (module level, after `_validate_payload`, ~line 82); rewire `provision()` (lines ~296–386)
- Test: `tests/test_cas.py` (new `class TestApkStoreDeploy(unittest.TestCase)`)

**Interfaces:**
- Consumes: `profiles.resolve_app_apk`, `config.apk_store_dir`, `profiles.save_manifest`, `Adb.raw`
- Produces:
  - `provision._split_manifest_apps(pay, pkgs, axes) -> (payload_list, managed_list)`
  - `provision._install_apk(adb, pkg, files, log) -> bool`

- [ ] **Step 1: Write the failing tests**

Add a new class to `tests/test_cas.py` (env-restoring, like `TestConfig`):

```python
class TestApkStoreDeploy(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("CAS_CONFIG", "CAS_PROFILES", "CAS_COMPANION_APK")}

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_split_manifest_apps(self):
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "p", apps=["com.captured"])                     # captured -> payload module exists
            pay = prof.payload
            pkgs = ["com.captured", "org.cocoon.app", "com.cfgonly"]
            axes = {"com.captured": (True, True), "org.cocoon.app": (True, False),
                    "com.cfgonly": (False, True)}
            payload, managed = PV._split_manifest_apps(pay, pkgs, axes)
            self.assertEqual(payload, ["com.captured"])
            self.assertEqual(managed, ["org.cocoon.app"])                 # apk-axis, no module; cfgonly excluded

    def test_install_apk_single_and_split(self):
        fr = FakeRunner(); adb = Adb(runner=fr)
        self.assertTrue(PV._install_apk(adb, "p", [pathlib.Path("/x/base.apk")], log=lambda *a: None))
        self.assertTrue(any(c[-1] == "/x/base.apk" and "install" in c for c in fr.calls))
        fr2 = FakeRunner(); adb2 = Adb(runner=fr2)
        PV._install_apk(adb2, "p", [pathlib.Path("/x/base.apk"), pathlib.Path("/x/split.apk")],
                        log=lambda *a: None)
        self.assertTrue(any("install-multiple" in c for c in fr2.calls))

    def test_provision_installs_managed_store_app(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "cocoon", apps=["org.es_de.frontend"])          # one captured app
            P.save_manifest(prof.manifest_path, ["org.es_de.frontend", "org.cocoon.app"],
                            {"settings": "on"}, header="# cocoon",
                            axes={"org.es_de.frontend": (True, True), "org.cocoon.app": (True, False)})
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.cocoon.app", "v1", content="apkbytes")
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            C.set_apk_store(str(store))
            fr = FakeRunner(model="Retroid Pocket 6"); adb = Adb(runner=fr)
            ok = PV.provision(adb, P.Profile(prof.path), log=lambda *a: None)
            self.assertTrue(ok, f"provision failed; calls={fr.cmds()}")
            self.assertTrue(any("install" in c and any("v1.apk" in x for x in c) for c in fr.calls),
                            f"expected managed-app install; calls={fr.cmds()}")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas.TestApkStoreDeploy -v`
Expected: FAIL with `AttributeError: module 'cas.provision' has no attribute '_split_manifest_apps'`

- [ ] **Step 3a: Add the helpers**

Add to `cas/provision.py` after `_validate_payload` (~line 82):

```python
def _split_manifest_apps(pay, pkgs, axes):
    """(payload_apps, managed_apps). PAYLOAD apps carry a captured module dir under `pay` (pushed + restored
    on-device, unchanged). MANAGED apps have the apk axis ON but NO captured module — they install PC-side
    from the server store. Companion is excluded from managed: it has its own install path."""
    pay = pathlib.Path(pay)
    payload = [p for p in pkgs if (pay / p).is_dir()]
    managed = [p for p in pkgs
               if not (pay / p).is_dir() and axes.get(p, (True, True))[0] and p != COMPANION_PKG]
    return payload, managed


def _install_apk(adb, pkg, files, log):
    """adb-install one app from the PC: `install -r -g` for a single APK, `install-multiple -r -g` for a
    split set. Best-effort — a failure is a WARNING, not an abort (matches install_companion). True on OK."""
    paths = [str(f) for f in files]
    if len(paths) == 1:
        rc, _, err = adb.raw("install", "-r", "-g", paths[0])
    else:
        rc, _, err = adb.raw("install-multiple", "-r", "-g", *paths)
    if rc == 0:
        log(f"installed {pkg} from the server store ({len(paths)} file(s)).")
        return True
    log(f"warning: install of {pkg} returned {rc}: {(err or '').strip()} (continuing).")
    return False
```

- [ ] **Step 3b: Rewire `provision()`**

In `cas/provision.py`, replace the block at lines ~296–300:

```python
    pay = profile.payload
    pkgs = profile.pkgs()
    flags = profile.flags()                            # @-flags from the manifest (settings/hardening/...)
    if not _validate_payload(pay, pkgs, log):
        return False
```

with:

```python
    pay = profile.payload
    pkgs = profile.pkgs()
    flags = profile.flags()                            # @-flags from the manifest (settings/hardening/...)
    axes = profile.axes()
    from . import config as _cfg
    # PAYLOAD apps go through push + on-device restore (unchanged); MANAGED apps (apk axis, no captured
    # module) install PC-side from the server store after restore. Validate/push only the payload apps.
    pay_pkgs, managed_pkgs = _split_manifest_apps(pay, pkgs, axes)
    if not _validate_payload(pay, pay_pkgs, log):
        return False
```

In the push loop, change line ~325 `for i, pkg in enumerate(pkgs, 1):` to:

```python
        for i, pkg in enumerate(pay_pkgs, 1):              # only the payload (captured) app modules
```

Change the internal-dirs loop at line ~338 `for pkg in pkgs:` to:

```python
        for pkg in pay_pkgs:                                # internal dirs for included PAYLOAD apps only
```

Replace the manifest push at lines ~346–347:

```python
        if not push(profile.manifest_path, f"{DEV}/manifest"):
            return False
```

with a FILTERED device manifest (managed apps removed so on-device restore never sees them):

```python
        tf = tempfile.NamedTemporaryFile(prefix="cas_manifest_", delete=False)
        tf.close()
        dev_manifest = pathlib.Path(tf.name)
        P.save_manifest(dev_manifest, pay_pkgs, flags, header=f"# {profile.name} (deploy)",
                        axes={p: axes.get(p, (True, True)) for p in pay_pkgs})
        ok_m = push(dev_manifest, f"{DEV}/manifest")
        try:
            dev_manifest.unlink()
        except OSError:
            pass
        if not ok_m:
            return False
```

Add the managed-app install AFTER the Companion/lockdown block (after line ~381, before the `rm -rf {DEV}` cleanup at ~382):

```python
    if not dry_push and managed_pkgs:
        store = _cfg.apk_store_dir()
        for pkg in managed_pkgs:
            files = P.resolve_app_apk(pkg, profile, store)
            if not files:
                log(f"WARNING: '{pkg}' is in the manifest but not in the server store ({store}) and not "
                    "captured — skipped (the config wants it).")
                continue
            _install_apk(adb, pkg, files, log)
```

- [ ] **Step 4: Run the tests + full suite**

Run: `python3 -m unittest tests.test_cas.TestApkStoreDeploy -v`
Expected: PASS

Run: `python3 -m unittest tests.test_cas tests.test_firmware tests.test_warnings`
Expected: OK (no regressions — existing provision/manifest tests still pass)

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(provision): install managed store apps PC-side; filter device manifest to payload apps

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: provision — kit APKs (Companion + Magisk) store-first with bundle fallback

**Files:**
- Modify: `cas/provision.py` — add `MAGISK_PKG` constant (~line 33) + `_kit_apk` helper; update `install_companion` (~line 192) and `root_all`'s magisk resolution (~line 899/926)
- Test: `tests/test_cas.py` (`class TestApkStoreDeploy`)

**Interfaces:**
- Consumes: `profiles.store_apk_files`, `profiles.resolve_asset`, `config.apk_store_dir`, `_install_apk` (Task 5)
- Produces: `provision._kit_apk(pkg, prof, appdir, fallback_rel) -> pathlib.Path`

- [ ] **Step 1: Write the failing tests**

Add to `class TestApkStoreDeploy`:

```python
    def test_kit_apk_prefers_store_then_resolve_asset(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            prof = _mk(t, "p", apps=["a"])
            store = pathlib.Path(t) / "store"
            appdir = pathlib.Path(t)
            (appdir / "data" / "Apps").mkdir(parents=True)
            (appdir / "data" / "Apps" / "Magisk.apk").write_text("m")
            self.assertEqual(PV._kit_apk(PV.MAGISK_PKG, prof, str(appdir), "data/Apps/Magisk.apk"),
                             appdir / "data" / "Apps" / "Magisk.apk")        # no store -> bundle fallback
            _seed_store(store, PV.MAGISK_PKG, "v30", content="x")
            C.set_apk_store(str(store))
            self.assertEqual(PV._kit_apk(PV.MAGISK_PKG, prof, str(appdir), "data/Apps/Magisk.apk").name,
                             "v30.apk")                                       # store build wins

    def test_install_companion_prefers_store_then_bundle(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            store = pathlib.Path(t) / "store"
            bundle = pathlib.Path(t) / "companion-bundle.apk"; bundle.write_text("b")
            os.environ["CAS_COMPANION_APK"] = str(bundle)
            fr = FakeRunner(); adb = Adb(runner=fr)
            PV.install_companion(adb, log=lambda *a: None)                    # no store -> bundle
            self.assertTrue(any("companion-bundle.apk" in x for c in fr.calls for x in c))
            _seed_store(store, PV.COMPANION_PKG, "v9", content="s")
            C.set_apk_store(str(store))
            fr2 = FakeRunner(); adb2 = Adb(runner=fr2)
            PV.install_companion(adb2, log=lambda *a: None)                   # store build wins
            self.assertTrue(any("v9.apk" in x for c in fr2.calls for x in c))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas.TestApkStoreDeploy.test_kit_apk_prefers_store_then_resolve_asset -v`
Expected: FAIL with `AttributeError: module 'cas.provision' has no attribute '_kit_apk'` (and `MAGISK_PKG`)

- [ ] **Step 3a: Add `MAGISK_PKG` + `_kit_apk`**

Add the constant near the other package ids in `cas/provision.py` (after line ~32):

```python
MAGISK_PKG = "com.topjohnwu.magisk"                          # store key for the Magisk app (kit APK)
```

Add `_kit_apk` after `_install_apk` (Task 5):

```python
def _kit_apk(pkg, prof, appdir, fallback_rel):
    """Resolve a KIT apk (Magisk/Companion) PC-side path: the server store's CURRENT build if present, else
    the bundled fallback via resolve_asset (profile.meta override > appdir-relative default). Store-first so
    a kit can be version-managed centrally; bundle fallback so an offline NAS never blocks rooting."""
    try:
        from . import config as _cfg
        files = P.store_apk_files(_cfg.apk_store_dir(), pkg)
        if files:
            return files[0]
    except Exception:
        pass
    return P.resolve_asset(prof, appdir, fallback_rel)
```

- [ ] **Step 3b: Update `install_companion`**

In `cas/provision.py`, replace the body of `install_companion` (lines ~197–208, the `src = …` through the final `return False`) so it prefers the store. Replace:

```python
    src = pathlib.Path(apk_src) if apk_src else \
        pathlib.Path(os.environ.get("CAS_COMPANION_APK", str(COMPANION_SRC)))
    if not src.is_file():
        log(f"Companion app not on this PC ({src.name}) — skipping its install (OTA self-update still applies).")
        return False
    log(f"installing the GameCove Companion app from PC: {src.name} ...")
    rc, _, err = adb.raw("install", "-r", "-g", str(src))
    if rc == 0:
        log("Companion app installed (from PC).")
        return True
    log(f"warning: Companion app install returned {rc}: {err.strip()} (provisioning still OK).")
    return False
```

with:

```python
    if not apk_src:                                          # prefer the server store's CURRENT Companion build
        try:
            from . import config as _cfg
            files = P.store_apk_files(_cfg.apk_store_dir(), COMPANION_PKG)
        except Exception:
            files = []
        if files:
            log("installing the GameCove Companion app from the server store ...")
            return _install_apk(adb, COMPANION_PKG, files, log)
    src = pathlib.Path(apk_src) if apk_src else \
        pathlib.Path(os.environ.get("CAS_COMPANION_APK", str(COMPANION_SRC)))
    if not src.is_file():
        log(f"Companion app not on this PC ({src.name}) — skipping its install (OTA self-update still applies).")
        return False
    log(f"installing the GameCove Companion app from PC: {src.name} ...")
    rc, _, err = adb.raw("install", "-r", "-g", str(src))
    if rc == 0:
        log("Companion app installed (from PC).")
        return True
    log(f"warning: Companion app install returned {rc}: {err.strip()} (provisioning still OK).")
    return False
```

- [ ] **Step 3c: Update Magisk resolution in `root_all`**

In `cas/provision.py`, find in `root_all` (~line 899 and ~926):

```python
            magisk_rel = prof.meta.get("magisk_apk") or DEFAULT_MAGISK_APK
            stock_path = P.resolve_asset(prof, appdir, stock_rel)
```

Leave `magisk_rel`/`stock_path` as-is, but change the `root(...)` call's `magisk_apk=` argument (line ~926) from:

```python
                      magisk_apk=P.resolve_asset(prof, appdir, magisk_rel),
```

to:

```python
                      magisk_apk=_kit_apk(MAGISK_PKG, prof, appdir, magisk_rel),
```

- [ ] **Step 4: Run the tests + full suite**

Run: `python3 -m unittest tests.test_cas.TestApkStoreDeploy -v`
Expected: PASS

Run: `python3 -m unittest tests.test_cas tests.test_firmware tests.test_warnings`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(provision): kit APKs (Magisk/Companion) resolve store-first, bundle fallback

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: GUI — "Managed APKs" store manager

**Files:**
- Modify: `cas/gui.py` — settings-menu entry (~line 211) + new method `_open_apk_store`

**Interfaces:**
- Consumes: `config.apk_store_dir`, `profiles.list_store_apks`, `profiles.put_store_apk`, `profiles.remove_store_apk`, existing `self._run_bg`, `self.log`
- Produces: GUI surface only (no new callable consumed elsewhere)

> GUI code isn't unit-tested in this repo (headless CI, no display). Verify manually per Step 4.

- [ ] **Step 1: Add the menu entry**

In `cas/gui.py`, just after the existing settings entry at line ~211 (`setm.add_command(label="Firmware folder…", command=self.choose_firmware_dir)`), add:

```python
        setm.add_command(label="Managed APKs…", command=self._open_apk_store)
```

- [ ] **Step 2: Add the manager method**

Add this method to the `App` class (near `_add_firmware`, ~line 1589):

```python
    def _open_apk_store(self):
        """Manage the server-side APK store (config.apk_store_dir()): list packages, Add/Update a build
        (sets it CURRENT — every config that lists the app then deploys it), or Remove (soft — clears
        current, keeps files). Shared across ALL profiles; uploads go to the NAS by default."""
        store = config.apk_store_dir()
        dlg = tk.Toplevel(self.win); dlg.title("Managed APKs (server store)"); dlg.transient(self.win)
        tk.Label(dlg, text=f"Server store: {store}", anchor="w").pack(fill="x", padx=8, pady=(8, 4))
        tree = ttk.Treeview(dlg, columns=("pkg", "label", "files"), show="headings", height=12)
        for c, w in (("pkg", 340), ("label", 160), ("files", 60)):
            tree.heading(c, text=c.upper()); tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=8)

        def refresh():
            tree.delete(*tree.get_children())
            for a in P.list_store_apks(config.apk_store_dir()):
                tree.insert("", "end", iid=a["pkg"], values=(a["pkg"], a["label"], a["nfiles"]))

        def _sel():
            s = tree.selection()
            return s[0] if s else None

        def _put(pkg):
            f = filedialog.askopenfilename(title=f"Choose the APK for {pkg}",
                                           filetypes=[("APK", "*.apk"), ("All files", "*.*")])
            if not f:
                return
            label = simpledialog.askstring("Version label", "Version label (blank = use the file name):",
                                           initialvalue=pathlib.Path(f).stem, parent=dlg) or None

            def work():
                lbl = P.put_store_apk(config.apk_store_dir(), pkg, f, label=label)
                self.log(f"server store: {pkg} → {lbl} (current).")
                self.win.after(0, refresh)
                return True
            self._run_bg(work, label=f"Uploading {pkg} to the store")

        def add():
            pkg = simpledialog.askstring("Add APK", "Package id (e.g. org.cocoon.app):", parent=dlg)
            if pkg and pkg.strip():
                _put(pkg.strip())

        def update():
            pkg = _sel()
            if not pkg:
                messagebox.showinfo("CAS", "Select a package row to update.")
                return
            _put(pkg)

        def remove():
            pkg = _sel()
            if not pkg:
                messagebox.showinfo("CAS", "Select a package row to remove.")
                return
            if messagebox.askyesno("CAS", f"Stop deploying {pkg}?\nFiles stay on the server (soft-remove)."):
                P.remove_store_apk(config.apk_store_dir(), pkg)
                self.log(f"server store: {pkg} removed (soft — files retained).")
                refresh()

        bar = tk.Frame(dlg); bar.pack(fill="x", padx=8, pady=8)
        for txt, cmd in (("Add APK…", add), ("Update…", update), ("Remove", remove), ("Close", dlg.destroy)):
            tk.Button(bar, text=txt, command=cmd).pack(side="left", padx=4)
        refresh()
```

- [ ] **Step 3: Import check**

Confirm `cas/gui.py` already imports `tk`, `ttk`, `filedialog`, `simpledialog`, `messagebox`, `pathlib`, `P` (profiles), `config`. (Line 18 imports the tkinter submodules; `P`/`config` are module-level.) If `import pathlib` is absent, the file uses `P.pathlib` elsewhere (see `_pick_downloads`); in that case write `P.pathlib.Path(f).stem`. Run a syntax check:

Run: `python3 -c "import ast; ast.parse(open('cas/gui.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 4: Manual verification**

Run the GUI (`python3 -m cas` or the project's run command). Then:
1. Settings → **Managed APKs…** opens a window titled "Managed APKs (server store)" showing the store path.
2. **Add APK…** → enter `org.cocoon.app`, pick any `.apk`, accept the default label → the row appears with the label and file count; the log shows `server store: org.cocoon.app → <label> (current)`.
3. Select the row, **Update…**, pick a different `.apk`, type a new label → the row's label changes.
4. Select the row, **Remove** → confirm → the row disappears; re-opening shows it gone, but the file remains on disk under the store.

Confirm each of the four behaviors before committing.

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py
git commit -m "feat(gui): Managed APKs manager (Add/Update/Remove against the server store)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: GUI — surface store apps in the Download picker

**Files:**
- Modify: `cas/gui.py` — `_pick_downloads` (~line 1350–1364)

**Interfaces:**
- Consumes: `profiles.download_rows` (Task 4), `profiles.list_store_apks`, `config.apk_store_dir`
- Produces: GUI behavior only

> Logic (`download_rows`) is unit-tested in Task 4. This task wires it in; verify the UI manually per Step 3.

- [ ] **Step 1: Wire store apps into the Download rows**

In `cas/gui.py`, inside `_pick_downloads`, replace lines ~1355 and the `labels=` argument of the `_app_pick_modal` call (~line 1364). Change:

```python
            rows = {pkg: saved.get(pkg, (True, True)) for pkg in prof.all_pkgs()}
            flags = prof.flags()
            flag_specs = [(fl, _DL_FLAG_LABELS[fl], _DL_FLAG_TIPS[fl], flags.get(fl, "on") == "on")
                          for fl in _DL_FLAGS]
            res = self._app_pick_modal(
                f"Download — restore “{name}”",
                "Tick which apps to INSTALL on the device(s) assigned this profile. APK installs the app; "
                "Config restores its saved data/settings/BIOS.",
                prof, rows, launchers, flag_specs,
                labels={launcher_pkg: _HOME_LAUNCHER_LABEL} if launcher_pkg else None)
```

to:

```python
            own_pkgs = prof.all_pkgs()
            store_pkgs = [a["pkg"] for a in P.list_store_apks(config.apk_store_dir())]
            rows = P.download_rows(own_pkgs, store_pkgs, saved)
            labels = {launcher_pkg: _HOME_LAUNCHER_LABEL} if launcher_pkg else {}
            for p in store_pkgs:
                if p not in own_pkgs:
                    labels[p] = f"{p}  ·  from store"
            flags = prof.flags()
            flag_specs = [(fl, _DL_FLAG_LABELS[fl], _DL_FLAG_TIPS[fl], flags.get(fl, "on") == "on")
                          for fl in _DL_FLAGS]
            res = self._app_pick_modal(
                f"Download — restore “{name}”",
                "Tick which apps to INSTALL on the device(s) assigned this profile. APK installs the app; "
                "Config restores its saved data/settings/BIOS. Apps marked “from store” install the "
                "server's current build.",
                prof, rows, launchers, flag_specs, labels=labels)
```

- [ ] **Step 2: Syntax check**

Run: `python3 -c "import ast; ast.parse(open('cas/gui.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Manual verification**

1. Add a managed app to the store (Task 7) for a package NOT captured in some profile.
2. Assign that profile to a device, click **Run** with Download ticked.
3. The Download modal lists the store app as a row labeled `<pkg>  ·  from store`, **APK ticked, Config unticked** by default.
4. Leave it ticked and Run → the profile's manifest now contains `<pkg> apk` (open the profile's `manifest` file to confirm), and on a real deploy that app installs from the store (Task 5 path).

Confirm the row appears and the manifest line is written.

- [ ] **Step 4: Run the full suite (no regressions)**

Run: `python3 -m unittest tests.test_cas tests.test_firmware tests.test_warnings`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py
git commit -m "feat(gui): Download picker lists server-store apps (APK-on/Config-off default)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Store location `library_root()/_apks`, server-by-default → Task 1. ✓
- Store layout (`meta` current, `<label>.apk`/`<label>/`, `_archive`) → Tasks 2–3. ✓
- Managed apps only; captured stays in payload → resolver Task 4, deploy split Task 5. ✓
- Always-latest single current, soft-archive prior → Task 3. ✓
- Deploy resolution payload→store→bundle→WARN → Task 4 (`resolve_app_apk`) + Task 5 (WARN+skip). ✓
- `lib-root.sh` unchanged (filtered device manifest) → Task 5. ✓
- Kit APKs store-first + bundle fallback → Task 6. ✓
- GUI manager (Add/Update/Remove) → Task 7; per-config include → Task 8. ✓
- Manifest uses existing `<pkg> apk` token, no new tokens → Tasks 4/8 (default `(True, False)`). ✓
- Out of scope (version pinning, APK parsing, sha256, OTA, payload migration) → not implemented. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**Type consistency:** `store_apk_files`/`resolve_app_apk` return `list[pathlib.Path]`; `_install_apk` accepts that list; `download_rows` returns `{pkg:(bool,bool)}` matching `_app_pick_modal`'s `rows`. `put_store_apk` returns `str`. `_kit_apk` returns a single `pathlib.Path` (matches `root()`'s `magisk_apk=` path arg). `_split_manifest_apps` returns two lists. ✓
