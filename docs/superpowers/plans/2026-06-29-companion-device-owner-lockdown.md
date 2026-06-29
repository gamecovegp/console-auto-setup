# Companion Device-Owner Lockdown — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the GameCove Companion app non-uninstallable and able to block factory reset by provisioning it as Android **Device Owner** during the ② Download flow, with a shop-only token-broadcast release path.

**Architecture:** Two repos. In **`gamecove-companion`** (Flutter + Kotlin) the app gains a `DeviceAdminReceiver`, a `DevicePolicyController` that applies/clears the lockdown, and a token-guarded `BroadcastReceiver` that self-clears Device Owner. In **`console-auto-setup`** (CAS, Python/Tkinter) the `provision()` (② Download) flow gains a best-effort `set_device_owner()` step, plus a `release()` function wired to a GUI menu item. Enforcement is pure Device Policy — **no root** is needed for the DO mechanism itself.

**Tech Stack:** Kotlin (Android `DevicePolicyManager`), Dart/Flutter (MethodChannel `gamecove/native`), Python 3 (`cas` package, `unittest`), Tkinter GUI.

## Global Constraints

- Companion package id: `com.gamecove.gamecove_companion` (verbatim; it is `COMPANION_PKG` in `cas/provision.py`).
- Method channel: `gamecove/native`.
- Device-admin component: `com.gamecove.gamecove_companion/.GcDeviceAdminReceiver`.
- Release receiver component: `com.gamecove.gamecove_companion/.GcReleaseReceiver`.
- Release broadcast action: `com.gamecove.companion.action.RELEASE`; token extra key: `token`.
- Shared guard token (default): `gc-release-7f3a9c2e` — MUST be byte-identical in the Companion `res/values/cas_release.xml` **and** CAS `config.RELEASE_TOKEN_DEFAULT`. It is a guard, not crypto (physical + USB-debug access is the real gate).
- Restrictions applied: `UserManager.DISALLOW_FACTORY_RESET` (dumpsys key `no_factory_reset`) and `UserManager.DISALLOW_SAFE_BOOT` (dumpsys key `no_safe_boot`), plus `setUninstallBlocked(self, true)`.
- `Adb.shell(cmd)` and `Adb.raw(*args)` return `(rc, out, err)`. The DO commands use **`adb shell`** (not `su`) — `dpm` runs as the shell user on a fresh device.
- Lockdown is **best-effort within ② Download**: a failure is a LOUD warning, never a hard provision abort (mirrors `install_companion`).
- Python tests run from `console-auto-setup/`: `python3 -m unittest discover -s tests -p 'test_*.py' -t .`
- Flutter tests run from `gamecove-companion/`: `flutter test`
- Commit after each task.

**On testing the Kotlin DPM code:** the Flutter android module has no JVM/Robolectric unit-test setup, and standing one up for ~40 lines of `DevicePolicyManager` glue is out of scope. Tasks A1/A2 are therefore verified by an **on-device smoke checklist** (exact adb commands + expected output) on a real fresh unit. The Dart bridge (A3) and all CAS Python (C1–C4) get real automated tests.

---

### Task A1: Device-owner policy controller + admin receiver (app)

**Repo:** `gamecove-companion`

**Files:**
- Create: `android/app/src/main/kotlin/com/gamecove/gamecove_companion/DevicePolicyController.kt`
- Create: `android/app/src/main/kotlin/com/gamecove/gamecove_companion/GcDeviceAdminReceiver.kt`
- Create: `android/app/src/main/res/xml/device_admin.xml`
- Create: `android/app/src/main/res/values/cas_release.xml`
- Modify: `android/app/src/main/AndroidManifest.xml` (add the receiver, inside `<application>`, after the `<provider>` element)

**Interfaces:**
- Produces: `object DevicePolicyController` with `fun isDeviceOwner(context): Boolean`, `fun apply(context)`, `fun release(context)`, and `val RESTRICTIONS: List<String>`. Consumed by A2 (release path) and A3 (status + reassert).

- [ ] **Step 1: Create the policy resource**

`android/app/src/main/res/xml/device_admin.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<device-admin xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-policies>
        <wipe-data />
    </uses-policies>
</device-admin>
```

- [ ] **Step 2: Create the shared guard-token resource**

`android/app/src/main/res/values/cas_release.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <!-- Operator-only un-provision guard token. NOT a cryptographic secret — physical + USB-debug
         access is the real gate. MUST match cas/config.py RELEASE_TOKEN_DEFAULT. -->
    <string name="cas_release_token" translatable="false">gc-release-7f3a9c2e</string>
</resources>
```

