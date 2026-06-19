# root/ — capture→restore provisioning (Magisk root)

Fast, unattended, model-by-model. Clones the golden's **emulator brain** onto each unit.

## Is it "exactly the same"?
**Yes for the emulators, no for the device identity — by design.**
- The clone copies each app's actual data, so every unit's emulators behave **byte-identically** to the
  golden: same settings, **overlay removed, key bindings, button + game mappings, cores, BIOS, keys**.
- It does **not** copy the golden's Wi-Fi passwords / Google account / device serial. Each unit stays its
  own device; only the emulator state is transplanted. (A raw full-`/data` byte clone would drag identity
  onto every unit — wrong for a product.)

## What gets cloned
| Source | Contents |
|---|---|
| `/data/data/<pkg>/` | settings, key binds, mappings, **RetroArch cores**, grant references, save configs |
| `/sdcard/Android/data/<pkg>/` | firmware, BIOS, keys, GPU driver |
| `<sd>/retroarch-cores/*.so` | the 205 fetched cores → bulk-copied into RetroArch's internal dir |
| SD (already there) | ROMs, ES-DE config |

## The two things that aren't a pure copy (handled automatically)
1. **App UID + SELinux** — `/data/data` is owned by the app's UID, which differs per device, and files
   carry SELinux labels. Restore re-`chown`s to **this unit's** UID and runs `restorecon`.
2. **SD-card serial** — the SAF game-folder grant embeds the card's unique hardware serial, so restore
   rewrites the golden serial → this unit's serial in the configs. (ES-DE-launched emulators don't need it.)

## Files
```
root/
  lib-root.sh   PKGS + helpers (runs as root)
  capture.sh    GOLDEN, once:  su -c 'sh .../root/capture.sh'   -> <sd>/golden_root_payload/
  restore.sh    each UNIT:      su -c 'sh .../root/restore.sh'   (re-own, relabel, serial-rewrite, cores)
```

## Per-model workflow (root sourced once per model)
**Once per model:** get the model's `init_boot.img` → Magisk patch → flash (FastbootD) → root →
`dd` stock+patched images to your library → `capture.sh` on the golden.
**Per unit forever after:**
```
fastboot flash init_boot_a <model>_magisk_patched.img      # (use FastbootD if "Flashing not allowed")
adb shell su -c 'sh /storage/<sd>/provision/root/restore.sh'
adb reboot
# optional: flash stock init_boot back to ship un-rooted (files persist)
```
→ one unattended pass, cores included. ~6–8 min/unit.

## ⚠ Needs validation on first root
- **SAF grant restore** — `restore.sh` step 4: either merge the serial-rewritten `urigrants.xml` into the
  unit's grant store, OR fall back to the no-root `uiauto saf_grant` (which we know works) for the SAF
  emulators. The exact grant-store path (`/data/system/urigrants.xml` vs `/data/system_de/0/...`) is
  confirmed during the first root.
- **tar `--exclude`** — falls back to a full tar if the device's toybox lacks it (just larger payload).
- Test on ONE unit end-to-end (capture golden → restore → reboot → boot a game per system) before volume.

## Why root (vs the no-root `../` toolkit)
Cores become a bulk `cp` instead of ~10–15 min of per-core taps; `/data/data` settings + grants clone
instead of in-app steps; the whole per-unit run is unattended. Trade-off: one `init_boot` per model.
The no-root Shizuku toolkit (`provision/`) stays the **universal fallback** for un-rootable units.
