# GameCove Companion — Plan 2: App Self-Update (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a curated, GitHub-hosted **app self-update** channel — the app checks an `app/latest.json` manifest, and on a newer version downloads the APK, sha256-verifies it, and installs it (prompted) — plus an Updates screen and a CI workflow that publishes releases.

**Architecture:** Reuses the Plan 1 machine (compare-version → download → sha256-verify → act). A `UpdateService` (Dart, `http`-based, injectable client) checks `app/latest.json` and downloads/verifies the APK; the native bridge gains `installApk(path)` (Android PackageInstaller via FileProvider, prompted); an `UpdatesScreen` drives it. A GitHub Actions workflow builds the release APK, attaches it to a Release, and updates the manifest. **The GitHub remote is linked by the owner later** — this plan only authors repo-side artifacts (workflow + config + docs); nothing is pushed.

**Tech Stack:** Existing app (Flutter 3.44 / Dart 3.12) at `/home/ccvisionary/Documents/Work/[07] Projects/gamecove-companion/`. New deps: `package_info_plus` (current versionCode). Tests use `http/testing.dart` `MockClient`.

**Scope note:** Plan 2 of 3 (see spec §5.1, §5.4). The **emulator** update channel is Plan 3 — out of scope. Install UX is **prompted** (one system dialog); device-owner silent install is a later fast-follow and is NOT in this plan.

> ⚠️ The native install path and the CI workflow **cannot be integration-tested here** (no Android/Gradle build — Java 26 may be incompatible — and no GitHub remote). They are authored + code-reviewed; the Dart logic (manifest parse, version compare, sha256, screen states) is fully unit/widget-tested. The actual install + CI run get verified on-device / once the remote is linked.

---

## File Structure (additions to the existing app)

```
lib/
  config.dart                          # const contentBaseUrl + updateBaseUrl (one config seam)
  models/app_release.dart              # AppRelease (+ fromJson)
  services/
    update_service.dart                # check() + downloadApk(): http GET, version compare, sha256
    app_version.dart                   # current versionCode via package_info_plus
  platform/native_bridge.dart          # ADD installApk(path)
  ui/updates_screen.dart               # check -> up-to-date / available -> install
  ui/home_screen.dart                  # ADD an "Updates" tile -> UpdatesScreen
  main.dart                            # use config.dart; throttled launch-time update check
android/app/src/main/
  AndroidManifest.xml                  # ADD REQUEST_INSTALL_PACKAGES + FileProvider
  res/xml/file_paths.xml               # FileProvider paths
  kotlin/.../MainActivity.kt           # ADD installApk handler
.github/workflows/release.yml          # build APK -> Release asset -> update latest.json
docs/RELEASE.md                        # how to link the remote + cut a release
test/
  models/app_release_test.dart
  services/update_service_test.dart
  ui/updates_screen_test.dart
```

---

## Task 1: Config seam (`config.dart`)

**Files:**
- Create: `lib/config.dart`
- Modify: `lib/main.dart`

- [ ] **Step 1: Create `lib/config.dart`**

```dart
// Central config for the GitHub-hosted feeds. The owner sets these to their
// repo's Pages/CDN base when the remote is linked (trailing slash required).
const String contentBaseUrl = 'https://gamecove.github.io/companion-content/';
const String updateBaseUrl = 'https://gamecove.github.io/companion-releases/';
```

- [ ] **Step 2: Use it in `main.dart`** — replace the inline `contentBaseUrl` const with an import.

In `lib/main.dart`, remove the local `const contentBaseUrl = ...;` line and add at the top:
```dart
import 'config.dart';
```
The existing `service.refresh(RemoteContentSource(contentBaseUrl));` now references the imported constant unchanged.

- [ ] **Step 3: Verify + commit**

Run: `flutter analyze` → Expected: no issues. Run: `flutter test` → Expected: 18/18 still pass.
```bash
git add lib/config.dart lib/main.dart
git commit -m "refactor: centralize feed base URLs in config.dart"
```

---

