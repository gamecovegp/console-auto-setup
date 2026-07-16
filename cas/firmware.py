"""Device-root-firmware library: list / match-by-identity / logic-check / ingest+version / resolve.

A firmware is a directory under `_firmware/<id>/` with:
  meta.json               id, label, device, brand, storage, flash_target, match{}, current, history[]
  versions/<version>/payload/   the firmware tree as-is (emmc|ufs + fh_loader/QSaharaServer/script)
  versions/<version>/version.meta.json   fingerprint, dev_code, os_version, storage, flash_target, source

DEVICE ROOT firmware only (handheld OS/boot images) — never emulator/app BIOS. CAS stores + advises;
it never flashes.
"""
import json
import os
import pathlib
import re
import shutil

from . import config


# ---------------------------------------------------------------------------
# JSON helpers (used throughout)
# ---------------------------------------------------------------------------

def _read_json(p):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return {}


def _write_json(p, obj):
    p = pathlib.Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Task 1 (adaptation): identity() as a free function (not Adb method)
# ---------------------------------------------------------------------------

def identity(adb):
    """One-shot device identity for auto-assign (getprop, no root). Calls adb.slot_suffix()/
    adb.boot_flash_target() which already exist on Adb.

    board_platform/android_release/bootdevice feed gate_check(). They are best-effort: a device that
    doesn't report one yields '', which makes that gate axis ABSTAIN rather than reject."""
    g = adb.getprop
    return {
        "serial": adb.serial or g("ro.serialno"),
        "device": g("ro.product.device"),
        "model": g("ro.product.model"),
        "brand": g("ro.product.manufacturer"),
        "soc": g("ro.soc.model"),
        "board_platform": g("ro.board.platform"),
        "android_release": g("ro.build.version.release"),
        "bootdevice": g("ro.boot.bootdevice"),
        "dev_code": g("ro.mangmi.dev.code"),
        "first_api": g("ro.product.first_api_level"),
        "slot": adb.slot_suffix(),
        "flash_target": adb.boot_flash_target(),
    }


def edl_only_device(identity):
    """True when a LIVE device is EDL-only — its bootloader fastboot can't write init_boot (e.g. MANGMI),
    so it MUST flash via EDL/Firehose from a firmware build. EDL-ness is otherwise a property of the
    firmware BUILD (QSaharaServer+fh_loader present), unreadable over adb; here we detect the DEVICE as
    MANGMI via ro.mangmi.dev.code (identity['dev_code'], present on MANGMI, absent elsewhere). Used by
    root_all/seal_all to fail-fast when no build resolves, instead of a doomed fastboot flash."""
    return bool(str((identity or {}).get("dev_code") or "").strip())


# A synthetic firmware id meaning "don't flash a library build — use the bundled DEFAULT kit init_boot".
# Selectable in the GUI firmware dropdown and assignable like any firmware, so an operator can EXPLICITLY
# pin a unit (e.g. a Retroid sharing the kalama image) to the default init_boot instead of leaving it as
# an unresolved "(no match)". resolve() short-circuits it to a no-build result; root_all then keeps the
# DEFAULT kit image (its firmware lookup yields no Firmware object).
DEFAULT_FW_ID = "(default kit)"


# ---------------------------------------------------------------------------
# Task 2 (adaptation): firmware_root / get_device_firmware / set_device_firmware
#                       in firmware.py reading/writing via the config module
# ---------------------------------------------------------------------------

def firmware_root():
    """Device-root-firmware library dir: the configured firmware_dir (e.g. a shared external CAS Profiles
    _firmware, so the catalog is shared across benches while goldens stay on a fast local library) or,
    unset, library_root()/_firmware. DEVICE ROOT firmware only — never emulator BIOS."""
    return config.firmware_dir()


def get_device_firmware():
    """Return {serial: {'firmware_id': str, 'version': str|None, 'manual': bool}} from config."""
    raw = config.load_config().get("device_firmware")
    out = {}
    if isinstance(raw, dict):
        for s, v in raw.items():
            if isinstance(v, dict) and v.get("firmware_id"):
                out[s] = {
                    "firmware_id": str(v["firmware_id"]),
                    "version": (str(v["version"]) if v.get("version") else None),
                    "manual": bool(v.get("manual")),
                }
    return out


