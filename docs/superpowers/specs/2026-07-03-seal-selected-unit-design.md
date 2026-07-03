# Seal selected unit — single-unit operator action in Settings

**Date:** 2026-07-03
**Status:** Approved (design)

## Problem

The **Settings** menu holds single-device *operator* lifecycle actions. Today it has
exactly one: **"Release selected unit (un-provision)…"** — `release_selected()` in
`cas/gui.py:1959`, which clears the Companion's Device-Owner lockdown on the one
selected unit (for RMA / repair / resale).

There is no matching single-unit *seal* action. The full retail seal exists only as the
batch **③ Lock** workflow step (`_run_batch("lock", …)` / `_stage("lock", …)` →
`PV.seal_all`). We want the operator to be able to seal **one specific unit on demand**
— a returned unit, a one-off, a re-seal — from the same Settings menu, pairing Seal with
Release as the two single-unit lifecycle actions.

## Scope

- **In:** one new Settings menu entry + one `seal_selected()` method that runs the full
  retail seal on the single selected device.
- **Out:** any new provisioning logic. Behavior is the single-device slice of ③ Lock —
  deliberately identical, no divergence (confirmed with the user).

## Approach (chosen)

**Reuse `PV.seal_all` on a one-device list.** `seal_all` already resolves everything
`seal()` needs, per device:

- stock init_boot (profile `stock_init_boot` override → bundled default kit),
- device-root firmware / EDL Firehose flasher when the unit's bootloader can't write
  (e.g. MANGMI),
- `model_match` brick-guard (won't flash a wrong-model init_boot unless forced),
- golden-guard (`skip-golden` — never seals the master),
- `on_critical` flash-critical brick-warning gate.

Scoping it to `[(serial, "device")]` gives "seal this one unit" with **zero new code
paths** and guaranteed parity with ③ Lock.

### Rejected alternatives

- **Call `seal()` directly** — the GUI would re-resolve stock init_boot + fastboot/EDL
  flasher itself, duplicating ~30 lines `seal_all` already does and risking drift from
  ③ Lock. Rejected.
- **No code; document that ③ Lock already seals a single selected device** — true, but
  loses the Release/Seal symmetry and the Settings discoverability we want. Rejected.

## Design

### 1. Menu entry (`cas/gui.py`, ~213-221)

Add directly **above** the Release entry (lifecycle order: seal = ship, release = undo):

```python
setm.add_command(label="Seal selected unit (retail lock)…", command=self.seal_selected)
setm.add_command(label="Release selected unit (un-provision)…", command=self.release_selected)
```

### 2. `seal_selected()` (new method, mirrors `release_selected`)

```
serial = self._selected_serial()
if not serial:  -> messagebox.showinfo("CAS", "Select ONE device in the list first."); return

confirm (strong, single-device):
    "Retail-seal <serial>?  This un-roots the unit (flashes stock init_boot, ~2-3 min),
     hides Developer options, and disables USB debugging — adb WILL disconnect.
     The golden is skipped. Use for a one-off / re-seal outside the ③ Lock batch."
if not askyesno: return

pm, force = self._profile_map([serial])   # resolves model_match + stock override + manual force,
                                          # exactly as the batch ③ Lock does

def work():
    cev = self.cancel_event
    res = PV.seal_all(
        lambda s: Adb(serial=s, adb=self.adb_bin, cancel=cev),
        lambda s: Fastboot(serial=s, fastboot=self.fb_bin, cancel=cev),
        [(serial, "device")],
        profiles_root=self.profiles_root, appdir=APPDIR, log=self.log,
        profile_map=pm, force_serials=force, on_critical=self._on_flash_critical)
    self.win.after(0, self.refresh_devices)
    return res                            # dict -> _report renders ✅/⏭/❌

self._run_bg(work, label=f"Sealing {serial}")
```

Returning the `{serial: (status, detail)}` dict lets the existing `_report` (gui.py:843)
render the same pass/skip/fail line the batch produces.

## Edge cases — all handled by reusing `seal_all` (no new logic)

| Case | Result |
|------|--------|
| Selected unit is the golden | `skip-golden` (never seals the master) |
| No profile assigned + no model auto-match | `no-profile` |
| EDL unit, firmware unusable | `fail` with the reason |
| Wrong-model init_boot | refused unless the profile was manually assigned (force) |
| Cancel mid-flash | existing `_on_flash_critical` brick-warning gate |
| Already busy | `_run_bg` refuses (single-op guard) |
| No / multi selection | `_selected_serial()` returns None → info dialog |

## Testing

Mirror the `App.__new__(App)` harness (`tests/test_cas.py:2357`, no real Tk):

- **no selection** → `_selected_serial()` None → `showinfo`, `PV.seal_all` NOT called.
- **confirm = no** → `askyesno` False → `PV.seal_all` NOT called.
- **confirm = yes** → `PV.seal_all` called once with `devices == [(serial, "device")]`
  and the `pm`/`force` from `_profile_map([serial])`.

Monkeypatch `_selected_serial`, `cas.gui.messagebox` (`askyesno`/`showinfo`),
`_run_bg` (invoke the passed `fn` synchronously to capture the call), and `PV.seal_all`.

## Files touched

- `cas/gui.py` — one menu line + `seal_selected()` method.
- `tests/test_cas.py` — the three assertions above.