## Task 2: `AppRelease` model

**Files:**
- Create: `lib/models/app_release.dart`
- Test: `test/models/app_release_test.dart`

- [ ] **Step 1: Write the failing test** — `test/models/app_release_test.dart`:

```dart
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/models/app_release.dart';

void main() {
  test('AppRelease parses the manifest fields', () {
    final r = AppRelease.fromJson({
      'versionCode': 7,
      'versionName': '1.2.0',
      'url': 'https://x/app-7.apk',
      'sha256': 'deadbeef',
      'minSupported': 3,
      'notes': 'Bug fixes',
    });
    expect(r.versionCode, 7);
    expect(r.versionName, '1.2.0');
    expect(r.url, 'https://x/app-7.apk');
    expect(r.sha256, 'deadbeef');
    expect(r.minSupported, 3);
    expect(r.notes, 'Bug fixes');
  });

  test('AppRelease tolerates a missing notes/minSupported', () {
    final r = AppRelease.fromJson({
      'versionCode': 1, 'versionName': '1.0.0', 'url': 'u', 'sha256': 's',
    });
    expect(r.minSupported, 0);
    expect(r.notes, '');
  });
}
```

- [ ] **Step 2: Run → FAIL** (`app_release.dart` not found): `flutter test test/models/app_release_test.dart`

- [ ] **Step 3: Implement** — `lib/models/app_release.dart`:

```dart
class AppRelease {
  final int versionCode;
  final String versionName;
  final String url;
  final String sha256;
  final int minSupported;
  final String notes;
  const AppRelease({
    required this.versionCode,
    required this.versionName,
    required this.url,
    required this.sha256,
    this.minSupported = 0,
    this.notes = '',
  });
  factory AppRelease.fromJson(Map<String, dynamic> j) => AppRelease(
        versionCode: j['versionCode'] as int,
        versionName: j['versionName'] as String,
        url: j['url'] as String,
        sha256: j['sha256'] as String,
        minSupported: (j['minSupported'] as int?) ?? 0,
        notes: (j['notes'] as String?) ?? '',
      );
}
```

- [ ] **Step 4: Run → PASS** (2 tests). 

- [ ] **Step 5: Commit**
```bash
git add lib/models/app_release.dart test/models/app_release_test.dart
git commit -m "feat: AppRelease manifest model"
```

---

## Task 3: `UpdateService.check`

**Files:**
- Create: `lib/services/update_service.dart`
- Test: `test/services/update_service_test.dart`

- [ ] **Step 1: Write the failing test** — `test/services/update_service_test.dart`:

```dart
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:http/http.dart' as http;
import 'package:gamecove_companion/services/update_service.dart';

String _manifest(int code) => jsonEncode({
      'versionCode': code, 'versionName': '1.$code.0',
      'url': 'https://x/app-$code.apk', 'sha256': 'abc', 'minSupported': 1, 'notes': 'n',
    });

void main() {
  test('check returns a release when the manifest is newer', () async {
    final client = MockClient((req) async => http.Response(_manifest(5), 200));
    final svc = UpdateService('https://base/', client: client);
    final r = await svc.check(4);
    expect(r, isNotNull);
    expect(r!.versionCode, 5);
  });

  test('check returns null when not newer (equal or older)', () async {
    final client = MockClient((req) async => http.Response(_manifest(4), 200));
    final svc = UpdateService('https://base/', client: client);
    expect(await svc.check(4), isNull); // equal
    expect(await svc.check(9), isNull); // current is newer
  });

  test('check returns null on a network/HTTP error', () async {
    final client = MockClient((req) async => http.Response('nope', 500));
    final svc = UpdateService('https://base/', client: client);
    expect(await svc.check(1), isNull);
  });

  test('check requests app/latest.json under the base url', () async {
    late Uri seen;
    final client = MockClient((req) async {
      seen = req.url;
      return http.Response(_manifest(2), 200);
    });
    await UpdateService('https://base/', client: client).check(1);
    expect(seen.toString(), 'https://base/app/latest.json');
  });
}
```

