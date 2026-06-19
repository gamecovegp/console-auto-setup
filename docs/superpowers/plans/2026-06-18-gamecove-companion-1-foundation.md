# GameCove Companion — Plan 1: Foundation & Content (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the GameCove companion app shell — a Flutter app with controller-navigable Home → Guide → QR screens, offline-first bundled content, and an over-the-air content refresh — that is a usable, shippable app on its own.

**Architecture:** One Flutter APK, three layers (Dart UI / Dart services / Kotlin MethodChannel bridge). Content is bundled in the APK as a seed, copied into a writable cache on first run, rendered from the cache (offline-first), and overlaid by an opportunistic remote refresh that only commits a complete, hash-validated set. The update *machinery* (compare-version → download → verify → atomic-commit) is introduced here for content; Plans 2 and 3 reuse it for the app APK and emulator APKs.

**Tech Stack:** Flutter (Dart), `flutter_markdown` (guide rendering), `qr_flutter` (QR codes), `path_provider` (cache dir), `crypto` (sha256), `http` (remote fetch), Kotlin (native bridge). Tests: `flutter_test`.

**Scope note:** This is Plan 1 of 3 for app v0.1 (see spec `docs/superpowers/specs/2026-06-18-gamecove-companion-app-v0.1-design.md`). The Updates screen, APK self-install, and emulator channel are **out of scope here** — they are Plans 2 and 3. This plan ends with a working app that shows guides/QRs offline and refreshes content over Wi-Fi.

**Project location:** `/home/ccvisionary/Documents/Work/[07] Projects/gamecove-companion/` (a new, standalone git repo — it gets its own GitHub repo for CI/releases in Plan 2).

> ⚠️ **Bracket-path caveat:** the parent path contains `[07]`. Gradle/NDK occasionally choke on `[`/`]` in an absolute build path. If the Android build fails in Task 0's `flutter run`, build from a bracket-free path (e.g. symlink `ln -s "[07] Projects/gamecove-companion" ~/gamecove-companion` and work from the symlink). The Dart/test tasks are unaffected.

---

## File Structure

```
gamecove-companion/
  pubspec.yaml                         # deps + asset registration
  lib/
    main.dart                          # entry: runApp(GameCoveApp())
    app.dart                           # GameCoveApp: MaterialApp, theme, routes
    models/
      content_models.dart             # GuideMeta, QrTarget, Promo, ContentIndex (+ fromJson)
    services/
      content_store.dart              # ContentStore interface + InMemoryContentStore + FileContentStore
      content_source.dart             # ContentSource interface + BundledContentSource + RemoteContentSource
      content_service.dart            # seed→cache init, offline reads, refresh() overlay
    platform/
      native_bridge.dart              # MethodChannel('gamecove/native'); DeviceInfo + deviceInfo()
    ui/
      home_screen.dart                # tile grid + promo banner + focus traversal
      guide_screen.dart               # markdown render by guide id (per-device variant)
      qr_screen.dart                  # QR + "Open link" by qr id
      widgets/
        focus_tile.dart               # controller/D-pad-navigable tile
  assets/
    content/
      index.json                      # {contentVersion, guides:[...]}
      qr.json                         # [{id,title,blurb,url}]
      promos.json                     # [{id,title,image,cta}]
      guides/
        start-here.md
        controls.md
        add-games.md
        ports.md
  android/app/src/main/kotlin/com/gamecove/gamecove_companion/MainActivity.kt   # 'gamecove/native' handler: deviceInfo
  test/
    models/content_models_test.dart
    services/content_store_test.dart
    services/content_service_test.dart
    platform/native_bridge_test.dart
    ui/home_screen_test.dart
    ui/guide_screen_test.dart
    ui/qr_screen_test.dart
```

**Responsibilities (one per file):**
- `content_models.dart` — pure data + JSON parsing. No I/O.
- `content_store.dart` — *where* content bytes live (cache). Swappable: in-memory for tests, files for the app.
- `content_source.dart` — *where content comes from* (bundled assets, remote HTTP). Read-only.
- `content_service.dart` — orchestration: seed the store, read for the UI, refresh from remote.
- `native_bridge.dart` — the only place that talks to Kotlin.
- `ui/*` — pure presentation; takes a `ContentService`.

---

## Task 0: Scaffold project, dependencies, git

**Files:**
- Create: the whole project at `/home/ccvisionary/Documents/Work/[07] Projects/gamecove-companion/`
- Modify: `pubspec.yaml`

- [ ] **Step 1: Create the Flutter project**

```bash
cd "/home/ccvisionary/Documents/Work/[07] Projects"
flutter create --project-name gamecove_companion --org com.gamecove --platforms android gamecove-companion
cd gamecove-companion
```

- [ ] **Step 2: Add dependencies**

Run:
```bash
flutter pub add flutter_markdown qr_flutter path_provider crypto http
flutter pub add --dev flutter_lints
```
Expected: `pubspec.yaml` gains those packages; `flutter pub get` succeeds.

- [ ] **Step 3: Register the content assets**

In `pubspec.yaml`, under `flutter:`, add:
```yaml
flutter:
  uses-material-design: true
  assets:
    - assets/content/
    - assets/content/guides/
```

- [ ] **Step 4: Initialise git**

```bash
git init
printf "build/\n.dart_tool/\n.idea/\n*.iml\n.flutter-plugins*\n" >> .gitignore
git add -A && git commit -m "chore: scaffold gamecove_companion Flutter project"
```

- [ ] **Step 5: Smoke-test the toolchain**

