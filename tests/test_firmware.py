# tests/test_firmware.py
import os
import sys
import json
import pathlib
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas.adb import Adb
from cas import config as C
from cas import firmware as FW


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

class IdRunner:
    """getprop runner returning a fixed prop table; everything else returns empty ok."""
    def __init__(self, props):
        self.props = props

    def __call__(self, args, input_text=None, timeout=900):
        if "shell" in args and args[-1].startswith("getprop"):
            return 0, (self.props.get(args[-1].split()[-1], "") + "\n"), ""
        return 0, "", ""


AIRX_PROPS = {
    "ro.serialno": "MQ66142509130541", "ro.product.device": "AIR_X",
    "ro.product.model": "AIR X", "ro.product.manufacturer": "MANGMI",
    "ro.soc.model": "SM6115", "ro.mangmi.dev.code": "MQ66",
    "ro.product.first_api_level": "33", "ro.boot.slot_suffix": "_b",
}


def make_fw(root, fid, device="AIR_X", flash="init_boot", storage="emmc",
            match=None, current="20260507-165105"):
    d = pathlib.Path(root) / fid
    (d / "versions" / current / "payload").mkdir(parents=True)
    FW._write_json(d / "meta.json", {
        "id": fid, "label": fid, "device": device, "flash_target": flash,
        "storage": storage, "match": match or {}, "current": current, "history": []})
    return FW.Firmware(d)


def fake_build(tmp, name, storage="emmc", with_init_boot=True, device="AIR_X",
               dev_code="MQ66", os_version="1.1.6"):
    """A minimal device-firmware tree: <name>/<storage>/{rawprogram1.xml, super_1.img}."""
    d = pathlib.Path(tmp) / name
    p = d / storage
    p.mkdir(parents=True)
    parts = '<program label="boot_a" /><program label="init_boot_a" />' if with_init_boot \
        else '<program label="boot_a" /><program label="boot_b" />'
    (p / "rawprogram1.xml").write_text(f"<data>{parts}</data>")
    (p / "super_1.img").write_text(
        f"ro.product.system.device={device}\nro.mangmi.dev.code={dev_code}\n"
        f"ro.mangmi.os.version={os_version}\n")
    return d


# ---------------------------------------------------------------------------
# Task 1 (adapted): identity() free function in firmware.py
# ---------------------------------------------------------------------------

class TestIdentity(unittest.TestCase):
    def test_identity_airx(self):
        idn = FW.identity(Adb(runner=IdRunner(AIRX_PROPS)))
        self.assertEqual(idn["serial"], "MQ66142509130541")
        self.assertEqual(idn["device"], "AIR_X")
        self.assertEqual(idn["soc"], "SM6115")
        self.assertEqual(idn["flash_target"], "init_boot_b")


# ---------------------------------------------------------------------------
# Task 2 (adapted): firmware_root / get_device_firmware / set_device_firmware
#                   all live in firmware.py (not config.py)
# ---------------------------------------------------------------------------

