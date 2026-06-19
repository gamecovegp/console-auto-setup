# NAS-Backed Profile Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the CAS golden library location configurable so it can live on the office NAS (a mounted SMB share), shared by every provisioning PC.

**Architecture:** Add `cas/config.py` that resolves the library path (env → JSON config → default) and persists it. Point `cli.py` and `gui.py` at `library_root()` instead of the hardcoded `profiles/`. The GUI gets a "Library…" control to set/initialize the path and a status line. The OS mounts the SMB share; CAS only stores and uses the path string.

**Tech Stack:** Python 3 stdlib only (`json`, `os`, `pathlib`); Tkinter (stdlib) for the GUI; `unittest` (stdlib) for tests.

## Global Constraints

- **Runtime deps:** stdlib only — NO new pip packages (`json`/`os`/`pathlib` are stdlib).
- **Config file:** `cas-config.json`, located at `APPDIR` (next to the app/exe). Overridable for tests via the `CAS_CONFIG` env var.
- **Library resolution priority (exact):** `CAS_PROFILES` env var → `library` key in the config file → default `APPDIR / "profiles"`. The default preserves all current behavior when unconfigured.
- **No credentials in CAS:** the SMB share is mounted by the OS; CAS stores/uses only the resulting path string.
- **Tests:** stdlib `unittest`, in `tests/test_cas.py`. Run from the `tests/` dir: `python3 -m unittest test_cas` (the repo path contains `[07]`, which breaks `unittest discover` globbing from the repo root — run from `tests/`). All existing tests must stay green.
- **Repo is NOT git-initialized.** Treat every "Commit" step as a checkpoint: either `git init` first if you want history, or skip the commit and just verify tests pass before moving on.
- Existing helpers already accept a root: `profiles.list_profiles(root)`, `profiles.match_profile(model, root)`, `provision.provision_all(..., root=)`, `provision.root_all/seal_all(..., profiles_root=)`. They do NOT change — callers pass the resolved root.

---

### Task 1: `cas/config.py` — library path resolution + persisted settings

**Files:**
- Create: `cas/config.py`
- Test: `tests/test_cas.py` (add a `TestConfig` class)