- [ ] **Step 3: Create `DevicePolicyController.kt`**

```kotlin
package com.gamecove.gamecove_companion

import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.os.UserManager

/** Applies / clears the GameCove device-owner lockdown. Every method is a no-op unless this app is the
 *  active Device Owner, so all are safe to call on every boot / app launch / broadcast. */
object DevicePolicyController {
    /** User restrictions held while we are Device Owner. */
    val RESTRICTIONS = listOf(
        UserManager.DISALLOW_FACTORY_RESET,
        UserManager.DISALLOW_SAFE_BOOT,
    )

    private fun dpm(context: Context) =
        context.getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager

    fun admin(context: Context) = ComponentName(context, GcDeviceAdminReceiver::class.java)

    fun isDeviceOwner(context: Context): Boolean = dpm(context).isDeviceOwnerApp(context.packageName)

    /** Apply the lockdown (idempotent). No-op unless we are Device Owner. */
    fun apply(context: Context) {
        val dpm = dpm(context)
        if (!dpm.isDeviceOwnerApp(context.packageName)) return
        val admin = admin(context)
        for (r in RESTRICTIONS) dpm.addUserRestriction(admin, r)
        dpm.setUninstallBlocked(admin, context.packageName, true)
    }

    /** Drop the lockdown and clear Device Owner (idempotent). No-op unless we are Device Owner. */
    fun release(context: Context) {
        val dpm = dpm(context)
        if (!dpm.isDeviceOwnerApp(context.packageName)) return
        val admin = admin(context)
        for (r in RESTRICTIONS) dpm.clearUserRestriction(admin, r)
        dpm.setUninstallBlocked(admin, context.packageName, false)
        @Suppress("DEPRECATION")
        dpm.clearDeviceOwnerApp(context.packageName)
    }
}
```

- [ ] **Step 4: Create `GcDeviceAdminReceiver.kt`**

```kotlin
package com.gamecove.gamecove_companion

import android.app.admin.DeviceAdminReceiver
import android.content.Context
import android.content.Intent

/** Device-admin handle DPM binds to. When this app becomes Device Owner (via `dpm set-device-owner`
 *  at provisioning) onEnabled fires and we apply the lockdown. */
class GcDeviceAdminReceiver : DeviceAdminReceiver() {
    override fun onEnabled(context: Context, intent: Intent) {
        DevicePolicyController.apply(context)
    }
}
```

- [ ] **Step 5: Wire the receiver into the manifest**

In `android/app/src/main/AndroidManifest.xml`, add inside `<application>`, immediately after the closing `</provider>` tag:
```xml
        <receiver
            android:name=".GcDeviceAdminReceiver"
            android:exported="true"
            android:permission="android.permission.BIND_DEVICE_ADMIN">
            <meta-data
                android:name="android.app.device_admin"
                android:resource="@xml/device_admin"/>
            <intent-filter>
                <action android:name="android.app.action.DEVICE_ADMIN_ENABLED"/>
            </intent-filter>
        </receiver>
```

- [ ] **Step 6: Build the app to verify it compiles**

Run (from `gamecove-companion/`): `flutter build apk --debug`
Expected: BUILD SUCCESSFUL; no Kotlin/manifest errors.

- [ ] **Step 7: On-device smoke (fresh unit, Companion installed, no accounts)**

```bash
adb shell dpm set-device-owner com.gamecove.gamecove_companion/.GcDeviceAdminReceiver
# Expected: "Success: Device owner set to package ComponentInfo{...GcDeviceAdminReceiver}"
adb shell dumpsys device_policy | grep -E "no_factory_reset|no_safe_boot"
# Expected: both keys present
adb shell pm uninstall com.gamecove.gamecove_companion
# Expected: "Failure [DELETE_FAILED_DEVICE_POLICY_MANAGER]" (uninstall blocked)
# Manually: Settings > System > Reset options > factory reset is greyed/blocked.
```
If `set-device-owner` returns "Not allowed... already some accounts on the device", the unit isn't fresh — wipe/use a fresh unit. (Leave this unit owned for Task A2's smoke.)

- [ ] **Step 8: Commit**

```bash
git add android/app/src/main/kotlin/com/gamecove/gamecove_companion/DevicePolicyController.kt \
        android/app/src/main/kotlin/com/gamecove/gamecove_companion/GcDeviceAdminReceiver.kt \
        android/app/src/main/res/xml/device_admin.xml \
        android/app/src/main/res/values/cas_release.xml \
        android/app/src/main/AndroidManifest.xml
git commit -m "feat(companion): device-owner lockdown controller + admin receiver"
```

