# Chip-keyed firmware matching

**Date:** 2026-07-16
**Status:** approved, pre-implementation

## Problem

Two operator-facing pains, one shared root cause.

1. **New same-chip units land as `(no match)`.** A handheld on already-proven silicon (e.g. any new
   `kalama` unit, when the Odin 2 build is already in the library) resolves to nothing, and an operator
   hand-pins it to `(default kit)` or a firmware. The `DEFAULT_FW_ID` docstring in `cas/firmware.py`
   admits this outright — the escape hatch exists "so an operator can EXPLICITLY pin a unit (e.g. a
   Retroid sharing the kalama image)".

2. **Warning fatigue on legitimate matches.** `logic_check()` emits
   `firmware device 'X' != device 'Y'` whenever a firmware's human label doesn't equal the live
   `ro.product.device`. On a legitimate cross-brand match (RP6 rooted from the Odin 2 build — proven to
   boot) this warning is *always true and never meaningful*. `resolve()` already skips `logic_check`
   entirely for `(default kit)` specifically to dodge this false positive.

Root cause: **compatibility is scored, not gated.** `match()` is a flat sum —
`serial_prefix=3, device=2, brand=1, soc=1`. The chip is the weakest signal, tied with brand, and
nothing ever populates it: `ingest()` seeds only `match["device"]`, and `detect_build()` never extracts
a SoC or Android version at all. So a `soc` rule exists only if hand-written into `meta.json`. None are.

### Latent bug this also fixes

Because the score is a flat sum, **`serial_prefix` (3) outvotes `soc` (1)**. A stale `serial_prefix`
rule on an old firmware entry can carry a wrong-chip build to the top of the ranking and win. Chip
compatibility is not an opinion that should lose a vote.

## The core rule

> **A firmware is disqualified only by a *known conflict*, never by *missing data*.**

This single rule carries both the safety design and the migration story. Every `meta.json` in the
library today records zero chip information. A gate that *required* a chip match would reject every
firmware for every device and send the whole fleet to `(no match)` — the pain we are fixing, amplified.

Under this rule, un-backfilled entries keep working exactly as they do today, and backfilling a
firmware's chip is what **upgrades** it from "warns" to "silent auto-select". No flag day; the
incentive points toward better data.

## Design: two-stage match

`match()` splits into a boolean gate followed by the existing scoring.

### Stage 1 — gate (before any scoring)

Three axes. Each rejects **only** when the same field is populated on both sides and the values
conflict.

| Axis | Device side (`identity()`) | Firmware side (`meta.json`, via `detect_build()`) |
|---|---|---|
| chip | `ro.board.platform` → `kalama`; also `ro.soc.model` → `SM8550` | `ro.board.platform=` / `ro.soc.model=` grepped from the build |
| android major | `ro.build.version.release` → `13` (major only) | `ro.build.version.release=` grepped from the build |
| storage | `ro.boot.bootdevice` → `…ufshc` / `…sdhci` (see Open items) | `Firmware.storage` (`emmc`\|`ufs`) — already exists |

**Both chip props are recorded on both sides, and an axis gates only on a same-prop comparison.**
Comparing `ro.board.platform` (`kalama`) against `ro.soc.model` (`SM8550`) would read as a conflict and
disqualify everything — the two spellings name the same silicon. If the two sides populated different
props, chip simply does not gate.

Android compares **major only** (`13` vs `13`), not `13` vs `13.1`.

A firmware rejected by the gate is not a candidate at all — no score, absent from the suggestion.

### Stage 2 — score (survivors only)

Soft rules unchanged: `serial_prefix=3, device=2, brand=1`. The `soc=1` rule is **retired** — chip is
now a gate, not a tiebreaker. Unique highest score wins; a tie → `None`, operator selects (as today).

Consequence: no soft rule can promote a firmware the gate rejected. The `serial_prefix`-outvotes-chip
bug becomes structurally impossible.

#### Affirmed vs vacuous gate passes (candidacy at score 0)

`gate_check()` must report not just *whether* it passed but *whether it actually compared anything*.
This is not a refinement — without it the feature fails on its motivating case:

> An RP6 matched against the Odin 2 build scores **zero**: no `serial_prefix` hit, `device` differs,
> `brand` differs. Under a naive `if score > 0` it would pass the gate and then be discarded.