Run: `flutter test`
Expected: the default `widget_test.dart` passes (PASS). If Android tooling is set up, optionally `flutter run` on a device. Delete the default `test/widget_test.dart` (we replace it):
```bash
git rm test/widget_test.dart && git commit -m "chore: drop default widget test"
```

---

## Task 1: Content models + JSON parsing

**Files:**
- Create: `lib/models/content_models.dart`
- Test: `test/models/content_models_test.dart`

- [ ] **Step 1: Write the failing test**

```dart
// test/models/content_models_test.dart
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/models/content_models.dart';

void main() {
  test('ContentIndex parses contentVersion and guides in order', () {
    final json = {
      'contentVersion': 3,
      'guides': [
        {'id': 'start-here', 'title': 'Start Here', 'icon': 'rocket',
         'file': 'guides/start-here.md', 'hash': 'abc', 'order': 1},
      ],
    };
    final idx = ContentIndex.fromJson(json);
    expect(idx.contentVersion, 3);
    expect(idx.guides.single.id, 'start-here');
    expect(idx.guides.single.file, 'guides/start-here.md');
  });

  test('QrTarget parses id/title/blurb/url', () {
    final qr = QrTarget.fromJson(
        {'id': 'support', 'title': 'Support', 'blurb': 'Get help', 'url': 'https://x'});
    expect(qr.id, 'support');
    expect(qr.url, 'https://x');
  });

  test('Promo parses id/title/image/cta', () {
    final p = Promo.fromJson(
        {'id': 'sale', 'title': 'Sale', 'image': 'a.png', 'cta': 'https://y'});
    expect(p.cta, 'https://y');
  });
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `flutter test test/models/content_models_test.dart`
Expected: FAIL — `content_models.dart` / types not found.

- [ ] **Step 3: Write the implementation**

```dart
// lib/models/content_models.dart
class GuideMeta {
  final String id, title, icon, file, hash;
  final int order;
  const GuideMeta({
    required this.id, required this.title, required this.icon,
    required this.file, required this.hash, required this.order,
  });
  factory GuideMeta.fromJson(Map<String, dynamic> j) => GuideMeta(
        id: j['id'] as String,
        title: j['title'] as String,
        icon: j['icon'] as String,
        file: j['file'] as String,
        hash: j['hash'] as String,
        order: j['order'] as int,
      );
}

class QrTarget {
  final String id, title, blurb, url;
  const QrTarget({required this.id, required this.title, required this.blurb, required this.url});
  factory QrTarget.fromJson(Map<String, dynamic> j) => QrTarget(
        id: j['id'] as String,
        title: j['title'] as String,
        blurb: j['blurb'] as String,
        url: j['url'] as String,
      );
}

class Promo {
  final String id, title, image, cta;
  const Promo({required this.id, required this.title, required this.image, required this.cta});
  factory Promo.fromJson(Map<String, dynamic> j) => Promo(
        id: j['id'] as String,
        title: j['title'] as String,
        image: j['image'] as String,
        cta: j['cta'] as String,
      );
}

