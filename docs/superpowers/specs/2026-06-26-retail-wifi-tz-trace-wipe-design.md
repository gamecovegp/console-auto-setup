# Retail prep: shop-WiFi on setup, Asia/Manila timezone, trace-wipe on lock

**Date:** 2026-06-26
**Status:** Approved design — ready for implementation plan
**Scope:** `cas/` (PC-driven) + `provision/` (on-device) — additive; no pipeline stage added.

## Problem

Units currently ship **offline by design** (`provision/root/capture.sh:118`, `restore.sh:226`):
WiFi is never provisioned and the OOBE wizard is skipped. That's good for the
customer, but it means during **setup/QA** we can't easily let a unit reach the
internet to update apps, pull cores/ROMs, sync the clock, or run OTA tests
without a manual per-unit WiFi tap.

It also means each unit accumulates **test fingerprints** before it ships:
recently-played lists in the emulators (PPSSPP, Mupen64, RetroArch, …), ES-DE
last-played/playcount, test save files and save-states, screenshots, logs, and
Android usage traces. Today `provision/cleanup.sh` deliberately *keeps* saves and
does nothing about recents. A retail unit should look **factory-fresh** while
keeping the ROMs and emulators we installed.

## Goals

1. **Auto-join shop WiFi during setup** from `cas-config.json` (hands-off across a batch).
2. **Pin timezone to Asia/Manila (UTC+8)** explicitly on every unit, NTP-synced clock.
3. **On lock/seal, erase every test fingerprint** — recents, saves/states, media/logs,
   Android usage traces — and **forget the shop WiFi** (radio left ON for the customer).
4. Keep ROMs, emulators, BIOS/firmware/keys, and emulator settings intact.

## Non-goals

- No new pipeline stage. These hook into the existing `provision` and `seal` stages.
- Not touching the golden-capture decision (units still ship offline *after* seal).
- No per-game allowlisting of which saves to keep — **all** user saves/states are wiped.

## Pipeline context (unchanged)

`root → provision → capture → seal`. "Seal" = the **lock** the operator runs last
(`cas/provision.py:seal()` PC-driven, and `provision/cleanup.sh` on-device). It
un-roots (flash stock init_boot), hides Developer Options, disables USB debugging.

The two new behaviors attach as:

| Behavior | Stage | Path |
|---|---|---|
| WiFi join + TZ pin | `provision()` | PC-driven adb (rooted) |
| WiFi forget + trace-wipe | `seal()` **first step, while still rooted** | PC-driven adb + canonical shell script |

Trace-wipe runs **before** the un-root fastboot flash because Mupen64 recents and
Android usage stats live in `/data/data` and need root. Flashing stock `init_boot`
does not touch `/data`, so forgotten networks and wiped data stay gone.

## Configuration — `cas-config.json`

