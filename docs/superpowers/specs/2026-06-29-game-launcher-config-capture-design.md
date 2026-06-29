# Game-launcher config capture & restore (auto-detected, portable)

- **Date:** 2026-06-29
- **Status:** Design — awaiting review
- **Area:** `provision/root/{lib-root.sh,capture.sh,restore.sh}`, `cas/{gui.py,provision.py,profiles.py}`, the per-profile `manifest`, `tests/test_cas.py`
- **Builds on:** `2026-06-29-selective-capture-and-lock-scrub-design.md` (per-app two-axis capture), `2026-06-27-default-launcher-and-update-mode-design.md` (HOME-role launcher — DISTINCT from this).

## 1. Background

The MANGMI handhelds run **two different launchers**:

- the Android **HOME app** — plain `com.android.launcher3` (what `home_launcher()` resolves and what the selective-capture / `@homescreen` path captures), and
- the **game frontend** — `com.handheld.launcher` (a rebranded ES-DE fork, system app), the app that actually holds the **per-system emulator picks** the operator sets by hand (PSX→DuckStation, PSP→PPSSPP, …).

Verified 2026-06-29 on AIR X `MQ66142509130541`: the game frontend stores those picks in its **Jetpack DataStore** — `/data/data/com.handheld.launcher/files/datastore/GameLauncher.preferences_pb` — as keys `<system>_select_emulator` holding a **token** launch template, e.g.

```
psx_select_emulator → %EMULATOR_DUCKSTATION% %ACTIVITY_CLEAR_TASK% %ACTIVITY_CLEAR_TOP% %EXTRABOOL_resumeState%=false %EXTRA_bootPath%=%ROMSAF%
psp_select_emulator → %EMULATOR_PPSSPP% %ACTION%=android.intent.action.VIEW … %DATA%=%ROMFILEPATH%
```

This override is what the launcher honors at launch — it **beats** the `GAME_INFO` SQLite scan-cache (whose `psx` row still shows the RetroArch+mednafen scan default). Because the values are **token-based** (`%EMULATOR_*%`, `%ROMSAF%`) with no SD serial or absolute path, the DataStore is **portable across units**; `GAME_INFO` is not (its ROM `path`s are bound to the SD volume serial and it self-rebuilds on first scan).

**Two gaps:**
1. **Wrong launcher detected.** The selective-capture feature surfaces "the default launcher" but resolves it as the HOME app (`home_launcher` → `com.android.launcher3`). The game frontend (`com.handheld.launcher`) is a *separate* app with **no OS "default" marker** (Android has one HOME role, held by launcher3), so it is never captured — the emulator picks are lost on a fresh unit and must be re-set by hand.
2. **Non-portable clone.** Where the launcher *is* captured, it's the whole data dir incl `GAME_INFO` — SD-serial-bound, scan-rebuilt — not the portable DataStore.

This spec adds an **auto-detected "game launcher"** concept (distinct from the HOME app), captures **only its portable config** on Save, and **re-applies it on Download** after auto-detecting the target unit's game launcher.

## 2. Goals / Non-goals

**Goals**
- A `game_launcher()` resolver: **probe → list → override** — find the frontend that holds the emulator-pick config, independent of the HOME role.
- Save **portable config only** for that launcher: the DataStore (`files/datastore/*.preferences_pb`) + `shared_prefs`, **excluding** `GAME_INFO*` and caches.
- Download **auto-detects the target's game launcher** and writes the saved config back as a **system app** (force-stop → extract → `system:system` + `system_app_data_file` relabel).
- Additive/best-effort throughout (WARN, never FAIL) — same treatment as the existing `@homescreen` path.
- Back-compatible: profiles with no game-launcher capture behave exactly as today.

