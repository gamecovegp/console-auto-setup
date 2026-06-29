# Game-launcher config capture & restore — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-detect the game frontend (e.g. `com.handheld.launcher`, distinct from the Android HOME app) and capture/restore only its portable per-system emulator picks (the DataStore), so a fresh unit inherits PSX→DuckStation etc. with no manual setup.

**Architecture:** A shell resolver `game_launcher()` (override → data-dir-signature probe → curated list) finds the frontend. Save tars only `files/datastore` + `shared_prefs` (never the SD-bound `GAME_INFO`) into `gamelauncher/` in the golden. Download auto-detects the target's frontend and writes the config back as a system app. It rides as an **additive behavior step gated by a `@gamelauncher` flag** (modeled on `@homescreen`: WARN-never-FAIL), NOT a regular manifest pkg line — because restore's APK loop would `FAIL` on a listed system-app pkg that has no APK and no per-pkg `meta`.

**Tech Stack:** POSIX `sh` (on-device, runs as root via Magisk `su`), Python 3 stdlib (`cas/` Tk GUI + provision), `unittest` + bash smoke tests.

## Global Constraints

- On-device scripts are POSIX `sh` (`#!/system/bin/sh`), run as root; no bashisms.
- All game-launcher steps are **additive/best-effort**: WARN on any problem, **never** bump `FAIL`/`CFAIL` (same contract as `@homescreen`).
- **Never** capture `GAME_INFO*` (SD-serial-bound + scan-rebuilt) or caches — portable config only (`files/datastore`, `shared_prefs`).
- Detection order is **override → probe → list** (override evaluated first so a manifest pin can't be lost to a stray probe hit).
- Back-compat: a golden with no `gamelauncher/` payload dir, or a manifest with no `@gamelauncher` flag, behaves exactly as today (step silently no-ops; flag defaults ON only when the payload carries the dir).
- Shell helpers honor a `DATA_ROOT` override (default `/data/data`) so they are testable off-device, mirroring `scrub_traces`.
- Python: stdlib only (no third-party runtime deps — CI runs `python -m unittest`).

---

### Task 1: `game_launcher()` resolver + `GAME_LAUNCHERS` (detection)

**Files:**
- Modify: `provision/root/lib-root.sh` (append after `home_launcher()`, ~line 118)
- Test: `tests/test_game_launcher.sh` (create)

**Interfaces:**
- Produces: `game_launcher [override_pkg]` → echoes the resolved frontend package (or nothing); honors `DATA_ROOT` for the probe root. `GAME_LAUNCHERS` (space-separated curated list). `_gl_installed <pkg>` → rc 0 if `pm path` resolves.
- Consumes: `pm` (device), `warn`/`ok` (lib-root.sh).

- [ ] **Step 1: Write the failing test** — create `tests/test_game_launcher.sh`

```bash
#!/usr/bin/env bash
# Local smoke test for game_launcher / gl_capture / gl_restore (no device). Stubs pm/am/chown/restorecon/stat
# and points DATA_ROOT at scratch trees. Run: bash tests/test_game_launcher.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# --- stubs for device binaries the helpers shell out to -----------------------------------------
INSTALLED=""                                   # space-separated pkgs pm should report as installed
pm(){ case "$1" in path) case " $INSTALLED " in *" $2 "*) return 0;; *) return 1;; esac;; *) return 0;; esac; }
am(){ :; }                                     # force-stop no-op
chown(){ :; }                                  # ownership no-op off-device
restorecon(){ :; }                             # relabel no-op off-device

# === game_launcher resolution order =============================================================
# (1) probe: a data dir with the DataStore signature wins over the list
mkdir -p "$tmp/data/com.handheld.launcher/files/datastore"
: > "$tmp/data/com.handheld.launcher/files/datastore/GameLauncher.preferences_pb"
got="$(DATA_ROOT="$tmp/data" game_launcher)"
[ "$got" = "com.handheld.launcher" ] || { echo "FAIL(1 probe): [$got]"; fail=1; }

# (2) override wins even when a different pkg would probe-hit
INSTALLED="com.oem.frontend"
got="$(DATA_ROOT="$tmp/data" game_launcher com.oem.frontend)"
[ "$got" = "com.oem.frontend" ] || { echo "FAIL(2 override): [$got]"; fail=1; }

# (3) no probe hit -> curated list fallback (pm-installed)
empty="$tmp/empty"; mkdir -p "$empty"
INSTALLED="com.handheld.launcher"
got="$(DATA_ROOT="$empty" game_launcher)"
[ "$got" = "com.handheld.launcher" ] || { echo "FAIL(3 list): [$got]"; fail=1; }

# (4) nothing matches -> empty
INSTALLED=""
got="$(DATA_ROOT="$empty" game_launcher)"
[ -z "$got" ] || { echo "FAIL(4 none): [$got]"; fail=1; }

[ "$fail" -eq 0 ] && { echo "PASS: game_launcher"; exit 0; } || exit 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test_game_launcher.sh`
Expected: FAIL — `game_launcher: command not found` / non-zero exit (function not defined yet).

- [ ] **Step 3: Implement the resolver** — append to `provision/root/lib-root.sh`

```sh
# The GAME FRONTEND (holds per-system emulator picks) — DISTINCT from the Android HOME app (home_launcher).
# Curated fallback list; the probe below handles OEM rebrands that keep the ES-DE-fork data shape.
GAME_LAUNCHERS="com.handheld.launcher"
_gl_installed(){ pm path "$1" >/dev/null 2>&1; }
# game_launcher [override_pkg] — resolve the frontend. Order: override (if installed) -> data-dir signature
# probe (databases/GAME_INFO or files/datastore/GameLauncher.preferences_pb) -> curated list. Echoes the
# package or nothing. DATA_ROOT overrides the probe root (default /data/data) so this is testable off-device.
game_launcher(){
  ov="$1"
  if [ -n "$ov" ] && _gl_installed "$ov"; then echo "$ov"; return 0; fi
  DR="${DATA_ROOT:-/data/data}"
  for d in "$DR"/*; do
    [ -d "$d" ] || continue
    if [ -f "$d/databases/GAME_INFO" ] || [ -f "$d/files/datastore/GameLauncher.preferences_pb" ]; then
      echo "${d##*/}"; return 0
    fi
  done
  for p in $GAME_LAUNCHERS; do _gl_installed "$p" && { echo "$p"; return 0; }; done
  return 0
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test_game_launcher.sh`
Expected: `PASS: game_launcher`

- [ ] **Step 5: Commit**

```bash
git add provision/root/lib-root.sh tests/test_game_launcher.sh
git commit -m "feat(root): game_launcher() resolver (override->probe->list)"
```

---

### Task 2: `gl_capture` + `gl_restore` helpers (portable config I/O)

**Files:**
- Modify: `provision/root/lib-root.sh` (append after `game_launcher()`)
- Test: `tests/test_game_launcher.sh` (extend)

**Interfaces:**
- Consumes: `game_launcher` (Task 1), `warn`/`ok`, `DATA_ROOT`, device `tar`/`am`/`chown`/`restorecon`/`stat`.
- Produces:
  - `gl_capture <out_dir> <pkg>` → writes `<out_dir>/gamelauncher/meta` (`pkg=`, `uid=`) + `<out_dir>/gamelauncher/config.tar` (members `files/datastore` + `shared_prefs`, **GAME_INFO/caches excluded**). rc 0 if a readable `config.tar` was produced, else 1 (and removes the stub).
  - `gl_restore <payload_dir> <pkg>` → force-stop, extract config.tar into `<DATA_ROOT>/<pkg>`, `chown system:system`, `restorecon -R`, verify a `*.preferences_pb` exists. rc 0 on verified write-back, else 1.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_game_launcher.sh` BEFORE the final PASS/exit line

```bash
# === gl_capture: portable subtrees only, GAME_INFO excluded =====================================
src="$tmp/data/com.handheld.launcher"
mkdir -p "$src/files/datastore" "$src/databases" "$src/cache"
printf 'psx_select_emulator' > "$src/files/datastore/GameLauncher.preferences_pb"
: > "$src/databases/GAME_INFO"
: > "$src/cache/junk"
out="$tmp/out"; mkdir -p "$out"
DATA_ROOT="$tmp/data" gl_capture "$out" "com.handheld.launcher" >/dev/null || { echo "FAIL(cap rc)"; fail=1; }
tar -tf "$out/gamelauncher/config.tar" 2>/dev/null | grep -q 'files/datastore/GameLauncher.preferences_pb' \
  || { echo "FAIL(cap: datastore missing)"; fail=1; }
tar -tf "$out/gamelauncher/config.tar" 2>/dev/null | grep -q 'GAME_INFO' \
  && { echo "FAIL(cap: GAME_INFO leaked)"; fail=1; }
grep -q '^pkg=com.handheld.launcher$' "$out/gamelauncher/meta" || { echo "FAIL(cap: meta pkg)"; fail=1; }

# === gl_restore: extracts + verifies a preferences_pb under the target data dir ==================
dst="$tmp/restore"; mkdir -p "$dst/com.handheld.launcher"     # target app data dir exists (installed)
DATA_ROOT="$dst" gl_restore "$out" "com.handheld.launcher" >/dev/null || { echo "FAIL(res rc)"; fail=1; }
[ -f "$dst/com.handheld.launcher/files/datastore/GameLauncher.preferences_pb" ] \
  || { echo "FAIL(res: preferences_pb not written)"; fail=1; }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test_game_launcher.sh`
Expected: FAIL — `gl_capture: command not found` (helpers not defined).

- [ ] **Step 3: Implement the helpers** — append to `provision/root/lib-root.sh`

```sh
# gl_capture <out_dir> <pkg> — capture ONLY the launcher's portable config (DataStore + shared_prefs);
# NEVER GAME_INFO (SD-bound + scan-rebuilt) or caches. DATA_ROOT overridable for tests.
gl_capture(){
  out="$1"; pkg="$2"; DR="${DATA_ROOT:-/data/data}"; src="$DR/$pkg"
  [ -d "$src" ] || { warn "gamelauncher: $pkg has no data dir — skip"; return 1; }
  mkdir -p "$out/gamelauncher"
  { echo "pkg=$pkg"; echo "uid=$(stat -c %u "$src" 2>/dev/null)"; } > "$out/gamelauncher/meta"
  ( cd "$src" 2>/dev/null && tar -cf "$out/gamelauncher/config.tar" \
      --exclude='files/datastore/*-shm' --exclude='files/datastore/*.tmp' \
      files/datastore shared_prefs 2>/dev/null )
  if tar -tf "$out/gamelauncher/config.tar" >/dev/null 2>&1; then
    ok "captured game launcher config: $pkg"; return 0
  fi
  warn "gamelauncher: no portable config for $pkg (no datastore/shared_prefs?) — skip"
  rm -f "$out/gamelauncher/config.tar"; return 1
}
# gl_restore <payload_dir> <pkg> — write the captured config back as a SYSTEM app: force-stop -> extract ->
# chown system:system -> restorecon -> verify a preferences_pb exists. DATA_ROOT overridable for tests.
gl_restore(){
  Pd="$1"; pkg="$2"; DR="${DATA_ROOT:-/data/data}"; tgt="$DR/$pkg"
  tar -tf "$Pd/gamelauncher/config.tar" >/dev/null 2>&1 || { warn "gamelauncher: config.tar missing/corrupt — skip"; return 1; }
  [ -d "$tgt" ] || { warn "gamelauncher: $pkg not installed here — skip"; return 1; }
  am force-stop "$pkg" 2>/dev/null
  mkdir -p "$tgt/files/datastore"
  tar -xf "$Pd/gamelauncher/config.tar" -C "$tgt" 2>/dev/null || { warn "gamelauncher: extract failed: $pkg"; return 1; }
  chown -R system:system "$tgt/files/datastore" 2>/dev/null
  [ -d "$tgt/shared_prefs" ] && chown -R system:system "$tgt/shared_prefs" 2>/dev/null
  restorecon -R "$tgt/files/datastore" 2>/dev/null || warn "gamelauncher: restorecon failed (verify on enforcing unit)"
  if ls "$tgt"/files/datastore/*.preferences_pb >/dev/null 2>&1; then
    ok "game launcher config applied: $pkg"; return 0
  fi
  warn "gamelauncher: write-back unverified (no preferences_pb) for $pkg"; return 1
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test_game_launcher.sh`
Expected: `PASS: game_launcher`

- [ ] **Step 5: Commit**

```bash
git add provision/root/lib-root.sh tests/test_game_launcher.sh
git commit -m "feat(root): gl_capture/gl_restore portable launcher config helpers"
```

---

### Task 3: Wire Save — `capture.sh` calls `gl_capture`

**Files:**
- Modify: `provision/root/capture.sh` (insert after the homescreen/wallpaper block, before the settings/SAF/grants tail — i.e. after line ~127, the `fi` that closes the `home_launcher` block; place it after the wallpaper `for` loop ends so it's grouped with launcher state)

**Interfaces:**
- Consumes: `game_launcher`, `gl_capture` (Tasks 1–2), `manifest_flag`, `$P`, `$CAS_MANIFEST`.
- Produces: `$P/gamelauncher/{meta,config.tar}` in the golden when a frontend is detected and `@gamelauncher` is not `off`.

- [ ] **Step 1: Add the capture block** — insert into `provision/root/capture.sh` after the wallpaper capture loop

```sh
# GAME LAUNCHER emulator picks — capture ONLY the portable DataStore/prefs (NOT GAME_INFO; that is SD-bound +
# scan-rebuilt). Auto-detected frontend, independent of the HOME launcher above. Additive (never bumps CFAIL).
# Gated by @gamelauncher (default on); "@gamelauncher off" disables; "@gamelauncher <pkg>" pins the frontend.
FGLC=on; OVLC=""
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then
  v="$(manifest_flag "$CAS_MANIFEST" gamelauncher)"
  case "$v" in "") : ;; off) FGLC=off ;; *.*) OVLC="$v" ;; esac
fi
if [ "$FGLC" = off ]; then
  log "game launcher: capture skipped (@gamelauncher off)"
else
  GL="$(game_launcher "$OVLC")"
  if [ -n "$GL" ]; then gl_capture "$P" "$GL"; else warn "game launcher: none detected — nothing to capture"; fi
fi
```

- [ ] **Step 2: Verify capture.sh still parses**

Run: `sh -n provision/root/capture.sh && echo OK`
Expected: `OK` (no syntax errors).

- [ ] **Step 3: Confirm the helpers it calls exist**

Run: `grep -nE 'gl_capture\(\)|game_launcher\(\)' provision/root/lib-root.sh`
Expected: both function definitions are listed.

- [ ] **Step 4: Run the full shell + python suites (no regressions)**

Run: `bash tests/test_game_launcher.sh && bash tests/test_manifest_axes.sh && bash tests/test_scrub.sh && python -m unittest discover -s tests -q`
Expected: each shell test prints `PASS:`; unittest ends `OK`.

- [ ] **Step 5: Commit**

```bash
git add provision/root/capture.sh
git commit -m "feat(capture): save auto-detected game-launcher portable config"
```

---

### Task 4: Wire Download — additive `gl_restore` step in `restore.sh`

**Files:**
- Modify: `provision/root/restore.sh` (add a new step alongside the `@homescreen` block, ~after line 300; it must run regardless of the homescreen outcome)

**Interfaces:**
- Consumes: `game_launcher`, `gl_restore` (Tasks 1–2), `manifest_flag`, `$P` (payload), `$CAS_MANIFEST`.
- Produces: writes the captured DataStore onto this unit's detected frontend. Additive — never touches `FAIL`.

- [ ] **Step 1: Add the restore step** — insert into `provision/root/restore.sh` near the homescreen block

```sh
# GAME LAUNCHER emulator picks (DataStore) — ADDITIVE (WARN, never FAIL), like @homescreen. Auto-detect THIS
# unit's frontend and write back the captured portable config. Default ON when the payload carries it;
# "@gamelauncher off" disables; "@gamelauncher <pkg>" pins/overrides the target frontend.
FGL=on; OVL=""
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then
  v="$(manifest_flag "$CAS_MANIFEST" gamelauncher)"
  case "$v" in "") : ;; off) FGL=off ;; *.*) OVL="$v" ;; *) FGL="$v" ;; esac
fi
if [ ! -d "$P/gamelauncher" ]; then
  : # back-compat: golden carried no game-launcher config — nothing to do
elif [ "$FGL" = off ]; then
  log "game launcher: skipped (@gamelauncher off)"
else
  GLPKG="$(sed -n 's/^pkg=//p' "$P/gamelauncher/meta" 2>/dev/null)"
  TGL="$(game_launcher "$OVL")"
  if [ -z "$TGL" ]; then
    warn "game launcher: none detected on this unit — skip"
  elif [ -n "$GLPKG" ] && [ "$TGL" != "$GLPKG" ]; then
    warn "game launcher: this unit ($TGL) != golden's ($GLPKG) — skip (different family?)"
  else
    gl_restore "$P" "$TGL" || true        # additive: a write-back miss must not fail the restore
  fi
fi
```

- [ ] **Step 2: Verify restore.sh still parses**

Run: `sh -n provision/root/restore.sh && echo OK`
Expected: `OK`.

- [ ] **Step 3: Add an off-device restore-gating assertion** — append to `tests/test_game_launcher.sh` before the final PASS/exit (proves the mismatch guard skips write-back without error)

```bash
# === restore mismatch guard: target frontend != golden's -> skip (rc tolerated, no preferences written) ===
dst2="$tmp/restore2"; mkdir -p "$dst2/com.other.launcher"
INSTALLED="com.other.launcher"
# golden meta says com.handheld.launcher; this unit detects com.other.launcher -> guarded skip
TGL_TEST="$(DATA_ROOT="$dst2" game_launcher)"
[ "$TGL_TEST" = "com.other.launcher" ] || { echo "FAIL(guard detect): [$TGL_TEST]"; fail=1; }
[ ! -f "$dst2/com.other.launcher/files/datastore/GameLauncher.preferences_pb" ] \
  || { echo "FAIL(guard: wrote to mismatched frontend)"; fail=1; }
```

- [ ] **Step 4: Run the shell suite**

Run: `bash tests/test_game_launcher.sh`
Expected: `PASS: game_launcher`

- [ ] **Step 5: Commit**

```bash
git add provision/root/restore.sh tests/test_game_launcher.sh
git commit -m "feat(restore): auto-detect frontend + write back game-launcher config"
```

---

### Task 5: Surface in CAS — push the payload dir + GUI flag

**Files:**
- Modify: `cas/provision.py:334-335` (push `gamelauncher/` like `homescreen/`)
- Modify: `cas/gui.py` (add `gamelauncher` to the behavior-flag list + labels/tips, ~lines 952-963)
- Test: `tests/test_cas.py` (add a `@gamelauncher` manifest round-trip case in the manifest/axes test class)

**Interfaces:**
- Consumes: `P.save_manifest` / `manifest_flags` (existing), the `push()` closure in `provision()`.
- Produces: `gamelauncher/` reaches `{DEV}/payload/`; GUI writes `@gamelauncher on`/`off`.

- [ ] **Step 1: Write the failing test** — add to the manifest test class in `tests/test_cas.py`

```python
    def test_gamelauncher_flag_roundtrips(self):
        import tempfile, pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        m = d / "manifest"
        P.save_manifest(m, ["com.foo"], {"gamelauncher": "on", "homescreen": "on"})
        self.assertEqual(P.manifest_flags(m).get("gamelauncher"), "on")
```

- [ ] **Step 2: Run it to confirm it passes for parsing, then assert the GUI wiring is missing**

Run: `python -m unittest tests.test_cas -k gamelauncher -v`
Expected: PASS (the parser is generic — this guards the round-trip). If `manifest_flags` is imported under a different name in the test module, match the existing tests' import (`P.manifest_flags`).

- [ ] **Step 3: Push the payload dir** — edit `cas/provision.py` right after the `homescreen` push (line 334-335)

```python
        if (pay / "homescreen").exists() and not push(pay / "homescreen", f"{DEV}/payload/"):
            return False                                   # launcher layout + wallpaper + widget map (optional)
        if (pay / "gamelauncher").exists() and not push(pay / "gamelauncher", f"{DEV}/payload/"):
            return False                                   # game-frontend emulator picks (DataStore), optional
```

- [ ] **Step 4: Add the GUI flag** — edit `cas/gui.py` flag list + labels/tips (the block near lines 952-963)

```python
        flag_labels = {"settings": "Display & system settings", "hardening": "Performance & update lock",
                       "grants": "Folder permissions", "homescreen": "Homescreen layout",
                       "gamelauncher": "Game launcher emulator picks"}
        flag_tips = {
            "settings": "Apply the saved display/brightness/animation/screen-timeout preferences.",
            "hardening": "Keep emulators awake (exempt from battery optimization so they're never killed) "
                         "and block OTA system updates that could break root.",
            "grants": "Restore folder-access permissions so ES-DE and the emulators can read your "
                      "ROM/BIOS folders without re-asking on first launch.",
            "homescreen": "Restore the homescreen layout — your app folders, icon/dock arrangement, "
                          "wallpaper (and widgets, best-effort).",
            "gamelauncher": "Save the game frontend's per-system emulator choices (PSX→DuckStation, "
                            "PSP→PPSSPP) and auto-apply them on Download — no manual setup per unit.",
        }
        ttk.Label(self.modf, text="— behavior —").pack(anchor="w", pady=(6, 0))
        for fl in ("settings", "hardening", "grants", "homescreen", "gamelauncher"):
            fv = tk.BooleanVar(value=(flags.get(fl, "on") == "on"))
            self.flag_vars[fl] = fv
            cb = ttk.Checkbutton(self.modf, text=f"{flag_labels.get(fl, fl)}  (@{fl})", variable=fv)
            _tip(cb, flag_tips.get(fl, "")).pack(anchor="w")
```

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest discover -s tests -q && bash tests/test_game_launcher.sh`
Expected: unittest `OK`; shell `PASS: game_launcher`.

- [ ] **Step 6: Commit**

```bash
git add cas/provision.py cas/gui.py tests/test_cas.py
git commit -m "feat(cas): push game-launcher payload + @gamelauncher GUI flag"
```

---

### Task 6: On-device verification (manual) + spec open-items

**Files:**
- Modify: `docs/superpowers/specs/2026-06-29-game-launcher-config-capture-design.md` (tick the §9 `[VERIFY on device]` items as confirmed)

This task has no unit test — it is the real-hardware acceptance check the spec calls for. Perform on the AIR X (`MQ66142509130541`) with the toolkit pushed.

- [ ] **Step 1: Probe correctness** — confirm detection picks the frontend, not the HOME app

Run (PC): `adb -s <serial> shell su -c 'sh -c ". /storage/<sd>/provision/root/lib-root.sh; game_launcher"'`
Expected: prints `com.handheld.launcher` (NOT `com.android.launcher3`).

- [ ] **Step 2: Capture is portable** — Save the golden, then inspect the artifact

Run (PC): `tar -tf <profile>/golden_root_payload/gamelauncher/config.tar`
Expected: lists `files/datastore/GameLauncher.preferences_pb`; contains **no** `GAME_INFO`.

- [ ] **Step 3: Round-trip** — on a fresh/reset unit, Download, then read the picks back

Run (PC): `adb -s <serial> shell su -c 'strings /data/data/com.handheld.launcher/files/datastore/GameLauncher.preferences_pb | grep _select_emulator'`
Expected: `psx_select_emulator` (→ `%EMULATOR_DUCKSTATION%`) present — i.e. PSX launches DuckStation with no manual setup. Confirm ownership: `stat -c '%U:%G' …/files/datastore` = `system:system`.

- [ ] **Step 4: Rescan survival** (spec §9 open item) — open the launcher, trigger a ROM rescan, re-read

Repeat Step 3's `strings` check after a full rescan.
Expected: `psx_select_emulator` still present. **If it is gone**, the launcher rebuilt the DataStore on scan → open a follow-up to add a post-scan re-apply (mirror the PS2-runner `GAME_INFO` fixup in `[[mangmi-launcher-internals]]`); do NOT mark this step complete.

- [ ] **Step 5: Update the spec + commit**

Tick the confirmed `[VERIFY on device]` markers in the design doc (probe paths, rescan survival) with the observed result.

```bash
git add docs/superpowers/specs/2026-06-29-game-launcher-config-capture-design.md
git commit -m "docs(spec): confirm game-launcher capture verified on AIR X"
```

---

## Self-Review

**Spec coverage:**
- §3 detection (override→probe→list) → Task 1. ✓
- §4 Save portable-config-only (datastore+shared_prefs, no GAME_INFO) → Tasks 2–3. ✓
- §5 Download auto-detect + system-app write-back → Tasks 2, 4. ✓
- §6 write-back = overwrite → `gl_restore` extracts over the target (overwrite); no merge. ✓
- §8 error handling (WARN-never-FAIL, back-compat no-op) → Global Constraints + Tasks 3–4 guards. ✓
- §10 testing (resolver order, GAME_INFO-excluded, write-back, round-trip) → Tasks 1–2, 5–6. ✓
- §9 open items (probe paths, rescan survival) → Task 6 Steps 1, 4. ✓
- GUI/manifest surfacing → Task 5. ✓ (Deviation from spec §4.2: surfaced as a `@gamelauncher` **behavior flag**, not an app-list row, to avoid restore's `RPKGS` APK-loop `FAIL` path — see Architecture.)

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows complete code; the only `[VERIFY on device]` markers are in Task 6, which is the explicit hardware-acceptance task. ✓

**Type/name consistency:** `game_launcher`, `gl_capture`, `gl_restore`, `GAME_LAUNCHERS`, `_gl_installed`, `DATA_ROOT`, payload dir `gamelauncher/` with `meta`(`pkg=`/`uid=`)+`config.tar`, flag `@gamelauncher` (`on`/`off`/`<pkg>`) — used identically across Tasks 1–5 and the device checks in Task 6. ✓
