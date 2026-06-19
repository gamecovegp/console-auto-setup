# NAS-Backed Profile Library — Design

**Goal:** Let the CAS golden library live on the office NAS (SMB) instead of each PC's local disk, so any
provisioning PC shares one central library — add / update / delete a golden once and every station sees it.

**Status:** Approved 2026-06-19. Approach 1 ("library on a mounted SMB share; the OS owns the login").

---

## 1. Architecture

CAS already treats its profile library as a plain folder (`profiles/<name>/` = `profile.meta` + `manifest`
+ `golden_root_payload/`). The whole feature is: **make that folder's location configurable** and point it
at the NAS share. The NAS copy is the single source of truth; the OS mounts the SMB share so credentials
never live in CAS.

```
  NAS (SMB 192.168.100.227)                 each provisioning PC
  \01 GAMECOVE\[03] SETUP\CAS Profiles\  ◄── mapped drive (Z:) ──►  CAS  (library path = Z:\…\CAS Profiles)
        odin2mini\ …                                                  New / Delete / Capture / Download
        mangmi-airx\ …                                                operate directly on the NAS library
        _archive\ …
```

- **Mount:** Windows *Map network drive* (reconnect at sign-in + saved credentials) or Linux cifs mount.
  Out of CAS scope — CAS only needs the resulting path.
- **CAS:** resolves its library root from config and uses it everywhere `profiles/` was used.
- **Toolkit (app + `provision/` scripts + `retroarch-cores/` + `windows-kit/` firmware/Magisk):** stays
  LOCAL on each PC (the profile.meta paths for firmware/cores resolve off `APPDIR`, not the library). A new
  PC copies the toolkit once from the NAS `CAS Toolkit\` folder, then maps the profiles. Only the dynamic,
  bulky goldens are centralized.

## 2. Storage layout (created on the NAS under `[03] SETUP\`)

```
[03] SETUP\
├── CAS Profiles\            ← CAS library root (point CAS here)
│   ├── odin2mini\           ← profile.meta + manifest + golden_root_payload\ + golden_root_payload.prev\
│   ├── <other devices>\
│   └── _archive\            ← Delete moves profiles here (soft-delete, never rm)
├── CAS Toolkit\            ← app + fixed assets for setting up a new PC (cas\, provision\, windows-kit\,
│   └── …                      retroarch-cores\, build/run scripts) — copied LOCAL per PC
└── SD Master Images\       ← optional: dd images of master ROM/game cards (the SD bulk-data layer)
```

CAS's configured library root = `…\[03] SETUP\CAS Profiles`. CAS reads/writes only inside `CAS Profiles\`
(incl. its `_archive\`). It does not touch `CAS Toolkit\` or `SD Master Images\`.

## 3. Components

### 3.1 `cas/config.py` (new) — library-path resolution + persisted settings
- `library_root() -> pathlib.Path` — resolves the profile library in priority order:
  1. `CAS_PROFILES` env var (override for scripts/CI),
  2. `library` key in the config file,
  3. default `APPDIR / "profiles"` (current behavior — nothing breaks if unconfigured).
- `config_path() -> pathlib.Path` = `APPDIR / "cas-config.json"` (next to the app/exe; writable).
- `load_config() -> dict` / `save_config(dict)` — tiny JSON read/write; tolerate a missing/corrupt file
  (return `{}`).
- `set_library(path: str)` — write `{"library": path}` to the config file; returns the resolved Path.

### 3.2 Consume `library_root()` everywhere `APPDIR / "profiles"` is used
- `cli.py`: `PROOT = str(library_root())`; add `--library PATH` (one-shot override; does not persist).
- `gui.py`: `self.profiles_root = str(library_root())`.
- Defaults in `profiles.list_profiles/match_profile` and `provision.*` already take a `root=`/`profiles_root=`
  param — the callers just pass the resolved root. No change to those signatures.

### 3.3 GUI "Library…" control
- A button (next to the Profile picker) opening a small dialog: shows the current library path, lets the
  user **Browse…** (a folder picker → the mapped drive) or paste a path, and **Save** (→ `set_library`).
- On save: re-point `self.profiles_root`, call `refresh_profiles()`, and log the new location.
- An **"Initialize library"** action: if the chosen folder is empty/new, create `CAS Profiles\` structure
  (the folder itself + `_archive\`) so a fresh NAS share is ready.
- The window title or a status line shows the active library (so it's obvious you're on the NAS vs local).

### 3.4 Reachability / error handling
- On startup and on every `refresh_profiles()`: if `library_root()` does not exist or isn't a directory,
  show a clear, non-fatal message ("Library not reachable: <path> — is the NAS drive mapped?") and present
  an empty profile list. Never crash; let the user open Library… to fix the path or map the drive.
- Capture/Download/Delete guard: if the library path is unreachable at action time, refuse with the same
  clear message (don't half-write to a dropped share).

## 4. Data flow (unchanged except the root)

- **List:** `list_profiles(library_root())` → dropdown. (Reads small `.meta`/`manifest` over SMB — fast.)
- **Download:** reads `…/CAS Profiles/<name>/golden_root_payload/` over the LAN, pushes to the device. (The
  ~7 GB read is LAN-speed; acceptable. Local cache is explicitly deferred — see §6.)
- **Capture:** writes the new payload into `…/CAS Profiles/<name>/` on the NAS (verify-before-rotate already
  keeps `.prev`; that logic is unchanged, just on the NAS path).
- **Delete:** `archive_profile` moves `<name>/` → `_archive/<name>_<stamp>/` on the NAS.

## 5. Testing

- `config`: env > config-file > default precedence; `set_library` round-trips; missing/corrupt config → `{}`
  and the default root.
- `library_root()` returns the env path when set, else the config path, else `APPDIR/profiles`.
- `list_profiles` against a temp "library" dir (already covered) — confirms the indirection is transparent.
- Unreachable-library handling: `refresh_profiles` with a non-existent root logs the message and yields an
  empty list (GUI logic test via the existing injectable patterns; no real SMB needed).
- All existing tests stay green (the change is additive; defaults preserve current behavior).

## 6. Out of scope (deferred — no rework needed to add later)

- **Local cache / pull-push (Approach 3):** if repeated 7 GB LAN reads drag, add a cache that mirrors the
  selected golden locally before Download. The configurable-root design doesn't preclude it.
- **CAS-managed SMB login (Approach 2):** the OS maps the drive; CAS stores no credentials.
- **Moving the Toolkit (cores/firmware) onto the NAS at runtime:** stays local; only profiles are centralized.
- **Creating the NAS folders from here:** I can run it once the share is mounted with credentials.
