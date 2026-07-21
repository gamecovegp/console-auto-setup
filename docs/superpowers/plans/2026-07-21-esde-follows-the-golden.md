# ES-DE Follows the Golden — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture and restore ES-DE's frontend config from wherever ES-DE actually lives on a unit (SD card on the AYN Thor, internal storage on the RP6), and carry the `ES-DE/scripts/` tree so ES-DE Companion works on a provisioned unit with no hand setup.

**Architecture:** Four pure helpers go into `provision/root/lib-root.sh` (the shared device-side library both scripts source). `capture.sh` probes for the golden's ES-DE home, records its *kind* (`sd` or `internal`) in `global.meta`, and captures `es_settings.xml` + `es_scripts.tar` from it. `restore.sh` never probes — it reads the recorded kind and resolves the equivalent location on the unit. A golden with no `esde_home=` key reads as `internal`, which is byte-for-byte today's behaviour.

**Tech Stack:** POSIX shell (Android `toybox`/`mksh` on device; `sh` on the CI runners). Tests are `tests/test_*.sh`, run by `.github/workflows/build.yml:87` as `sh "$f"` on the Linux and macOS legs only.

**Spec:** `docs/superpowers/specs/2026-07-21-esde-follows-the-golden-design.md`

## Global Constraints

- **POSIX `sh` only.** CI invokes these with `sh`, not bash — no `[[ ]]`, no arrays, no `local`, no `${var,,}`. Use `case` for pattern matching.
- **Never `sed -i`.** BSD/macOS `sed` reads the next argument as a backup suffix. Use `sed SCRIPT f > f.tmp && mv f.tmp f` — the idiom already in `restore.sh:195`.
- **No `&&`/`||`/`;` inside a `su -c` command string** (adb space-joins argv). Not expected in this work, but it is the standing rule.
- **Prefix new local variables in `lib-root.sh` functions** (`_eh_`, `_ef_`, `_esv_`) — the file has no `local`, so every variable is global and a bare `d`/`n` would collide with a caller's.
- **Additive capture.** A missing or unreadable ES-DE tree must `warn`, never bump `CFAIL` in `capture.sh`.
- Files under `provision/root/` are already bundled by `cas.spec`; this plan adds **no new files** there, so no spec/build changes.

---

### Task 1: ES-DE home helpers in `lib-root.sh`

**Files:**
- Modify: `provision/root/lib-root.sh:37-38` (replace the `ES_SETTINGS_PATH` constant)
- Test: `tests/test_esde_settings.sh` (extend; currently 55 lines, does not source `lib-root.sh`)

**Interfaces:**
- Consumes: nothing.
- Produces, all used by Tasks 2-5:
  - `esde_home [storage_root]` → prints the ES-DE dir on THIS device (e.g. `/storage/9C33-6BBD/ES-DE`), rc 1 + no output when none exists. Probes external volumes first, then internal. Honours `$CAS_STORAGE_ROOT` (default `/storage`) so tests can point it at a temp tree.
  - `esde_home_kind <dir>` → prints `internal` or `sd`.
  - `esde_home_for <kind> <sd_serial> [storage_root]` → prints the ES-DE dir for THIS unit given the golden's kind, rc 1 + no output when kind is `sd` and the unit has no card.
  - `es_setting_value <key> <file>` → prints the `value="…"` of an `es_settings.xml` line, empty when absent or empty.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_esde_settings.sh`. Insert the two `ROOT=`/`.` lines directly after the `#!/usr/bin/env bash` header comment block (before the existing `fail=0`), and append the assertion block at the end of the file, **above** the final `[ "$fail" -eq 0 ] && echo …` summary line:

```sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
```