**Non-goals**
- No cloning of `GAME_INFO` (SD-bound, scan-rebuilt — explicitly excluded).
- No cross-family launcher cloning (same-family only, as today).
- No change to the HOME-role launcher / homescreen-layout capture (`@homescreen`) or the 2026-06-27 "set ES-DE as HOME" feature — those stay as-is.
- No protobuf-level key merge on restore (whole-file overwrite — see §6).
- ES-DE (`org.es_de.frontend`) as game launcher is **out of this resolver** — its per-system `<alternativeEmulator>` config lives on `/sdcard/ES-DE` and already rides `INTERNAL_DIRS`/`internal_ES-DE.tar`.

## 3. Detection — `game_launcher()` (`lib-root.sh`)

Resolution order (first hit wins):

1. **Override:** if the manifest has `@gamelauncher <pkg>` **and** `<pkg>` is installed, return it. (Explicit escape hatch; checked first so it always wins.)
2. **Probe:** scan `/data/data/*/` for the game-frontend signature — a `databases/GAME_INFO` file **or** a `files/datastore/GameLauncher.preferences_pb`. Return the first matching package. (Self-adapting to OEM rebrands that keep the ES-DE-fork data shape.)
3. **List:** fall back to a curated `GAME_LAUNCHERS` set in `lib-root.sh` (seed: `com.handheld.launcher`; room for other OEM frontends as they're confirmed). Return the first one installed.
4. Nothing matches → return empty. Callers **WARN and skip** (never fail).

`GAME_LAUNCHERS` is a space-separated list, same idiom as `INTERNAL_DIRS`. The probe is bounded to `/data/data/*` top-level (one `ls`/`test` per package dir; cheap, root-only).

> Note on order: the design presents this as "probe → list → override", but **override is evaluated first** so a manifest pin can never be beaten by a stray probe hit. Probe precedes list because the probe is signature-based (accurate for rebrands), the list is a static fallback.

## 4. Save — `capture.sh` + GUI

### 4.1 `capture.sh`
- Resolve `GL=$(game_launcher)`. If empty → WARN "no game launcher detected" and skip this block.
- If `GL` is selected for **config** (manifest `config` axis, or back-compat "capture everything"):
  - `mkdir -p "$P/gamelauncher"`; write `gamelauncher/meta` = `pkg=$GL`, `uid=$(app_uid "$GL")`.
  - `tar -cf "$P/gamelauncher/config.tar" -C /data/data/"$GL" \
      --exclude="files/datastore/*-shm" --exclude="files/datastore/*.tmp" \
      files/datastore shared_prefs 2>/dev/null` — only the portable subtrees, **GAME_INFO and caches never included**. Tar tolerates a missing `shared_prefs` (the AIR X has it empty); WARN + skip if neither subtree exists.
  - `ok "captured game launcher config: $GL"`.
- This block is **independent** of the `@homescreen` HOME-launcher capture (launcher3 favorites/layout) — different app, different artifact. Both may run.

### 4.2 `cas/gui.py` + `cas/profiles.py`
- The Save app-list "default launcher" row binds to the **detected game launcher** (`Profile.meta["game_launcher_pkg"]`, recorded at capture), in addition to the existing HOME `launcher_pkg` row. APK axis disabled (system firmware); **Config on** by default; Config gates the §4.1 capture.
- `@gamelauncher <pkg>` is written to the manifest **only** when the operator overrides detection (rare). `Profile.flags()`/`save_manifest` preserve it like other value-flags (`@home`).

## 5. Download — `restore.sh` (auto-detect target, write-back)

Under a new step (gated like `@homescreen`, additive):
- Resolve the **target unit's** launcher: `TGL=$(game_launcher)` (same helper — this is the "auto-detect on download").
- If `gamelauncher/config.tar` exists **and** `TGL` == captured `gamelauncher/meta pkg`:
  1. `am force-stop "$TGL"` (drop the in-memory DataStore copy so our file isn't overwritten).
  2. `mkdir -p /data/data/$TGL/files/datastore` if absent; `tar -xf gamelauncher/config.tar -C /data/data/$TGL` (overwrites `files/datastore/*.preferences_pb` + `shared_prefs`).
  3. `chown -R system:system /data/data/$TGL/files/datastore /data/data/$TGL/shared_prefs`; `restorecon -R` (or `chcon u:object_r:system_app_data_file:s0`) on the written paths.
  4. **Verify:** `*.preferences_pb` exists with the right SELinux label; one retry; WARN on failure.
- `TGL` empty, or ≠ captured pkg (cross-family), or no `config.tar` → **WARN + skip**; does **not** bump `FAIL`.
- DataStore writes a single `.preferences_pb` atomically (no WAL); no journal to fold — simpler than the `GAME_INFO`/sqlite write-back. Any `*-shm`/`*.tmp` are excluded at capture and cleared if stale.

## 6. Write-back policy — overwrite (decided)

Restore **overwrites** the whole `preferences_pb` rather than merging only `*_select_emulator` keys (merging would require protobuf editing in shell — out of scope). Consequence: the golden's `LastPlayGameCollectionKey` (last-played pointer) and any UI int prefs ride along. These are benign, and the **Lock scrub** (selective-capture spec §6.2) already clears `LastPlayGameCollectionKey` before sealing, so a shipped unit has no stale last-played. The emulator picks (`*_select_emulator`) are the payload that matters and they transfer intact.

## 7. Data flow

**Save:** `game_launcher()` (probe→list→override) → tar `files/datastore`+`shared_prefs` (no GAME_INFO) → `gamelauncher/config.tar` + `meta` in the golden.
**Download:** `game_launcher()` on the target → if same pkg, force-stop → extract → chown/relabel → verify → emulator picks live on the fresh unit, no manual setup.
**Lock:** existing scrub clears `LastPlayGameCollectionKey` (the only non-portable thing the overwrite introduced).

## 8. Error handling

- Detection returns empty → WARN, skip (both Save and Download).
- Capture: neither portable subtree present → WARN, skip (no empty/broken artifact).
- Download: target launcher absent / mismatched pkg / missing artifact → WARN, skip; **never FAIL** (additive, recoverable — re-run after the launcher is present).
- Write-back verify fails after one retry → WARN; the unit still functions (falls back to the GAME_INFO scan default).
- Back-compat: a profile with no `gamelauncher/` artifact → the new steps no-op silently.

## 9. Open items (resolve during implementation)

- **Probe path portability** `[VERIFY on device]`: confirm the `databases/GAME_INFO` / `files/datastore/GameLauncher.preferences_pb` signature paths across MANGMI builds (and that the probe doesn't false-positive on unrelated apps). Pin on the AIR X.
- **Rescan survival** `[VERIFY on device]`: confirm the restored DataStore picks persist after the launcher's first ROM rescan on a fresh unit. If a scan ever clobbers `preferences_pb`, add a **post-scan re-apply** (same pattern as the PS2-runner `GAME_INFO` fixup) — flagged, not built unless needed.
- **`restorecon` vs `chcon`** `[VERIFY on device]`: prefer `restorecon` (policy-correct) if present in the root toolset; else explicit `chcon u:object_r:system_app_data_file:s0`.

## 10. Testing

- Python suite (`tests/test_cas.py`, mocks adb): `game_launcher()` resolution order — override-wins, probe-hit, list-fallback, none→empty; `@gamelauncher` parse + `save_manifest` round-trip preserving it; `Profile.meta` exposes `game_launcher_pkg`.
- Off-device smoke (mirrors the `grant_special_appops` smoke): a fake `/data/data` tree → assert the right pkg is detected, `config.tar` contains `files/datastore` but **not** `databases/GAME_INFO`, and the restore path force-stops + chowns the expected target.
- Shell paths carry `[VERIFY on device]` markers; end-to-end on a real AIR X: set PSX→DuckStation on a golden, Save, factory-reset/fresh unit, Download, confirm PSX launches DuckStation with no manual step; then rescan and re-confirm (open item §9).
- All existing tests stay green.

## 11. Scope note

Two independently-testable pieces ship together: **(A)** `game_launcher()` + Save capture, **(B)** Download auto-detect + write-back. B depends on A's artifact format only. The implementation plan may phase them A→B, with detection (`game_launcher()` + tests) landing first since both phases use it.
