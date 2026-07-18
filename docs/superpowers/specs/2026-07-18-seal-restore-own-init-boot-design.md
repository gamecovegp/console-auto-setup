# Design: `seal()` restores each unit's own factory `init_boot`

- **Date:** 2026-07-18
- **Status:** Approved (design), pending implementation plan
- **Author:** Donald (CTO) + Claude

## Problem

A CAS-rooted-then-sealed Retroid Pocket 6 fails its device OTA with `upgrade failed, code = 20`
(`update_engine` `kDownloadStateInitializationError`; the Retroid updater surfaces it as
`VALIDATE_SOURCE_HASH_ERROR`). Verified live on unit `caecc295` (kalama, build `eng.RP6.20260119`):
the delta OTA applies every partition fine **except `init_boot`**, where the on-disk image doesn't
match the source hash the incremental payload was built against:

```
Applying 8 operations to partition "init_boot"
Unrecoverable source hash mismatch found on partition init_boot extents: [0, 493]
Expected: ACF0E60334...   Calculated: A583DDFE...   → ErrorCode::kDownloadStateInitializationError (20)
```

The bootloader being unlocked did **not** block the OTA (it walked all the way into writing the
payload). The sole cause is a wrong `init_boot`.

## Root cause

`root()`/`seal()` do not use a blind static image — they resolve `stock_init_boot` from the firmware
**library**, matched by device **model/chip**, not by **exact build**:

- `root_all` worker: `provision.py:1854-1857` — `fw.stock_boot_image()` ("the unit's own init_boot from its firmware build").
- `seal_all` worker: `provision.py:1938-1961` — same `fw.stock_boot_image()` resolution.

The library holds RP6 = build `20260115`; the unit runs `20260119`. So both Root and Seal flash a
**same-model, wrong-build** `init_boot` (`A583DDFE…` = the `20260115` kit image, hash-confirmed
identical to `~/Downloads/RP6/RP6_20260115/init_boot.img` blocks `[0,493]`). An A/B **incremental**
OTA source-verifies each partition against the exact installed build, so the wrong-build `init_boot`
fails. The exact factory image the OTA wants (`161F65…`) is not in the library and cannot be, since
units ship on builds newer than any package we hold.

Everything CAS provisions (apps, games, emulator configs, golden layout) lives in `/data`, which an
A/B OTA does not wipe. So the *only* thing standing between a provisioned unit and a working OTA is
restoring the unit's own factory `init_boot`.

## Goal / success criteria

- A unit that CAS roots, provisions, and seals ends with its **own exact-build factory `init_boot`**
  on the active slot, so its device OTA applies cleanly (no `code 20`).
- Fleet-safe: works across models without per-profile hand-picking; no dependency on the library
  holding the unit's exact build.
- No regression to the existing Root/Seal flash + un-root-confirm + lockdown mechanics.

## Non-goals (v1)

- Recovering units already scrambled by manual flashing (that's the separate manual-QFIL / future
  "Factory Restore" recovery tool).
- Handling units OTA'd **before** they reach the shop (slots hold different builds) — documented
  limitation below, guard deferred.
- Preserving root *through* an OTA (we ship un-rooted; root and OTA are mutually exclusive by design).

## Design

### Component 1 — capture store (`cas/initboot_store.py`, new)

A small, dependency-light module owning the on-disk store, testable in isolation.

- **Location:** `<library_root>/_init_boot_factory/<build-slug>/` (sibling of `_firmware`, so it
  travels with the shared library between shop PCs).
- **Key:** the unit's build fingerprint (`ro.build.fingerprint`). On a clean factory unit this equals
  the bootimage build the `init_boot` belongs to. `<build-slug>` = a filesystem-safe slug of the
  fingerprint (or its `ro.build.version.incremental`, e.g. `eng.RP6.20260119.170007`).
- **Contents per build:** `init_boot.img` (raw 8 MB partition dump) + `meta.json`
  (`{fingerprint, incremental, sha256, size, source_serial, captured_utc}`).
- **API:**
  - `has(store_root, fingerprint) -> bool`
  - `get(store_root, fingerprint) -> Path | None`
  - `put(store_root, fingerprint, img_path, meta) -> Path` — idempotent; first clean capture per build
    wins (does not overwrite an existing good capture).

### Component 2 — capture at root (`provision.py`)

New `capture_factory_init_boot(adb, store_root, log)`, invoked as the **last step of a successful
`root()`** (after it has confirmed boot + root, so `su` is available). `root()` gains an optional
`capture_store=None` param; `root_all` worker and the GUI single-Root path pass the resolved store
root. When `None`, capture is skipped.

