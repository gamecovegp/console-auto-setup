# Default-launcher propagation + Companion-grant robustness + non-destructive Update mode

- **Date:** 2026-06-27
- **Status:** Design — awaiting review
- **Area:** `cas/provision.py`, `cas/gui.py`, `provision/root/{restore.sh,capture.sh,lib-root.sh}`, `tests/test_cas.py`

## 1. Background

A Retroid Pocket 6 golden was used to set up MANGMI AIR X units. Three symptoms followed:

1. **Emulators (PPSSPP, Citra, …) not properly configured.** Cross-silicon (Adreno 740 → 610) GPU/renderer settings don't translate; PPSSPP's config lives behind a SAF tree URI on the SD (golden serial `9C33-6BBD`) and is lost on a different SD serial.
2. **"Files into one folder" didn't finish.** The homescreen favorites-DB clone is same-family only — `restore.sh` skips it when the unit's launcher ≠ the golden's (`com.android.launcher3`). The comment literally names Mangmi as a self-skip case.
3. **GameCove Companion file access not automatic.** The Companion declares `MANAGE_EXTERNAL_STORAGE`; `restore.sh` grants it via `appops`, but the grant did not stick on MANGMI's A14 build, so it was enabled by hand.

**Root cause:** CAS golden cloning is same-family by architecture (the Retroid `profile.meta` even scopes itself to `Odin2 Mini|Retroid Pocket 6`). GPU config, launcher layout, and file grants are inherently per-hardware. There is no automatic Retroid→MANGMI golden; a one-time MANGMI golden, captured on real MANGMI hardware, is required. After that, MANGMI→MANGMI clones cleanly (the proven same-family path).

**Operator intent (clarified):** Across Retroid, Odin, and MANGMI, the OEM launchers are unused — **ES-DE is the frontend**. The operator wants ES-DE set as the default launcher to propagate, and wants to push such changes to **already-provisioned** units without re-wiping them.

This spec covers the chosen scope for this round: **(1) default game launcher (HOME role)** and **(2) Companion-grant robustness**, delivered via **(3) a new non-destructive Update mode**. Out of scope this round: widening the device-settings allowlist, cross-family GPU remediation, "loud skip reporting".

## 2. Goals / Non-goals

**Goals**
- Set the device's **default HOME launcher** to a configurable frontend (manifest flag `@home`, **default ES-DE** this round; switchable to e.g. Cocoon later) so it propagates across all families without reading the OEM launcher.
- Make the **All-Files-Access** grant for declaring apps (e.g. the Companion) reliably stick, or fail loudly.
- Add an **Update** action that re-applies settings + default launcher + grants to existing units **without** wiping per-app data (saves/states).

**Non-goals**
- No cross-family magic (a MANGMI golden is still required for MANGMI units).
- No change to which emulators/data are captured.
- No SAF-urigrants merge logic (Update skips that overwrite — see §6).

## 3. Key technical finding — ES-DE is HOME-capable

ES-DE's `AndroidManifest.xml` declares `android.intent.category.HOME` on a dedicated activity `org.es_de.frontend/org.es_de.frontend.MainActivityHomeApp` (also `CATEGORY_LAUNCHER` and `LEANBACK_LAUNCHER`). Therefore `cmd package set-home-activity org.es_de.frontend/org.es_de.frontend.MainActivityHomeApp` can make ES-DE the Android home, replacing the OEM launcher. Because this is launcher-agnostic, it sidesteps the cross-family favorites-DB problem (symptom #2).

**Determining the home launcher — a configurable frontend, defaulting to ES-DE; never the OEM launcher.** MANGMI, Retroid, and Odin each ship a *different* OEM launcher, so capturing "whatever is HOME on the golden" is fragile and family-specific. Instead the home app is **chosen by a new manifest flag `@home <pkg>`** and is family-agnostic. Resolution order:

1. `@home <pkg>` value from the manifest, if present;
2. else `org.es_de.frontend` if it is in the manifest (the **default** this round);
3. else the captured `launcher_component` / `launcher_pkg` from the golden (no-frontend fallback).