- [ ] **Step 2: Run → FAIL** (`update_service.dart` not found).

- [ ] **Step 3: Implement** — `lib/services/update_service.dart`:

```dart
import 'dart:convert';
import 'package:http/http.dart' as http;
import '../models/app_release.dart';

class UpdateService {
  final String baseUrl; // trailing slash
  final http.Client client;
  UpdateService(this.baseUrl, {http.Client? client}) : client = client ?? http.Client();

  /// Fetch app/latest.json; return the release only if it is newer than
  /// [currentVersionCode]. Returns null on not-newer or any error.
  Future<AppRelease?> check(int currentVersionCode) async {
    try {
      final r = await client.get(Uri.parse('${baseUrl}app/latest.json'));
      if (r.statusCode != 200) return null;
      final release =
          AppRelease.fromJson(jsonDecode(r.body) as Map<String, dynamic>);
      if (release.versionCode <= currentVersionCode) return null;
      return release;
    } catch (_) {
      return null;
    }
  }
}
```

- [ ] **Step 4: Run → PASS** (4 tests).

- [ ] **Step 5: Commit**
```bash
git add lib/services/update_service.dart test/services/update_service_test.dart
git commit -m "feat: UpdateService.check (manifest fetch + version compare)"
```

---

## Task 4: `UpdateService.downloadApk` (sha256-verified)

**Files:**
- Modify: `lib/services/update_service.dart`
- Test: `test/services/update_service_download_test.dart`

- [ ] **Step 1: Write the failing test** — `test/services/update_service_download_test.dart`:

```dart
import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:http/http.dart' as http;
import 'package:gamecove_companion/models/app_release.dart';
import 'package:gamecove_companion/services/update_service.dart';

void main() {
  final tmp = Directory.systemTemp.createTempSync('upd_test').path;
  final apkBytes = List<int>.generate(64, (i) => i);
  final goodSha = sha256.convert(apkBytes).toString();

  AppRelease rel(String sha) => AppRelease(
      versionCode: 5, versionName: '1.5.0', url: 'https://x/app.apk', sha256: sha);

  test('downloadApk writes the file when the sha256 matches', () async {
    final client = MockClient((req) async => http.Response.bytes(apkBytes, 200));
    final svc = UpdateService('https://base/', client: client);
    final path = await svc.downloadApk(rel(goodSha), toDir: tmp);
    expect(File(path).existsSync(), true);
    expect(File(path).readAsBytesSync(), apkBytes);
  });

  test('downloadApk throws and writes nothing on a sha256 mismatch', () async {
    final client = MockClient((req) async => http.Response.bytes(apkBytes, 200));
    final svc = UpdateService('https://base/', client: client);
    expect(() => svc.downloadApk(rel('wronghash'), toDir: tmp), throwsException);
  });
}
```

- [ ] **Step 2: Run → FAIL** (`downloadApk` not defined).

- [ ] **Step 3: Add to `UpdateService`** — append `import 'dart:io';` and `import 'package:crypto/crypto.dart';` to the top of `update_service.dart`, then add inside the class:

```dart
  /// Download the APK to [toDir], verify its sha256 against the manifest, and
  /// return the file path. Throws (and writes no final file) on mismatch.
  Future<String> downloadApk(AppRelease release, {required String toDir}) async {
    final r = await client.get(Uri.parse(release.url));
    if (r.statusCode != 200) throw Exception('download ${release.url} -> ${r.statusCode}');
    final digest = sha256.convert(r.bodyBytes).toString();
    if (digest != release.sha256) {
      throw Exception('sha256 mismatch: got $digest want ${release.sha256}');
    }
    final path = '$toDir/gamecove-${release.versionCode}.apk';
    await File(path).writeAsBytes(r.bodyBytes, flush: true);
    return path;
  }
```

- [ ] **Step 4: Run → PASS** (2 tests). Then run the full suite (`flutter test`) — Expected: all green.

