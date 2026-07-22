# Homescreen Layout Owner — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the homescreen layout from the package that actually owns it, so a HOME shim like the AYN Thor's Mjolnir can no longer make the golden archive an empty data dir as "the homescreen".

**Architecture:** `capture.sh` currently uses `home_launcher()` for two different jobs — who gets `set-home-activity` on restore, and whose `/data/data` becomes `launcher_data.tar`. Task 1 adds a `layout_launcher` resolver to `lib-root.sh` that answers only the second question, reusing the existing `homescreen_apps` token scan as its "does this dir hold a layout?" test. Task 2 wires capture to it. Task 3 stops `restore.sh` gating the layout on "is this package HOME?", which is wrong the moment the two roles differ.

**Tech Stack:** POSIX shell (Android `toybox`/`mksh` on device; `sh` on the CI runners). Tests are `tests/test_*.sh`, run by `.github/workflows/build.yml:87` as `sh "$f"` on the Linux and macOS legs only.

**Spec:** `docs/superpowers/specs/2026-07-22-homescreen-layout-owner-design.md`

## Global Constraints

- **POSIX `sh` only.** CI invokes these with `sh`, not bash — no `[[ ]]`, no arrays, no `local`, no `${var,,}`. Use `case` for pattern matching.
- **Never `sed -i`.** BSD/macOS `sed` reads the next argument as a backup suffix. Use `sed SCRIPT f > f.tmp && mv f.tmp f`.
- **Prefix new variables inside `lib-root.sh` functions** (`_ll_`, `_hl_`) — the file has no `local`, so every variable is global and a bare `d`/`p` would collide with a caller's.
- **Additive homescreen.** Every failure path in the homescreen block `warn`s; it must never bump `CFAIL` in `capture.sh` or `FAIL` in `restore.sh`. The wallpaper restore lives *inside* the same block, so a wrongly-skipped layout also costs the wallpaper — that is what makes the restore gate in Task 3 load-bearing.
- **Back-compat is load-bearing.** All four working goldens (`retroid-pocket-6-512/256`, `mangmi-air-x-256`, `ayn-odin-3-256`) record `launcher_pkg=com.android.launcher3` and take the "HOME owns the layout" path. Their capture and restore behaviour must be byte-identical after this change.
- **Mjolnir stays HOME.** This plan changes only which package's *data* is archived and restored. `launcher_component` still names the active HOME and `set_home_component` still applies it.
- Files under `provision/root/` are already bundled by `cas.spec`; this plan adds **no new files** there.

---

### Task 1: `has_layout` + `layout_launcher` in `lib-root.sh`

**Files:**
- Modify: `provision/root/lib-root.sh` (add next to `homescreen_apps`, which the new code reuses)
- Test: `tests/test_homescreen_apps.sh` (extend — it already sources `lib-root.sh` and exercises the homescreen helpers)

**Interfaces:**
- Consumes: `homescreen_apps <launcher_data_dir>` — existing; prints deduped placed-app package names found in a launcher data dir, empty when there are none.
- Produces, used by Tasks 2-3:
  - `has_layout <data_dir>` → returns 0 when that dir holds a real layout (i.e. `homescreen_apps` finds at least one placed-app reference), non-zero otherwise. Prints nothing.
  - `layout_launcher <home_pkg> [override]` → prints the package whose data holds the icon/folder layout; prints nothing and returns 1 when none qualifies. Resolution order: `override` (when installed) → `home_pkg` (when its data dir passes `has_layout`) → each `HOME_LAUNCHERS` entry (when installed **and** passes `has_layout`). Honours `DATA_ROOT` (default `/data/data`).
  - `HOME_LAUNCHERS` → space-separated fallback list, initially `com.android.launcher3`.