---

### Task A2: Token-guarded release receiver (app)

**Repo:** `gamecove-companion`

**Files:**
- Create: `android/app/src/main/kotlin/com/gamecove/gamecove_companion/GcReleaseReceiver.kt`
- Modify: `android/app/src/main/AndroidManifest.xml` (add the receiver after `GcDeviceAdminReceiver`)

**Interfaces:**
- Consumes: `DevicePolicyController.release(context)` (Task A1), `R.string.cas_release_token` (Task A1 Step 2).
- Produces: a receiver for action `com.gamecove.companion.action.RELEASE` with string extra `token`.

- [ ] **Step 1: Create `GcReleaseReceiver.kt`**

```kotlin
package com.gamecove.gamecove_companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Operator-only un-provision. Triggered by an adb broadcast carrying the shared guard token
 *  (res/values/cas_release.xml). A wrong/absent token is ignored — physical + USB-debug access is the
 *  real gate; this only stops a rogue on-device app from triggering release. */
class GcReleaseReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_RELEASE) return
        val provided = intent.getStringExtra(EXTRA_TOKEN)
        val expected = context.getString(R.string.cas_release_token)
        if (provided == null || provided != expected) return
        DevicePolicyController.release(context)
    }

    companion object {
        const val ACTION_RELEASE = "com.gamecove.companion.action.RELEASE"
        const val EXTRA_TOKEN = "token"
    }
}
```

- [ ] **Step 2: Wire the receiver into the manifest**

In `android/app/src/main/AndroidManifest.xml`, add inside `<application>`, immediately after the `GcDeviceAdminReceiver` `</receiver>`:
```xml
        <receiver
            android:name=".GcReleaseReceiver"
            android:exported="true">
            <intent-filter>
                <action android:name="com.gamecove.companion.action.RELEASE"/>
            </intent-filter>
        </receiver>
```

- [ ] **Step 3: Build to verify it compiles**

Run: `flutter build apk --debug`
Expected: BUILD SUCCESSFUL.

- [ ] **Step 4: On-device smoke (reuse the owned unit from Task A1)**

```bash
adb shell am broadcast -a com.gamecove.companion.action.RELEASE -e token WRONG \
  -n com.gamecove.gamecove_companion/.GcReleaseReceiver
adb shell dpm list-owners
# Expected: still shows Companion (wrong token ignored)

adb shell am broadcast -a com.gamecove.companion.action.RELEASE -e token gc-release-7f3a9c2e \
  -n com.gamecove.gamecove_companion/.GcReleaseReceiver
adb shell dpm list-owners
# Expected: "No device owner." (cleared)
adb shell pm uninstall com.gamecove.gamecove_companion
# Expected: "Success" (uninstall now permitted)
```

- [ ] **Step 5: Commit**

```bash
git add android/app/src/main/kotlin/com/gamecove/gamecove_companion/GcReleaseReceiver.kt \
        android/app/src/main/AndroidManifest.xml
git commit -m "feat(companion): token-guarded device-owner release receiver"
```

---

### Task A3: Device-owner status bridge + reassert-on-launch (app)

**Repo:** `gamecove-companion`

**Files:**
- Modify: `android/app/src/main/kotlin/com/gamecove/gamecove_companion/MainActivity.kt` (add a `deviceOwnerStatus` method case + a reassert call in `configureFlutterEngine`)
- Create: `android/app/src/main/kotlin/com/gamecove/gamecove_companion/GcBootReceiver.kt` (BOOT_COMPLETED reassert — Step 6)
- Modify: `android/app/src/main/AndroidManifest.xml` (RECEIVE_BOOT_COMPLETED permission + GcBootReceiver — Step 6)
- Modify: `lib/platform/native_bridge.dart` (add `isDeviceOwner()`)
- Test: `test/platform/native_bridge_device_owner_test.dart` (new)

**Interfaces:**
- Consumes: `DevicePolicyController` (Task A1).
- Produces: Dart `NativeBridge().isDeviceOwner() -> Future<bool>`; native method `deviceOwnerStatus` returning `{isDeviceOwner: bool, restrictions: List<String>}`.

- [ ] **Step 1: Write the failing Dart test**

