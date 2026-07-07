# CAS Windows USB drivers

Windows doesn't ship the USB drivers CAS needs to **flash** a unit (adb works out of the box; the
flash interfaces don't). These files install them **once per PC, into the Windows driver store**, so
every unit auto-binds on plug — **no Zadig, no per-device setup**.

Operators just run **`setup-windows.bat`** (in the kit root, one level up). Everything here is what it
uses under the hood.

## Why not Zadig?

Zadig binds WinUSB **per device instance** (keyed to each unit's USB serial), so a fleet with unique
serials makes you re-Zadig every unit. It also installs *generic* WinUSB, which doesn't register the
Android device-interface GUID `fastboot.exe` looks for — so `fastboot devices` stays blank even after a
"successful" Zadig install. Publishing a proper driver to the **driver store** fixes both: it binds by
USB id (reused across all same-id units) and registers the right interface.

## What gets installed

| Kind | Devices | USB id | Driver |
|------|---------|--------|--------|
| fastboot | Retroid / AYN / Odin (bootloader flash) | `VID_18D1&PID_D00D` (+ fastboot class `FF/42/03`) | WinUSB (in-box) |
| EDL | MANGMI (Qualcomm EDL / Firehose flash) | `VID_05C6&PID_9008` | usbser COM port (in-box) |

`18D1:D00D` is the **standard Android fastboot id** — the same across brands (verified on both Retroid
and MANGMI bootloaders), which is why one fastboot driver covers the whole fleet. The fallback INF also
matches the fastboot **interface class**, so a future model with a different id still binds.

## Source preference (best first)

`install-drivers.ps1` picks the most-trusted source available:

1. **Vendor driver** you dropped into `vendor\fastboot\` or `vendor\edl\` — signed by the vendor.
   - fastboot: Google USB Driver (`android_winusb.inf`) — https://developer.android.com/studio/run/win-usb
   - EDL: Qualcomm QDLoader HS-USB driver (`.inf`)
2. **Google's signed driver, downloaded** (fastboot only) if the bench is online.
3. **Bundled self-signed fallback INF** (`fallback\...`) — works offline. It only maps the device to an
   **in-box** Microsoft driver (winusb.sys / usbser.sys), so no third-party kernel binary is shipped;
   `install-drivers.ps1` self-signs its catalog with a machine-local cert (no Secure Boot changes).

## Adding a new device model

If a new handheld shows blank in `fastboot devices` even after setup, grab its bootloader **Hardware Id**
(Device Manager ▸ the device ▸ Details ▸ Hardware Ids, e.g. `USB\VID_xxxx&PID_xxxx`) and add that line to
`fallback\fastboot\cas-fastboot.inf` under both `[Cas.NTamd64]` and `[Cas.NTarm64]`, then re-run
`setup-windows.bat`. (Most devices are `18D1:D00D` and need nothing.)

## Note: EDL on Windows is not yet field-proven

The fastboot path is the everyday one. The EDL (MANGMI) path on Windows is new — validate it on a bench,
and if the in-box usbser fallback doesn't enumerate a COM port, drop the vendor QDLoader driver into
`vendor\edl\`, or flash MANGMI on Linux (EDL there is one udev rule: `scripts/setup-linux.sh`).