def set_device_firmware(serial, firmware_id, version=None, manual=True):
    """Remember (firmware_id truthy) or forget (falsy) a device's firmware assignment.
    `version` is set ONLY for an explicit rollback pin; omit it so the firmware's current
    version propagates."""
    if not serial:
        return
    cfg = config.load_config()
    df = cfg.get("device_firmware")
    if not isinstance(df, dict):
        df = {}
    if firmware_id:
        rec = {"firmware_id": str(firmware_id), "manual": bool(manual)}
        if version:
            rec["version"] = str(version)
        df[serial] = rec
    else:
        df.pop(serial, None)
    cfg["device_firmware"] = df
    config.save_config(cfg)


# ---------------------------------------------------------------------------
# Task 3: Firmware class + list_firmware + find
# ---------------------------------------------------------------------------

class Firmware:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.id = self.path.name
        self.meta = _read_json(self.path / "meta.json")

    @property
    def label(self):
        return self.meta.get("label", self.id)

    @property
    def device(self):
        return self.meta.get("device", "")

    @property
    def flash_target(self):
        return self.meta.get("flash_target", "")

    @property
    def storage(self):
        return self.meta.get("storage", "")

    def match_rules(self):
        m = self.meta.get("match")
        return m if isinstance(m, dict) else {}

    def current(self):
        return self.meta.get("current")

    def versions(self):
        d = self.path / "versions"
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    def payload_dir(self, version=None):
        v = version or self.current()
        if not v:
            return None
        return self.path / "versions" / v / "payload"

    @property
    def flash_method(self):
        """How the patched ramdisk is written: 'fastboot' (bootloader fastboot — Retroid/AYN/Odin) or
        'edl' (Qualcomm Firehose — MANGMI, whose bootloader fastboot rejects flash). Recorded on ingest;
        falls back to detecting from the payload (Firehose tools present → edl) so firmwares ingested before
        this field existed still resolve correctly without a re-ingest."""
        m = self.meta.get("flash_method")
        if m:
            return m
        return "edl" if self.edl_tools() else "fastboot"

    def _payload_glob(self, pattern, version=None):
        pd = self.payload_dir(version)
        if not pd:
            return None
        hits = sorted(pd.glob(pattern))
        return hits[0] if hits else None

    def stock_boot_image(self, version=None):
        """The stock image to Magisk-patch from this build: <payload>/**/<flash_target>.img."""
        ft = self.flash_target or "init_boot"
        return self._payload_glob(f"**/{ft}.img", version)

    def edl_tools(self, version=None):
        """(QSaharaServer, fh_loader, prog_firehose) paths bundled in the payload, or None if not present.

        The two HOST tools are OS-specific executables: Windows needs the .exe builds (QSaharaServer.exe /
        fh_loader.exe, from Qualcomm QPST/QFIL), Linux the extensionless ELF. Running the wrong one blows
        up — on Windows `subprocess` raises `WinError 193: %1 is not a valid Win32 application` when handed
        a Linux ELF (exactly the MQ66 EDL-flash failure). So prefer the host-appropriate variant, but fall
        back to the other so flash_method still DETECTS an EDL build even when only the wrong-OS tool is
        present (the flasher then reports the mismatch cleanly instead of blaming the driver). The
        programmer .elf runs ON the device, so it's the same file on every host."""
        def host(stem):
            exe = self._payload_glob(f"**/{stem}.exe", version)
            bare = self._payload_glob(f"**/{stem}", version)
            return (exe or bare) if os.name == "nt" else (bare or exe)
        q = host("QSaharaServer")
        f = host("fh_loader")
        p = (self._payload_glob("**/prog_firehose_ddr.elf", version)
             or self._payload_glob("**/prog_firehose*.elf", version))
        return (q, f, p) if (q and f and p) else None

    def init_boot_geometry(self, slot, version=None):
        """Parse the firmware's rawprogram for label 'init_boot<slot>' (slot '_a'/'_b'/'') →
        {sector_size, num_sectors, partition, start_sector, start_byte_hex}, or None. Used to build the
        one-entry rawprogram for an EDL write of the patched init_boot to the right offset/LUN."""
        pd = self.payload_dir(version)
        if not pd:
            return None
        label = (self.flash_target or "init_boot") + (slot or "")
        for xml in sorted(pd.glob("**/rawprogram*.xml")):
            try:
                txt = xml.read_text(errors="ignore")
            except OSError:
                continue
            m = re.search(r'<program\b[^>]*\blabel="' + re.escape(label) + r'"[^>]*/>', txt)
            if not m:
                continue
            tag = m.group(0)

            def g(attr):
                mm = re.search(attr + r'="([^"]*)"', tag)
                return mm.group(1) if mm else None
            return {"sector_size": g("SECTOR_SIZE_IN_BYTES"), "num_sectors": g("num_partition_sectors"),
                    "partition": g("physical_partition_number"), "start_sector": g("start_sector"),
                    "start_byte_hex": g("start_byte_hex")}
        return None

    def __repr__(self):
        return f"<Firmware {self.id} device={self.device} flash={self.flash_target}>"