class ContentIndex {
  final int contentVersion;
  final List<GuideMeta> guides;
  const ContentIndex({required this.contentVersion, required this.guides});
  factory ContentIndex.fromJson(Map<String, dynamic> j) {
    final guides = (j['guides'] as List)
        .map((e) => GuideMeta.fromJson(e as Map<String, dynamic>))
        .toList()
      ..sort((a, b) => a.order.compareTo(b.order));
    return ContentIndex(contentVersion: j['contentVersion'] as int, guides: guides);
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `flutter test test/models/content_models_test.dart`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add lib/models/content_models.dart test/models/content_models_test.dart
git commit -m "feat: content models with JSON parsing"
```

---

## Task 2: ContentStore (cache abstraction) + in-memory impl

**Files:**
- Create: `lib/services/content_store.dart`
- Test: `test/services/content_store_test.dart`

- [ ] **Step 1: Write the failing test**

```dart
// test/services/content_store_test.dart
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/services/content_store.dart';

void main() {
  test('InMemoryContentStore starts empty, commits and reads back', () async {
    final store = InMemoryContentStore();
    expect(await store.isEmpty, true);

    await store.commit({'index.json': utf8.encode('{"contentVersion":1}')});
    expect(await store.isEmpty, false);
    expect(await store.exists('index.json'), true);
    expect(await store.readString('index.json'), '{"contentVersion":1}');
  });

  test('commit replaces the whole set atomically', () async {
    final store = InMemoryContentStore();
    await store.commit({'a.md': utf8.encode('v1'), 'b.md': utf8.encode('v1')});
    await store.commit({'a.md': utf8.encode('v2'), 'b.md': utf8.encode('v2')});
    expect(await store.readString('a.md'), 'v2');
    expect(await store.readString('b.md'), 'v2');
  });
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `flutter test test/services/content_store_test.dart`
Expected: FAIL — `content_store.dart` not found.

- [ ] **Step 3: Write the implementation**

```dart
// lib/services/content_store.dart
import 'dart:convert';
import 'dart:io';
import 'package:path_provider/path_provider.dart';

/// Where cached content bytes live. Swappable for tests.
abstract class ContentStore {
  Future<bool> get isEmpty;
  Future<bool> exists(String relPath);
  Future<String> readString(String relPath);
  Future<List<int>> readBytes(String relPath);

  /// Atomically replace [files] (relPath -> bytes) in the store.
  /// Callers pass a fully-downloaded, already-validated set.
  Future<void> commit(Map<String, List<int>> files);
}

class InMemoryContentStore implements ContentStore {
  final Map<String, List<int>> _files = {};
  @override
  Future<bool> get isEmpty async => _files.isEmpty;
  @override
  Future<bool> exists(String relPath) async => _files.containsKey(relPath);
  @override
  Future<List<int>> readBytes(String relPath) async => _files[relPath]!;
  @override
  Future<String> readString(String relPath) async => utf8.decode(_files[relPath]!);
  @override
  Future<void> commit(Map<String, List<int>> files) async => _files.addAll(files);
}

class FileContentStore implements ContentStore {
  final Directory dir;
  FileContentStore(this.dir);

  static Future<FileContentStore> open() async {
    final base = await getApplicationSupportDirectory();
    final dir = Directory('${base.path}/content');
    if (!await dir.exists()) await dir.create(recursive: true);
    return FileContentStore(dir);
  }

  File _file(String relPath) => File('${dir.path}/$relPath');

  @override
  Future<bool> get isEmpty async =>
      !await _file('index.json').exists();
  @override
  Future<bool> exists(String relPath) async => _file(relPath).exists();
  @override
  Future<List<int>> readBytes(String relPath) async => _file(relPath).readAsBytes();
  @override
  Future<String> readString(String relPath) async => _file(relPath).readAsString();

  @override
  Future<void> commit(Map<String, List<int>> files) async {
    // Write each to a temp sibling then rename — per-file atomic on the same FS.
    for (final entry in files.entries) {
      final dest = _file(entry.key);
      await dest.parent.create(recursive: true);
      final tmp = File('${dest.path}.tmp');
      await tmp.writeAsBytes(entry.value, flush: true);
      await tmp.rename(dest.path);
    }
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `flutter test test/services/content_store_test.dart`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add lib/services/content_store.dart test/services/content_store_test.dart
git commit -m "feat: ContentStore with in-memory and file-backed impls"
```

---

## Task 3: ContentSource (bundled + remote)

**Files:**
- Create: `lib/services/content_source.dart`
- Test: `test/services/content_source_test.dart`

- [ ] **Step 1: Write the failing test**

```dart
// test/services/content_source_test.dart
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/services/content_source.dart';

void main() {
  test('FakeContentSource returns seeded bytes', () async {
    final src = FakeContentSource({'index.json': utf8.encode('{"contentVersion":2}')});
    expect(await src.readString('index.json'), '{"contentVersion":2}');
    expect(await src.exists('index.json'), true);
    expect(await src.exists('missing'), false);
  });
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `flutter test test/services/content_source_test.dart`
Expected: FAIL — `content_source.dart` not found.

- [ ] **Step 3: Write the implementation**

```dart
// lib/services/content_source.dart
import 'dart:convert';
import 'package:flutter/services.dart' show rootBundle;
import 'package:http/http.dart' as http;

/// Read-only origin of content (bundled assets, remote feed, or a test fake).
abstract class ContentSource {
  Future<bool> exists(String relPath);
  Future<List<int>> readBytes(String relPath);
  Future<String> readString(String relPath) async => utf8.decode(await readBytes(relPath));
}

/// Seed content shipped inside the APK under assets/content/.
class BundledContentSource implements ContentSource {
  @override
  Future<bool> exists(String relPath) async {
    try {
      await rootBundle.load('assets/content/$relPath');
      return true;
    } catch (_) {
      return false;
    }
  }

  @override
  Future<List<int>> readBytes(String relPath) async {
    final data = await rootBundle.load('assets/content/$relPath');
    return data.buffer.asUint8List(data.offsetInBytes, data.lengthInBytes);
  }

  @override
  Future<String> readString(String relPath) async => utf8.decode(await readBytes(relPath));
}

/// Remote content feed (CDN base URL ending in '/').
class RemoteContentSource implements ContentSource {
  final String baseUrl;
  final http.Client client;
  RemoteContentSource(this.baseUrl, {http.Client? client}) : client = client ?? http.Client();

  Uri _uri(String relPath) => Uri.parse('$baseUrl$relPath');

  @override
  Future<bool> exists(String relPath) async {
    final r = await client.head(_uri(relPath));
    return r.statusCode == 200;
  }

  @override
  Future<List<int>> readBytes(String relPath) async {
    final r = await client.get(_uri(relPath));
    if (r.statusCode != 200) {
      throw Exception('GET ${_uri(relPath)} -> ${r.statusCode}');
    }
    return r.bodyBytes;
  }

  @override
  Future<String> readString(String relPath) async => utf8.decode(await readBytes(relPath));
}

/// Test double.
class FakeContentSource implements ContentSource {
  final Map<String, List<int>> files;
  FakeContentSource(this.files);
  @override
  Future<bool> exists(String relPath) async => files.containsKey(relPath);
  @override
  Future<List<int>> readBytes(String relPath) async {
    final b = files[relPath];
    if (b == null) throw Exception('missing $relPath');
    return b;
  }
  @override
  Future<String> readString(String relPath) async => utf8.decode(await readBytes(relPath));
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `flutter test test/services/content_source_test.dart`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/services/content_source.dart test/services/content_source_test.dart
git commit -m "feat: ContentSource (bundled, remote, fake)"
```

---

## Task 4: ContentService — seed init + reads

**Files:**
- Create: `lib/services/content_service.dart`
- Test: `test/services/content_service_test.dart`

- [ ] **Step 1: Write the failing test**

```dart
// test/services/content_service_test.dart
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/services/content_store.dart';
import 'package:gamecove_companion/services/content_source.dart';
import 'package:gamecove_companion/services/content_service.dart';

Map<String, List<int>> _seed() => {
      'index.json': utf8.encode(jsonEncode({
        'contentVersion': 1,
        'guides': [
          {'id': 'start-here', 'title': 'Start Here', 'icon': 'rocket',
           'file': 'guides/start-here.md', 'hash': 'h1', 'order': 1},
        ],
      })),
      'qr.json': utf8.encode(jsonEncode([
        {'id': 'support', 'title': 'Support', 'blurb': 'Help', 'url': 'https://s'},
      ])),
      'promos.json': utf8.encode(jsonEncode(const [])),
      'guides/start-here.md': utf8.encode('# Welcome'),
    };

void main() {
  test('init seeds an empty store from the bundled source', () async {
    final store = InMemoryContentStore();
    final svc = ContentService(store: store, bundled: FakeContentSource(_seed()));
    expect(await store.isEmpty, true);

    await svc.init();

    expect(await store.isEmpty, false);
    expect(svc.index.contentVersion, 1);
    expect(svc.index.guides.single.id, 'start-here');
    expect(svc.qrTargets.single.id, 'support');
    expect(await svc.guideMarkdown('start-here'), '# Welcome');
  });

  test('init does not overwrite a populated store', () async {
    final store = InMemoryContentStore();
    // Pre-populate as if a newer refresh already ran (contentVersion 5).
    final newer = Map<String, List<int>>.from(_seed());
    newer['index.json'] = utf8.encode(jsonEncode({'contentVersion': 5, 'guides': []}));
    await store.commit(newer);

    final svc = ContentService(store: store, bundled: FakeContentSource(_seed()));
    await svc.init();

    expect(svc.index.contentVersion, 5); // kept the cache, did not reseed to 1
  });
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `flutter test test/services/content_service_test.dart`
Expected: FAIL — `content_service.dart` not found.

- [ ] **Step 3: Write the implementation**

```dart
// lib/services/content_service.dart
import 'dart:convert';
import 'content_models.dart' show ContentIndex, GuideMeta, QrTarget, Promo;
import 'content_source.dart';
import 'content_store.dart';

/// The files that make up a content set.
const _kManifestFiles = ['index.json', 'qr.json', 'promos.json'];

class ContentService {
  final ContentStore store;
  final ContentSource bundled;
  ContentService({required this.store, required this.bundled});

  late ContentIndex _index;
  late List<QrTarget> _qr;
  late List<Promo> _promos;

  ContentIndex get index => _index;
  List<QrTarget> get qrTargets => _qr;
  List<Promo> get promos => _promos;

  /// Populate the cache from bundled seed on first run, then load into memory.
  Future<void> init() async {
    if (await store.isEmpty) {
      await _seedFromBundled();
    }
    await _loadFromStore();
  }

  Future<void> _seedFromBundled() async {
    final idxBytes = await bundled.readBytes('index.json');
    final idx = ContentIndex.fromJson(
        jsonDecode(utf8.decode(idxBytes)) as Map<String, dynamic>);
    final files = <String, List<int>>{};
    for (final f in _kManifestFiles) {
      files[f] = await bundled.readBytes(f);
    }
    for (final g in idx.guides) {
      files[g.file] = await bundled.readBytes(g.file);
    }
    await store.commit(files);
  }

  Future<void> _loadFromStore() async {
    _index = ContentIndex.fromJson(
        jsonDecode(await store.readString('index.json')) as Map<String, dynamic>);
    _qr = (jsonDecode(await store.readString('qr.json')) as List)
        .map((e) => QrTarget.fromJson(e as Map<String, dynamic>))
        .toList();
    _promos = (jsonDecode(await store.readString('promos.json')) as List)
        .map((e) => Promo.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Read a guide's markdown, preferring a device-specific variant when present
  /// (e.g. guides/controls.odin2.md over guides/controls.md).
  Future<String> guideMarkdown(String id, {String? model}) async {
    final g = _index.guides.firstWhere((g) => g.id == id);
    if (model != null) {
      final variant = g.file.replaceFirst(RegExp(r'\.md$'), '.$model.md');
      if (await store.exists(variant)) return store.readString(variant);
    }
    return store.readString(g.file);
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `flutter test test/services/content_service_test.dart`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add lib/services/content_service.dart test/services/content_service_test.dart
git commit -m "feat: ContentService seed init + offline reads"
```

---

## Task 5: ContentService.refresh — remote overlay, hash-validated, atomic

**Files:**
- Modify: `lib/services/content_service.dart`
- Test: `test/services/content_service_refresh_test.dart`

- [ ] **Step 1: Write the failing test**

```dart
// test/services/content_service_refresh_test.dart
import 'dart:convert';
import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/services/content_store.dart';
import 'package:gamecove_companion/services/content_source.dart';
import 'package:gamecove_companion/services/content_service.dart';

String _sha(String s) => sha256.convert(utf8.encode(s)).toString();

Map<String, List<int>> _set(int version, String body) => {
      'index.json': utf8.encode(jsonEncode({
        'contentVersion': version,
        'guides': [
          {'id': 'start-here', 'title': 'Start Here', 'icon': 'rocket',
           'file': 'guides/start-here.md', 'hash': _sha(body), 'order': 1},
        ],
      })),
      'qr.json': utf8.encode(jsonEncode(const [])),
      'promos.json': utf8.encode(jsonEncode(const [])),
      'guides/start-here.md': utf8.encode(body),
    };

void main() {
  late ContentService svc;
  late InMemoryContentStore store;

  setUp(() async {
    store = InMemoryContentStore();
    svc = ContentService(store: store, bundled: FakeContentSource(_set(1, '# v1')));
    await svc.init();
  });

  test('refresh applies a newer remote set', () async {
    final remote = FakeContentSource(_set(2, '# v2'));
    final updated = await svc.refresh(remote);
    expect(updated, true);
    expect(svc.index.contentVersion, 2);
    expect(await svc.guideMarkdown('start-here'), '# v2');
  });

  test('refresh ignores a non-newer remote set', () async {
    final remote = FakeContentSource(_set(1, '# also-v1'));
    final updated = await svc.refresh(remote);
    expect(updated, false);
    expect(svc.index.contentVersion, 1);
    expect(await svc.guideMarkdown('start-here'), '# v1'); // unchanged
  });

  test('refresh rejects a set whose guide hash mismatches (keeps last-good)', () async {
    final bad = _set(3, '# v3');
    bad['guides/start-here.md'] = utf8.encode('TAMPERED'); // body no longer matches hash
    final remote = FakeContentSource(bad);
    final updated = await svc.refresh(remote);
    expect(updated, false);
    expect(svc.index.contentVersion, 1); // rolled nothing in
    expect(await svc.guideMarkdown('start-here'), '# v1');
  });

  test('refresh is a no-op when the remote is unreachable', () async {
    final remote = FakeContentSource({}); // empty -> index fetch throws
    final updated = await svc.refresh(remote);
    expect(updated, false);
    expect(svc.index.contentVersion, 1);
  });
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `flutter test test/services/content_service_refresh_test.dart`
Expected: FAIL — `refresh` not defined.

- [ ] **Step 3: Add the `refresh` method**

Add these imports at the top of `lib/services/content_service.dart`:
```dart
import 'package:crypto/crypto.dart';
```
Add inside the `ContentService` class:
```dart
  /// Opportunistic remote refresh. Downloads + validates the FULL set before
  /// committing. Returns true if a newer, valid set was applied; false on
  /// not-newer, validation failure, or any network error (keeps last-good).
  Future<bool> refresh(ContentSource remote) async {
    try {
      final remoteIdxBytes = await remote.readBytes('index.json');
      final remoteIdx = ContentIndex.fromJson(
          jsonDecode(utf8.decode(remoteIdxBytes)) as Map<String, dynamic>);
      if (remoteIdx.contentVersion <= _index.contentVersion) return false;

      // Stage the whole set in memory.
      final staged = <String, List<int>>{'index.json': remoteIdxBytes};
      for (final f in ['qr.json', 'promos.json']) {
        staged[f] = await remote.readBytes(f);
      }
      for (final g in remoteIdx.guides) {
        final bytes = await remote.readBytes(g.file);
        final digest = sha256.convert(bytes).toString();
        if (digest != g.hash) return false; // corrupt/tampered -> abort, keep last-good
        staged[g.file] = bytes;
      }

      // Everything validated -> commit + reload.
      await store.commit(staged);
      await _loadFromStore();
      return true;
    } catch (_) {
      return false; // offline / partial / parse error -> no-op
    }
  }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `flutter test test/services/content_service_refresh_test.dart`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add lib/services/content_service.dart test/services/content_service_refresh_test.dart
git commit -m "feat: ContentService.refresh with hash-validated atomic overlay"
```

---

## Task 6: Native bridge — deviceInfo()

**Files:**
- Create: `lib/platform/native_bridge.dart`
- Modify: `android/app/src/main/kotlin/com/gamecove/gamecove_companion/MainActivity.kt`
- Test: `test/platform/native_bridge_test.dart`

- [ ] **Step 1: Write the failing test**

```dart
// test/platform/native_bridge_test.dart
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/platform/native_bridge.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('deviceInfo parses the native map', () async {
    const channel = MethodChannel('gamecove/native');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      if (call.method == 'deviceInfo') {
        return {'model': 'Odin2', 'androidSdk': 33};
      }
      return null;
    });

    final info = await NativeBridge().deviceInfo();
    expect(info.model, 'Odin2');
    expect(info.androidSdk, 33);
    expect(info.modelSlug, 'odin2'); // lowercased, used to pick guide variants
  });
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `flutter test test/platform/native_bridge_test.dart`
Expected: FAIL — `native_bridge.dart` not found.

- [ ] **Step 3: Write the Dart bridge**

```dart
// lib/platform/native_bridge.dart
import 'package:flutter/services.dart';

class DeviceInfo {
  final String model;
  final int androidSdk;
  const DeviceInfo({required this.model, required this.androidSdk});
  String get modelSlug => model.toLowerCase().replaceAll(RegExp(r'[^a-z0-9]+'), '');
}

class NativeBridge {
  static const _channel = MethodChannel('gamecove/native');

  Future<DeviceInfo> deviceInfo() async {
    final m = await _channel.invokeMapMethod<String, dynamic>('deviceInfo');
    return DeviceInfo(
      model: (m?['model'] as String?) ?? 'unknown',
      androidSdk: (m?['androidSdk'] as int?) ?? 0,
    );
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `flutter test test/platform/native_bridge_test.dart`
Expected: PASS.

- [ ] **Step 5: Implement the Kotlin handler**

Replace `android/app/src/main/kotlin/com/gamecove/gamecove_companion/MainActivity.kt` with:
```kotlin
package com.gamecove.gamecove_companion

import android.os.Build
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private val channel = "gamecove/native"

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "deviceInfo" -> result.success(
                        mapOf("model" to Build.MODEL, "androidSdk" to Build.VERSION.SDK_INT)
                    )
                    // tools/* reserved for Plan 2/3 (installApk, installedPackages, …)
                    else -> result.notImplemented()
                }
            }
    }
}
```
> Note the package path must match the `--org`/name from Task 0 (`com.gamecove` + `gamecove_companion` → `com/gamecove/gamecove_companion`). If `flutter create` produced a different folder, move the file to match and keep the `package` line in sync.

- [ ] **Step 6: Commit**

```bash
git add lib/platform/native_bridge.dart android/app/src/main/kotlin/com/gamecove/gamecove_companion/MainActivity.kt test/platform/native_bridge_test.dart
git commit -m "feat: native bridge deviceInfo() + Kotlin handler"
```

---

## Task 7: Focus tile widget (controller/D-pad navigable)

**Files:**
- Create: `lib/ui/widgets/focus_tile.dart`
- Test: `test/ui/focus_tile_test.dart`

- [ ] **Step 1: Write the failing test**

```dart
// test/ui/focus_tile_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/ui/widgets/focus_tile.dart';