**Interfaces:**
- Consumes: `cas.APPDIR` (existing `pathlib.Path`).
- Produces:
  - `config_path() -> pathlib.Path` — `CAS_CONFIG` env if set, else `APPDIR / "cas-config.json"`.
  - `load_config() -> dict` — parsed JSON, or `{}` if missing/corrupt.
  - `save_config(cfg: dict) -> None` — writes pretty JSON to `config_path()`.
  - `set_library(path) -> pathlib.Path` — persists `{"library": str(path)}`, returns `library_root()`.
  - `library_root() -> pathlib.Path` — `CAS_PROFILES` env → config `library` → `APPDIR / "profiles"`.
  - `library_reachable() -> bool` — `library_root().is_dir()`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cas.py`, add (place after the `TestAdb` class):

```python
class TestConfig(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_is_appdir_profiles(self):
        from cas import config as C, APPDIR
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")  # no config file
            os.environ.pop("CAS_PROFILES", None)
            self.assertEqual(C.library_root(), APPDIR / "profiles")

    def test_config_library_wins_over_default(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ.pop("CAS_PROFILES", None)
            self.assertEqual(str(C.set_library("/mnt/nas/CAS Profiles")), "/mnt/nas/CAS Profiles")
            self.assertEqual(C.load_config().get("library"), "/mnt/nas/CAS Profiles")

    def test_env_wins_over_config(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            C.set_library("/mnt/nas/lib")
            os.environ["CAS_PROFILES"] = "/tmp/override"
            self.assertEqual(str(C.library_root()), "/tmp/override")

    def test_corrupt_config_is_empty(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            cfg = pathlib.Path(t) / "cas-config.json"
            cfg.write_text("{ this is not json")
            os.environ["CAS_CONFIG"] = str(cfg)
            self.assertEqual(C.load_config(), {})

    def test_library_reachable(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ["CAS_PROFILES"] = t
            self.assertTrue(C.library_reachable())
            os.environ["CAS_PROFILES"] = str(pathlib.Path(t) / "nope")
            self.assertFalse(C.library_reachable())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd tests && python3 -m unittest test_cas.TestConfig -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cas.config'`.

- [ ] **Step 3: Create `cas/config.py`**

```python
"""Where the CAS profile library lives. Resolves the library root from (priority):
  1. CAS_PROFILES env var   (one-shot override for scripts/CI)
  2. 'library' key in cas-config.json   (set via the GUI / persisted)
  3. APPDIR/profiles        (default — unchanged behavior when unconfigured)
The SMB share is mounted by the OS; we only ever store/use the path string (no credentials here)."""
import json
import os
import pathlib

from . import APPDIR


def config_path():
    """cas-config.json next to the app (override with CAS_CONFIG, mainly for tests)."""
    return pathlib.Path(os.environ.get("CAS_CONFIG", str(APPDIR / "cas-config.json")))


def load_config():
    """Parsed config dict, or {} if the file is missing or unparseable."""
    try:
        return json.loads(config_path().read_text())
    except Exception:
        return {}


def save_config(cfg):
    config_path().write_text(json.dumps(cfg, indent=2))


def library_root():
    """The profile library directory (CAS_PROFILES env > config 'library' > APPDIR/profiles)."""
    env = os.environ.get("CAS_PROFILES")
    if env:
        return pathlib.Path(env)
    lib = load_config().get("library")
    if lib:
        return pathlib.Path(lib)
    return APPDIR / "profiles"


def set_library(path):
    """Persist the library location to cas-config.json. Returns the resolved library_root()."""
    cfg = load_config()
    cfg["library"] = str(path)
    save_config(cfg)
    return library_root()


def library_reachable():
    """True if the configured library path exists as a directory (e.g. the NAS drive is mapped)."""
    try:
        return library_root().is_dir()
    except OSError:
        return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd tests && python3 -m unittest test_cas.TestConfig -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full suite (nothing regressed)**

Run: `cd tests && python3 -m unittest test_cas -q`
Expected: `OK`.

- [ ] **Step 6: Commit (checkpoint)**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(cas): configurable profile-library location (env > config > default)"
```
(Repo not under git → skip or `git init` first; either way confirm the suite is green before continuing.)

---

### Task 2: Point `cli.py` and `gui.py` at `library_root()`

**Files:**
- Modify: `cas/cli.py` (the `PROOT` definition + `main()` argparse)
- Modify: `cas/gui.py` (`App.__init__` — `self.profiles_root`)
- Test: manual CLI verification (below)

**Interfaces:**
- Consumes: `cas.config.library_root` (Task 1).
- Produces: `cli.main` honors a new `--library PATH` flag (one-shot override); `gui.App.profiles_root` is the resolved library.

- [ ] **Step 1: Modify `cas/cli.py`**

Replace the module-level line:
```python
PROOT = str(APPDIR / "profiles")
```
with:
```python
from .config import library_root
```
(Place that import with the other `from . import` lines at the top; delete the `PROOT = …` module constant.)

Then inside `main(argv=None)`, immediately after `a = ap.parse_args(argv)`, add:
```python
    proot = a.library or str(library_root())
```
Add the argparse option (next to `--adb`/`--fastboot`):
```python
    ap.add_argument("--library", default=None,
                    help="profile-library path (default: cas-config.json / CAS_PROFILES / APPDIR/profiles)")
```
Then replace every remaining use of `PROOT` in `main()` with `proot` (there are uses in: `list`, `provision-all`, `root-all`/`seal-all`, `capture`, and `_resolve_profile`). For `_resolve_profile`, change its body to take the root explicitly — update the function to:
```python
def _resolve_profile(adb, name, proot):
    """Explicit --profile NAME, else auto-match the device's ro.product.model."""
    if name:
        d = pathlib.Path(proot) / name
        return P.Profile(d) if (d / "profile.meta").exists() else None
    return P.match_profile(adb.getprop("ro.product.model"), proot)
```
and update its two call sites in `main()` to pass `proot`: `_resolve_profile(adb, a.profile, proot)`. Also change the `seal`/`root` branches that build `APPDIR / stock_rel` to keep using `APPDIR` (firmware stays local — do NOT change those to `proot`).

- [ ] **Step 2: Modify `cas/gui.py`**

In `App.__init__`, replace:
```python
        self.profiles_root = str(APPDIR / "profiles")
```
with:
```python
        from .config import library_root
        self.profiles_root = str(library_root())
```

- [ ] **Step 3: Verify the CLI resolves a custom library**

```bash
cd "/home/ccvisionary/Documents/Work/[07] Projects/console-auto-setup"
mkdir -p /tmp/nastest/odin2mini && printf 'model_match=Odin2 ?Mini\nfrontend=es-de\n' > /tmp/nastest/odin2mini/profile.meta && printf 'com.x\n' > /tmp/nastest/odin2mini/manifest
CAS_PROFILES=/tmp/nastest python3 -m cas.cli list
python3 -m cas.cli --library /tmp/nastest list
```
Expected (both): a line listing `odin2mini   frontend=es-de  match=Odin2 ?Mini`.

- [ ] **Step 4: Run the full suite**

Run: `cd tests && python3 -m unittest test_cas -q`
Expected: `OK`.

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add cas/cli.py cas/gui.py
git commit -m "feat(cas): cli/gui use the configurable library_root()"
```

---

### Task 3: GUI "Library…" control — set / initialize / status / reachability

**Files:**
- Modify: `cas/gui.py` (imports; the Profile row in `_build`; `refresh_profiles`; add `set_library`/`init_library` handlers)
- Test: manual GUI smoke (below) — Tk rendering isn't unit-tested; the testable logic (resolution, list-against-root, reachability) is covered by Task 1 + `list_profiles`.

**Interfaces:**
- Consumes: `cas.config.set_library`, `cas.config.library_root`, `cas.config.library_reachable` (Task 1); existing `App.refresh_profiles`, `App.log`.
- Produces: a "Library…" button + `App.set_library_dialog()` and `App.init_library()` methods; `refresh_profiles` shows a reachability message instead of crashing.

- [ ] **Step 1: Add the config import**

In `cas/gui.py`, add near the top imports:
```python
from .config import set_library, library_root, library_reachable
```
(and remove the local `from .config import library_root` added in Task 2's `__init__`, keeping just this top-level import; set `self.profiles_root = str(library_root())` in `__init__` using it.)

Add this to the imports line `from tkinter import …`: ensure `filedialog` is included, i.e.:
```python
from tkinter import ttk, scrolledtext, messagebox, simpledialog, filedialog
```

- [ ] **Step 2: Add the "Library…" button to the Profile row**

In `_build`, in the `row` frame next to `New…`/`Delete…` (the `row = ttk.Frame(prof)` block), append:
```python
        _tip(ttk.Button(row, text="Library…", command=self.set_library_dialog),
             "Where the golden library lives. Point this at the NAS share "
             "(e.g. the mapped drive …\\01 GAMECOVE\\[03] SETUP\\CAS Profiles). Saved to cas-config.json; "
             "shared by every PC mapped to the NAS.").pack(side="left", padx=2)
```

- [ ] **Step 3: Add a library status line under the Profile picker**

In `_build`, right after the `row.pack(fill="x")` for the profile row, add:
```python
        self.lib_var = tk.StringVar()
        ttk.Label(prof, textvariable=self.lib_var, foreground="#555").pack(anchor="w", pady=(2, 0))
        self._update_lib_label()
```

- [ ] **Step 4: Add the handler methods**

Add these methods to `App` (place near `new_profile`/`delete_profile`):
```python
    def _update_lib_label(self):
        root = self.profiles_root
        mark = "✓" if library_reachable() else "✗ not reachable (map the NAS drive?)"
        self.lib_var.set(f"Library: {root}   {mark}")

    def set_library_dialog(self):
        cur = self.profiles_root
        choice = filedialog.askdirectory(title="Select the CAS Profiles library folder", initialdir=cur)
        if not choice:
            return
        self.profiles_root = str(set_library(choice))
        self.log(f"library set to: {self.profiles_root}")
        self._update_lib_label()
        if not library_reachable():
            messagebox.showwarning("CAS", f"Library set, but not reachable yet:\n{self.profiles_root}\n\n"
                                          "Is the NAS drive mapped? You can still set it now and map later.")
        elif not (P.pathlib.Path(self.profiles_root) / "_archive").exists() and \
                not list(P.pathlib.Path(self.profiles_root).glob("*/profile.meta")):
            if messagebox.askyesno("CAS", "This library looks empty. Initialize it (create the _archive "
                                          "folder so it's ready for profiles)?"):
                self.init_library()
        self.refresh_profiles()

    def init_library(self):
        root = P.pathlib.Path(self.profiles_root)
        try:
            (root / "_archive").mkdir(parents=True, exist_ok=True)
            self.log(f"initialized library at {root}")
        except OSError as e:
            messagebox.showerror("CAS", f"Could not create the library folder:\n{e}")
```

- [ ] **Step 5: Make `refresh_profiles` reachability-aware**

In `refresh_profiles`, at the very top of the method body (before `names = …`), add:
```python
        self._update_lib_label()
        if not library_reachable():
            self.log(f"Library not reachable: {self.profiles_root} — is the NAS drive mapped? "
                     "Use 'Library…' to fix the path.")
```
(`list_profiles` already returns `[]` for a non-existent root, so the dropdown simply empties — no crash.)

- [ ] **Step 6: Smoke-test the GUI (manual)**

Run: `python3 -m cas` (a display is required).
Verify, in order:
1. The status line shows `Library: <path>  ✓` for the current (local) library.
2. Click **Library…**, pick `/tmp/nastest` (from Task 2 Step 3) → status updates, dropdown shows `odin2mini`, log says "library set to: /tmp/nastest".
3. Click **Library…**, pick a fresh empty folder → it offers to Initialize; accept → an `_archive/` folder is created; status shows `✓`.
4. Click **Library…**, type/pick a non-existent path → status shows `✗ not reachable`, log shows the "Library not reachable" line, no crash.
5. Re-launch `python3 -m cas` → it remembers the last library (read from `cas-config.json`).

- [ ] **Step 7: Run the full suite + confirm config persisted**

Run: `cd tests && python3 -m unittest test_cas -q` → Expected: `OK`.
Run: `cat "/home/ccvisionary/Documents/Work/[07] Projects/console-auto-setup/cas-config.json"` → shows `{"library": "…"}`.
(Then reset for local use if desired: delete `cas-config.json` or set the library back to the local `profiles` path.)

- [ ] **Step 8: Commit (checkpoint)**

```bash
git add cas/gui.py
git commit -m "feat(gui): Library… control to point the golden library at the NAS + reachability status"
```

---

## Notes for the implementer

- **Why no SMB code:** the operator maps `\\192.168.100.227\01 GAMECOVE` as a drive (Windows Map-network-drive with saved credentials, or Linux cifs mount) once per PC. CAS only stores the resulting path. This keeps credentials in the OS and the code tiny.
- **Firmware/cores stay local:** `profile.meta`'s `stock_init_boot`/`patched_init_boot`/`magisk_apk` and the RetroArch cores resolve off `APPDIR` (the local toolkit), NOT the library — do not repoint them. Only the profile *payloads* live on the NAS.
- **The NAS folder layout** (`CAS Profiles\`, `CAS Toolkit\`, `SD Master Images\`) is documented in the spec; `init_library` only creates `CAS Profiles\_archive\` — the rest is created by use (capture writes profiles) or set up by hand.
- **Deferred:** local cache for the 7 GB reads; CAS-managed SMB login. Not in this plan.
