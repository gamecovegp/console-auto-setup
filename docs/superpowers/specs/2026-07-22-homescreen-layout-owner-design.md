# Homescreen: Capture the Layout Owner, Not the HOME App — Design

- **Date:** 2026-07-22
- **Status:** Approved (design); implementation pending
- **Author:** Donald (CTO) + Claude
- **Scope:** Stop `capture.sh` from archiving the wrong package's data as "the homescreen". Split the two
  roles it currently conflates — *which app is HOME* and *which app owns the icon/folder layout* — so a
  HOME shim like the AYN Thor's Mjolnir can no longer produce an empty golden layout.
- **Device-side shell only.** No GUI, no Python, no new payload files, no new capture-manifest flag.

---

## 1. The bug

A provisioned Thor came up with an unorganised homescreen. The golden was saved correctly; the *capture*
took the wrong package's data.

`ayn-thor-512`'s `homescreen/launcher_data.tar` is **4,608 bytes** and contains only:

```
xyz.blacksheep.mjolnir/shared_prefs/                                  (empty)
xyz.blacksheep.mjolnir/files/profileinstaller_...lastUpdateTime.dat    8 B
xyz.blacksheep.mjolnir/files/profileInstalled                         24 B
```

all dated 2026-02-06 — factory. Mjolnir holds **zero user state**, so the golden's layout is empty and
Download has nothing to apply.

### The controlled comparison

Every golden on the library drive, 2026-07-22:

| Profile | `launcher_data.tar` | `homescreen/meta` `launcher_pkg` | Layout works? |
|---|---|---|---|
| retroid-pocket-6-512 | 946,688 B | `com.android.launcher3` | yes |
| retroid-pocket-6-256 | 914,432 B | `com.android.launcher3` | yes |
| mangmi-air-x-256 | 1,190,912 B | `com.android.launcher3` | yes |
| ayn-odin-3-256 | 824,320 B | `com.android.launcher3` | yes |
| **ayn-thor-512** | **4,608 B** | **`xyz.blacksheep.mjolnir`** | **no** |

Four working goldens, one broken, exactly one variable different. **The retrieval mechanism is correct and
always has been** — it is the *resolver* that picks the wrong package, and only on the Thor.

### Why only the Thor

`capture.sh`'s homescreen block uses `home_launcher()` — `cmd package resolve-activity -a MAIN -c HOME` —
for two different jobs at once:

1. which package gets `set-home-activity` on restore (recorded as `launcher_component`), and
2. whose `/data/data` becomes `launcher_data.tar`.

Those are the same package on every unit CAS had seen. Then Mjolnir — a HOME-key interceptor that
registers `xyz.blacksheep.mjolnir/.HomeActivity` — was made the home app, `resolve-activity` started
returning it, and capture followed it off a cliff.

The Thor's real launcher is `com.android.launcher3`, corroborated three independent ways from the factory
baseline (`data/profiles/_baseline/ayn-thor-factory-20260720/`):

- `pm/default_home.txt` → `com.android.launcher3.uioverrides.QuickstepLauncher`,
  `/system_ext/priv-app/Launcher3QuickStepEX6/`, `dataDir=/data/user/0/com.android.launcher3`