class TestDeviceFirmware(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        os.environ["CAS_PROFILES"] = self.tmp   # pins library_root() to tmp

    def tearDown(self):
        os.environ.pop("CAS_CONFIG", None)
        os.environ.pop("CAS_PROFILES", None)

    def test_firmware_root_under_library(self):
        self.assertEqual(FW.firmware_root(), pathlib.Path(self.tmp) / "_firmware")

    def test_set_get_roundtrip_and_forget(self):
        FW.set_device_firmware("MQ66x", "mangmi-air-x-mq66", manual=True)
        got = FW.get_device_firmware()["MQ66x"]
        self.assertEqual(got["firmware_id"], "mangmi-air-x-mq66")
        self.assertTrue(got["manual"])
        self.assertIsNone(got["version"])
        FW.set_device_firmware("MQ66x", None)   # forget
        self.assertNotIn("MQ66x", FW.get_device_firmware())

    def test_pinned_version_persists(self):
        FW.set_device_firmware("S", "fw", version="20260507-165105", manual=True)
        self.assertEqual(FW.get_device_firmware()["S"]["version"], "20260507-165105")


# ---------------------------------------------------------------------------
# Task 3: Firmware class + list_firmware + find
# ---------------------------------------------------------------------------

class TestFirmwareClass(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        self.root.mkdir(parents=True)

    def test_list_and_find_and_props(self):
        make_fw(self.root, "mangmi-air-x-mq66", match={"serial_prefix": ["MQ66"]})
        fws = FW.list_firmware(self.root)
        self.assertEqual([f.id for f in fws], ["mangmi-air-x-mq66"])
        f = FW.find("mangmi-air-x-mq66", self.root)
        self.assertEqual(f.flash_target, "init_boot")
        self.assertEqual(f.current(), "20260507-165105")
        self.assertEqual(f.versions(), ["20260507-165105"])
        self.assertTrue(f.payload_dir().is_dir())

    def test_find_missing(self):
        self.assertIsNone(FW.find("nope", self.root))

    def test_nondirectory_entry_not_listed_as_firmware(self):
        # index.json is skipped because it is a file (not a dir), not because of its name
        (self.root / "index.json").write_text("{}")
        make_fw(self.root, "ayn-m0", device="AYN", flash="boot", storage="ufs")
        self.assertEqual([f.id for f in FW.list_firmware(self.root)], ["ayn-m0"])


# ---------------------------------------------------------------------------
# Task 4: match() — suggestion by identity
# ---------------------------------------------------------------------------

class TestMatch(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        self.root.mkdir(parents=True)
        make_fw(self.root, "mangmi-air-x-mq66",
                match={"serial_prefix": ["MQ66"], "device": "AIR_X", "soc": "SM6115"})
        make_fw(self.root, "mangmi-air-x-mq65",
                match={"serial_prefix": ["MQ65"], "device": "AIR_X", "soc": "SM6115"})
        make_fw(self.root, "mangmi-pocket-max", device="Pocket_Max", flash="boot", storage="ufs",
                match={"device": "Pocket_Max"})

    def test_serial_prefix_splits_airx(self):
        m = FW.match({"serial": "MQ66999", "device": "AIR_X", "soc": "SM6115", "brand": "MANGMI"},
                     self.root)
        self.assertEqual(m[0].id, "mangmi-air-x-mq66")
        m = FW.match({"serial": "MQ65111", "device": "AIR_X", "soc": "SM6115", "brand": "MANGMI"},
                     self.root)
        self.assertEqual(m[0].id, "mangmi-air-x-mq65")

    def test_pocket_max_by_device(self):
        m = FW.match({"serial": "PKX1", "device": "Pocket_Max", "brand": "MANGMI"}, self.root)
        self.assertEqual(m[0].id, "mangmi-pocket-max")

    def test_returns_current_version(self):
        m = FW.match({"serial": "MQ66999", "device": "AIR_X", "soc": "SM6115"}, self.root)
        self.assertEqual(m[1], "20260507-165105")

    def test_no_match_returns_none(self):
        self.assertIsNone(FW.match({"serial": "ZZ", "device": "OTHER"}, self.root))


# ---------------------------------------------------------------------------
# Task 5: logic_check() — brick-guard
# ---------------------------------------------------------------------------

class TestLogicCheck(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        self.root.mkdir(parents=True)
        self.fw = make_fw(self.root, "mangmi-air-x-mq66", device="AIR_X", flash="init_boot",
                          match={"serial_prefix": ["MQ66"], "device": "AIR_X"})

    def test_ok_when_consistent(self):
        ok, warns = FW.logic_check(self.fw, {"serial": "MQ66x", "device": "AIR_X",
                                             "flash_target": "init_boot_b"})
        self.assertTrue(ok)
        self.assertEqual(warns, [])

    def test_warns_on_partition_mismatch(self):
        ok, warns = FW.logic_check(self.fw, {"serial": "MQ66x", "device": "AIR_X",
                                             "flash_target": "boot_a"})
        self.assertFalse(ok)
        self.assertTrue(any("init_boot" in w and "boot" in w for w in warns))

    def test_warns_on_serial_and_device_mismatch(self):
        ok, warns = FW.logic_check(self.fw, {"serial": "MQ65x", "device": "Pocket_Max",
                                             "flash_target": "init_boot_b"})
        self.assertFalse(ok)
        self.assertEqual(len(warns), 2)


# ---------------------------------------------------------------------------
# Task 6: detect_build + ingest
# ---------------------------------------------------------------------------

class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def test_detect_build(self):
        src = fake_build(
            self.tmp,
            "MANGMI_Vex6115_FlatBuild_TurboX-C6115_xx.xx_la2.0.l.user.20260507.165105")
        d = FW.detect_build(src)
        self.assertEqual(d["storage"], "emmc")
        self.assertEqual(d["flash_target"], "init_boot")
        self.assertEqual(d["version"], "20260507-165105")
        self.assertEqual(d["device"], "AIR_X")
        self.assertEqual(d["dev_code"], "MQ66")
        self.assertEqual(d["os_version"], "1.1.6")

    def test_ingest_creates_version_and_history(self):
        src = fake_build(
            self.tmp,
            "MANGMI_Vex6115_FlatBuild_TurboX-C6115_xx.xx_la2.0.l.user.20260507.165105")
        fw = FW.ingest(src, self.root, firmware_id="mangmi-air-x-mq66",
                       match={"serial_prefix": ["MQ66"], "device": "AIR_X"})
        self.assertEqual(fw.current(), "20260507-165105")
        self.assertTrue((fw.payload_dir() / "emmc" / "super_1.img").is_file())
        self.assertEqual(len(fw.meta["history"]), 1)
        self.assertEqual(fw.flash_target, "init_boot")

    def test_ingest_idempotent_same_version(self):
        src = fake_build(self.tmp, "MANGMI_x_la2.0.l.user.20260507.165105")
        FW.ingest(src, self.root, firmware_id="fw")
        fw = FW.ingest(src, self.root, firmware_id="fw")   # no-op re-ingest
        self.assertEqual(len(fw.meta["history"]), 1)

    def test_ingest_second_version_bumps_current_keeps_old(self):
        FW.ingest(fake_build(self.tmp, "a_la2.0.l.user.20260506.000000"),
                  self.root, firmware_id="fw")
        fw = FW.ingest(fake_build(self.tmp, "b_la2.0.l.user.20260507.000000"),
                       self.root, firmware_id="fw")
        self.assertEqual(fw.current(), "20260507-000000")
        self.assertEqual(sorted(fw.versions()), ["20260506-000000", "20260507-000000"])
        self.assertEqual(len(fw.meta["history"]), 2)

    def test_ingest_wrong_device_guard(self):
        FW.ingest(fake_build(self.tmp, "a_la2.0.l.user.20260507.000000", device="AIR_X"),
                  self.root, firmware_id="fw")
        with self.assertRaises(ValueError):
            FW.ingest(fake_build(self.tmp, "b_la2.0.l.user.20260508.000000", device="Pocket_Max"),
                      self.root, firmware_id="fw")

    def test_ingest_no_label_raises_value_error(self):
        """Brick-guard: no boot/init_boot rawprogram label → ValueError."""
        d = pathlib.Path(self.tmp) / "no_label_build"
        p = d / "emmc"
        p.mkdir(parents=True)
        # rawprogram XML exists but contains only unrelated labels (no boot or init_boot)
        (p / "rawprogram1.xml").write_text('<data><program label="persist" /></data>')
        with self.assertRaises(ValueError):
            FW.ingest(d, self.root, firmware_id="fw-no-label")

    def test_ingest_seeds_device_match_from_detection(self):
        """A GUI ingest passes no match → ingest must seed match.device from the detected device so the
        firmware auto-matches instead of staying '(no match)'."""
        src = fake_build(self.tmp, "x_la2.0.l.user.20260507.000000", device="AIR_X")
        fw = FW.ingest(src, self.root, firmware_id="fw")
        self.assertEqual(fw.match_rules().get("device"), "AIR_X")

    def test_ingest_merges_caller_serial_prefix_with_detected_device(self):
        """Caller's serial_prefix (the MQ65/MQ66 discriminator) is kept AND device filled from detection."""
        src = fake_build(self.tmp, "y_la2.0.l.user.20260507.000000", device="AIR_X")
        fw = FW.ingest(src, self.root, firmware_id="fw2", match={"serial_prefix": ["MQ66"]})
        self.assertEqual(fw.match_rules().get("serial_prefix"), ["MQ66"])
        self.assertEqual(fw.match_rules().get("device"), "AIR_X")


# ---------------------------------------------------------------------------
# Task 7 (adapted): resolve() — uses fw-local get/set_device_firmware
# ---------------------------------------------------------------------------

class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)
        make_fw(self.root, "mangmi-air-x-mq66",
                match={"serial_prefix": ["MQ66"], "device": "AIR_X"})
        make_fw(self.root, "mangmi-air-x-mq65",
                match={"serial_prefix": ["MQ65"], "device": "AIR_X"})

    def tearDown(self):
        os.environ.pop("CAS_CONFIG", None)

    def _idn(self, serial):
        return {"serial": serial, "device": "AIR_X", "soc": "SM6115",
                "flash_target": "init_boot_b"}

    def test_auto_suggests_and_remembers(self):
        r = FW.resolve("MQ66x", self._idn("MQ66x"), self.root)
        self.assertEqual(r["firmware_id"], "mangmi-air-x-mq66")
        self.assertEqual(r["suggested"], "mangmi-air-x-mq66")
        self.assertFalse(r["manual"])
        self.assertTrue(r["ok"])
        self.assertEqual(FW.get_device_firmware()["MQ66x"]["firmware_id"], "mangmi-air-x-mq66")

    def test_manual_override_wins(self):
        FW.set_device_firmware("MQ66x", "mangmi-air-x-mq65", manual=True)
        r = FW.resolve("MQ66x", self._idn("MQ66x"), self.root)
        self.assertEqual(r["firmware_id"], "mangmi-air-x-mq65")
        self.assertTrue(r["manual"])
        self.assertFalse(r["ok"])  # logic_check warns: MQ66 serial vs MQ65 firmware

    def test_default_kit_sentinel_pins_to_bundled_init_boot(self):
        # Assigning the "(default kit)" sentinel pins the unit to the bundled DEFAULT init_boot: no Firmware
        # object (so root falls back to the kit image), no auto-match, no warning, sticky.
        FW.set_device_firmware("ZZ", FW.DEFAULT_FW_ID, manual=True)
        r = FW.resolve("ZZ", {"serial": "ZZ", "device": "OTHER"}, self.root)
        self.assertEqual(r["firmware_id"], FW.DEFAULT_FW_ID)
        self.assertIsNone(r["firmware"])     # no build -> root_all keeps the DEFAULT kit init_boot
        self.assertTrue(r["ok"])             # NOT "(no match)" / not an error
        self.assertTrue(r["manual"])
        self.assertFalse(r["warnings"])
        # and it must NOT get auto-reassigned away
        self.assertEqual(FW.get_device_firmware()["ZZ"]["firmware_id"], FW.DEFAULT_FW_ID)

    def test_pinned_version_used(self):
        FW.set_device_firmware("S", "mangmi-air-x-mq66", version="20260101-000000", manual=True)
        r = FW.resolve("S", self._idn("S"), self.root)
        self.assertEqual(r["version"], "20260101-000000")

    def test_no_match(self):
        r = FW.resolve("ZZ", {"serial": "ZZ", "device": "OTHER"}, self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertFalse(r["ok"])


# ---------------------------------------------------------------------------
# Task 7b: log_event() — assignment/update audit jsonl
# ---------------------------------------------------------------------------

class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        os.environ["CAS_PROFILES"] = self.tmp

    def tearDown(self):
        os.environ.pop("CAS_CONFIG", None)
        os.environ.pop("CAS_PROFILES", None)

    def test_log_event_appends_jsonl(self):
        FW.log_event("S1", "fw", "v1", "assign", True, when="2026-06-27 12:00")
        FW.log_event("S2", "fw2", "v2", "update", False, when="2026-06-27 12:01")
        p = pathlib.Path(C.history_dir()) / C.history_filename("firmware-history")
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["serial"], "S1")
        self.assertEqual(lines[1]["action"], "update")


def fake_edl_build(tmp, name):
    """A Firehose/EDL device-firmware build: bundled QSaharaServer/fh_loader + emmc/{prog_firehose,
    rawprogram with init_boot_a/_b geometry, init_boot.img}."""
    d = pathlib.Path(tmp) / name
    d.mkdir(parents=True)
    (d / "QSaharaServer").write_text("#!/bin/sh\n")
    (d / "fh_loader").write_text("#!/bin/sh\n")
    p = d / "emmc"
    p.mkdir(parents=True)
    (p / "prog_firehose_ddr.elf").write_bytes(b"\x7fELF")
    (p / "init_boot.img").write_bytes(b"ANDROID!" + b"\0" * 64)
    (p / "rawprogram1.xml").write_text(
        '<data>'
        '<program SECTOR_SIZE_IN_BYTES="512" filename="init_boot.img" label="init_boot_a" '
        'num_partition_sectors="16384" physical_partition_number="0" '
        'start_byte_hex="0x1f5802000" start_sector="16433168" />'
        '<program SECTOR_SIZE_IN_BYTES="512" filename="" label="init_boot_b" '
        'num_partition_sectors="16384" physical_partition_number="0" '
        'start_byte_hex="0x1f6002000" start_sector="16449552" />'
        '</data>')
    (p / "super_1.img").write_text("ro.product.system.device=AIR_X\nro.mangmi.dev.code=MQ66\n")
    return d


class TestFlashMethod(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def test_ingest_detects_edl_and_exposes_tools_and_geometry(self):
        src = fake_edl_build(self.tmp, "MANGMI_x_la2.0.l.user.20260507.165105")
        fw = FW.ingest(src, self.root, firmware_id="air-x")
        self.assertEqual(fw.flash_method, "edl")
        tools = fw.edl_tools()
        self.assertIsNotNone(tools)
        self.assertTrue(str(tools[0]).endswith("QSaharaServer"))
        g = fw.init_boot_geometry("_b")
        self.assertEqual(g["start_sector"], "16449552")
        self.assertEqual(g["partition"], "0")
        self.assertEqual(g["sector_size"], "512")

    def test_ingest_non_firehose_is_fastboot(self):
        src = fake_build(self.tmp, "Retroid_la2.0.l.user.20260507.000000")   # no QSahara/fh_loader/firehose
        fw = FW.ingest(src, self.root, firmware_id="rp")
        self.assertEqual(fw.flash_method, "fastboot")

    def test_flasher_for_firmware_picks_edl_vs_fastboot(self):
        from cas import provision as PV
        edl_fw = FW.ingest(fake_edl_build(self.tmp, "MANGMI_la2.0.l.user.20260507.165105"),
                           self.root, firmware_id="air-x")
        flasher, reason = PV.flasher_for_firmware(edl_fw, fastboot=None, slot="_b",
                                                  runner=lambda *a, **k: (0, "", ""))
        self.assertIsNotNone(flasher)
        self.assertIsNone(reason)
        fb_fw = FW.ingest(fake_build(self.tmp, "Retroid_la2.0.l.user.20260507.000000"),
                          self.root, firmware_id="rp")
        flasher2, reason2 = PV.flasher_for_firmware(fb_fw, fastboot="FBOBJ", slot="_a")
        self.assertIsNotNone(flasher2)
        self.assertIsNone(reason2)

    def test_edl_tools_prefers_windows_exe(self):
        """On Windows, edl_tools() returns the .exe host tools when present (the Linux ELF can't run there
        — subprocess raises WinError 193). On POSIX it stays with the extensionless ELF."""
        src = fake_edl_build(self.tmp, "MANGMI_win_la2.0.l.user.20260507.165105")
        (src / "QSaharaServer.exe").write_bytes(b"MZ")     # a real Windows PE marker
        (src / "fh_loader.exe").write_bytes(b"MZ")
        fw = FW.ingest(src, self.root, firmware_id="air-x")
        with mock.patch.object(FW.os, "name", "nt"):
            q, f, _ = fw.edl_tools()
            self.assertTrue(str(q).endswith("QSaharaServer.exe"))
            self.assertTrue(str(f).endswith("fh_loader.exe"))
        with mock.patch.object(FW.os, "name", "posix"):
            q, f, _ = fw.edl_tools()
            self.assertTrue(str(q).endswith("QSaharaServer"))
            self.assertFalse(str(q).endswith(".exe"))

    def test_flasher_windows_rejects_linux_only_edl_tools(self):
        """A Windows bench with only the Linux QSaharaServer/fh_loader must fail fast with a clear,
        honest reason (name the missing .exe / QPST) — NOT blame the QDLoader driver and NOT strand the
        unit in EDL. This is the MQ66 'WinError 193 %1 is not a valid Win32 application' case."""
        from cas import provision as PV
        edl_fw = FW.ingest(fake_edl_build(self.tmp, "MANGMI_linux_la2.0.l.user.20260507.165105"),
                           self.root, firmware_id="air-x")
        with mock.patch.object(FW.os, "name", "nt"):
            flasher, reason = PV.flasher_for_firmware(edl_fw, fastboot=None, slot="_b",
                                                      runner=lambda *a, **k: (0, "", ""))
        self.assertIsNone(flasher)
        self.assertIsNotNone(reason)
        self.assertIn(".exe", reason)


if __name__ == "__main__":
    unittest.main()
