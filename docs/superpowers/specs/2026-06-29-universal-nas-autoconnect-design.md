# Universal NAS auto-connect (connect → discover → resolve)

- **Date:** 2026-06-29
- **Status:** Design — awaiting review
- **Area:** `cas/config.py` (path resolution + connect + discovery), `tests/test_cas.py`. No change to profile/firmware/log I/O elsewhere.

## 1. Background

CAS stores its shared library (profiles, firmware catalog, run logs) on the GameCove NAS SMB share `\\192.168.100.227\01 GAMECOVE`, under `[03] SETUP\CAS Profiles`. CAS already ships NAS credentials (a saved account + a shipped default `console-auto-setup` app account, `config.get_nas_credentials()`) and already calls `nas_connect()` at GUI startup so a fresh bench auto-connects with no manual drive-mapping.

**The bug (Linux, observed 2026-06-29):** the firmware list — and the whole NAS library — is empty even though the NAS is up. Root cause is an OS mismatch:

- `nas_default_path()` returns a **hardcoded** POSIX mountpoint `NAS_DEFAULT_POSIX = "/mnt/gamecove/[03] SETUP/CAS Profiles"` (config.py:25,28-30).
- `nas_connect()` on non-Windows runs `gio mount smb://…` (config.py), which mounts into **gvfs** (`/run/user/<uid>/gvfs/smb-share:…`), **not** `/mnt/gamecove`.
- So even after a successful `gio mount`, `library_reachable()` (which tests the `/mnt/gamecove` path) stays false → `library_root()` silently falls back to the **local** profiles dir (this is why profiles still work locally), and `firmware_dir()` — which has **no local fallback** by design (config.py:113-119) and was pinned via an explicit `firmware_dir` config override to `/mnt/gamecove/…` — resolves to a dead path → empty list.

On Windows `net use` maps the UNC and `nas_default_path()` returns the UNC, so it works there; Linux auto-connect was never wired to land on the path the resolver uses, and macOS was never implemented.

**Operator intent (clarified):** auto-connect should be **universal** — CAS authenticates with the pre-configured credentials and makes the library available **on every OS** (Windows/macOS/Linux) with no manual mount, no `fstab`, no sudo. Not a Linux-only patch.

## 2. Goals / Non-goals

**Goals**
- A **connect → discover → resolve** model: CAS connects the share itself per-OS (userspace, using the stored creds), discovers where the share actually mounted, and resolves the library path relative to that discovered root — replacing the hardcoded `/mnt/gamecove` assumption.
- Works on **Windows, macOS, and Linux** with the credentials CAS already has.
- The firmware catalog and run logs **follow discovery** so they populate once connected, with no per-bench config surgery.
- Graceful offline behavior: local fallback + the existing "library unreachable" warning (no silent empty list).