def list_firmware(root):
    """All Firmware under `root`: lists subdirectories that contain a meta.json.
    Non-directory entries (files such as index.json) and dotfile dirs are skipped."""
    root = pathlib.Path(root)
    if not root.is_dir():
        return []
    return [
        Firmware(p) for p in sorted(root.iterdir())
        if p.is_dir() and not p.name.startswith(".") and (p / "meta.json").exists()
    ]


def find(firmware_id, root):
    """Return Firmware for firmware_id under root, or None if not found."""
    p = pathlib.Path(root) / (firmware_id or "")
    return Firmware(p) if (p / "meta.json").exists() else None


def default_kit_firmware(root):
    """The library Firmware designated (in config) as the '(default kit)', or None. Lets resolve() back
    the '(default kit)' choice with a REAL library build's init_boot (present on the operator's library
    drive) instead of the hard-coded odin2 path that ships in no release. None when unset or the
    designated id is no longer in the library (Root then falls back to the hard-coded path as before)."""
    fid = config.default_kit_firmware()
    return find(fid, root) if fid else None


# ---------------------------------------------------------------------------
# Task 2b: _storage_from_bootdevice() + _android_major() — gate helpers
# ---------------------------------------------------------------------------

def _storage_from_bootdevice(bootdevice):
    """'ufs' | 'emmc' | '' from ro.boot.bootdevice (e.g. '1d84000.ufshc' -> 'ufs', '4804000.sdhci' ->
    'emmc'). '' = unrecognized, which makes the storage gate axis ABSTAIN.

    UNVERIFIED against real hardware — the '' fallback is deliberate: a wrong guess here degrades to
    'storage does not gate' (legacy behavior), never to a wrong flash."""
    b = (bootdevice or "").strip().lower()
    if "ufs" in b:
        return "ufs"
    if "sdhci" in b or "mmc" in b:
        return "emmc"
    return ""


def _android_major(release):
    """'13.1' -> '13'; '' -> ''. Android gates on MAJOR only."""
    return str(release or "").strip().split(".")[0]


# ---------------------------------------------------------------------------
# Task 4: match() — suggestion by device identity
# ---------------------------------------------------------------------------

def _serial_prefix_hit(rules, serial):
    return bool(serial) and any(serial.startswith(p) for p in (rules.get("serial_prefix") or []))


def match(identity_dict, root):
    """Suggest a Firmware for a device identity. Score per rule (serial_prefix=3, device=2, brand=1,
    soc=1); the unique highest score wins. Tie or zero -> None (operator selects). Returns
    (Firmware, current_version) or None."""
    serial = identity_dict.get("serial") or ""
    scored = []
    for fw in list_firmware(root):
        r = fw.match_rules()
        score = 0
        if _serial_prefix_hit(r, serial):
            score += 3
        if r.get("device") and r["device"] == identity_dict.get("device"):
            score += 2
        if r.get("brand") and r["brand"].lower() == (identity_dict.get("brand") or "").lower():
            score += 1
        if r.get("soc") and r["soc"] == identity_dict.get("soc"):
            score += 1
        if score > 0:
            scored.append((score, fw))
    if not scored:
        return None
    top = max(s for s, _ in scored)
    winners = [fw for s, fw in scored if s == top]
    if len(winners) != 1:
        return None
    fw = winners[0]
    return (fw, fw.current())


# ---------------------------------------------------------------------------
# Task 5: logic_check() — brick-guard
# ---------------------------------------------------------------------------

