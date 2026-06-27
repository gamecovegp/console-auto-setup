# tests/test_firmware.py
import os
import sys
import json
import pathlib
import tempfile
import unittest

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

    def test_index_json_not_listed_as_firmware(self):
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
        p = pathlib.Path(C.history_dir()) / "firmware-history.jsonl"
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["serial"], "S1")
        self.assertEqual(lines[1]["action"], "update")


if __name__ == "__main__":
    unittest.main()
