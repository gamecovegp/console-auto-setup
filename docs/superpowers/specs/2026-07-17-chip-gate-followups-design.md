# Chip gate follow-ups: soc SKU tolerance + honest backfill reporting

**Date:** 2026-07-17
**Status:** approved, pre-implementation
**Origin:** both findings come from the first probe of a LIVE RP6 (serial `caecc295`) against the
shipped chip gate. Neither was visible from fixtures.

Two independent parts. Either is shippable alone.

---

## Ground truth (measured, not assumed)

The live RP6 reports:

```
ro.product.model          = Retroid Pocket 6
ro.product.device         = kalama
ro.product.manufacturer   = Moorechip
ro.board.platform         = kalama
ro.soc.model              = QCS8550          <-- NOT SM8550
ro.build.version.release  = 13
ro.boot.bootdevice        = 1d84000.ufshc
```

Three things this settles:

1. **`ro.boot.bootdevice` is CONFIRMED.** The spec's `_storage_from_bootdevice` probe was flagged as
   "unverified against real hardware" since day one. The real device returns `1d84000.ufshc` — the
   exact string the spec guessed — and the probe maps it to `ufs`. **That open item is closed.**
2. **The gate works on real silicon.** Every wrong-chip entry rejects with a precise reason
   (`chip kalama != firmware bengal` / `sun` / `kona`), `ayn-m0`/`ayn-m2` reject on the android axis
   (`android 13 != firmware 10`), `odin2-default` passes vacuously (`agreed=0`, not a candidate), and
   `retroid-pocket-6` wins with `agreed=1, score=3`. `match()` → `retroid-pocket-6`.
3. **A prior claim was WRONG and is retracted.** It was asserted that `set odin2-default --chip kalama`
   would tie with `retroid-pocket-6` and break the RP6. That rested on an INVENTED identity
   (`device='RP6'`, `brand='Retroid'`). The real RP6 reports `device=kalama` / `brand=Moorechip`, so
   `retroid-pocket-6` scores 3 and wins outright — sandbox-verified with the real identity: before
   `retroid-pocket-6`, after `retroid-pocket-6`. **No tie.** Skipping the set was harmless; the reason
   given for it was false.

---

## Part 1 — `board_platform` agreement outranks a `soc` conflict

### Problem

`gate_check()` compares `board_platform` and `soc` independently, and **a conflict on either rejects,
even when the other agrees**:

```python
    for key in ("board_platform", "soc"):
        want, live = r.get(key), identity_dict.get(key)
        if want and live:
            if want.strip().lower() != live.strip().lower():
                return (False, f"chip {live} != firmware {want}", 0)
            agreed += 1
```

The live RP6 reports `soc = QCS8550`. A generic kalama build's super image records
`ro.soc.model = SM8550`. **Both are Snapdragon 8 Gen 2** — `QCS` is the IoT/embedded SKU of the same
silicon — and `board_platform` is `kalama` on both sides.

So ingesting such a build makes this RP6 **rejected**, despite kalama matching on both sides. Worse,
`_no_match_reasons` would then report *"no firmware matches this chip (kalama)"* — flatly wrong, since
kalama agreed. The operator goes hunting for a build they already own.

This is the same cross-vocabulary trap the gate was built to avoid (`kalama` vs `SM8550`). It was
simply not anticipated *within* the soc field.