Steps:
1. Compute the **inactive** slot from `adb.slot_suffix()` (`init_boot_a` ↔ `init_boot_b`). The inactive
   slot is the pristine factory copy — CAS only ever flashes the **active** slot; the active slot now
   holds the just-flashed Magisk-patched image, so it is not a capture source.
2. `su -c 'dd if=/dev/block/by-name/init_boot_<inactive> of=/data/local/tmp/factory_ib.img'`, pull it.
3. **Validity guard:** the dumped image must be a valid boot image — `ANDROID!` magic at offset 0 and
   the expected partition size (8 MB). This rejects the case where the factory only single-slot-flashed
   the unit and the inactive slot is empty/zeroed (which the Magisk scan alone would not catch). If
   invalid → do **not** store; warn and return.
4. **Poisoning guard:** scan the bytes for Magisk markers (`MAGISKINIT`, `.magisk`, Magisk config
   block). If found, it is not a factory image → do **not** store; warn and return.
5. If `has(store, fingerprint)` already → skip (idempotent). Else `put(...)`.

**Additive & non-fatal:** any failure (no su, dd error, pull error, guard trips) logs a warning and
leaves `root()`'s success unchanged. Flash-method-agnostic — it only *reads* a partition, so it works
identically for fastboot and EDL/MANGMI units.

### Component 3 — seal resolution change (`provision.py`, `seal_all` worker ~1938-1961)

Insert a capture-store lookup that takes **precedence** over `fw.stock_boot_image()`:

1. Resolve `fingerprint = adb.getprop("ro.build.fingerprint")`.
2. If `initboot_store.get(store_root, fingerprint)` → set `stock_path` to the captured image (the
   exact-build factory `init_boot`).
3. Else → keep the current library/profile image **and log a loud warning**:
   `"no factory init_boot captured for build <fp> — sealing with library image; OTA may fail on this unit."`

Everything downstream in `seal()` is unchanged: model cross-check, flash via the existing flasher
(fastboot or EDL) to the active slot, confirm un-root, then the retail lockdown. The captured raw
partition image is a valid input to both flash backends.

### Data flow

```
root(): patch kit init_boot → flash to ACTIVE slot → boot + confirm root
        └─(new, last step)→ dd INACTIVE slot → guard(not Magisk) → store[<build fp>] = factory init_boot
   ... provisioning (writes /data) ...
seal(): fp = getprop ro.build.fingerprint
        stock_path = store.get(fp)  ?? library image (+warn)
        → flash stock_path to ACTIVE slot → confirm un-root → lockdown
device OTA: source-verifies init_boot against factory → matches → applies
```

### Error handling

- Capture failure → warn, root still succeeds; seal later falls back to the library image + warn.
- Store lookup miss at seal → library image + explicit "OTA may break" warning (chosen behavior).
- Corrupt/short capture (`size != 8 MB` or sha mismatch on read) → treated as a miss; warn.

## Edge cases & known limitations

- **Unit OTA'd before reaching the shop:** its two slots hold different builds, so the inactive slot
  may not equal the running build. The poisoning guard won't catch this (the image is un-patched, just
  a different build). For fresh factory units — the shop's normal case — inactive == running build, so
  the capture is exact. A stronger guard (verify captured image corresponds to the running build) is a
  documented follow-up, not in v1.
- **Cross-PC:** capture and seal must share the same library root. If Root ran against a different
  library than Seal, the lookup misses and Seal falls back + warns.
- **EDL / MANGMI units:** capture (a read) works via `su`; restore uses the existing EDL flasher with
  the captured image as `stock_path`. No special-casing needed.

## Testing plan (no device required)

- `initboot_store`: `put`/`get`/`has` round-trip by fingerprint; `put` is idempotent (won't overwrite
  a good capture); slug sanitization.
- Validity guard: rejects a zeroed/empty fixture and a wrong-size fixture (reuses the existing
  `ANDROID!`-magic parsing already in `provision.py`, cf. `_img_kernel_size`); accepts a valid one.
- Poisoning guard: rejects a Magisk-marked fixture image; accepts a clean fixture.
- `capture_factory_init_boot`: computes the correct inactive slot; stores under the fingerprint key;
  non-fatal on `su`/`dd`/pull failure (mock `adb.su`/pull with fixtures).
- `seal` resolution: prefers a present capture over the library image; falls back to the library image
  **and emits the warning** when no capture exists.

## Blast radius

- New: `cas/initboot_store.py`, `tests/test_initboot_store.py` (+ seal/capture cases in existing
  provision tests).
- Modified: `cas/provision.py` — `root()` gains `capture_store` param + final capture call; `root_all`
  and `seal_all` workers resolve the store root and pass/consult it; `seal_all` swaps in the
  capture-first resolution.
- Unchanged: flash backends, un-root confirmation, lockdown, firmware library/`fw.stock_boot_image`
  (kept as the fallback).