```sh
# --- esde_home / esde_home_kind / esde_home_for / es_setting_value ------------------------------------
# ES-DE Android's home is a USER PICK: the AYN Thor keeps its whole tree on the SD card, the RP6 on
# internal. CAS used to hardcode the internal path, so an SD-home golden captured nothing at all.
eq(){ [ "$2" = "$3" ] || { echo "FAIL($1): got '$2', want '$3'"; fail=1; }; }

# a Thor-shaped device: ES-DE on the card, nothing internal
sdroot="$tmp/sdunit"; mkdir -p "$sdroot/9C33-6BBD/ES-DE/settings" "$sdroot/emulated/0"
eq "esde_home sd"      "$(esde_home "$sdroot")"                      "$sdroot/9C33-6BBD/ES-DE"
eq "kind sd"           "$(esde_home_kind "$(esde_home "$sdroot")")"  "sd"

# an RP6-shaped device: ES-DE internal, a card present but with no ES-DE tree
inroot="$tmp/inunit"; mkdir -p "$inroot/emulated/0/ES-DE/settings" "$inroot/ABCD-1234"
eq "esde_home internal" "$(esde_home "$inroot")"                      "$inroot/emulated/0/ES-DE"
eq "kind internal"      "$(esde_home_kind "$(esde_home "$inroot")")"  "internal"

# no ES-DE anywhere -> empty output AND non-zero rc
noroot="$tmp/noesde"; mkdir -p "$noroot/emulated/0" "$noroot/ABCD-1234"
eq "esde_home none" "$(esde_home "$noroot")" ""
esde_home "$noroot" >/dev/null 2>&1 && { echo "FAIL: esde_home returned 0 with no ES-DE"; fail=1; }

# restore-side resolution: the golden's KIND + this unit's serial -> this unit's ES-DE dir
eq "for sd"       "$(esde_home_for sd 6ED25E36D25E032F "$tmp/u")" "$tmp/u/6ED25E36D25E032F/ES-DE"
eq "for internal" "$(esde_home_for internal 6ED25E36D25E032F "$tmp/u")" "$tmp/u/emulated/0/ES-DE"
# sd-home golden onto a unit with NO card -> empty + non-zero (restore must warn and skip, not guess)
eq "for sd no card" "$(esde_home_for sd '' "$tmp/u")" ""
esde_home_for sd '' "$tmp/u" >/dev/null 2>&1 && { echo "FAIL: esde_home_for sd returned 0 with no card"; fail=1; }

# es_setting_value reads one key out of an es_settings.xml
esv="$tmp/esv.xml"
printf '%s\n' '<bool name="CustomEventScripts" value="true" />' \
              '<string name="MediaDirectory" value="" />' \
              '<string name="ROMDirectory" value="/storage/GOLD-1234/ROMs" />' > "$esv"
eq "esv bool"   "$(es_setting_value CustomEventScripts "$esv")" "true"
eq "esv empty"  "$(es_setting_value MediaDirectory "$esv")"     ""
eq "esv path"   "$(es_setting_value ROMDirectory "$esv")"       "/storage/GOLD-1234/ROMs"
eq "esv absent" "$(es_setting_value NoSuchKey "$esv")"          ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd tests && sh test_esde_settings.sh`
Expected: FAIL — `esde_home: not found` / `esde_home_kind: not found` errors and `FAIL(esde_home sd): got '', want '…'`, exit 1.

- [ ] **Step 3: Write minimal implementation**

In `provision/root/lib-root.sh`, replace lines 37-38:

```sh
# The one internal ES-DE file the golden carries (the rest of ES-DE rides the SD card).
ES_SETTINGS_PATH="/storage/emulated/0/ES-DE/settings/es_settings.xml"
```

with:

```sh
# WHERE ES-DE LIVES. ES-DE Android's home directory is a USER PICK, not a fixed path: the AYN Thor keeps
# its whole tree (settings/scripts/downloaded_media) on the SD card, while the RP6 keeps it on internal
# storage. This used to be a hardcoded /storage/emulated/0 constant, so on an SD-home unit capture's
# `[ -f ]` test silently failed and the golden shipped with NO es_settings.xml — the ES-DE Companion's
# event-script toggles were lost and every provisioned unit needed hand setup.
# CAS_STORAGE_ROOT overrides /storage for off-device tests (same idiom as CAS_INST_DIR in install_apks).
# esde_home [storage_root] — print this device's ES-DE dir; empty + rc 1 when there is none. External
# volumes are probed FIRST (a handheld normally holds ES-DE on its card), internal last.
esde_home(){
  _eh_root="${1:-${CAS_STORAGE_ROOT:-/storage}}"
  for _eh_d in "$_eh_root"/*/; do
    _eh_d="${_eh_d%/}"; _eh_n="${_eh_d##*/}"
    case "$_eh_n" in emulated|self|'*') continue;; esac
    [ -d "$_eh_d/ES-DE/settings" ] && { echo "$_eh_d/ES-DE"; return 0; }
  done
  [ -d "$_eh_root/emulated/0/ES-DE/settings" ] && { echo "$_eh_root/emulated/0/ES-DE"; return 0; }
  return 1
}
# esde_home_kind <dir> — which KIND of home that path is. Recorded in the golden's global.meta so restore
# can resolve the same kind of location on a unit whose card has a DIFFERENT volume id.
esde_home_kind(){ case "$1" in */emulated/0|*/emulated/0/*) echo internal;; *) echo sd;; esac; }
# esde_home_for <kind> <sd_serial> [storage_root] — the ES-DE dir on THIS unit for a golden captured with
# <kind>. RESTORE NEVER PROBES: it obeys the kind the golden recorded ("follow the golden"). Empty + rc 1
# when the golden was SD-home and this unit has no card — the caller must warn and skip, never guess.
esde_home_for(){
  _ef_root="${3:-${CAS_STORAGE_ROOT:-/storage}}"
  case "$1" in
    internal) echo "$_ef_root/emulated/0/ES-DE"; return 0;;
    sd) if [ -n "$2" ]; then echo "$_ef_root/$2/ES-DE"; return 0; fi; return 1;;
  esac
  return 1
}
# es_setting_value <key> <es_settings.xml> — the value="…" of one setting line (empty if absent OR empty).
# es_settings.xml is a FLAT list of <type name=".." value=".." /> lines with no root wrapper.
es_setting_value(){ sed -n "s/.*name=\"$1\"[[:space:]]*value=\"\([^\"]*\)\".*/\1/p" "$2" 2>/dev/null | head -1; }
```