Not live today: no library entry carries a kalama `soc` rule (`pocket-max`'s `soc=SM8250` is kona).
This is a latent false-rejection, fixed before it bites.

### Design

**`board_platform` is the PLATFORM; `soc` is the SKU. Same platform + different SKU = same ramdisk.**
So platform agreement outranks a soc conflict:

- If `board_platform` is populated on both sides and **agrees** → a `soc` mismatch **does not reject**.
  It also does not affirm (it contributes nothing to `agreed`).
- If `board_platform` did **not** compare (absent on either side), a `soc` conflict rejects exactly as
  it does today — `soc` remains the fallback chip axis when `board_platform` is unavailable.
- A `board_platform` conflict still rejects unconditionally. Nothing weakens that.

`agreed` semantics are unchanged: only chip axes affirm, and a rejected firmware still returns
`agreed = 0`.

### Why not the alternatives

- **Normalize SoC SKUs (`QCS8550` ≡ `SM8550`).** A per-chip-family lookup table that must be
  maintained for every future part and silently rots the moment one is missed.
- **Drop `soc` from the gate.** Loses the fallback for a device or build that reports `soc` and no
  `board_platform`.
- **Leave it.** The failure presents as a confusing "no firmware matches this chip (kalama)" when
  kalama matched fine — precisely the class of misleading message this project has been removing.

### Testing

- `board_platform` agrees + `soc` conflicts → **passes**, `agreed == 1` (platform affirmed; soc adds
  nothing). Use the real values: firmware `board_platform=kalama, soc=SM8550` vs device
  `board_platform=kalama, soc=QCS8550`.
- `board_platform` absent on the firmware + `soc` conflicts → **rejects** (fallback intact).
- `board_platform` absent on the device + `soc` conflicts → **rejects**.
- `board_platform` conflicts → **rejects**, regardless of `soc`.
- `board_platform` agrees + `soc` agrees → passes, `agreed == 2` (unchanged).
- A rejected firmware still returns `agreed == 0`.
- Mutation: make a `soc` conflict reject even when `board_platform` agrees → the QCS8550/SM8550 test
  must fail.

---

## Part 2 — honest backfill reporting

### Problem

Measured on the real library: `python3 -m cas.firmware backfill` ran **91 minutes** (12:14:50 →
13:46:10), printed **nothing until it finished**, and could not help the three entries that needed it.

```
air-x      → board_platform=bengal, android_release=14
air-x-i2c  → board_platform=bengal, android_release=14
ayn-m0     → android_release=10 ONLY (no chip)
ayn-m2     → android_release=10 ONLY (no chip)
pocket-max → board_platform=kona, soc=SM8250, android_release=13
5 firmware backfilled
```

`odin2-default`, `odin3`, and `retroid-pocket-5` have payloads with **no super/system image at all**
(bare `init_boot.img` / `boot.img`). Backfill filled nothing for them **and said nothing about them**.

Three distinct defects:

1. **Zero progress output.** 91 minutes of silence is indistinguishable from a hang. (It also read
   ~19.4 GB off the same USB drive a concurrent Save was using, amplifying an unrelated hang.)
2. **Silent skips.** An entry backfill can *never* help looks identical to one it simply had nothing
   to add to.
3. **Dead-end advice.** `_no_match_reasons` tells the operator to "run backfill" — for entries backfill
   cannot touch, that is a 91-minute round trip to `0 firmware backfilled` and the same `(no match)`.

### Design — report, never guess

**Explicitly rejected: a `device`-field chip fallback.** It was the original proposal and it is wrong.
The `device` field is not reliably a chip:

| entry | `device` | is it a chip? |
|---|---|---|
| `odin3` | `sun` | yes |
| `retroid-pocket-5` | `kona` | yes |
| `retroid-pocket-6` | `kalama` | yes |
| `odin2-default` | `Odin2 (kalama)` | buried in parens |
| `air-x` | `AIR_X` | **no** — product name |
| `pocket-max` | `Pocket_Max` | **no** — product name |
| RP6 kit as ingested | `qssi` | **no** — generic Qualcomm image name |

Reading it as a chip needs a codename allowlist (which rots) or paren-parsing (which guesses), and
would confidently write `AIR_X` or `qssi` as a chip. **A wrong chip GATES and silently false-rejects
forever; a missing chip abstains and is safe.** A wrong chip is strictly worse than no chip. All three
motivating entries are already hand-set (`odin3 --chip sun`, `retroid-pocket-5 --chip kona`;
`odin2-default` needs none — `resolve()` short-circuits `(default kit)` before `match()`). The fallback
would be new heuristic machinery for a problem that no longer exists.

So, three changes, all reporting:

1. **Per-entry progress**, emitted BEFORE each entry is scanned, via the existing `log` callback:
   `[2/9] air-x: scanning 4.8 GB…`. The count and size are already derivable (`list_firmware`,
   `payload_dir`).
2. **`backfill()` returns skips as well as fills**, each with a reason. New return shape:
   `(filled: [(id, dict)], skipped: [(id, reason)])`. The CLI prints every skip. Reasons:
   - `"no super/system image in payload — backfill can never detect this; use 'set --chip'"` — the one
     that matters: it tells the operator the truth (this entry is unfixable by scanning, forever) and
     the exact next command.
   - `"nothing new detected"` — scanned, found nothing missing to add.
   - `"meta.json did not parse — left untouched"` — the corrupt-entry guard.
   - `"payload unreadable (<err>)"` — `detect_build` raised.

   **Correction (found while planning):** an earlier draft of this spec listed `"already has a chip"`
   as a skip reason. That is wrong and must NOT be implemented. `backfill` fills ANY missing gate
   field, not just the chip — skipping a chip-having entry would silently prevent its
   `android_release`/`soc` from ever being filled. `retroid-pocket-6` is exactly that shape today
   (`board_platform` + `android_release`, no `soc`). `"nothing new detected"` covers the case
   correctly. The governing principle is unchanged: no silent skips.
3. **`_no_match_reasons` stops recommending backfill for entries it cannot help.** Today it counts any
   chip-less entry toward "run backfill". It must only count entries whose payload actually has a
   super/system image. An entry with no super image is reported as
   `"N firmware(s) record no chip and have no super image — use 'set --chip'"`.

### Deliberately out of scope

**The per-needle rescan.** `detect_build` calls `_grep_value` once per needle (7 needles; an *absent*
needle costs a full pass over the payload), which is why 19.4 GB takes ~91 minutes. Making it
single-pass multi-needle would cut that toward ~13 minutes. It is a real defect, but it is a change to
a shared helper with its own risk surface, and it is orthogonal to honesty. Progress output makes the
91 minutes *legible*; this spec does not make it shorter. Separate spec if it proves worth it.

### Testing

- `backfill()` returns `(filled, skipped)`; an entry with no super image appears in `skipped` with the
  `"no super/system image"` reason.
- An entry that already has a chip appears in `skipped` with `"already has a chip"`, not silently.
- An entry whose payload yields nothing new appears with `"nothing new detected"`.
- Progress is emitted before each entry (assert via a capturing `log` stub, and that it fires for an
  entry that ends up SKIPPED too — a skipped entry that prints nothing is the bug being fixed).
- `_no_match_reasons` does NOT say "run backfill" when every chip-less entry lacks a super image; it
  says `"use 'set --chip'"` instead.
- `_no_match_reasons` DOES still say "run backfill" when a chip-less entry has a super image.
- Existing `backfill` tests updated for the new return shape — the never-overwrite and
  only-changed-entries rules must remain pinned (both are currently mutation-verified; do not weaken).
- Mutation: make a skipped entry emit nothing → a test must fail.

---

## Non-negotiable

`_img_kernel_size()` in `cas/provision.py` stays untouched and independent of all of the above.
Matching is a heuristic; that check is physics.
