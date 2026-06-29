# Selective per-app capture + ship-clean Lock scrub — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make golden capture a per-app two-axis selection (APK / Config, incl. config-only and the default launcher), have Download deploy the captured set, and have Lock scrub usage traces + saved game states before un-rooting.

**Architecture:** The per-profile `manifest` gains optional per-app tokens (`apk`/`config`); `capture.sh` writes only the selected pieces so the payload's file presence encodes the choices; `restore.sh` deploys what's present (config-only = no-APK is not an error); a new `seal()` scrub step clears curated usage/save paths. Python (`profiles.py`/`gui.py`) carries the parser + UI + manifest writer; shell (`lib-root.sh`/`capture.sh`/`restore.sh`) carries the device behavior.

**Tech Stack:** Python 3 (`unittest`, mocked adb via `FakeRunner`), POSIX shell (`/system/bin/sh` on-device; bash-runnable helpers on the dev host), Android `sqlite3` (build-tools on host for tests).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-29-selective-capture-and-lock-scrub-design.md`.
- **Back-compatible:** a bare-line manifest (`com.foo`) and a capture with no `CAS_MANIFEST` MUST behave exactly as today — both axes captured for every app.
- Manifest tokens: `apk`, `config`, or both (whitespace-separated, order-insensitive). Bare line ⇒ both. A line's first field is always the package (`manifest_pkgs` unchanged).
- Default launcher: APK axis is **ignored** (system firmware); only its config axis is meaningful; its clone is **same-family only** (unit launcher pkg == golden's) and uses system-app ownership/relabel — reuse the existing `@homescreen` path.
- Scrub runs **while rooted, inside `seal()`, before the un-root flash**; every scrub step WARNs on failure and never blocks/fails a seal; never runs on the golden (existing `is_golden()` guard).
- Device-side shell behavior that can't run in CI carries a `[VERIFY on device]` note (repo convention); the Python suite (`tests/test_cas.py`) must stay green.
- Branch: current branch (`feat/companion-device-owner-lockdown`); plain `git commit` per task.

---

## Phase 1 — Selective per-app capture + deploy

### Task 1: `manifest_axes` parser (profiles.py)

**Files:**
- Modify: `cas/profiles.py` (add `manifest_axes` after `manifest_flags`, ~line 117)
- Test: `tests/test_cas.py`

**Interfaces:**
- Produces: `manifest_axes(manifest_path) -> dict[str, tuple[bool, bool]]` mapping `pkg -> (apk, config)`. Bare line ⇒ `(True, True)`. Token `apk` ⇒ `(True, False)`; `config` ⇒ `(False, True)`; both tokens ⇒ `(True, True)`. Consumed by Task 2 (`save_manifest` round-trip), Task 6 (GUI).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cas.py` (inside the module, a new `unittest.TestCase`):

```python
class TestManifestAxes(unittest.TestCase):
    def _write(self, text):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "manifest").write_text(text)
        return d / "manifest"

    def test_bare_line_is_both_axes(self):
        m = self._write("# h\ncom.foo\n")
        self.assertEqual(P.manifest_axes(m), {"com.foo": (True, True)})

    def test_apk_only_and_config_only(self):
        m = self._write("com.bar apk\nxyz.aethersx2.android config\n")
        ax = P.manifest_axes(m)
        self.assertEqual(ax["com.bar"], (True, False))
        self.assertEqual(ax["xyz.aethersx2.android"], (False, True))

    def test_both_tokens_order_insensitive_and_flags_ignored(self):
        m = self._write("com.baz config apk\n@settings on\n")
        self.assertEqual(P.manifest_axes(m), {"com.baz": (True, True)})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestManifestAxes -q`
Expected: FAIL — `AttributeError: module 'cas.profiles' has no attribute 'manifest_axes'`.

- [ ] **Step 3: Implement `manifest_axes`**

In `cas/profiles.py`, immediately after the `manifest_flags` function, add:

