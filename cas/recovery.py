"""Post-failure recovery guidance: given an operation, a coarse phase, and the device's probed mode,
produce ordered, OS-aware 'here is the state, here is what to do next' steps. Pure (advise/summary) +
a best-effort device probe. Stdlib only. Surfaced by provision.py (per worker) and gui.py (popup + row
hint). See docs/superpowers/specs/2026-07-16-recovery-guidance-design.md."""
import dataclasses
import enum
import os

from . import adb as _adb


class DeviceMode(enum.Enum):
    BOOTED_ADB = "booted"          # reachable in adb
    ADB_OFFLINE = "offline"        # present but offline/unauthorized, or mid-reboot
    FASTBOOT = "fastboot"          # bootloader fastboot
    FASTBOOTD = "fastbootd"        # userspace fastbootd (advised identically to FASTBOOT)
    EDL_9008 = "edl"               # Qualcomm EDL / 9008 (black screen)
    ABSENT = "absent"              # not in adb, fastboot, or EDL
    SEALED_OK = "sealed"           # Lock finished, adb gone BY DESIGN — not a failure


def _is_windows():
    """Monkeypatchable OS check (tests flip this instead of os.name)."""
    return os.name == "nt"


_OP_NAME = {"root": "Root", "save": "Save", "download": "Download",
            "warmup": "Warm up", "lock": "Lock"}

# Per-operation safety note appended to every attention block so the operator knows retry is safe.
_OP_SAFETY = {
    "root": "The unit is unharmed — a failed root leaves it bootable; nothing was sealed.",
    "save": "Your existing profile was left untouched — a failed Save never overwrites the good golden.",
    "download": "Download is idempotent — re-running re-pushes the payload cleanly.",
    "warmup": "Warm-up changes nothing persistent — safe to re-run once the unit is booted.",
    "lock": "The unit may be partially sealed — re-run Lock to finish (safe to repeat).",
}


def _driver_hint_fastboot():
    if _is_windows():
        return ("If `fastboot devices` is empty, the bootloader USB driver is missing — "
                "run scripts\\setup-windows.bat (Administrator), then replug.")
    return "If `fastboot devices` is empty, install the android-udev rules, then replug."


def _driver_hint_edl():
    if _is_windows():
        return ("Windows needs the QDLoader 9008 driver + QPST host tools — "
                "run scripts\\install-edl-host-tools.ps1 if EDL tooling is missing.")
    return "On Linux the /dev/ttyUSB port needs access — scripts/setup-linux.sh installs the udev rule."


@dataclasses.dataclass
class Recovery:
    state_label: str
    steps: list
    operation: str
    needs_attention: bool = True

    def log_block(self):
        lines = [f"  STATE: {self.state_label}", "  DO NEXT:"]
        lines += [f"    {i}. {s}" for i, s in enumerate(self.steps, 1)]
        return "\n".join(lines)

    def row_hint(self):
        first = self.steps[0] if self.steps else ""
        return f"{self.state_label} — {first}".replace("\n", " ")

    def popup_line(self, serial):
        first = self.steps[0] if self.steps else ""
        return f"  {serial}  {self.state_label} — {first}".replace("\n", " ")


def _effective_mode(phase, mode):
    """When the device is ABSENT (nothing visible), fall back to the phase to guess the mode: a unit that
    vanished during an EDL write is dark in 9008; during a fastboot write it's in the bootloader."""
    if mode is DeviceMode.ABSENT:
        if phase == "edl_flash":
            return DeviceMode.EDL_9008
        if phase == "fastboot_flash":
            return DeviceMode.FASTBOOT
    return mode


def advise(operation, phase, mode):
    op_verb = _OP_NAME.get(operation, operation)
    eff = _effective_mode(phase, mode)
    safety = _OP_SAFETY.get(operation, "")

    if eff is DeviceMode.SEALED_OK:
        return Recovery("SEALED (adb disconnects by design)",
                        ["Nothing to do — the unit sealed and adb went away as expected."],
                        operation, needs_attention=False)

    if eff is DeviceMode.EDL_9008:
        steps = [f"Hold Power ~12s to boot back to Android, then replug and re-run {op_verb}.",
                 _driver_hint_edl(), safety]
        return Recovery("EDL / 9008 (black screen)", [s for s in steps if s], operation)

    if eff in (DeviceMode.FASTBOOT, DeviceMode.FASTBOOTD):
        steps = [f"Run `fastboot reboot` to return to Android, then re-run {op_verb}.",
                 _driver_hint_fastboot(), safety]
        return Recovery("fastboot / bootloader", [s for s in steps if s], operation)

    if eff is DeviceMode.ADB_OFFLINE:
        steps = ["Wait ~30s for the unit to reappear; if it returns 'unauthorized', unlock the screen "
                 "and tap 'Allow USB debugging'.",
                 f"If it stays gone, replug a DATA cable (not charge-only) and re-run {op_verb}.", safety]
        return Recovery("offline / rebooting", [s for s in steps if s], operation)

    if eff is DeviceMode.ABSENT:
        steps = [f"Hold Power ~10-12s to force a reboot, watch for the boot logo, replug, re-run {op_verb}.",
                 "If it never shows in adb/fastboot/EDL, try a different cable or USB port.", safety]
        return Recovery("not visible (black screen / cable?)", [s for s in steps if s], operation)

    # BOOTED_ADB — still online, so the failure is operational, not a mode problem.
    online = {
        "root": "Root reported a failure but the unit is still online — check the log above; safe to re-run Root.",
        "save": "Not rooted? run Root first. Otherwise the capture hit an error — check the log; re-run Save.",
        "download": "Restore reported an error — check the log above; the unit is still online, re-run Download.",
        "warmup": "An app failed to launch (maybe not installed) — run Download first, then re-run Warm up.",
        "lock": "Lock reported a failure but the unit is still online — check the log; re-run Lock.",
    }
    return Recovery("still online", [online.get(operation, f"Re-run {op_verb}."), safety], operation)


def _fastboot_present(fb):
    """True iff a device is listed in `fastboot devices` (any non-empty line)."""
    try:
        out = fb.devices() or ""
    except Exception:
        return False
    return any(ln.strip() for ln in out.splitlines())


def probe_mode(adb, fb, edl_ports=None):
    """Best-effort current mode for this device. adb: Adb; fb: Fastboot; edl_ports: callable()->list
    (defaults to adb._edl_ports). Every probe is wrapped so a probe error never raises into the caller."""
    edl_ports = edl_ports or _adb._edl_ports
    try:
        st = adb.state()
    except Exception:
        st = ""
    if st == "device":
        return DeviceMode.BOOTED_ADB
    if st in ("offline", "unauthorized"):
        return DeviceMode.ADB_OFFLINE
    # st == "" -> not in adb; check fastboot, then EDL.
    if _fastboot_present(fb):
        return DeviceMode.FASTBOOT
    try:
        if edl_ports():
            return DeviceMode.EDL_9008
    except Exception:
        pass
    return DeviceMode.ABSENT


def summary_popup(recs, action):
    """One end-of-run dialog body listing every device that needs attention, or None if none do.
    `recs` is {serial: Recovery|None}."""
    hot = [(s, r) for s, r in recs.items() if r is not None and r.needs_attention]
    if not hot:
        return None
    head = f"{len(hot)} device(s) need attention after {action}:\n"
    return head + "\n".join(r.popup_line(s) for s, r in hot)