The resolved target must be HOME-capable (declare `CATEGORY_HOME`) and present in the manifest/installed on the unit. ES-DE qualifies (`MainActivityHomeApp`). A different frontend — e.g. **Cocoon** — becomes the default later simply by setting `@home <cocoon-pkg>` in the profile manifest (no code change), once it is confirmed HOME-capable. This is consistent with how CAS already special-cases `org.es_de.frontend` (box art, MediaDirectory). The current Retroid golden's `launcher_pkg=com.android.launcher3` is therefore irrelevant: with ES-DE in the manifest and no `@home` override, restore sets ES-DE as home. No "set the home on the golden before capture" step is required.

A GUI picker for `@home` is **out of scope this round** (defaults to ES-DE; switchable by editing the manifest) — added when a second frontend (Cocoon) is actually onboarded.

## 4. Component design

### 4.1 `provision/root/lib-root.sh` (shared helpers)
- `home_launcher_component()` — returns the full `pkg/cls` of the resolved HOME activity (existing `home_launcher()` returns only the package). Falls back to empty if unresolved. Used only to *record* the golden's HOME for the no-ES-DE fallback.
- `set_home_launcher(pkg, component)`:
  1. `cmd role add-role-holder --user 0 android.app.role.HOME "<pkg>"` (preferred — package-only; Android selects the package's HOME activity, e.g. ES-DE's `MainActivityHomeApp`). Works for the ES-DE policy case with no component needed.
  2. fallback `cmd package set-home-activity "<component>"` when a component is supplied (the no-ES-DE fallback path).
  3. verify via `home_launcher` == `pkg`; return non-zero if neither stuck.
- `grant_all_files(pkg)` — robust MANAGE_EXTERNAL_STORAGE:
  - `appops set "$pkg" MANAGE_EXTERNAL_STORAGE allow`, then `appops set --user 0 "$pkg" …`, then by uid `appops set --uid "$(app_uid pkg)" …` (forms vary across OEM A14 builds).
  - verify `appops get "$pkg" MANAGE_EXTERNAL_STORAGE | grep -q allow`; one retry; return status.

### 4.2 `provision/root/capture.sh`
- In the homescreen capture block, additionally write `launcher_component=$(home_launcher_component)` into `homescreen/meta` (alongside the existing `launcher_pkg` / `launcher_uid`). No other capture change.

