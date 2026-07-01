# CAS Local-Only Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CAS a purely local tool — remove all NAS/SMB code and resolve the golden library from a local/external drive, with per-machine run-history logs that survive copy-paste sync between benches.

**Architecture:** `library_root()` drops its NAS branch and resolves `CAS_PROFILES` env → `library` config → `APPDIR/data/profiles`. The GUI loses its NAS login / auto-connect / NAS-flavored labels. Run-history filenames get a per-machine suffix so two benches never write the same file. `firmware_dir`/`apk_store_dir`/`history_dir` are unchanged in logic and keep following the library root.

**Tech Stack:** Python 3 stdlib only (`tkinter`, `socket`, `re`, `pathlib`, `json`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-01-cas-local-only-library-design.md`

## Global Constraints

- Work in the isolated worktree on branch `refactor/local-only-library`. All commands below run from the worktree root.
- No new dependencies. Python-3 stdlib only. Match the surrounding code's comment density and style.
- Library resolution is **manual set-only**: `CAS_PROFILES` env → `library` config → `APPDIR/data/profiles`. No auto-discovery, no NAS.
- **Keep** `adb.py`'s `_staged_exec` — an external FAT/exFAT drive is noexec too; only its comments change.
- Run-history logs are **write-only** (nothing reads them back); only writers change.
- Test runner: `python3 -m pytest tests/ -q` from the worktree root. Suite must be green after every task.
- Do NOT commit `cas-config.json` (gitignored). Task 5 is a local machine setup step, not a code commit.

---

### Task 1: Per-machine run-history filenames

**Files:**
- Modify: `cas/config.py` (add `machine_tag`, `history_filename` after `download_mbps`/before `library_reachable`)
- Modify: `cas/provision.py` (`_append_history` param + path; two callers)
- Modify: `cas/firmware.py:500` (`log_event` filename)
- Test: `tests/test_cas.py` (`test_download_run_logged_to_library`, `test_append_history_writes_jsonl`, `test_append_history_routes_to_log_dir`, + new tests), `tests/test_firmware.py:358` (`test_log_event_appends_jsonl`)

**Interfaces:**
- Produces: `config.machine_tag() -> str` (sanitized lowercased hostname, `"unknown"` if empty); `config.history_filename(stem: str) -> str` returning `f"{stem}.{machine_tag()}.jsonl"`.
- Consumes: `provision._append_history(root, stem, rec, log, summary)` now takes a **stem** (e.g. `"download-history"`), not a full filename.

- [ ] **Step 1: Write failing tests for the new helpers**

Add to `tests/test_cas.py` (in the config test class — put next to `test_library_reachable`):

```python
    def test_machine_tag_sanitizes_hostname(self):
        from cas import config as C
        from unittest import mock
        with mock.patch("socket.gethostname", return_value="Bench 01/Room#2"):
            self.assertEqual(C.machine_tag(), "bench-01-room-2")
        with mock.patch("socket.gethostname", return_value=""):
            self.assertEqual(C.machine_tag(), "unknown")

    def test_history_filename_shape(self):
        from cas import config as C
        from unittest import mock
        with mock.patch.object(C, "machine_tag", lambda: "bench-01"):
            self.assertEqual(C.history_filename("download-history"),
                             "download-history.bench-01.jsonl")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_cas.py -k "machine_tag or history_filename_shape" -v`
Expected: FAIL — `AttributeError: module 'cas.config' has no attribute 'machine_tag'`.

- [ ] **Step 3: Implement the helpers in `cas/config.py`**

Insert before `def library_reachable():` (both `re` and `socket` are already imported at the top of the module):

```python
def machine_tag():
    """A filesystem-safe per-machine tag (the sanitized hostname) used to namespace the run-history logs, so
    multiple benches that sync the library by whole-directory copy-paste never clobber each other's
    (write-only) audit logs. Lowercased; any run of non-[A-Za-z0-9._-] -> '-'; 'unknown' if empty."""
    try:
        raw = socket.gethostname() or ""
    except OSError:
        raw = ""
    tag = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.").lower()
    return tag or "unknown"


def history_filename(stem):
    """`<stem>.<machine_tag>.jsonl` — the per-machine run-history filename (copy-paste-safe across benches)."""
    return f"{stem}.{machine_tag()}.jsonl"
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_cas.py -k "machine_tag or history_filename_shape" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Rewire the writers to pass a stem**

In `cas/provision.py`, `_append_history` (around line 570): rename the `fname` parameter to `stem` and build the filename via `config`:

```python
def _append_history(root, stem, rec, log=print, summary=""):
    """Append ONE JSON-line record to <history_dir>/<stem>.<machine>.jsonl — the per-machine run history.
    Namespaced by machine so benches syncing the library by copy-paste never clobber each other's logs.
    Destination is the configured+reachable `log_dir` override else the library root (`root`). Best-effort:
    a write failure WARNS, never aborts; the summary shows WHERE it landed."""
    import json
    from . import config
    dest = config.history_dir(default=root)
    path = pathlib.Path(dest) / config.history_filename(stem)
```

Update the two callers (leave the appended `→ {path}` to report the real name):
- Line ~614: `_append_history(root, "download-history.jsonl", rec, log,` → `_append_history(root, "download-history", rec, log,` and change its `summary=(f"download run logged → download-history.jsonl: {len(devs)} device(s), "` to `summary=(f"download run logged: {len(devs)} device(s), "`.
- Line ~708: `_append_history(root, "save-history.jsonl", {` → `_append_history(root, "save-history", {` and change its `summary=f"save logged → save-history.jsonl: {name} ..."` to `summary=f"save logged: {name} ({b // 1048576} MB)"`.

In `cas/firmware.py`, `log_event` (line ~500):

```python
        p = pathlib.Path(config.history_dir()) / config.history_filename("firmware-history")
```

- [ ] **Step 6: Update the existing history tests to the per-machine filename**

`tests/test_cas.py` — `test_download_run_logged_to_library`: add `from cas import config as C` inside the `with`, and change
`hist = pathlib.Path(t) / "download-history.jsonl"` → `hist = pathlib.Path(t) / C.history_filename("download-history")`.

`test_append_history_writes_jsonl`: add `from cas import config as C`, change both
`PV._append_history(t, "save-history.jsonl", ...)` → `PV._append_history(t, "save-history", ...)` and
`lines = (pathlib.Path(t) / "save-history.jsonl").read_text()...` → `lines = (pathlib.Path(t) / C.history_filename("save-history")).read_text()...`.

`test_append_history_routes_to_log_dir`: rename var `nas` → `alt`, drop the "NAS" wording in the comment, and update the four references:
```python
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            lib = pathlib.Path(t) / "lib"; lib.mkdir()
            alt = pathlib.Path(t) / "alt"; alt.mkdir()
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            try:
                C.set_log_dir(str(alt))
                PV._append_history(str(lib), "download-history", {"ok": 1}, log=lambda m: None)
                self.assertTrue((alt / C.history_filename("download-history")).exists())   # log_dir override
                self.assertFalse((lib / C.history_filename("download-history")).exists())  # NOT the library root
                C.set_log_dir(str(pathlib.Path(t) / "gone"))                               # unreachable -> fallback
                PV._append_history(str(lib), "save-history", {"ok": 1}, log=lambda m: None)
                self.assertTrue((lib / C.history_filename("save-history")).exists())       # fell back to library root
            finally:
                os.environ.pop("CAS_CONFIG", None)
```

`tests/test_firmware.py` — `test_log_event_appends_jsonl` (line ~358):
`p = pathlib.Path(C.history_dir()) / "firmware-history.jsonl"` → `p = pathlib.Path(C.history_dir()) / C.history_filename("firmware-history")`.

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (all green).

- [ ] **Step 8: Commit**

```bash
git add cas/config.py cas/provision.py cas/firmware.py tests/test_cas.py tests/test_firmware.py
git commit -m "feat(logs): per-machine run-history filenames (copy-paste-safe across benches)"
```

---

### Task 2: Strip NAS from the GUI

**Files:**
- Modify: `cas/gui.py` — `_profile_library_label` (34), menu (208), `__init__` (165), `_open_library` (350), `_about` (451), `choose_log_dir`/`choose_firmware_dir` titles (369/385), `choose_library` initialdir (415), `_update_lib_label` (1970); DELETE `_nas_autoconnect` (503) and `nas_login_dialog` (513).
- Test: `tests/test_cas.py` — the `_profile_library_label` tests (~2880–2900).

**Interfaces:**
- Produces: `_profile_library_label(root, reachable) -> str` (drops `has_override`, `local_fallback`, `nas_default`).
- After this task the GUI references **no** `config` NAS symbol (`NAS_DEFAULT`, `nas_default_path`, `get_nas_credentials`, `nas_connect`, `set_nas_credentials`), so Task 3 can delete them.

- [ ] **Step 1: Rewrite the label tests (failing)**

In `tests/test_cas.py`, replace `test_nas_default_dropped_shows_unreachable_and_fallback`, `test_nas_mounted_shows_reachable`, and `test_explicit_override_reachable` with:

```python
    def test_library_reachable_shows_ok(self):
        out = self.label("/mnt/ext/CAS Profiles", reachable=True)
        self.assertEqual(out, "Library: /mnt/ext/CAS Profiles   ✓")

    def test_library_unreachable_shows_unplugged(self):
        out = self.label("/mnt/ext/CAS Profiles", reachable=False)
        self.assertIn("✗", out)
        self.assertIn("/mnt/ext/CAS Profiles", out)
        self.assertIn("unplugged", out)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_cas.py -k "library_reachable_shows_ok or library_unreachable_shows_unplugged" -v`
Expected: FAIL — `_profile_library_label()` still requires the old keyword args (`TypeError`).

- [ ] **Step 3: Simplify `_profile_library_label`**

Replace the function (lines ~34–45) with:

```python
def _profile_library_label(root, reachable):
    """'Library: …' status line for the profile library, with a reachability marker (the drive may be
    unplugged/unmounted)."""
    root = str(root)
    if not reachable:
        return f"Library: {root}   ✗ not reachable (external drive unplugged?)"
    return f"Library: {root}   ✓"
```

- [ ] **Step 4: Update `_update_lib_label` (line ~1970)**

```python
    def _update_lib_label(self):
        self.lib_var.set(_profile_library_label(self.profiles_root, self._lib_reachable()))
```

- [ ] **Step 5: Remove NAS auto-connect + login**

- In `__init__` (line ~165) delete the line `self._nas_autoconnect()                 # log into the NAS ...`.
- In `_build_menu` (line ~207–209) delete `setm.add_command(label="NAS login…", command=self.nas_login_dialog)` and the extra `setm.add_separator()` immediately above it (keep exactly one separator before "Release selected unit…").
- Delete the whole `_nas_autoconnect` method (~503–511) and the whole `nas_login_dialog` method (~513–556).

- [ ] **Step 6: De-NAS `_open_library`, `_about`, and the dir pickers**

`_open_library` (~350) — open the active library only:

```python
    def _open_library(self):
        """Open the active library folder in the OS file manager."""
        target = str(self.profiles_root)
        if not self._open_path(target):
            messagebox.showwarning(
                "CAS",
                f"Couldn't open a file manager for:\n{target}\n\n"
                "Open it manually in your file manager (paste the path above).")
```

`_about` (~451) — drop the NAS branch and its import:

```python
    def _about(self):
        from .config import load_config
        p = str(self.profiles_root)
        if os.environ.get("CAS_PROFILES") or load_config().get("library"):
            where = "configured library"
        elif p == str(APPDIR / "data" / "profiles"):
            where = "local default"
        else:
            where = ""
```

`choose_library` (~413–415) — initialdir no longer calls `nas_default_path()`; and update the cancel message:
```python
        d = filedialog.askdirectory(
            title="Profile/golden library folder — e.g. the external drive '…/CAS Profiles'  (Cancel to clear)",
            initialdir=(cur or str(APPDIR / "data")))
```
and change the cancel `messagebox.askyesno` body from "The library will follow the NAS when it's mounted (local fallback only when offline)." to "The library falls back to the local default (APPDIR/data/profiles)."

`choose_log_dir` (~375) title → `"Run-history log folder — a local/shared folder for download/save logs  (Cancel to clear)"`.
`choose_firmware_dir` (~390) title → `"Firmware library folder — a local/external folder for device root firmware  (Cancel to clear)"`.

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS. Then sanity-check no GUI→config NAS refs remain:
Run: `grep -nE "NAS_DEFAULT|nas_default_path|nas_connect|get_nas_credentials|set_nas_credentials|_nas_autoconnect|nas_login" cas/gui.py`
Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add cas/gui.py tests/test_cas.py
git commit -m "refactor(gui): remove NAS login/auto-connect and NAS-flavored library labels"
```

---

### Task 3: Remove NAS code from config; local-only `library_root`

**Files:**
- Modify: `cas/config.py` — module docstring, `library_root`, delete all NAS/SMB/credential code.
- Test: `tests/test_cas.py` — delete the NAS config tests; rewrite `test_library_root_local_when_nas_unmounted`.

**Interfaces:**
- Produces: `library_root()` = `CAS_PROFILES` env → `library` config → `APPDIR/data/profiles`.
- Removes (must be unreferenced after Task 2): `NAS_DEFAULT`, `nas_default_path`, `nas_share_root`, `nas_share_name`, `nas_subpath`, `nas_host`, `nas_mountpoint`, `_unescape_mount`, `_linux_cifs_mountpoint`, `nas_reachable`, `nas_connect`, `_OBF`, `NAS_DEFAULT_USER`, `NAS_DEFAULT_PW`, `_xor`, `set_nas_credentials`, `get_nas_credentials`.

- [ ] **Step 1: Rewrite the local-default test (failing)**

In `tests/test_cas.py` replace `test_library_root_local_when_nas_unmounted` with:

```python
    def test_library_root_local_default(self):
        from cas import config as C, APPDIR
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")
            os.environ.pop("CAS_PROFILES", None)
            self.assertEqual(C.library_root(), APPDIR / "data" / "profiles")
```

Delete these NAS test methods entirely: `test_linux_cifs_mountpoint_discovered_from_proc_mounts`, `test_linux_cifs_mountpoint_none_when_no_match`, `test_default_falls_back_to_local_when_nas_unreachable`, `test_nas_default_used_when_reachable`, `test_nas_credentials_roundtrip_and_default`, `test_nas_share_root`, `test_nas_share_name_and_subpath`, `test_nas_mountpoint_linux_gvfs`, `test_nas_mountpoint_macos_volumes`, `test_nas_default_path_follows_mountpoint`, `test_nas_connect_attempts_mount_despite_local_library`, `test_nas_connect_macos_mounts_share`, `test_nas_connect_linux_mounts_share_not_subpath`.

- [ ] **Step 2: Run to verify the new test fails**

Run: `python3 -m pytest tests/test_cas.py -k "library_root_local_default" -v`
Expected: FAIL initially only if collection breaks; more importantly the deleted-test names must be gone. Primary gate is Step 4's full run. (If `library_root` still has the NAS branch, this test can still pass — that's fine; it's a guard for the final state.)

- [ ] **Step 3: Delete the NAS code and simplify `library_root`**

Replace the module docstring (top of `cas/config.py`) with:

```python
"""Where the CAS profile library lives. Resolves the library root from (priority):
  1. CAS_PROFILES env var   (one-shot override for scripts/CI)
  2. 'library' key in cas-config.json   (set via the GUI 'Library folder…' picker)
  3. APPDIR/data/profiles   (local default)
The library is a local/external drive folder — set it once per bench (Settings -> Library folder)."""
```

Replace `library_root` with:

```python
def library_root():
    """The profile library directory: CAS_PROFILES env > config 'library' > local (APPDIR/data/profiles)."""
    env = os.environ.get("CAS_PROFILES")
    if env:
        return pathlib.Path(env)
    lib = load_config().get("library")
    if lib:
        return pathlib.Path(lib)
    return APPDIR / "data" / "profiles"
```

Delete `NAS_DEFAULT` (const) and `nas_default_path`. Delete the entire NAS auth/mount block: `_OBF`, `NAS_DEFAULT_USER`, `NAS_DEFAULT_PW`, `_xor`, `set_nas_credentials`, `get_nas_credentials`, `nas_share_root`, `nas_share_name`, `nas_subpath`, `nas_mountpoint`, `_unescape_mount`, `_linux_cifs_mountpoint`, `nas_host`, `nas_reachable`, `nas_connect`. KEEP `get_release_token`/`RELEASE_TOKEN_DEFAULT`, `library_reachable`, `firmware_dir`, `apk_store_dir`, `history_dir` (drop any "NAS" wording from their docstrings — e.g. `history_dir` "(e.g. the NAS…)" → "(e.g. a shared folder)", `firmware_dir`/`apk_store_dir` "stale NAS-pinned override" → "stale override").

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS. Then:
Run: `grep -rnE "nas|NAS|smb|SMB|cifs|192\.168\.100\.227" cas/config.py`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "refactor(config): remove all NAS/SMB code; library_root resolves local-only"
```

---

### Task 4: De-NAS the remaining wording (warnings, adb comments, scripts)

**Files:**
- Modify: `cas/warnings.py:117-121` (`library_unreachable` detail/fix)
- Modify: `cas/adb.py` (comments at ~468, ~483, ~488, ~517)
- Modify: `scripts/build-win.bat:57-61`, `scripts/update.sh:15,51`, `scripts/update-win.bat:15,57`

**Interfaces:** none (text only). `test_warnings.py` asserts on the code `"library_unreachable"`, not the text, so it stays green.

- [ ] **Step 1: Update the warning text (`cas/warnings.py`)**

```python
    "library_unreachable": {
        "title": "library folder not reachable",
        "detail": "The profile library path isn't a reachable directory (external drive unplugged?). "
                  "Download and Save need it.",
        "fix": "Set Settings → Library folder… to the drive, then click 'Refresh devices'.",
```

- [ ] **Step 2: Update the `adb.py` staging comments**

At the `_staged_exec` docstring/comments (~468, ~483, ~488, ~517) replace "the NAS mount is noexec" / "the firmware library lives on a CIFS/NAS" / "the NAS copy is noexec" with wording like "the library drive may be noexec (CIFS or a removable FAT/exFAT drive), so tools/images are staged to a writable LOCAL dir before exec". Comments only — no logic change.

- [ ] **Step 3: Update the shipping scripts**

`scripts/build-win.bat` (~57–61): replace the "library now DEFAULTS to the NAS (\\192.168.100.227\…): use Settings -> NAS login… once and every PC shares it." block with:
```
echo   1) profiles\      -- OPTIONAL. The library is a local/external "CAS Profiles"
echo                        folder set once via Settings -^> Library folder...
echo                        (a local profiles\ is only the fallback when unset).
```
`scripts/update.sh` (~15, ~51): change the "golden library = NAS when mounted; else local profiles/" notes to "golden library = the folder set in Settings -> Library folder (else local profiles/)".
`scripts/update-win.bat` (~15, ~57): same substitution ("Settings -> Library folder", drop "NAS login").

- [ ] **Step 4: Run the full suite + final NAS grep**

Run: `python3 -m pytest tests/ -q`
Expected: PASS.
Run: `grep -rnE "nas|NAS|smb|SMB|cifs|net use|gio mount|192\.168\.100\.227" cas/`
Expected: only intentional matches (ideally none; a stray "canvas"-style substring is fine — verify each hit is not NAS logic).

- [ ] **Step 5: Commit**

```bash
git add cas/warnings.py cas/adb.py scripts/build-win.bat scripts/update.sh scripts/update-win.bat
git commit -m "docs: drop NAS wording from warnings, adb comments, and shipping scripts"
```

---

### Task 5: Point THIS bench at the external drive (local setup — not committed)

**Files:**
- Modify: `cas-config.json` (gitignored; local machine state only)

**Interface:** none. This is the operator switch that makes this box read the drive at full speed. Do it after Tasks 1–4 are green.

- [ ] **Step 1: Confirm the drive path**

Run: `ls "/run/media/ccvisionary/6045-F51C/CAS Profiles"`
Expected: `_apks  _archive  _firmware  mangmi-air-x-256  retroid-pocket-6-512  *-history.jsonl`.

- [ ] **Step 2: Rewrite `cas-config.json` — strip NAS creds, clear NAS pins, set the local library**

Edit `cas-config.json` (repo root): remove the `nas_user` and `nas_pw` keys; remove the `firmware_dir` and `log_dir` keys (they currently point at `/mnt/gamecove/...` — cleared, they follow `library`); add:
```json
"library": "/run/media/ccvisionary/6045-F51C/CAS Profiles"
```
Keep `device_profiles`, `download_stats`, `device_firmware` as-is.

- [ ] **Step 3: Verify resolution points at the drive**

Run: `python3 -c "import os; os.chdir('$PWD'); from cas import config as C; print('lib   =', C.library_root()); print('fw    =', C.firmware_dir()); print('apks  =', C.apk_store_dir()); print('logs  =', C.history_dir()); print('reach =', C.library_reachable()); print('hist  =', C.history_filename('download-history'))"`
Expected: `lib`, `fw`, `apks`, `logs` all under `/run/media/ccvisionary/6045-F51C/CAS Profiles`; `reach = True`; `hist` shows this bench's hostname suffix.

- [ ] **Step 4: (Manual, optional) GUI smoke**

Launch the GUI (`python3 -m cas` or the project's run-gui script). Confirm: Settings has **no** "NAS login…"; the Library line shows the drive path with ✓; the profile list shows `mangmi-air-x-256` and `retroid-pocket-6-512`; the Firmware tab lists the drive `_firmware`. No commit (config is gitignored).

---

## Self-Review

**Spec coverage:**
- NAS removal (code + shipped account) → Tasks 2 (gui) + 3 (config). ✓
- `library_root` = env → `library` → `APPDIR/data/profiles` → Task 3. ✓
- `firmware_dir`/`apk_store_dir`/`history_dir` follow library → unchanged logic, docstrings cleaned in Task 3. ✓
- `_staged_exec` kept, comments updated → Task 4. ✓
- Per-machine run-history filenames → Task 1. ✓
- Warnings/scripts wording → Task 4. ✓
- `cas-config.json` switch (strip creds, clear pins, set library) → Task 5. ✓
- Test rewrite (remove NAS suite, add local + per-machine tests) → Tasks 1–3. ✓

**Placeholder scan:** All code/test steps carry concrete code; script/comment edits give exact new strings. No TBD/TODO.

**Type consistency:** `machine_tag()`/`history_filename(stem)` defined in Task 1 and used consistently in provision/firmware and every updated test. `_profile_library_label(root, reachable)` defined in Task 2 and used by `_update_lib_label` + both label tests. `library_root()` signature unchanged. ✓

**Green-after-each ordering:** Task 1 is additive (green). Task 2 removes all GUI→config NAS references (green; config NAS symbols still exist, now only self-referenced). Task 3 deletes the now-unreferenced config NAS symbols (green). Task 4 is text-only (green). Task 5 is local, uncommitted.
