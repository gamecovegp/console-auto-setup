# ES-DE Follows the Golden — Design

- **Date:** 2026-07-21
- **Status:** Approved (design); implementation pending
- **Author:** Donald (CTO) + Claude
- **Scope:** Make CAS capture and restore ES-DE's frontend config from wherever ES-DE actually lives on a
  given unit — the SD card on the AYN Thor, internal storage on the RP6 — and carry the `ES-DE/scripts/`
  tree so the ES-DE Companion works on a provisioned unit without hand setup.
- **Shell-only.** No GUI, no Python, no new capture-manifest flag.

---

## 1. The bug this fixes

A provisioned Thor came up with ES-DE Companion dead: its custom event scripts never fired, so Donald
re-enabled them by hand on unit `8ea3d1aa`.

The Companion is not the broken part. **ES-DE's own settings file was never captured**, because CAS looks
for it in one hardcoded place:

```sh
ES_SETTINGS_PATH="/storage/emulated/0/ES-DE/settings/es_settings.xml"   # lib-root.sh:38
ES_SET="/storage/emulated/0/ES-DE/settings/es_settings.xml"             # restore.sh:178
```

ES-DE Android's home directory is a **user pick**. The Thor keeps its whole ES-DE tree on the SD card;
that internal path does not exist there. Evidence gathered 2026-07-21:

| Fact | Source |
|---|---|
| Thor's ES-DE home is the SD: `/storage/9C33-6BBD/ES-DE/{settings,scripts,downloaded_media,…}` | live unit `8ea3d1aa` |
| `/storage/emulated/0/ES-DE/` does not exist on that unit | `ls` → "No such file or directory" |
| Capture's `if [ -f "$ES_SETTINGS_PATH" ]` therefore silently skipped — the golden has **no `es_settings.xml`** | `ayn-thor-512/golden_root_payload/` listing: `global.meta`, `homescreen`, `internal_*.tar`, `settings`, `urigrants.xml`, `wifi`, per-app dirs — no `es_settings.xml` |
| Restore wrote `MediaDirectory`/`ROMDirectory` into an internal file ES-DE never reads | live SD copy still shows `MediaDirectory value=""`, untouched by restore |
| The Companion's integration is 7 hooks: `ES-DE/scripts/<event>/esdecompanion-<event>.sh` | live SD (`game-start`, `game-end`, `game-select`, `system-select`, 3 × `screensaver-*`) |
| Both toggles are plain lines in `es_settings.xml` | live file: `<bool name="CustomEventScripts" value="true" />`, `<bool name="CustomEventScriptsBrowsing" value="true" />` |

The Companion's file access is **not** part of the gap: it declares `MANAGE_EXTERNAL_STORAGE`, which
`grant_special_appops` (`lib-root.sh:184`) already grants and verifies at restore. Confirmed `allow` on the
live unit.

**Root cause in one line:** CAS assumes ES-DE's home is internal storage; on an SD-home unit the golden
captures nothing and the restore writes to a file nobody reads.

---

## 2. Decisions taken

Two options were put to Donald and settled before design:

1. **How to locate `es_settings.xml`** → *follow the golden*. Capture records where the golden's ES-DE
   lived; restore writes to the same **kind** of location on the unit. (Rejected: probe the unit
   independently at restore time; a per-profile `esde_home` key.)
2. **What the golden carries** → *`es_settings.xml` + the `scripts/` tree*. Self-contained, so a unit
   provisions correctly even when its SD image predates the Companion setup — the same philosophy as
   `@homescreen` bundling placed-app APKs. (Rejected: settings only; or dropping ES-DE from the payload
   and letting the SD master image own it.)

---

## 3. Design

### 3.1 Locating the ES-DE home (`lib-root.sh`)

Two helpers, kept pure and free of side effects so `tests/test_esde_settings.sh` can **source** them
instead of replicating snippets (today's test copy-pastes restore.sh's logic, which is free to drift):

- `esde_home()` — probe `<vol>/ES-DE/settings` across the external volume(s) first, then
  `/storage/emulated/0`; print the ES-DE dir found, empty if none. Volume enumeration reuses the existing
  `detect_sd` idiom (format-agnostic — never assume a hyphenated volume id).
- `esde_home_kind <dir>` — print `internal` for a path under `/storage/emulated/0`, else `sd`.

`ES_SETTINGS_PATH` stops being a hardcoded constant. **`esde_home()` probing is a capture-side tool only** —
restore never probes the unit, it resolves the path from the kind the golden recorded (3.3). That is what
"follow the golden" means: the golden decides, the unit obeys.

### 3.2 Capture (`capture.sh`)