New optional keys (all absent ⇒ feature is a no-op, preserving today's behavior):

```json
"wifi":     { "ssid": "Luxium-Shop", "password": "…", "security": "wpa2", "hidden": false },
"timezone": "Asia/Manila",
"retail_clean": { "recents": true, "saves": true, "media": true, "android_traces": true }
```

- `wifi` absent ⇒ skip join (and there's nothing to forget).
- `timezone` defaults to `Asia/Manila` when the key is absent.
- All four `retail_clean` flags default **true**.
- `security` ∈ {`open`, `wpa2`, `wpa3`}. `hidden:true` adds the hidden-SSID flag on join.

Loaded via the existing `cas/config.py` accessor pattern (mirrors `es_media_src`, `nas_user`).

## Component design

### 1. `cas/adb.py` — two helpers

- `wifi_join(ssid, pw, security="wpa2", hidden=False) -> bool`
  - `su -c 'svc wifi enable'` then
  - `su -c 'cmd wifi connect-network "<ssid>" <security> "<pw>"'` (+ `-h` if hidden;
    omit the password arg for `open`). Returns success; logs and returns False on failure.
- `wifi_forget_all() -> None`
  - `cmd wifi list-networks` → parse each network id → `cmd wifi forget-network <id>`.
  - **Radio left ON** (never `svc wifi disable`). Best-effort.

Rationale for `cmd wifi`: works on the Android 13 units (RP6 / Odin2), avoids
hand-editing `WifiConfigStore.xml`, and `forget-network` cleanly drops the saved
credential without disabling the radio.

### 2. `apply_timezone(adb, tz="Asia/Manila")` (in `cas/provision.py`)

Makes the TZ guarantee explicit on the PC-driven path (today only `restore.sh`
pins it, and only from the golden's captured value):

- `su -c 'setprop persist.sys.timezone <tz>'`
- `settings put global auto_time_zone 0` — pin it; don't let the network re-resolve.
- `settings put global auto_time 1` — NTP clock sync (now meaningful: the unit is online).

Run **right after** `wifi_join` in `provision()` so NTP can sync the wall clock while
WiFi is up. The pinned TZ + synced clock persist after WiFi is forgotten at seal.

### 3. Canonical wipe: `provision/retail-clean.sh`

**Single source of truth** for the wipe, using `lib.sh` helpers (`detect_sd`, `SH`, `log`).
Four flag-gated sections (`CLEAN_RECENTS`/`CLEAN_SAVES`/`CLEAN_MEDIA`/`CLEAN_TRACES`,
default 1). Every delete is **best-effort** (`rm -rf … 2>/dev/null`, warn-not-abort)
and **idempotent**.

- **On-device path:** `cleanup.sh` sources `retail-clean.sh` **before** uninstalling
  Termux/Shizuku (root still present) and **before** it deletes `provision/` from the SD.
- **PC-driven path:** `clean_for_retail(adb, opts)` pushes `retail-clean.sh` + `lib.sh`
  to `/data/local/tmp`, detects `SDID` over adb, and runs it via `su -c 'sh …'` with the
  flag envs set from `retail_clean`. (Pushed fresh to `/data/local/tmp` because seal may
  run after the SD's `provision/` copy is already gone.)

This keeps one wipe implementation; the `.py` is just the launcher.

### Wipe target map

**Preserve everywhere:** emulator BIOS/firmware/keys, NAND *system*, controller
profiles, all emulator config/settings, ROMs, ES-DE config (themes/custom_systems).
**Wipe:** only the per-category targets below. NAND/memcard **save data** is wiped
while the firmware/keys/empty-card structure stays (Eden/NetherSX2/Citra still launch).

| Emulator (pkg) | A. Recents/history | B. Saves & states | C. Media/logs |
|---|---|---|---|
| PPSSPP `org.ppsspp.ppsspp` (memstick `/storage/$SDID/ROMs/psp`) | strip `[Recent]` in `PSP/SYSTEM/ppsspp.ini` | `PSP/SAVEDATA/`, `PSP/PPSSPP_STATE/` | `PSP/SCREENSHOT/` |
| RetroArch `com.retroarch.aarch64` (`…/files`) | `content_history.lpl`, `content_favorites.lpl`, `content_{image,music,video}_history.lpl` | `saves/`, `states/` | `screenshots/`, `thumbnails/`, `logs/` |
| Mupen64 FZ `org.mupen64plusae.v3.fzurita` (**`/data/data`, root**) | gallery cache + `[GALLERY]`/recent in cfg | saves/states dirs | — |
| DuckStation `com.github.stenzek.duckstation` | recent list (shared_prefs, root / `cache/`) | `memcards/`, `savestates/` | `screenshots/` |
| Eden `dev.eden.eden_emulator` | `recentFiles` in `config/qt-config.ini` | `nand/user/save/` (keep `nand/system`, `keys/`) | `screenshots/` |
| NetherSX2 `xyz.aethersx2.tturnip` | recent (shared_prefs/`inis/`) | `memcards/`, `sstates/` (keep `bios/`) | `snaps/` |
| MelonDS `me.magnum.melonds.nightly` | recent (shared_prefs) | save/state files (keep `firmware.bin`, bios7/9) | — |
| Dolphin `org.dolphinemu.dolphinemu` | recent (shared_prefs) | `GC/` memcards, Wii NAND saves, `StateSaves/` (keep `Sys/`) | `ScreenShots/` |
| Citra `org.citra.emu` | recent in `config/config.ini` | `sdmc/…/save`, NAND save (keep system NAND, `aes_keys`) | `screenshots/` |
| Flycast `com.flycast.emulator` | recent in `emu.cfg` | VMU `vmu_save_*.bin`, `*.state` (keep `dc_boot.bin`, `dc_flash.bin`) | — |
| **ES-DE** `org.es_de.frontend` (`/storage/$SDID/ES-DE`) | strip `<lastplayed>` + zero `<playcount>` in every `gamelists/*/gamelist.xml` (empties the auto "Last Played" collection) | — | `es_log*.txt` |

**D. Android traces (root):** clear recent-apps (restart launcher), `/data/system/usagestats/`,
logcat (`logcat -c`), clipboard.

> Exact subdirectory names vary slightly by emulator version; the implementation
> plan will confirm each against a test unit before finalizing the delete list. The
> **preserve allowlist is authoritative** — when unsure, a path is kept, not deleted.

### 4. `seal()` ordering (in `cas/provision.py`)

```
seal():
  guard: refuse if golden
  model cross-check (unchanged)
  clean_for_retail(adb, retail_clean)   # NEW — rooted, best-effort, NEVER aborts seal
      wifi_forget_all()                 #   forget shop WiFi, radio stays ON
      run retail-clean.sh (A/B/C/D per flags)
  uninstall Magisk → reboot bootloader → flash stock init_boot → confirm un-root
  hide Developer Options → disable USB debugging (drops adb)   # unchanged
```

`clean_for_retail` failures **warn and continue** — a clean failure must never leave a
unit half-un-rooted or stranded in fastboot.

## CLI / GUI surface

- `cas.cli clean [--serial S] [--dry-run]` — run/preview the wipe standalone (no un-root).
  `--dry-run` lists every target without deleting.
- `cas.cli seal` / `seal-all` — unchanged invocation; now wipe-then-un-root.
- GUI "Lock" button maps to `seal` (no UI change required); `provision` gains the WiFi+TZ
  step transparently.

## Error handling

- **WiFi join** failure (bad creds, no AP) ⇒ warn, continue provisioning. Setup never
  blocks on connectivity.
- **TZ pin** is independent `settings`/`setprop` calls; each best-effort.
- **Wipe** is per-target best-effort and cannot abort `seal()`.
- All new behavior is **no-op when its config is absent**, preserving current behavior
  for anyone without the new keys.

## Testing

Unit tests with the existing `FakeRunner`/`Adb(runner=…)` harness (`tests/test_cas.py`):

1. `wifi_join` emits `svc wifi enable` + `cmd wifi connect-network` with ssid/security/pw;
   `hidden` adds `-h`; `open` omits the password.
2. `wifi_forget_all` forgets each listed id and **never** issues `svc wifi disable`.
3. `apply_timezone` emits `setprop persist.sys.timezone Asia/Manila`, `auto_time_zone 0`,
   `auto_time 1`.
4. `clean_for_retail` issues the expected targets per enabled flag; a flag set false omits
   its targets.
5. **Safety:** no BIOS/firmware/keys/`nand/system`/`Sys/`/ROM path ever appears in a delete
   command (assert against the recorded calls).
6. **Resilience:** a failing delete does not abort; `seal()` still reaches the un-root flash.
7. **No-op:** absent `wifi`/`retail_clean` ⇒ no wifi/clean commands issued.

Manual pass on a test unit: join shop WiFi during provision → confirm online + clock
correct + TZ Manila → play a few games across PPSSPP/N64/RetroArch → `seal` →
verify: recents empty everywhere, ES-DE shows no last-played, saves/states gone, BIOS
intact and games still launch, no saved WiFi network (radio still on), launcher recents
empty, unit boots clean and un-rooted.

## Risks

- `cmd wifi connect-network` permission/format varies by Android build — verify on RP6
  and Odin2 early; fall back to `WifiConfigStore.xml` (root) only if needed.
- Over-deletion of a save dir that doubles as firmware (Citra/Eden NAND) — mitigated by
  the preserve allowlist and test #5.