`test/platform/native_bridge_device_owner_test.dart`:
```dart
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/platform/native_bridge.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('isDeviceOwner reads the native device-owner status', () async {
    const channel = MethodChannel('gamecove/native');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      if (call.method == 'deviceOwnerStatus') {
        return {
          'isDeviceOwner': true,
          'restrictions': <String>['no_user_control_disabled'],
        };
      }
      return null;
    });
    expect(await NativeBridge().isDeviceOwner(), isTrue);
  });

  test('isDeviceOwner defaults to false when native returns null', () async {
    const channel = MethodChannel('gamecove/native');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async => null);
    expect(await NativeBridge().isDeviceOwner(), isFalse);
  });
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `flutter test test/platform/native_bridge_device_owner_test.dart`
Expected: FAIL — `NativeBridge` has no method `isDeviceOwner`.

- [ ] **Step 3: Add the Dart bridge method**

In `lib/platform/native_bridge.dart`, add inside the `NativeBridge` class (after `deviceInfo()`):
```dart
  /// Whether this app is the active Device Owner (the lockdown is in force). False on error/legacy.
  Future<bool> isDeviceOwner() async {
    final m = await _channel.invokeMapMethod<String, dynamic>('deviceOwnerStatus');
    return (m?['isDeviceOwner'] as bool?) ?? false;
  }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `flutter test test/platform/native_bridge_device_owner_test.dart`
Expected: PASS (both tests).

- [ ] **Step 5: Add the native method + reassert-on-launch**

In `MainActivity.kt`, add this line at the start of `configureFlutterEngine`, immediately after `super.configureFlutterEngine(flutterEngine)`:
```kotlin
        DevicePolicyController.apply(this)   // re-assert the lockdown on every launch (no-op unless DO)
```
And add this case to the `when (call.method)` block (e.g. after the `"deviceInfo"` case):
```kotlin
                    "deviceOwnerStatus" -> result.success(
                        mapOf(
                            "isDeviceOwner" to DevicePolicyController.isDeviceOwner(this),
                            "restrictions" to DevicePolicyController.RESTRICTIONS,
                        )
                    )
```

- [ ] **Step 6: Add a BOOT_COMPLETED reassert receiver**

Spec §4.2 requires the lockdown to re-assert on `onEnabled`, app launch, AND boot. `onEnabled` (A1) and launch (Step 5) are wired; this adds boot. Because CAS reboots the unit immediately after locking, the boot reassert re-applies the restrictions on first boot without waiting for the user to open the app.

Create `android/app/src/main/kotlin/com/gamecove/gamecove_companion/GcBootReceiver.kt`:
```kotlin
package com.gamecove.gamecove_companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Re-asserts the device-owner lockdown on boot (no-op unless this app is Device Owner). */
class GcBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) DevicePolicyController.apply(context)
    }
}
```
Add the permission at the top of `AndroidManifest.xml` (with the other `<uses-permission>` lines):
```xml
    <uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED"/>
```
And register the receiver inside `<application>`, after the `GcReleaseReceiver`:
```xml
        <receiver
            android:name=".GcBootReceiver"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.BOOT_COMPLETED"/>
            </intent-filter>
        </receiver>
```

- [ ] **Step 7: Build to verify it compiles**

Run: `flutter build apk --debug`
Expected: BUILD SUCCESSFUL.

- [ ] **Step 8: Commit**

```bash
git add lib/platform/native_bridge.dart \
        android/app/src/main/kotlin/com/gamecove/gamecove_companion/MainActivity.kt \
        android/app/src/main/kotlin/com/gamecove/gamecove_companion/GcBootReceiver.kt \
        android/app/src/main/AndroidManifest.xml \
        test/platform/native_bridge_device_owner_test.dart
git commit -m "feat(companion): deviceOwnerStatus bridge + reassert lockdown on launch/boot"
```

---

### Task C1: Release-token config getter (CAS)

**Repo:** `console-auto-setup`

**Files:**
- Modify: `cas/config.py` (add `RELEASE_TOKEN_DEFAULT` + `get_release_token()`)
- Test: `tests/test_cas.py` (new `TestReleaseToken` class)