**Why `home_pkg` is a parameter rather than an internal `home_launcher()` call:** it keeps the function free of `cmd package resolve-activity`, so the test drives the real code with nothing but a `pm` stub and `DATA_ROOT` — the same shape `game_launcher` tests use.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_homescreen_apps.sh`, above its final summary/exit lines. If that file has no `pm` stub yet, add the `INSTALLED`/`pm()` pair shown here (it is copied from `tests/test_game_launcher.sh:11`); if it already defines one, reuse it rather than redefining.

```sh
# === has_layout / layout_launcher ===============================================================
# THE BUG: capture.sh used home_launcher() to pick whose /data/data becomes launcher_data.tar. The AYN
# Thor's HOME is xyz.blacksheep.mjolnir — a HOME-key shim with an EMPTY data dir — so the golden archived
# 4608 bytes of nothing while the real folder grid sat in com.android.launcher3. Four sibling goldens
# (RP6 512/256, AIR X, Odin 3) all recorded com.android.launcher3 with ~1 MB; the Thor alone differed.
INSTALLED=""                                   # space-separated pkgs pm should report as installed
pm(){ case "$1" in path) case " $INSTALLED " in *" $2 "*) return 0;; *) return 1;; esac;; *) return 0;; esac; }

lay="$tmp/lay"
# a REAL launcher data dir: a favorites DB carrying placed-app references
mkdir -p "$lay/com.android.launcher3/databases"
printf 'component=org.ppsspp.ppsspp/.Main package=com.retroarch.aarch64\n' \
  > "$lay/com.android.launcher3/databases/launcher.db"
# a SHIM's data dir: present, but no placed-app references anywhere (the Thor's Mjolnir)
mkdir -p "$lay/xyz.blacksheep.mjolnir/shared_prefs" "$lay/xyz.blacksheep.mjolnir/files"
printf 'x' > "$lay/xyz.blacksheep.mjolnir/files/profileInstalled"

has_layout "$lay/com.android.launcher3" || { echo "FAIL: real launcher dir not detected as a layout"; fail=1; }
has_layout "$lay/xyz.blacksheep.mjolnir" && { echo "FAIL: empty shim dir counted as a layout"; fail=1; }
has_layout "$lay/does.not.exist"         && { echo "FAIL: missing dir counted as a layout"; fail=1; }

INSTALLED="com.android.launcher3 xyz.blacksheep.mjolnir com.oem.pinned"
# (1) the HOME app owns the layout -> it wins. This is the path all four WORKING goldens take, so this
#     assertion is the back-compat guard: it must keep passing unchanged.
got="$(DATA_ROOT="$lay" layout_launcher com.android.launcher3)"
[ "$got" = "com.android.launcher3" ] || { echo "FAIL(1 home owns layout): [$got]"; fail=1; }

# (2) THE THOR CASE: HOME is a shim with no layout -> fall back to the real launcher
got="$(DATA_ROOT="$lay" layout_launcher xyz.blacksheep.mjolnir)"
[ "$got" = "com.android.launcher3" ] || { echo "FAIL(2 shim HOME falls back): [$got]"; fail=1; }

# (3) override pins an installed package, even when the fallback would have hit
mkdir -p "$lay/com.oem.pinned/databases"
printf 'component=com.foo.bar/.Main\n' > "$lay/com.oem.pinned/databases/launcher.db"
got="$(DATA_ROOT="$lay" layout_launcher xyz.blacksheep.mjolnir com.oem.pinned)"
[ "$got" = "com.oem.pinned" ] || { echo "FAIL(3 override): [$got]"; fail=1; }

# (4) an override that is NOT installed is ignored, not blindly trusted
got="$(DATA_ROOT="$lay" layout_launcher xyz.blacksheep.mjolnir com.not.installed)"
[ "$got" = "com.android.launcher3" ] || { echo "FAIL(4 uninstalled override ignored): [$got]"; fail=1; }

# (5) nothing qualifies -> empty output AND non-zero rc (caller must warn, never capture an empty layout)
empty="$tmp/empty"; mkdir -p "$empty/xyz.blacksheep.mjolnir"
got="$(DATA_ROOT="$empty" layout_launcher xyz.blacksheep.mjolnir)"
[ -z "$got" ] || { echo "FAIL(5 none qualifies): [$got]"; fail=1; }
DATA_ROOT="$empty" layout_launcher xyz.blacksheep.mjolnir >/dev/null 2>&1 \
  && { echo "FAIL(5 rc): layout_launcher returned 0 with no layout anywhere"; fail=1; }

