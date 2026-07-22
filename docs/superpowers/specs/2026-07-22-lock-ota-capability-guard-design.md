# Design: Lock leaves every unit able to take a vendor OTA

- **Date:** 2026-07-22
- **Status:** Approved (design), pending implementation plan
- **Author:** Donald (CTO) + Claude

## Problem

A unit that CAS roots, provisions and seals must still be able to take its vendor firmware update.
Today it can silently ship unable to. Proven live on MANGMI AIR X `MQ66142509130541`
(bengal, build `AIR_X_user_20260507` / `eng.hxh.20260507.141302`): the OTA stalled at **6%
installation**, three separate times, each on a different partition.

An A/B **delta** OTA bsdiffs against the installed image and source-verifies every partition it
touches by SHA-256 **over blocks**. Any partition left differing from that unit's factory image
aborts the whole payload before a single byte is written:

```
Applying 79 operations to partition "boot"        OK
Applying  7 operations to partition "dtbo"        OK
Applying  8 operations to partition "init_boot"   FAIL
  Expected:   2EB65743...   Calculated: 57846F04...
  → ErrorCode::kDownloadStateInitializationError (20)
```

Three dirty partitions were found in sequence, each hiding the next:

| partition | why it differed | who caused it |
|---|---|---|
| `init_boot` | Seal restored a **wrong image** from the per-build store | **CAS** |
| `product` | 57 MB `GcCompanion.apk` baked into `/product/app` | manual, 2026-07-02 |
| `vbmeta` | verity disabled so `/product` could be remounted rw | manual, 2026-07-02 |

After restoring all three to factory the OTA completed: **0 source-hash failures,
`DownloadAction: ErrorCode::kSuccess`**, every partition through `modem` and `abl`.

Only the first is CAS's doing, and it is the one this spec addresses.

## Root cause

`2026-07-18-seal-restore-own-init-boot-design.md` correctly made Seal restore the unit's **own**
factory `init_boot` from a per-build store rather than a wrong-build library image. That fix works
— the RP6 seals and updates today. The defect is in how the store is **filled and trusted**.

`_capture_factory_init_boot` (`cas/provision.py:995-1002`) dumps the **inactive slot** and accepts
it if two guards pass:

```python
if not _ibs.looks_like_boot_image(data): ... return False
if _ibs.contains_magisk(data):           ... return False
```

Both guards passed on the AIR X, because the captured image genuinely *was* a valid, Magisk-free
boot image. It was simply **not the factory one**. It was read at 2026-07-18 18:05, minutes after a
full day of OTA attempts that had been writing to slot B. Seal then flashed it onto the good slot,
and every subsequent OTA failed source-verify.

The guards answer "is this a plausible boot image?" The question that matters is "is this **this
build's factory** boot image?" — and nothing asks it. Worse, the correct image was in the firmware
library the entire time:

```
_firmware/air-x/versions/20260507-165105/payload/emmc/init_boot.img   → ext-hash 2eb65743  (what the OTA wants)
_init_boot_factory/MANGMI_...20260507.../init_boot.img                → ext-hash 57846f04  (what Seal flashed)
```

## Goal / success criteria

- After Root → Save → Download → Lock, a unit takes its vendor delta OTA without stalling or
  erroring, on **Retroid, AYN and MANGMI**.
- A unit whose OTA-capability cannot be assured is **reported as such at Lock**, not discovered by a
  customer.
- **The shipped RP6 behaviour does not change.** No regression to units that work today.

## Non-goals (v1)

- Verifying `super` (`system`/`product`/`system_ext`) — 4.45 GiB to read, and CAS never writes it.
  The AIR X's dirty `product` came from manual bake work, not from CAS.
- Restoring partitions CAS never touches (`vbmeta`, `product`). Out of scope; manual modification is
  an operator action, not a tool behaviour.
- Shipping the latest firmware build (kit-from-golden). Explicitly dropped from this scope.
- Making `ships_rooted` units OTA-capable — see the RP5 waiver below.

## Design

### Component 0 — kit build provenance (`cas/firmware.py`)

**Every kit's `fingerprint` field is currently empty (`''`)** — `retroid-pocket-6`, `air-x`,
`ayn-thor`, `odin3` alike. So today there is no metadata that can tell "this kit *is* this unit's
factory build" from "this kit is a different build of the same model". That distinction is the
whole ballgame:

| unit build | kit version | same build? | correct source |
|---|---|---|---|
| `eng.RP6.20260119.170007` | `RP6_20260115` | **no** | store capture |
| `eng.Thor.20260206.163241` | `Thor_20260112` | **no** | store capture |
| `eng.hxh.20260507.141302` | `20260507-165105` | **yes** | kit |

Add the ability to record a kit's build fingerprint, and treat a kit as authoritative **only** when
that recorded fingerprint equals the unit's. A kit with an empty fingerprint is never authoritative.

The AIR X kit can be marked proven immediately — we have hash evidence it matches the OTA's expected
source. RP6/Thor stay unproven, so nothing about them changes.

### Component 1 — one factory-image resolver (`cas/provision.py`)

Replace Seal's direct store lookup with a single resolver returning `(image, provenance)` where
provenance is one of `proven-kit` / `captured` / `unverified`:

