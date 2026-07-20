# CAS Firmware Slimming — Design

**Date:** 2026-07-20
**Status:** approved, not implemented
**Supersedes nothing.** Amends the retention policy of
`2026-06-27-cas-firmware-library-design.md` (§3 "keep all", §5 ingest).

---

## 1. Background

`_firmware/` is 31.4 GB. CAS reads **745 MB of it — 2%.**

Measured on the live library (2026-07-20, drive `6045-F51C`):

| category | size | consumer |
|---|---|---|
| `super_*` / `system_*` / `userdata_*` / `vm-bootsys` images | 26.3 GB | `detect_build()` **at ingest only** (`firmware.py:543`) |
| other package files | 4.3 GB | nothing |
| boot/init_boot images, EDL tools, programmers, rawprogram XML | **0.745 GB** | the flash path |

Nothing in root, seal, or EDL flashing touches the bulk. Verified by grepping
every reference to `super_`/`system_`/`userdata` in `cas/` — the only hits are
`detect_build()` and the two helpers that report on it.

### Why the library grew this way

Two assumptions from the original design are now false:

1. **"No flashing / EDL automation. CAS exposes paths + flash targets; the
   operator flashes."** (§2 Non-goals.) CAS has since grown real fastboot *and*
   EDL/Firehose flashing, so copying the whole vendor tree is no longer what the
   consumer needs — it was material for a manual QFIL step CAS now performs itself.
2. **"Default: keep all (NAS has room)."** (§3.) The NAS was dropped for being
   ~7 MB/s; the library is now a 239 GB USB drive at ~23 MB/s read.

### Operator context (decided 2026-07-20)

- Donald flashes **only through CAS**. Manual QFIL is not a bench workflow.
- **Space is not the goal — a clean working set is.** Masters stay on the same
  drive; the win is that CAS's working set contains only what it flashes.
- 5 of 10 builds (`air-x`, `air-x-i2c`, `pocket-max`, `ayn-m0`, `ayn-m2`,
  = 22 GB) have **no surviving source** — their `source` paths
  (`data/device-firmwares/…`) are gone, so the library copy is the only master.
  This is why masters are moved and never deleted.

### The end state already exists

`retroid-pocket-5` (98 MB) and `odin3` (9.8 MB) are already essentials-only —
just the flash image, no bulk — and both resolve and flash correctly through the
normal path. Slim builds are proven in production, not theoretical.

(The RP5's separate ③ Lock failure is its image being a Magisk-patched overclock
build, not its slimness — see `cbce171`. A slim build and a rooted build are
unrelated properties.)

---

## 2. Goals / Non-goals

**Goals**

1. CAS's firmware working set contains **only files CAS reads**.
2. Adding firmware keeps only what is needed, by default, in one code path
   shared with the cleanup of existing builds.
3. Full masters are **retained and restorable**, never deleted.
4. Build metadata is **captured before** the bulk it is derived from moves away.
5. A documented, visual procedure for adding firmware correctly.

**Non-goals**

- Not a space-reclamation project. Masters stay on the same drive.
- No change to matching, gating, or flashing behaviour. Relative paths inside
  `payload/` are preserved, so every existing glob keeps resolving.
- Not fixing the kalama `.melf` EDL programmer gap. No build in the library uses
  a `.melf`, and RP6 resolves as `fastboot`; the essentials list simply reserves
  the pattern so a future kalama-EDL build is not slimmed into breakage.
- No pruning of `versions/` history, `.prev` payloads, `_archive/`, or
  `retroarch-cores`. Separate decisions, tracked in §9.

---

## 3. What is essential

Determined by **calling the real accessors**, never by re-deriving their globs.
This is the load-bearing rule: if the slim set were computed from a duplicated
glob list, a future change to `stock_boot_image()` or `edl_tools()` would
silently start deleting files CAS had begun to need.

