"""Device-root-firmware library: list / match-by-identity / logic-check / ingest+version / resolve.

A firmware is a directory under `_firmware/<id>/` with:
  meta.json               id, label, device, brand, storage, flash_target, match{}, current, history[]
  versions/<version>/payload/   the firmware tree as-is (emmc|ufs + fh_loader/QSaharaServer/script)
  versions/<version>/version.meta.json   fingerprint, dev_code, os_version, storage, flash_target, source

DEVICE ROOT firmware only (handheld OS/boot images) — never emulator/app BIOS. CAS stores + advises;
it never flashes.
"""
import datetime
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
    def ships_rooted(self):
        """True when this build is DELIBERATELY rooted and its units ship that way — its stock image is
        Magisk-patched by design (e.g. the RP5's 905MHz overclock kernel, distributed only as a
        root+OC image). ③ Lock then skips the un-root flash, which could only re-root the unit, and does
        the retail lockdown alone. Opt-in per build: absent/False means a patched image is REFUSED."""
        return bool(self.meta.get("ships_rooted", False))

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

    def payload_size(self, version=None):
        """Total bytes of the whole payload tree (0 if absent) — what the library window shows per row.
        NOT _payload_scan_size_bytes(), which deliberately measures only the super_*/system_*.img files
        detect_build() greps. Best-effort and exception-free: this is called from a UI worker thread, and
        an unmounted or half-copied payload must render as a size, never raise."""
        pd = self.payload_dir(version)
        if not pd:
            return 0
        total = 0
        try:
            for f in pd.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

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

    CONFIRMED on real hardware: a live Retroid Pocket 6 (serial caecc295) returned
    ro.boot.bootdevice = '1d84000.ufshc' — the exact string this design guessed — and this probe
    correctly mapped it to 'ufs'. The '' fallback stays deliberate regardless: an unrecognized value
    degrades to 'storage does not gate' (legacy behavior), never to a wrong flash."""
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


# ---------------------------------------------------------------------------
# Task 4b: gate_check() — the core rule (hard compatibility gate, before scoring)
# ---------------------------------------------------------------------------

def gate_check(firmware, identity_dict):
    """Hard compatibility gate, evaluated BEFORE scoring. Returns (ok, reason, agreed).

    CORE RULE: reject only on a KNOWN CONFLICT — never on missing data. An axis gates only when the
    SAME field is populated on BOTH sides and the values differ; absence on either side abstains. That
    is what lets today's chip-less meta.json entries keep resolving exactly as they always have.

    NEVER compare ro.board.platform against ro.soc.model. 'kalama' and 'SM8550' name the same silicon,
    so a cross-prop compare would read as a conflict and disqualify the whole library. Each chip prop is
    compared only against its own counterpart.

    PLATFORM OUTRANKS SKU: when board_platform is populated on both sides and AGREES, a differing
    ro.soc.model does NOT reject (and does not affirm). board_platform names the platform (kalama);
    soc names the SKU (SM8550 vs QCS8550 — the IoT variant of the same Snapdragon 8 Gen 2). Same
    platform means the same ramdisk. soc still rejects on conflict when board_platform did not
    compare, so it remains the fallback chip axis for a device or build that reports only soc.

    `agreed` = how many CHIP axes (board_platform, soc — and ONLY those two) actually COMPARED AND
    AGREED (as opposed to abstaining). agreed>0 is a positive affirmation of compatibility and makes a
    firmware a candidate even at score 0 — which is what makes cross-model reuse work at all (an RP6 on
    the Odin 2 build scores zero on every soft rule). agreed==0 is a vacuous pass: the gate affirmed
    nothing, so match() still requires a positive score, preserving today's behavior for un-backfilled
    entries.

    WHY ONLY CHIP COUNTS: `agreed` exists to answer "did the gate affirm the CHIP?" — because chip
    agreement is the only evidence strong enough to justify selecting a firmware that scores zero on
    every soft rule (the proven Retroid Pocket 6 ≡ AYN Odin 2 cross-brand pair). Android and storage
    are corroborating axes: strong enough to REJECT on a known conflict (below), far too weak to
    PROMOTE on agreement. Storage in particular is a 1-bit axis — "we are both UFS" says nothing about
    ramdisk compatibility — yet counting it into `agreed` let a chip-less, storage-only entry (e.g. a
    build whose payload has no super image to grep a chip out of — a permanent universal wildcard for
    its storage type) become a score-0 candidate for EVERY device sharing that storage, chip be damned.

    CONTRACT: `agreed` is 0 whenever `ok` is False. A rejected firmware affirmed nothing — whatever
    partial agreement accumulated on earlier axes before the conflicting axis tripped is discarded, so
    a caller can safely read `agreed > 0` as "compatible" without checking `ok` first.
    """
    r = firmware.match_rules()
    agreed = 0

    # board_platform is the PLATFORM; soc is the SKU. Same platform + different SKU = the same
    # silicon and the same ramdisk: a live RP6 reports soc=QCS8550 (the IoT SKU) where a generic
    # kalama build's super image records SM8550, and board_platform is 'kalama' on both sides. So
    # platform agreement OUTRANKS a soc conflict — otherwise that build falsely rejects the RP6 and
    # _no_match_reasons blames "this chip (kalama)" when kalama agreed perfectly.
    # A platform CONFLICT still rejects unconditionally, and soc still rejects when no platform
    # compared (it remains the fallback chip axis).
    platform_agreed = False
    want, live = r.get("board_platform"), identity_dict.get("board_platform")
    if want and live:
        if want.strip().lower() != live.strip().lower():
            return (False, f"chip {live} != firmware {want}", 0)
        agreed += 1
        platform_agreed = True

    want, live = r.get("soc"), identity_dict.get("soc")
    if want and live:
        if want.strip().lower() != live.strip().lower():
            if not platform_agreed:
                return (False, f"chip {live} != firmware {want}", 0)
            # else: the platform already agreed — a differing SKU neither rejects nor affirms.
        else:
            agreed += 1

    want_a, live_a = r.get("android_release"), identity_dict.get("android_release")
    if want_a and live_a:
        if _android_major(want_a) != _android_major(live_a):
            return (False, f"android {live_a} != firmware {want_a}", 0)
        # Agreement here corroborates but does not AFFIRM the chip — does not count into `agreed`.

    want_s = firmware.storage
    live_s = _storage_from_bootdevice(identity_dict.get("bootdevice"))
    if want_s and live_s:
        if want_s.strip().lower() != live_s:
            return (False, f"storage {live_s} != firmware {want_s}", 0)
        # Same as android: rejects on conflict, never promotes on agreement (see docstring above).

    return (True, None, agreed)


def match(identity_dict, root):
    """Suggest a Firmware for a device identity. TWO STAGES:

      1. gate_check() — a hard compatibility gate (chip/android/storage). A rejected firmware is not a
         candidate at all; no soft rule can promote it. This is what fixes the latent bug where
         serial_prefix (3) outvoted soc (1) and could carry a wrong-chip build to the top.
      2. score, among survivors only — serial_prefix=3, device=2, brand=1. `soc` is NOT scored: chip is
         a gate now, not a tiebreaker.

    Candidacy: score>0, OR an AFFIRMED gate pass (agreed>0). The affirmed case is essential — an RP6 on
    the Odin 2 build scores zero on every soft rule, and the gate's affirmation is the only evidence
    there is. A VACUOUS pass (agreed==0, a legacy chip-less entry) affirms nothing and still requires a
    positive score, preserving today's behavior.

    The unique highest score wins. Tie -> None (operator selects). Returns (Firmware, version) or None.
    """
    serial = identity_dict.get("serial") or ""
    scored = []
    for fw in list_firmware(root):
        ok, _reason, agreed = gate_check(fw, identity_dict)
        if not ok:
            continue
        r = fw.match_rules()
        score = 0
        if _serial_prefix_hit(r, serial):
            score += 3
        if r.get("device") and r["device"] == identity_dict.get("device"):
            score += 2
        if r.get("brand") and r["brand"].lower() == (identity_dict.get("brand") or "").lower():
            score += 1
        if score > 0 or agreed > 0:
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
    A warned firmware is still selectable — the operator just sees why (brick-guard).

    NOTE: there is deliberately NO 'firmware device != device' warning. A firmware's human device label
    ('Odin2 (kalama)') never equals a live ro.product.device codename, so that warning fired on every
    legitimate cross-brand match (the RP6-on-the-Odin-2-build pair is PROVEN to boot) — always true,
    never meaningful, and the reason operators learned to click through warnings. Chip compatibility is
    enforced by gate_check() instead, which rejects rather than warns.
    """
    warns = []
    live_base = _strip_slot(identity_dict.get("flash_target") or "")
    if firmware.flash_target and live_base and firmware.flash_target != live_base:
        warns.append(
            f"firmware expects '{firmware.flash_target}' but device exposes '{live_base}'"
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


# EDL-only extras. The host tools and programmer are found via edl_tools(); these globs cover the
# files no accessor returns directly but the flash still needs — the rawprogram XML init_boot_geometry()
# parses, plus two reserved patterns that cost kilobytes: patch*.xml, and the *devprg*.melf programmer a
# kalama EDL build would use (no build in the library uses one today, but slimming it away would be
# silent and unrecoverable).
_EDL_EXTRA_GLOBS = ("**/rawprogram*.xml", "**/patch*.xml", "**/*devprg*.melf", "**/prog_firehose*.elf")


def essential_files(firmware, version=None):
    """The set of payload files CAS actually reads for this build — everything else is archivable.

    Derived by CALLING the accessors the flash path uses (stock_boot_image, edl_tools) rather than
    re-deriving their globs. That is the load-bearing property: if the slim set were an independent
    copy of those patterns, a later change to an accessor would start silently deleting files CAS had
    begun to need, and the failure would only surface at a flash.

    EDL builds additionally keep the whole Firehose toolchain. That is not an optimisation:
    flash_method() DERIVES 'edl' from those tools being present, so dropping them flips the build to
    'fastboot' and sends a unit whose bootloader cannot write to a doomed bootloader flash."""
    pd = firmware.payload_dir(version)
    if pd is None or not pd.is_dir():
        return set()
    keep = set()
    stock = firmware.stock_boot_image(version)
    if stock:
        keep.add(stock)
    if firmware.flash_method == "edl":
        tools = firmware.edl_tools(version)
        if tools:
            keep.update(t for t in tools if t)
        # Both host variants, not just the one edl_tools() picked for THIS machine: it prefers the
        # host build and falls back to the other so a wrong-OS package still reports cleanly.
        for stem in ("QSaharaServer", "fh_loader"):
            keep.update(pd.glob(f"**/{stem}"))
            keep.update(pd.glob(f"**/{stem}.exe"))
        for pattern in _EDL_EXTRA_GLOBS:
            keep.update(pd.glob(pattern))
    return {p for p in keep if p.is_file()}


def masters_root(root):
    """Where full vendor packages are parked once a build is slimmed: <root's parent>/_firmware_masters.

    Sited BESIDE _firmware/ rather than inside it, so list_firmware() and every rglob in this module
    never walk it. Same volume as the library on purpose — the master move is then a filesystem
    rename: atomic and instant, with no half-copied-multi-GB failure mode. Mirrors
    initboot_store.store_root()'s layout so the two can't drift."""
    return pathlib.Path(root).parent / "_firmware_masters"


def _version_meta_path(firmware, version):
    return firmware.path / "versions" / version / "version.meta.json"


def _ensure_build_metadata(firmware, version, log):
    """True once this build's fingerprint/chip are recorded. Captures them FIRST if missing.

    Order matters and is the whole reason this exists: super_*/system_*.img are the ONLY place that
    data lives, and slim is about to move them away. Capture before the move or lose it permanently —
    and it is exactly the data that breaks auto-match ties (two builds claiming one chip tie to
    'no match')."""
    p = _version_meta_path(firmware, version)
    meta = _read_json(p)
    if str(meta.get("fingerprint") or "").strip() or str(meta.get("board_platform") or "").strip():
        return True
    pd = firmware.payload_dir(version)
    # Reuse detect_build()'s own detectability test rather than re-globbing: a bare boot.img payload
    # (odin2-default, odin3, retroid-pocket-5) can NEVER yield metadata no matter how long it scans.
    if pd is None or not _payload_has_build_images(firmware, version):
        return False
    log("  capturing build metadata before the bulk moves (greps the super images — slow, one time)…")
    try:
        info = detect_build(pd)
    except Exception as e:                                  # a malformed package must refuse, not raise
        log(f"  metadata capture failed: {e}")
        return False
    for k in ("fingerprint", "board_platform", "soc", "android_release", "device", "dev_code",
              "os_version", "storage"):
        if str(info.get(k) or "").strip() and not str(meta.get(k) or "").strip():
            meta[k] = info[k]
    if not (str(meta.get("fingerprint") or "").strip() or str(meta.get("board_platform") or "").strip()):
        return False
    _write_json(p, meta)
    log("  ✓ metadata captured.")
    return True


def _tree_stats(d):
    files = [p for p in pathlib.Path(d).rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def slim(firmware, version=None, dry_run=False, log=print):
    """Reduce a build's payload to only the files CAS flashes, parking the full package in
    masters_root(). Returns a result dict; never raises for an expected refusal.

    Nothing is ever deleted — the payload is MOVED and can be restored with unslim(). The operation
    verifies itself: after rebuilding the payload it re-resolves stock_boot_image / edl_tools /
    init_boot_geometry and rolls the master back if any of them regress, so a slim can never leave a
    build that CAS can no longer flash."""
    version = version or firmware.current()
    res = {"slimmed": False, "moved_files": 0, "moved_bytes": 0, "kept": [], "reason": ""}
    if not version:
        res["reason"] = "no version"
        return res
    vmeta = _read_json(_version_meta_path(firmware, version))
    if vmeta.get("slim"):
        res["reason"] = "already slim"
        return res
    pd = firmware.payload_dir(version)
    if pd is None or not pd.is_dir():
        res["reason"] = "no payload"
        return res

    keep = essential_files(firmware, version)
    if not keep or firmware.stock_boot_image(version) is None:
        res["reason"] = "no stock boot image resolved — refusing to slim an unflashable build"
        log(f"REFUSING: {res['reason']}")
        return res

    rel = sorted(p.relative_to(pd) for p in keep)
    res["kept"] = [str(r) for r in rel]
    n_files, n_bytes = _tree_stats(pd)
    kept_bytes = sum(p.stat().st_size for p in keep)
    res["moved_files"], res["moved_bytes"] = n_files - len(keep), n_bytes - kept_bytes

    # "Nothing to move" is decided BEFORE the metadata gate. A bare boot.img payload (odin2-default,
    # odin3, retroid-pocket-5) is already in the end state and has no super image to derive metadata
    # from — demanding it there would report an alarming refusal for a build that needs no work.
    if res["moved_files"] <= 0:
        res["reason"] = "already minimal — nothing to move"
        return res

    # Dry run reports and stops BEFORE the metadata gate: that gate greps multi-GB super images and
    # writes version.meta.json, and a preview of the whole library must touch nothing.
    if dry_run:
        log(f"  dry-run: would keep {len(keep)} file(s), move {res['moved_files']} "
            f"({res['moved_bytes'] / 2**30:.2f} GB) to {masters_root(firmware.path.parent)}")
        return res

    if not _ensure_build_metadata(firmware, version, log):
        res["reason"] = ("build metadata (fingerprint/chip) is missing and could not be derived from "
                         "this payload — refusing to slim, because the images it would be derived "
                         "from are what slim moves away")
        log(f"REFUSING: {res['reason']}")
        return res
    # RE-READ: the gate above may have just written the captured fingerprint/chip into this same
    # file. The copy loaded at the top of slim() is now stale, and stamping it back would silently
    # erase the very metadata we captured.
    vmeta = _read_json(_version_meta_path(firmware, version))

    dest = masters_root(firmware.path.parent) / firmware.id / version / "payload"
    if dest.exists():
        res["reason"] = f"a master already exists at {dest} — refusing to overwrite it"
        log(f"REFUSING: {res['reason']}")
        return res
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(pd, dest)                       # same volume -> atomic rename, no copy
    try:
        pd.mkdir(parents=True)
        for r in rel:
            (pd / r).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest / r, pd / r)
        fresh = Firmware(firmware.path)        # re-resolve through a clean read of the meta
        if (fresh.stock_boot_image(version) is None
                or (vmeta.get("flash_method") == "edl" and fresh.edl_tools(version) is None)):
            raise RuntimeError("post-slim verification failed: CAS can no longer resolve what it flashes")
    except Exception as e:
        shutil.rmtree(pd, ignore_errors=True)  # roll the master back; leave the build exactly as found
        os.replace(dest, pd)
        res["reason"] = str(e)
        log(f"ERROR: {e} — master restored, build unchanged.")
        return res

    vmeta.update({
        "slim": True,
        "master_at": str(pathlib.Path(dest).relative_to(masters_root(firmware.path.parent).parent)),
        "removed_files": res["moved_files"],
        "removed_bytes": res["moved_bytes"],
        "slimmed_utc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    _write_json(_version_meta_path(firmware, version), vmeta)
    res["slimmed"] = True
    log(f"  ✓ slimmed {firmware.id}@{version}: kept {len(keep)} file(s), "
        f"moved {res['moved_bytes'] / 2**30:.2f} GB to the master store.")
    return res


_slim = slim   # module-level alias: ingest()'s `slim=` parameter shadows the name inside its body


def unslim(firmware, version=None, log=print):
    """Restore a slimmed build's full vendor package from masters_root(). The inverse of slim()."""
    version = version or firmware.current()
    res = {"restored": False, "reason": ""}
    if not version:
        res["reason"] = "no version"
        return res
    src = masters_root(firmware.path.parent) / firmware.id / version / "payload"
    if not src.is_dir():
        res["reason"] = f"no master package at {src}"
        log(f"cannot restore: {res['reason']}")
        return res
    pd = firmware.payload_dir(version)
    shutil.rmtree(pd, ignore_errors=True)      # the slim payload is a strict subset of the master
    os.replace(src, pd)
    vmeta = _read_json(_version_meta_path(firmware, version))
    for k in ("slim", "master_at", "removed_files", "removed_bytes", "slimmed_utc"):
        vmeta.pop(k, None)
    vmeta["slim"] = False
    _write_json(_version_meta_path(firmware, version), vmeta)
    res["restored"] = True
    log(f"  ✓ restored the full package for {firmware.id}@{version}.")
    return res


def ingest(src, root, firmware_id=None, label=None, match=None, copy=True, slim=True, log=print):
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
    fw = Firmware(fw_dir)
    # Keep only what CAS flashes, parking the full package in _firmware_masters/. Default ON so the
    # library never re-accumulates the ~98% of a vendor tree nothing reads; slim=False keeps the whole
    # tree for a build under investigation. Same function the cleanup path uses, so the two can't drift.
    # detect_build() has already run above, so slim's metadata gate is satisfied and costs nothing here.
    if slim:
        _slim(fw, version=version, log=log)         # _slim: the parameter shadows the function name
        fw = Firmware(fw_dir)                       # re-read: slim rewrote version.meta.json
    return fw


# ---------------------------------------------------------------------------
# Task 9: set_gate_fields() — the escape hatch
# ---------------------------------------------------------------------------

def set_gate_fields(firmware_id, root, chip=None, soc=None, android=None, storage=None):
    """Write gate fields on an existing firmware without re-ingesting — the escape hatch for a build
    whose props detect_build() can't grep out of its super image. Only the named fields are touched.
    Raises ValueError on an unknown id. Returns the Firmware."""
    fw = find(firmware_id, root)
    if fw is None:
        raise ValueError(f"no firmware '{firmware_id}' in {root}")
    meta = _read_json(fw.path / "meta.json")
    m = dict(meta.get("match") or {})
    for key, val in (("board_platform", chip), ("soc", soc), ("android_release", android)):
        if val:
            m[key] = str(val)
    meta["match"] = m
    if storage:
        meta["storage"] = str(storage)
    _write_json(fw.path / "meta.json", meta)
    return Firmware(fw.path)


def build_fingerprint(firmware, version=None):
    """The build fingerprint RECORDED for this kit version, or None when unset/blank.

    Provenance gate: a kit is only authoritative as "this unit's factory image" when it carries a
    fingerprint EQUAL to the unit's. Every kit currently records "" (unset), so callers treating
    None as "unproven" leave today's behaviour untouched — which is the point: the RP6/Thor kits are
    a DIFFERENT build than their units (eng.RP6.20260119 vs kit RP6_20260115), so preferring a kit
    there would flash a wrong-build init_boot and break the very OTA this is meant to protect."""
    if firmware is None:
        return None
    v = version or firmware.current()          # current() is a METHOD, not a property
    if not v:
        return None
    fp = str(_read_json(_version_meta_path(firmware, v)).get("fingerprint") or "").strip()
    return fp or None


def set_build_fingerprint(firmware_id, root, version, fingerprint):
    """Record the build fingerprint for one kit version. Only that key is touched, so gate fields and
    ingest metadata already in version.meta.json survive. Raises ValueError on an unknown id.

    Deliberate, never inferred: a version string resembling a build id ('20260507-165105' vs
    'AIR_X_user_20260507') is suggestive and is NOT evidence — hash agreement with the OTA's expected
    source is."""
    fw = find(firmware_id, root)
    if fw is None:
        raise ValueError(f"no firmware '{firmware_id}' in {root}")
    p = _version_meta_path(fw, version)
    meta = _read_json(p)
    meta["fingerprint"] = str(fingerprint or "").strip()
    _write_json(p, meta)
    return meta["fingerprint"]


# ---------------------------------------------------------------------------
# Task 10: backfill() — migration without a flag day
# ---------------------------------------------------------------------------

# The exact skip reason for an entry backfill can NEVER fix. It carries the next command, because the
# operator's alternative is a 91-minute scan that ends in "0 firmware backfilled" and no explanation.
NO_BUILD_IMAGES = ("no super/system image in payload — backfill can never detect this; "
                   "use 'set --chip'")


def _payload_has_build_images(firmware, version=None):
    """True when the payload holds a super_*/system_*.img for detect_build() to grep. Mirrors
    detect_build()'s own base-directory logic (payload/<emmc|ufs>/ if present, else payload/) so the
    two can never disagree about whether an entry is detectable.

    False means backfill can NEVER fill this entry's chip no matter how long it scans — its payload is
    a bare init_boot.img/boot.img (odin2-default, odin3, retroid-pocket-5 are all this shape). That is
    a different situation from 'scanned and found nothing new', and the operator needs to know which."""
    pd = firmware.payload_dir(version)
    if not pd or not pd.is_dir():
        return False
    storage = "emmc" if (pd / "emmc").is_dir() else ("ufs" if (pd / "ufs").is_dir() else "")
    base = (pd / storage) if storage else pd
    return bool(list(base.glob("super_*.img")) or list(base.glob("system_*.img")))


def _payload_scan_size_bytes(firmware, version=None):
    """Total size (bytes) of the super_*/system_*.img files detect_build() is about to read for this
    entry, or None if it can't be sized. Mirrors _payload_has_build_images()'s base-dir/glob logic (same
    files, so the two can never disagree on which images are in play) — duplicated rather than shared,
    since _payload_has_build_images() only needs to know whether any exist, not their size.

    For a 91-minute scan the size is the operator's ONLY ETA signal. Raises OSError upward on a stat()
    failure (e.g. a file that vanishes mid-scan) — the caller is responsible for treating that as
    best-effort and falling back to the size-less wording; sizing must never abort an entry."""
    pd = firmware.payload_dir(version)
    if not pd or not pd.is_dir():
        return None
    storage = "emmc" if (pd / "emmc").is_dir() else ("ufs" if (pd / "ufs").is_dir() else "")
    base = (pd / storage) if storage else pd
    imgs = list(base.glob("super_*.img")) + list(base.glob("system_*.img"))
    if not imgs:
        return None
    return sum(f.stat().st_size for f in imgs)


def backfill(root, log=print):
    """Re-run detect_build() over every firmware's CURRENT version payload and fill the gate fields it
    is MISSING. The payload is a verbatim copy of the build tree, so detect_build() works on it as-is.

    Returns (filled, skipped): filled = [(id, {field: value})] for entries actually changed;
    skipped = [(id, reason)] for every entry that was not. NOTHING IS SKIPPED SILENTLY — a measured run
    on a real library took 91 MINUTES, printed nothing, and quietly passed over the three entries it
    could never help, which is indistinguishable from a hang followed by a shrug. Progress is emitted
    via `log` BEFORE each entry is scanned, for the same reason — and the pre-scan line names the
    payload size (best-effort) since that is the operator's only ETA signal during the wait.

    Never overwrites an existing value — an operator's `set` wins over detection. Best-effort per
    entry: an unreadable or undetectable payload is skipped, never raised.

    CORRUPT-META GUARD: list_firmware() only returns dirs that CONTAIN a meta.json — so if fw.meta is
    empty/falsy, the file exists but did NOT parse (_read_json() swallows the error and returns {}).
    That is a corrupt entry, never a legitimate backfill target: treating {} as "every gate field is
    missing" would call set_gate_fields(), which re-reads the same unparseable file, also gets {}, and
    writes back a meta.json containing almost nothing — silently dropping device/storage/flash_target/
    current/history/label/id. Skip it instead, before touching anything."""
    filled_out, skipped = [], []
    fws = list_firmware(root)
    total = len(fws)
    for i, fw in enumerate(fws, 1):
        head = f"[{i}/{total}] {fw.id}"

        def skip(reason):
            skipped.append((fw.id, reason))
            log(f"{head}: skipped — {reason}")

        if not fw.meta:
            skip("meta.json did not parse — left untouched")
            continue
        if not _payload_has_build_images(fw):
            skip(NO_BUILD_IMAGES)
            continue
        try:
            size_bytes = _payload_scan_size_bytes(fw)
        except OSError:
            size_bytes = None
        if size_bytes is not None:
            log(f"{head}: scanning {size_bytes / 1e9:.1f} GB…")
        else:
            log(f"{head}: scanning payload…")
        try:
            info = detect_build(fw.payload_dir())
        except Exception as e:
            skip(f"payload unreadable ({e})")
            continue
        r = fw.match_rules()
        filled = {}
        for key in ("board_platform", "soc", "android_release"):
            if info.get(key) and not r.get(key):
                filled[key] = info[key]
        if not filled:
            skip("nothing new detected")
            continue
        set_gate_fields(fw.id, root, chip=filled.get("board_platform"),
                        soc=filled.get("soc"), android=filled.get("android_release"))
        filled_out.append((fw.id, filled))
        log(f"{head}: filled {filled}")
    return (filled_out, skipped)


# ---------------------------------------------------------------------------
# Task 7 (adapted): resolve() — uses fw-local get/set_device_firmware
# ---------------------------------------------------------------------------

def _no_match_reasons(identity_dict, root):
    """Why did nothing match? Distinguishes situations, because they imply DIFFERENT operator actions:
    'the library has no build for this silicon' (ingest one) vs 'a build for this chip exists but was
    rejected on android/storage' (the mismatch is elsewhere — ingesting another chip build won't help)
    vs 'entries exist but record no chip' (run backfill). A bare 'no match' leaves the operator with no
    next step — and this spec deletes a warning for being uninformative, so it must not add one.

    USES THE PER-AXIS REASON (I2 fix): gate_check() already computes a precise reason per rejected
    firmware ("chip X != firmware Y" / "android ..." / "storage ..."); this used to discard it
    (`ok, _reason, agreed = ...`) and lump every rejection into one 'no firmware matches this chip'
    bucket — impossible advice when the rejection was actually on android or storage and the chip build
    is sitting right there in the library. Rejections are now split by axis: only a CHIP-axis rejection
    ("chip ..." reason) is reported as a missing chip build; android/storage rejections name their own
    conflict instead, so the operator isn't told to ingest a build for a chip they already have.

    'records no chip' checks BOTH board_platform and soc (mirrors gate_check(), which treats both as
    chip axes) — an entry with a `soc` rule recorded is not legacy just because board_platform is
    unset, even if THIS device's identity happened not to report soc (abstain, not "no chip on file").

    NEVER RECOMMEND BACKFILL FOR AN ENTRY IT CANNOT FIX: a chip-less entry whose payload has no
    super/system image (a bare init_boot.img — odin2-default, odin3, retroid-pocket-5) can never be
    filled by scanning, no matter how long. Sending the operator to backfill for those is a measured
    91-MINUTE round trip ending in "0 firmware backfilled" and the same (no match). Those are reported
    with the command that actually works: `set --chip`."""
    chip_rejected = 0
    axis_rejections = []
    legacy_scannable = 0        # chip-less, payload HAS a super image -> backfill can fill it
    legacy_unscannable = 0      # chip-less, no super image -> backfill can NEVER fill it
    for fw in list_firmware(root):
        ok, reason, agreed = gate_check(fw, identity_dict)
        if not ok:
            if reason and reason.startswith("chip "):
                chip_rejected += 1
            else:
                axis_rejections.append(reason)
        elif agreed == 0 and not (fw.match_rules().get("board_platform")
                                  or fw.match_rules().get("soc")):
            if _payload_has_build_images(fw):
                legacy_scannable += 1
            else:
                legacy_unscannable += 1
    out = []
    chip = identity_dict.get("board_platform") or identity_dict.get("soc") or "unknown"
    if chip_rejected:
        out.append(f"no firmware matches this chip ({chip}) — {chip_rejected} rejected by the gate; "
                   f"ingest a build for it")
    for reason in axis_rejections:
        out.append(f"a build for this chip exists but was rejected on {reason} — ingest won't help; "
                   f"the mismatch is elsewhere")
    if legacy_scannable:
        out.append(f"{legacy_scannable} firmware(s) record no chip — "
                   f"run 'python3 -m cas.firmware backfill'")
    if legacy_unscannable:
        out.append(f"{legacy_unscannable} firmware(s) record no chip and have no super image to scan "
                   f"— use 'python3 -m cas.firmware set --chip <name> <id>'")
    if not out:
        out.append("no match — select manually")
    return out


def resolve(serial, identity_dict, root):
    """Decide the firmware for a connected device. Manual override (sticky) wins; else match() and
    remember it (manual=False). Version = pinned rollback or the firmware's current. Always runs
    logic_check. Returns a dict the UI/CLI render directly.

    RE-GATE ON READ (I1): a cached assignment with manual=False is an AUTO-SUGGESTION cached at some
    earlier resolve() — possibly by code that predates gate_check() as a hard gate (the very
    "stale serial_prefix outvotes chip" class of bug this module exists to fix). Trusting it blindly
    would mean a stale cache entry in the operator's cas-config.json survives the fix forever, handing
    back a confident ok=True on a firmware that gate_check() would now reject outright. So a non-manual
    cached assignment is re-run through gate_check() here; if it now fails, it is discarded and
    resolve() falls through to match() as if the device had never been assigned — re-matching against
    the CURRENT gate, not trusting old history. An explicit operator override (manual=True) is left
    alone: it is spec'd, intentional behavior, and must keep working even against a firmware
    gate_check() would reject (logic_check below still surfaces warnings on it, same as today)."""
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
        if fw is not None and not manual:
            gate_ok, _gate_reason, _agreed = gate_check(fw, identity_dict)
            if not gate_ok:
                # Stale auto-suggestion that no longer passes the gate — discard and fall through to
                # match() below as if unassigned. manual=False already, so a fresh suggestion writes
                # back through the same manual=False path a few lines down.
                fw, pinned = None, None
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
            "warnings": _no_match_reasons(identity_dict, root),
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


def log_proven_pair(identity_dict, firmware_id, version, when=None):
    """Record a (chip, android, storage, model, firmware_id, version) tuple that ACTUALLY BOOTED.

    EVIDENCE, NOT A GATE — nothing reads this to allow or block a flash. It exists so a proven
    cross-model pair (an RP6 rooted from the Odin 2 build) stops being knowledge in one person's head
    and becomes data in the library. Best-effort; never raises."""
    try:
        if when is None:
            import datetime
            when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        idn = identity_dict or {}
        rec = {
            "when": when,
            "serial": idn.get("serial"),
            "model": idn.get("model"),
            "chip": idn.get("board_platform") or idn.get("soc"),
            "android": _android_major(idn.get("android_release")),
            "storage": _storage_from_bootdevice(idn.get("bootdevice")),
            "firmware_id": firmware_id,
            "version": version,
        }
        p = pathlib.Path(config.history_dir()) / config.history_filename("firmware-proven")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Task 8 (CLI adaptation): main() — `python3 -m cas.firmware`
# ---------------------------------------------------------------------------

def main(argv=None):
    """Entry point for `python3 -m cas.firmware`. Six subcommands: list, ingest, show, assign, set,
    backfill."""
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

    st = sub.add_parser("set", help="write gate fields (chip/android/storage) on a firmware")
    st.add_argument("id")
    st.add_argument("--chip", help="ro.board.platform, e.g. kalama")
    st.add_argument("--soc", help="ro.soc.model, e.g. SM8550")
    st.add_argument("--android", help="major Android release, e.g. 13")
    st.add_argument("--storage", choices=["emmc", "ufs"])

    sub.add_parser("backfill", help="fill gate fields on existing firmware from their payloads")

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

    elif args.cmd == "set":
        fw = set_gate_fields(args.id, root, chip=args.chip, soc=args.soc,
                             android=args.android, storage=args.storage)
        print(f"{fw.id}: match={fw.match_rules()} storage={fw.storage}")

    elif args.cmd == "backfill":
        filled, skipped = backfill(root)
        for fid, fields in filled:
            print(f"{fid}: filled {fields}")
        for fid, reason in skipped:
            print(f"{fid}: skipped — {reason}")
        print(f"{len(filled)} firmware backfilled, {len(skipped)} skipped")

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
