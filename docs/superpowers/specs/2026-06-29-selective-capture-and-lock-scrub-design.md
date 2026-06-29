# Selective per-app capture (Save) + ship-clean Lock scrub

- **Date:** 2026-06-29
- **Status:** Design — awaiting review
- **Area:** `provision/root/{capture.sh,restore.sh,lib-root.sh}`, `cas/{gui.py,provision.py}`, the per-profile `manifest`, `tests/test_cas.py`

## 1. Background

Today CAS clones a golden whole-hog: `capture.sh` grabs **every** third-party app (`user_pkgs`) with its APK + internal data + external BIOS/keys, and `restore.sh` installs + restores data for the apps ticked in the per-profile `manifest`. Inclusion is **one axis per app** (in or out → install APK *and* restore its config). Three gaps surfaced in field work:

1. **No APK/config split.** Some apps should ship **clean** (APK, no golden's saves — e.g. a launcher whose state is per-unit), and some configs belong to apps CAS **doesn't install** (e.g. stock AetherSX2, which the MANGMI OEM launcher self-installs — CAS only wants to drop its BIOS/settings). Neither is expressible.
2. **The default launcher isn't selectable.** It's a system app, excluded by `user_pkgs` (`pm list packages -3`). Its whole-data clone exists only behind the `@homescreen` flag.
3. **Units ship with usage traces + saves.** A provisioned unit carries the golden's (and testers') recently-used apps, last-played games, recent-ROM lists, and **saved game states** — it should ship factory-fresh.

This spec makes capture a **per-app, two-axis selection** (APK / config), surfaces the **default launcher** as a selectable app, and adds a **Lock-time scrub** that wipes targeted usage traces + saved game states before the unit is un-rooted and sealed.

## 2. Goals / Non-goals

**Goals**
- Per-app, independent **APK** and **Config** capture toggles → four outcomes: APK+config, APK-only, config-only, excluded.
- Include the **default launcher** (resolved HOME app) in the selection; its config = whole data dir (incl. `GAME_INFO`), same-family, system-app ownership.
- The Save-time selection is the **single source of truth**; Download deploys the captured set (no per-app subsetting at download). Behavior flags (`@settings/@hardening/@grants/@homescreen`) stay at download.
- A **Lock scrub** that clears targeted usage traces (Android recents, launcher last-played, per-app recent-ROM/MRU/search) **and saved game states**, then proceeds to the existing un-root/seal.
- **Back-compatible:** bare-line manifests and no-selection captures behave exactly as today.

**Non-goals**
- No comprehensive per-app history/log/cache scrub (only the curated "visible traces" + saves).
- No change to rooting, EDL, or the un-root flash itself.
- No cross-family launcher cloning (the launcher clone stays same-family, as today).

## 3. Manifest schema (the per-profile selection)

