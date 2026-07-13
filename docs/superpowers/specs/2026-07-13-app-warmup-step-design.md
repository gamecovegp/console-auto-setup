# App warm-up step (③ Warm up) — design

**Date:** 2026-07-13
**Status:** approved, not yet implemented

## Problem

On a freshly provisioned unit, launching a game from the frontend (`com.handheld.launcher` / ES-DE)
fails for an emulator that has never been opened. The emulator has to be opened once — by hand, today —
before it picks up its restored settings and indexes its games. Once it has, the frontend launches games
into it normally.

Every unit therefore needs a manual pass: open each emulator, back out, repeat. That pass is the last
hands-on step between Download and Lock, and it is what this design removes.

## Solution

A fourth chain step, `warmup`, that runs between Download and Lock. It launches every app the Download
installed, once, in sequence, and leaves each one running so it can finish indexing in the background.

It is its own checkbox and is independently tickable: warm-up can be run alone on a unit that was
provisioned earlier, without re-running Download.

### Chain position and numbering

`_CHAIN_ORDER` becomes `("root", "save", "download", "warmup", "lock")`. The GUI labels renumber:

| Old | New |
|---|---|
| ⓪ Root | ⓪ Root |
| ① Save → profile | ① Save → profile |
| ② Download | ② Download |
| — | **③ Warm up** |
| ③ Lock | **④ Lock** |

Lock moves from ③ to ④. The numbers denote chain order, so leaving Lock at ③ would misrepresent the
order; the renumber is accepted deliberately.

Warm-up is mutually exclusive with Save, like Download and Lock: `_on_chain_tick`'s `unit_on` gains
`warmup`.

**The post-Download reboot needs no new code.** `_run_chain_core` (`gui.py:1904`) sets `wait_boot=True`
on the Download stage whenever any step follows it. Inserting `warmup` after `download` turns that on
automatically, so warm-up never touches a rebooting device.

## Per-device behavior

For each app, in manifest order:

1. `pm path <pkg>` — if the package is not on the unit, log a skip and move on.
2. `monkey -p <pkg> -c android.intent.category.LAUNCHER 1` — launch its LAUNCHER activity.
3. Poll `uiauto.foreground(adb)` until the package is the resumed activity, up to
   `WARMUP_FOREGROUND_TIMEOUT = 15` seconds.
4. Sleep `warmup_dwell_s` (default **3**).
5. Move to the next app.

**No app is force-stopped during the pass.** Launching app B pushes app A to the background, where it
keeps indexing. A `force-stop` right after a 3-second dwell would kill a scan that had just started —
the exact failure this step exists to fix.

**The pass then ends with a settle, a sweep, and a home.** (Revised after the whole-branch review.)

1. **Settle** — once every app has been launched, wait `warmup_settle_s` (default **30**). Without this
   the "they keep indexing in the background" premise is false in a Download → Warm up → Lock chain:
   `seal()` scrubs, un-roots, flashes and reboots within ~20s of the pass ending, so the *last* apps
   launched — which are the frontends, placed last precisely so they index against a warm set — would
   get almost no background time at all. A reboot 20 seconds later is functionally the same kill as the
   force-stop we rejected. The settle is what makes the premise true.
2. **Sweep** — force-stop every app the pass launched. This is safe *because* it comes after the settle,
   and it is what keeps ~14 emulators out of the shipped unit's **Android recents**. Lock's scrub deletes
   `/data/system_ce/0/recent_tasks/*`, but ActivityManager is still live at that moment and re-persists
   its in-memory task list on the seal reboot — so without this sweep a customer could unbox the unit,
   swipe recents, and find every emulator CAS opened.
3. **Home** — `am start -a android.intent.action.MAIN -c android.intent.category.HOME`, so a warmed unit
   is never left sitting inside an emulator.

### Refusals and the silent-no-op guard

Warm-up **requires root**, then refuses the **golden master** — root first, exactly like `provision()`.

