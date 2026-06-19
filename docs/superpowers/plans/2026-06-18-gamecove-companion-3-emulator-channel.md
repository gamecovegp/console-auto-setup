# GameCove Companion — Plan 3: Emulator Update Channel (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a **curated emulator update channel** — the app reads one `emulators/manifest.json` (GameCove's blessed set), compares each emulator's blessed versionCode to what's installed, and updates the ones that are behind (prompted), with a signing-key guard so an update can never wipe saves.

**Architecture:** Reuses Plan 2's download→sha256→install machinery applied to N packages. A native `installedPackages()` returns each emulator's installed versionCode + signer; `EmulatorUpdateService` fetches the manifest, classifies each emulator (update / up-to-date / not-installed / signer-mismatch / app-too-old), and the screen installs the available ones via the existing `installApk`. **One repo, one manifest, one release per emulator version** — updating one emulator never touches the others.

**Tech Stack:** Existing app (Flutter 3.44 / Dart 3.12) at `/home/ccvisionary/Documents/Work/[07] Projects/gamecove-companion/`. No new pub deps. Tests use `http/testing.dart` `MockClient` + injected lookups.

**Depends on:** Plan 2 (uses its `installApk` native method, the `updateBaseUrl` config, and the `REQUEST_INSTALL_PACKAGES`/FileProvider setup). **Build Plan 2 first.**

> ⚠️ Native package enumeration/signature reads and the actual installs **cannot be integration-tested here** (no Gradle/device). Dart logic (manifest parse, version compare, status classification, signer guard, screen states) is fully unit/widget-tested; the Kotlin + `<queries>` are authored + code-reviewed and verified on-device later.

---

## Repo layout (one repo hosts app + all emulators)

```
companion-releases/                      ← the SAME repo as Plan 2
  app/latest.json                        ← app self-update (Plan 2)
  emulators/manifest.json                ← ALL emulators — single control file (this plan)
  Releases (tag + APK asset, one per emulator VERSION you publish):
    retroarch-1.19.1   → retroarch.apk
    dolphin-2506       → dolphin.apk
    duckstation-…      → duckstation.apk
```
Updating RetroArch = cut a `retroarch-<ver>` release + edit RetroArch's one line in `emulators/manifest.json`. Dolphin etc. untouched. The app fetches **one** file (`emulators/manifest.json`), never the Releases API, so "latest" ambiguity and rate limits don't apply.

---

## File Structure (additions)

```
lib/
  models/emulator_release.dart          # EmulatorRelease + EmulatorManifest (+ fromJson)
  services/emulator_update_service.dart  # check() classification + downloadApk()
  platform/native_bridge.dart            # ADD InstalledApp + installedPackages(packages)
  ui/emulator_updates_screen.dart        # list available updates -> install (per-item + all)
  ui/home_screen.dart                    # ADD an "Emulator Updates" tile
android/app/src/main/
  AndroidManifest.xml                    # ADD <queries> for the emulator packages
  kotlin/.../MainActivity.kt             # ADD installedPackages handler (versionCode + signer)
docs/EMULATORS.md                        # how to bless/publish an emulator version
test/
  models/emulator_release_test.dart
  services/emulator_update_service_test.dart
  ui/emulator_updates_screen_test.dart
```

---

## Task 1: `EmulatorRelease` + `EmulatorManifest` models

**Files:**
- Create: `lib/models/emulator_release.dart`
- Test: `test/models/emulator_release_test.dart`

- [ ] **Step 1: Write the failing test** — `test/models/emulator_release_test.dart`:

