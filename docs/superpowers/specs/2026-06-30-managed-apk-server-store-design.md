# Managed APK server store (central, NAS-hosted, version-managed)

**Date:** 2026-06-30
**Status:** Approved — ready for implementation plan
**Area:** `cas/config.py`, `cas/profiles.py`, `cas/provision.py`, `cas/gui.py` (on-device `provision/root/lib-root.sh` **unchanged**)

## Problem

A config (profile) can already list an app as **config-only** — `config` axis on, no
`apk` (the *cocoon* case; commit `c7673e4` made a config-only app that is absent at
deploy a WARN, not a FAIL). But there is no way, after the fact, to **attach an APK** to
such an app, or to **bump its version**, without hand-editing files in the profile
payload. APK binaries today live *inside each profile* at
`golden_root_payload/<pkg>/apk/*.apk`, so the same third-party app is duplicated across
profiles and a version bump must be repeated per profile.

The operator wants to **add** an APK to a config that doesn't have one yet, **edit**
(replace) it to update the version, and **host the APKs on the server** so management
happens in one place.

## Goal

A single **server-hosted APK store**, keyed by package, where one **current** version of
each app is the build that deploys. Adding/updating an APK is a library-wide action; a
config opts into an app per-config. The store lives on the NAS by default and reuses the
existing library-root plumbing.

## Decisions (settled with the user)

| Question | Decision |
|---|---|
| Where do added/edited APKs live? | **Central store only** — profiles reference the store; they do not carry the managed binary. |
| Store scope | **Managed APKs only.** Golden-**capture is unchanged** — captured APKs still land in `golden_root_payload/<pkg>/apk/`. The store and the payload coexist. |
| Default store location | **The server (NAS) by default**, via the existing `library_root()` resolution. |
| Version binding | **Every config, always latest.** One `current` version per app in the store; every config using the app deploys it. No per-config pinning. |
| Replaced versions | **Retained (soft-archive)** — never hard-deleted (mirrors `archive_profile`). Only `current` deploys. |
| Kit APKs (Magisk + GameCove Companion) | **Also move into the store**, but keep a **CAS-bundle fallback** (`data/Apps/…`) so rooting/provisioning still works with the NAS offline. |
| GUI surface | **Dedicated "Managed APKs" manager** (Add / Update / Remove, library-wide) **plus a per-config tick** to include a store app in a config. |

## Architecture

### Store layout

A new top-level store beside the profile + firmware libraries, resolved by a new
`apk_store_dir()` that mirrors `firmware_dir()` exactly: `library_root()/_apks`, with an
explicit override honored **only if its path currently exists** (so a stale NAS-pinned
override on an offline bench is ignored and the store follows the discovered library).
Because `library_root()` already defaults to the NAS when mounted, the store is
"on the server by default" with no extra wiring.

```
_apks/<pkg>/
  meta              # key=value (like profile.meta):  current=<label>  (+ optional source, added)
  <label>.apk       # the live build; older labels retained (never hard-deleted)
  <label>/          # OR a folder of split APKs, for split-APK apps
  _archive/         # prior bytes of a re-used label, kept (never hard-deleted)
```

- **`current=<label>`** in `meta` names the build that deploys. Older `<label>.apk` files
  stay in place (the soft-archive); they simply aren't current.
- **Label** is the version string the operator supplies in the Add/Update dialog; it
  **defaults to the chosen APK's filename stem** (e.g. `cocoon-1.5.0.apk` → `cocoon-1.5.0`).
  Auto-reading `versionName`/`versionCode` from the APK is a future enhancement (Out of
  scope) — the operator types or accepts the default.
- **Split APKs**: a label may be a single `<label>.apk` **or** a `<label>/` directory of
  splits; the resolver returns the file list and the installer uses `install-multiple` for
  a multi-file set.

### Two categories + captured coexistence

- **Managed third-party apps** (cocoon, …) — server-only. A config opts in **per-config**.
  Absent at deploy → **WARN, not FAIL** (consistent with `c7673e4`).
- **Kit APKs** (Magisk, Companion) — live in the store too, but resolve **store → bundle
  fallback**, so an offline NAS never blocks rooting/provisioning.
- **Captured golden APKs** — **unchanged**: still written to
  `golden_root_payload/<pkg>/apk/` by capture. The store does not touch the capture path.

### Deploy resolution (per app with the `apk` axis on)

`resolve_app_apk(pkg, prof, store_dir, bundle_dir)` returns the deployable APK(s) in this
order:

1. Profile payload `golden_root_payload/<pkg>/apk/*.apk` (captured) → use it (today's
   behavior, unchanged).
2. Else server store `_apks/<pkg>/` current label → **`adb install` PC-side** (the same
   mechanism Companion/Magisk already use to push an APK off the PC filesystem).
3. Else **kit-only** bundle fallback `data/Apps/…` (Magisk / Companion).
4. Else **WARN + skip**.

Managed apps install **PC-side**, so the on-device `lib-root.sh` restore is **unchanged**
— deliberately small blast radius.

### How a config "uses" a store app

A config references a managed app with an ordinary manifest line `<pkg> apk` (apk axis on,
config axis off — there is no captured config for a store-only app). This reuses the
existing `manifest_axes` / `save_manifest` format with **no new tokens**.

