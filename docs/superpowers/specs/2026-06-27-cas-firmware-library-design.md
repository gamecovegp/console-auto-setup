# CAS Firmware Library — Design

- **Date:** 2026-06-27
- **Status:** Approved (design); implementation pending
- **Author:** Donald (CTO) + Claude
- **Scope:** Add a labeled, versioned firmware library to CAS that auto-matches firmware to a connected device (suggestion + logic check), allows operator override / selection from the full list, supports easy version updates with history, and stores everything on the shared NAS. **CAS stores and advises — it never flashes.**
- **"Firmware" means DEVICE ROOT FIRMWARE only** — the handheld's own OS/boot images (MANGMI/AYN flat-builds, and the `init_boot`/`boot` ramdisk used for rooting + EDL un-brick restore). This is **NOT** emulator/app firmware or BIOS (e.g. Eden/Switch firmware+keys, PSX/PS2 BIOS), which are emulator *runtime* assets carried in the golden payload (the "Bios to internal" subsystem) and are out of scope here. Throughout this document, "firmware" = device-level firmware.

---

## 1. Background

CAS provisions GameCove handhelds. Rooting is per-device (Magisk-patched ramdisk), and the partition/slot to flash is already auto-derived by `Adb.boot_flash_target()` (`init_boot`/`boot` × `_a`/`_b`). What's missing is a way to know **which firmware build** a given connected unit needs — the marketing model alone is insufficient (two AIR X hardware revisions ship the *same* `ro.product.model` and even the *same* baked `ro.mangmi.dev.code`).

### Existing infrastructure this builds on
- **Shared NAS library** (`cas/config.py`): `library_root()` resolves `CAS_PROFILES` env → config `library` → NAS (`\\192.168.100.227\01 GAMECOVE\[03] SETUP\CAS Profiles`) → local. NAS auto-connect with a low-priv app account.
- **Per-serial assignment memory** (`config.get/set_device_profile`): `{serial: {profile, manual}}` in `cas-config.json`; first auto-match saved, operator override saved `manual=True` and always wins.
- **Profile matching** (`profiles.match_profile`): regex-free, token-based, SD-size tiebreak.
- **History** (`config.history_dir`, `record_download`): jsonl run logs + rolling stats.
- **adb wrapper** (`cas/adb.py`): `getprop`, `slot_suffix`, `boot_partition`, `boot_flash_target`.

The firmware library deliberately mirrors these patterns (`profiles.py` ↔ new `firmware.py`; `device_profiles` ↔ `device_firmware`).

### Confirmed device → firmware labels (verified from firmware contents + a live unit)

| Auto-assign key | Device | Firmware folder | Storage | Root flash target | Android |
|---|---|---|---|---|---|
| serial `MQ66…` | AIR X (non-I2C) | `MANGMI_Vex6115_FlatBuild_TurboX-C6115_…20260507.165105` | eMMC | `init_boot_<slot>` | 14 |
| serial `MQ65…` | AIR X (I2C) | `MANGMI_Vex6115_I2C_FlatBuild_TurboX-C6115_…20260506.192132` | eMMC | `init_boot_<slot>` | 14 |
| `ro.product.device=Pocket_Max` | Pocket Max | `MANGMI_VIEGO_07_FlatBuild_TurboX_C2130_…20260316.183818` | UFS | `boot_<slot>` | 11 |
| AYN serial → M0 | AYN | `AYN_ufs_M0_user` | UFS | `boot_<slot>` | ~12 |
| AYN serial → M2 | AYN | `AYN_ufs_M2_user` | UFS | `boot_<slot>` | ~12 |

The MQ66→non-I2C / MQ65→I2C direction is confirmed by the live MQ66 unit's baked fingerprints (`hxh05071601`/`hxh05071410`) matching the non-I2C 20260507 build exactly. `ro.mangmi.dev.code` is `MQ66` in **both** AIR X builds, so it is **not** usable to split them — the serial prefix is the key.

---

## 2. Goals / Non-goals

**Goals**
1. Store all firmware builds on the NAS, labeled and versioned, with retained history.
2. Auto-**suggest** the firmware for a connected device from its adb identity.
3. **Logic-check** the suggestion against the live device and warn on mismatch (brick-guard).
4. Allow operator **override** and **selection from the full list**; persist per-serial (sticky `manual`).
5. **Easy update**: drop in a new build folder → new version, current bumped, old retained.