- [ ] **Step 5: Commit**
```bash
git add lib/services/update_service.dart test/services/update_service_download_test.dart
git commit -m "feat: UpdateService.downloadApk with sha256 verification"
```

---

## Task 5: Current app version (`app_version.dart`)

**Files:**
- Create: `lib/services/app_version.dart`
- Modify: `pubspec.yaml` (add `package_info_plus`)

- [ ] **Step 1: Add the dependency**
```bash
flutter pub add package_info_plus
```

- [ ] **Step 2: Implement** — `lib/services/app_version.dart`:

```dart
import 'package:package_info_plus/package_info_plus.dart';

/// Reads the installed app's versionCode (Android buildNumber).
class AppVersion {
  static Future<int> current() async {
    final info = await PackageInfo.fromPlatform();
    return int.tryParse(info.buildNumber) ?? 0;
  }
}
```
> No unit test: `PackageInfo.fromPlatform()` needs the platform channel. It's exercised at runtime; the Updates screen takes the versionCode as an injected int so the screen stays testable.

- [ ] **Step 3: Verify + commit**

Run: `flutter analyze` → no issues. `flutter test` → still green.
```bash
git add lib/services/app_version.dart pubspec.yaml pubspec.lock
git commit -m "feat: AppVersion.current() via package_info_plus"
```

---

## Task 6: Native `installApk` (PackageInstaller via FileProvider)

**Files:**
- Modify: `lib/platform/native_bridge.dart`
- Modify: `android/app/src/main/AndroidManifest.xml`
- Create: `android/app/src/main/res/xml/file_paths.xml`
- Modify: `android/app/src/main/kotlin/com/gamecove/gamecove_companion/MainActivity.kt`
- Test: `test/platform/native_bridge_install_test.dart`

- [ ] **Step 1: Write the failing test (Dart side)** — `test/platform/native_bridge_install_test.dart`:

```dart
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/platform/native_bridge.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('installApk invokes the native method with the path', () async {
    const channel = MethodChannel('gamecove/native');
    final calls = <MethodCall>[];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      return null;
    });

    await NativeBridge().installApk('/tmp/app.apk');

    expect(calls.single.method, 'installApk');
    expect((calls.single.arguments as Map)['path'], '/tmp/app.apk');
  });
}
```

- [ ] **Step 2: Run → FAIL** (`installApk` not defined).

- [ ] **Step 3: Add the Dart method** — in `lib/platform/native_bridge.dart`, inside `NativeBridge`:

```dart
  Future<void> installApk(String path) async {
    await _channel.invokeMethod('installApk', {'path': path});
  }
```

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: Android manifest** — in `android/app/src/main/AndroidManifest.xml`, add under `<manifest>` (with the INTERNET line):
```xml
    <uses-permission android:name="android.permission.REQUEST_INSTALL_PACKAGES"/>
```
and inside `<application>` add a FileProvider:
```xml
        <provider
            android:name="androidx.core.content.FileProvider"
            android:authorities="${applicationId}.fileprovider"
            android:exported="false"
            android:grantUriPermissions="true">
            <meta-data
                android:name="android.support.FILE_PROVIDER_PATHS"
                android:resource="@xml/file_paths"/>
        </provider>
```

- [ ] **Step 6: FileProvider paths** — create `android/app/src/main/res/xml/file_paths.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<paths>
    <cache-path name="cache" path="."/>
    <files-path name="files" path="."/>
    <external-files-path name="ext" path="."/>
</paths>
```