# (6) the fallback must be INSTALLED, not merely present in the data root
INSTALLED="xyz.blacksheep.mjolnir"
got="$(DATA_ROOT="$lay" layout_launcher xyz.blacksheep.mjolnir)"
[ -z "$got" ] || { echo "FAIL(6 uninstalled fallback): [$got]"; fail=1; }
INSTALLED="com.android.launcher3 xyz.blacksheep.mjolnir com.oem.pinned"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd tests && sh test_homescreen_apps.sh`
Expected: FAIL — `has_layout: not found` / `layout_launcher: not found`, plus `FAIL(1 home owns layout): []`, exit 1.

- [ ] **Step 3: Write the implementation**

In `provision/root/lib-root.sh`, immediately **after** the `homescreen_apps()` function (it ends with the `} | sort -u` line and its closing `}`), insert:

```sh
# Fallback launchers to consult when the HOME app owns no layout of its own. Every working golden on the
# library drive (RP6 512/256, MANGMI AIR X, Odin 3) independently recorded com.android.launcher3, and the
# AYN Thor's factory dumps name it too (pm default_home + the HOME role holder) — this is the fleet's real
# launcher, not a guess.
HOME_LAUNCHERS="com.android.launcher3"
# has_layout <data_dir> — does this app's data actually hold a homescreen ARRANGEMENT? Reuses the
# homescreen_apps token scan: a favorites DB names the apps placed on the grid, so a dir yielding no
# placed-app references holds no layout. Cheap, launcher-family-agnostic, no sqlite3.
has_layout(){ [ -d "$1" ] && [ -n "$(homescreen_apps "$1")" ]; }
# layout_launcher <home_pkg> [override] — the package whose data holds the icon/folder LAYOUT, which is
# NOT always the HOME app. The AYN Thor's HOME is xyz.blacksheep.mjolnir, a HOME-key shim whose data dir
# is empty; capture followed resolve-activity to it and archived 4608 bytes of nothing while the real grid
# sat in com.android.launcher3. Resolution: override (if installed) -> the HOME app (if its data really
# holds a layout — the path every working golden takes) -> the first installed HOME_LAUNCHERS entry that
# does. Prints nothing and returns 1 when none qualifies, so the caller warns instead of capturing an
# empty layout. DATA_ROOT overrides /data/data for off-device tests, as in game_launcher.
layout_launcher(){
  _ll_home="$1"; _ll_ov="$2"; _ll_dr="${DATA_ROOT:-/data/data}"
  if [ -n "$_ll_ov" ] && pm path "$_ll_ov" >/dev/null 2>&1; then echo "$_ll_ov"; return 0; fi
  if [ -n "$_ll_home" ] && has_layout "$_ll_dr/$_ll_home"; then echo "$_ll_home"; return 0; fi
  for _ll_p in $HOME_LAUNCHERS; do
    [ "$_ll_p" = "$_ll_home" ] && continue
    if pm path "$_ll_p" >/dev/null 2>&1 && has_layout "$_ll_dr/$_ll_p"; then echo "$_ll_p"; return 0; fi
  done
  return 1
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd tests && sh test_homescreen_apps.sh`
Expected: `PASS: homescreen_apps` (the file's existing summary line), exit 0.

- [ ] **Step 5: Run the whole shell suite and check syntax**

Run:
```bash
cd tests && for f in test_*.sh; do echo "--- $f"; sh "$f" || echo "FAILED: $f"; done
sh -n provision/root/lib-root.sh && echo SYNTAX-OK
```
Expected: no `FAILED:` line, then `SYNTAX-OK`.

- [ ] **Step 6: Commit**

```bash
git add provision/root/lib-root.sh tests/test_homescreen_apps.sh
git commit -m "feat(homescreen): resolve the package that actually owns the layout"
```

---

### Task 2: `capture.sh` archives the layout owner

**Files:**
- Modify: `provision/root/capture.sh` — the `@homescreen` flag parse, and the homescreen block's `LP=` line, `meta` write and `homescreen_bundle_apps` call
- Test: `tests/test_homescreen_apps.sh`

**Interfaces:**
- Consumes: `layout_launcher <home_pkg> [override]`, `has_layout <data_dir>` (Task 1); existing `home_launcher`, `home_component`, `app_uid`, `homescreen_bundle_apps`, `manifest_flag`.
- Produces, for Task 3: `homescreen/meta` with `launcher_pkg` = **the layout owner** (whose data is inside `launcher_data.tar`), `launcher_uid` = that package's uid, and `launcher_component` = **the active HOME** component (unchanged meaning). On every existing golden all three still name the same package.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_homescreen_apps.sh`, above its final summary/exit lines:

```sh
# === capture.sh: meta records the LAYOUT OWNER, the component still records the HOME app =========
# Replicates capture.sh's meta write (that script only runs as root on a device). The two keys answer
# DIFFERENT questions and must be allowed to disagree: launcher_pkg = whose data is in the tar,
# launcher_component = what set-home-activity gets. On the Thor they legitimately differ.
capmeta="$tmp/capmeta"; mkdir -p "$capmeta"
HOMEPKG="xyz.blacksheep.mjolnir"
LP="$(DATA_ROOT="$lay" layout_launcher "$HOMEPKG")"
{ echo "launcher_pkg=$LP"; echo "launcher_uid=10091"
  echo "launcher_component=$HOMEPKG/.HomeActivity"; } > "$capmeta/meta"
[ "$(sed -n 's/^launcher_pkg=//p' "$capmeta/meta")" = "com.android.launcher3" ] \
  || { echo "FAIL: meta launcher_pkg is not the layout owner"; fail=1; }
[ "$(sed -n 's/^launcher_component=//p' "$capmeta/meta")" = "xyz.blacksheep.mjolnir/.HomeActivity" ] \
  || { echo "FAIL: meta launcher_component lost the golden's HOME choice"; fail=1; }

# back-compat: when the HOME app DOES own the layout (all four working goldens), both keys agree
HOMEPKG="com.android.launcher3"
LP="$(DATA_ROOT="$lay" layout_launcher "$HOMEPKG")"
[ "$LP" = "$HOMEPKG" ] || { echo "FAIL(back-compat): layout owner should equal HOME here, got [$LP]"; fail=1; }

# @homescreen parsing: off disables, a dotted value PINS the layout owner, on/absent leave it alone
parse_hs(){ FHS=on; OVHS=""; case "$1" in "") : ;; off) FHS=off ;; on) : ;; *.*) OVHS="$1" ;; esac
            echo "$FHS/$OVHS"; }
[ "$(parse_hs '')"                = "on/" ]                     || { echo "FAIL(@homescreen absent)"; fail=1; }
[ "$(parse_hs on)"                = "on/" ]                     || { echo "FAIL(@homescreen on)"; fail=1; }
[ "$(parse_hs off)"               = "off/" ]                    || { echo "FAIL(@homescreen off)"; fail=1; }
[ "$(parse_hs com.oem.pinned)"    = "on/com.oem.pinned" ]       || { echo "FAIL(@homescreen pin)"; fail=1; }
```

- [ ] **Step 2: Run the test — shape lock, expected PASS**

Run: `cd tests && sh test_homescreen_apps.sh`
Expected: PASS. `capture.sh` only runs as root on a device and cannot be invoked in CI, so this block replicates its meta write and flag parse; it passes as soon as Task 1's helpers exist. **Your job in Step 3 is to make `capture.sh` match the shape locked here, exactly.** Do not contort the test to fail first.

- [ ] **Step 3: Write the implementation**

In `provision/root/capture.sh`, replace the `@homescreen` flag parse:

```sh
FHS=on
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then
  [ "$(manifest_flag "$CAS_MANIFEST" homescreen)" = off ] && FHS=off
fi
```

with:

```sh
# @homescreen: off disables; a DOTTED value pins the layout-owner package (same override idiom as
# "@gamelauncher <pkg>"); on/absent means "resolve it". The pin is the escape hatch for a unit whose real
# launcher is neither the HOME app nor anything in HOME_LAUNCHERS.
FHS=on; OVHS=""
if [ -n "${CAS_MANIFEST:-}" ] && [ -f "$CAS_MANIFEST" ]; then
  v="$(manifest_flag "$CAS_MANIFEST" homescreen)"
  case "$v" in "") : ;; off) FHS=off ;; on) : ;; *.*) OVHS="$v" ;; esac
fi
```

Then, inside the homescreen block, replace:

```sh
  HS="$P/homescreen"; mkdir -p "$HS"
  LP="$(home_launcher)"
  if [ -n "$LP" ] && [ -d "/data/data/$LP" ]; then
```

with:

```sh
  HS="$P/homescreen"; mkdir -p "$HS"
  # WHO IS HOME vs WHO OWNS THE LAYOUT are different questions. The AYN Thor answers them with different
  # packages: Mjolnir is HOME (a HOME-key shim with an empty data dir) while com.android.launcher3 holds
  # the folder grid. Capturing the HOME app's data blindly archived 4608 bytes of nothing.
  HOMEPKG="$(home_launcher)"
  LP="$(layout_launcher "$HOMEPKG" "$OVHS")"
  if [ -n "$LP" ] && [ -d "/data/data/$LP" ]; then
    [ -n "$HOMEPKG" ] && [ "$HOMEPKG" != "$LP" ] && \
      log "homescreen: HOME app is $HOMEPKG but the arrangement lives in $LP — capturing $LP's layout (HOME choice preserved)"
```

The existing `{ echo "launcher_pkg=$LP"; … }` meta write, the `tar` of `/data/data/$LP`, and the `homescreen_bundle_apps "/data/data/$LP" "$P" "$LP"` call all now operate on the layout owner **with no further edits** — they already interpolate `$LP`.

Finally, replace the failure message so it names the real condition:

```sh
    warn "no home launcher resolved (or it has no data dir) — homescreen layout NOT captured"
```

with:

```sh
    warn "homescreen: no package with an actual layout found (HOME app '$HOMEPKG' holds none, and no HOME_LAUNCHERS fallback qualified) — layout NOT captured. Pin one with '@homescreen <pkg>' if this unit's launcher is unusual."
```

- [ ] **Step 4: Run the tests and check syntax**

Run:
```bash
cd tests && for f in test_*.sh; do echo "--- $f"; sh "$f" || echo "FAILED: $f"; done
sh -n provision/root/capture.sh && echo SYNTAX-OK
```
Expected: no `FAILED:` line, then `SYNTAX-OK`.

- [ ] **Step 5: Commit**

```bash
git add provision/root/capture.sh tests/test_homescreen_apps.sh
git commit -m "feat(homescreen): capture the layout owner, not whichever app is HOME"
```

---

### Task 3: `restore.sh` stops gating the layout on "is this package HOME?"

**Files:**
- Modify: `provision/root/restore.sh` — the homescreen block's `LP`/`LC`/`CUR` resolution, the `set_home_component` condition, and the gate chain
- Test: `tests/test_home_app.sh` (it already covers `home_component`/`set_home_component` and the capture/restore wiring)

**Interfaces:**
- Consumes: `homescreen/meta` keys from Task 2 — `launcher_pkg` (layout owner), `launcher_component` (active HOME), `launcher_uid`.
- Produces: nothing for later tasks; this is the last one.

**The bug being removed:** the chain contains `elif [ -n "$CUR" ] && [ "$CUR" != "$LP" ]; then warn … SKIP`. Once HOME and layout owner legitimately differ, that comparison is false-by-construction on a Thor — `CUR` becomes Mjolnir after `set_home_component`, `LP` is launcher3 — so the layout **and the wallpaper, which lives inside the same block,** would be skipped every single time.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_home_app.sh`, above its final summary/exit lines:

```sh
# === restore.sh: the layout goes to the LAYOUT OWNER, whoever happens to be HOME ==================
# Replicates restore.sh's homescreen gate. The old chain skipped the layout unless the unit's CURRENT
# home app equalled the golden's launcher_pkg. Once those are different roles that test is wrong by
# construction: on a Thor the unit's HOME becomes Mjolnir while the layout owner is launcher3, so the
# layout AND the wallpaper (restored inside the same block) were skipped every time.
INSTALLED="com.android.launcher3 xyz.blacksheep.mjolnir"
pm(){ case "$1" in path) case " $INSTALLED " in *" $2 "*) return 0;; *) return 1;; esac;; *) return 0;; esac; }

