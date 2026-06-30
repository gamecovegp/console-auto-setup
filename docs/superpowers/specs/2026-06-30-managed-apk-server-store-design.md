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
  meta              # key=value (like profile.meta):  current=<label>
                    #   optional: versionName, versionCode, sha256, source, added
  <label>.apk       # the live build; older labels retained (never hard-deleted)
  <label>/          # OR a folder of split APKs, for split-APK apps
```

- **`current=<label>`** in `meta` names the build that deploys. Older `<label>.apk` files
  stay in place (the soft-archive); they simply aren't current.
- **Label** is the version string: best-effort parsed `versionName` (`versionCode` as a
  tiebreak) from the APK, else the chosen filename stem; operator-overridable in the
  Add/Update dialog.
- **Split APKs**: a label may be a single `<label>.apk` **or** a `<label>/` directory of
  splits; the resolver handles both (install-multiple for a directory).
- **`sha256`** (optional) is recorded on add/update and checked before install
  (best-effort; a mismatch WARNs and skips rather than installing a corrupt build).

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
- `list_store_apks(store_dir)` — packages present in the store + their current label/size.
- `store_current(store_dir, pkg)` — current label and resolved file(s), or `None`.
- `add_store_apk(store_dir, pkg, apk_path(s), label=None)` — copy into the store, write
  `meta` (`current`, best-effort version/sha), return the entry.
- `update_store_apk(store_dir, pkg, apk_path(s), label=None)` — add a new label and
  repoint `current`; **retain** the prior label.
- `remove_store_apk(store_dir, pkg)` — soft-remove: clear `meta: current` so the app stops
  deploying everywhere, while **retaining** all `<label>` files in place (re-adding
  restores it). Never hard-deletes.
- `resolve_app_apk(pkg, prof, store_dir, bundle_dir)` — the resolution order above.
- `_parse_apk_version(apk_path)` — best-effort `versionName`/`versionCode`; never raises.

### `cas/provision.py`
- Deploy/restore install loop: for an app with the `apk` axis on and **no payload APK**,
  resolve from the store and `adb install` it PC-side; honor the WARN-not-FAIL path when
  absent.
- `root()` Magisk source and `install_companion(...)`: prefer the store's current build,
  fall back to the bundled `data/Apps/…` copy when the store entry is unreachable.

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
- **`sha256`** (when recorded) is verified before install; a mismatch WARNs and skips.

## Testing

Pure-function unit tests (tmp dirs, no device):

- Store accessors: add → `current` set; update → new `current`, prior label retained;
  remove → soft-removed, files retained.
- `resolve_app_apk` order: payload → store → kit-bundle → WARN, including the split-APK
  directory case.
- `_parse_apk_version`: returns a label for a real APK; never raises on a junk file.
- Manifest round-trip: ticking a store app writes `<pkg> apk`; unticking removes it;
  `manifest_axes` reads it back as `(True, False)`.
- NAS-offline fallback: kit app resolves to the bundle when the store dir is absent;
  managed app resolves to `None` (→ WARN).

Minimal GUI smoke: the Managed APKs manager opens, lists a seeded store, and Add/Update
rewrites `meta`.

## Out of scope (YAGNI)

- Per-config version pinning (rejected — always-latest).
- Internet/OTA fetching of APKs into the store.
- Migrating existing captured payload APKs into the store (capture path stays as-is).
- Signature/certificate verification beyond the optional `sha256` integrity check.