def _strip_slot(part):
    for suf in ("_a", "_b"):
        if part.endswith(suf):
            return part[:-2]
    return part


def logic_check(firmware, identity_dict):
    """Validate a (suggested or chosen) firmware against the LIVE device. Returns (ok, [warnings]).
    A warned firmware is still selectable — the operator just sees why (brick-guard)."""
    warns = []
    live_base = _strip_slot(identity_dict.get("flash_target") or "")
    if firmware.flash_target and live_base and firmware.flash_target != live_base:
        warns.append(
            f"firmware expects '{firmware.flash_target}' but device exposes '{live_base}'"
        )
    if firmware.device and identity_dict.get("device") and firmware.device != identity_dict["device"]:
        warns.append(
            f"firmware device '{firmware.device}' != device '{identity_dict['device']}'"
        )
    prefixes = firmware.match_rules().get("serial_prefix") or []
    serial = identity_dict.get("serial") or ""
    if prefixes and serial and not any(serial.startswith(p) for p in prefixes):
        warns.append(f"serial '{serial}' matches none of {prefixes}")
    return (not warns, warns)


# ---------------------------------------------------------------------------
# Task 6: detect_build + ingest
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"(\d{8})\.(\d{6})")   # …user.YYYYMMDD.HHMMSS -> groups


def _grep_value(paths, needle, cap=80):
    """First ASCII value after `needle` found scanning files in 1 MiB chunks (best-effort; a match split
    across a chunk boundary is skipped — fine for build.prop-in-image text). '' if not found."""
    nb = needle.encode()
    for p in paths:
        try:
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    i = chunk.find(nb)
                    if i >= 0:
                        j, out = i + len(nb), bytearray()
                        while j < len(chunk) and chunk[j] not in (0, 10, 13) and len(out) < cap:
                            out.append(chunk[j])
                            j += 1
                        return out.decode("ascii", "ignore").strip()
        except OSError:
            pass
    return ""


def detect_build(src):
    """Inspect a raw firmware build folder and return what we can determine without a network:
    storage (emmc|ufs), flash_target (init_boot|boot), version (YYYYMMDD-HHMMSS), and best-effort
    device / dev_code / os_version / fingerprint from the partition images."""
    src = pathlib.Path(src)
    storage = "emmc" if (src / "emmc").is_dir() else ("ufs" if (src / "ufs").is_dir() else "")
    base = (src / storage) if storage else src
    labels = ""
    for xml in sorted(base.glob("rawprogram*.xml")):
        try:
            labels += xml.read_text(errors="ignore")
        except OSError:
            pass
    flash_target = "init_boot" if "init_boot" in labels else ("boot" if "boot" in labels else "")
    # A Firehose/EDL build ships QSaharaServer + fh_loader + a prog_firehose programmer → flash via EDL
    # (its bootloader fastboot can't write, e.g. MANGMI). Otherwise the patched ramdisk goes via fastboot.
    is_edl = ((src / "QSaharaServer").exists() and (src / "fh_loader").exists()
              and bool(list(base.glob("prog_firehose*.elf"))))
    m = _VERSION_RE.search(src.name)
    version = f"{m.group(1)}-{m.group(2)}" if m else src.name
    imgs = sorted(base.glob("super_*.img")) + sorted(base.glob("system_*.img"))
    return {
        "storage": storage,
        "flash_target": flash_target,
        "flash_method": "edl" if is_edl else "fastboot",
        "version": version,
        "device": _grep_value(imgs, "ro.product.system.device="),
        "dev_code": _grep_value(imgs, "ro.mangmi.dev.code="),
        "os_version": _grep_value(imgs, "ro.mangmi.os.version="),
        "fingerprint": _grep_value(imgs, "ro.build.fingerprint="),
        # Gate fields. Both chip spellings are captured: gate_check() compares board_platform to
        # board_platform and soc to soc, never across ('kalama' != 'SM8550' would false-conflict).
        "board_platform": _grep_value(imgs, "ro.board.platform="),
        "soc": _grep_value(imgs, "ro.soc.model="),
        "android_release": _grep_value(imgs, "ro.build.version.release="),
    }