It does not otherwise need root (`monkey`, `am` and `pm` all run as shell); the *golden guard* needs it.
`adb.is_golden()` is **fail-closed**: an ambiguous or blocked `su` reads as "golden". Both ways of dodging
that are broken, and both were tried:

- Probing **without** confirming root first gives a false golden-lock refusal on every real unit whose
  Magisk shell grant didn't survive the Download reboot — warm-up is the first step to call `su` after it.
- **Skipping** the probe when root is absent fails the other way and warms the **master**: all ~14 apps
  opened on the golden, dirtying its first-run state and recents. The golden is never sealed, so it is
  never scrubbed, and the damage rides the next ① Save into every future unit's payload.

Requiring root closes both: the probe always answers honestly, and an unrooted unit gets `provision()`'s
actionable "click ⓪ Root first" message instead of a wrong one. This costs nothing in the real flow —
warm-up runs between Download and Lock, where the unit is rooted. In an "Apply to ALL" batch the golden
reports `skip-golden`, as it does under `root_all`/`seal_all`, rather than as a red failure.

A pass that warms **nothing** is a hard `fail`, not an `ok`: an empty/unreadable manifest (e.g. the library
drive is not mounted — `profile.pkgs()` returns `[]` rather than raising) or zero apps successfully opened
would otherwise report green and let Lock seal a unit carrying the exact defect this feature removes. This
does not weaken the additive rule below — a warm-up *miss* (some apps failed) still never blocks a seal;
only warming *nothing at all* does.

`warmup` is also added to the `library_unreachable` and `no_profile` gates in `cas/warnings.py`, so the
preflight blocks it for the same reasons it blocks Download.

Devices run in parallel via the existing `_each_device` fan-out; apps run sequentially within a device.

### Which apps, and in what order

The app set comes **PC-side from the profile manifest** — `profile.pkgs()` — not from the device.
Download deletes the device manifest when it finishes (`provision.py:617`), so there is nothing to read
back off the unit.

`profile.pkgs()` is used rather than `_split_manifest_apps()` because it is the *superset* that warm-up
actually wants: payload apps, store-managed apps, Companion, **and** config-only apps (APK axis off,
config axis on). A config-only app was already on the unit but just had new settings restored into it —
it needs the warm-up launch as much as a freshly installed one does.

Two filters apply:

- **Skip-list** — `warmup_skip_pkgs` in `cas-config.json`. Default: **`com.topjohnwu.magisk` only.**
  Magisk is a host tool, not a shipped app, and launching it does nothing for the unit. **Everything
  else warms** — Companion, SteamLink, every emulator. A blanket "launch it once" is the cheapest rule
  to reason about, and at 3s an app the cost of an unnecessary launch is 3 seconds. The config key is
  the escape hatch if a specific app turns out to misbehave on launch.
- **Presence** — the `pm path` guard in step 1 above.

**The frontends are warmed too, and they go last.** `WARMUP_FRONTENDS = ("org.es_de.frontend",
"com.handheld.launcher")` is appended to the end of the launch order, so each frontend opens *after*
every emulator has been initialized and can index against a warm set. They are handled as an explicit
package list rather than through the manifest because `com.handheld.launcher` is a **system** app on
MANGMI units — `user_pkgs()` (`lib-root.sh:104`) lists only `-3` packages, so the launcher never appears
in a golden's manifest and would otherwise never be warmed. A frontend already present in the manifest
(ES-DE often is) is launched **once**, in the frontend slot at the end, not twice.

**Homescreen-bundled apps count too.** `@homescreen` restores the golden's homescreen layout, and
`homescreen_install_missing()` (`lib-root.sh:328`) installs an APK for every placed app that is absent on
the target. By construction those are apps whose APK is *not* in the golden payload — so they are **not in
the manifest**, and `profile.pkgs()` alone would miss them. That is precisely the un-opened-emulator bug,
on the one install path warm-up wouldn't cover. The app set therefore unions `profile.pkgs()` with the
package directories under `payload/homescreen/apps/`.

