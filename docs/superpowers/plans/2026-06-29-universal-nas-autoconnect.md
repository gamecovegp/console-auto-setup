# Universal NAS auto-connect — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CAS auto-connect the NAS share on Windows/macOS/Linux with its pre-configured credentials and resolve the library/firmware/log paths to wherever the share actually mounted — so the firmware list (and whole NAS library) populates with no manual mount on any OS.

**Architecture:** Replace the hardcoded `/mnt/gamecove` (POSIX) / UNC (Windows) constants with a **connect → discover → resolve** flow in `cas/config.py`: parse the one NAS spec into host/share/subpath, connect the share per-OS (userspace), discover the share's local mountpoint per-OS, and resolve `nas_default_path()`/`library_root()`/`firmware_dir()` relative to it. A stale NAS-pinned config override is auto-ignored when its path doesn't exist.

**Tech Stack:** Python 3 stdlib only (`subprocess`, `pathlib`, `os`, `sys`, `socket`, `urllib.parse`); `unittest` + `unittest.mock`. No new dependency.

## Global Constraints

- Python stdlib only — no third-party runtime deps (CI runs `python -m unittest`).
- All connect/discovery is **best-effort**: any failure returns False/None and falls back to the local library; never raises into the GUI.
- The change is confined to `cas/config.py` + `tests/test_cas.py`. No change to profile/firmware/log I/O elsewhere.
- NAS spec source of truth: `NAS_DEFAULT = r"\\192.168.100.227\01 GAMECOVE\[03] SETUP\CAS Profiles"` → host `192.168.100.227`, share `01 GAMECOVE`, subpath `[03] SETUP/CAS Profiles`.
- Credentials come from `get_nas_credentials()` (saved account else shipped default) — already implemented; do not change.
- Live per-OS mount commands (`mount_smbfs`/`gio mount`/`net use`) carry `[VERIFY on <os>]` — unit tests assert command construction + path resolution, not a real mount.

---

### Task 1: Parse the NAS spec — `nas_share_name()` + `nas_subpath()`

**Files:**
- Modify: `cas/config.py` (add after `nas_share_root()`, ~line 311)
- Test: `tests/test_cas.py` (add to `TestConfig`)

**Interfaces:**
- Consumes: `NAS_DEFAULT` (module constant).
- Produces: `nas_share_name() -> str` (e.g. `"01 GAMECOVE"`); `nas_subpath() -> str` (POSIX-separated, e.g. `"[03] SETUP/CAS Profiles"`).

- [ ] **Step 1: Write the failing tests** — add to `TestConfig` in `tests/test_cas.py`

```python
    def test_nas_share_name_and_subpath(self):
        from cas import config as C
        self.assertEqual(C.nas_share_name(), "01 GAMECOVE")
        self.assertEqual(C.nas_subpath(), "[03] SETUP/CAS Profiles")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_cas.TestConfig.test_nas_share_name_and_subpath -v`
Expected: FAIL — `AttributeError: module 'cas.config' has no attribute 'nas_share_name'`.

- [ ] **Step 3: Implement the helpers** — add to `cas/config.py` after `nas_share_root()`

```python
def nas_share_name():
    r"""The SMB share name from NAS_DEFAULT — the segment after the host (e.g. '01 GAMECOVE')."""
    parts = NAS_DEFAULT.lstrip("\\").split("\\")
    return parts[1] if len(parts) >= 2 else ""


def nas_subpath():
    r"""The path UNDER the share from NAS_DEFAULT, POSIX-separated (e.g. '[03] SETUP/CAS Profiles')."""
    parts = NAS_DEFAULT.lstrip("\\").split("\\")
    return "/".join(parts[2:]) if len(parts) > 2 else ""
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m unittest tests.test_cas.TestConfig.test_nas_share_name_and_subpath -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(config): parse NAS spec into share name + subpath"
```

---

### Task 2: `nas_mountpoint()` — per-OS discovery of the share's local path

**Files:**
- Modify: `cas/config.py` (add after `nas_subpath()`)
- Test: `tests/test_cas.py` (add to `TestConfig`)