- [ ] **Step 7: Kotlin handler** — in `MainActivity.kt`, add an `installApk` branch (keep the existing `deviceInfo`). Replace the `when` block with:
```kotlin
                when (call.method) {
                    "deviceInfo" -> result.success(
                        mapOf("model" to Build.MODEL, "androidSdk" to Build.VERSION.SDK_INT)
                    )
                    "installApk" -> {
                        val path = call.argument<String>("path")
                        if (path == null) { result.error("ARG", "path required", null); return@setMethodCallHandler }
                        val file = java.io.File(path)
                        val uri = androidx.core.content.FileProvider.getUriForFile(
                            this, "$packageName.fileprovider", file)
                        val intent = android.content.Intent(android.content.Intent.ACTION_VIEW).apply {
                            setDataAndType(uri, "application/vnd.android.package-archive")
                            addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
                        }
                        startActivity(intent)
                        result.success(true)
                    }
                    else -> result.notImplemented()
                }
```
> `androidx.core:core` (which provides FileProvider) ships with the Flutter Android embedding, so no extra Gradle dep is normally needed. If a release build later reports FileProvider unresolved, add `implementation("androidx.core:core-ktx:1.13.1")` to `android/app/build.gradle`. Do NOT run a Gradle build now.

- [ ] **Step 8: Verify (Dart) + commit**

Run: `flutter test` → all green (the Dart `installApk` test + prior suite). `flutter analyze` → no issues.
```bash
git add lib/platform/native_bridge.dart android/app/src/main/AndroidManifest.xml android/app/src/main/res/xml/file_paths.xml android/app/src/main/kotlin/com/gamecove/gamecove_companion/MainActivity.kt test/platform/native_bridge_install_test.dart
git commit -m "feat: native installApk via PackageInstaller + FileProvider"
```

---

## Task 7: Updates screen

**Files:**
- Create: `lib/ui/updates_screen.dart`
- Test: `test/ui/updates_screen_test.dart`

- [ ] **Step 1: Write the failing test** — `test/ui/updates_screen_test.dart`:

```dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/models/app_release.dart';
import 'package:gamecove_companion/services/update_service.dart';
import 'package:gamecove_companion/ui/updates_screen.dart';

class _FakeUpdateService extends UpdateService {
  final AppRelease? result;
  _FakeUpdateService(this.result) : super('https://test/');
  @override
  Future<AppRelease?> check(int currentVersionCode) async => result;
}

void main() {
  testWidgets('shows "up to date" when check returns null', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: UpdatesScreen(
        service: _FakeUpdateService(null),
        currentVersionCode: 5,
        onInstall: (_) async {},
      ),
    ));
    await tester.tap(find.text('Check for updates'));
    await tester.pumpAndSettle();
    expect(find.textContaining('up to date'), findsOneWidget);
  });

  testWidgets('shows an Install button and fires onInstall when newer', (tester) async {
    var installed = false;
    final rel = AppRelease(
        versionCode: 8, versionName: '1.8.0', url: 'u', sha256: 's', notes: 'New stuff');
    await tester.pumpWidget(MaterialApp(
      home: UpdatesScreen(
        service: _FakeUpdateService(rel),
        currentVersionCode: 5,
        onInstall: (_) async => installed = true,
      ),
    ));
    await tester.tap(find.text('Check for updates'));
    await tester.pumpAndSettle();
    expect(find.textContaining('1.8.0'), findsOneWidget);
    expect(find.textContaining('New stuff'), findsOneWidget);

    await tester.tap(find.text('Install'));
    await tester.pumpAndSettle();
    expect(installed, true);
  });
}
```

- [ ] **Step 2: Run → FAIL** (`updates_screen.dart` not found).

- [ ] **Step 3: Implement** — `lib/ui/updates_screen.dart`:

