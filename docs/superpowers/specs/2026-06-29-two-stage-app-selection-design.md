# Two-stage app/config selection (independent Save and Download picks)

- **Date:** 2026-06-29
- **Status:** Design — awaiting review
- **Area:** `cas/gui.py`, `cas/profiles.py`, `cas/provision.py`, `provision/root/restore.sh`, `tests/`. (`provision/root/capture.sh` is already axis-aware — no change.)
- **Builds on:** `2026-06-29-selective-capture-and-lock-scrub-design.md` (per-app two-axis manifest), `2026-06-29-game-launcher-config-capture-design.md` (`@gamelauncher`), the 2026-06-27 `@homescreen` launcher capture.

## 1. Background

CAS already has a per-app two-axis selection (APK / Config) on the "Apps & options" tab, persisted to a per-profile `manifest`. But the selection is only half-wired and it's a single shared list:

- **Save ignores it.** The Save path `capture_to_pc()` runs `capture.sh` **without** `CAS_MANIFEST` (`cas/provision.py:546`), so capture grabs **every** third-party app with full APK+config regardless of the checkboxes. The operator cannot choose what goes into the golden.
- **Download honors inclusion only.** `provision()` passes `CAS_MANIFEST=manifest` (`cas/provision.py:360`), so excluded apps aren't deployed — but `restore.sh` deploys whatever the golden captured for an *included* app; it does not honor the per-app **APK vs Config** axis at deploy time.
- **The list is golden-derived, not device-derived.** It comes from the captured `pkglist` (post-capture) + the launcher — so on Save you can't pick from the device's installed apps *before* capturing, and system-app frontends (the game launcher) never appear because `pm list -3` is third-party-only.

**Operator intent (clarified):** two **independent** selections — what to **capture into the golden** (chosen from the connected device's apps) and what to **deploy onto a unit** (chosen from the golden's apps) — each honoring APK and Config independently, with sensible defaults and select/deselect-all.

## 2. Goals / Non-goals

**Goals**
- Two independent per-profile selections: a **capture** selection (Save) and a **deploy** selection (Download).
- **Save honors its selection:** `capture_to_pc()` passes the capture selection to `capture.sh`; only ticked apps are captured, per axis.
- **Save list is device-driven:** populated from a live scan of the connected device's apps, **plus** the auto-detected game launcher and HOME launcher (system apps `pm list -3` misses).
- **Download honors its selection per-axis:** `restore.sh` installs an app's APK only if its APK axis is on, and restores its config/data only if its Config axis is on.
- **Defaults:** Save auto-checks detected emulators + the game launcher; Download auto-checks everything in the golden.
- **Select all / Deselect all** on each list.

**Non-goals**
- No change to `capture.sh`'s axis logic (already honors `CAS_MANIFEST` axes) or to the `@gamelauncher`/`@homescreen` capture mechanisms.
- No new dependency; no change to how a resolved path is read/written.
- No reconciliation logic between the two selections — they are independent by design.

## 3. Data model — two per-profile manifests

- **`capture-manifest`** (NEW): the **Save** selection — tokened app lines (`pkg` / `pkg apk` / `pkg config`) for what to capture, plus `@gamelauncher`/`@homescreen` flags for the system-app frontends, plus the existing `@settings/@hardening/@grants` behavior flags relevant to capture.
- **`manifest`** (EXISTING — unchanged name): the **deploy** selection — same format. `provision()` already reads it; keeping the name means no change there.

Both reuse the existing parsers (`manifest_pkgs`, `manifest_axes`, `manifest_flags`, `save_manifest`). `Profile` gains `capture_axes()`/`capture_pkgs()` reading `capture-manifest`, mirroring the existing `axes()`/`pkgs()` on `manifest`.

## 4. Save flow (capture)

### 4.1 GUI — "Save selection (from device)"
- On profile load with a device connected, **live-scan** the device's third-party apps (`pm list packages -3`) via the existing `Adb`.
- Build the list as `pm -3 apps` ∪ {**game launcher** (`game_launcher()` on the device), **HOME launcher** (`home_launcher()`)}. Normal apps get two-axis (`APK`|`Config`) rows; the **launcher rows are config-only** (APK disabled — system firmware).
- **Default checks:** detected **emulators** (a new `EMULATOR_PKGS` set, see §6) checked both-axes; the **game launcher** checked (config); everything else (incl. HOME launcher) unchecked.
- **Select all / Deselect all** buttons.
- **No device connected:** fall back to the golden's `pkglist` (or `EMULATOR_PKGS`) with a "connect a device to pick from its installed apps" note.
- Persisting writes `capture-manifest`: normal ticked apps as tokened lines; the **game-launcher row → `@gamelauncher` flag** (on/off), the **HOME-launcher row → `@homescreen` flag** — NOT package lines (the launcher is deliberately excluded from the per-app capture loop; this matches the built `@gamelauncher` design).