**Interfaces:**
- Consumes: `nas_share_name()`, `nas_share_root()`, `nas_host()`, `sys.platform`, `os.environ`, `os.getuid` (POSIX), `pathlib`.
- Produces: `nas_mountpoint() -> str | None` — the local path of the SHARE ROOT on this OS (to which `nas_subpath()` is appended), or `None` if not mounted.

Discovery per OS (verified by existence, not by parsing tool output):
- **Windows:** the UNC `nas_share_root()` if it resolves as a dir.
- **macOS:** `/Volumes/<share>` if it is a dir.
- **Linux/other:** the conventional gvfs FUSE path `${XDG_RUNTIME_DIR:-/run/user/<uid>}/gvfs/smb-share:server=<host>,share=<share-lowercased>` if it is a dir. (Refinement vs spec §5: the gvfs FUSE path is the stable, documented location `gio mount` uses; checking its existence is more robust and testable than scraping `gio mount -l`, which does not print the local path.)

- [ ] **Step 1: Write the failing tests** — add to `TestConfig`

```python
    def test_nas_mountpoint_linux_gvfs(self):
        from cas import config as C
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            os.environ["XDG_RUNTIME_DIR"] = t
            try:
                gv = pathlib.Path(t) / "gvfs" / "smb-share:server=192.168.100.227,share=01 gamecove"
                with mock.patch.object(C.sys, "platform", "linux"):
                    self.assertIsNone(C.nas_mountpoint())     # not mounted yet
                    gv.mkdir(parents=True)
                    self.assertEqual(C.nas_mountpoint(), str(gv))
            finally:
                os.environ.pop("XDG_RUNTIME_DIR", None)

    def test_nas_mountpoint_macos_volumes(self):
        from cas import config as C
        from unittest import mock
        with mock.patch.object(C.sys, "platform", "darwin"), \
             mock.patch.object(C.pathlib.Path, "is_dir", lambda self: str(self) == "/Volumes/01 GAMECOVE"):
            self.assertEqual(C.nas_mountpoint(), "/Volumes/01 GAMECOVE")
```

Note: `test_nas_mountpoint_linux_gvfs` sets `XDG_RUNTIME_DIR`; add `"XDG_RUNTIME_DIR"` to the `setUp`/`tearDown` saved-env keys in `TestConfig` so the suite stays hermetic:

```python
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES", "XDG_RUNTIME_DIR")}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_cas.TestConfig.test_nas_mountpoint_linux_gvfs tests.test_cas.TestConfig.test_nas_mountpoint_macos_volumes -v`
Expected: FAIL — `AttributeError: module 'cas.config' has no attribute 'nas_mountpoint'`.

- [ ] **Step 3: Implement** — add to `cas/config.py` after `nas_subpath()`

```python
def nas_mountpoint():
    """The local path of the NAS SHARE ROOT on THIS OS (to which nas_subpath() is appended), or None if the
    share is not mounted. Discovered by existence, never hardcoded: Windows -> the UNC; macOS ->
    /Volumes/<share>; Linux -> the conventional gvfs FUSE path gio mounts the share at."""
    share = nas_share_name()
    if not share:
        return None
    try:
        if sys.platform == "win32":
            unc = nas_share_root()
            return unc if pathlib.Path(unc).is_dir() else None
        if sys.platform == "darwin":
            p = pathlib.Path("/Volumes") / share
            return str(p) if p.is_dir() else None
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        p = pathlib.Path(runtime) / "gvfs" / f"smb-share:server={nas_host()},share={share.lower()}"
        return str(p) if p.is_dir() else None
    except OSError:
        return None
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m unittest tests.test_cas.TestConfig.test_nas_mountpoint_linux_gvfs tests.test_cas.TestConfig.test_nas_mountpoint_macos_volumes -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(config): nas_mountpoint() discovers the share path per-OS"
```

---

### Task 3: `nas_default_path()` discovery-based + `library_root()` None-safe

**Files:**
- Modify: `cas/config.py:28-30` (`nas_default_path`), `cas/config.py:53-68` (`library_root`)
- Test: `tests/test_cas.py` (add to `TestConfig`)