```python
def manifest_axes(manifest_path):
    """{pkg: (apk_bool, config_bool)} from manifest app lines. A bare line (no tokens)
    means BOTH axes (back-compat). Tokens 'apk' and/or 'config' narrow it."""
    p = pathlib.Path(manifest_path)
    axes = {}
    if not p.exists():
        return axes
    for line in _read_text(p).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("@"):
            continue
        parts = line.split()
        pkg, toks = parts[0], set(parts[1:])
        if toks:
            axes[pkg] = ("apk" in toks, "config" in toks)
        else:
            axes[pkg] = (True, True)
    return axes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestManifestAxes -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(profiles): manifest_axes — per-app APK/config token parser (bare=both)"
```

---

### Task 2: `save_manifest` writes axes tokens (profiles.py)

**Files:**
- Modify: `cas/profiles.py:120` (`save_manifest`)
- Test: `tests/test_cas.py`

**Interfaces:**
- Consumes: `manifest_axes` (Task 1).
- Produces: `save_manifest(manifest_path, pkgs, flags, header="# manifest", axes=None)`. When `axes` is a `dict[pkg, (apk, config)]`, each pkg line is written with tokens (`pkg` for both, `pkg apk`, `pkg config`). When `axes is None`, bare lines (today's behavior). Consumed by Task 6 (GUI).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cas.py`:

```python
class TestSaveManifestAxes(unittest.TestCase):
    def test_axes_roundtrip(self):
        d = pathlib.Path(tempfile.mkdtemp())
        m = d / "manifest"
        P.save_manifest(m, ["com.foo", "com.bar", "xyz.aethersx2.android"],
                        {"settings": "on"},
                        axes={"com.foo": (True, True), "com.bar": (True, False),
                              "xyz.aethersx2.android": (False, True)})
        self.assertEqual(P.manifest_axes(m),
                         {"com.foo": (True, True), "com.bar": (True, False),
                          "xyz.aethersx2.android": (False, True)})
        self.assertEqual(P.manifest_pkgs(m),
                         ["com.foo", "com.bar", "xyz.aethersx2.android"])

    def test_no_axes_writes_bare_lines(self):
        d = pathlib.Path(tempfile.mkdtemp())
        m = d / "manifest"
        P.save_manifest(m, ["com.foo"], {"settings": "on"})
        self.assertEqual(P.manifest_axes(m), {"com.foo": (True, True)})
        self.assertIn("\ncom.foo\n", "\n" + m.read_text())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestSaveManifestAxes -q`
Expected: FAIL — `save_manifest() got an unexpected keyword argument 'axes'`.

- [ ] **Step 3: Implement**

Replace `save_manifest` in `cas/profiles.py` with:

```python
def save_manifest(manifest_path, pkgs, flags, header="# manifest", axes=None):
    def _line(pkg):
        if not axes or pkg not in axes:
            return pkg                       # bare = both axes (back-compat)
        apk, cfg = axes[pkg]
        toks = ([] if (apk and cfg) else (["apk"] if apk else []) + (["config"] if cfg else []))
        return pkg if not toks else f"{pkg} {' '.join(toks)}"
    lines = [header]
    lines += [_line(p) for p in pkgs]
    lines += [f"@{k} {v}" for k, v in flags.items()]
    pathlib.Path(manifest_path).write_text("\n".join(lines) + "\n")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestSaveManifestAxes -q`
Expected: PASS (2 tests). Also run the whole suite — `python3 -m pytest tests/test_cas.py -q` — to confirm existing `save_manifest` callers (which pass no `axes`) still pass.

- [ ] **Step 5: Commit**

```bash
git add cas/profiles.py tests/test_cas.py
git commit -m "feat(profiles): save_manifest writes per-app axes tokens (axes=None keeps bare lines)"
```

---

### Task 3: `manifest_axes` shell helper (lib-root.sh)

**Files:**
- Modify: `provision/root/lib-root.sh` (add after `manifest_flag`, ~line 31)
- Test: `tests/test_manifest_axes.sh` (new)

**Interfaces:**
- Consumes: nothing.
- Produces: `manifest_axes <manifest> <pkg>` — echoes one of `apk config` (default/bare), `apk`, `config`, or empty (pkg absent). Consumed by Task 4 (`capture.sh`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_manifest_axes.sh`:

```bash
#!/usr/bin/env bash
# Local test for manifest_axes (pure text — no device). Run: bash tests/test_manifest_axes.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
printf '# h\ncom.foo\ncom.bar apk\nxyz.aethersx2.android config\ncom.baz config apk\n@settings on\n' > "$tmp/m"

check(){ got="$(manifest_axes "$tmp/m" "$1")"; [ "$got" = "$2" ] || { echo "FAIL $1: [$got] != [$2]"; fail=1; }; }
check com.foo "apk config"
check com.bar "apk"
check xyz.aethersx2.android "config"
check com.baz "apk config"
check com.absent ""
[ "$fail" -eq 0 ] && { echo "PASS: manifest_axes"; exit 0; } || exit 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "$(git rev-parse --show-toplevel)" && bash tests/test_manifest_axes.sh`
Expected: FAIL — `manifest_axes: command not found`.

- [ ] **Step 3: Implement**

In `provision/root/lib-root.sh`, immediately after the `manifest_flag()` line, add:

```sh
# manifest_axes <manifest> <pkg> — echoes the capture axes for a pkg: "apk config" (bare/default),
# "apk", "config", or empty if the pkg isn't listed. Tokens after the pkg name narrow it.
manifest_axes(){
  line="$(sed -e 's/#.*//' "$1" 2>/dev/null | grep -vE '^[[:space:]]*@' | awk -v p="$2" 'NF && $1==p {print; exit}')"
  [ -n "$line" ] || return 0
  rest="$(echo "$line" | cut -s -d' ' -f2-)"
  case "$rest" in
    "") echo "apk config" ;;                                  # bare = both
    *apk*config*|*config*apk*) echo "apk config" ;;
    *apk*) echo "apk" ;;
    *config*) echo "config" ;;
    *) echo "apk config" ;;                                   # unknown token -> both (back-compat)
  esac
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "$(git rev-parse --show-toplevel)" && bash tests/test_manifest_axes.sh`
Expected: `PASS: manifest_axes`.

- [ ] **Step 5: Commit**

```bash
git add provision/root/lib-root.sh tests/test_manifest_axes.sh
git commit -m "feat(root): manifest_axes shell helper — per-app APK/config axes (bare=both)"
```

---

### Task 4: `capture.sh` honors axes + records them in meta

**Files:**
- Modify: `provision/root/capture.sh` (per-app loop ~lines 36-62; meta write ~line 60)

**Interfaces:**
- Consumes: `manifest_axes` (Task 3). Reads `CAS_MANIFEST` (already used elsewhere; if unset, capture everything with both axes).

- [ ] **Step 1: Gate APK + config capture per axes, write `axes=` to meta**

In `capture.sh`, at the TOP of the `for pkg in $(cat "$P/pkglist.txt"); do` loop body (right after `log "capturing $pkg…"`), insert the axes resolution:

```sh
  # capture axes for THIS pkg: from the manifest if one was passed, else both (back-compat).
  AX="apk config"; [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ] && AX="$(manifest_axes "$CAS_MANIFEST" "$pkg")"
  case " $AX " in *" apk "*) CAP_APK=1;; *) CAP_APK=0;; esac
  case " $AX " in *" config "*) CAP_CFG=1;; *) CAP_CFG=0;; esac