So `gate_check()` returns `(ok, reason, agreed)`, where `agreed` counts the axes that **compared and
agreed** (rather than abstained):

- **Affirmed pass (`agreed > 0`)** — the gate positively confirmed chip/android/storage compatibility.
  The firmware is a candidate **even at score 0**. This is what makes cross-model reuse work.
- **Vacuous pass (`agreed == 0`)** — every axis abstained (a legacy, un-backfilled entry). The gate
  affirmed nothing, so the firmware still needs a positive score to be a candidate — today's behavior,
  preserved exactly.

This keeps the core rule intact in both directions: missing data never rejects a firmware, and it never
promotes one either.

### Selection behavior

Gate passes → **silent auto-select**, cross-brand or not. No confirm.

This was decided against two rejected alternatives:

- *Confirm on novel pairs* — rejected as friction on the case we're trying to make frictionless.
- *Confirm cross-brand only* — rejected because **brand does no work in either direction.** The
  cross-brand pair (RP6 ≡ Odin 2 Mini) is the one **proven to boot**, while the real traps are
  same-brand: MANGMI AIR X vs Pocket Max must never be cross-flashed; Odin 2 vs Odin 3 would brick.
  The chip gate catches both same-brand traps on the chip axis. A brand rule would have prompted on the
  proven pair and stayed silent on unproven same-brand SKUs — exactly backwards. Storage replaces brand
  as the real discriminator: storage type lives in the fstab, which lives in the ramdisk, which is what
  `init_boot` *is*, and eMMC/UFS SKU splits are common *within* a brand's lineup.

## Changes to `logic_check()`

The `firmware device 'X' != device 'Y'` warning is **deleted**, not softened. It is always true on
legitimate cross-brand matches, and that is what trained operators to click through warnings.

Gate passes → nothing to warn about. Gate rejects → the firmware was never offered.

The `flash_target` and `serial_prefix` warnings stay as-is.

## Proven pairs

After a root that **actually boots**, record a tuple in the library:

```
(chip, android_major, storage, model, firmware_id, version)
```

This is **evidence, not a gate** — it never blocks or unblocks a flash. It turns "RP6 ≡ Odin 2 works"
from knowledge in one person's head into data in the tool, and gives a future maintainer a real basis
for judging a novel combination.

Written via the existing `log_event()` jsonl pattern (per-machine, best-effort, never raises).

## What explicitly does not change

`_img_kernel_size()` in `cas/provision.py` stays exactly where it is and shares **nothing** with this
logic. It reads the `ANDROID!` boot-image header and refuses an image whose type doesn't match the
flash target (an `init_boot` is ramdisk-only → `kernel_size == 0`; a full `boot.img` → `> 0`), which is
what prevents the kernel-less-image-into-`boot` brick that killed the RP5.

Matching is a heuristic about compatibility. The kernel-size check is physics. Defense in depth means
the last line of defense must not share a failure mode with the first — when chip matching is
eventually wrong, that guard is what stands between the operator and another brick.

`(default kit)` pinning also stays. It remains the explicit operator override.

## Data-model changes

- **`identity()`** — add `board_platform` (`ro.board.platform`), `android_release`
  (`ro.build.version.release`), `bootdevice` (`ro.boot.bootdevice`). Keep existing `soc`.
- **`detect_build()`** — extract `board_platform`, `soc`, `android_release` from the build's
  `super_*.img` / `system_*.img` via the existing `_grep_value()` helper, which already does exactly
  this for `ro.build.fingerprint=` and `ro.product.system.device=`.
- **`ingest()`** — seed `match{}` with the detected chip/android alongside the existing `device` seed,
  and persist them into `version.meta.json`.
- **`meta.json`** — new optional `match` keys: `board_platform`, `soc`, `android_release`. All optional;
  absence means the axis doesn't gate.

## Operator workflow: adding a new chip or firmware

**The normal path requires no chip knowledge from the operator.** Adding a new firmware is unchanged
from today: `FirmwareWindow` → "Add / update…" → `ingest()`. The build self-describes — `ingest()`
already detects `device`, `storage`, `flash_target`, `version`, and `fingerprint` from its own
`super_*.img`; this spec adds `board_platform`, `soc`, and `android_release` to that same pass and
seeds them into `match{}`.

