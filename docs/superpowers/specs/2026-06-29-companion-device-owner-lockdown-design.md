# GameCove Companion — Device-Owner lockdown (non-uninstallable + reset-blocked)

- **Date:** 2026-06-29
- **Status:** Design — awaiting review
- **Area:** `gamecove-companion` (Flutter app + native Kotlin: `DeviceAdminReceiver`, policy applier, release receiver) · `console-auto-setup` (`cas/provision.py`, `cas/gui.py`, a new release command, `tests/test_cas.py`)

## 1. Background

The operator wants the GameCove Companion app to be **(a) impossible for an end user to uninstall** and **(b) survive a factory reset**. These are two different durability tiers:

- **(a) Non-uninstallable** is cheap and safe: a software capability via Android's **Device Owner** (Device Policy Manager). No firmware work, no brick risk.
- **(b) Survive a true factory reset** is expensive: `/data` — where all user apps, the Device Owner assignment, and any Magisk modules live — is always wiped by a factory/recovery reset. Only content baked into a **read-only partition** (system/product) survives, which requires an EDL/`rawprogram` flash of an OEM flat-build **per device tier** (per-SoC image engineering + brick risk).

**Decision (this round):** ship the **software lockdown now**, defer true-wipe-survival to a separate firmware milestone. Device Owner makes Companion non-removable and blocks every factory reset a normal end user can reach (Settings + programmatic). A deliberate **recovery/EDL wipe** (operator-only, in the shop) still works and is the sanctioned un-provision/escape path.

This also directly unlocks the **silent "Update all"** install path the Companion v0.1 spec (`2026-06-18-gamecove-companion-app-v0.1-design.md` §5.4) already wanted from Device Owner — that feature is enabled-by but not built-in this round.

## 2. Goals / Non-goals

**Goals**
- Companion (`com.gamecove.gamecove_companion`) is **non-uninstallable** by the end user.
- **Factory reset is blocked** from the OS (Settings UI + programmatic), along with the safe-boot bypass.
- CAS sets this up **per unit during provisioning**, with a fail-loud contract (never seal a half-locked unit).
- A **shop-only `cas release`** path cleanly un-provisions a unit (RMA/repair/resale) without a recovery wipe.

**Non-goals (this round)**
- **True factory-reset survival** (firmware bake / EDL flash per tier). Deferred milestone.
- **Kiosk extras** — `DISALLOW_ADD_USER`, lock-task launcher pinning, app-install blocking. Future toggles.
- **Magisk system-app placement** of the APK (defense-in-depth). Revisit with the firmware milestone.
- **Silent "Update all"** — enabled by this work, but its own feature.
- **Blocking ADB / `DISALLOW_DEBUGGING_FEATURES`** — intentionally *not* set; the release path rides ADB.

## 3. Why Device Owner (not root / not Magisk)

- "Disable factory reset" is **not** a root/Magisk capability — it is a **Device Policy** capability. A Device Owner can set `DISALLOW_FACTORY_RESET`; root cannot stop a recovery-mode wipe.
- A Device Owner app is **non-removable while it holds the role** — that is the non-uninstall mechanism, for free.
- Android does **not** let an external caller strip a Device Owner; only the DO app can clear *itself* (`clearDeviceOwnerApp()`). This shapes the release flow (§6): adb nudges the app, the app clears itself.
- `dpm set-device-owner` requires a **fresh device** (no accounts, no secondary users, no other device admins). The GameCove handhelds are AOSP/no-GMS and are provisioned fresh, so this precondition holds naturally.

## 4. Companion-app components (`gamecove-companion`, Flutter + Kotlin)

1. **`GcDeviceAdminReceiver`** — a Kotlin `DeviceAdminReceiver`, declared in `AndroidManifest.xml` with `android.permission.BIND_DEVICE_ADMIN` and a `res/xml/device_admin.xml` policy resource. This is the admin component DPM binds to (`com.gamecove.gamecove_companion/.GcDeviceAdminReceiver`).
2. **Policy applier** — when the app detects it is Device Owner (`DevicePolicyManager.isDeviceOwnerApp(pkg)`), it applies and re-asserts the restriction set:
   - `DISALLOW_FACTORY_RESET` — core ask (blocks Settings + programmatic reset).
   - `DISALLOW_SAFE_BOOT` — mandatory companion; without it, a safe-mode boot disables Device Admin and bypasses the lock.
   - `setUninstallBlocked(admin, pkg, true)` — belt-and-suspenders on top of DO's inherent non-removability (covers update-uninstall edge cases).
   - Invoked from `onEnabled()`, and re-asserted (idempotently) on app launch and `BOOT_COMPLETED`, so a partial state self-heals.
3. **`ReleaseReceiver`** — a guarded `BroadcastReceiver` for action `com.gamecove.companion.action.RELEASE` carrying a secret `token` extra. On a valid token: clear the restrictions, `setUninstallBlocked(false)`, `clearDeviceOwnerApp()`, emit a result log. Invalid/absent token → ignore + log. Realistically only `adb` (physical + USB-debug) can deliver it.
4. **Status surface** — a small method channel so the Flutter UI can show "Managed: yes/no" for operator verification.

## 5. CAS provisioning hook (`console-auto-setup`)