**Non-goals**
- No mount-free direct-SMB I/O layer (rejected as over-scoped — CAS keeps using filesystem paths).
- No change to how profiles/firmware/logs are read or written once a path is resolved.
- No new third-party dependency (uses each OS's built-in userspace mount tool).
- No `fstab`/systemd/sudo setup on any bench.

## 3. NAS spec — parse the single source into parts

`NAS_DEFAULT = r"\\192.168.100.227\01 GAMECOVE\[03] SETUP\CAS Profiles"` already encodes everything. Add helpers (alongside the existing `nas_host()`/`nas_share_root()`) so connect and discovery can target the share and the resolver can append the subpath:

- `nas_host()` → `192.168.100.227` (exists).
- `nas_share_name()` — NEW → `01 GAMECOVE` (the share, first UNC segment after host).
- `nas_subpath()` — NEW → `[03] SETUP/CAS Profiles` (everything after the share, POSIX-separated).
- `get_nas_credentials()` → `(user, pw)` from the saved account else the shipped default (exists).

## 4. `nas_connect()` — cross-OS (extend the existing function)

Keep the existing contract (idempotent; fast-skip via `library_reachable()` and `nas_reachable()`; returns whether the library is reachable afterwards). Per platform, authenticate the share with `get_nas_credentials()`:

- **Windows:** `net use \\host\share pw /user:user /persistent:yes` (existing; unchanged).
- **macOS:** create the mountpoint and `mount_smbfs //user:pw@host/<share> /Volumes/<share>` (userspace, no sudo). `<share>` URL-encoded. `[VERIFY on macOS]` exact arg form / encoding.
- **Linux:** `gio mount smb://host/<share>` with `user\n\npw\n` on stdin (existing mechanism; now we mount the **share**, and §5 discovers where it landed). `[VERIFY on Linux]`.
- After the platform branch, return `library_reachable()` (which is now discovery-aware — §5/§6).

All connect attempts are best-effort: failure returns False, never raises.

## 5. `nas_mountpoint()` — NEW: discover the share's local path

Returns the local filesystem path of the **share root** on this OS (the dir to which `nas_subpath()` is appended), or `None` if the share isn't mounted. Never hardcodes a path.

- **Windows:** the UNC `\\host\share` (usable directly after `net use`); return it if it resolves.
- **macOS:** `/Volumes/<share>` if it exists.
- **Linux:** parse `gio mount -l` for the SMB mount matching `host`/`share`; return its `/run/user/<uid>/gvfs/smb-share:server=<host>,share=<share-lower>` local path as reported by gio (discovered, not computed). `[VERIFY on Linux]` exact `gio mount -l` field format.

Discovery is read-only and cheap; callers tolerate `None`.

## 6. Path resolution (rewrite the resolvers)

- `nas_default_path()` → `nas_mountpoint()` + `nas_subpath()` when a mountpoint is discovered, else `None`. **Replaces** the hardcoded `NAS_DEFAULT`/`NAS_DEFAULT_POSIX` return. (Windows still effectively yields the UNC+subpath via the UNC mountpoint.)
- `library_root()` — unchanged priority, NAS path now discovered: `CAS_PROFILES` env > config `library` > `nas_default_path()` (if it exists) > local `APPDIR/data/profiles`.
- `firmware_dir()` and the `log_dir` override consumer: derive from `library_root()` by default (`library_root()/_firmware` for firmware). An explicit config override (`firmware_dir`/`log_dir`) is honored **only if that path currently exists** — so a stale NAS-pinned override (e.g. the field's current `/mnt/gamecove/…`, now non-existent) is **auto-ignored** and the path follows discovery. No config rewrite needed; the user's existing stale overrides stop mattering the moment the resolver checks existence.
- `library_reachable()` → `nas_default_path()` is not None **and** is a directory (discovery-aware).

## 7. Startup & offline

GUI startup already calls `nas_connect()` (gui.py). New flow: connect (per-OS) → `nas_mountpoint()` discovers the path → resolvers return the discovered NAS root → firmware/profiles/logs populate. When the NAS is off-network or connect fails: `nas_default_path()` is `None` → `library_root()` falls back to local, `firmware_dir()` → `library_root()/_firmware` (local, typically empty), and the existing library-unreachable warning explains the empty state. No silent blank.

## 8. Error handling

- Every connect mechanism is best-effort: any failure (no creds, NAS off-network, tool missing, mount error) returns False; CAS proceeds on the local library. Never raises into the GUI.
- `nas_mountpoint()` returns `None` on any parse/availability failure; resolvers treat `None` as "not connected" → local fallback.
- Stale absolute overrides that don't exist are ignored (§6), not errors.

## 9. Testing

Unit tests (`tests/test_cas.py`, mockable `sys.platform`, no real network):
- `nas_share_name()`/`nas_subpath()` parse `NAS_DEFAULT` correctly (share = `01 GAMECOVE`, subpath = `[03] SETUP/CAS Profiles`).
- `nas_mountpoint()` per OS: Windows → UNC; macOS → `/Volumes/<share>` when present (else None); Linux → the path parsed from a **faked `gio mount -l`** output (else None). Inject the command runner / `os.path.exists` so no real mount is touched.
- `nas_default_path()` = discovered mountpoint + subpath; `None` when not mounted.
- `library_root()` picks the discovered NAS root when present, local when `nas_mountpoint()` is None (priority preserved).
- `firmware_dir()` follows `library_root()`; an explicit override is used only when it exists, and a **non-existent NAS-pinned override is ignored** (the exact regression behind the empty firmware list).
- `library_reachable()` true only when the discovered path is a directory.
- All existing tests stay green.

Live per-OS mount commands carry `[VERIFY on <os>]` markers — the path-resolution + discovery-parsing logic is unit-tested; the actual `mount_smbfs`/`gio mount`/`net use` round-trip is verified on each OS during rollout.

## 10. Scope note

Single focused change confined to `cas/config.py` + `tests/`. The connect (§4) and discovery (§5) are independently testable from the resolvers (§6); the resolvers are the behavioral core and carry the regression coverage.