Note the `'*')` arm in the `case`: when a glob matches nothing, POSIX `sh` leaves the literal `"$_eh_root"/*/` in place, and that guard skips it.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd tests && sh test_esde_settings.sh`
Expected: `test_esde_settings: ALL PASS`, exit 0.

- [ ] **Step 5: Check nothing else referenced the deleted constant**

Run: `grep -rn "ES_SETTINGS_PATH" provision/ cas/ tests/`
Expected: exactly two hits, both in `provision/root/capture.sh` (lines ~114-115) — Task 2 replaces them. If `restore.sh` or any `.py` appears, stop and reconcile before continuing.

- [ ] **Step 6: Commit**

```bash
git add provision/root/lib-root.sh tests/test_esde_settings.sh
git commit -m "feat(esde): locate ES-DE's home instead of hardcoding internal storage"
```

---

### Task 2: `capture.sh` records the home kind and captures from it

**Files:**
- Modify: `provision/root/capture.sh:28-32` (the `global.meta` block) and `:111-118` (the ES-DE capture block)
- Test: `tests/test_esde_settings.sh`

**Interfaces:**
- Consumes: `esde_home`, `esde_home_kind` (Task 1).
- Produces, for Task 3: payload files `es_settings.xml` (unchanged name) and `es_scripts.tar` (new — a tar whose single top-level member is `scripts`), plus the `global.meta` key `esde_home=sd|internal`. **The key is written only when an ES-DE home was found**; its absence means "internal" to Task 3.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_esde_settings.sh`, above the summary line:

```sh
# --- capture side: record the kind, tar the scripts tree ----------------------------------------------
# The golden carries the ES-DE/scripts/ tree (the 7 esdecompanion-*.sh hooks) so a unit provisions even
# when its SD image predates the Companion setup. Replicates capture.sh's snippet against a temp tree.
gsd="$tmp/golden"; mkdir -p "$gsd/9C33-6BBD/ES-DE/settings" "$gsd/emulated/0"
GH="$gsd/9C33-6BBD/ES-DE"
printf '%s\n' '<bool name="CustomEventScripts" value="true" />' > "$GH/settings/es_settings.xml"
for ev in game-start game-end game-select system-select \
          screensaver-start screensaver-end screensaver-game-select; do
  mkdir -p "$GH/scripts/$ev"; printf '#!/bin/sh\n' > "$GH/scripts/$ev/esdecompanion-$ev.sh"
done
PAY="$tmp/pay"; mkdir -p "$PAY"; printf 'golden_serial=9C33-6BBD\n' > "$PAY/global.meta"

ESDE_HOME="$(esde_home "$gsd")"
[ -n "$ESDE_HOME" ] && echo "esde_home=$(esde_home_kind "$ESDE_HOME")" >> "$PAY/global.meta"
[ -n "$ESDE_HOME" ] && [ -f "$ESDE_HOME/settings/es_settings.xml" ] && cp "$ESDE_HOME/settings/es_settings.xml" "$PAY/es_settings.xml"
if [ -n "$ESDE_HOME" ] && [ -d "$ESDE_HOME/scripts" ] && [ -n "$(ls -A "$ESDE_HOME/scripts" 2>/dev/null)" ]; then
  tar -cf "$PAY/es_scripts.tar" -C "$ESDE_HOME" scripts 2>/dev/null
fi

eq "capture kind" "$(sed -n 's/^esde_home=//p' "$PAY/global.meta")" "sd"
[ -f "$PAY/es_settings.xml" ] || { echo "FAIL: es_settings.xml not captured from the SD home"; fail=1; }
[ -f "$PAY/es_scripts.tar" ] || { echo "FAIL: es_scripts.tar not captured"; fail=1; }
n="$(tar -tf "$PAY/es_scripts.tar" 2>/dev/null | grep -c 'esdecompanion-.*\.sh$')"
eq "captured hooks" "$n" "7"
# the golden_serial line the existing pipeline depends on must survive the append
grep -q '^golden_serial=9C33-6BBD$' "$PAY/global.meta" || { echo "FAIL: global.meta clobbered"; fail=1; }
```

- [ ] **Step 2: Run the test — shape lock, expected PASS**

