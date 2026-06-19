# Per-tier ES-DE via capture-per-tier golden — Design

**Date:** 2026-06-18
**Status:** Approved for planning
**Author:** Donald (CTO) + Claude

## Problem

ES-DE content must vary by SD-card storage tier, because a larger card carries
more games — and therefore a different ES-DE home (gamelists, downloaded_media,
collections must match the ROM set present). The ES-DE home is authored by us
("comes from us"), not generated per customer. We need CAS to ship the correct
ES-DE variant to each unit, fast, for a ~10–12 GB payload.

Today ES-DE rides on the SD card; the goal is to store the ES-DE home (and BIOS)
**on the device's internal storage** and map ES-DE from there, removing the
per-unit SD dependency for ES-DE/BIOS (ROMs still ride the SD).

## Decisions (locked during brainstorming)

1. **Variant selection = one profile per tier.** A profile encodes device-model
   × SD-tier (`rp6-256gb`, `rp6-512gb`, `odin2mini-512gb`, …). Selection is which
   profile the operator runs. Reuses existing profile machinery.
2. **Push is folded into the existing provision/restore.** ES-DE rides inside
   `restore.sh` as the `internal_ES-DE.tar` it already handles. No new command.
3. **Masters are produced by capturing a golden per tier** — no new *production*
   mechanism (no inject/stamp/shared-asset machinery); the existing capture→restore
   pipeline carries ES-DE. The tier's golden *is* the curated source of truth.
4. **Add a capture-time guard** against silently shipping a tier with no ES-DE.
   This is the lone code change in scope; `restore.sh` is untouched.

## Architecture / model

- ES-DE ↔ internal-storage coupling already exists:
  `org.es_de.frontend → ES-DE` in `INTERNAL_DIRS` (`provision/root/lib-root.sh:10,12`).
- `capture.sh` tars `/storage/emulated/0/ES-DE` → `internal_ES-DE.tar`
  (`provision/root/capture.sh:38-44`) **only if the dir exists and is non-empty**.
- `restore.sh` extracts `internal_ES-DE.tar` → `/storage/emulated/0/ES-DE`
  **only if `org.es_de.frontend` is in the manifest** (`provision/root/restore.sh:128-136`).
- A profile = `profiles/<model>-<tier>/` with `profile.meta` (incl. `model_match`),
  `manifest` (lists `org.es_de.frontend`), and `golden_root_payload/` containing
  `internal_ES-DE.tar` + per-app apk/data + BIOS (`adata.tar`) + grants.

Net: with ES-DE on internal at golden time, the existing capture→restore pipeline
carries the per-tier ES-DE home end-to-end. The only addition is the safety guard.

## Per-tier golden prep (runbook — done once per tier)

1. Insert the tier's SD (its ROM set for that capacity).
2. Install ES-DE; set its home to `/storage/emulated/0/ES-DE` (internal); map the
   ROM directory to the SD via ES-DE's SAF picker.
3. Scrape/arrange so gamelists + `downloaded_media` reflect that tier's games.
   Confirm `/storage/emulated/0/ES-DE` is populated (not the SD copy).
4. Root the unit (Magisk-patched init_boot flash) — capture requires root.
5. `python -m cas.cli capture <model>-<tier>` → `profiles/<model>-<tier>/`;
   payload gets `internal_ES-DE.tar` + apps/data/BIOS/grants.
6. Fill `profile.meta` (`model_match`, `frontend=es-de`, firmware paths) and
   confirm `manifest` lists `org.es_de.frontend`.

## Provisioning a unit

- Operator selects the matching **tier profile explicitly** (see constraint below).
- `restore.sh` installs apps, restores data/BIOS, extracts `internal_ES-DE.tar`
  → internal, rewrites the golden SD serial → this unit's serial in the SAF grants.
- Transfer is **single-tar streams** (one `adb push` per internal tar, extracted
  on-device), so the ~11.5 GB ES-DE moves at USB line-speed — no 41k-small-file
  penalty. Root or no-root is immaterial to the speed; restore uses root anyway.

## The one code change — capture guard

**Goal:** never ship a tier whose ES-DE silently failed to capture (exactly what
happened to the Odin golden, whose payload has no `internal_ES-DE.tar` because
ES-DE was on the SD at capture time).

Two layers:

1. **Device-side visibility (`provision/root/capture.sh`).** After the
   `INTERNAL_DIRS` loop, for each captured pkg that has an `internal_for` mapping
   (ES-DE / Citra / RetroArch), warn if its `internal_<dir>.tar` is absent/empty.
   For `org.es_de.frontend` specifically, bump `CFAIL` (capture already exits
   non-zero when `CFAIL > 0`, lines 87-93) — an ES-DE golden with ES-DE not on
   internal is wrong by definition in this design. Citra/RetroArch stay
   warn-only (their internal dirs may legitimately be empty).

2. **PC-side authoritative gate (`cas/provision.py: capture_to_pc`).** The pull
   already verifies `global.meta` + `pkglist.txt` before rotating good→prev
   (`provision.py:162-165`). Add: if the pulled `pkglist.txt` contains
   `org.es_de.frontend`, require a **non-empty** `internal_ES-DE.tar` in the
   incoming payload; otherwise treat the capture as failed and leave the existing
   profile untouched. This is the real gate — it blocks a bad golden from
   overwriting a good profile.

No change to `restore.sh`.

## Constraints (accepted, documented)

- **Tier disambiguation.** Two tiers of the same device share a `model_match`
  (both `Retroid_Pocket_6`), so `match_profile` returns ambiguous → refuses to
  guess (`profiles.py:106-120`); `provision-all`'s auto-match likewise can't split
  tiers. **Mitigation:** operator picks the tier profile explicitly; naming
  convention `<model>-<tier>` keeps the list readable. Batch-by-model across
  mixed tiers is out of scope.
- **ES-DE duplicated across device models.** ES-DE home is device-agnostic per
  tier, but capture-per-tier re-captures it inside each device-model profile.
  Accepted tradeoff for zero new capture/restore code (vs a shared tier asset).
- **Payload size.** Each tier profile's payload grows by ~11.5 GB (the ES-DE
  home, mostly `downloaded_media`). Accepted — it's the cost of SD-independence.

## Speed

- Per-unit transfer is already optimal: one tar stream per internal dir, extracted
  on the device. The earlier slowness was a one-time SD→internal `cp -r` of 41k
  files, not the provisioning path.
- No gzip (downloaded_media is already-compressed JPEG/PNG/video).
- Possible later enhancement (out of scope here): parallelize `provision_all`
  across multiple connected units; today it is sequential.

## Testing

- **Guard, device-side:** on a golden with ES-DE on the SD (empty internal),
  `capture.sh` must exit non-zero and name ES-DE. On a golden with ES-DE on
  internal, it must succeed and produce a non-empty `internal_ES-DE.tar`.
- **Guard, PC-side:** `capture_to_pc` must refuse to rotate (existing profile
  untouched) when the incoming `pkglist` includes ES-DE but `internal_ES-DE.tar`
  is missing/empty; must succeed otherwise. Existing `tests/` cover the rotate
  path — extend with these two cases using a fixture payload.
- **End-to-end (manual):** capture an RP6-<tier> golden, provision a wiped RP6,
  confirm ES-DE launches from internal and lists that tier's games, with the SD's
  ROMs mapped via the rewritten grant.

## Out of scope / YAGNI

- No standalone "push ES-DE" command (folded into restore, per decision 2).
- No shared/referenced tier-asset store (per decision 3).
- No auto-detect of tier from SD capacity or SD tag (per decision 1).
- No ROM management — ROMs are prepared on the SD separately.