**Interfaces:**
- Produces: `config.RELEASE_TOKEN_DEFAULT` (str) and `config.get_release_token() -> str`. Consumed by C2 `release()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cas.py` (new top-level class):
```python
class TestReleaseToken(unittest.TestCase):
    def test_default_token_when_no_override(self):
        from cas import config as C
        orig = C.load_config
        C.load_config = lambda: {}
        try:
            self.assertEqual(C.get_release_token(), C.RELEASE_TOKEN_DEFAULT)
            self.assertEqual(C.RELEASE_TOKEN_DEFAULT, "gc-release-7f3a9c2e")
        finally:
            C.load_config = orig

    def test_operator_override_wins(self):
        from cas import config as C
        orig = C.load_config
        C.load_config = lambda: {"release_token": "custom-xyz"}
        try:
            self.assertEqual(C.get_release_token(), "custom-xyz")
        finally:
            C.load_config = orig
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_cas.TestReleaseToken -v`
Expected: FAIL — `module 'cas.config' has no attribute 'RELEASE_TOKEN_DEFAULT'`.

- [ ] **Step 3: Add the config getter**

In `cas/config.py`, add after the `get_nas_credentials()` function:
```python
# Operator-only un-provision guard token. NOT a cryptographic secret (physical + USB-debug access is the
# real gate); it only stops a rogue on-device app from triggering release. MUST match the Companion app's
# res/values/cas_release.xml. Operator can override per-PC via cas-config.json ("release_token").
RELEASE_TOKEN_DEFAULT = "gc-release-7f3a9c2e"


def get_release_token():
    """The release guard token: an operator override from cas-config.json if present, else the shipped
    default (which matches the Companion build)."""
    cfg = load_config()
    t = cfg.get("release_token")
    return t if t else RELEASE_TOKEN_DEFAULT
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_cas.TestReleaseToken -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(cas): release-token config getter (default + operator override)"
```

---

### Task C2: Device-owner set + release functions (CAS)

**Repo:** `console-auto-setup`

**Files:**
- Modify: `cas/provision.py` (add constants + `_is_device_owner`, `set_device_owner`, `release`)
- Modify: `tests/test_cas.py` (extend `FakeRunner`; new `TestDeviceOwner` class)

**Interfaces:**
- Consumes: `Adb.shell` (returns `(rc, out, err)`), `config.get_release_token()` (Task C1), `COMPANION_PKG` (existing in `provision.py`).
- Produces: `provision.set_device_owner(adb, log=print) -> bool`, `provision.release(adb, log=print) -> bool`, and module constants `DEVICE_ADMIN`, `RELEASE_RECEIVER`, `RELEASE_ACTION`. Consumed by C3 (provision flow) and C4 (GUI).

- [ ] **Step 1: Extend `FakeRunner` to answer dpm/dumpsys/am**

In `tests/test_cas.py`, modify `FakeRunner.__init__` signature to add the new kwargs (keep all existing params/order; append these):
```python
    def __init__(self, model="Odin2 Mini", golden=False, root=True, boot="1", sd=True,
                 push_ok=True, pull_ok=True, su_blocked=False, slot="_a", first_api="33",
                 device_owner=False, do_set_ok=True, do_restrict=True, release_clears=True):
```
At the end of `__init__`, add:
```python
        self._owner = device_owner          # current device-owner state (mutated by a release broadcast)
        self.do_set_ok, self.do_restrict, self.release_clears = do_set_ok, do_restrict, release_clears
```
Then, inside `__call__`, in the `if "shell" in args:` branch, immediately after `tail = args[-1]` (before the `boot_patch.sh` check), insert:
```python
            if tail.startswith("dpm list-owners"):
                return 0, ("Device owner: com.gamecove.gamecove_companion\n" if self._owner
                           else "No device owner.\n"), ""
            if tail.startswith("dpm set-device-owner"):
                if self.do_set_ok:
                    self._owner = True
                    return 0, "Success: Device owner set to package\n", ""
                return 255, "", "java.lang.IllegalStateException: Not allowed to set the device owner\n"
            if tail.startswith("dumpsys device_policy"):
                return 0, ("no_factory_reset no_safe_boot\n" if (self._owner and self.do_restrict)
                           else "\n"), ""
            if tail.startswith("am broadcast") and "action.RELEASE" in tail:
                if self.release_clears and "gc-release-7f3a9c2e" in tail:
                    self._owner = False
                return 0, "Broadcast completed: result=0\n", ""
            if tail.startswith("am start"):
                return 0, "Starting: Intent\n", ""
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_cas.py` (new top-level class):
```python
class TestDeviceOwner(unittest.TestCase):
    def _adb(self, **kw):
        return Adb(runner=FakeRunner(**kw))

    def test_set_device_owner_success(self):
        a = self._adb(device_owner=False, do_set_ok=True, do_restrict=True)
        self.assertTrue(PV.set_device_owner(a, log=lambda *_: None))
        self.assertTrue(any("dpm set-device-owner" in c for c in a.runner.cmds()))

    def test_set_device_owner_idempotent_when_already_owner(self):
        r = FakeRunner(device_owner=True, do_restrict=True)
        a = Adb(runner=r)
        self.assertTrue(PV.set_device_owner(a, log=lambda *_: None))
        self.assertFalse(any("dpm set-device-owner" in c for c in r.cmds()))  # did not re-set

    def test_set_device_owner_fails_when_not_fresh(self):
        a = self._adb(device_owner=False, do_set_ok=False)
        self.assertFalse(PV.set_device_owner(a, log=lambda *_: None))

    def test_set_device_owner_fails_when_restrictions_missing(self):
        a = self._adb(device_owner=False, do_set_ok=True, do_restrict=False)
        self.assertFalse(PV.set_device_owner(a, log=lambda *_: None))

    def test_release_sends_token_broadcast_and_confirms_cleared(self):
        r = FakeRunner(device_owner=True, release_clears=True)
        a = Adb(runner=r)
        self.assertTrue(PV.release(a, log=lambda *_: None))
        self.assertTrue(any("am broadcast" in c and "action.RELEASE" in c for c in r.cmds()))

    def test_release_fails_when_owner_not_cleared(self):
        a = Adb(runner=FakeRunner(device_owner=True, release_clears=False))
        self.assertFalse(PV.release(a, log=lambda *_: None))

    def test_release_noop_when_not_owner(self):
        a = Adb(runner=FakeRunner(device_owner=False))
        self.assertTrue(PV.release(a, log=lambda *_: None))
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_cas.TestDeviceOwner -v`
Expected: FAIL — `module 'cas.provision' has no attribute 'set_device_owner'`.