Each app is a line; **optional trailing tokens** narrow capture. Bare line = both (today's behavior).

```
com.foo                          # APK + config   (default)
com.bar                     apk  # APK only — clean install, no golden's saves/config
xyz.aethersx2.android    config  # config only — app installed elsewhere (OEM launcher); apply its BIOS/settings
com.handheld.launcher    config  # DEFAULT LAUNCHER — config only (whole data dir incl GAME_INFO, same-family)
@settings on   @hardening on   @grants on   @homescreen on
```

- Tokens: `apk`, `config`, or both (space-separated, order-insensitive). Omitting a line = exclude.
- `manifest_pkgs` (existing) still returns the first field (the pkg), so legacy callers are unaffected.
- **New parser** `manifest_axes <manifest> <pkg>` → echoes `apk`, `config`, `apk config`, or (bare line) the default `apk config`. Pure shell, in `lib-root.sh`.

## 4. Save — `capture.sh` + GUI

### 4.1 `capture.sh`
- Resolve the **capture selection**: if `CAS_MANIFEST` is set, capture only its listed pkgs honoring each pkg's axes; else capture everything (back-compat, current `user_pkgs` behavior with both axes).
- Per selected app:
  - **APK axis on** → write `<pkg>/apk/` (as today).
  - **Config axis on** → write `<pkg>/data.tar`, `<pkg>/adata.tar`, and the app's shared internal dir (Citra/RetroArch/ES-DE) — as today.
  - **Config axis off** → skip those (the app ships clean).
  - **APK axis off** → skip `<pkg>/apk/` (config-only: the app is provided elsewhere).
- `pkglist.txt` lists every captured app (apk OR config present). A per-app `meta` line `axes=<apk|config|apk config>` records the intent (so restore can distinguish "config-only by design" from "APK missing by error").
- **Default launcher** is offered for selection even though `user_pkgs` excludes it: when the launcher (`home_launcher`) is in the manifest with `config`, run the existing homescreen capture (`launcher_data.tar` = `/data/data/<launcher>` minus caches, incl. `GAME_INFO`) — this is the `@homescreen` capture, now gated by the launcher's config axis. Its APK axis is ignored (system firmware).

### 4.2 `cas/gui.py`
- The app list shows **two checkboxes per app — `APK` | `Config`** (replacing the single include checkbox). `pkg_vars` becomes `pkg -> (apk_var, config_var)`. Default both on.
- The **default launcher** (resolved from the connected golden's HOME, or the profile's captured `launcher_pkg`) appears in the list with **APK disabled/greyed** and **Config on** by default.
- `save_manifest` writes the tokened lines (bare when both on; `apk`/`config` otherwise) plus the existing `@flags`. Preserves any value-flags (e.g. `@home`) as the current code already must.

### 4.3 `cas/provision.py`
- Save (capture) passes the selection manifest to `capture.sh` (`CAS_MANIFEST`). Download (restore) unchanged in how it invokes restore; behavior below.

## 5. Download — `restore.sh`

- `RPKGS` = **apps present in the payload** (apk OR data) — `payload_pkgs`-style, since the Save selection already pruned the payload. (The manifest still supplies the `@flags`.)
- **Phase 1 (install APK):** install `<pkg>/apk/*.apk` **if present**. If absent:
  - `meta` says `axes=config` (config-only) → **skip install, not a FAIL**; WARN only if the app isn't already installed (`pm path` empty) — its config can't apply to an absent app.
  - `meta` missing/`apk` but no APK → genuine error → FAIL (today's contract).
- **Phase 2 (restore data):** restore `<pkg>/data.tar`/`adata.tar` if present (already gated by file existence). Config-only apps land their data onto the externally-installed app; ownership chowned to the resolved app uid as today.
- **Default launcher:** restored via the existing same-family homescreen path (launcher pkg must match the golden's; system-app ownership = launcher uid, `system_app_data_file` relabel). **Caveat (accepted):** `GAME_INFO` carries the golden's SD serial in **binary** form — the text serial-rewrite misses it — so a cloned unit shows the golden's library/paths **until its first rescan**. Documented, not fixed.

## 6. Lock — usage + saves scrub (`cas/provision.py: seal()` + `lib-root.sh`)

A new **scrub step runs inside `seal()` while the unit is still rooted**, before the Magisk-app removal / un-root flash (steps 2–3). Read-mostly-then-delete; failures WARN (a scrub miss must not strand a unit), and the scrub never runs on the golden (the existing `is_golden()` guard already gates `seal`).

Scrub targets (**"targeted visible traces" + saved game states**):
1. **Android recents** — clear the system recent-tasks list (root: remove `/data/system_ce/0/recent_tasks/*` / `/data/system/recent_tasks/*`, best path per build; `[VERIFY on device]`).
2. **Launcher last-played** — in `com.handheld.launcher`'s `GAME_INFO`: `UPDATE game SET lastOpenedTimestamp=NULL`; clear the datastore `LastPlayGameCollectionKey` (and any `*_last*` recents key). (Mechanism mirrors the offline-sqlite edit pattern: force-stop launcher → edit → restore owner/context.)
3. **Per-app recent-ROM / MRU / search** — a new curated `USAGE_TRACES` list in `lib-root.sh` (same member-relative form as `IDENTITY_EXCLUDES`): files/dirs per emulator that hold recent-ROM lists, search history, MRU (e.g. RetroArch `content_history.lpl`, PPSSPP recents, DuckStation/Citra recent lists — exact paths `[VERIFY on device]`).
4. **Saved game states** — a new curated `SAVE_STATES` list: per-emulator savestate (and in-game save / memory-card) paths. **Open decision (§9):** default removes both savestates *and* in-game saves so units ship with zero progress.

`USAGE_TRACES` and `SAVE_STATES` are seeded for the known emulator set and carry `[VERIFY on device]` markers; the scrub iterates them with `rm -rf`/sqlite `UPDATE` as appropriate, scoped to installed packages.

## 7. Data flow

**Save:** GUI two-axis selection → manifest (tokened) → `capture.sh` writes only the selected APK/config pieces per app (+ launcher data if its config is on) → golden payload encodes the choices.
**Download:** `restore.sh` installs APKs present, restores configs present, handles config-only/apk-only via `meta axes` → converged unit (still carrying usage/saves from setup + testing).
**Lock:** `seal()` → **scrub** (recents + launcher last-played + `USAGE_TRACES` + `SAVE_STATES`) → un-root flash → hide Dev Options → shipped clean.

## 8. Error handling

- Selection parse: unknown token → treat as no token (capture both) + WARN; never abort capture.
- Config-only with the app absent at restore: WARN (recoverable; re-run after the app installs), not FAIL.
- Scrub: every step WARN-on-failure; the scrub is additive cleanup and must not block or fail a seal. The existing "do not seal a unit with restore failures" gate is unchanged.
- Back-compat: a bare-line manifest or a capture with no `CAS_MANIFEST` captures both axes for every app — identical to today.

## 9. Open decisions (resolve at spec review)

- **Saves granularity:** does "saved game states" mean savestates only, or savestates **+ in-game saves / memory cards**? *Spec assumes both* (ship zero progress). Flag if memory-card saves should be preserved.
- **`USAGE_TRACES` / `SAVE_STATES` exact paths:** seeded per known emulator but require on-device discovery; carried as `[VERIFY on device]`.
- **Android-recents clear mechanism:** the exact root path/command varies by build (`recent_tasks` removal vs a `cmd`); pinned during implementation on the AIR X.

## 10. Testing

- Python suite (`tests/test_cas.py`, mocks adb): `manifest_axes` parses `apk`/`config`/both/bare; `save_manifest` round-trips two-axis selection (and preserves `@home`); `provision`(capture) passes the selection; capture writes only selected pieces (assert `apk/` absent for config-only, `data.tar` absent for apk-only); restore treats config-only-missing-APK as WARN not FAIL.
- Shell logic in `capture.sh`/`restore.sh`/the scrub carries `[VERIFY on device]` markers; end-to-end validation on a real MANGMI AIR X (launcher config clone, config-only AetherSX2 BIOS apply, Lock scrub of recents/last-played/savestates).
- All existing tests stay green.

## 11. Scope note

Three components ship together but are independently testable: **(A) two-axis capture + launcher**, **(B) restore deploy semantics**, **(C) Lock scrub**. The implementation plan may sequence them as separate phases; C depends on neither A nor B (it operates on whatever is on the unit at Lock).
