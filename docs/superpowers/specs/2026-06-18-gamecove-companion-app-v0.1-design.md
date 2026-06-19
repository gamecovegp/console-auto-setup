# GameCove Companion App — v0.1 Design

**Date:** 2026-06-18
**Status:** Approved design (brainstorm) → next: implementation plan
**Owner:** Donald (CTO)

## 1. Summary

A customer-facing Android app (Flutter) pre-installed on shipped GameCove handhelds
(Odin2 / Odin2 Mini first; Mangmi later). It:

1. Keeps **itself**, its **content**, and the device's **emulators** current via curated
   over-the-air updates that GameCove controls centrally; and
2. Onboards the buyer with offline guides and routes them to support / warranty / accessories.

It is a **separate artifact** from `cas` (the PC-side provisioning toolkit). `cas` stays the
provisioner; this app is the post-sale companion. It reuses `cas` data (the emulator registry
`lib/emulators.txt`, the golden layout) but is its own codebase.

## 2. Goals (v0.1)

- Self-updating APK (no Play Store).
- Curated over-the-air **content** (guides, promos, QR targets).
- Curated over-the-air **emulator** updates, fleet-controlled by GameCove.
- Must-have guides + QRs: Start Here, Controls/Hotkeys, Add Games (legally), PortMaster/Ports,
  Support / Warranty / Accessories.
- Fully offline-capable; controller / D-pad navigable.
- Survives the retail seal (no root required at runtime).

## 3. Non-goals (explicitly out of v0.1)

- **Tier-3 on-device tools** (Device Check, Backup Saves, Storage Check) — later versions; need
  Shizuku/root for app-data access.
- **On-device self-provisioning** ("the APK is the setup") — rejected: needs root that ships
  stripped, can't self-root or self-seal (fastboot), and is obsoleted by factory preload.
- **"Re-link my games folder" SAF wizard** (no-root, retail-safe) — parked for a later version.
- **Pushing RetroArch cores / app-data assets** — blocked on sealed units (app-data wall); stays
  with RetroArch's own Online Updater / provisioning.
- **Personalization** (GameCove theme, config-pack installer) — later tier.

## 4. Architecture

One Flutter APK, three layers:

- **UI (Dart):** Home → Guide / QR / Updates screens. Guides render Markdown (`flutter_markdown`).
  Controller / D-pad focus traversal (A = select, B = back) is a first-class requirement, not a
  retrofit.
- **Services (Dart):**
  - `ContentService` — bundled seed content + feed overlay + local cache; renders from cache
    (offline-first); atomic swap only on a complete, valid download.
  - `UpdateService` — drives all three update channels: version compare, download, sha256 verify,
    hand-off to the native installer.
- **Native bridge (Kotlin via MethodChannel):** v0.1 needs `installApk(path)`, `deviceInfo()`,
  `installedPackages(...)`. Reserve a `tools/*` namespace (stubbed) so tier-3 tools bolt on later
  over Shizuku/root without reworking the shell.

## 5. Update subsystem — three channels, one machine

All three follow the same pattern: read a manifest → compare → download → **sha256 verify** →
install/apply. Hosted on GitHub (static, free, git-versioned).

### 5.1 App channel (self-update)

- `app/latest.json`: `{versionCode, versionName, url, sha256, minSupported, notes}`.
- Trigger: launch (throttled ~daily) + manual "Check for updates".
- If `latest.versionCode > installed` → "Update available" card → download → sha256 → native install.
- Safety: downgrade protection, hash-mismatch reject.

### 5.2 Content channel (guides / promos / QR)

- `content/index.json`: content version + list `{id, title, icon, file, hash, order}`, plus
  `qr.json` and `promos.json`.
- Bundled seed in the APK; the feed overlays it; the cache is the render source (offline-first).
- Atomic swap only on a complete, valid fetch; failure = keep last-good.
- QR targets and promo content update here → no APK rebuild to change a landing page.

### 5.3 Emulator channel (curated fleet updates) — GameCove-controlled

- `emulators/manifest.json`: per emulator
  `{id, package, blessedVersionCode, versionName, url, sha256, signerSha256, minAppVersion, mandatory, notes}`.
  APKs hosted as GitHub release assets.
- **Control model:** the manifest is the single control point; the app is a dumb enforcement
  client. Publishing a manifest entry **is** the act of blessing that version (only after it passes
  on the golden). Fleet-wide levers by editing one file: roll-forward / hold / pin / force
  (`mandatory`).