- [ ] **Step 4: Add the constants + functions to `provision.py`**

In `cas/provision.py`, add after the `COMPANION_SRC = ...` line (near the existing `COMPANION_PKG`):
```python
DEVICE_ADMIN = f"{COMPANION_PKG}/.GcDeviceAdminReceiver"     # the Companion's DeviceAdminReceiver
RELEASE_RECEIVER = f"{COMPANION_PKG}/.GcReleaseReceiver"
RELEASE_ACTION = "com.gamecove.companion.action.RELEASE"
_LOCK_RESTRICTIONS = ("no_factory_reset", "no_safe_boot")    # dumpsys keys for the applied restrictions
```
Then add these functions (place them just above `def provision(`):
```python
def _is_device_owner(adb):
    """True if the Companion app is the active Device Owner on this unit."""
    rc, out, _ = adb.shell("dpm list-owners")
    return rc == 0 and COMPANION_PKG in out


def set_device_owner(adb, log=print):
    """Make the Companion the Device Owner: non-uninstallable + can block factory reset. Idempotent — a
    unit that already has the Companion as Device Owner is a success (re-assert + verify). Returns True
    only when ownership AND the lockdown restrictions are confirmed. The CALLER decides how to treat a
    False (Download treats it as a loud warning, not an abort)."""
    if _is_device_owner(adb):
        log("Companion already Device Owner — re-asserting lockdown.")
    else:
        rc, out, err = adb.shell(f"dpm set-device-owner {DEVICE_ADMIN}")
        if rc != 0 or "Success" not in out:
            log(f"Device Owner NOT set ({(err or out).strip()}). Needs a FRESH unit (no accounts / "
                "secondary users). Unit is NOT locked down.")
            return False
        log("Companion set as Device Owner.")
    adb.shell(f"am start -n {COMPANION_PKG}/.MainActivity")   # nudge so onEnabled/launch re-assert ran
    rc, dump, _ = adb.shell("dumpsys device_policy")
    missing = [r for r in _LOCK_RESTRICTIONS if r not in dump]
    if missing:
        log(f"Device Owner set but restrictions missing {missing} — lockdown NOT confirmed.")
        return False
    log("lockdown confirmed (non-uninstallable + factory-reset/safe-boot blocked).")
    return True


def release(adb, log=print):
    """Operator-only un-provision: tell the Companion (via a token-guarded broadcast) to drop the lockdown
    and clear Device Owner, so the unit can be factory-reset / the app uninstalled (RMA/repair/resale).
    Returns True once Device Owner is confirmed cleared. If this ever fails, an EDL/recovery wipe remains
    the hard fallback."""
    from . import config as _cfg
    if not _is_device_owner(adb):
        log("Companion is not Device Owner on this unit — nothing to release.")
        return True
    log("sending un-provision (release) broadcast to the Companion...")
    adb.shell(f"am broadcast -a {RELEASE_ACTION} -e token {_cfg.get_release_token()} -n {RELEASE_RECEIVER}")
    if _is_device_owner(adb):
        log("release did NOT clear Device Owner (token mismatch or app missing?). Unit still locked — "
            "retry, or fall back to an EDL/recovery wipe.")
        return False
    log("unit released — Device Owner cleared; factory reset / uninstall now permitted.")
    return True
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_cas.TestDeviceOwner -v`
Expected: PASS (all 7).