- `dumpsys/role.txt` → `android.app.role.HOME` holder = `com.android.launcher3`
- `screens/display0_main_1080x1920.png` → a stock Quickstep grid (its "Wallpapers / Widgets / Home
  settings" long-press menu), with **Mjolnir present merely as an icon on that grid**

**Second-order damage:** restore does `rm -rf /data/data/$LP/*` then extracts the tar, so it also wipes
Mjolnir's own data on the unit and applies nothing in its place.

---

## 2. Design

### 2.1 `layout_launcher [override]` (`lib-root.sh`)

A new resolver that answers *whose data holds the layout*, in order:

1. **`override`** — from `@homescreen <pkg>`, when installed. Same escape hatch `@gamelauncher <pkg>`
   already provides for the game frontend.
2. **The active HOME**, if its data dir passes the layout signature test below. This is the path all four
   working goldens take, so their behaviour is provably unchanged.
3. **`com.android.launcher3`**, if installed and it passes the same test. Not a guess: it is what all four
   working profiles independently recorded, and what the Thor's own factory baseline names.
4. Otherwise **empty** → the caller warns and captures no layout.

**The signature test reuses existing, tested machinery.** `homescreen_apps <data_dir>` (`lib-root.sh:272`)
already scans a launcher data dir for plaintext `component=<pkg>/` and `package=<pkg>` references in the
favorites DB — launcher-family-agnostic, no `sqlite3`, and it degrades to empty on an exotic blob. A dir
that yields no placed-app references holds no layout. Mjolnir's yields nothing; launcher3's yields the
grid. No new detection logic is invented.

`layout_launcher` honours `DATA_ROOT` (default `/data/data`), exactly as `game_launcher` does, so it is
**testable off-device with real execution coverage** rather than the snippet-replication pattern.

### 2.2 `homescreen/meta` splits the two roles

Both keys already exist; this pins down what each one means.

- `launcher_pkg` — **whose data is inside `launcher_data.tar`**, i.e. the layout owner. Restore untars into
  `/data/data/$launcher_pkg` and chowns to its uid, exactly as today.
- `launcher_component` — **what to hand `set-home-activity`**, i.e. the active HOME. Unchanged meaning.
  On the Thor this stays Mjolnir, so Donald's HOME choice is preserved.
- `launcher_uid` — the layout owner's uid, matching `launcher_pkg`.

On all four existing goldens these name the same package, so **every current payload restores
byte-identically**. Goldens captured before `launcher_component` existed are unaffected.

### 2.3 `restore.sh` gate

Today the layout is skipped unless the unit's current HOME equals the golden's `launcher_pkg`. Once the
two roles legitimately differ that comparison is wrong by construction — on a Thor the unit's HOME becomes
Mjolnir while the layout owner is launcher3, so it would skip every time.

New order:

1. Apply the golden's HOME choice from `launcher_component` (unchanged, already guarded — `set_home_component`
   refuses a package that isn't installed).
2. Restore the layout when `pm path "$launcher_pkg"` shows the layout owner present on this unit.
   `com.android.launcher3` is firmware, so on these handhelds it always is.
   **The layout is restored whether or not the layout owner is the active HOME.** This is a requirement,
   not a side effect: Mjolnir stays the home app, launcher3 still receives the golden's arrangement, so if
   anyone later switches HOME to launcher3 by hand the organised grid is already there waiting. Gating the
   restore on "is this package HOME?" would defeat that and is exactly the mistake being removed.
3. `homescreen_bundle_apps` (capture) and `homescreen_install_missing` (restore) operate on the layout
   owner's data dir, not the HOME app's.

### 2.4 Failure handling

- No layout owner resolvable → `warn`, capture no layout. Additive: never bumps `CFAIL`, consistent with
  the rest of the homescreen block.
- **New guard:** if the resolved layout owner differs from the active HOME, `log` both — so the operator
  can see at Save time that the HOME app is a shim and the layout came from elsewhere. Silence is what let
  this ship.
- Restore keeps `warn`-only on the whole homescreen path; it never bumps `FAIL`.

---

## 3. Out of scope

- The ES-DE / Companion work shipped separately (`b097f26..cbb42e0`).
- Changing which app is HOME on the Thor. **Mjolnir as HOME is correct and stays** — Donald confirmed it
  is not the problem. Only the *layout source* changes: which package's data the golden archives and
  restores. The unit still boots with Mjolnir as its home app exactly as it does today.
- Capturing more than one launcher's data. Mjolnir's dir is 4.6 KB of nothing, so capturing it alongside
  buys nothing — rejected as YAGNI during design.

---

## 4. Testing

`layout_launcher` is pure and `DATA_ROOT`-overridable, so the shell test drives the **real function**:

- HOME's data dir has placed-app refs → returns the HOME package (the four working profiles' path).
- HOME's dir is Mjolnir-shaped — present but with no placed-app refs → falls back to
  `com.android.launcher3` (the Thor case).
- Override pins a package that is installed.
- Nothing anywhere qualifies → empty output, non-zero return.
- Regression guard: a stateless-shim data dir must never win over a real launcher DB.

Constraints (CI runs these as `sh` on Linux **and** macOS): POSIX only, no `sed -i`.

---

## 5. Verification gate

1. Re-Save the golden Thor. `homescreen/launcher_data.tar` must jump from 4,608 B to the ~1 MB range the
   other four profiles show, and `homescreen/meta` must read `launcher_pkg=com.android.launcher3` with
   `launcher_component=xyz.blacksheep.mjolnir/.HomeActivity`.
2. Download to a fresh Thor. Mjolnir must still be the home app, and `/data/data/com.android.launcher3`
   must hold the golden's layout — verify by switching HOME to launcher3 by hand and confirming the
   organised folders/icons appear. Whether the grid is visible *without* that switch depends on what
   Mjolnir's `.HomeActivity` draws and is not what this change promises; the promise is that the
   arrangement is present and correct on the unit.
3. Confirm no regression on an RP6 re-Save — `launcher_pkg` must stay `com.android.launcher3` and the tar
   stay ~1 MB.

**Bench gate is OPEN until step 2 passes on hardware.** The `com.android.launcher3` fallback is inferred
from four sibling profiles and the Thor's factory dumps, not from a Thor golden containing launcher3 data —
that golden has none, precisely because of this bug.