**Interfaces:**
- Consumes: `nas_mountpoint()`, `nas_subpath()`.
- Produces: `nas_default_path() -> str | None` (discovered mountpoint + subpath, or None); `library_root()` unchanged signature, now tolerant of `nas_default_path()` returning None.

- [ ] **Step 1: Write the failing tests** — add to `TestConfig`

```python
    def test_nas_default_path_follows_mountpoint(self):
        from cas import config as C
        from unittest import mock
        with mock.patch.object(C, "nas_mountpoint", lambda: "/mnt/x/01 GAMECOVE"):
            self.assertEqual(C.nas_default_path(), "/mnt/x/01 GAMECOVE/[03] SETUP/CAS Profiles")
        with mock.patch.object(C, "nas_mountpoint", lambda: None):
            self.assertIsNone(C.nas_default_path())

    def test_library_root_local_when_nas_unmounted(self):
        from cas import config as C, APPDIR
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")
            os.environ.pop("CAS_PROFILES", None)
            with mock.patch.object(C, "nas_default_path", lambda: None):     # NEW: None case
                self.assertEqual(C.library_root(), APPDIR / "data" / "profiles")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_cas.TestConfig.test_nas_default_path_follows_mountpoint tests.test_cas.TestConfig.test_library_root_local_when_nas_unmounted -v`
Expected: FAIL — `test_nas_default_path_follows_mountpoint` returns the old hardcoded constant; `test_library_root_local_when_nas_unmounted` raises `TypeError` (old `pathlib.Path(nas_default_path())` chokes on None).

- [ ] **Step 3: Implement** — replace `nas_default_path` (lines 28-30) and the NAS block in `library_root` (lines 61-67)

```python
def nas_default_path():
    """The NAS library path on THIS OS: the discovered share mountpoint + the subpath, or None when the
    share isn't mounted. Replaces the old hardcoded UNC/POSIX constants so the path follows wherever the OS
    mounted the share."""
    mp = nas_mountpoint()
    if not mp:
        return None
    sub = nas_subpath()
    return str(pathlib.Path(mp) / sub) if sub else mp
```

In `library_root()`, replace the block currently reading:

```python
    # Default to the shared NAS library if it's mounted (UNC on Windows / cifs mount on POSIX); else local.
    nas = pathlib.Path(nas_default_path())
    try:
        if nas.is_dir():
            return nas
    except OSError:
        pass
    return APPDIR / "data" / "profiles"
```

with:

```python
    # Default to the shared NAS library when the share is mounted (path discovered per-OS); else local.
    nas = nas_default_path()
    if nas:
        try:
            if pathlib.Path(nas).is_dir():
                return pathlib.Path(nas)
        except OSError:
            pass
    return APPDIR / "data" / "profiles"
```

- [ ] **Step 4: Run to verify they pass — and that existing TestConfig tests still pass**

