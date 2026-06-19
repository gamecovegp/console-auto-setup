# Modular, Multi-Profile, PC-Sourced Provisioning — Design

**Date:** 2026-06-16 · **Status:** approved (design), pending spec review · **Owner:** Donald (CTO)

## 1. Goal
Evolve the proven single-golden root-clone toolkit (`provision/root/`) into a **modular, multi-profile,
PC-sourced** provisioning system that can set up any handheld variant (Odin, Retroid, Mangmi Air X 256GB,
…) to match a per-variant golden — selecting which emulators and which frontend to include — with the SD
card holding only **bulk game data** (ROMs + large PC-game files) and the **PC sourcing everything else**
(apps, BIOS, configs, scripts, payload). Build the **modular CLI foundation** now; a button-driven GUI is a later
layer over the same orchestrator.

## 2. Scope
**In scope (this spec):**
- A PC-side **profile library** (one profile per device variant/SKU).
- **Manifest-driven** component selection (which emulators / frontend / settings a unit gets).
- **PC-sourced provisioning**: the PC pushes the selected modules to the device; **SD = ROM games only**.
- **Profile update / re-capture** flow (goldens evolve — e.g. add PC games to GameHub).
- Refactor `restore.sh` / `capture.sh` to parametrize payload source + module set.
- Thin PC **orchestrators** (`provision`, `capture-to-pc`) + Windows `.bat` wrappers.
- **Batch provisioning** of multiple connected devices at once — each auto-matched to its own profile.

**Out of scope (later):**
- The **GUI app** (buttons: pick device → preset → toggle emulators → choose frontend). The orchestrator +
  manifest are designed so the UI is a thin front-end, but no UI is built here.
- Compose-from-scratch / cross-profile module sharing (we use **superset golden per variant**).
- A brick-recovery full-firmware flow (tracked separately in `flash-bringup/`).

## 3. Background / current state (proven)
- `provision/root/`: `capture.sh`, `restore.sh`, `lib-root.sh`, `verify.sh` (+ dev/diagnostic scripts).
- Payload `golden_root_payload/` = per-app modules (`<pkg>/{apk,data.tar,adata.tar,meta}`), internal-storage
  dirs (`internal_ES-DE.tar`, `internal_citra-emu.tar`, `internal_RetroArch.tar`), `urigrants.xml` (SAF
  grants), `settings/` (system/secure/global dumps), `pkglist.txt`, `global.meta`.
- **Validated end-to-end** on a factory-reset unit: root survives reset, all apps reinstall, `Android/data`
  ownership fix proven with new UIDs, keys/BIOS/grants/cores/settings correct.
- **Today the SD carries** payload + scripts + ROMs + Bios + apps. **This spec moves all non-ROM data to
  the PC.**

## 4. Architecture overview
Six pieces:
1. **Component library** — the payload, already one self-contained module per app + internal dirs + grants.
2. **Manifest** — selects which modules to apply for a job (the "modular" knob; what the UI will edit).
3. **Profile** — per device variant: a *superset* golden payload + default manifest(s) + match metadata.
4. **Provision orchestrator (PC)** — pick profile → push selected modules → run restore → cleanup → reboot.
5. **Capture orchestrator (PC)** — run capture on a golden → pull into the profile (with `.prev` versioning).
6. **SD = bulk game data** (ROMs + large PC-game files) — never written by *provisioning* (no
   apps/BIOS/scripts/payload on it); it only supplies game files at runtime.

## 5. Component library (module types)
Each is independently captured/restored:
- **App module** `<pkg>/`: `apk/` (exact installed APKs) + `data.tar` (`/data/data/<pkg>`) + `adata.tar`
  (`Android/data/<pkg>` — firmware/BIOS/keys/nand) + `meta`. Covers emulators, **frontends** (ES-DE,
  GameHub, Cocoon), and **native game apps** (Stardew, Balatro mobile) alike.
- **Internal-dir module** `internal_<dir>.tar`: shared internal-storage state coupled to an app —
  `ES-DE`↔`org.es_de.frontend`, `citra-emu`↔`org.citra.emu`, `RetroArch`↔`com.retroarch.aarch64`.
  Restored only if its owning app is in the manifest (coupling defined in `lib-root.sh:internal_for()`).
- **SAF grants** `urigrants.xml`: per-package folder grants (serial-rewritten per unit).
- **Settings** `settings/`: device-experience allowlist applied at restore.
- **Cores**: RetroArch cores ride inside `com.retroarch.aarch64/data.tar` (no separate transfer).