```
ALWAYS
  fw.stock_boot_image(version)          → boot.img | init_boot.img

IF fw.flash_method == "edl"
  QSaharaServer, QSaharaServer.exe      → edl_tools()
  fh_loader, fh_loader.exe              → edl_tools()
  prog_firehose*.elf                    → edl_tools()
  *devprg*.melf                         → reserved (kalama EDL, see §2)
  rawprogram*.xml                       → init_boot_geometry()
  patch*.xml                            → reserved, kilobytes

EVERYTHING ELSE                         → master
```

Code-verified resolution across the live library:

| firmware | method | target | needs |
|---|---|---|---|
| air-x, air-x-i2c, pocket-max | edl | init_boot / boot | image + EDL tools + programmer + rawprogram |
| ayn-m0, ayn-m2 | fastboot | boot | **image only** |
| ayn-thor, odin2-default, odin3, retroid-pocket-6 | fastboot | init_boot | **image only** |
| retroid-pocket-5 | fastboot | boot | **image only** |

Seven of ten builds need exactly one file.

Both host variants of the EDL tools are kept (`QSaharaServer` *and*
`QSaharaServer.exe`): `edl_tools()` prefers the host-appropriate build and falls
back to the other so a wrong-OS package still *detects* as EDL and reports the
mismatch cleanly. Keeping only one variant would turn that clean report into a
"not an EDL build" misdiagnosis.

**`flash_method` is derived from these files.** `detect_build()` sets
`is_edl` from the presence of QSaharaServer + fh_loader + `prog_firehose*.elf`.
Slimming an EDL build without them would flip it to `fastboot` — a doomed
bootloader flash on a unit whose bootloader cannot write. Hence they are
mandatory essentials for EDL builds, not an optimisation.

---

## 4. Storage layout

```
CAS Profiles/
  _firmware/<id>/versions/<version>/payload/      # essentials only, ORIGINAL relative paths
  _firmware/<id>/versions/<version>/version.meta.json
  _firmware_masters/<id>/<version>/payload/       # the full vendor tree, moved here
```

`_firmware_masters/` sits beside `_firmware/` on the **same volume**, which makes
the master move a filesystem rename: atomic, instant, and free of the
half-copied-22 GB failure mode. It is deliberately outside `_firmware/` so
`list_firmware()` and every `rglob` in the module never walk it.

The library drive is **exFAT**. Directory rename across paths on the same volume
is verified working there (2026-07-20), and exFAT has no 4 GB file cap, so the
2.5 GB `super_*.img` members move intact. No symlinks are used — consistent with
the original design's CIFS/Windows-safe constraint.

---

## 5. The slim operation

```
firmware slim <id>[@version] [--all] [--dry-run]

1. Resolve Firmware + version. Already slim (version.meta.json: slim=true) → no-op.

2. METADATA GATE
   If version.meta.json lacks fingerprint or board_platform, run detect_build()
   NOW — while super_*.img still exists — and merge the result.
   If detection still yields neither field → REFUSE, change nothing.

3. Compute the essential set (§3) by calling the accessors.
   Empty set, or no stock image → REFUSE.

4. --dry-run → report kept/removed file counts and bytes; stop.

5. MOVE payload/ → _firmware_masters/<id>/<version>/payload/   (os.replace)

6. Recreate payload/ and copy the essentials back at IDENTICAL relative paths.

7. VERIFY: reopen the Firmware. stock_boot_image(), edl_tools() and
   init_boot_geometry() must resolve to the same shape as step 3.
   Any regression → move the master back to payload/, delete the partial, FAIL.

8. Stamp version.meta.json:
   slim=true, master_at=<relpath>, removed_bytes, removed_files, slimmed_utc
```

### Why the metadata gate comes first

8 of 10 builds currently have an **empty** `version.meta.json` — no fingerprint,
no chip. The `super_*.img` files are the only place that data exists. Moving
them away first would make it unrecoverable, and that data is exactly what
resolves auto-match ties (the `retroid-pocket-5` vs `pocket-max` `kona` tie found
on 2026-07-20, where both scored equally and `match()` returned `None`).