```dart
import 'package:flutter/material.dart';
import '../models/app_release.dart';
import '../services/update_service.dart';

class UpdatesScreen extends StatefulWidget {
  final UpdateService service;
  final int currentVersionCode;
  final Future<void> Function(AppRelease) onInstall;
  const UpdatesScreen({
    super.key,
    required this.service,
    required this.currentVersionCode,
    required this.onInstall,
  });

  @override
  State<UpdatesScreen> createState() => _UpdatesScreenState();
}

class _UpdatesScreenState extends State<UpdatesScreen> {
  bool _checking = false;
  bool _checked = false;
  AppRelease? _available;

  Future<void> _check() async {
    setState(() => _checking = true);
    final r = await widget.service.check(widget.currentVersionCode);
    setState(() {
      _checking = false;
      _checked = true;
      _available = r;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Updates')),
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            if (_checking) const CircularProgressIndicator(),
            if (!_checking && _checked && _available == null)
              const Text('You are up to date.'),
            if (!_checking && _available != null) ...[
              Text('Update available: v${_available!.versionName}'),
              if (_available!.notes.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.all(12),
                  child: Text(_available!.notes, textAlign: TextAlign.center),
                ),
              FilledButton(
                autofocus: true,
                onPressed: () => widget.onInstall(_available!),
                child: const Text('Install'),
              ),
            ],
            const SizedBox(height: 24),
            OutlinedButton(
              autofocus: !_checked,
              onPressed: _checking ? null : _check,
              child: const Text('Check for updates'),
            ),
          ],
        ),
      ),
    );
  }
}
```

- [ ] **Step 4: Run → PASS** (2 tests).

- [ ] **Step 5: Commit**
```bash
git add lib/ui/updates_screen.dart test/ui/updates_screen_test.dart
git commit -m "feat: Updates screen (check + install)"
```

---

## Task 8: Wire Updates in + launch check + CI/release artifacts

**Files:**
- Modify: `lib/ui/home_screen.dart`, `lib/main.dart`
- Create: `.github/workflows/release.yml`, `docs/RELEASE.md`, `app-latest.sample.json`
- Test: (covered by existing home_screen_test; add an Updates-tile assertion)

- [ ] **Step 1: Add an Updates entry to Home** — in `lib/ui/home_screen.dart`, add the imports:
```dart
import '../services/update_service.dart';
import '../services/app_version.dart';
import '../platform/native_bridge.dart';
import 'updates_screen.dart';
import '../config.dart';
```
Add a trailing tile after the QR loop's closing `,` (inside the `children:` list, after the `for (final q in qrs) ...` entry):
```dart
          FocusTile(
            label: 'Updates',
            icon: Icons.system_update,
            onActivate: () => Navigator.of(context).push(MaterialPageRoute(
              builder: (_) => UpdatesScreen(
                service: UpdateService(updateBaseUrl),
                // versionCode is read lazily inside onInstall path; for display
                // use 0 until known (Check still works against the manifest).
                currentVersionCode: 0,
                onInstall: (release) async {
                  final dir = Directory.systemTemp.path;
                  final path = await UpdateService(updateBaseUrl)
                      .downloadApk(release, toDir: dir);
                  await NativeBridge().installApk(path);
                },
              ),
            )),
          ),
```
Add `import 'dart:io';` at the top of `home_screen.dart` for `Directory`.
> Note: passing `currentVersionCode: 0` makes the manifest's release always count as "newer", so "Check" will always offer the latest. That's acceptable for v0.1 (the install is prompted and idempotent). A later refinement reads `AppVersion.current()` to suppress the prompt when already current — left out here to keep the screen synchronous and testable. Record this as a known simplification.

- [ ] **Step 2: Update the Home widget test** — in `test/ui/home_screen_test.dart`, after the existing expects, add:
```dart
    expect(find.text('Updates'), findsOneWidget);
```

- [ ] **Step 3: Run → PASS** (`flutter test test/ui/home_screen_test.dart`, then full suite).

- [ ] **Step 4: Launch-time check (throttled)** — in `lib/main.dart`, after the content refresh line, add a fire-and-forget update check that logs availability (no auto-install at launch):
```dart
  // ignore: discarded_futures
  UpdateService(updateBaseUrl).check(await AppVersion.current()).then((r) {
    if (r != null) debugPrint('App update available: v${r.versionName}');
  });
```
Add imports to `main.dart`: `import 'services/update_service.dart';` and `import 'services/app_version.dart';` and ensure `import 'package:flutter/foundation.dart';` for `debugPrint` (or use the one from material). 
> v0.1 only surfaces the update in the Updates screen; the launch check just logs. A launch-time badge is a later refinement.

