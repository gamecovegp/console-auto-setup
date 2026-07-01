# Self-contained homescreen layout (icons resolve on any model)

**Date:** 2026-07-01
**Status:** Approved — ready for implementation plan
**Area:** `provision/root/lib-root.sh`, `provision/root/capture.sh`, `provision/root/restore.sh`, `cas/gui.py` (tip copy only), `tests/test_homescreen_apps.sh` (new)

## Problem

`@homescreen` captures the HOME launcher's private data — the favorites DB that encodes
the icon / folder / **tab** / dock arrangement (`capture.sh` §HOMESCREEN, `home_launcher`
in `lib-root.sh`). Restore re-applies that DB **last**, after every app in the payload is
installed, so components resolve (`restore.sh` step 8). That much already works.

But **an icon only resolves if its app is installed on the target unit.** The set of apps
the golden installs is the operator's *selective* capture pick (`pkglist.txt` =
manifest pkgs minus the HOME launcher), and the **game launcher was pulled out of the app
list entirely** in `23ba16d` (treated as "system firmware, a behavior not an app"). So any
home-screen icon whose app is **not** in that installed set silently drops on restore:

- the **game launcher** (its APK is never captured — `gl_capture` grabs only the DataStore
  emulator-picks config, not the installer);
- apps the operator left **APK-unticked** (config-only);
- **system apps that vary by device model** — present on the golden, absent on some target.

Concretely: the operator arranges the golden with the Retroid / game launcher placed in an
"Emulators" tab, Saves, then Downloads to a fresh unit of a **different model** — and the
Emulators-tab spot comes back empty because the launcher app isn't on that unit. The saved
layout is **not self-contained**.

## Goal

When `@homescreen` is on, the golden is **self-contained**: every app the layout references
has its installer bundled, so on **any** target model, apps that are absent get installed
and **every icon resolves in its saved tab / folder**. The game launcher is the motivating
case; the fix is general (any placed app).

## Decisions (settled with the user)

| Question | Decision |
|---|---|
| Scope | **Any placed app** — a self-contained layout, not a launcher-only special case. |
| Is the launcher preinstalled firmware? | **Varies by device model** — CAS cannot assume it (or any placed app) is present → the mechanism must be *install-if-absent*. |
| Which referenced apps get an installer bundled? | **Any app with a resolvable APK** (3rd-party **and** system, whatever `pm path` returns). Platform-signed system apps that refuse a foreign signing key on restore are **warned, not failed**. |
| Approach | **A — payload-derived, install-if-absent.** Derive the referenced-package set from the launcher data by plaintext token scan; bundle missing APKs; install the absent ones before re-applying the layout. |
| Failure contract | **Additive end-to-end**, matching `@homescreen` today: capture problems WARN (never bump `CFAIL`); restore problems WARN (never bump `FAIL`). A clone with a partly-resolved layout is still functionally clean. |
| New GUI toggle? | **No.** Self-containment is an implementation detail of `@homescreen`. Only the tip copy is updated. |

### Approaches considered

- **A — payload-derived, install-if-absent (chosen).** Launcher-agnostic, additive, reuses
  the existing capture + staged-install idioms, no new toggle, solves the general goal, and
  as a bonus unblocks `@gamelauncher` restore on models that shipped without the frontend.
- **B — bundle every installed APK when `@homescreen` is on.** No parsing, but bloats
  goldens with apps that aren't on the home screen and still misses model-specific system
  apps. Rejected.
- **C — parse the favorites DB with `sqlite3`.** Cleaner in theory, but `sqlite3` is not
  guaranteed in the su / toybox context and the schema differs per launcher. The plaintext
  token grep in A is strictly more robust for this purpose. Rejected.

## Architecture

Three thin additions plus one refactor, all gated by the existing `@homescreen` flag:

```
capture.sh  @homescreen block, after launcher_data.tar succeeds:
    for pkg in $(homescreen_apps <launcher_data_dir>):
        skip if pkg == launcher, or $P/<pkg>/apk already captured by the app loop
        pm path <pkg> -> copy base+splits into  $P/homescreen/apps/<pkg>/
    (additive: never bump CFAIL; log count + total size)

lib-root.sh  new helper:
    homescreen_apps <dir>  -> deduped referenced package set
        grep -aoE 'component=<pkg>/'  +  'package=<pkg>'  over the launcher data
        strip prefixes, sort -u, drop the launcher pkg + any pkg with no APK path

restore.sh  step 8, BEFORE extracting launcher_data.tar:
    for d in $P/homescreen/apps/*:
        pkg=basename; pm path <pkg> ok -> already present, skip
        else install_apks "$d" "<pkg>"   (additive: WARN on failure, never bump FAIL)
    then the existing favorites-DB extract  ->  icons resolve

restore.sh  refactor:
    install_apks <apk_source_dir> <pkg_label>   # the proven staged split-install block
    reused by step 1 (app install) and step 8 (homescreen install-if-absent)
```

### `homescreen_apps <launcher_data_dir>` (new, `lib-root.sh`)

- **Input:** the launcher data directory, passed **as an argument** (on the golden this is
  `/data/data/<launcher_pkg>`; in tests a scratch fixture dir) — so it is testable
  off-device with no `DATA_ROOT` needed. `pm path` (for the APK-exists filter) is stubbed in
  tests, matching the `test_game_launcher.sh` pattern.