void main() {
  testWidgets('FocusTile shows its label and fires onActivate on tap', (tester) async {
    var activated = false;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: FocusTile(
          label: 'Start Here',
          icon: Icons.rocket_launch,
          onActivate: () => activated = true,
        ),
      ),
    ));
    expect(find.text('Start Here'), findsOneWidget);
    await tester.tap(find.byType(FocusTile));
    expect(activated, true);
  });
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `flutter test test/ui/focus_tile_test.dart`
Expected: FAIL — `focus_tile.dart` not found.

- [ ] **Step 3: Write the implementation**

```dart
// lib/ui/widgets/focus_tile.dart
import 'package:flutter/material.dart';

/// A large, controller-navigable tile. Enter/Select or tap fires [onActivate];
/// it draws a visible focus ring so D-pad users can see where they are.
class FocusTile extends StatelessWidget {
  final String label;
  final IconData icon;
  final VoidCallback onActivate;
  final bool autofocus;
  const FocusTile({
    super.key,
    required this.label,
    required this.icon,
    required this.onActivate,
    this.autofocus = false,
  });

  @override
  Widget build(BuildContext context) {
    return InkWell(
      autofocus: autofocus,
      onTap: onActivate,
      borderRadius: BorderRadius.circular(16),
      focusColor: Theme.of(context).colorScheme.primary.withOpacity(0.25),
      child: Builder(builder: (context) {
        final focused = Focus.of(context).hasFocus;
        return Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(16),
            border: Border.all(
              color: focused
                  ? Theme.of(context).colorScheme.primary
                  : Colors.transparent,
              width: 3,
            ),
            color: Theme.of(context).colorScheme.surfaceContainerHighest,
          ),
          padding: const EdgeInsets.all(16),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(icon, size: 40),
              const SizedBox(height: 12),
              Text(label, textAlign: TextAlign.center,
                  style: Theme.of(context).textTheme.titleMedium),
            ],
          ),
        );
      }),
    );
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `flutter test test/ui/focus_tile_test.dart`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/ui/widgets/focus_tile.dart test/ui/focus_tile_test.dart
git commit -m "feat: controller-navigable FocusTile"
```