### 4.2 `capture_to_pc()` (the gap fix)
- Push `capture-manifest` to the device and run `CAS_MANIFEST=<pushed capture-manifest> CAS_OUT=… sh capture.sh`. `capture.sh` already: builds `pkglist.txt` from the manifest (minus the launchers), captures each app per its axes, and gates `@gamelauncher`/`@homescreen` capture on those flags. So only the selected set is saved.
- After a successful capture, seed the **deploy** `manifest` from the captured `pkglist` if it has no app lines (existing `seed_default_manifest`), so Download has a sensible default.

## 5. Download flow (deploy)

### 5.1 GUI — "Download selection (from golden)"
- List = the golden's captured apps (`Profile.all_pkgs()` — pkglist + captured launchers), two-axis rows, **default all-on**. Select/Deselect all. Persists `manifest` (unchanged target).

### 5.2 `restore.sh` — honor per-app axes
- Today: install APK if present in payload; restore data if present. **New:** read `manifest_axes` per app and:
  - **APK axis off** → skip installing the app (even if its APK is in the payload).
  - **Config axis off** → skip restoring its `data.tar`/`adata.tar`/internal dirs (even if present).
  - Both on / bare line → today's behavior. Config-only-with-app-installed-elsewhere stays as today (the existing `axes=config` handling).
- `provision()` already passes `manifest`; no change there.

## 6. Emulator detection (Save defaults)

A Python `EMULATOR_PKGS` constant (in `cas/profiles.py` or `cas/config.py`) mirroring `provision/root/lib-root.sh`'s `PKGS` set (RetroArch, DuckStation, PPSSPP, Dolphin, Flycast, AetherSX2, melonDS, Citra, Mupen64, Eden, ES-DE, GameHub). Used only for the Save-list default-check. The game launcher is auto-checked separately (it's the frontend, not in `PKGS`). Keep the Python set and `lib-root.sh PKGS` in sync (one comment cross-references the other).

## 7. GUI structure

The "Apps & options" tab presents the two lists clearly separated — **"Save → golden (from this device)"** and **"Download → unit (from the golden)"** — each a scrollable two-axis app list with its own **Select all / Deselect all**. The existing single "Select all apps" toggle is replaced by the per-list buttons. The two "Save selection" actions write `capture-manifest` and `manifest` respectively.

## 8. Error handling

- No device at Save-config time → device list unavailable → fall back to golden pkglist / `EMULATOR_PKGS` + a note; never crash.
- `capture-manifest` absent (older profile) → `capture_to_pc` falls back to today's capture-all (`user_pkgs`), so behavior is unchanged for profiles that predate this feature (back-compat).
- `restore.sh` axis enforcement is additive to existing guards; a missing axis defaults to "on" (bare line) — back-compat with existing manifests.
- Empty capture selection → capture nothing meaningful; warn (mirrors restore's "manifest selects no apps").

## 9. Testing

- `capture_to_pc` passes `CAS_MANIFEST=<capture-manifest>` and the file reaches the device (FakeAdb asserts the command + push).
- `Profile.capture_axes()`/`capture_pkgs()` parse `capture-manifest`.
- Save-list default selection: emulators + game launcher checked, others off (given a fake device app list + `EMULATOR_PKGS`).
- `restore.sh` per-app axis enforcement (shell smoke test, `DATA_ROOT`-style): APK-axis-off skips install; Config-axis-off skips data restore; bare line = both (back-compat).
- Select/Deselect all toggles every row in the targeted list.
- Back-compat: a profile with no `capture-manifest` captures-all (unchanged); a bare-line `manifest` deploys both axes (unchanged).
- All existing tests stay green.

## 10. Scope / phasing

Cohesive but sizable; the plan may phase it:
- **Phase A — Save side:** `capture-manifest` model, `capture_to_pc` passes it, GUI Save list (device scan + launchers + emulator defaults + select/deselect-all).
- **Phase B — Download side:** `restore.sh` per-app axis enforcement, GUI Download list (golden apps, all-on, select/deselect-all).
Phase A delivers "choose what to save" (the primary gap); Phase B delivers "choose what to deploy, per axis." Each is independently testable.