- [ ] **Step 5: CI workflow** — create `.github/workflows/release.yml`:
```yaml
name: release
on:
  push:
    tags: ['v*']
permissions:
  contents: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: subosito/flutter-action@v2
        with: { channel: stable }
      - run: flutter pub get
      - run: flutter test
      - run: flutter build apk --release
      - name: Compute sha256 + manifest
        run: |
          APK=build/app/outputs/flutter-apk/app-release.apk
          SHA=$(sha256sum "$APK" | cut -d' ' -f1)
          CODE=$(echo "${GITHUB_REF_NAME}" | sed 's/^v//' | awk -F. '{print $1*10000+$2*100+$3}')
          cat > latest.json <<EOF
          { "versionCode": ${CODE}, "versionName": "${GITHUB_REF_NAME#v}",
            "url": "https://github.com/${GITHUB_REPOSITORY}/releases/download/${GITHUB_REF_NAME}/app-release.apk",
            "sha256": "${SHA}", "minSupported": 0, "notes": "Release ${GITHUB_REF_NAME}" }
          EOF
      - name: Publish release + manifest
        uses: softprops/action-gh-release@v2
        with:
          files: |
            build/app/outputs/flutter-apk/app-release.apk
            latest.json
```
> The app fetches `app/latest.json` from `updateBaseUrl`. Owner choice when linking the remote: either publish `latest.json` to a Pages branch served at `updateBaseUrl/app/latest.json`, or point `updateBaseUrl` at the release-asset path. Documented in RELEASE.md.

- [ ] **Step 6: Sample manifest + docs** — create `app-latest.sample.json`:
```json
{ "versionCode": 10000, "versionName": "1.0.0", "url": "https://github.com/OWNER/REPO/releases/download/v1.0.0/app-release.apk", "sha256": "<filled-by-ci>", "minSupported": 0, "notes": "Initial release" }
```
Create `docs/RELEASE.md`:
```markdown
# Releasing GameCove Companion

## One-time: link the remote
1. Create a GitHub repo, then: `git remote add origin <url> && git push -u origin main`
2. Decide where `app/latest.json` is served and set `updateBaseUrl` in `lib/config.dart`
   to that base (trailing slash). Same for `contentBaseUrl` (the content repo).

## Cut a release
1. Bump `version:` in `pubspec.yaml` (e.g. `1.0.0+10000` — the `+N` is the versionCode).
2. `git tag v1.0.0 && git push origin v1.0.0`
3. The `release` workflow builds the APK, attaches it + `latest.json` to the GitHub Release.
4. Publish/point `app/latest.json` at `updateBaseUrl` so installed apps see the new version.

## versionCode convention
`major*10000 + minor*100 + patch` (e.g. 1.2.3 -> 10203). Forward-only — never reuse or lower.
```

- [ ] **Step 7: Verify + commit**

Run: `flutter test` → all green. `flutter analyze` → no issues.
```bash
git add -A
git commit -m "feat: wire Updates tile + launch check + release CI and docs"
```

---

## Done / Definition of Done
- `flutter test` and `flutter analyze` green; manifest parse, version compare, sha256 download, and screen states all unit/widget-tested.
- Home has an **Updates** tile → check → (up-to-date | available + notes → Install).
- Native `installApk` + FileProvider + `REQUEST_INSTALL_PACKAGES` in place (prompted install; verified on-device later).
- `release.yml` + `RELEASE.md` author the CI/release path; **owner links the remote and sets `updateBaseUrl` later** — nothing is pushed by this plan.

## Known simplifications (documented, not bugs)
- Updates screen uses `currentVersionCode: 0` for display so "Check" always offers the latest; a later pass reads `AppVersion.current()` to suppress when current.
- Launch check only logs availability; no badge/auto-install.
- Install is **prompted**; device-owner silent install is a later fast-follow.

## Follow-on
- **Plan 3 — Emulator channel:** `emulators/manifest.json` + `<queries>` + `installedPackages()`/version compare + `signerSha256` guard + multi-package update UI (reuses `UpdateService`'s download/verify + the native installer from this plan).