def ingest(src, root, firmware_id=None, label=None, match=None, copy=True):
    """Add a raw build folder to the library as a new version. Detects storage/flash_target/version/device,
    copies the tree to versions/<version>/payload, writes version.meta.json, sets current, appends history.
    Idempotent if the version already exists. Raises ValueError if the detected device contradicts an
    existing firmware id's device (anti-misfile guard). Returns the Firmware."""
    src = pathlib.Path(src)
    root = pathlib.Path(root)
    info = detect_build(src)
    if not info["flash_target"]:
        raise ValueError(
            f"{src.name}: not a device-firmware build (no boot/init_boot rawprogram labels)"
        )
    fid = firmware_id or f"{(info['device'] or src.name).lower().replace('_', '-')}"
    fw_dir = root / fid
    existing = _read_json(fw_dir / "meta.json")
    if existing.get("device") and info["device"] and existing["device"] != info["device"]:
        raise ValueError(
            f"device mismatch: id '{fid}' is {existing['device']}, build is {info['device']}"
        )

    version = info["version"]
    vdir = fw_dir / "versions" / version
    if vdir.is_dir():                                   # idempotent — already ingested
        return Firmware(fw_dir)

    if copy:
        shutil.copytree(src, vdir / "payload")
    else:
        (vdir / "payload").mkdir(parents=True)
    _write_json(vdir / "version.meta.json", {
        "fingerprint": info["fingerprint"],
        "dev_code": info["dev_code"],
        "os_version": info["os_version"],
        "storage": info["storage"],
        "flash_target": info["flash_target"],
        "flash_method": info["flash_method"],
        "board_platform": info["board_platform"],
        "soc": info["soc"],
        "android_release": info["android_release"],
        "source": str(src),
    })

    meta = existing or {"id": fid, "history": []}
    meta.setdefault("history", [])
    meta.update({
        "id": fid,
        "label": label or meta.get("label", fid),
        "device": info["device"] or meta.get("device", ""),
        "storage": info["storage"] or meta.get("storage", ""),
        "flash_target": info["flash_target"],
        "flash_method": info["flash_method"],
        "current": version,
    })
    # Seed match rules so a freshly-ingested firmware auto-matches immediately: start from the caller's
    # rules (e.g. serial_prefix for the MQ65/MQ66 split — both report device AIR_X), then fill from
    # detection whatever the caller didn't set. Without this a GUI ingest produced an empty match{} and
    # nothing ever auto-matched.
    #
    # The gate fields land here too — this is the zero-knowledge operator path: the build self-describes
    # its chip/android, so adding a NEW chip (e.g. the Odin 3 'sun' build) needs no operator input at
    # all. A field detection couldn't read stays absent, which makes that gate axis abstain (the core
    # rule: missing data never rejects, and never promotes).
    m = dict(match) if match else dict(meta.get("match") or {})
    for key in ("device", "board_platform", "soc", "android_release"):
        if info.get(key) and not m.get(key):
            m[key] = info[key]
    meta["match"] = m
    meta["history"].append({
        "version": version,
        "fingerprint": info["fingerprint"],
        "os_version": info["os_version"],
        "source": str(src),
    })
    _write_json(fw_dir / "meta.json", meta)
    log_event("", fid, version, "update", False)
    return Firmware(fw_dir)


# ---------------------------------------------------------------------------
# Task 7 (adapted): resolve() — uses fw-local get/set_device_firmware
# ---------------------------------------------------------------------------

