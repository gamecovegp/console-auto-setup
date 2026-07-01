# CAS Local-Only Library — remove the NAS

**Date:** 2026-07-01
**Status:** Design (approved direction; awaiting spec review)

## Problem

Serving the golden library from the office NAS over SMB is network-bound: an
observed 2.2 GB pull ran at ~6–10 MB/s, a hard regression versus local disk. The
decision is to drop the NAS entirely and keep the golden library on a local /
external drive.

The external drive already carries the **identical** `CAS Profiles/` layout the
NAS used — profiles as subfolders (`mangmi-air-x-256`, `retroid-pocket-6-512`),
plus `_firmware/`, `_apks/`, `_archive/`, and the `*-history.jsonl` files at the
root (mounted at `/run/media/ccvisionary/6045-F51C/CAS Profiles`). So **no
on-disk layout change is required** — CAS already reads exactly this structure.

## Goal

CAS is local-only. No SMB/NAS code, no shipped NAS account, no auto-connect. The
library is set **manually per bench** (Settings → Library folder…), defaulting to
`APPDIR/data/profiles` when unset.

## Non-goals

- Auto-discovery of removable drives — explicitly declined; manual set-only.
- Any change to the on-disk `CAS Profiles/` layout.
- Any change to the device-side capture/restore engine.

## Design

### Library resolution (`cas/config.py`)

`library_root()` priority becomes:

1. `CAS_PROFILES` env var (one-shot override for scripts/CI)
2. `library` key in `cas-config.json` (set via the GUI picker)
3. `APPDIR/data/profiles` (local fallback)

The NAS branch is removed. The dependent dirs are unchanged in logic and continue
to follow the library:

- `firmware_dir()` → explicit `firmware_dir` override *if it exists*, else `library_root()/_firmware`
- `apk_store_dir()` → explicit `apk_store` override *if it exists*, else `library_root()/_apks`
- `history_dir()` → explicit `log_dir` *if it exists*, else `library_root()`
- `library_reachable()` → `library_root().is_dir()`

(The "honored only if the path exists" rule stays — it lets an operator point a
dir at an external subfolder and gracefully ignores a stale/absent override.)

### Removals (`cas/config.py`)

Delete: `NAS_DEFAULT`, `nas_default_path`, `nas_share_root`, `nas_share_name`,
`nas_subpath`, `nas_host`, `nas_mountpoint`, `_unescape_mount`,
`_linux_cifs_mountpoint`, `nas_reachable`, `nas_connect`, `set_nas_credentials`,
`get_nas_credentials`, `_OBF`, `_xor`, `NAS_DEFAULT_USER`, `NAS_DEFAULT_PW`.
Removing the shipped account is also a security win — no near-plaintext creds in
source. `release_token` helpers stay (not NAS-related).

### GUI (`cas/gui.py`)

- Remove the **"NAS login…"** menu item and `nas_login_dialog`.
- Remove `_nas_autoconnect` and its startup call.
- `_profile_library_label(...)` — drop the `nas_default` parameter and all NAS
  text. New behavior: `Library: <root>  ✓` when reachable; `Library: <root>
  ✗ not reachable (drive unplugged?)` otherwise.
- `_open_library` / `_open_path` — drop the `smb://` / `NAS_DEFAULT` special
  casing; just open the resolved local library path in the file manager.
- Keep the Library / Firmware / Log folder pickers; update their titles/help to
  drop "NAS" (say "external / shared drive"). `choose_library` initialdir uses
  the current `library` or `APPDIR/data` (no `nas_default_path()`).

### `cas/warnings.py`

`library_unreachable` title/detail/fix drop NAS wording → e.g. detail: "The
library folder isn't a reachable directory (external drive unplugged?)", fix: "Set
Settings → Library folder… and click 'Refresh devices'."

### `cas/adb.py`

Keep `_staged_exec` — it is still required. Update its comments only: the library
drive may be **noexec** (CIFS **or** a removable FAT/exFAT drive), so bundled
tools/images stage to a local writable dir before execution.

### `cas/provision.py` / `cas/firmware.py`

The "server store" is now the local `_apks` store. Keep functional; optionally
rename user-facing log strings ("server store" → "local app store"). Low priority.

### Scripts / README

`scripts/build-win.bat`, `scripts/update.sh`, `scripts/update-win.bat`, and
`README.md` drop the "library defaults to the NAS / Settings → NAS login" notes;
state the library is a local/external `CAS Profiles` folder set via Settings →
Library folder.

### `cas-config.json` (local, gitignored — a setup step, not a committed change)

- Remove `nas_user`, `nas_pw`.
- Clear `firmware_dir` and `log_dir` (currently NAS paths) so they follow `library`.
- Set `library` = `/run/media/ccvisionary/6045-F51C/CAS Profiles`.

### Tests (`tests/test_cas.py`)

Remove the NAS suite: `test_linux_cifs_mountpoint_*`,
`test_nas_default_used_when_reachable`,
`test_default_falls_back_to_local_when_nas_unreachable`,
`test_nas_credentials_roundtrip_and_default`, `test_nas_share_root`,
`test_nas_share_name_and_subpath`, `test_nas_mountpoint_linux_gvfs`, and any
`set_library` cases pinned to `\\`/`/mnt/nas` paths (rewrite with local paths).

Add: `library_root()` resolution (env > `library` config > `APPDIR/data/profiles`);
`firmware_dir`/`apk_store_dir`/`history_dir` follow `library_root()`;
`set_library` roundtrip + clear with a local path; `library_reachable()`.

## Risks

- A leftover reference to a removed `nas_*` / `NAS_DEFAULT` symbol → import error.
  The GUI imports them lazily inside methods; those imports must be removed too. A
  final `grep -rniE 'nas|smb|cifs|net use|gio mount|192\.168\.100\.227' cas/`
  should return only intentional matches (ideally none).
- The current `cas-config.json` pins `firmware_dir`/`log_dir` to a NAS path that
  won't exist off-network. It already falls back gracefully, but we clear it to be
  clean.

## Verification

- `pytest` green with the updated suite.
- Launch the GUI: Settings has no "NAS login…"; the Library label shows the drive
  path with ✓; the profile list shows `mangmi-air-x-256` and
  `retroid-pocket-6-512`; the Firmware tab shows the drive `_firmware`; a Download
  reads from the drive at local-disk speed (no ~7 MB/s ceiling).
- `grep -rniE 'nas|smb|cifs|net use|gio mount|192\.168\.100\.227' cas/` is clean.