---

## Task 8: Guide screen (markdown by id)

**Files:**
- Create: `lib/ui/guide_screen.dart`
- Test: `test/ui/guide_screen_test.dart`

- [ ] **Step 1: Write the failing test**

```dart
// test/ui/guide_screen_test.dart
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/services/content_store.dart';
import 'package:gamecove_companion/services/content_source.dart';
import 'package:gamecove_companion/services/content_service.dart';
import 'package:gamecove_companion/ui/guide_screen.dart';

void main() {
  testWidgets('GuideScreen renders the guide markdown heading', (tester) async {
    final seed = {
      'index.json': utf8.encode(jsonEncode({
        'contentVersion': 1,
        'guides': [
          {'id': 'start-here', 'title': 'Start Here', 'icon': 'rocket',
           'file': 'guides/start-here.md', 'hash': 'h', 'order': 1},
        ],
      })),
      'qr.json': utf8.encode(jsonEncode(const [])),
      'promos.json': utf8.encode(jsonEncode(const [])),
      'guides/start-here.md': utf8.encode('# Welcome to GameCove'),
    };
    final svc = ContentService(
        store: InMemoryContentStore(), bundled: FakeContentSource(seed));
    await svc.init();

    await tester.pumpWidget(MaterialApp(
      home: GuideScreen(service: svc, guideId: 'start-here'),
    ));
    await tester.pumpAndSettle();

    expect(find.text('Start Here'), findsWidgets);          // app bar title
    expect(find.text('Welcome to GameCove'), findsOneWidget); // rendered H1
  });
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `flutter test test/ui/guide_screen_test.dart`
Expected: FAIL — `guide_screen.dart` not found.

- [ ] **Step 3: Write the implementation**

```dart
// lib/ui/guide_screen.dart
import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import '../services/content_service.dart';