# decide(): the gate as it must now behave. $1=launcher_pkg $2=launcher_component $3=this unit's HOME
decide(){ _lp="$1"; _lc="$2"; _cur="$3"; _lhome="${_lc%%/*}"
  _sethome=no
  [ -n "$_lc" ] && [ -n "$_lhome" ] && [ -n "$_cur" ] && [ "$_cur" != "$_lhome" ] && _sethome=yes
  if [ -z "$_lp" ]; then echo "$_sethome/skip-no-pkg"; return; fi
  if ! pm path "$_lp" >/dev/null 2>&1; then echo "$_sethome/skip-absent"; return; fi
  echo "$_sethome/restore-$_lp"; }

# THE THOR CASE: golden's layout owner is launcher3, its HOME is Mjolnir, the fresh unit boots launcher3.
# HOME must be switched to Mjolnir AND the layout must still land in launcher3.
[ "$(decide com.android.launcher3 xyz.blacksheep.mjolnir/.HomeActivity com.android.launcher3)" \
  = "yes/restore-com.android.launcher3" ] || { echo "FAIL(thor): $(decide com.android.launcher3 xyz.blacksheep.mjolnir/.HomeActivity com.android.launcher3)"; fail=1; }

# …and once HOME is already Mjolnir, the layout STILL restores (the old CUR != LP test broke exactly here)
[ "$(decide com.android.launcher3 xyz.blacksheep.mjolnir/.HomeActivity xyz.blacksheep.mjolnir)" \
  = "no/restore-com.android.launcher3" ] || { echo "FAIL(thor 2nd run): $(decide com.android.launcher3 xyz.blacksheep.mjolnir/.HomeActivity xyz.blacksheep.mjolnir)"; fail=1; }