- Resolve the golden's ES-DE home once. Record the kind in `global.meta` as `esde_home=sd|internal`,
  alongside the existing `golden_serial`/`golden_tz`/`golden_locale` keys.
- Copy `<home>/settings/es_settings.xml` → `payload/es_settings.xml` (unchanged behaviour, correct source).
- Tar `<home>/scripts/` → `payload/es_scripts.tar`, only when the directory exists and is non-empty
  (same "skip empty" rule the `INTERNAL_DIRS` loop already uses to avoid shipping empty dirs).
- No ES-DE home found → `warn`, capture nothing. Additive: never bumps `CFAIL`.

### 3.3 Restore (`restore.sh`)

Gate is unchanged: this whole block still runs only when `org.es_de.frontend` is in `RPKGS`.

1. Read `esde_home` from the payload's `global.meta`. **Absent → `internal`.** Every golden captured
   before this change therefore behaves exactly as it does today; the RP6 fleet is untouched.
2. Resolve this unit's ES-DE home: `internal` → `/storage/emulated/0/ES-DE`; `sd` → `/storage/$SERIAL/ES-DE`.
3. `mkdir -p`, restore `es_settings.xml`, then untar `es_scripts.tar` into the home.
4. Re-point the per-card directories (see 3.4).
5. `relabel` only for an internal home — an SD lives on FUSE/exFAT, which carries no SELinux labels and
   would just log a failure.

### 3.4 Per-unit path rewrites

`MediaDirectory` and `ROMDirectory` are per-card and cannot be cloned verbatim.

- **internal-home golden** — unchanged from today: force both to `/storage/$SERIAL/…`. Deliberately no
  behaviour change, so the RP6 path carries zero risk.
- **SD-home golden** — an empty value is **preserved**. ES-DE resolves an empty `MediaDirectory` relative
  to its own home, which for these units *is* the card, so leaving it empty stays portable across cards
  instead of baking one unit's volume id into every clone. (The live Thor has exactly this: `value=""`.)
  A non-empty value is still re-pointed at this unit's serial, since it would otherwise carry the
  *golden's* volume id.

### 3.5 Failure handling

- SD-home golden restoring onto a unit with **no SD** → loud `warn`, skip the ES-DE block. A card-less
  unit genuinely cannot have SD-home ES-DE. Matches the existing "no SD serial — MediaDirectory left at
  default" warning; does not bump `FAIL`.
- **New guard:** if `com.esde.companion` is in the restore set but the restored `es_settings.xml` does not
  set `CustomEventScripts` true, `warn`. This exact bug shipped a unit with a silently dead Companion;
  the guard surfaces a mis-captured golden during the run instead of at the operator's desk.
- A failed `es_settings.xml` copy keeps today's contract: `warn` + `FAIL++`.

---

## 4. What is explicitly out of scope

- **`android.permission.DUMP`.** The Companion declares it (decoded from the baseline APK) and neither
  `pm install -g` nor `SPECIAL_APPOPS` grants it. It was investigated as a suspect and is *not* the cause
  of this bug. If a provisioned unit still misbehaves after this change, that is the next thread to pull —
  a separate spec.
- **`settings secure` restore.** `capture.sh:126` dumps `secure.txt`, but `restore.sh:272-273` applies only
  `SET_SYSTEM`/`SET_GLOBAL` — there is no `SET_SECURE`. Latent gap, unrelated to ES-DE, not touched here.
- Moving ES-DE between internal and SD, or changing the SD master image.

---

## 5. Testing

`tests/test_esde_settings.sh` is extended to **source** the new helpers rather than replicate them:

- `esde_home()` finds an SD-shaped tree; finds an internal-shaped tree; prints empty when neither exists.
- `esde_home_kind` maps both shapes correctly.
- Kind → unit-path resolution for `sd` (uses this unit's serial) and `internal`.
- Back-compat: a `global.meta` with **no** `esde_home=` key resolves to `internal` and reproduces today's
  rewrite byte-for-byte.
- The 3.4 rule both ways: SD-home + empty `MediaDirectory` stays empty; SD-home + a golden-serial value is
  re-pointed at this unit; internal-home is forced as it is today.
- `es_scripts.tar` round trip: all 7 `esdecompanion-*.sh` hooks land under the right event dirs.

Constraints (CI runs these on Linux **and** macOS hosts): POSIX only, and no `sed -i` — use
`sed SCRIPT f > f.tmp && mv`, the BSD trap this repo has already been bitten by.

---

## 6. Verification gate

Tests and a golden re-Save prove the capture side on the PC. The end-to-end claim — a freshly provisioned
Thor boots with the Companion's scripts firing and **no** hand setup — is **unproven until it runs on
hardware**. Bench gate stays OPEN until a Save → Download cycle on a real Thor confirms it.