**Non-goals**
- No flashing / EDL automation. CAS exposes paths + flash targets; the operator flashes.
- No change to the existing fastboot root flow this pass (advisory only; wiring deferred).
- **No emulator/app firmware or BIOS** (Eden/Switch firmware+keys, PSX/PS2 BIOS, etc.). Those are golden-payload runtime assets, a separate subsystem; this library is device root firmware only. The `_firmware/` tree must never hold emulator BIOS — ingest's device-detection guard (§5) rejects anything without a recognizable device-firmware layout (rawprogram XML + boot/init_boot partitions).

---

## 3. Storage layout (NAS — SMB/Windows-safe, no symlinks)

```
CAS Profiles/_firmware/
  index.json                          # registry: firmware-id → match rules + current version (fast listing)
  <firmware-id>/                      # mangmi-air-x-mq66, mangmi-air-x-mq65,
    meta.json                         #   mangmi-pocket-max, ayn-m0, ayn-m2
    versions/
      <version>/
        payload/                      # the firmware tree as-is (emmc|ufs dir + fh_loader/QSaharaServer/script)
        version.meta.json             # build fingerprint, dev_code, os_version, storage, flash_target, source
```

- `current` is a **key in `meta.json`** (a version dir name) — never a symlink (CIFS + Windows safe; consistent with `config.py`).
- Old `versions/<version>/` dirs are retained → that *is* the history payload. Default: keep all (NAS has room); optional prune-to-N later.
- `index.json` is a denormalized cache of all `meta.json` match rules + current versions so the GUI can match without walking every version dir. Rebuildable from the `meta.json` files (source of truth).

### `meta.json` (per firmware-id)
```json
{
  "id": "mangmi-air-x-mq66",
  "label": "MANGMI AIR X (MQ66, non-I2C)",
  "device": "AIR_X",
  "brand": "MANGMI",
  "storage": "emmc",
  "flash_target": "init_boot",
  "match": { "serial_prefix": ["MQ66"], "device": "AIR_X", "soc": "SM6115" },
  "current": "20260507-165105",
  "history": [
    { "version": "20260507-165105", "fingerprint": "MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/hxh05071410:user/release-keys",
      "os_version": "1.1.6", "added": "2026-06-27 11:20", "source": "MANGMI_Vex6115_FlatBuild_TurboX-C6115_…20260507.165105" }
  ]
}
```

### `version.meta.json` (per version)
```json
{
  "fingerprint": "MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/hxh05071410:user/release-keys",
  "dev_code": "MQ66", "os_version": "1.1.6",
  "storage": "emmc", "flash_target": "init_boot",
  "source": "…/MANGMI_Vex6115_FlatBuild_TurboX-C6115_…20260507.165105"
}
```

### `device_firmware` (in `cas-config.json`, mirrors `device_profiles`)
```json
{ "device_firmware": { "MQ66142509130541": { "firmware_id": "mangmi-air-x-mq66", "version": "20260507-165105", "manual": false } } }
```

---

## 4. Identity & matching

### `Adb.identity()` (new, one-shot, no root)
Returns a dict from a single `getprop` batch:
```
{ serial, device (ro.product.device), model (ro.product.model), brand (ro.product.manufacturer),
  soc (ro.soc.model), dev_code (ro.mangmi.dev.code), first_api (ro.product.first_api_level),
  slot (ro.boot.slot_suffix), flash_target (boot_flash_target()) }
```

### `firmware.match(identity, root)` — suggestion
Score each firmware's `match` rules against `identity`, most-specific wins (mirrors `profiles.match_profile`):
1. `serial_prefix` hit (device serial starts with any listed prefix) — strongest signal.
2. `device` equals `identity.device`.
3. `brand` equals `identity.brand`.
4. `soc` equals `identity.soc`.

Score = count of satisfied rules. Highest unique score wins; tie or zero → `None` (operator selects). Returns `(firmware_id, version)` for the firmware's `current`, or `None`.

### `firmware.logic_check(firmware, identity)` — brick-guard
After a suggestion (auto or manual), validate consistency and return `(ok: bool, warnings: [str])`:
- `firmware.flash_target` vs live `identity.flash_target` partition (`init_boot` vs `boot`) — **mismatch → warn**.
- `firmware.device` vs `identity.device` — mismatch → warn.
- any declared `serial_prefix` vs the live serial — mismatch → warn.

A warned suggestion is still selectable; the operator sees the reason (e.g. "firmware expects `boot` but device exposes `init_boot`", "serial `MQ66…` ≠ firmware MQ65").

### Assignment resolution (what the GUI shows for a connected serial)
1. If `device_firmware[serial].manual` is set → that `firmware_id` wins (sticky override).
2. Else `firmware.match(identity)` → suggestion; save it with `manual=false` (remembered first-find).
3. Run `logic_check`; surface ✓ / ⚠+reason.
4. Operator may override via a dropdown of **all** library firmwares → saved with `manual=true`.