```

Then guard the **APK copy** block (the `mkdir -p "$P/$pkg/apk"` + the `for ap in $(pm path ...)` copy) with `if [ "$CAP_APK" = 1 ]; then … fi`, and guard the **data.tar + adata.tar + obb** captures (the `mk_tar "$P/$pkg/data.tar" …` through the `obb.tar` line) with `if [ "$CAP_CFG" = 1 ]; then … fi`.

Finally, change the per-app meta write (`echo "golden_uid=$(app_uid "$pkg")" > "$P/$pkg/meta"`) to also record the axes:

```sh
  { echo "golden_uid=$(app_uid "$pkg")"; echo "axes=$AX"; } > "$P/$pkg/meta"
```

(If a pkg resolves to empty axes — not in the manifest — it shouldn't be in `pkglist.txt`; the existing pkglist is built by `user_pkgs`. For selective capture, Task 6 writes `pkglist.txt` from the manifest's pkgs; see Task 6 Step 4. For a no-`CAS_MANIFEST` capture, AX stays "apk config".)

- [ ] **Step 2: Static verification**

Run:
```bash
cd "$(git rev-parse --show-toplevel)"
grep -n 'manifest_axes "$CAS_MANIFEST"' provision/root/capture.sh
grep -n 'CAP_APK=1' provision/root/capture.sh && grep -n 'CAP_CFG=1' provision/root/capture.sh
grep -n 'axes=$AX' provision/root/capture.sh
```
Expected: each prints a match. `[VERIFY on device]`: on a golden, capture with a manifest containing `com.bar apk` and `xyz.aethersx2.android config` → assert `<…>/com.bar/data.tar` is ABSENT and `<…>/com.bar/apk/` present; `<…>/xyz.aethersx2.android/apk/` ABSENT and `data.tar`/`adata.tar` present; each `meta` has the right `axes=`.

- [ ] **Step 3: Commit**

```bash
git add provision/root/capture.sh
git commit -m "feat(capture): honor per-app APK/config axes; record axes= in per-app meta"
```

---

### Task 5: `restore.sh` deploys the captured set; config-only is not a failure

**Files:**
- Modify: `provision/root/restore.sh` (RPKGS derivation ~line 29; APK-install phase ~lines 47-65)

**Interfaces:**
- Consumes: per-app `meta` `axes=` (Task 4); `payload_pkgs` (existing).

- [ ] **Step 1: RPKGS = the captured set when no explicit app-manifest list**

`restore.sh:29` already does `RPKGS="$(payload_pkgs "$P")"` (from the earlier golden-driven work). Leave it. (The manifest, when present, still supplies `@flags`; its app list and the payload are the same set under selective capture.)

- [ ] **Step 2: Make a missing APK non-fatal for config-only apps**

In the install loop (`for pkg in $RPKGS; do` … the block that does `set -- "$P/$pkg/apk/"*.apk` and `[ -f "$1" ] || { warn "no APK in payload for $pkg"; FAIL=$((FAIL+1)); continue; }`), replace that guard with an axes-aware one:

```sh
  set -- "$P/$pkg/apk/"*.apk
  if [ ! -f "$1" ]; then
    AX="$(sed -n 's/^axes=//p' "$P/$pkg/meta" 2>/dev/null)"
    case " $AX " in
      *" config "*)   # config-only by design — app is provided elsewhere (e.g. OEM launcher)
        if pm path "$pkg" >/dev/null 2>&1; then
          log "config-only: $pkg already installed — applying config, no APK in payload"
        else
          warn "config-only: $pkg NOT installed on this unit — its config can't apply yet (install it, then re-run Update)"
        fi
        continue ;;
      *)
        warn "no APK in payload for $pkg"; FAIL=$((FAIL+1)); continue ;;
    esac
  fi