1. **Proven kit** whose recorded fingerprint == the unit's → authoritative.
2. **Store capture** for the unit's fingerprint → primary in every other case. *This preserves the
   RP6 and Thor paths exactly as they ship today.*
3. Neither → `unverified`: Seal proceeds only per the operator's configured behaviour and says so
   loudly.

Note the priority: the store capture is **not** demoted. A kit only wins where it has been proven,
which today is the AIR X alone.

### Component 2 — cross-check and quarantine

When a **proven** kit and a store capture both exist for the same fingerprint and disagree, the kit
wins and the capture is quarantined (moved aside with the reason recorded), so the bad entry cannot
be silently reused on the next unit of that build. This is exactly the AIR X case.

### Component 3 — capture hardening (`cas/provision.py:995-1002`)

Keep both existing guards; they are necessary but not sufficient. Add: **refuse to capture from the
inactive slot while an update is staged or partially applied.** That is precisely how this store got
poisoned, and it is the only change that stops a repeat at source.

Detection, in order of preference — the first that proves reliable on all three brands wins, and the
plan must pin exactly one:

1. `update_engine_client --status` reporting anything other than `IDLE` (notably
   `UPDATED_NEED_REBOOT`, or a non-zero progress).
2. The inactive slot being marked unbootable / not-successful via `bootctl`, which is the state a
   half-applied payload leaves behind.

Both are cheap and root-available at capture time. If neither can be read, treat that as "cannot
prove the slot is clean" and **skip the capture** — absence of a capture is recoverable, a poisoned
capture is not.

Where a proven kit exists for the same fingerprint, prefer it over dumping the slot at all.

### Component 4 — verify at Seal, and report

Before flashing, verify the **source image** offline against the proven kit for that build. Cheap,
needs no root, and catches the real failure mode (flashing the wrong source). Record the outcome —
`proven-kit` / `captured` / `unverified` — in run history, so a unit's OTA-capability is visible
after the fact rather than inferred.

Verifying the source rather than re-reading the device is deliberate: Seal's flash *is* the un-root,
so root is gone immediately afterwards and the partition can no longer be read over adb.

### Component 5 — brand coverage and the RP5 waiver

Target partition comes from `adb.boot_partition()` / the kit's `flash_target`: `init_boot` for RP6,
Odin, Thor and AIR X; `boot` for RP5. `check_image_partition_type` already refuses the cross-type
mistake (a ramdisk-only image onto `boot` bootloops the unit).

**RP5 is an explicit exception.** It ships rooted by design — its 905 MHz OC kernel only exists as a
root+OC image (`ships_rooted`), so its `boot` partition can never match factory and it can never
take a vendor delta OTA. Report this as a **known waiver**, not a pass and not a failure.

### Data flow

```
Root   → capture factory image (refuse if update staged; prefer proven kit)
                 ↓
           _init_boot_factory/<build fingerprint>/
                 ↓
Lock   → resolve(image, provenance):  proven-kit > capture > unverified
       → cross-check; quarantine a capture a proven kit contradicts
       → verify source offline
       → flash, confirm un-root
       → record provenance in run history
```

### Error handling

- No image resolvable → Seal reports `unverified` rather than flashing blind.
- Proven kit contradicts capture → kit wins, capture quarantined with reason.
- Capture refused (update staged) → non-fatal; Root continues, absence recorded.
- `ships_rooted` → waiver reported; no OTA-capability claim made.

## Edge cases & known limitations

- A unit whose build matches no kit and has no capture stays `unverified`. This is honest, not a
  regression — it is today's silent behaviour made visible.
- Provenance must be *established*, not guessed. A kit is never auto-proven by version-string
  resemblance; `20260507-165105` matching `AIR_X_user_20260507` is suggestive, and the hash evidence
  is what actually proved it.
- Manual modification of `vbmeta`/`product` remains undetected by v1 and still breaks OTA. Documented,
  not solved.
- Adjacent bug, out of scope: `Edl._launcher` (`cas/adb.py:790`) guards escalation with
  `self.runner is not subprocess_runner` to spot a mocked test runner. That identity check also fires
  for any legitimate wrapper, silently skipping `pkexec` and surfacing as a misleading
  `"Could not connect to /dev/ttyUSB0"`. Worth gating on an explicit flag instead.

## Testing plan (no device required)

- Resolver priority: proven kit wins; unproven kit never overrides a capture (**RP6/Thor regression
  guard**); capture wins when no kit is proven; neither → `unverified`.
- Cross-check: disagreeing proven kit quarantines the capture; agreeing one leaves it.
- Capture hardening: staged-update state → refuse; clean state → capture.
- Both existing guards still reject a non-boot image and a Magisk-carrying image.
- `ships_rooted` → waiver, not failure.
- Fixture from the real defect: a valid, Magisk-free, **wrong-build** image must be rejected by the
  resolver when a proven kit disagrees — the exact image that passed both guards on the AIR X.

## Blast radius

- RP6 and Thor: no behaviour change (their kits stay unproven; capture path untouched).
- AIR X: Seal stops flashing the poisoned capture and uses the proven kit.
- All brands: a new provenance field in run history; a new refusal path at capture time.
- Nothing in the Root → Save → Download flow changes.