## 6. Manifest (the modular knob)
A line-based file (sh-parseable; no JSON dependency on toybox). Default per profile = every captured app.
```
# manifest — apps to apply on this unit (one per line). Frontend is just an app line.
org.es_de.frontend          # frontend
dev.eden.eden_emulator
com.github.stenzek.duckstation
... (any subset of the profile's payload) ...
# flags
@settings   on              # apply the settings allowlist
@hardening  on              # Doze-exempt + OTA-disable
@grants     on              # rebuild SAF grants for included apps
```
- `restore.sh` already loops a package list (`RPKGS`) — the manifest **is** that list. Excluding an emulator
  = omit its line; choosing Cocoon vs ES-DE = include that frontend's line.
- Internal dirs + SAF grants are auto-scoped to the included apps (via `internal_for()` and per-pkg grant
  filtering) — so the manifest stays a simple app list.
- **The future UI edits this file** (checkboxes → lines) and calls the provision orchestrator.

## 7. Profile layout (PC)
Profiles are **per device variant/SKU** (arbitrary names):
```
profiles/
  odin2mini/            golden_root_payload/  manifest  profile.meta
  retroid-p6/           golden_root_payload/  manifest  profile.meta
  mangmi-airx-256/      golden_root_payload/  manifest  profile.meta   # incl GameHub PC games
scripts/                restore.sh  lib-root.sh  verify.sh             # shared, versioned once
provision.sh  provision.bat
capture-to-pc.sh  capture-to-pc.bat
```
`profile.meta`: `model_match=` (regex on `ro.product.model` for auto-select), `frontend=`, `notes=`,
`captured=<date>`. Each `golden_root_payload` is a *superset* (e.g. Odin carries ES-DE **and** Cocoon) so
the manifest can pick.

## 7.1 Profile management — the collection (choose / update / delete)
A `profiles` command (and the future UI's left panel) manages the collection:
- **list** — `profiles list`: name, frontend, `model_match`, captured date, module count. The UI shows these
  as the selectable buttons.
- **choose** — provisioning takes a profile by name, or auto-matches `ro.product.model`; the UI = one tap.
- **update content (easiest update path)** — `capture-to-pc <profile>`: re-capture the golden after you
  change it (add GameHub games, tweak settings). Rotates the old payload to `.prev` for one-step rollback.
- **update selection** — edit the profile's `manifest` (which modules a unit gets). The UI = checkboxes.
- **new / clone** — `profiles new <name> [--from <existing>]` to spin a variant off an existing profile
  (e.g. `mangmi-airx-256` from `mangmi-airx-128`).
- **delete — DELIBERATELY HARD (anti-footgun)**: `profiles delete <name>` must NOT `rm`. It:
  1. Requires the operator to **type the exact profile name** to confirm (no accidental Enter).
  2. **Moves** the profile to `profiles/_archive/<name>_<YYYYMMDD>/` rather than deleting — fully
     recoverable. (Matches the repo rule: never delete, move to `_archive/`.)
  3. The UI surfaces delete behind a confirm dialog wired to the same name-typing guard.

These map 1:1 to future UI buttons (List / Select / Re-capture / Edit / New / Delete-with-confirm).

## 8. Capture / update flow (per golden)
`capture-to-pc <profile>`:
1. Verify device is the intended golden (root, `.cas_golden` lock present), reachable.
2. `adb shell su -c "CAS_OUT=/data/local/tmp/cas_cap sh .../capture.sh"` → writes payload to internal temp
   (NOT the SD — SD is games-only).
3. If `profiles/<profile>/golden_root_payload` exists → rotate it to `…/.prev` (one-deep fallback).
4. `adb pull /data/local/tmp/cas_cap` → `profiles/<profile>/golden_root_payload`; `adb shell rm -rf` the temp.
5. Regenerate the default `manifest` (all captured apps) unless one exists (preserve operator edits).

**Updating a golden** (e.g. add Tunic/Hades/Balatro/Stardew to GameHub on Mangmi Air X 256): set it up on
the golden, then re-run `capture-to-pc mangmi-airx-256`. The new app modules appear in the payload + default
manifest automatically. `.prev` lets you roll back a bad update.

**Capture once, re-setup anytime.** A captured golden becomes a *persistent profile* on the PC — the
durable source of truth. Re-apply it to any matching unit, or re-setup the *same* device after a wipe/reset,
at any later time, with no re-capture. The golden device is disposable once captured; only the profile must
be preserved (hence the archive-not-delete guard in §7.1).

## 9. Provision flow (per unit) — PC-sourced
`provision <profile|auto>`:
1. Resolve profile (explicit arg, or auto-match `ro.product.model` against each `profile.meta:model_match`).
2. Refuse if the device is a golden (`.cas_golden` present) — safety.
3. Ensure root (`/debug_ramdisk/su`); if shell not granted, instruct the one-time Magisk toggle.
4. Read the profile's `manifest`; **push only the selected modules** (+ `urigrants.xml`, `settings/`,
   internal dirs for included apps) + `scripts/` → `/data/local/tmp/cas/`. (Pushing the subset, not the
   whole payload, keeps USB time down for trimmed manifests.)