def resolve(serial, identity_dict, root):
    """Decide the firmware for a connected device. Manual override (sticky) wins; else match() and
    remember it (manual=False). Version = pinned rollback or the firmware's current. Always runs
    logic_check. Returns a dict the UI/CLI render directly."""
    assigned = get_device_firmware().get(serial)
    fw, manual, suggested, pinned = None, False, None, None
    if assigned and assigned["firmware_id"] == DEFAULT_FW_ID:
        # Pinned to the default kit. If a library build is designated as the default kit, flash ITS
        # init_boot (present on the library drive) — this is the fix for '(default kit)' → 'missing
        # init_boot.img', since the hard-coded odin2 path ships in no release. Kept FRICTIONLESS by
        # design (no logic_check → no warning): the default kit is the operator's explicit "just use
        # this" choice, and the brick-relevant guard (an init_boot image must not go to a plain 'boot'
        # partition) lives in provision.root() itself (_img_kernel_size), independent of this. Running
        # logic_check here would ALSO fire a permanent false 'device' warning, because a firmware's
        # human device label ('Odin2 (kalama)') never equals the live ro.product.device codename. No
        # designation → keep the old behavior (fall back to the hard-coded path in root_all), so
        # nothing regresses for existing benches.
        dk = default_kit_firmware(root)
        if dk is not None:
            return {"firmware_id": DEFAULT_FW_ID, "version": dk.current(),
                    "manual": assigned.get("manual", True), "suggested": None,
                    "ok": True, "warnings": [], "firmware": dk}
        return {"firmware_id": DEFAULT_FW_ID, "version": None, "manual": assigned.get("manual", True),
                "suggested": None, "ok": True, "warnings": [], "firmware": None}
    if assigned:
        fw = find(assigned["firmware_id"], root)
        manual = assigned["manual"]
        pinned = assigned.get("version")
    if fw is None:
        m = match(identity_dict, root)
        if m:
            fw, _ = m
            suggested = fw.id
            set_device_firmware(serial, fw.id, version=None, manual=False)
            log_event(serial, fw.id, fw.current(), "suggest", False)
    if fw is None:
        return {
            "firmware_id": None,
            "version": None,
            "manual": False,
            "suggested": None,
            "ok": False,
            "warnings": ["no match — select manually"],
            "firmware": None,
        }
    ok, warns = logic_check(fw, identity_dict)
    return {
        "firmware_id": fw.id,
        "version": pinned or fw.current(),
        "manual": manual,
        "suggested": suggested,
        "ok": ok,
        "warnings": warns,
        "firmware": fw,
    }


# ---------------------------------------------------------------------------
# Task 7b: log_event() — assignment/update audit jsonl
# ---------------------------------------------------------------------------

def log_event(serial, firmware_id, version, action, manual, when=None):
    """Append one audit line to the per-machine firmware-history.<machine>.jsonl in history_dir().
    Best-effort (never raises)."""
    try:
        if when is None:
            import datetime
            when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        rec = {
            "when": when,
            "serial": serial,
            "firmware_id": firmware_id,
            "version": version,
            "action": action,
            "manual": bool(manual),
        }
        p = pathlib.Path(config.history_dir()) / config.history_filename("firmware-history")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Task 8 (CLI adaptation): main() — `python3 -m cas.firmware`
# ---------------------------------------------------------------------------

def main(argv=None):
    """Entry point for `python3 -m cas.firmware`. Four subcommands: list, ingest, show, assign."""
    import argparse
    from .adb import Adb
    from . import find_adb

    ap = argparse.ArgumentParser(
        prog="cas.firmware",
        description="Device-root-firmware library (list/ingest/show/assign)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    ig = sub.add_parser("ingest")
    ig.add_argument("src")
    ig.add_argument("--id", dest="firmware_id")
    ig.add_argument("--label")

    sh = sub.add_parser("show")
    sh.add_argument("--serial")

    asg = sub.add_parser("assign")
    asg.add_argument("--serial")
    asg.add_argument("id")

    args = ap.parse_args(argv)
    root = firmware_root()

    if args.cmd == "list":
        for f in list_firmware(root):
            print(
                f"{f.id:28} device={f.device:10} flash={f.flash_target:10} current={f.current()}"
            )

    elif args.cmd == "ingest":
        f = ingest(args.src, root, firmware_id=args.firmware_id, label=args.label)
        print(f"ingested {f.id} version {f.current()}")

    elif args.cmd in ("show", "assign"):
        a = Adb(serial=getattr(args, "serial", None), adb=find_adb("adb"))
        idn = identity(a)
        if args.cmd == "assign":
            set_device_firmware(idn["serial"], args.id, manual=True)
            log_event(idn["serial"], args.id, None, "assign", True)
        r = resolve(idn["serial"], idn, root)
        print(f"serial={idn['serial']} device={idn['device']} flash_target={idn['flash_target']}")
        print(
            f"firmware={r['firmware_id']} version={r['version']} "
            f"manual={r['manual']} ok={r['ok']}"
        )
        for w in r["warnings"]:
            print(f"  ! {w}")
        if r["firmware"]:
            print(f"  payload: {r['firmware'].payload_dir(r['version'])}")


if __name__ == "__main__":
    main()