```

(Phase 2, the data-restore loop, already does `[ -f "$P/$pkg/data.tar" ] || continue` and `pm path "$pkg" || skip`, so config-only data lands on the externally-installed app and apk-only apps skip data cleanly — no change needed there.)

- [ ] **Step 3: Static verification**

Run:
```bash
cd "$(git rev-parse --show-toplevel)"
grep -n 'config-only: ' provision/root/restore.sh
! grep -n 'no APK in payload for \$pkg"; FAIL=$((FAIL+1)); continue; }' provision/root/restore.sh
```
Expected: first prints the two new log/warn lines; the negated grep confirms the old unconditional FAIL guard is gone (exit 0). `[VERIFY on device]`: restore a payload where `xyz.aethersx2.android` is config-only (data.tar, no apk) onto a unit that already has AetherSX2 → data restored, no FAIL; onto a unit without it → WARN, no FAIL.

- [ ] **Step 4: Commit**

```bash
git add provision/root/restore.sh
git commit -m "feat(restore): config-only (no-APK) apps apply config without failing the clone"
```

---

### Task 6: GUI two-axis checkboxes + default launcher + selective pkglist

**Files:**
- Modify: `cas/gui.py` (`on_select_profile` ~lines 901-924; `save_manifest` ~line 1046; `_toggle_all_apps`/`_sync_selall` helpers)
- Modify: `cas/profiles.py` (`Profile.all_pkgs` to include the default launcher)
- Test: `tests/test_cas.py`

**Interfaces:**
- Consumes: `manifest_axes`, `save_manifest(axes=…)` (Tasks 1-2).

- [ ] **Step 1: Test the data path — Profile includes the launcher, save uses axes**

Add to `tests/test_cas.py`:

```python
class TestProfileLauncherAndAxes(unittest.TestCase):
    def test_all_pkgs_includes_launcher_meta(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "golden_root_payload").mkdir(parents=True)
        (d / "golden_root_payload" / "pkglist.txt").write_text("com.foo\n")
        (d / "profile.meta").write_text("launcher_pkg=com.handheld.launcher\n")
        prof = P.Profile(d)
        self.assertIn("com.handheld.launcher", prof.all_pkgs())
        self.assertIn("com.foo", prof.all_pkgs())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestProfileLauncherAndAxes -q`
Expected: FAIL — launcher not in `all_pkgs()`.

- [ ] **Step 3: Implement `Profile.all_pkgs` launcher inclusion**

In `cas/profiles.py`, replace `Profile.all_pkgs` with:

```python
    def all_pkgs(self):
        """Every selectable app: the captured set (pkglist.txt) plus the default launcher
        (a system app excluded by user_pkgs) when known, so it can be ticked for config."""
        pl = self.payload / "pkglist.txt"
        pkgs = [l.strip() for l in _read_text(pl).splitlines() if l.strip()] if pl.exists() else self.pkgs()
        lp = self.meta.get("launcher_pkg") or _read_meta(self.payload / "homescreen" / "meta").get("launcher_pkg")
        if lp and lp not in pkgs:
            pkgs.append(lp)
        return pkgs
