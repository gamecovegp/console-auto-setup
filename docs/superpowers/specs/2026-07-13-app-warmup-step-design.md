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

**Apps are never force-stopped.** Launching app B pushes app A to the background, where it keeps
indexing. A `force-stop` after a 3-second dwell would kill a scan that had just started — the exact
failure this step exists to fix. The Lock reboot cleans up; when warm-up runs standalone, the apps are
simply left running, which is harmless.

At the end of the pass the step returns the unit to its launcher
(`am start -a android.intent.action.MAIN -c android.intent.category.HOME`) so a warmed unit is not left
sitting inside an emulator.

Devices run in parallel via the existing `_each_device` fan-out; apps run sequentially within a device.

### Which apps

The app set is recomputed **PC-side from the profile manifest**, not read off the device — Download
deletes the device manifest when it finishes (`provision.py:617`), so there is nothing to read back.
`_split_manifest_apps()` (`provision.py:96`) already yields exactly what Download installs:

    payload apps (captured in the golden) + managed apps (APK axis on, from the store) + Companion

Two filters apply:

- **Skip-list** — `warmup_skip_pkgs` in `cas-config.json`. Default:
  `com.topjohnwu.magisk`, `com.gamecove.gamecove_companion`, `org.es_de.frontend`,
  `com.handheld.launcher`. These gain nothing from a launch (Magisk is a host tool; the Companion is
  already nudged by the lockdown path at `provision.py:409`; the frontends are the thing being warmed
  *for*). Editing the list in config is how SteamLink gets added if its 3 seconds are not worth paying.
- **Presence** — the `pm path` guard in step 1 above.

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
| `warmup_skip_pkgs` | see above | Packages the step never launches. A stored list overrides the default; an empty list means "skip nothing". |

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