# BACK-COMPAT (the four working goldens): HOME and layout owner are the same package
[ "$(decide com.android.launcher3 com.android.launcher3/.uioverrides.QuickstepLauncher com.android.launcher3)" \
  = "no/restore-com.android.launcher3" ] || { echo "FAIL(rp6 back-compat)"; fail=1; }

# a golden with no launcher_component at all (captured before that key existed) still restores its layout
[ "$(decide com.android.launcher3 '' com.android.launcher3)" = "no/restore-com.android.launcher3" ] \
  || { echo "FAIL(legacy no-component golden)"; fail=1; }

# layout owner absent from this unit -> skip (nothing to extract into)
INSTALLED="xyz.blacksheep.mjolnir"
[ "$(decide com.android.launcher3 xyz.blacksheep.mjolnir/.HomeActivity xyz.blacksheep.mjolnir)" \
  = "no/skip-absent" ] || { echo "FAIL(absent layout owner)"; fail=1; }
INSTALLED="com.android.launcher3 xyz.blacksheep.mjolnir"

# payload with no launcher_pkg -> skip
[ "$(decide '' xyz.blacksheep.mjolnir/.HomeActivity com.android.launcher3)" = "yes/skip-no-pkg" ] \
  || { echo "FAIL(no launcher_pkg)"; fail=1; }