```

Run Step 1's test → PASS.

- [ ] **Step 4: GUI — two BooleanVars per app, two checkboxes, launcher APK disabled**

In `cas/gui.py` `on_select_profile`, change `self.pkg_vars = {}` to hold a pair per pkg and replace the app-row loop. Replace:

```python
        for pkg in prof.all_pkgs():
            var = tk.BooleanVar(value=(pkg in included))
            self.pkg_vars[pkg] = var
            cb = ttk.Checkbutton(self.modf, text=f" {_app_label(pkg)}", variable=var,
                                 command=self._on_app_toggle)
            icon = self._app_icon(prof, pkg) or self._placeholder_icon(_app_label(pkg))
            if icon is not None:
                cb.configure(image=icon, compound="left")
            _tip(cb, f"Package: {pkg}").pack(anchor="w")
```

with:

```python
        axes = prof.axes()                                   # {pkg: (apk, cfg)} from the saved manifest
        launcher_pkg = prof.meta.get("launcher_pkg")
        for pkg in prof.all_pkgs():
            apk0, cfg0 = axes.get(pkg, (pkg in included, pkg in included))
            is_launcher = (pkg == launcher_pkg)
            if is_launcher:
                apk0 = False                                 # system firmware — never reinstalled
            apk_v, cfg_v = tk.BooleanVar(value=apk0), tk.BooleanVar(value=cfg0)
            self.pkg_vars[pkg] = (apk_v, cfg_v)
            row = ttk.Frame(self.modf); row.pack(anchor="w", fill="x")
            ttk.Label(row, text=f" {_app_label(pkg)}", width=28).pack(side="left")
            apk_cb = ttk.Checkbutton(row, text="APK", variable=apk_v, command=self._on_app_toggle)
            if is_launcher:
                apk_cb.configure(state="disabled")
            _tip(apk_cb, f"Bundle {pkg}'s installer (off for a clean install / system launcher)").pack(side="left")
            _tip(ttk.Checkbutton(row, text="Config", variable=cfg_v, command=self._on_app_toggle),
                 f"Bundle {pkg}'s data/settings/BIOS (whole data dir for the launcher)").pack(side="left")
```

Add `Profile.axes()` to `cas/profiles.py` (next to `pkgs`/`flags`):

```python
    def axes(self):
        return manifest_axes(self.manifest_path)