```dart
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/models/emulator_release.dart';

void main() {
  test('EmulatorManifest parses a list of emulators', () {
    final m = EmulatorManifest.fromJson({
      'emulators': [
        {
          'id': 'retroarch', 'package': 'com.retroarch.aarch64',
          'blessedVersionCode': 119010, 'versionName': '1.19.1',
          'url': 'https://x/retroarch.apk', 'sha256': 'aa', 'signerSha256': 'bb',
          'minAppVersion': 0, 'mandatory': false, 'notes': 'n',
        },
      ],
    });
    expect(m.emulators.single.id, 'retroarch');
    expect(m.emulators.single.package, 'com.retroarch.aarch64');
    expect(m.emulators.single.blessedVersionCode, 119010);
    expect(m.emulators.single.signerSha256, 'bb');
  });

  test('EmulatorRelease defaults optional fields', () {
    final r = EmulatorRelease.fromJson({
      'id': 'dolphin', 'package': 'org.dolphinemu.dolphinemu',
      'blessedVersionCode': 2506, 'versionName': '2506',
      'url': 'u', 'sha256': 's', 'signerSha256': 'sig',
    });
    expect(r.minAppVersion, 0);
    expect(r.mandatory, false);
    expect(r.notes, '');
  });
}
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — `lib/models/emulator_release.dart`:

```dart
class EmulatorRelease {
  final String id, package, versionName, url, sha256, signerSha256, notes;
  final int blessedVersionCode, minAppVersion;
  final bool mandatory;
  const EmulatorRelease({
    required this.id,
    required this.package,
    required this.blessedVersionCode,
    required this.versionName,
    required this.url,
    required this.sha256,
    required this.signerSha256,
    this.minAppVersion = 0,
    this.mandatory = false,
    this.notes = '',
  });
  factory EmulatorRelease.fromJson(Map<String, dynamic> j) => EmulatorRelease(
        id: j['id'] as String,
        package: j['package'] as String,
        blessedVersionCode: j['blessedVersionCode'] as int,
        versionName: j['versionName'] as String,
        url: j['url'] as String,
        sha256: j['sha256'] as String,
        signerSha256: j['signerSha256'] as String,
        minAppVersion: (j['minAppVersion'] as int?) ?? 0,
        mandatory: (j['mandatory'] as bool?) ?? false,
        notes: (j['notes'] as String?) ?? '',
      );
}

class EmulatorManifest {
  final List<EmulatorRelease> emulators;
  const EmulatorManifest({required this.emulators});
  factory EmulatorManifest.fromJson(Map<String, dynamic> j) => EmulatorManifest(
        emulators: (j['emulators'] as List)
            .map((e) => EmulatorRelease.fromJson(e as Map<String, dynamic>))
            .toList(),
      );
}
```

- [ ] **Step 4: Run → PASS** (2 tests).
- [ ] **Step 5: Commit**
```bash
git add lib/models/emulator_release.dart test/models/emulator_release_test.dart
git commit -m "feat: EmulatorRelease + EmulatorManifest models"
```

---

## Task 2: Native `installedPackages()` (versionCode + signer)

**Files:**
- Modify: `lib/platform/native_bridge.dart`
- Modify: `android/app/src/main/AndroidManifest.xml` (add `<queries>`)
- Modify: `android/app/src/main/kotlin/com/gamecove/gamecove_companion/MainActivity.kt`
- Test: `test/platform/native_bridge_installed_test.dart`

- [ ] **Step 1: Write the failing test (Dart side)** — `test/platform/native_bridge_installed_test.dart`:

```dart
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/platform/native_bridge.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('installedPackages maps results, null for not-installed', () async {
    const channel = MethodChannel('gamecove/native');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      if (call.method == 'installedPackages') {
        return {
          'com.retroarch.aarch64': {'versionCode': 119010, 'signerSha256': 'bb'},
          // 'org.dolphinemu.dolphinemu' intentionally absent -> not installed
        };
      }
      return null;
    });

    final res = await NativeBridge()
        .installedPackages(['com.retroarch.aarch64', 'org.dolphinemu.dolphinemu']);
    expect(res['com.retroarch.aarch64']!.versionCode, 119010);
    expect(res['com.retroarch.aarch64']!.signerSha256, 'bb');
    expect(res['org.dolphinemu.dolphinemu'], isNull);
  });
}
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Add to `lib/platform/native_bridge.dart`** — add the class + method:

```dart
class InstalledApp {
  final int versionCode;
  final String signerSha256;
  const InstalledApp({required this.versionCode, required this.signerSha256});
}
```
and inside `NativeBridge`:
```dart
  /// Returns each requested package's installed versionCode + signer sha256,
  /// or null for packages that are not installed.
  Future<Map<String, InstalledApp?>> installedPackages(List<String> packages) async {
    final raw = await _channel.invokeMapMethod<String, dynamic>(
        'installedPackages', {'packages': packages});
    return {
      for (final p in packages) p: _parseInstalled(raw == null ? null : raw[p]),
    };
  }

  InstalledApp? _parseInstalled(dynamic v) {
    if (v == null) return null;
    final m = (v as Map).cast<String, dynamic>();
    return InstalledApp(
      versionCode: m['versionCode'] as int,
      signerSha256: m['signerSha256'] as String,
    );
  }
```

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: `<queries>` in the manifest** — Android 11+ hides other packages unless declared. In `android/app/src/main/AndroidManifest.xml`, add as a direct child of `<manifest>`:
```xml
    <queries>
        <package android:name="com.retroarch.aarch64"/>
        <package android:name="org.dolphinemu.dolphinemu"/>
        <package android:name="com.github.stenzek.duckstation"/>
        <package android:name="com.flycast.emulator"/>
        <package android:name="io.github.lime3ds.android"/>
        <package android:name="dev.eden.eden_emulator"/>
        <package android:name="me.magnum.melonds.nightly"/>
        <package android:name="xyz.aethersx2.tturnip"/>
        <package android:name="org.ppsspp.ppsspp"/>
        <package android:name="org.mupen64plusae.v3.fzurita"/>
        <package android:name="org.es_de.frontend"/>
    </queries>
```
> Package list mirrors `console-auto-setup/lib/emulators.txt`. Add variants there if you bless alternates.