The lockdown rides the existing **② Download** action (`provision()` in `cas/provision.py`), **right after the Companion APK is installed** (`install_companion`) and before the reboot — exactly where the operator already "downloads the app to a new device." It is **not** a separate button. Gated on `COMPANION_PKG in pkgs` and a manifest flag `@lockdown` that **defaults on** when the Companion is present (`@lockdown off` opts a profile out, e.g. dev/test units).

1. **Preconditions** (surface the real reason): Companion package present; device fresh (no accounts/secondary users). Re-provisioning a unit that is *already* Companion-owned is treated as success (idempotent re-assert).
2. **Set Device Owner:** `adb shell dpm set-device-owner com.gamecove.gamecove_companion/.GcDeviceAdminReceiver`.
3. **Verify** (don't trust exit code alone): `dpm list-owners` shows Companion **and** `dumpsys device_policy` shows the restriction keys (`no_factory_reset`, `no_safe_boot`) active. (The app applies them on `onEnabled`; CAS nudges it with one `am start` then reads back.)
4. **Failure contract (Download path):** lockdown is **best-effort within Download** — a failure is a **LOUD warning** (consistent with how `install_companion` already treats a failed app install), *not* a hard provision abort; the apps/config still ship. The operator sees the unit went out **un-locked** and can fix it (ensure fresh) + re-Download. (A hard "must be locked to ship" gate could later be added at ③ Lock — out of scope this round.)

**Interaction with the golden clone:** Device Owner state lives in `/data/system/…`, **not** in the per-app `/data/data` trees that `capture.sh`/`restore.sh` handle — so it is *naturally excluded* from the golden payload, which is correct. Device Owner is set **fresh per unit via `dpm`**, never cloned (cloning DO assignment across units is unsupported and fragile).

**Ordering:** root/flash → `restore.sh` (installs Companion + clones config) → `install_companion` (PC-build refresh) → **set-device-owner + apply policy + verify** → reboot. (Independent of ③ Lock/seal.)

**App permission grants (separate from DO, in the `restore.sh` rights layer):** the Companion's special-access appops — **"All files access"** (`MANAGE_EXTERNAL_STORAGE`) and **"Install unknown apps"** (`REQUEST_INSTALL_PACKAGES`, so it can self-install emulators / app updates without the unknown-sources prompt) — are granted **declaration-driven** by `restore.sh` (`grant_special_appops`, `$SPECIAL_APPOPS` in `lib-root.sh`), verified + `FAIL`-counted, alongside the runtime perms `pm install -g` already grants. These ride the golden restore for **every** unit carrying the Companion (independent of `@lockdown`), not the Device-Owner step.

## 6. Release / un-provision flow (shop-only)

A **single-device GUI action** (a menu item — "Release selected unit…", behind a confirm — since this is an exceptional RMA action, not part of the routine ①②③ flow):

1. Operator connects the unit (USB; debugging is still enabled — we never blocked it).
2. CAS sends the guarded broadcast:
   `adb shell am broadcast -a com.gamecove.companion.action.RELEASE -e token <secret> -n com.gamecove.gamecove_companion/.GcReleaseReceiver`
3. Companion validates the token → drops restrictions, `setUninstallBlocked(false)`, `clearDeviceOwnerApp()` → emits a result line.
4. CAS confirms `dpm list-owners` is now empty. Only then is the unit "released" (factory reset / uninstall permitted again).
5. **Idempotent:** if DO is already cleared, it is a no-op success.

**The token** is a secret baked into the **release build** of Companion and mirrored in CAS config (sourced like other CAS secrets, not committed in plaintext). It exists to stop a *rogue app* from triggering release; physical + USB access is the real gate. If the token is ever lost/mismatched, the **recovery/EDL wipe is the hard fallback** — the operator is never truly locked out.

## 7. Failure handling

- `set-device-owner` rejected (accounts present / not fresh) → surface exact `dpm` stderr, **LOUD warning**, Download still succeeds un-locked (best-effort, like `install_companion`). Operator fixes + re-Downloads. (Already-Companion-owned is success, not a failure.)
- Policy verify mismatch (a restriction didn't stick) → same LOUD warning; report the unit as un-locked rather than aborting the whole Download.
- Release token mismatch → app ignores + logs; CAS times out and tells the operator to retry or fall back to EDL.
- Boot-time re-assert is idempotent, so a partially-applied state self-heals on next launch.

## 8. Testing

- **App (unit):** policy applier against a mocked `DevicePolicyManager` (asserts the three restrictions); release-receiver token guard (valid → clears; invalid/absent → no-op).
- **App (on-device smoke):** set DO via adb → verify `dpm list-owners`, Settings reset blocked, uninstall blocked, safe-boot blocked → run release → verify DO cleared + uninstall re-enabled.
- **CAS:** provisioning + release steps with mocked adb (matches `tests/test_cas.py` style); assert the fail-contract paths.

## 9. Out of scope (explicit)

- **True factory-reset survival** (firmware bake / EDL flash of an OEM flat-build with Companion as a system app, per tier). The deferred milestone; revisit in the firmware library.
- **Kiosk extras** — `DISALLOW_ADD_USER`, lock-task launcher pinning, app-install blocking. Future toggles.
- **Magisk system-app placement** (Approach B) — revisit alongside the firmware milestone.
- **Silent "Update all"** — enabled by this DO work, built separately.