```

- [ ] **Step 2: Run the test — shape lock, expected PASS**

Run: `cd tests && sh test_home_app.sh`
Expected: PASS. `restore.sh` only runs as root on a device, so this block replicates its gate; **Step 3 must make `restore.sh` match the shape locked here.** Do not contort the test to fail first.

- [ ] **Step 3: Write the implementation**

In `provision/root/restore.sh`, replace:

```sh
  LP="$(sed -n 's/^launcher_pkg=//p' "$HS/meta" 2>/dev/null)"
  LC="$(sed -n 's/^launcher_component=//p' "$HS/meta" 2>/dev/null)"
  CUR="$(home_launcher)"
```

with:

```sh
  # TWO ROLES, TWO KEYS. launcher_pkg = whose data is inside launcher_data.tar (the LAYOUT OWNER);
  # launcher_component = what set-home-activity gets (the golden's HOME app). They are the same package
  # on every golden captured before 2026-07-22 and on every unit whose launcher is its own HOME, but the
  # AYN Thor separates them: HOME is a Mjolnir shim, the arrangement lives in com.android.launcher3.
  LP="$(sed -n 's/^launcher_pkg=//p' "$HS/meta" 2>/dev/null)"
  LC="$(sed -n 's/^launcher_component=//p' "$HS/meta" 2>/dev/null)"
  LHOME="${LC%%/*}"                       # the golden's HOME package, derived from its component
  CUR="$(home_launcher)"
