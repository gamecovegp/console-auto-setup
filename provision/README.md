# provision/ — modular per-emulator provisioning

One script per emulator + a `master.sh` orchestrator. Same scripts run **two ways**:

| Mode | How | Best for |
|---|---|---|
| **ADB (PC)** | host runs `./master.sh`, drives device over `adb` | bench/retail (nothing extra left on the unit) |
| **Shizuku (on-device, no PC)** | run inside `rish` (shell-uid shell) in Termux | field / PC-free setup |

`lib.sh` auto-detects the mode (override `CAS_MODE=adb|local`). All file ops are on-device `cp` from
the SD, so the **golden assets live on the SD** and work in both modes.

## Files
```
provision/
  lib.sh                 engine: SH/INPUT/ui_tap/ui_waittap/saf_grant/clone_into/setkey/grant_* (mode-aware)
  master.sh              runs emulators in fixed order; flags: RESET=1, PAYLOAD=..., CAS_MODE=...
  emulators/<emu>.sh     one per emulator (eden citra dolphin duckstation nethersx2 melonds ppsspp m64plus retroarch esde)
```
`uiauto.sh` (parent dir) is the standalone uiautomator helper used during development.

## Payload (golden assets) layout on the SD
`$PAYLOAD` (default `$SD/golden_payload`) — captured once from the golden:
```
eden/{keys,nand/system,gpu_drivers,config}   dolphin/Config   duckstation/bios   nethersx2/bios
retroarch/retroarch.cfg                       citra-emu/...    (+ melonds DS BIOS, ppsspp later)
```

## Usage
```
./master.sh                       # all, ADB mode
./master.sh eden retroarch        # subset
RESET=1 ./master.sh duckstation   # pm clear first (simulate fresh unit) — destructive
CAS_MODE=local sh master.sh       # on-device via rish (Shizuku)
```

## Provisioning classes (how each emulator is handled)
- **A — push + ES-DE handoff (click-free):** Eden. (boots the ROM URI ES-DE hands it.)
- **B — `pm grant` legacy/media (no SAF picker):** Citra (config also survives reset → zero-UI), M64Plus, PPSSPP.
- **C — SAF folder grant via `uiauto` macro:** Dolphin, DuckStation, NetherSX2, melonDS.
## Settings adjusted per emulator
Most settings ride the **cloned config**; `setkey` enforces overrides + per-unit values. `/data/data`-only
settings can't be pushed → set on the golden (they default acceptably for ES-DE launching).
| Emulator | how | settings applied |
|---|---|---|
| RetroArch | clone cfg + `setkey` (quoted) | overlay **OFF** (`input_overlay_enable=false`, hide-when-gamepad, opacity 0); ROM dir = `/storage/<sd>/ROMs`; binds + `video_driver=gl` from clone |
| Flycast | clone + `setkey` | `pvr.rend=4` (Vulkan); `VirtualGamepadTransparency=0` (overlay hidden) |
| Eden | clone config.ini (authoritative) | renderer/vsync(2)/resolution(3)/aspect(0)/nvdec(2)/disk-shader-cache + Turnip driver_path (key=value PAIRED with `\default`, so clone — not setkey) |
| Citra | clone /sdcard/citra-emu | graphics API/resolution/shaders; `game_storage_path=/storage` (portable) |
| Dolphin | clone files/Config | graphics (GFX.ini) + **button mapping** (GCPadNew.ini) + hotkeys + ISOPath |
| DuckStation/NetherSX2/M64Plus | — | settings live in `/data/data` (fast-boot, controller, renderer) → set on golden/in-app |
| melonDS | — | DS/DSi mode + BIOS in `/data/data` → in-app |
> `setkey` edits EXISTING keys only, so it runs AFTER the config clone (which supplies the full key set).
> RetroArch needs quoted values (`key = "value"`); Flycast/Citra use unquoted. Eden uses `key\default` pairs
> → don't `setkey` Eden (clone is authoritative).

## Shizuku (on-device, no PC) — setup + restrictions
**Setup (once per boot, no PC on Android 11+):** install Shizuku + Termux → start Shizuku via the device's
own **Wireless debugging** (Developer options) → in Termux use `rish` to get a shell-uid shell → run the
scripts (`CAS_MODE=local`). Termux needs `pkg install python` for `ui_tap`.

**Shizuku = ADB-level (uid 2000 shell), NOT root. So it CAN:**
`pm grant` · `appops set` · `pm clear`/install · `input tap`/`keyevent` · `uiautomator dump` · `am`/`monkey`
· `settings put` · read/write `/sdcard` + `/storage`.

**It CANNOT (root-only):**
- read/write other apps' `/data/data` (so it can't inject SAF grants or edit internal settings.ini directly)
- modify `/data/system` / system partitions
→ Class C still needs the **uiauto click-through** (which Shizuku CAN drive); GL-UI apps (PPSSPP, RetroArch)
  still ignore synthetic taps → their in-app buttons need a human/pixel tap. Shizuku stops on reboot (restart per boot).

## Known manual / GL-UI exceptions (both modes)
- **RetroArch cores** — Online Updater (online + GL-UI). Or route GC/Wii/3DS/DS to standalones.
- **PPSSPP** — memstick prompt's first/final OK are GL-UI; the folder picker between them is automatable.
- **melonDS DS BIOS** — internal; re-pick at SD `Bios` or use FreeBIOS.
- **Dolphin Wii** — add the `wii` folder as a 2nd grant pass.