## New / changed units

### `cas/config.py`
- `apk_store_dir()` — `library_root()/_apks`, override-if-exists (mirror `firmware_dir`).
- `set_apk_store(path)` — persist/clear an override (mirror `set_firmware_dir`).

### `cas/profiles.py`
- `apk_store_pkg_dir(store_dir, pkg)` — `store_dir/<pkg>` (path helper).
- `store_current_label(store_dir, pkg)` — the `current=` label from `meta`, or `None`.
- `store_apk_files(store_dir, pkg)` — the current label's APK file list (one for a single
  `.apk`, many for a split directory), or `[]`.
- `list_store_apks(store_dir)` — `[{pkg, label, nfiles, bytes}]` for each app with a
  current build.
- `put_store_apk(store_dir, pkg, src, label=None)` — copy `src` (a single `.apk` file or a
  directory of splits) into the store under `<label>`, repoint `meta: current`, archiving
  any prior bytes of a re-used label. **Backs both Add and Update** (Update is the same op
  on an app that already exists). Returns the label.
- `remove_store_apk(store_dir, pkg)` — soft-remove: clear `meta: current` so the app stops
  deploying everywhere, while **retaining** all `<label>` files in place (re-adding
  restores it). Never hard-deletes.
- `resolve_app_apk(pkg, prof, store_dir, bundle_fallback=None)` — the resolution order
  above; returns a list of APK paths (`install-multiple` for >1), or `None`.
- `download_rows(all_pkgs, store_pkgs, saved)` — pure helper for the GUI: the ordered
  `{pkg: (apk, cfg)}` row map for the Download modal, appending store-only apps (default
  `(True, False)` — APK on, no captured config) after the profile's own apps.

### `cas/provision.py`
- Split the manifest's apps into **payload apps** (have a captured module dir under the
  payload) and **managed apps** (apk axis on, no payload module). `_validate_payload` and
  the push loop use the payload apps only; the device manifest pushed to `{DEV}/manifest`
  is **filtered to the payload apps + flags** so on-device `restore.sh` never sees a
  managed app (keeps `lib-root.sh` unchanged).
- After restore, install each **managed app** PC-side via `adb install`/`install-multiple`,
  resolved from the store. Unresolved → **WARN + skip** (Companion is handled by its own
  path, not this loop).
- `root()` Magisk source and `install_companion(...)`: prefer the store's current build for
  the kit package, fall back to the bundled `data/Apps/…` copy when the store entry is
  unreachable (so an offline NAS never blocks rooting/provisioning).

### `cas/gui.py`
- **Managed APKs manager** (library-wide view): a table of store apps — icon (via the
  existing `_icon_from_apks`, reading the store APK), package, current label — with
  `[+ Add APK…]` and per-row `[Update…]` / `[Remove]`. Add = supply the package name
  (auto-read from the APK when parseable, else typed) + pick the `.apk` / split set →
  uploads to the store and sets current.
- **Per-config Apps list**: store apps appear as tickable `(from store)` rows, merged with
  the payload/launcher apps already shown. Ticking writes the manifest line `<pkg> apk`;
  unticking removes it.

### `provision/root/lib-root.sh`
- **Unchanged.** Managed apps install PC-side via `adb install`.

## NAS-offline behavior

- **Managed apps** → WARN + skip, clearly logged (the store is their only source).
- **Kit apps** → bundle fallback; provisioning/rooting unaffected.

## Testing

Pure-function unit tests (tmp dirs, no device):

- `apk_store_dir`: override-if-exists, else `library_root()/_apks` (mirror the
  `firmware_dir` tests).
- Store accessors: `put_store_apk` → `current` set + file in place; a second `put` →
  new `current`, prior label retained; `remove_store_apk` → `current` cleared, files
  retained; label defaults to the source filename stem.
- `resolve_app_apk` order: payload → store → kit-bundle → `None`, including the split-APK
  directory case (returns the multi-file list).
- `download_rows`: store-only apps appended with default `(True, False)`; a saved axis for
  a pkg overrides the default; profile apps keep their order first.
- Manifest round-trip: a row written as `(True, False)` serializes to `<pkg> apk` and
  `manifest_axes` reads it back as `(True, False)` (existing format, no new tokens).
- Provision integration (FakeRunner): a profile whose manifest lists a managed app with a
  store entry produces an `install` call for that APK; `_validate_payload` does not fail on
  the managed app's missing payload module; the device manifest is filtered to payload apps.
- Kit fallback (FakeRunner): with the store dir absent, `install_companion` installs the
  bundled Companion; with a store entry present, it installs the store build.

Minimal GUI smoke (manual, documented in the task): the Managed APKs manager opens, lists a
seeded store, and Add/Update rewrites `meta`.

## Out of scope (YAGNI)

- Per-config version pinning (rejected — always-latest).
- Auto-reading `versionName`/`versionCode` from the APK (operator types the label; the
  filename stem is the default).
- `sha256`/signature/certificate integrity verification before install.
- Internet/OTA fetching of APKs into the store.
- Migrating existing captured payload APKs into the store (capture path stays as-is).