Run: `cd tests && sh test_esde_settings.sh`
Expected: PASS. This block replicates the snippet rather than calling `capture.sh` (that script only runs on a rooted device), so it passes as soon as Task 1's helpers exist — it is a **regression lock on the snippet's shape**, and Step 3 must make `capture.sh` match it exactly.

- [ ] **Step 3: Write the implementation**

In `provision/root/capture.sh`, immediately **after** the existing `} > "$P/global.meta"` line (~line 32), add:

```sh
# WHERE THIS GOLDEN KEEPS ES-DE (sd | internal). Restore resolves the same KIND of location on the unit
# rather than probing it — "follow the golden". Omitted when the golden has no ES-DE tree at all, and
# restore reads an absent key as "internal", which is exactly how every pre-2026-07-21 golden behaved.
ESDE_HOME="$(esde_home)"
[ -n "$ESDE_HOME" ] && echo "esde_home=$(esde_home_kind "$ESDE_HOME")" >> "$P/global.meta"
```

Then replace the ES-DE capture block (currently lines ~111-118, the `if [ -f "$ES_SETTINGS_PATH" ]` stanza) with:

```sh
# ES-DE: capture es_settings.xml (the per-system alternative-emulator picks + frontend settings, incl. the
# CustomEventScripts / CustomEventScriptsBrowsing toggles the ES-DE Companion needs) and the scripts/ tree
# (the Companion's 7 event hooks). NOT the multi-GB gamelists/themes/box-art tree — that rides the SD.
# Read from wherever ES-DE actually lives; the old hardcoded internal path captured NOTHING on the Thor.
if [ -z "$ESDE_HOME" ]; then
  log "ES-DE: no ES-DE home found on this golden — nothing to capture"
else
  if [ -f "$ESDE_HOME/settings/es_settings.xml" ]; then
    cp "$ESDE_HOME/settings/es_settings.xml" "$P/es_settings.xml" \
      && ok "captured ES-DE es_settings.xml from $ESDE_HOME ($(du -h "$P/es_settings.xml" 2>/dev/null | cut -f1))" \
      || warn "could not capture ES-DE es_settings.xml"
  else
    warn "ES-DE: $ESDE_HOME/settings/es_settings.xml missing — frontend settings NOT captured"
  fi
  # custom event scripts — tiny, and they make the golden self-contained: a unit whose SD image predates
  # the Companion setup still comes up with the hooks in place. Skipped when the dir is absent or EMPTY
  # (same rule as the INTERNAL_DIRS loop, so we never ship an empty dir that restore would recreate).
  if [ -d "$ESDE_HOME/scripts" ] && [ -n "$(ls -A "$ESDE_HOME/scripts" 2>/dev/null)" ]; then
    tar -cf "$P/es_scripts.tar" -C "$ESDE_HOME" scripts 2>/dev/null
    if tar -tf "$P/es_scripts.tar" >/dev/null 2>&1; then
      ok "captured ES-DE custom event scripts ($(tar -tf "$P/es_scripts.tar" 2>/dev/null | grep -c '\.sh$') script(s))"
    else
      warn "ES-DE es_scripts.tar looks corrupt — custom event scripts NOT captured"; rm -f "$P/es_scripts.tar"
    fi
  fi
fi
```

- [ ] **Step 4: Run the tests**

Run: `cd tests && sh test_esde_settings.sh && sh test_internal_dirs.sh`
Expected: both print `ALL PASS`, exit 0. (`test_internal_dirs.sh` also sources `lib-root.sh` and asserts the `INTERNAL_DIRS`/`internal_for` coupling — it must not regress.)

- [ ] **Step 5: Verify capture.sh still parses**

Run: `sh -n provision/root/capture.sh && echo SYNTAX-OK`
Expected: `SYNTAX-OK`.

- [ ] **Step 6: Commit**

```bash
git add provision/root/capture.sh tests/test_esde_settings.sh
git commit -m "feat(esde): capture settings + the scripts tree from the golden's real ES-DE home"
```

---

### Task 3: `restore.sh` resolves the unit's ES-DE home and restores both artefacts

**Files:**
- Modify: `provision/root/restore.sh:178-188` (the `ES_SET=` constant and the `2c-pre` restore stanza)
- Test: `tests/test_esde_settings.sh`