- [ ] **Step 6: Kotlin handler** — add an `installedPackages` branch to the `when` in `MainActivity.kt` (keep `deviceInfo` + `installApk`):
```kotlin
                    "installedPackages" -> {
                        @Suppress("UNCHECKED_CAST")
                        val pkgs = call.argument<List<String>>("packages") ?: emptyList()
                        val out = HashMap<String, Any>()
                        val pm = packageManager
                        for (pkg in pkgs) {
                            try {
                                val info = pm.getPackageInfo(
                                    pkg, android.content.pm.PackageManager.GET_SIGNING_CERTIFICATES)
                                val code = androidx.core.content.pm.PackageInfoCompat.getLongVersionCode(info)
                                val sig = info.signingInfo?.apkContentsSigners?.firstOrNull()?.toByteArray()
                                val sha = if (sig != null)
                                    java.security.MessageDigest.getInstance("SHA-256").digest(sig)
                                        .joinToString("") { "%02x".format(it) }
                                else ""
                                out[pkg] = mapOf("versionCode" to code.toInt(), "signerSha256" to sha)
                            } catch (e: android.content.pm.PackageManager.NameNotFoundException) {
                                // not installed -> omit from map (Dart maps it to null)
                            }
                        }
                        result.success(out)
                    }
```
> Uses `androidx.core.content.pm.PackageInfoCompat` (ships with the Flutter embedding's androidx.core). If a release build reports it unresolved, add `implementation("androidx.core:core-ktx:1.13.1")` to `android/app/build.gradle`. Do NOT run a Gradle build now.

- [ ] **Step 7: Verify (Dart) + commit**

Run: `flutter test` (the new Dart test + full suite green); `flutter analyze` clean.
```bash
git add lib/platform/native_bridge.dart android/app/src/main/AndroidManifest.xml android/app/src/main/kotlin/com/gamecove/gamecove_companion/MainActivity.kt test/platform/native_bridge_installed_test.dart
git commit -m "feat: native installedPackages (versionCode + signer) + queries"
```

---

## Task 3: `EmulatorUpdateService` (classify + download)

**Files:**
- Create: `lib/services/emulator_update_service.dart`
- Test: `test/services/emulator_update_service_test.dart`

- [ ] **Step 1: Write the failing test** — `test/services/emulator_update_service_test.dart`:

```dart
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:http/http.dart' as http;
import 'package:gamecove_companion/platform/native_bridge.dart';
import 'package:gamecove_companion/services/emulator_update_service.dart';

String _manifest() => jsonEncode({
      'emulators': [
        {'id': 'retroarch', 'package': 'ra', 'blessedVersionCode': 120000, 'versionName': '1.20',
         'url': 'https://x/ra.apk', 'sha256': 'a', 'signerSha256': 'SIG', 'minAppVersion': 0},
        {'id': 'dolphin', 'package': 'dol', 'blessedVersionCode': 2506, 'versionName': '2506',
         'url': 'https://x/dol.apk', 'sha256': 'b', 'signerSha256': 'SIG', 'minAppVersion': 0},
        {'id': 'ppsspp', 'package': 'pp', 'blessedVersionCode': 100, 'versionName': '1.0',
         'url': 'https://x/pp.apk', 'sha256': 'c', 'signerSha256': 'SIG', 'minAppVersion': 999},
        {'id': 'flycast', 'package': 'fly', 'blessedVersionCode': 50, 'versionName': '5',
         'url': 'https://x/fly.apk', 'sha256': 'd', 'signerSha256': 'GOODSIG', 'minAppVersion': 0},
      ],
    });

void main() {
  EmulatorUpdateService build(Map<String, InstalledApp?> installed) {
    final client = MockClient((req) async => http.Response(_manifest(), 200));
    return EmulatorUpdateService('https://base/',
        client: client, installedLookup: (_) async => installed);
  }

  test('classifies each emulator correctly', () async {
    final svc = build({
      'ra': const InstalledApp(versionCode: 119000, signerSha256: 'SIG'), // behind -> update
      'dol': const InstalledApp(versionCode: 2506, signerSha256: 'SIG'),  // equal -> up to date
      'pp': const InstalledApp(versionCode: 1, signerSha256: 'SIG'),      // app too old (minAppVersion 999)
      'fly': const InstalledApp(versionCode: 10, signerSha256: 'SIG'),    // signer mismatch (GOODSIG != SIG)
    });
    final updates = await svc.check(1); // appVersionCode = 1
    EmulatorUpdate by(String id) => updates.firstWhere((u) => u.release.id == id);
    expect(by('retroarch').status, EmUpdateStatus.updateAvailable);
    expect(by('dolphin').status, EmUpdateStatus.upToDate);
    expect(by('ppsspp').status, EmUpdateStatus.appTooOld);
    expect(by('flycast').status, EmUpdateStatus.signerMismatch);
  });

  test('not-installed emulators are classified notInstalled (never auto-installed)', () async {
    final svc = build({'ra': null, 'dol': null, 'pp': null, 'fly': null});
    final updates = await svc.check(1000);
    expect(updates.every((u) => u.status == EmUpdateStatus.notInstalled), true);
  });

  test('check returns empty on a manifest fetch error', () async {
    final client = MockClient((req) async => http.Response('x', 500));
    final svc = EmulatorUpdateService('https://base/',
        client: client, installedLookup: (_) async => {});
    expect(await svc.check(1), isEmpty);
  });
}
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — `lib/services/emulator_update_service.dart`:

```dart
import 'dart:convert';
import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;
import '../models/emulator_release.dart';
import '../platform/native_bridge.dart';

enum EmUpdateStatus { updateAvailable, upToDate, notInstalled, signerMismatch, appTooOld }

class EmulatorUpdate {
  final EmulatorRelease release;
  final int? installedVersionCode;
  final EmUpdateStatus status;
  const EmulatorUpdate(
      {required this.release, required this.installedVersionCode, required this.status});
}

typedef InstalledLookup = Future<Map<String, InstalledApp?>> Function(List<String>);

class EmulatorUpdateService {
  final String baseUrl; // trailing slash
  final http.Client client;
  final InstalledLookup installedLookup;
  EmulatorUpdateService(this.baseUrl,
      {required this.installedLookup, http.Client? client})
      : client = client ?? http.Client();

  /// Fetch emulators/manifest.json, look up installed versions, and classify
  /// each blessed emulator. Returns [] on any fetch/parse error.
  Future<List<EmulatorUpdate>> check(int appVersionCode) async {
    try {
      final r = await client.get(Uri.parse('${baseUrl}emulators/manifest.json'));
      if (r.statusCode != 200) return [];
      final manifest =
          EmulatorManifest.fromJson(jsonDecode(r.body) as Map<String, dynamic>);
      final installed =
          await installedLookup(manifest.emulators.map((e) => e.package).toList());

      return manifest.emulators.map((rel) {
        final inst = installed[rel.package];
        final EmUpdateStatus status;
        if (inst == null) {
          status = EmUpdateStatus.notInstalled;
        } else if (appVersionCode < rel.minAppVersion) {
          status = EmUpdateStatus.appTooOld;
        } else if (inst.signerSha256 != rel.signerSha256) {
          status = EmUpdateStatus.signerMismatch;
        } else if (rel.blessedVersionCode > inst.versionCode) {
          status = EmUpdateStatus.updateAvailable;
        } else {
          status = EmUpdateStatus.upToDate;
        }
        return EmulatorUpdate(
            release: rel, installedVersionCode: inst?.versionCode, status: status);
      }).toList();
    } catch (_) {
      return [];
    }
  }

  /// Download the emulator APK to [toDir], verify sha256, return the file path.
  /// Throws on mismatch.
  Future<String> downloadApk(EmulatorRelease release, {required String toDir}) async {
    final r = await client.get(Uri.parse(release.url));
    if (r.statusCode != 200) throw Exception('download ${release.url} -> ${r.statusCode}');
    final digest = sha256.convert(r.bodyBytes).toString();
    if (digest != release.sha256) {
      throw Exception('sha256 mismatch for ${release.id}');
    }
    final path = '$toDir/${release.id}-${release.blessedVersionCode}.apk';
    await File(path).writeAsBytes(r.bodyBytes, flush: true);
    return path;
  }
}
```

- [ ] **Step 4: Run → PASS** (3 tests), then full suite.
- [ ] **Step 5: Commit**
```bash
git add lib/services/emulator_update_service.dart test/services/emulator_update_service_test.dart
git commit -m "feat: EmulatorUpdateService (classify + signer guard + download)"
```

---

## Task 4: Emulator updates screen

**Files:**
- Create: `lib/ui/emulator_updates_screen.dart`
- Test: `test/ui/emulator_updates_screen_test.dart`

- [ ] **Step 1: Write the failing test** — `test/ui/emulator_updates_screen_test.dart`:

```dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/models/emulator_release.dart';
import 'package:gamecove_companion/services/emulator_update_service.dart';
import 'package:gamecove_companion/ui/emulator_updates_screen.dart';

EmulatorRelease _rel(String id) => EmulatorRelease(
    id: id, package: id, blessedVersionCode: 2, versionName: '2',
    url: 'u', sha256: 's', signerSha256: 'sig');

class _FakeSvc extends EmulatorUpdateService {
  final List<EmulatorUpdate> result;
  _FakeSvc(this.result)
      : super('https://t/', installedLookup: (_) async => {});
  @override
  Future<List<EmulatorUpdate>> check(int appVersionCode) async => result;
}

void main() {
  testWidgets('lists only updatable emulators and fires onInstall', (tester) async {
    final installed = <String>[];
    final svc = _FakeSvc([
      EmulatorUpdate(release: _rel('retroarch'), installedVersionCode: 1, status: EmUpdateStatus.updateAvailable),
      EmulatorUpdate(release: _rel('dolphin'), installedVersionCode: 2, status: EmUpdateStatus.upToDate),
    ]);
    await tester.pumpWidget(MaterialApp(
      home: EmulatorUpdatesScreen(
        service: svc, appVersionCode: 5,
        onInstall: (r) async => installed.add(r.id),
      ),
    ));
    await tester.pumpAndSettle(); // check() runs in initState

    expect(find.text('retroarch'), findsOneWidget);
    expect(find.text('dolphin'), findsNothing); // up-to-date hidden

    await tester.tap(find.text('Update'));
    await tester.pumpAndSettle();
    expect(installed, ['retroarch']);
  });

  testWidgets('shows an all-current message when nothing is updatable', (tester) async {
    final svc = _FakeSvc([
      EmulatorUpdate(release: _rel('dolphin'), installedVersionCode: 2, status: EmUpdateStatus.upToDate),
    ]);
    await tester.pumpWidget(MaterialApp(
      home: EmulatorUpdatesScreen(service: svc, appVersionCode: 5, onInstall: (_) async {}),
    ));
    await tester.pumpAndSettle();
    expect(find.textContaining('up to date'), findsOneWidget);
  });
}
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — `lib/ui/emulator_updates_screen.dart`:

```dart
import 'package:flutter/material.dart';
import '../models/emulator_release.dart';
import '../services/emulator_update_service.dart';

class EmulatorUpdatesScreen extends StatefulWidget {
  final EmulatorUpdateService service;
  final int appVersionCode;
  final Future<void> Function(EmulatorRelease) onInstall;
  const EmulatorUpdatesScreen({
    super.key,
    required this.service,
    required this.appVersionCode,
    required this.onInstall,
  });

  @override
  State<EmulatorUpdatesScreen> createState() => _EmulatorUpdatesScreenState();
}

class _EmulatorUpdatesScreenState extends State<EmulatorUpdatesScreen> {
  bool _loading = true;
  List<EmulatorUpdate> _updates = const [];

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final all = await widget.service.check(widget.appVersionCode);
    setState(() {
      _loading = false;
      _updates = all.where((u) => u.status == EmUpdateStatus.updateAvailable).toList();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Emulator updates')),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _updates.isEmpty
              ? const Center(child: Text('All emulators are up to date.'))
              : ListView(
                  children: [
                    for (final u in _updates)
                      ListTile(
                        title: Text(u.release.id),
                        subtitle: Text('v${u.release.versionName}'
                            '${u.release.mandatory ? ' • required' : ''}'),
                        trailing: FilledButton(
                          onPressed: () => widget.onInstall(u.release),
                          child: const Text('Update'),
                        ),
                      ),
                  ],
                ),
    );
  }
}
```

- [ ] **Step 4: Run → PASS** (2 tests).
- [ ] **Step 5: Commit**
```bash
git add lib/ui/emulator_updates_screen.dart test/ui/emulator_updates_screen_test.dart
git commit -m "feat: emulator updates screen"
```

---

## Task 5: Wire into Home + docs

**Files:**
- Modify: `lib/ui/home_screen.dart`
- Create: `docs/EMULATORS.md`
- Test: extend `test/ui/home_screen_test.dart`

- [ ] **Step 1: Add an "Emulator Updates" tile** — in `lib/ui/home_screen.dart`, add imports:
```dart
import '../services/emulator_update_service.dart';
import '../services/app_version.dart';
import 'emulator_updates_screen.dart';
```
Add a tile after the "Updates" tile (from Plan 2), inside the `children:` list:
```dart
          FocusTile(
            label: 'Emulators',
            icon: Icons.videogame_asset,
            onActivate: () async {
              final appV = await AppVersion.current();
              if (!context.mounted) return;
              Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => EmulatorUpdatesScreen(
                  service: EmulatorUpdateService(
                    updateBaseUrl,
                    installedLookup: (pkgs) => NativeBridge().installedPackages(pkgs),
                  ),
                  appVersionCode: appV,
                  onInstall: (release) async {
                    final path = await EmulatorUpdateService(
                      updateBaseUrl,
                      installedLookup: (p) => NativeBridge().installedPackages(p),
                    ).downloadApk(release, toDir: (await getTemporaryDirectory()).path);
                    await NativeBridge().installApk(path);
                  },
                ),
              ));
            },
          ),
```
(`updateBaseUrl`, `NativeBridge`, and `getTemporaryDirectory` should already be imported from Plan 2's Home wiring — confirm `import '../config.dart';`, `import '../platform/native_bridge.dart';`, and `import 'package:path_provider/path_provider.dart';` are present; add any that are missing. Do NOT use `Directory.systemTemp` — it is outside the FileProvider roots and the install would throw.)

- [ ] **Step 2: Extend the Home test** — in `test/ui/home_screen_test.dart`, add:
```dart
    expect(find.text('Emulators'), findsOneWidget);
```

- [ ] **Step 3: Run → PASS** (Home test + full suite). `flutter analyze` clean.

- [ ] **Step 4: Docs** — create `docs/EMULATORS.md`:
```markdown
# Blessing & publishing emulator updates

The app reads ONE file — `emulators/manifest.json` in the releases repo — and updates
each installed emulator whose blessed versionCode is higher than what's on the device.
Publishing is the act of blessing: only add a manifest entry after the build passes on
the golden.

## To bless a new emulator version
1. Obtain the vetted APK from the emulator's single chosen source (same signer as the
   installed build — a different signer fails to update / would wipe saves).
2. Cut a GitHub Release tagged `<id>-<version>` (e.g. `retroarch-1.19.1`) with that APK.
3. Compute:
   - `sha256` = `sha256sum <apk>`
   - `signerSha256` = the APK signing cert SHA-256 (`apksigner verify --print-certs <apk>`
     → SHA-256 of the cert; must match the installed build's signer)
4. Edit that emulator's single entry in `emulators/manifest.json`:
   `blessedVersionCode`, `versionName`, `url` (the new release asset), `sha256`, `signerSha256`.
   Set `mandatory: true` only for critical fixes. Leave every other emulator untouched.

## Rules
- **Forward-only.** Android blocks downgrades; never lower a `blessedVersionCode`. To undo a
  bad build, publish a higher-versioned corrected APK.
- **One source/signer per emulator.** The app refuses a signer-mismatched update.
- **Cores are NOT shipped here** (RetroArch app-data is walled off) — leave cores to
  RetroArch's own updater / the golden.
- Package list lives in `AndroidManifest.xml` `<queries>` (mirror `console-auto-setup/lib/emulators.txt`).
```

- [ ] **Step 5: Commit**
```bash
git add lib/ui/home_screen.dart docs/EMULATORS.md test/ui/home_screen_test.dart
git commit -m "feat: wire Emulator Updates tile + blessing docs"
```

---

## Done / Definition of Done
- `flutter test` + `flutter analyze` green; manifest parse, status classification (update / up-to-date / not-installed / signer-mismatch / app-too-old), sha256 download, and screen states all unit/widget-tested.
- Home has an **Emulators** tile → lists only updatable emulators → prompted install (reuses Plan 2's `installApk`).
- Native `installedPackages()` returns versionCode + signer; `<queries>` declares the emulator packages.
- One repo, one `emulators/manifest.json`, one release per emulator version; updating one never touches another. `EMULATORS.md` documents blessing.

## Known simplifications (documented, not bugs)
- Not-installed emulators are surfaced as `notInstalled` and never auto-installed (installing a missing emulator is provisioning's job, not the update channel's).
- Signer mismatch and app-too-old are classified and hidden from the update list (not surfaced as warnings yet) — a later pass can show them as advisories.
- Install is **prompted**, one dialog per emulator; device-owner **silent** multi-install is the fast-follow that makes "Update all" seamless (set the app as device-owner at provisioning).
- "Update all" is per-item buttons in this version; a single batched control pairs naturally with the silent path.