Run: `python -m unittest tests.test_cas.TestConfig -v`
Expected: PASS, including the pre-existing `test_default_falls_back_to_local_when_nas_unreachable` and `test_nas_default_used_when_reachable` (they monkeypatch `nas_default_path` to a string, which the None-safe block still handles).

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(config): nas_default_path follows discovered mountpoint; library_root None-safe"
```

---

### Task 4: `firmware_dir()` honors an explicit override only if it exists

**Files:**
- Modify: `cas/config.py:113-119` (`firmware_dir`)
- Test: `tests/test_cas.py` (add to `TestConfig`)

**Interfaces:**
- Consumes: `load_config()`, `library_root()`.
- Produces: `firmware_dir()` unchanged signature — now ignores a configured `firmware_dir` whose path doesn't exist (mirrors `history_dir`'s log_dir handling), falling back to `library_root()/_firmware`.

- [ ] **Step 1: Write the failing tests** — add to `TestConfig`

```python
    def test_firmware_dir_ignores_stale_override(self):
        from cas import config as C
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            cfgp = pathlib.Path(t) / "cas-config.json"
            cfgp.write_text('{"firmware_dir": "/mnt/gamecove/does-not-exist/_firmware"}')
            os.environ["CAS_CONFIG"] = str(cfgp)
            os.environ.pop("CAS_PROFILES", None)
            lib = pathlib.Path(t) / "lib"; lib.mkdir()
            with mock.patch.object(C, "library_root", lambda: lib):
                self.assertEqual(C.firmware_dir(), lib / "_firmware")     # stale override ignored

    def test_firmware_dir_honors_existing_override(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            cfgp = pathlib.Path(t) / "cas-config.json"
            real = pathlib.Path(t) / "fw"; real.mkdir()
            cfgp.write_text('{"firmware_dir": %s}' % __import__("json").dumps(str(real)))
            os.environ["CAS_CONFIG"] = str(cfgp)
            self.assertEqual(C.firmware_dir(), real)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_cas.TestConfig.test_firmware_dir_ignores_stale_override tests.test_cas.TestConfig.test_firmware_dir_honors_existing_override -v`
Expected: `test_firmware_dir_ignores_stale_override` FAILS (current code returns the stale `/mnt/gamecove/...` path unconditionally); `test_firmware_dir_honors_existing_override` passes already.

- [ ] **Step 3: Implement** — replace `firmware_dir` (lines 113-119)

```python
def firmware_dir():
    """The device-root-firmware library directory. An explicit 'firmware_dir' override is honored ONLY if its
    path currently exists (so a stale NAS-pinned override on an offline bench is ignored and the catalog
    follows the discovered library); otherwise library_root()/_firmware. Mirrors history_dir's log_dir rule."""
    d = load_config().get("firmware_dir")
    if d:
        p = pathlib.Path(d)
        try:
            if p.is_dir():
                return p
        except OSError:
            pass
    return library_root() / "_firmware"
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m unittest tests.test_cas.TestConfig.test_firmware_dir_ignores_stale_override tests.test_cas.TestConfig.test_firmware_dir_honors_existing_override -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "fix(config): firmware_dir ignores a stale non-existent override (follows discovery)"
```

---

### Task 5: `nas_connect()` — cross-OS (add macOS; mount the share on Linux)

**Files:**
- Modify: `cas/config.py` (`nas_connect`, the non-Windows branch, ~lines 351-360)
- Test: `tests/test_cas.py` (add to `TestConfig`)

**Interfaces:**
- Consumes: `get_nas_credentials()`, `nas_reachable()`, `library_reachable()`, `nas_host()`, `nas_share_name()`, `sys.platform`, `subprocess`, `urllib.parse.quote`.
- Produces: `nas_connect()` unchanged signature/contract; now connects the SHARE on macOS (`mount_smbfs` → `/Volumes/<share>`) and Linux (`gio mount smb://host/share`), so `nas_mountpoint()` can discover it.

- [ ] **Step 1: Write the failing tests** — add to `TestConfig`

```python
    def _connect_cmd(self, platform):
        # Run nas_connect on a faked OS with NAS 'reachable' but library not yet mounted, capturing the
        # subprocess command it would run. Returns the argv (list) of the mount command.
        from cas import config as C
        from unittest import mock
        captured = {}
        def fake_run(args, *a, **k):
            captured["argv"] = args
            class R: returncode = 0; stdout = ""; stderr = ""
            return R()
        reach = iter([False, True])    # not reachable before, reachable after (so it returns True)
        with mock.patch.object(C.sys, "platform", platform), \
             mock.patch.object(C, "get_nas_credentials", lambda: ("u", "p w")), \
             mock.patch.object(C, "nas_reachable", lambda timeout=1.5: True), \
             mock.patch.object(C, "library_reachable", lambda: next(reach)), \
             mock.patch.object(C.subprocess, "run", fake_run), \
             mock.patch.object(C.pathlib.Path, "mkdir", lambda self, **kw: None):
            C.nas_connect()
        return captured.get("argv")

    def test_nas_connect_macos_mounts_share(self):
        argv = self._connect_cmd("darwin")
        self.assertEqual(argv[0], "mount_smbfs")
        self.assertTrue(any("01%20GAMECOVE" in str(x) for x in argv))   # share, URL-encoded
        self.assertTrue(any(str(x).endswith("/Volumes/01 GAMECOVE") for x in argv))

    def test_nas_connect_linux_mounts_share_not_subpath(self):
        argv = self._connect_cmd("linux")
        self.assertEqual(argv[:2], ["gio", "mount"])
        self.assertEqual(argv[2], "smb://192.168.100.227/01%20GAMECOVE")  # share only, no subpath
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_cas.TestConfig.test_nas_connect_macos_mounts_share tests.test_cas.TestConfig.test_nas_connect_linux_mounts_share_not_subpath -v`
Expected: FAIL — macOS branch doesn't exist (no `mount_smbfs` command captured); Linux builds `smb://…/01 GAMECOVE/[03] SETUP/CAS Profiles` (the full subpath from `NAS_DEFAULT`), not the share root.

- [ ] **Step 3: Implement** — replace the `else:` (non-Windows) branch of `nas_connect` (currently lines ~355-358)

Current:
```python
        else:
            url = "smb://" + NAS_DEFAULT.lstrip("\\").replace("\\", "/")
            subprocess.run(["gio", "mount", url], input=f"{user}\n\n{pw}\n",
                           text=True, capture_output=True, timeout=timeout)
        return library_reachable()
```

Replace with:
```python
        elif sys.platform == "darwin":
            from urllib.parse import quote
            mp = pathlib.Path("/Volumes") / nas_share_name()
            mp.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["mount_smbfs",
                 f"//{quote(user)}:{quote(pw)}@{nas_host()}/{quote(nas_share_name())}", str(mp)],
                capture_output=True, text=True, timeout=timeout)
        else:
            from urllib.parse import quote
            url = f"smb://{nas_host()}/{quote(nas_share_name())}"   # mount the SHARE; discovery appends subpath
            subprocess.run(["gio", "mount", url], input=f"{user}\n\n{pw}\n",
                           text=True, capture_output=True, timeout=timeout)
        return library_reachable()
```

`[VERIFY on macOS]` exact `mount_smbfs` URL/encoding and that the mountpoint is empty/creatable; `[VERIFY on Linux]` `gio mount` of the share URL lands at the gvfs path Task 2 computes.

- [ ] **Step 4: Run to verify they pass + the whole suite is green**

Run: `python -m unittest discover -s tests -q`
Expected: `OK` (now ~200 tests). Then: `python -m unittest tests.test_cas.TestConfig -v` — all NAS tests pass.

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(config): nas_connect adds macOS mount_smbfs; mounts the share on Linux"
```

---

## Self-Review

**Spec coverage:**
- §3 parse spec (share/subpath) → Task 1. ✓
- §4 cross-OS `nas_connect` (mac/linux/win) → Task 5 (Windows unchanged). ✓
- §5 `nas_mountpoint()` discovery → Task 2 (refined: gvfs FUSE-path existence check instead of `gio mount -l` scrape — noted in Task 2). ✓
- §6 resolvers follow discovery; firmware/log override honored only-if-exists → Tasks 3 (nas_default_path/library_root) + 4 (firmware_dir). log_dir already has the rule (`history_dir`, config.py:91-97) — no change needed. ✓
- §7 offline → local fallback → Task 3 (library_root None-safe). ✓
- §8 best-effort, never raises → existing `nas_connect` try/except retained; `nas_mountpoint` wrapped in try/except. ✓
- §9 testing (per-OS discovery, resolution, stale-override-ignored, command construction) → Tasks 2,3,4,5. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; the only `[VERIFY on <os>]` markers are on the live mount commands in Task 5, as the spec dictates. ✓

**Type/name consistency:** `nas_share_name`, `nas_subpath`, `nas_mountpoint`, `nas_default_path` used identically across Tasks 1-5; `nas_mountpoint() -> str|None` consumed by `nas_default_path()` (Task 3) matches its definition (Task 2); `nas_default_path() -> str|None` consumed by `library_root()` (Task 3) matches. Test env-key hygiene (`XDG_RUNTIME_DIR` added to `setUp`) is set in Task 2 before Task 2's test uses it. ✓

## Out of scope (separate follow-up)
Two ship-as-is game-launcher review minors the operator asked to also clean up are NOT in this plan (different feature/file): `gl_restore`'s `shared_prefs` restorecon `|| warn` symmetry and the dead `uid=` field in `gl_capture`'s meta (`provision/root/lib-root.sh`). Handle as a separate small TDD commit after this plan.