- [ ] **Step 6: Run the full suite (no regressions in FakeRunner consumers)**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -t .`
Expected: OK (all tests pass).

- [ ] **Step 7: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(cas): set_device_owner + release functions with idempotent verify"
```

---

### Task C3: Wire lockdown into the ② Download flow (CAS)

**Repo:** `console-auto-setup`

**Files:**
- Modify: `cas/provision.py` (call `set_device_owner` inside `provision()` after `install_companion`)
- Test: `tests/test_cas.py` (new `TestProvisionLockdown` class)

**Interfaces:**
- Consumes: `set_device_owner` (Task C2), `COMPANION_PKG`, `profile.flags()` (the manifest `@`-flags dict).
- Produces: lockdown applied during `provision()` when Companion is present and `@lockdown` ≠ `off`; failure is a loud warning (provision still returns True).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cas.py` (new top-level class; uses the existing `make_profile` helper):
```python
class TestProvisionLockdown(unittest.TestCase):
    def _profile(self, tmp, flags):
        apps = ["org.es_de.frontend", PV.COMPANION_PKG]
        d = pathlib.Path(tmp) / "p"
        pay = d / "golden_root_payload"
        pay.mkdir(parents=True)
        (d / "profile.meta").write_text("model_match=Odin2 ?Mini\nfrontend=es-de\ncaptured=2026-06-16\n")
        (pay / "pkglist.txt").write_text("\n".join(apps) + "\n")
        (pay / "global.meta").write_text("golden_serial=9C33-6BBD\n")
        for a in apps:
            (pay / a / "apk").mkdir(parents=True)
            (pay / a / "apk" / "base.apk").write_text("x")
            (pay / a / "data.tar").write_text("x")
        P.save_manifest(d / "manifest", apps, flags, header="# p")
        return P.Profile(d)

    def test_download_sets_device_owner_when_lockdown_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile(tmp, {"settings": "on", "lockdown": "on"})
            r = FakeRunner()
            self.assertTrue(PV.provision(Adb(runner=r), prof, log=lambda *_: None))
            self.assertTrue(any("dpm set-device-owner" in c for c in r.cmds()))

    def test_download_skips_device_owner_when_lockdown_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile(tmp, {"settings": "on", "lockdown": "off"})
            r = FakeRunner()
            self.assertTrue(PV.provision(Adb(runner=r), prof, log=lambda *_: None))
            self.assertFalse(any("dpm set-device-owner" in c for c in r.cmds()))

    def test_download_succeeds_even_if_lockdown_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile(tmp, {"settings": "on", "lockdown": "on"})
            r = FakeRunner(do_set_ok=False)          # device not fresh -> lockdown fails
            self.assertTrue(PV.provision(Adb(runner=r), prof, log=lambda *_: None))  # still provisions
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_cas.TestProvisionLockdown -v`
Expected: FAIL — no `dpm set-device-owner` call is emitted by `provision()` yet.

- [ ] **Step 3: Add the lockdown call to `provision()`**

In `cas/provision.py`, in `provision()`, find:
```python
    if not dry_push and COMPANION_PKG in pkgs:
        install_companion(adb, log=log)                # refresh the in-manifest Companion app to the PC build
```
Immediately after that block, insert:
```python
        # Lockdown rides ② Download: make the Companion the Device Owner so it's non-uninstallable and
        # factory reset is blocked. Default ON when the Companion ships; `@lockdown off` opts a profile out.
        # Best-effort, like install_companion above: a failure is a LOUD warning, not a provision abort.
        if flags.get("lockdown", "on") != "off":
            if not set_device_owner(adb, log=log):
                log("WARNING: device-owner lockdown FAILED — unit shipped UN-LOCKED (uninstallable / "
                    "factory-resettable). Ensure the unit is FRESH and re-Download to lock it.")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_cas.TestProvisionLockdown -v`
Expected: PASS (all 3).

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -t .`
Expected: OK.