### 4.3 `provision/root/restore.sh`
- **New env `CAS_MODE`**: `full` (default) | `update`.
- **Extract the All-Files grant** out of the destructive per-app loop into its own pass over `RPKGS` (using `grant_all_files`), so it runs in both modes. Grant failure increments `FAIL` (unchanged contract).
- **New set-default-HOME step** (under `@homescreen`), independent of the favorites-DB clone, following the §3 resolution order:
  - resolve target pkg: `manifest_flag @home` → else `org.es_de.frontend` if ∈ `RPKGS` → else captured `launcher_pkg`/`launcher_component`;
  - if the resolved pkg is installed here, call `set_home_launcher pkg [component]` (component only used for the captured-fallback path; the `@home`/ES-DE path is package-only via role-holder);
  - resolved pkg not in manifest / not installed / not HOME-capable → **WARN** and skip;
  - set failure → **WARN** (additive/recoverable, consistent with homescreen's existing additive treatment); does **not** bump `FAIL`.
- **Favorites-DB clone guard:** skip the favorites-DB layout restore when `launcher_pkg` ∈ `RPKGS` (its data already arrives via per-app restore — avoids a redundant destructive `rm -rf`/extract). Otherwise unchanged (same-family only).
- **`CAS_MODE=update` gating** — skip the destructive/serial phases, run only the idempotent ones (see §6).

### 4.4 `cas/provision.py`
- `provision(adb, profile, log, dry_push=False, es_media_src=None, mode="full")`; thread `mode` through `provision_all`.
- In `update` mode:
  - keep the root + not-golden guards; **drop the SD-required guard** (launcher/grants/settings need no SD);
  - push a **reduced payload**: `manifest`, `pkglist.txt`, `global.meta`, `settings/`, `homescreen/meta` (the small meta file only — not `launcher_data.tar`/wallpaper), plus `restore.sh` + `lib-root.sh`. No APKs, no `data.tar`/`adata.tar`/`obb.tar`, no `internal_*.tar`, no `urigrants.xml`. Fast.
  - run `CAS_MODE=update CAS_MANIFEST=… CAS_PAYLOAD=… sh restore.sh`.
  - the Companion APK refresh (`install_companion`) still runs after a successful update if the Companion is in the manifest.

### 4.5 `cas/gui.py`
- Add an **"Update"** button to the action footer between Download and Lock (workflow: Root → Save → Download → **Update** → Lock). Honors the existing "Apply to ALL connected / selected rows" toggle and runs in parallel like Download. Tooltip: *"Re-apply settings, default launcher, and file-access grants to already-set-up units — does NOT wipe saves/state."* Calls `provision(mode="update")` per target.
- **Preserve `@home` on save:** `save_manifest()` currently writes only the four checkbox flags (`settings/hardening/grants/homescreen`), which would drop a value-flag like `@home`. Fix: start from the manifest's existing flags (`prof.flags()`) and overlay the four checkbox values, so `@home` (and any future non-checkbox flag) survives a "Save selection". No `@home` GUI control this round (defaults to ES-DE).

## 5. Data flow (Update)

GUI **Update** → for each selected/connected unit `provision(mode="update")` → push reduced payload → `CAS_MODE=update sh restore.sh` → set default HOME (ES-DE) + All-Files grants + `@settings` + `@hardening` → reboot → converged; per-app saves/state untouched.

## 6. Update mode — exact run / skip

**Runs (idempotent, non-destructive):**
- Set default HOME launcher (from `homescreen/meta`), gated by `@homescreen`.
- All-Files-Access grants (`grant_all_files`) for declaring apps in `RPKGS`.
- `@settings` device-experience allowlist.
- `@hardening` (battery-optimization exemption + OTA disable).

In Update mode the grant pass only attempts apps actually installed on the unit (`pm path`); a manifest app absent on the unit is **logged, not failed** (Update never installs apps). In full restore the grant pass runs after install, so absence there still counts as a failure as today.

**Skips:**
- App (re)install and per-app `data.tar`/`adata.tar`/`obb.tar` restore (the `rm -rf /data/data/<pkg>` phases).
- `internal_*.tar` shared-storage overwrite (Citra/RetroArch/ES-DE saves live here).
- Favorites-DB launcher-layout clone (`rm -rf /data/data/<launcher>`).
- SAF `urigrants.xml` overwrite — **skipped to protect SAF grants a used unit added post-provision**; the Companion grant is `appops`, not urigrants, so it is unaffected. (Changing a unit's SD serial still requires full Download.)
- OOBE/provisioned flags, locale/tz, RetroArch cores top-up.

## 7. Error handling / failure contract

`restore.sh` keeps the fail-closed contract (any counted failure → exit non-zero → PC treats the unit as not provisioned). This round:
- **All-Files grant failure = FAIL** (existing behavior; surfaces a unit that would need a manual toggle).
- **Default-launcher set failure = WARN** (additive/recoverable; a unit whose home didn't switch still boots and is data-clean).

## 8. Testing

Python suite (`tests/test_cas.py`, mocks adb):
- capture writes `launcher_component` into `homescreen/meta`.
- `manifest_flags` parses `@home <pkg>`; `save_manifest` via the GUI path **preserves** an existing `@home` when only the four checkbox flags are edited.
- `provision(mode="update")` pushes the reduced payload set (asserts APKs/data/internal tars are **not** pushed) and invokes `restore.sh` with `CAS_MODE=update`; SD guard not enforced in update mode.
- `provision(mode="full")` still pushes the full payload and runs `CAS_MODE=full`/default.
- All 106 existing tests stay green.

Shell-level logic in `restore.sh`/`lib-root.sh` (set-home, grant robustness) carries `[VERIFY on device]` markers — end-to-end validation requires one real MANGMI AIR X unit (set-home stickiness and `appops` form vary by OEM A14 build).

## 9. Operational notes

- **Golden prep:** not required for the launcher — restore pins ES-DE as home by policy (§3) whenever ES-DE is in the manifest, regardless of what the golden's HOME was. (Setting ES-DE as home on the golden anyway is still nice so the golden itself boots to ES-DE for verification.)
- **First MANGMI golden is still manual once:** seed from the Retroid golden if useful, hand-finish emulator GPU settings on one MANGMI, set ES-DE as home, capture → that becomes the MANGMI golden. Subsequent units use Download (fresh) or Update (existing).