Final per-device order: `[manifest apps ∪ homescreen-bundled apps, minus skip-list, minus frontends] +
[frontends present on the unit]`.

### Failure behavior

Additive, like `scrub.sh`. An app that never reaches the foreground within 15s gets a `[warn]` line
naming the package **and the activity that was actually foreground**, and the pass continues. That log
line is the diagnostic that tells us which app needs a longer dwell or a uiauto tap.

Only a dead device (adb gone / cancel) makes the unit `fail` and drops it from the chain before Lock. A
warm-up miss must never block a seal.

`adb.cancel` is polled between apps, so ✖ Cancel stops within one app's dwell.

### Interaction with Lock

Lock's `scrub_traces` (`lib-root.sh:88-99`) removes only named MRU/savestate members
(`content_history.lpl`, `savestates`, `sstates`), not whole app data dirs. Everything an emulator writes
during warm-up — its initialized config, its game index — survives the seal. No change to `scrub.sh` is
needed, and no warm-up state needs to be protected from it.

## Configuration

Two new keys in `cas-config.json`, both with getters in `cas/config.py` following the
`auto_grant_shell` / `always_install_pkgs` pattern:

| Key | Default | Meaning |
|---|---|---|
| `warmup_dwell_s` | `3` | Seconds to leave each app in the foreground before launching the next. |
| `warmup_settle_s` | `30` | Seconds to let every launched app keep indexing in the background after the last one is opened, before the force-stop sweep. |
| `warmup_skip_pkgs` | `["com.topjohnwu.magisk"]` | Packages the step never launches. A stored list overrides the default; an empty list means "skip nothing". |

**Known trade-off of a fixed dwell:** an app whose scan takes longer than the dwell keeps scanning in the
background (we do not kill it), so the dwell mainly controls *how long we watch*, not how long it gets.
If a unit still ships with an unindexed emulator, raising `warmup_dwell_s` is the first lever.

## Surface area

- **`cas/provision.py`** — `warmup(adb, profile, log=print, dwell=None, skip=None)` and
  `warmup_all(make_adb, devices, root=..., log=print, profile=None, profile_map=None, parallel=True)`,
  modeled on `provision` / `provision_all` (`:454` / `:678`). The worker returns `(status, detail)` and
  never raises; it polls `adb.cancel`.
- **`cas/adb.py`** — `Adb.launch(pkg)` (monkey LAUNCHER intent) and `Adb.go_home()`.
- **`cas/gui.py`** — the checkbox tuple (`:657`), `_CHAIN_ORDER` (`:1467`), a `_stage` branch (`:1865`),
  `_on_chain_tick`'s `unit_on` (`:1405`), and the `names` display dict in `_run_chain` (`:1915`).
- **`cas/cli.py`** — `warmup` / `warmup-all` subcommands, following the `provision` / `provision-all`
  pattern (`:44-53`).
- **`cas/config.py`** — the two getters above.
- **`cas/warnings.py`** — `"warmup"` added to `ACTIONS` (`:16`) so the step is preflight-gateable.

No device-side shell script. No change to `restore.sh`, `scrub.sh`, or `lib-root.sh`.

## Tests (`tests/test_cas.py`)

- `TestRunChain` — the new step appears in the expected `_stage_calls` order, and Download's `wait_boot`
  is `True` when warm-up follows it even with Lock unticked.
- `TestResolveChain` — warm-up is rejected alongside Save.
- `TestWarmup` (new, `FakeRunner`) —
  - launches every installed app in manifest order, and **no** `force-stop` reaches adb;
  - a package in `warmup_skip_pkgs` is never launched;
  - a package absent from the device (`pm path` fails) is skipped with a log line, not launched;
  - an app that never reaches the foreground produces a `[warn]` naming the package, and the pass
    continues to the next app;
  - the pass ends with the HOME intent;
  - a set `cancel` event stops the pass between apps.