- [ ] **Step 6: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(cas): apply device-owner lockdown during Download (@lockdown, best-effort)"
```

---

### Task C4: Release menu action in the GUI (CAS)

**Repo:** `console-auto-setup`

**Files:**
- Modify: `cas/gui.py` (add a "Release selected unit…" menu command + handler, mirroring `capture_update`'s single-device `_run_bg` pattern)

**Interfaces:**
- Consumes: `PV.release` (Task C2), existing GUI helpers `self._selected_serial()`, `self._run_bg(fn, label=...)`, `self.cancel_event`, `self.adb_bin`, `Adb`, `messagebox`.
- Produces: a GUI entry point to un-provision a single selected unit. (gui.py has no automated tests in this repo; verified by launching the app — consistent with the rest of `gui.py`.)

- [ ] **Step 1: Add the handler method**

In `cas/gui.py`, add this method to the app class (place it next to `capture_update`, around line 1294):
```python
    def release_selected(self):
        """Operator-only: un-provision the selected unit (clear the Companion's Device-Owner lockdown so it
        can be factory-reset / uninstalled). Exceptional RMA action — single device, behind a confirm."""
        serial = self._selected_serial()
        if not serial:
            messagebox.showinfo("CAS", "Select ONE device in the list first.")
            return
        if not messagebox.askyesno(
                "CAS — release (un-provision) unit?",
                f"Clear the GameCove Companion lockdown on {serial}?\n\n"
                "After this, the app can be uninstalled and the unit can be factory-reset. "
                "Use this for RMA / repair / resale."):
            return
        def work():
            return PV.release(Adb(serial=serial, adb=self.adb_bin, cancel=self.cancel_event), log=self.log)
        self._run_bg(work, label=f"Releasing {serial}")
```

- [ ] **Step 2: Add the menu command**

In `cas/gui.py`, in `_build_menu`, in the Settings menu block (after the `setm.add_command(label="NAS login…", ...)` line), add:
```python
        setm.add_separator()
        setm.add_command(label="Release selected unit (un-provision)…", command=self.release_selected)
```

- [ ] **Step 3: Verify the module imports/parses**

Run: `python3 -c "import ast; ast.parse(open('cas/gui.py').read()); print('gui.py OK')"`
Expected: `gui.py OK`.

- [ ] **Step 4: Manual GUI verification**

Launch CAS (`python3 -m cas` or the project's run entry), connect a locked unit, select it, and run **Settings → Release selected unit (un-provision)…**. Confirm the log shows "unit released — Device Owner cleared". (No automated test — `gui.py` is untested by design in this repo.)

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py
git commit -m "feat(cas-gui): Release (un-provision) menu action for the selected unit"
```

---

## Self-Review

**Spec coverage:**
- §3 non-uninstallable + reset block → Tasks A1 (controller/receiver/restrictions/uninstall-block), A3 (reassert).
- §4 app components (admin receiver, policy applier, release receiver, status surface) → A1, A2, A3.
- §5 CAS provisioning hook in ② Download, `@lockdown` default-on, idempotent, best-effort warning → C2 (`set_device_owner`), C3 (wiring + flag + warning).
- §6 release flow (token broadcast, confirm cleared, idempotent) → A2 (receiver), C1 (token), C2 (`release`), C4 (GUI entry).
- §7 failure handling (loud warning, not abort; idempotent) → C2, C3.
- §8 testing (Dart unit, CAS unit, on-device smoke) → A1/A2 smoke, A3 Dart test, C1–C3 unit tests.
- §9 out-of-scope (firmware survival, kiosk extras, Magisk placement, silent update) → not implemented, by design.

**Placeholder scan:** none — every step has concrete code/commands. `<secret>`/`<serial>` appear only in the spec prose, not as plan code; the plan uses the literal token `gc-release-7f3a9c2e` and real serials from fixtures.

**Type/name consistency:** `DevicePolicyController` (`apply`/`release`/`isDeviceOwner`/`RESTRICTIONS`), `GcDeviceAdminReceiver`, `GcReleaseReceiver`, action `com.gamecove.companion.action.RELEASE`, token extra `token`, channel `gamecove/native`, method `deviceOwnerStatus`, CAS `set_device_owner`/`release`/`_is_device_owner`/`DEVICE_ADMIN`/`RELEASE_RECEIVER`/`RELEASE_ACTION`/`_LOCK_RESTRICTIONS`, `config.RELEASE_TOKEN_DEFAULT`/`get_release_token`, token `gc-release-7f3a9c2e` — all used identically across tasks and both repos.