This step is slow and one-time: a multi-GB grep per build at ~23 MB/s. It must
log progress, because the existing `backfill` reads 19.4 GB with no output for
91 minutes and reads as a hang.

### Restore

`firmware unslim <id>[@version]` — move `_firmware_masters/<id>/<version>/payload/`
back over `payload/`, clear the `slim` stamp. Restores the full vendor tree for a
build that ever needs manual QFIL.

---

## 6. Ingest integration

`ingest(src, root, …, slim=True)` — default on:

```
copy tree → detect_build() → write version.meta.json → slim()
```

One code path, so "adding firmware" and "cleaning old firmware" cannot drift.
`--keep-full` opts out for a build that is being investigated.

**Entry points.** There is currently **no `cas firmware` CLI at all** — `cli.py`
only references the module for the init_boot store. Ingest is reachable solely
through the GUI (`gui.py:_add_firmware` → `FW.ingest`). This design adds
`slim` / `unslim` / `verify` as library functions plus a thin CLI group, and
wires `slim` into the existing GUI ingest so the default path needs no new
operator action. The operator tutorial marks CLI steps as planned until that
lands.

Note the ordering already favours this: `detect_build()` runs on `src` *before*
the copy, so on the ingest path the metadata gate in §5 step 2 is already
satisfied and costs nothing.

---

## 7. Safety invariants

1. **Nothing is deleted.** Masters are moved; restore is a move back.
2. **Refuse rather than guess.** No recoverable metadata → no slim.
3. **Verify or roll back.** Every slim re-resolves the accessors and reverts on
   any regression.
4. **Atomic boundary.** The same-volume rename means a crash leaves either the
   full payload or the master — never a half-tree.
5. **Idempotent.** Re-slimming is a no-op.
6. **Paths preserved.** Essentials return to identical relative paths, so no
   glob, `_payload_glob`, or consumer anywhere in CAS changes.

---

## 8. Testing plan (TDD, tmp library, no device)

| # | Test | Asserts |
|---|---|---|
| 1 | fastboot build | keeps exactly the stock image; bulk moved to masters |
| 2 | edl build | keeps image + both host tool variants + programmer + rawprogram |
| 3 | edl build stays edl | `flash_method` still `"edl"`, `edl_tools()` still resolves post-slim |
| 4 | metadata absent, detect succeeds | metadata written **before** the move |
| 5 | metadata absent, detect fails | REFUSES; payload untouched |
| 6 | verification regression | master restored, payload intact, returns False |
| 7 | idempotency | second slim is a no-op |
| 8 | dry-run | reports numbers, changes nothing on disk |
| 9 | unslim | full tree restored byte-identical |
| 10 | ingest(slim=True) | new build lands slim with metadata populated |

Bench check before anything moves: `firmware slim --all --dry-run` against the
real library, reviewed by the operator.

---

## 9. Open items (explicit, not blockers)

- **`.prev` rollback payloads — 5.7 GB.** Every profile keeps a full-size
  rollback forever. Needs a retention policy; out of scope here.
- **`_archive/odin2mini_20260626` — 11 GB.** One old golden. Disposition undecided.
- **`retroarch-cores` — 2.4 GB, 67% in 8 files** (`qemu` alone is 767 MB / 33%).
  A product decision about which cores the fleet needs. The push itself is now
  gated on RetroArch being deployed (`f28672e`).
- **RP6 256 vs 512 payloads** differ by 2 MB out of 2.26 GB (same app set). A
  shared base + tier overrides would save ~2.2 GB.
- **Leaked `cas_xfer_*.tar` — 2.28 GB** at the library root, from a run that died
  before `_push_dir`'s `finally` could unlink it. Wants a startup sweep of stale
  transfer archives.
- **`backfill`** reads 19.4 GB in 91 minutes with no output and does not populate
  the `device` field it needs. The metadata gate (§5) overlaps its purpose and
  should probably replace it.