Worked example — the Odin 3 (SD 8 Elite) build arrives:

1. Operator ingests it like any other build. No new step.
2. It seeds its own `match{}` with its detected chip / android / storage.
3. Every Odin 3 unit now auto-matches it. Every kalama unit is **rejected by the gate**, so an Odin 3
   build can never be offered for an RP6 (and vice versa) — which is the kernel-less-brick pair from
   the Odin 3 bring-up notes.

The operator never types a chip codename anywhere. `firmware_rows()` (`cas/dialogs.py`) already renders
a *match rules* column, so detected chip/android/storage become visible in the Firmware window without
new UI.

### Escape hatch: detection came up empty

`_grep_value()` greps `super_*.img` / `system_*.img`. A build whose props live elsewhere (vendor image,
compressed payload) yields `""` → chip unknown → entry stays legacy → gate abstains. Per the core rule
this is *safe*, but it is **silent**, and its symptom (`(no match)`) is indistinguishable from the
problem this spec exists to fix.

Add `python3 -m cas.firmware set <id> [--chip X] [--android Y] [--storage emmc|ufs]` to write the gate
fields on an existing entry without re-ingesting. Today the only recourse is hand-editing `meta.json`.

### `(no match)` must explain itself

This spec deletes a warning that was noisy and false. It must not replace it with a dead end that is
quiet and uninformative. When `resolve()` returns no candidate, the reason string must distinguish:

- `no firmware matches this chip (kalama)` — 4 entries rejected on chip → the library genuinely lacks a
  build for this silicon; ingest one.
- `2 firmware(s) have no chip recorded — run 'cas.firmware backfill'` — legacy entries abstained; the
  data is missing, not the build.

These are different operator actions, so they must be different messages. The existing
`{"warnings": [...]}` field on the `resolve()` dict carries them; no new plumbing.

## Migration / backfill

Existing firmware entries have payloads on the library drive, so backfill is re-detection, not
re-ingestion. Add `python3 -m cas.firmware backfill` to re-run `detect_build()` over each firmware's
current version payload and fill the new `match` keys in place.

Un-backfilled entries continue to behave as they do today (legacy scoring, gate abstains). Backfilling
is what earns the silent auto-select.

## Testing

- Gate rejects on known conflict (chip differs, android major differs, storage differs) — each axis
  independently.
- Gate abstains on missing data on either side — the RP6-vs-un-backfilled-Odin-2 case must still
  resolve exactly as it does today.
- Gate does not compare `ro.board.platform` against `ro.soc.model` (the `kalama` vs `SM8550` false
  conflict).
- `serial_prefix` can no longer promote a gate-rejected firmware (the latent bug — regression test).
- Android `13` vs `13.1` compares equal; `13` vs `14` conflicts.
- The proven RP6 ≡ Odin 2 pair passes the gate once both are backfilled, and produces **no warning**.
- `logic_check()` no longer emits a device-inequality warning.
- `_img_kernel_size()` behavior is untouched (existing tests must pass unmodified).
- `ingest()` on a build with detectable props seeds chip/android into `match{}` with no caller input
  (the zero-knowledge operator path).
- `ingest()` on a build with **no** detectable chip leaves the entry legacy and does not raise.
- `set --chip/--android/--storage` writes the gate fields on an existing entry and is idempotent.
- `resolve()` with no candidate distinguishes "no build for this chip" from "entries have no chip
  recorded — run backfill". Two distinct reason strings, because they imply different operator actions.

## Open items — verify before/during implementation

1. **`ro.boot.bootdevice` shape is unverified.** Proposed as the no-root storage probe
   (`…ufshc` → `ufs`, `…sdhci`/`mmc` → `emmc`), but not yet read off a real RP6 or AIR X. If it returns
   something unrecognized, storage resolves to unknown → that axis abstains → falls back to legacy
   behavior, never to a wrong flash. **Must be confirmed on-bench before the storage axis is trusted.**
2. **Confirm the proven pair passes its own gate.** If a live RP6 and the Odin 2 build disagree on any
   of the three axes, the gate is wrong and this design needs revisiting — the pair is known to boot.
   This is the single highest-value bench check.
3. Whether any existing library build fails to yield `ro.board.platform=` from its `super_*.img` (in
   which case that entry stays legacy until someone supplies the chip by hand).