- **Extraction:** intent strings in the Launcher3-family favorites DB (and most OEM
  launchers) are stored as **plaintext** inside the SQLite / XML files. Scan for both
  `component=<pkg>/<cls>` and `package=<pkg>` tokens:
  `grep -aohE 'component=[a-zA-Z0-9._]+/'` → strip `component=` and the trailing `/…`;
  `grep -aohE 'package=[a-zA-Z0-9._]+'` → strip `package=`. Union, then `sort -u`.
  (`-a` so a mostly-binary DB is still scanned as text; `-h` no filename prefix.)
- **Filter:** drop the launcher's own package; drop any candidate for which `pm path`
  returns nothing (framework components with no installable APK, e.g. bare `android`).
- **Degrade:** a launcher that stores intents in a compressed / non-text blob yields no
  tokens → empty result, `rc 0` → capture bundles nothing extra (today's behavior). Log a
  one-line note when the launcher data existed but no referenced apps were derived.

### Capture glue (`capture.sh`)

- Runs **only** inside the existing `@homescreen` block, and **only after**
  `launcher_data.tar` is confirmed non-corrupt (no point bundling apps for a layout that
  will not be captured).
- `HS_APPS="$HS/apps"`. For each `pkg` from `homescreen_apps "/data/data/$LP"`:
  - skip if `pkg = "$LP"`;
  - skip if `[ -d "$P/$pkg/apk" ]` — already bundled by the per-app loop (§app loop copies
    base+splits there when the `apk` axis is on); no duplication;
  - `mkdir -p "$HS_APPS/$pkg"`; for `ap in $(pm path "$pkg" | sed 's/^package://')` copy to
    `"$HS_APPS/$pkg/"`.
- Log: `homescreen: bundled N placed-app installer(s) (<size>) so icons resolve on any model`.
- **Additive:** any copy miss is a WARN; it never bumps `CFAIL`. The final `chmod -R a+rX`
  on `$P` (already present) covers the new `homescreen/apps/` tree for the non-root pull.

### Restore glue (`restore.sh`)

- **Refactor first:** extract the current inline staged split-install (`restore.sh:68–80`,
  incl. the proven gotchas — stage to `/data/local/tmp/_inst`, single vs
  `install-create`/`install-write`/`install-commit`) into:
  `install_apks <apk_source_dir> <pkg_label>` returning non-zero on failure. Step 1 calls it
  as today (its FAIL accounting stays in the caller).
- **New, step 8, before the favorites-DB extract** (so components exist when the DB loads):
  `for d in "$P"/homescreen/apps/*/`: `pkg=$(basename "$d")`; if `pm path "$pkg"` succeeds →
  already present, skip; else `install_apks "$d" "$pkg"` — on failure **WARN**
  (`homescreen: could not install <pkg> — its icon may not resolve (platform-signed system
  app on a foreign key?)`), **never bump `FAIL`**. This block is inside the existing
  `@homescreen`-on / same-family-guard scope; the guard on the favorites-DB extract is
  unchanged.
- **Ordering / synergy:** because absent placed apps now install here (step 8, before the
  game-launcher-config step at `restore.sh:349`), `gl_restore` — which currently skips when
  the frontend is not installed — will now find the frontend present on models that shipped
  without it.

### GUI (`cas/gui.py`)

- **No new control.** Update the `@homescreen` tip in **both** modal paths (Save tip at the
  `tips` dict, ~`gui.py:1360`; Download `_DL_FLAG_TIPS["homescreen"]`, ~`gui.py:103`) to note
  that it *"bundles the installers for the apps you placed, so every icon resolves on any
  unit model."* Copy-only.

## Error handling

- **Additive throughout** — consistent with `@homescreen` being additive today. Capture:
  WARN, never `CFAIL`. Restore: WARN, never `FAIL`. A clone whose layout only partly
  resolved is still functionally clean.
- **Dedup** — never bundle an APK the per-app loop already captured (`$P/<pkg>/apk`).
- **APK-less framework components** — filtered out at capture (`pm path` empty).
- **Platform-signed system apps** — may refuse to install on a target with a different
  platform key; that surfaces as the step-8 WARN above, not a failure.
- **Same-family guard** on the favorites-DB extract is unchanged (a different-launcher unit
  still skips the layout entirely).

## Testing

- **New `tests/test_homescreen_apps.sh`** (mirrors `tests/test_game_launcher.sh`: source
  `lib-root.sh`, stub `pm`, scratch fixtures):
  - a fixture launcher-data dir containing a fake favorites DB with
    `component=com.foo/.Bar`, `package=com.bar`, the launcher's own pkg, and a
    framework token → assert `homescreen_apps` returns exactly `{com.foo, com.bar}`
    (launcher excluded; framework pkg excluded because its stubbed `pm path` is empty);
  - **dedup** — the same pkg appearing in multiple tokens returns once;
  - **binary / no-token blob** → empty result, `rc 0`;
  - **install-if-absent** stub cases: with `pm path` reporting a pkg present, no install
    fires; absent → `install_apks` is invoked exactly for the absent pkg(s).
- **Python suite** unaffected apart from any assertion on the `@homescreen` tip text (update
  to match the new copy). Behavior tests in `tests/test_cas.py` do not change.

## Non-goals

- No new GUI toggle; no per-app UI for the bundled installers.
- No attempt to reinstall or swap the **HOME launcher itself** (the same-family guard stays;
  a foreign launcher unit still skips the layout).
- Widget bindings remain **best-effort** (unchanged).
- No `sqlite3`-based DB parsing (see Approach C).
- `@gamelauncher` semantics are unchanged; it still captures only the DataStore picks. It
  merely *benefits* from the frontend now being installed by the homescreen path.