```

- [ ] **Step 5: GUI — `save_manifest` writes both axes; fix the `pkg_vars` consumers**

Replace `save_manifest` in `cas/gui.py` with:

```python
    def save_manifest(self):
        name = self.prof_var.get()
        if not name:
            return
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
        axes = {p: (a.get(), c.get()) for p, (a, c) in self.pkg_vars.items()}
        pkgs = [p for p, (a, c) in axes.items() if a or c]          # included if EITHER axis on
        axes = {p: axes[p] for p in pkgs}
        flags = {fl: ("on" if v.get() else "off") for fl, v in self.flag_vars.items()}
        P.save_manifest(prof.manifest_path, pkgs, flags, header=f"# {name}", axes=axes)
        self.log(f"saved manifest for {name}: {len(pkgs)} app(s), flags={flags}")
```

Then fix every other reader of `self.pkg_vars` to use the pair. In `_toggle_all_apps`, `_sync_selall`, `_on_app_toggle`, and `_sync_media_tab` replace `v.get()`/`v.set(...)` over `self.pkg_vars.values()` with operating on **both** vars, e.g.:
- `_toggle_all_apps`: `for a, c in self.pkg_vars.values(): a.set(val); c.set(val)`
- `_sync_selall`: `all(a.get() or c.get() for a, c in self.pkg_vars.values())`
- `_sync_media_tab` ES-DE check (`self.pkg_vars.get(_ESDE_PKG)`): treat ES-DE "selected" as `a.get() or c.get()` of its pair.

(Search `self.pkg_vars` in `cas/gui.py` and update each site; the GUI rendering itself is `[VERIFY in app]`.)

- [ ] **Step 6: Verify data path + suite green**

Run:
```bash
cd "$(git rev-parse --show-toplevel)"
python3 -m pytest tests/test_cas.py -q
grep -n 'self.pkg_vars\[pkg\] = (apk_v, cfg_v)' cas/gui.py
! grep -nE 'for [a-z]+, v in self\.pkg_vars' cas/gui.py    # no leftover single-var iteration
```
Expected: suite PASS; first grep matches; the negated grep finds no stale single-var loops. `[VERIFY in app]`: launch CAS, select a profile → each app shows APK | Config; the launcher's APK box is disabled; Save selection writes tokened lines.

- [ ] **Step 7: Commit**

```bash
git add cas/gui.py cas/profiles.py tests/test_cas.py
git commit -m "feat(gui): two-axis (APK|Config) app selection + default launcher row; profiles.axes()"
```

---

## Phase 2 — Lock scrub (usage traces + saved game states)

### Task 7: `scrub_traces` + curated lists (lib-root.sh)

**Files:**
- Modify: `provision/root/lib-root.sh` (add `USAGE_TRACES`, `SAVE_STATES`, `scrub_traces` near `IDENTITY_EXCLUDES`, ~line 46)
- Test: `tests/test_scrub.sh` (new)

**Interfaces:**
- Produces: `scrub_traces` — clears Android recents, launcher last-played, `USAGE_TRACES`, and `SAVE_STATES` for installed pkgs. Each step WARNs on failure, never aborts. Consumed by Task 8 (`seal()`).

- [ ] **Step 1: Write the failing test (pure path-iteration on scratch dirs)**

Create `tests/test_scrub.sh`:

```bash
#!/usr/bin/env bash
# Local test for the scrub path-iteration (no device): point DATA_ROOT at a scratch tree and
# confirm SAVE_STATES/USAGE_TRACES members are removed and untargeted files survive.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/com.github.stenzek.duckstation/savestates" "$tmp/com.github.stenzek.duckstation/settings"
touch "$tmp/com.github.stenzek.duckstation/savestates/slot1.sav" "$tmp/com.github.stenzek.duckstation/settings/settings.ini"