**Version resolution (so updates propagate correctly):** an assignment stores a `firmware_id`; the *version* shown/used is the firmware's **`current`** at read time — so dropping in a newer build (§5) automatically applies to every device assigned that `firmware_id`, manual or not. A persisted `version` field is only written when the operator explicitly **pins** a specific historical version (rollback); a pinned version is reused verbatim until the operator clears the pin. This keeps "easy update" a one-place change while still allowing per-device rollback.

---

## 5. Easy update (drop-in new version)

`firmware.ingest(src_folder, firmware_id=None, copy=True)`:
1. **Detect** from the build folder (deterministic, no network):
   - storage: presence of `emmc/` vs `ufs/` subdir.
   - flash_target: scan `rawprogram*.xml` partition labels for `init_boot*` → `init_boot`, else `boot`.
   - device / dev_code / os_version / fingerprint: grep the `super_*.img` / `system_*.img` for `ro.product.system.device`, `ro.mangmi.dev.code`, `ro.mangmi.os.version`, and the build fingerprint (the same extraction used to build the table in §1).
   - version: parse the build stamp from the folder name (`…user.YYYYMMDD.HHMMSS` → `YYYYMMDD-HHMMSS`); fall back to fingerprint date.
2. If `firmware_id` omitted, infer it (`<brand>-<device>-<variant>` lowercased); create the dir + `meta.json` if new.
3. Copy the tree to `versions/<version>/payload/`, write `version.meta`, set `meta.current = version`, append to `meta.history`, refresh `index.json`.
4. **Idempotent**: if `versions/<version>/` already exists, no-op (log + return).
5. **Guard**: if detected `device` ≠ the firmware-id's existing `device`, refuse and warn (prevents pushing an AIR X build into the Pocket Max id).

GUI: an **"Add / update firmware"** button (folder picker) calling `ingest`; a thin CLI entry mirrors it for bench scripting.

---

## 6. History & audit

- `meta.history[]` — version lineage (version, fingerprint, os_version, added, source), newest last.
- Retained `versions/<version>/` dirs — the actual rollback payloads.
- **Assignment audit log** — a `firmware-history.jsonl` via `config.history_dir`, one line per suggest / override / update event: `{when, serial, firmware_id, version, action, manual}`. Gives a per-device trail of which firmware decision was made when.

---

## 7. Code touch points

| File | Change |
|---|---|
| `cas/firmware.py` (new) | `list_firmware`, `match`, `logic_check`, `ingest`, version/history helpers, `Firmware` class. Mirrors `profiles.py`. |
| `cas/config.py` | `firmware_root()` (= `library_root()/_firmware`); `get/set_device_firmware(serial, …)` (mirror `device_profiles`). |
| `cas/adb.py` | `Adb.identity()` one-shot prop batch. |
| `cas/gui.py` | Firmware panel: identity, suggestion + version, logic-check ✓/⚠, override dropdown (all firmwares), flash_target + on-disk paths, "Add / update firmware". |
| `tests/test_cas.py` | TDD coverage (below). |

---

## 8. Testing plan (TDD — injectable runner / tmp library, no real device)

1. `match`: identity dicts for MQ66/MQ65/Pocket_Max → expected firmware-id; ambiguous → `None`.
2. `logic_check`: matching vs mismatched `flash_target`/`device`/serial → `ok` vs warnings with reasons.
3. `ingest`: fake build folder (rawprogram with `init_boot` labels + `emmc/` subdir) → creates `versions/<v>/`, `meta.json`, `current`, `history`; re-ingest same version → no-op; second version → `current` bumps, old version retained in `history`; wrong-device guard refuses.
4. `config.get/set_device_firmware`: manual override persists and wins over a match.
5. `Adb.identity()`: mocked `getprop` → correct dict incl. `flash_target` composition.

---

## 9. Open items (explicit, not blockers)

- **AYN M0/M2 serial prefixes** — data pending. The serial-prefix matcher is fully specified; the exact prefixes get captured from an untouched AYN unit (or supplied) and entered into `ayn-m0` / `ayn-m2` `meta.match.serial_prefix`. Until then AYN auto-suggest returns `None` → operator selects (override path), which is the intended fallback.
- **Pocket Max serial prefix** — unknown; matched on `device=Pocket_Max` (sufficient — single Pocket Max firmware).
- **Root-flow wiring** — deferred. Library is advisory; a later pass can let the fastboot root flow consume the assigned firmware's init_boot/boot.