- **Policy:** updates surface as *recommended* by default (user taps to apply); the `mandatory`
  flag forces a critical update; silent application requires device-owner (§5.4). Run Backup Saves
  (tier-3) before a mass bump, once that tool exists.
- App reads each installed emulator's versionCode via `PackageManager` (requires `<queries>`
  entries for the emulator packages on Android 13), compares to blessed, and updates.
- **Signing continuity:** Android only updates an app along the same signer lineage; the app
  verifies `signerSha256` and refuses a mismatched APK (a forced reinstall would wipe that
  emulator's saves). One source/signer per emulator.
- **Forward-only:** Android blocks downgrades; "rollback" = publish a *higher*-versioned corrected
  build, never a literal downgrade.
- Covers ES-DE (frontend) by the same mechanism.
- **Cores excluded** (app-data wall) → RetroArch's own updater / provisioning.
- **Licensing:** re-host only license-compatible open-source emulators (RetroArch, Dolphin,
  DuckStation, PPSSPP, melonDS — per their GPL/MIT terms). Exclude AetherSX2 and Switch-emulator
  forks (redistribution murky); keeps GameCove clean, same ethos as the "add games legally" guide.

### 5.4 Install UX

- **v0.1: prompted** — one system install dialog; works on a sealed unit with
  `REQUEST_INSTALL_PACKAGES` pre-granted at provisioning. Simple, robust.
- **Fast-follow: device-owner silent** — `cas` sets the app as device-owner at provisioning →
  silent installs, no dialogs. Strongly preferred once emulator updates go live (N packages =
  N dialogs otherwise).

## 6. Screens / Information architecture

- **Home:** GameCove-branded; tiles for the 4 guides + 3 QRs; promo banner up top; an Updates entry
  that badges when an update is available. Controller-navigable.
- **Guide:** one Markdown screen, fed by id.
- **QR:** title + blurb + QR (`qr_flutter`) + "Open link" button, fed by id.
- **Updates:** app version; "Check for updates"; available app/emulator updates (version + notes →
  install); content version + last-refreshed.
- **Per-device content:** the controls (and parts of Start Here) variant is chosen from
  `deviceInfo()` model; unknown model → generic fallback. Odin content first; Mangmi variant later.

## 7. Error handling / edge cases

- Offline → render from cache/seed; update checks fail silently ("using saved content").
- Corrupt/partial content fetch → keep last-good (atomic swap only when complete + valid).
- Corrupt APK (any channel) → sha256 reject; never install.
- Signer mismatch (emulator) → refuse; surface it, never reinstall.
- Install declined/failed → stay on current version; retry next check.
- `minSupported` / `minAppVersion` → prompt an app update first; degrade gracefully.
- Updates only ever swap apps — never touch ROMs / saves / SD.

## 8. Delivery & testing

- **Provisioning:** `cas` installs the APK in the payload and pre-grants install permission (and
  sets device-owner for the silent fast-follow) — all pre-seal; the app needs no runtime root, so
  it survives sealing.
- **CI:** GitHub Actions (mirroring the existing `build.yml`) builds the APK, attaches it to a
  Release, and bumps `app/latest.json`.
- **Tests:** Dart unit tests for version-compare, sha256 verify, downgrade/signer guards, and the
  atomic content swap; a native installer integration test on-device; a manual pass for offline
  render, content refresh, prompted install end-to-end, and controller navigation.

## 9. Hosting

GitHub: Releases for APK assets (app + emulators); a content repo served via CDN (GitHub Pages /
jsDelivr) for `content/`, `qr.json`, `promos.json`, and the manifests. Static, free, git-versioned,
no server to run. Movable behind a GameCove domain later. (Alternative considered: Firebase Hosting
+ Remote Config — revisit if bundled analytics / crash reporting becomes wanted.)

## 10. Open items (content / ops — do not block implementation)

- Support / Warranty / Accessories URLs.
- Guide copy (the 4 guides) + the Mangmi controls variant.
- Chosen upstream source (and signer) per blessed emulator.

## 11. Success criteria

- A fresh, never-online unit: all guides + QRs usable; fully controller-navigable.
- Editing the content repo refreshes a unit's guides / promos / QRs over Wi-Fi with no APK change.
- Publishing `app/latest.json` updates the app on-device (prompted install).
- Publishing an `emulators/manifest.json` entry updates that emulator on-device, with sha256 +
  signer verified; nothing moves unless GameCove publishes it.