scrub_members "$tmp" "com.github.stenzek.duckstation/savestates"      # helper under test
[ ! -e "$tmp/com.github.stenzek.duckstation/savestates/slot1.sav" ] || { echo "FAIL: savestate not removed"; fail=1; }
[ -e "$tmp/com.github.stenzek.duckstation/settings/settings.ini" ] || { echo "FAIL: settings wrongly removed"; fail=1; }
[ "$fail" -eq 0 ] && { echo "PASS: scrub_members"; exit 0; } || exit 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "$(git rev-parse --show-toplevel)" && bash tests/test_scrub.sh`
Expected: FAIL — `scrub_members: command not found`.

- [ ] **Step 3: Implement lists + helpers**

In `provision/root/lib-root.sh`, after the `IDENTITY_EXCLUDES=` line, add:

```sh
# Ship-clean scrub (run at Lock, while rooted, before un-root). Member-relative "pkg/reldir-or-file",
# same form as IDENTITY_EXCLUDES. USAGE_TRACES = recent-ROM/MRU/search; SAVE_STATES = savestates + saves.
# [VERIFY on device] — exact paths confirmed on the AIR X during implementation; seed the known set.
USAGE_TRACES="com.retroarch.aarch64/content_history.lpl com.retroarch.aarch64/content_image_history.lpl"
SAVE_STATES="com.github.stenzek.duckstation/savestates xyz.aethersx2.android/files/sstates"
# scrub_members <data_root> <member…> — rm -rf each member under data_root (WARN on failure, never abort).
scrub_members(){ dr="$1"; shift; for m in "$@"; do rm -rf "$dr/$m" 2>/dev/null || warn "scrub: could not remove $m"; done; }
# scrub_traces — the Lock-time entry point. DATA_ROOT defaults to /data/data; ADATA to external storage.
scrub_traces(){
  DR="${DATA_ROOT:-/data/data}"; AR="${ADATA_ROOT:-/sdcard/Android/data}"
  for m in $USAGE_TRACES $SAVE_STATES; do
    p="${m%%/*}"; pm path "$p" >/dev/null 2>&1 || continue           # only installed pkgs
    scrub_members "$DR" "$m"; scrub_members "$AR" "$m"               # member may live in either root
  done
  # Android recent tasks (best path per build) + launcher last-played handled in restore-side helper.
  rm -rf /data/system_ce/0/recent_tasks/* /data/system/recent_tasks/* 2>/dev/null || warn "scrub: recents"
  ok "scrub_traces: usage + saved-states cleared"
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "$(git rev-parse --show-toplevel)" && bash tests/test_scrub.sh`
Expected: `PASS: scrub_members`. Also re-run `bash tests/test_manifest_axes.sh` and `bash tests/test_payload_pkgs.sh` (sourcing lib-root.sh still clean).

- [ ] **Step 5: Commit**

```bash
git add provision/root/lib-root.sh tests/test_scrub.sh
git commit -m "feat(root): scrub_traces + USAGE_TRACES/SAVE_STATES lists (Lock-time ship-clean)"
```

---

### Task 8: Lock launcher last-played scrub + wire scrub into `seal()`

**Files:**
- Modify: `provision/root/restore.sh` or a new `provision/root/scrub.sh` invoked at Lock (launcher GAME_INFO last-played)
- Modify: `cas/provision.py` (`seal()` ~line 761 — run the scrub before the un-root flash)
- Test: `tests/test_cas.py`

**Interfaces:**
- Consumes: `scrub_traces` (Task 7).

- [ ] **Step 1: Add the launcher last-played scrub script**

Create `provision/root/scrub.sh`:

```sh
#!/system/bin/sh
# scrub.sh — run AS ROOT at Lock (before un-root). Clears usage traces + saved game states so the unit
# ships factory-fresh. Additive: every step WARNs on failure, never fails the seal.
DIR="$(cd "$(dirname "$0")" && pwd)"; . "$DIR/lib-root.sh"
is_root || { echo "must run as root (su)"; exit 0; }       # exit 0 — never block a seal
scrub_traces
# launcher last-played: null out lastOpenedTimestamp in GAME_INFO (whichever launcher is HOME)
LP="$(home_launcher)"; DB="/data/data/$LP/databases/GAME_INFO"
if [ -n "$LP" ] && [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  am force-stop "$LP" 2>/dev/null
  sqlite3 "$DB" "UPDATE game SET lastOpenedTimestamp=NULL;" 2>/dev/null && ok "scrub: launcher last-played cleared" \
    || warn "scrub: GAME_INFO update skipped (no sqlite3 or schema differs)"
fi
ok "scrub.sh done"
```

(`sqlite3` may be absent on-device; the WARN path is acceptable — `[VERIFY on device]` whether the AIR X ships `sqlite3`, else fall back to the offline pull/edit/push used elsewhere.)

- [ ] **Step 2: Write the failing test — seal runs the scrub before un-root**

Add to `tests/test_cas.py` (mirror an existing `seal` test's FakeRunner setup; assert the scrub is pushed+run before the stock-init_boot flash):

```python
class TestSealScrub(unittest.TestCase):
    def test_seal_runs_scrub_before_unroot(self):
        calls = []
        # Reuse the suite's fake adb/fastboot; record shell invocations.
        # (Match the pattern of the existing seal test in this file.)
        # Assert that a 'sh .../scrub.sh' (or scrub_traces) shell call appears in calls
        # BEFORE any 'flash init_boot'/stock-init_boot flash call.
        ...
```

(Implementer: copy the existing `seal()` test's fixture in this file, capture the ordered command list, and assert `scrub` precedes the un-root flash. Keep it a real ordering assertion, not a smoke test.)

- [ ] **Step 3: Run test to verify it fails**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestSealScrub -q`
Expected: FAIL — seal doesn't run the scrub yet.

- [ ] **Step 4: Wire the scrub into `seal()`**

In `cas/provision.py` `seal()`, BEFORE the un-root flash step (step 3, the stock init_boot flash), and only when still rooted, push + run the scrub:

```python
    # (2.5) ship-clean scrub while still rooted — clears usage traces + saved game states. Additive:
    #       never blocks the seal (scrub.sh exits 0 on any internal failure).
    if adb.is_root():
        log("scrub: clearing usage traces + saved game states before un-root…")
        adb.push_root_toolkit()        # the same staging the other root steps use; see existing seal/root code
        adb.shell_su("sh %s/scrub.sh" % adb.root_dir)   # match the project's su-invocation helper
```

(Implementer: use the exact root-staging + su-run helpers `seal()`/root already use in this file — do not invent new ones. The call must occur after the golden guard and Magisk-app removal, before the un-root flash.)

- [ ] **Step 5: Run test to verify it passes**

Run: `cd "$(git rev-parse --show-toplevel)" && python3 -m pytest tests/test_cas.py::TestSealScrub -q && python3 -m pytest tests/test_cas.py -q`
Expected: the new test PASSES; full suite stays green. `[VERIFY on device]`: Lock a provisioned AIR X → recents empty, launcher shows no recently-played, emulator savestates gone, then un-root completes as before.

- [ ] **Step 6: Commit**

```bash
git add provision/root/scrub.sh cas/provision.py tests/test_cas.py
git commit -m "feat(lock): scrub usage traces + saved states in seal() before un-root"
```

---

## Self-Review

**Spec coverage** (against `2026-06-29-selective-capture-and-lock-scrub-design.md`):
- §3 manifest tokens → Tasks 1 (parse), 2 (write), 3 (shell). ✓
- §4.1 capture per-axis + meta `axes=` + launcher config gating → Task 4 (launcher capture reuses existing `@homescreen` path, gated by its config axis selected in Task 6). ✓
- §4.2 GUI two-axis + launcher row → Task 6. ✓
- §4.3 provision passes selection → capture reads `CAS_MANIFEST` (Task 4); GUI/Save persists it (Task 6). ✓
- §5 restore deploy semantics, config-only not-FAIL → Task 5. ✓
- §6 Lock scrub (recents, launcher last-played, USAGE_TRACES, SAVE_STATES) → Tasks 7-8. ✓
- §2 back-compat (bare line / no-manifest = both axes) → Tasks 1/2/3 defaults + Task 5 fallback. ✓

**Placeholder scan:** Code steps carry real code. Two steps intentionally defer to existing-fixture matching (Task 8 Step 2 test, Step 4 helper names) because the exact `seal()` fake-adb fixture + root-staging helper names live in `cas/provision.py`/`tests/test_cas.py` and must be copied verbatim, not invented — each says exactly what to assert/call and where. `[VERIFY on device]`/`[VERIFY in app]` markers are the repo convention, not placeholders.

**Type/name consistency:** `manifest_axes` returns `{pkg:(apk,cfg)}` in both Python (Tasks 1,6) and as the shell `apk config` string (Task 3); `save_manifest(..., axes=...)` shape matches Task 1's output and Task 6's writer; `self.pkg_vars[pkg]` is a `(apk_v, cfg_v)` pair consistently after Task 6; `scrub_traces`/`scrub_members` names match across Tasks 7-8. ✓