class GuideScreen extends StatelessWidget {
  final ContentService service;
  final String guideId;
  final String? model;
  const GuideScreen({
    super.key,
    required this.service,
    required this.guideId,
    this.model,
  });

  @override
  Widget build(BuildContext context) {
    final meta = service.index.guides.firstWhere((g) => g.id == guideId);
    return Scaffold(
      appBar: AppBar(title: Text(meta.title)),
      body: FutureBuilder<String>(
        future: service.guideMarkdown(guideId, model: model),
        builder: (context, snap) {
          if (!snap.hasData) {
            return const Center(child: CircularProgressIndicator());
          }
          return Markdown(data: snap.data!);
        },
      ),
    );
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `flutter test test/ui/guide_screen_test.dart`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/ui/guide_screen.dart test/ui/guide_screen_test.dart
git commit -m "feat: GuideScreen renders markdown by id"
```

---

## Task 9: QR screen (QR + open link)

**Files:**
- Create: `lib/ui/qr_screen.dart`
- Test: `test/ui/qr_screen_test.dart`

- [ ] **Step 1: Write the failing test**

```dart
// test/ui/qr_screen_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qr_flutter/qr_flutter.dart';
import 'package:gamecove_companion/models/content_models.dart';
import 'package:gamecove_companion/ui/qr_screen.dart';

void main() {
  testWidgets('QrScreen shows blurb and a QR for the url', (tester) async {
    const qr = QrTarget(id: 'support', title: 'Support', blurb: 'Get help fast', url: 'https://gamecove.example/support');
    await tester.pumpWidget(const MaterialApp(home: QrScreen(target: qr)));

    expect(find.text('Get help fast'), findsOneWidget);
    final widget = tester.widget<QrImageView>(find.byType(QrImageView));
    expect(widget.data, 'https://gamecove.example/support');
  });
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `flutter test test/ui/qr_screen_test.dart`
Expected: FAIL — `qr_screen.dart` not found.

- [ ] **Step 3: Write the implementation**

```dart
// lib/ui/qr_screen.dart
import 'package:flutter/material.dart';
import 'package:qr_flutter/qr_flutter.dart';
import '../models/content_models.dart';

class QrScreen extends StatelessWidget {
  final QrTarget target;
  const QrScreen({super.key, required this.target});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(target.title)),
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 32),
              child: Text(target.blurb,
                  textAlign: TextAlign.center,
                  style: Theme.of(context).textTheme.titleMedium),
            ),
            const SizedBox(height: 24),
            Container(
              color: Colors.white,
              padding: const EdgeInsets.all(12),
              child: QrImageView(data: target.url, size: 220),
            ),
            const SizedBox(height: 16),
            SelectableText(target.url),
          ],
        ),
      ),
    );
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `flutter test test/ui/qr_screen_test.dart`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/ui/qr_screen.dart test/ui/qr_screen_test.dart
git commit -m "feat: QrScreen with QR image + link"
```

---

## Task 10: Home screen + app wiring + seed content

**Files:**
- Create: `lib/ui/home_screen.dart`, `lib/app.dart`, `lib/main.dart`
- Create: `assets/content/index.json`, `assets/content/qr.json`, `assets/content/promos.json`, `assets/content/guides/{start-here,controls,add-games,ports}.md`
- Test: `test/ui/home_screen_test.dart`

- [ ] **Step 1: Write the failing test**

```dart
// test/ui/home_screen_test.dart
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gamecove_companion/services/content_store.dart';
import 'package:gamecove_companion/services/content_source.dart';
import 'package:gamecove_companion/services/content_service.dart';
import 'package:gamecove_companion/ui/home_screen.dart';

void main() {
  testWidgets('HomeScreen shows a tile per guide and per QR target', (tester) async {
    final seed = {
      'index.json': utf8.encode(jsonEncode({
        'contentVersion': 1,
        'guides': [
          {'id': 'start-here', 'title': 'Start Here', 'icon': 'rocket', 'file': 'guides/start-here.md', 'hash': 'h', 'order': 1},
          {'id': 'controls', 'title': 'Controls', 'icon': 'gamepad', 'file': 'guides/controls.md', 'hash': 'h', 'order': 2},
        ],
      })),
      'qr.json': utf8.encode(jsonEncode([
        {'id': 'support', 'title': 'Support', 'blurb': 'b', 'url': 'https://s'},
      ])),
      'promos.json': utf8.encode(jsonEncode(const [])),
      'guides/start-here.md': utf8.encode('# A'),
      'guides/controls.md': utf8.encode('# B'),
    };
    final svc = ContentService(store: InMemoryContentStore(), bundled: FakeContentSource(seed));
    await svc.init();

    await tester.pumpWidget(MaterialApp(home: HomeScreen(service: svc)));
    await tester.pumpAndSettle();

    expect(find.text('Start Here'), findsOneWidget);
    expect(find.text('Controls'), findsOneWidget);
    expect(find.text('Support'), findsOneWidget);
  });
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `flutter test test/ui/home_screen_test.dart`
Expected: FAIL — `home_screen.dart` not found.

- [ ] **Step 3: Write the home screen**

```dart
// lib/ui/home_screen.dart
import 'package:flutter/material.dart';
import '../services/content_service.dart';
import 'guide_screen.dart';
import 'qr_screen.dart';
import 'widgets/focus_tile.dart';

class HomeScreen extends StatelessWidget {
  final ContentService service;
  final String? model;
  const HomeScreen({super.key, required this.service, this.model});

  IconData _icon(String name) {
    switch (name) {
      case 'rocket': return Icons.rocket_launch;
      case 'gamepad': return Icons.sports_esports;
      case 'download': return Icons.download;
      case 'extension': return Icons.extension;
      case 'support': return Icons.support_agent;
      case 'verified': return Icons.verified_user;
      case 'cart': return Icons.shopping_bag;
      default: return Icons.article;
    }
  }

  @override
  Widget build(BuildContext context) {
    final guides = service.index.guides;
    final qrs = service.qrTargets;
    return Scaffold(
      appBar: AppBar(title: const Text('GameCove')),
      body: GridView.count(
        crossAxisCount: 3,
        padding: const EdgeInsets.all(16),
        mainAxisSpacing: 16,
        crossAxisSpacing: 16,
        children: [
          for (var i = 0; i < guides.length; i++)
            FocusTile(
              label: guides[i].title,
              icon: _icon(guides[i].icon),
              autofocus: i == 0,
              onActivate: () => Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => GuideScreen(
                    service: service, guideId: guides[i].id, model: model),
              )),
            ),
          for (final q in qrs)
            FocusTile(
              label: q.title,
              icon: _icon('support'),
              onActivate: () => Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => QrScreen(target: q),
              )),
            ),
        ],
      ),
    );
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `flutter test test/ui/home_screen_test.dart`
Expected: PASS.

- [ ] **Step 5: Write the app entry + bootstrap**

```dart
// lib/app.dart
import 'package:flutter/material.dart';
import 'services/content_service.dart';
import 'ui/home_screen.dart';

class GameCoveApp extends StatelessWidget {
  final ContentService service;
  final String? model;
  const GameCoveApp({super.key, required this.service, this.model});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'GameCove',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF1FB6A6)),
        useMaterial3: true,
      ),
      home: HomeScreen(service: service, model: model),
    );
  }
}
```

```dart
// lib/main.dart
import 'package:flutter/material.dart';
import 'app.dart';
import 'platform/native_bridge.dart';
import 'services/content_service.dart';
import 'services/content_source.dart';
import 'services/content_store.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final store = await FileContentStore.open();
  final service = ContentService(store: store, bundled: BundledContentSource());
  await service.init();

  String? model;
  try {
    model = (await NativeBridge().deviceInfo()).modelSlug;
  } catch (_) {
    model = null; // non-Android / no native side -> generic content
  }

  // Opportunistic content refresh — fire-and-forget, never blocks startup.
  // Base URL is the CDN root for the content repo (trailing slash required).
  const contentBaseUrl = 'https://gamecove.github.io/companion-content/';
  // ignore: discarded_futures
  service.refresh(RemoteContentSource(contentBaseUrl));

  runApp(GameCoveApp(service: service, model: model));
}
```

- [ ] **Step 6: Author the seed content**

`assets/content/index.json`:
```json
{
  "contentVersion": 1,
  "guides": [
    {"id": "start-here", "title": "Start Here", "icon": "rocket", "file": "guides/start-here.md", "hash": "", "order": 1},
    {"id": "controls", "title": "Controls", "icon": "gamepad", "file": "guides/controls.md", "hash": "", "order": 2},
    {"id": "add-games", "title": "Add Games", "icon": "download", "file": "guides/add-games.md", "hash": "", "order": 3},
    {"id": "ports", "title": "Ports", "icon": "extension", "file": "guides/ports.md", "hash": "", "order": 4}
  ]
}
```
> `hash` may be empty in the **bundled seed** (seeding does not verify hashes — only remote `refresh()` does). Plan 2's CI fills real hashes for the remote feed.

`assets/content/qr.json`:
```json
[
  {"id": "support", "title": "Support", "blurb": "Get help with your device.", "url": "https://gamecove.example/support"},
  {"id": "warranty", "title": "Warranty", "blurb": "Register your warranty.", "url": "https://gamecove.example/warranty"},
  {"id": "accessories", "title": "Accessories", "blurb": "Cases, cards, and more.", "url": "https://gamecove.example/accessories"}
]
```
> URLs are placeholders — real targets are an open ops item from the spec (§10).

`assets/content/promos.json`:
```json
[]
```

`assets/content/guides/start-here.md` (and the other three — placeholder copy, real copy is an open content item):
```markdown
# Start Here

Welcome to your GameCove handheld. This guide covers powering on, charging,
and launching your first game.

> Final copy pending (spec §10).
```
Create `controls.md`, `add-games.md`, `ports.md` with a matching `# Title` + one-line placeholder each.

- [ ] **Step 7: Verify the whole suite + analyzer**

Run: `flutter test`
Expected: PASS (all tests across tasks 1–10).
Run: `flutter analyze`
Expected: no errors.

- [ ] **Step 8: Manual smoke (if a device/emulator is available)**

Run: `flutter run`
Verify: Home shows 4 guide tiles + 3 QR tiles; D-pad/arrow keys move the focus ring; selecting a guide renders its markdown; selecting a QR shows a scannable code; airplane-mode still works (renders from the seed).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: home screen, app bootstrap, and bundled seed content"
```

---

## Done / Definition of Done

- `flutter test` and `flutter analyze` are green.
- A fresh install shows guides + QRs **offline** from the bundled seed.
- Navigation works with a **controller/D-pad** (visible focus ring).
- `service.refresh(...)` applies a newer, hash-valid remote set and is a safe no-op otherwise.
- The native `deviceInfo()` bridge returns the model, and guides resolve a per-device variant when one exists.

## Follow-on plans (not in this plan)
- **Plan 2 — App self-update:** `UpdateService` + `app/latest.json` + native `installApk` (REQUEST_INSTALL_PACKAGES; device-owner silent fast-follow) + Updates screen + GitHub Actions release/CI that fills content hashes; plus the **promo banner** on Home (promos.json is already parsed in Plan 1, just not yet rendered).
- **Plan 3 — Emulator channel:** `emulators/manifest.json` + `<queries>` + `installedPackages()`/version compare + `signerSha256` guard + multi-package update UI.