```

Then replace the `set_home_component` condition (currently keyed on `$LP`) so it compares the unit's HOME against the golden's **HOME**, not against the layout owner:

```sh
  if [ -n "$LC" ] && [ -n "$LP" ] && [ -n "$CUR" ] && [ "$CUR" != "$LP" ]; then
    if set_home_component "$LC"; then
      ok "homescreen: default home app $CUR -> $LP"
      CUR="$(home_launcher)"
```

becomes:

```sh
  if [ -n "$LC" ] && [ -n "$LHOME" ] && [ -n "$CUR" ] && [ "$CUR" != "$LHOME" ]; then
    if set_home_component "$LC"; then
      ok "homescreen: default home app $CUR -> $LHOME"
      CUR="$(home_launcher)"
```

Finally, delete the launcher-equality arm from the gate chain — this is the actual fix:

```sh
  elif [ -n "$CUR" ] && [ "$CUR" != "$LP" ]; then
    warn "homescreen: this unit's launcher ($CUR) != golden's ($LP) — would not apply, SKIP (different family?)"
```

so the chain reads:

```sh
  if [ -z "$LP" ]; then
    warn "homescreen: payload has no launcher_pkg — skip"
  elif ! pm path "$LP" >/dev/null 2>&1; then
    warn "homescreen: layout owner $LP not present on this unit — skip"
```

**Do not add any replacement equality check.** The layout is restored into the layout owner *whether or not it is the active HOME* — that is the requirement (spec §2.3): Mjolnir stays HOME, launcher3 still receives the arrangement, so switching HOME by hand later reveals it. The safety the deleted arm provided is retained by the two surviving gates: the tar contains exactly `$LP/…`, and it is only extracted when `pm path "$LP"` confirms that package exists here.

- [ ] **Step 4: Run the full suite and check syntax**

Run:
```bash
cd tests && for f in test_*.sh; do echo "--- $f"; sh "$f" || echo "FAILED: $f"; done
cd tests && python -m unittest discover -p "test_*.py" 2>&1 | tail -3
sh -n provision/root/restore.sh && echo SYNTAX-OK
```
Expected: no `FAILED:` line; the Python suite reports `OK` (762 tests as of `cbb42e0` — report the real number, do not assume); then `SYNTAX-OK`.

- [ ] **Step 5: Commit**

```bash
git add provision/root/restore.sh tests/test_home_app.sh
git commit -m "fix(homescreen): restore the layout to its owner regardless of which app is HOME"
```

---

## Verification gate

Tests prove the shell contract on the PC. They do **not** prove the end-to-end claim. Before this is called done:

1. **Re-Save the golden Thor.** `homescreen/launcher_data.tar` must jump from 4,608 B into the ~1 MB range the four sibling profiles show, and `homescreen/meta` must read `launcher_pkg=com.android.launcher3` with `launcher_component=xyz.blacksheep.mjolnir/.HomeActivity`.
2. **Download to a fresh Thor.** Mjolnir must still be the home app, and `/data/data/com.android.launcher3` must hold the golden's layout — confirm by switching HOME to launcher3 by hand and checking the organised folders appear.
3. **No RP6 regression.** Re-Save an RP6 golden: `launcher_pkg` must stay `com.android.launcher3` and the tar stay ~1 MB.

Bench gate stays **OPEN** until step 2 passes on hardware. The `com.android.launcher3` fallback is inferred from four sibling profiles and the Thor's factory dumps, not from a Thor golden containing launcher3 data — that golden has none, precisely because of this bug.