**Interfaces:**
- Consumes: `esde_home_for` (Task 1); payload `es_settings.xml`, `es_scripts.tar`, `global.meta` key `esde_home` (Task 2). `$SERIAL` and `$P` already exist in `restore.sh` (`:17`, `:19-22`).
- Produces, for Tasks 4-5: shell variables `EHK` (the golden's kind, defaulted to `internal`), `ES_HOME` (this unit's ES-DE dir, empty when unresolvable) and `ES_SET` (`$ES_HOME/settings/es_settings.xml`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_esde_settings.sh`, above the summary line:

```sh
# --- restore side: resolve THIS unit's home from the golden's kind ------------------------------------
# "Follow the golden": restore never probes the unit. An SD-home golden lands on the unit's OWN card
# (different volume id), an internal-home golden lands on internal, and a golden with NO esde_home key
# reads as internal — that back-compat default is what keeps every pre-existing RP6 golden working.
uroot="$tmp/unit"; mkdir -p "$uroot/AAAA-BBBB" "$uroot/emulated/0"
resolve(){ # $1 = payload dir, $2 = this unit's card serial -> echoes ES_HOME ("" if unresolvable)
  _k="$(sed -n 's/^esde_home=//p' "$1/global.meta" 2>/dev/null)"; [ -n "$_k" ] || _k=internal
  esde_home_for "$_k" "$2" "$uroot"
}
mkdir -p "$tmp/p_sd" "$tmp/p_int" "$tmp/p_old"
printf 'esde_home=sd\n'       > "$tmp/p_sd/global.meta"
printf 'esde_home=internal\n' > "$tmp/p_int/global.meta"
printf 'golden_serial=X\n'    > "$tmp/p_old/global.meta"        # pre-2026-07-21 golden: NO esde_home key
eq "resolve sd"       "$(resolve "$tmp/p_sd"  AAAA-BBBB)" "$uroot/AAAA-BBBB/ES-DE"
eq "resolve internal" "$(resolve "$tmp/p_int" AAAA-BBBB)" "$uroot/emulated/0/ES-DE"
eq "resolve legacy"   "$(resolve "$tmp/p_old" AAAA-BBBB)" "$uroot/emulated/0/ES-DE"
eq "resolve sd nocard" "$(resolve "$tmp/p_sd" '')"        ""

# restoring both artefacts into the resolved home
ES_HOME="$(resolve "$tmp/p_sd" AAAA-BBBB)"
cp "$PAY/es_settings.xml" "$tmp/es_src.xml"                      # reuse Task 2's captured payload
mkdir -p "$ES_HOME/settings"
cp "$tmp/es_src.xml" "$ES_HOME/settings/es_settings.xml"
tar -xf "$PAY/es_scripts.tar" -C "$ES_HOME" 2>/dev/null
[ -f "$ES_HOME/settings/es_settings.xml" ] || { echo "FAIL: settings not restored into the SD home"; fail=1; }
[ -x "$ES_HOME/scripts/game-start/esdecompanion-game-start.sh" ] \
  || [ -f "$ES_HOME/scripts/game-start/esdecompanion-game-start.sh" ] \
  || { echo "FAIL: game-start hook not restored"; fail=1; }
m="$(find "$ES_HOME/scripts" -name 'esdecompanion-*.sh' 2>/dev/null | wc -l | tr -d ' ')"
eq "restored hooks" "$m" "7"
```

- [ ] **Step 2: Run the test — shape lock, expected PASS**

Run: `cd tests && sh test_esde_settings.sh`
Expected: PASS (snippet-replication lock again — it depends only on Task 1 helpers). Step 3 must make `restore.sh` match this shape exactly. If it FAILS, Task 1 or 2 is wrong; fix that before continuing.

- [ ] **Step 3: Write the implementation**

In `provision/root/restore.sh`, replace the hardcoded constant (line ~178):

```sh
ES_SET="/storage/emulated/0/ES-DE/settings/es_settings.xml"
```

with:

```sh
# WHERE THIS UNIT'S ES-DE GOES. "Follow the golden": capture recorded the KIND of home the golden used
# (sd | internal) in global.meta; we resolve the same kind here against THIS unit's card serial. Restore
# deliberately does NOT probe the unit — a fresh unit has no ES-DE tree yet, so there would be nothing to
# find. An absent key means a pre-2026-07-21 golden: read it as internal, i.e. exactly today's behaviour.
EHK="$(sed -n 's/^esde_home=//p' "$P/global.meta" 2>/dev/null)"; [ -n "$EHK" ] || EHK=internal
ES_HOME="$(esde_home_for "$EHK" "$SERIAL")"
ES_SET="$ES_HOME/settings/es_settings.xml"
```

Then replace the `2c-pre` stanza (the `if echo "$RPKGS" | grep -q org.es_de.frontend && [ -f "$P/es_settings.xml" ]; then` block, lines ~183-188) with:

```sh
if [ -z "$ES_HOME" ]; then
  # SD-home golden, card-less unit. Cannot guess a location: writing to internal would produce a file
  # ES-DE never reads (the exact bug this whole change fixes). Warn loudly and skip — not a FAIL, a
  # card-less unit legitimately cannot host an SD-home ES-DE.
  warn "ES-DE: golden is SD-home but this unit has no SD card — frontend settings and custom event scripts NOT restored"
elif echo "$RPKGS" | grep -q org.es_de.frontend; then
  # 2c-pre) the golden's es_settings.xml: per-system alternative-emulator picks (3DS→Citra, DS→melonDS,
  # PS2→NetherSX2…) AND the CustomEventScripts/CustomEventScriptsBrowsing toggles the Companion needs.
  # Restored BEFORE the per-unit path rewrites below, so those land on top of the golden's settings.
  if [ -f "$P/es_settings.xml" ]; then
    mkdir -p "${ES_SET%/*}"
    if cp "$P/es_settings.xml" "$ES_SET"; then
      # SELinux labels exist on internal storage only; the SD is FUSE/exFAT and carries none, so a
      # restorecon there would just log a failure for a file that is already correct.
      [ "$EHK" = internal ] && relabel "$ES_SET"
      ok "restored ES-DE es_settings.xml -> $ES_SET ($EHK)"
    else warn "ES-DE es_settings.xml restore failed"; FAIL=$((FAIL+1)); fi
  fi
  # the Companion's custom event scripts — self-contained golden: the unit gets the hooks even if its SD
  # image predates the Companion setup.
  if [ -f "$P/es_scripts.tar" ]; then
    mkdir -p "$ES_HOME"
    if tar -xf "$P/es_scripts.tar" -C "$ES_HOME" 2>/dev/null; then
      [ "$EHK" = internal ] && relabel -R "$ES_HOME/scripts"
      ok "restored ES-DE custom event scripts -> $ES_HOME/scripts"
    else warn "ES-DE custom event scripts extract failed — the Companion's hooks will be missing"; fi
  fi
fi
```

**Then update the guard on the block that follows.** The next stanza currently opens with
`if echo "$RPKGS" | grep -q org.es_de.frontend && [ -f "$ES_SET" ]; then` — leave that condition as-is; `[ -f "$ES_SET" ]` is now false when `ES_HOME` is empty (the path degenerates to `/settings/es_settings.xml`), so the rewrite block correctly skips too.

- [ ] **Step 4: Run the tests**

Run: `cd tests && sh test_esde_settings.sh && sh -n ../provision/root/restore.sh && echo SYNTAX-OK`
Expected: `test_esde_settings: ALL PASS` then `SYNTAX-OK`.

- [ ] **Step 5: Commit**

```bash
git add provision/root/restore.sh tests/test_esde_settings.sh
git commit -m "feat(esde): restore settings + scripts to the home kind the golden recorded"
```

---

### Task 4: Preserve an empty `MediaDirectory`/`ROMDirectory` on SD-home goldens

**Files:**
- Modify: `provision/root/restore.sh:189-219` (the `2c` MediaDirectory and `2d` ROMDirectory rewrites)
- Test: `tests/test_esde_settings.sh`

**Interfaces:**
- Consumes: `es_setting_value` (Task 1); `EHK`, `ES_SET` (Task 3).
- Produces: nothing new for later tasks.

**Why:** these two keys are per-card and cannot be cloned verbatim. On an **internal**-home golden the rewrite is mandatory and stays exactly as it is today. On an **SD**-home golden an *empty* value is correct and portable — ES-DE resolves an empty path inside its own home, which is the card itself (the live Thor has `MediaDirectory value=""`), so forcing an absolute path would bake one unit's volume id into every clone. A *non-empty* value still carries the **golden's** volume id and must be re-pointed.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_esde_settings.sh`, above the summary line:

```sh
# --- MediaDirectory / ROMDirectory: rewrite rule by home kind -----------------------------------------
# internal-home  -> always force to THIS unit's card (unchanged behaviour; the RP6 path must not move).
# sd-home, empty -> LEAVE EMPTY. ES-DE resolves it inside its own home, which IS this card, so an empty
#                   value is portable across cards; an absolute one would carry a foreign volume id.
# sd-home, set   -> re-point, because the value carries the GOLDEN's volume id.
rewrite(){ # $1 = file, $2 = kind, $3 = this unit's serial, $4 = key, $5 = replacement value
  if [ "$2" = internal ] || [ -n "$(es_setting_value "$4" "$1")" ]; then
    sed "/name=\"$4\"/d" "$1" > "$1.cas" && mv "$1.cas" "$1"
    [ -s "$1" ] && [ -n "$(tail -c1 "$1" 2>/dev/null)" ] && printf '\n' >> "$1"
    printf '<string name="%s" value="%s" />\n' "$4" "$5" >> "$1"
  fi
}
U=6ED25E36D25E032F

f="$tmp/r_sd_empty.xml"; printf '%s\n' '<string name="MediaDirectory" value="" />' > "$f"
rewrite "$f" sd "$U" MediaDirectory "/storage/$U/ES-DE/downloaded_media"
eq "sd+empty stays empty" "$(es_setting_value MediaDirectory "$f")" ""

f="$tmp/r_sd_set.xml"; printf '%s\n' '<string name="MediaDirectory" value="/storage/GOLD-1234/ES-DE/downloaded_media" />' > "$f"
rewrite "$f" sd "$U" MediaDirectory "/storage/$U/ES-DE/downloaded_media"
eq "sd+set re-pointed" "$(es_setting_value MediaDirectory "$f")" "/storage/$U/ES-DE/downloaded_media"
grep -qF GOLD-1234 "$f" && { echo "FAIL: golden volume id survived an sd-home rewrite"; fail=1; }

f="$tmp/r_int_empty.xml"; printf '%s\n' '<string name="MediaDirectory" value="" />' > "$f"
rewrite "$f" internal "$U" MediaDirectory "/storage/$U/ES-DE/downloaded_media"
eq "internal+empty forced" "$(es_setting_value MediaDirectory "$f")" "/storage/$U/ES-DE/downloaded_media"
eq "internal no dupes" "$(grep -c 'name="MediaDirectory"' "$f")" "1"

f="$tmp/r_sd_rom.xml"; printf '%s\n' '<string name="ROMDirectory" value="" />' > "$f"
rewrite "$f" sd "$U" ROMDirectory "/storage/$U/ROMs"
eq "sd+empty ROMDirectory stays empty" "$(es_setting_value ROMDirectory "$f")" ""
```

- [ ] **Step 2: Run the test — shape lock, expected PASS**

Run: `cd tests && sh test_esde_settings.sh`
Expected: PASS — snippet-replication lock. Step 3 makes `restore.sh` match. If it FAILS, `es_setting_value` from Task 1 is wrong.

- [ ] **Step 3: Write the implementation**

In `provision/root/restore.sh`, wrap **each** of the two existing rewrites in the kind test. The MediaDirectory rewrite becomes:

```sh
  if [ -n "$MEDIA_DIR" ]; then
    # An SD-home golden that left this EMPTY is already correct: ES-DE resolves an empty MediaDirectory
    # inside its own home, which IS this unit's card. Forcing an absolute path there would bake one
    # unit's volume id into every clone. Only re-point a value that actually carries the GOLDEN's id.
    if [ "$EHK" = internal ] || [ -n "$(es_setting_value MediaDirectory "$ES_SET")" ]; then
      sed '/name="MediaDirectory"/d' "$ES_SET" > "$ES_SET.cas" 2>/dev/null && mv "$ES_SET.cas" "$ES_SET"  # portable in-place (BSD `sed -i` reads the script as a suffix)
      [ -s "$ES_SET" ] && [ -n "$(tail -c1 "$ES_SET" 2>/dev/null)" ] && printf '\n' >> "$ES_SET"  # ensure EOL
      printf '<string name="MediaDirectory" value="%s" />\n' "$MEDIA_DIR" >> "$ES_SET"
      [ "$EHK" = internal ] && relabel "$ES_SET"
      ok "ES-DE MediaDirectory -> $MEDIA_DIR (${CAS_ES_MEDIA:-sd})"
    else
      log "ES-DE MediaDirectory left empty (SD-home golden — ES-DE resolves it inside its own home on this card)"
    fi
  else
    warn "ES-DE box art: SD mode but no SD serial — MediaDirectory left at default (box art may be absent)."
  fi
```

and the ROMDirectory rewrite becomes:

```sh
  if [ -n "$SERIAL" ]; then
    ROM_DIR="/storage/$SERIAL/ROMs"
    # Same rule as MediaDirectory above: an SD-home golden's empty value is portable, keep it.
    if [ "$EHK" = internal ] || [ -n "$(es_setting_value ROMDirectory "$ES_SET")" ]; then
      sed '/name="ROMDirectory"/d' "$ES_SET" > "$ES_SET.cas" 2>/dev/null && mv "$ES_SET.cas" "$ES_SET"  # portable in-place (BSD `sed -i` reads the script as a suffix)
      [ -s "$ES_SET" ] && [ -n "$(tail -c1 "$ES_SET" 2>/dev/null)" ] && printf '\n' >> "$ES_SET"  # ensure EOL
      printf '<string name="ROMDirectory" value="%s" />\n' "$ROM_DIR" >> "$ES_SET"
      [ "$EHK" = internal ] && relabel "$ES_SET"
      ok "ES-DE ROMDirectory -> $ROM_DIR"
    else
      log "ES-DE ROMDirectory left empty (SD-home golden — ROMs resolve inside ES-DE's own home on this card)"
    fi
  else
    warn "ES-DE ROMs: no SD serial — ROMDirectory left at default (ES-DE may re-prompt for the ROM folder)."
  fi
```

Delete the two now-duplicated bare `relabel "$ES_SET" 2>/dev/null` lines that previously followed each `printf` — they are folded into the `[ "$EHK" = internal ] && relabel "$ES_SET"` lines above.

- [ ] **Step 4: Run the tests**

Run: `cd tests && sh test_esde_settings.sh && sh -n ../provision/root/restore.sh && echo SYNTAX-OK`
Expected: `test_esde_settings: ALL PASS` then `SYNTAX-OK`. The file's **original** assertions (golden `AlternativeEmulator.*` picks preserved, ROM/Media re-pointed exactly once, `GOLD-1234` gone) must still pass — they encode the internal-home path and are the regression guard for the RP6 fleet.

- [ ] **Step 5: Commit**

```bash
git add provision/root/restore.sh tests/test_esde_settings.sh
git commit -m "feat(esde): keep an SD-home golden's empty media/ROM paths portable across cards"
```

---

### Task 5: Warn when the Companion is restored but its toggles are off

**Files:**
- Modify: `provision/root/restore.sh` (append inside the ES-DE block from Task 3, after the rewrites)
- Test: `tests/test_esde_settings.sh`

**Interfaces:**
- Consumes: `es_setting_value` (Task 1); `ES_SET`, `RPKGS` (already in `restore.sh:23-29`).
- Produces: nothing.

**Why:** this exact bug shipped a unit with a silently dead Companion. The guard turns a mis-captured golden into a visible warning during the run instead of a discovery at the operator's desk.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_esde_settings.sh`, above the summary line:

```sh
# --- guard: Companion restored, but the golden never enabled its event scripts ------------------------
companion_guard(){ # $1 = es_settings.xml, $2 = RPKGS -> echoes the warning text, or nothing
  echo "$2" | grep -q com.esde.companion || return 0
  [ "$(es_setting_value CustomEventScripts "$1")" = true ] && return 0
  echo "WARN"
}
f="$tmp/g_off.xml"; printf '%s\n' '<bool name="CustomEventScripts" value="false" />' > "$f"
eq "guard fires when off"  "$(companion_guard "$f" 'org.es_de.frontend com.esde.companion')" "WARN"
f="$tmp/g_on.xml";  printf '%s\n' '<bool name="CustomEventScripts" value="true" />'  > "$f"
eq "guard silent when on"  "$(companion_guard "$f" 'org.es_de.frontend com.esde.companion')" ""
eq "guard silent w/o companion" "$(companion_guard "$f" 'org.es_de.frontend')" ""
f="$tmp/g_absent.xml"; : > "$f"
eq "guard fires when key absent" "$(companion_guard "$f" 'com.esde.companion')" "WARN"
```

- [ ] **Step 2: Run the test — shape lock, expected PASS**

Run: `cd tests && sh test_esde_settings.sh`
Expected: PASS — snippet-replication lock; Step 3 makes `restore.sh` match.

- [ ] **Step 3: Write the implementation**

In `provision/root/restore.sh`, inside the ES-DE block, after the ROMDirectory stanza and before the block's closing `fi`, add:

```sh
  # The ES-DE Companion works ONLY through ES-DE's custom event scripts. If we are installing it but the
  # golden's es_settings.xml does not enable them, the unit ships with a Companion that never fires —
  # which is exactly how this shipped once. Surface it during the run, don't fail the unit (the operator
  # may be provisioning a golden that legitimately has ES-DE without the Companion configured yet).
  if echo "$RPKGS" | grep -q com.esde.companion; then
    if [ "$(es_setting_value CustomEventScripts "$ES_SET")" = true ]; then
      ok "ES-DE custom event scripts enabled (ES-DE Companion will fire)"
    else
      warn "ES-DE Companion is being installed but the golden's es_settings.xml does NOT enable CustomEventScripts — its event scripts will never fire. Re-Save the golden with the Companion set up."
    fi
  fi
```

- [ ] **Step 4: Run the full suite**

Run:
```bash
cd tests && for f in test_*.sh; do echo "--- $f"; sh "$f" || echo "FAILED: $f"; done
python -m unittest discover -p "test_*.py"
```
Expected: every shell test prints `ALL PASS` with no `FAILED:` line, and the Python suite reports `OK` (760 tests as of `ca88c1e`).

- [ ] **Step 5: Commit and push**

```bash
git add provision/root/restore.sh tests/test_esde_settings.sh
git commit -m "feat(esde): warn when the Companion ships with its event scripts disabled"
git push origin main
```

---

## Verification gate

The tests prove the shell contract on the PC. They do **not** prove the end-to-end claim. Before this is called done:

1. Re-Save the `ayn-thor-512` golden with the library drive mounted. Confirm the payload now contains `es_settings.xml` **and** `es_scripts.tar`, and that `global.meta` has `esde_home=sd`.
2. Download onto a fresh Thor. Confirm `/storage/<unit card>/ES-DE/settings/es_settings.xml` exists with `CustomEventScripts` / `CustomEventScriptsBrowsing` true, and that all 7 `esdecompanion-*.sh` hooks are present under that card's `ES-DE/scripts/`.
3. Launch a game and exit it — the Companion should act with no hand setup.

Bench gate stays **OPEN** until step 3 passes on hardware. Report honestly if it does not.