5. `adb shell su -c "CAS_PAYLOAD=/data/local/tmp/cas/payload CAS_MANIFEST=/data/local/tmp/cas/manifest sh
   /data/local/tmp/cas/restore.sh"`.
6. `adb shell su -c "rm -rf /data/local/tmp/cas"` (the pushed payload is transient; restored data now lives
   in `/data`).
7. `adb reboot`; then `verify.sh`.

Profile resolution: **auto-match** `ro.product.model` against each `profile.meta:model_match`; **`--profile
<name>` overrides** the auto-match (decided).

## 9.1 Batch provisioning (multiple devices)
`provision --all` (and the future UI's "provision all connected"):
1. Enumerate every connected device via `adb devices` (each has a unique serial).
2. For **each** device, auto-match its own profile from `ro.product.model` — so a mixed batch (some Odin,
   some Mangmi) is fine; each gets the right profile. `--profile <name>` forces one profile for the whole batch.
3. Provision each via `adb -s <serial> …` (the single-device flow above, serial-scoped).
4. **Default sequential** (simple, reliable; the 3 GB push per device shares one USB bus). A
   `--parallel[=N]` option runs N at once for wall-clock speed (bandwidth-bound) — tunable, off by default.
5. Per-device pass/fail summary at the end; a failed unit doesn't abort the batch.

## 10. Code refactors
- **`restore.sh`**: `P="${CAS_PAYLOAD:-$SD/golden_root_payload}"` (PC-push path, falls back to SD for
  back-compat); `RPKGS` from `${CAS_MANIFEST}` if set, else `pkglist.txt`, else `PKGS`; internal dirs via
  `internal_for(pkg)` only for included apps; `detect_sd` used **only** for the ROM path + serial-rewrite.
- **`capture.sh`**: `P="${CAS_OUT:-$SD/golden_root_payload}"` (internal temp for PC pull).
- **`lib-root.sh`**: add `internal_for()` (pkg→internal dir coupling); manifest parser helper.
- All other behavior (APK staging to `/data/local/tmp`, `Android/data` re-own to `uid:1078`,
  `MANAGE_EXTERNAL_STORAGE` grant, settings, hardening) is unchanged and already proven.

## 11. Error handling / safety / idempotency
- `provision` refuses a golden (`.cas_golden`), refuses if not rooted, verifies free space before push.
- `restore.sh` is idempotent (re-runnable); cable drop → reseat + re-run.
- `capture-to-pc` rotates `.prev` before overwriting (rollback).
- `verify.sh` extended to check only the manifest's modules.

## 12. Decisions (resolved) + remaining detail
- **Large PC-game files — RESOLVED: keep on the SD (for now).** Multi-GB compat-layer titles (Hades, Tunic)
  live on the **SD** alongside ROMs (a "PC-games" area); the PC-pushed payload carries only **app modules +
  small native ports** (Stardew, Balatro mobile). So the SD isn't *strictly* ROM-only — it also holds these
  big game files — but it's still "bulk game data on the card, everything else from PC." Revisit later.
- **Profile selection — RESOLVED: auto-match `ro.product.model`, with `--profile <name>` override.**
- **Push subset vs whole payload** (impl detail): spec assumes push-only-selected modules; if that
  complicates, fall back to push-whole + restore-filters-by-manifest.

## 13. Testing / verification
- `canary.sh` (single-pkg, exists) for the live restore path.
- Full `provision <profile>` on a factory-reset unit → `verify.sh` → boot a game per system.
- A trimmed-manifest run (exclude one emulator) to prove modular selection.

## 14. Future UI (out of scope — the seam)
The GUI is a thin layer that (a) lists profiles + their modules, (b) writes a `manifest` from checkboxes,
(c) calls `provision <profile>`. Nothing in the UI touches the device directly — it drives the same
orchestrator the CLI uses. Designing the manifest + orchestrator now makes the UI additive.

## 15. Migration from current state
1. Add `CAS_PAYLOAD`/`CAS_OUT`/`CAS_MANIFEST` parametrization to `restore.sh`/`capture.sh` (back-compatible).
2. Create `profiles/odin2mini/` from the existing validated payload; write its `manifest` + `profile.meta`.
3. Build `provision` + `capture-to-pc` orchestrators + `.bat` wrappers; update `windows-kit`.
4. Re-capture the Odin golden into the profile; provision a test unit PC-sourced (SD = ROMs only).
5. Add `retroid` / `mangmi-airx-256` profiles as those goldens are built.
</content>
