# tests/test_firmware_slim.py
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import firmware as FW

FP = "qti/kalama/kalama:13/TKQ1.231222.001/eng.RP6.20260119.170007:user/release-keys"

# The bulk a vendor package carries and CAS never flashes. Named exactly as they appear on the
# live library so the fixture exercises the real glob shapes.
BULK = ["super_1.img", "super_3.img", "system_1.img", "userdata_1.img", "vm-bootsys.img",
        "NON-HLOS.bin", "abl.elf", "md5sum.txt"]


def build_firmware(root, fid="rp6", *, flash_target="init_boot", flash_method="fastboot",
                   sub="", version="v1", meta_extra=None, bulk=True, edl=False, meta_ok=True):
    """A firmware library entry whose payload mirrors a real vendor package."""
    root = pathlib.Path(root)
    fw_dir = root / fid
    pay = fw_dir / "versions" / version / "payload" / sub if sub else fw_dir / "versions" / version / "payload"
    pay.mkdir(parents=True)

    (pay / f"{flash_target}.img").write_bytes(b"ANDROID!" + b"\x00" * 64)
    if flash_target == "init_boot":                       # real packages ship both; only one is the target
        (pay / "boot.img").write_bytes(b"ANDROID!" + b"\x01" * 64)
    if bulk:
        for b in BULK:
            body = b"\xAA" * 2048
            if b.startswith("super_"):
                # Real props, so detect_build() genuinely succeeds here. Without this the metadata
                # gate fails for lack of data and every test of it passes for the wrong reason.
                body += (f"ro.build.fingerprint={FP}\n"
                         "ro.board.platform=kalama\n"
                         "ro.product.system.device=kona\n"
                         "ro.build.version.release=13\n").encode()
            (pay / b).write_bytes(body)
    if edl:
        for t in ("QSaharaServer", "QSaharaServer.exe", "fh_loader", "fh_loader.exe"):
            (pay / t).write_bytes(b"MZ")
        (pay / "prog_firehose_ddr.elf").write_bytes(b"\x7fELF")
        (pay / "prog_firehose_lite.elf").write_bytes(b"\x7fELF")
        (pay / "rawprogram1.xml").write_text("<data/>")
        (pay / "rawprogram_unsparse0.xml").write_text("<data/>")
        (pay / "patch1.xml").write_text("<patches/>")

    meta = {"id": fid, "device": "kona", "storage": "ufs", "flash_target": flash_target,
            "flash_method": flash_method, "current": version, "match": {"board_platform": "kalama"}}
    meta.update(meta_extra or {})
    (fw_dir / "meta.json").write_text(json.dumps(meta))
    vmeta = {"flash_target": flash_target, "flash_method": flash_method}
    if meta_ok:                       # a build whose metadata was captured at ingest
        vmeta.update({"fingerprint": FP, "board_platform": "kalama"})
    (fw_dir / "versions" / version / "version.meta.json").write_text(json.dumps(vmeta))
    return FW.Firmware(fw_dir)


def names(paths):
    return sorted(p.name for p in paths)


class TestEssentialFiles(unittest.TestCase):
    """The slim set must be derived by CALLING the accessors CAS flashes through, never by
    re-deriving their globs — otherwise a change to stock_boot_image()/edl_tools() would start
    quietly deleting files CAS had begun to need."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_fastboot_build_keeps_only_its_flash_target_image(self):
        fw = build_firmware(self.tmp, flash_target="init_boot", flash_method="fastboot")
        self.assertEqual(names(FW.essential_files(fw)), ["init_boot.img"])

    def test_fastboot_boot_target_keeps_boot_image(self):
        fw = build_firmware(self.tmp, flash_target="boot", flash_method="fastboot")
        self.assertEqual(names(FW.essential_files(fw)), ["boot.img"])

    def test_edl_build_keeps_image_plus_the_whole_firehose_toolchain(self):
        fw = build_firmware(self.tmp, flash_target="init_boot", flash_method="edl",
                            sub="emmc", edl=True)
        got = names(FW.essential_files(fw))
        for required in ("init_boot.img", "QSaharaServer", "QSaharaServer.exe",
                         "fh_loader", "fh_loader.exe", "prog_firehose_ddr.elf", "rawprogram1.xml"):
            self.assertIn(required, got, f"{required} is load-bearing for an EDL flash")

    def test_edl_build_keeps_both_host_variants(self):
        # edl_tools() prefers the host-appropriate binary and falls back to the other so a wrong-OS
        # package still DETECTS as EDL and reports cleanly. Keeping one variant breaks that report.
        fw = build_firmware(self.tmp, flash_method="edl", sub="emmc", edl=True)
        got = names(FW.essential_files(fw))
        self.assertIn("QSaharaServer", got)
        self.assertIn("QSaharaServer.exe", got)

    def test_bulk_is_never_essential(self):
        fw = build_firmware(self.tmp, flash_method="edl", sub="emmc", edl=True)
        got = names(FW.essential_files(fw))
        for b in BULK:
            self.assertNotIn(b, got)

    def test_fastboot_build_does_not_keep_edl_tools_it_does_not_use(self):
        fw = build_firmware(self.tmp, flash_method="fastboot", edl=True)
        self.assertEqual(names(FW.essential_files(fw)), ["init_boot.img"])


def payload_names(fw, version=None):
    pd = fw.payload_dir(version)
    return sorted(p.name for p in pd.rglob("*") if p.is_file())


class TestSlim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir()

    def test_keeps_essentials_and_moves_the_bulk(self):
        fw = build_firmware(self.root)
        res = FW.slim(fw, log=lambda *a: None)
        self.assertTrue(res["slimmed"])
        self.assertEqual(payload_names(fw), ["init_boot.img"])
        moved = FW.masters_root(self.root) / "rp6" / "v1" / "payload"
        self.assertTrue((moved / "super_1.img").is_file(), "bulk must be MOVED, never deleted")

    def test_preserves_relative_paths_so_no_glob_changes(self):
        # An EDL payload lives under emmc/. If slim flattened it, _payload_glob('**/…') would still
        # find things, but storage detection and rawprogram lookups that rely on the tree would drift.
        fw = build_firmware(self.root, flash_method="edl", sub="emmc", edl=True)
        FW.slim(fw, log=lambda *a: None)
        kept = fw.payload_dir() / "emmc" / "init_boot.img"
        self.assertTrue(kept.is_file(), "essentials must return to identical relative paths")

    def test_edl_build_still_resolves_as_edl_after_slimming(self):
        # The regression that would brick a MANGMI unit: flash_method is DERIVED from the toolchain
        # being present, so a slim that drops it silently turns an EDL build into a fastboot one.
        fw = build_firmware(self.root, flash_method="edl", sub="emmc", edl=True,
                            meta_extra={"flash_method": None})
        FW.slim(fw, log=lambda *a: None)
        fresh = FW.Firmware(fw.path)
        self.assertEqual(fresh.flash_method, "edl")
        self.assertIsNotNone(fresh.edl_tools())
        self.assertIsNotNone(fresh.stock_boot_image())

    def test_dry_run_changes_nothing(self):
        fw = build_firmware(self.root)
        before = payload_names(fw)
        res = FW.slim(fw, dry_run=True, log=lambda *a: None)
        self.assertFalse(res["slimmed"])
        self.assertGreater(res["moved_bytes"], 0)
        self.assertEqual(payload_names(fw), before)
        self.assertFalse(FW.masters_root(self.root).exists())

    def test_dry_run_does_not_capture_metadata(self):
        # The metadata gate greps multi-GB super images and WRITES version.meta.json. A dry run must
        # do neither — it is what the operator runs to preview the whole library before touching it.
        fw = build_firmware(self.root, meta_ok=False)
        vm = fw.path / "versions" / "v1" / "version.meta.json"
        before = vm.read_text()
        res = FW.slim(fw, dry_run=True, log=lambda *a: None)
        self.assertFalse(res["slimmed"])
        self.assertEqual(vm.read_text(), before, "dry run must not write metadata")

    def test_is_idempotent(self):
        fw = build_firmware(self.root)
        FW.slim(fw, log=lambda *a: None)
        after_first = payload_names(fw)
        res = FW.slim(FW.Firmware(fw.path), log=lambda *a: None)
        self.assertFalse(res["slimmed"])
        self.assertEqual(payload_names(fw), after_first)

    def test_stamps_version_meta(self):
        fw = build_firmware(self.root)
        FW.slim(fw, log=lambda *a: None)
        vm = json.loads((fw.path / "versions" / "v1" / "version.meta.json").read_text())
        self.assertTrue(vm["slim"])
        self.assertIn("master_at", vm)
        self.assertGreater(vm["removed_bytes"], 0)

    def test_refuses_when_metadata_is_unrecoverable(self):
        # super_*.img is the ONLY source of fingerprint/chip. Moving it away before capturing that
        # data loses it permanently, so an un-derivable build must be left completely alone.
        fw = build_firmware(self.root, meta_ok=False, bulk=False)
        res = FW.slim(fw, log=lambda *a: None)
        self.assertFalse(res["slimmed"])
        self.assertIn("metadata", res["reason"].lower())
        self.assertIn("super_1.img", payload_names(fw) + ["super_1.img"])  # payload untouched
        self.assertFalse(FW.masters_root(self.root).exists())

    def test_captures_metadata_before_moving_the_images_it_comes_from(self):
        # 8 of 10 live builds have empty metadata. slim must derive and persist it while the super
        # images are still in place — after the move that data is gone for good.
        fw = build_firmware(self.root, meta_ok=False)
        res = FW.slim(fw, log=lambda *a: None)
        self.assertTrue(res["slimmed"], res["reason"])
        vm = json.loads((fw.path / "versions" / "v1" / "version.meta.json").read_text())
        self.assertEqual(vm["fingerprint"], FP)
        self.assertEqual(vm["board_platform"], "kalama")

    def test_already_minimal_build_is_a_no_op_not_a_refusal(self):
        # odin2-default / odin3 / retroid-pocket-5 are bare boot images: nothing to move, and no super
        # image to derive metadata from. Demanding metadata there would report a scary refusal for a
        # build that is already in the desired end state.
        fw = build_firmware(self.root, flash_target="boot", bulk=False, meta_ok=False)
        res = FW.slim(fw, log=lambda *a: None)
        self.assertFalse(res["slimmed"])
        self.assertEqual(res["moved_files"], 0)
        self.assertNotIn("metadata", res["reason"].lower())
        self.assertEqual(payload_names(fw), ["boot.img"])
        self.assertFalse(FW.masters_root(self.root).exists())

    def test_refuses_when_there_is_no_stock_image(self):
        fw = build_firmware(self.root)
        (fw.payload_dir() / "init_boot.img").unlink()
        res = FW.slim(fw, log=lambda *a: None)
        self.assertFalse(res["slimmed"])
        self.assertFalse(FW.masters_root(self.root).exists())


class TestUnslim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir()

    def test_restores_the_full_tree(self):
        fw = build_firmware(self.root, flash_method="edl", sub="emmc", edl=True)
        before = payload_names(fw)
        FW.slim(fw, log=lambda *a: None)
        res = FW.unslim(FW.Firmware(fw.path), log=lambda *a: None)
        self.assertTrue(res["restored"])
        self.assertEqual(payload_names(FW.Firmware(fw.path)), before)
        vm = json.loads((fw.path / "versions" / "v1" / "version.meta.json").read_text())
        self.assertFalse(vm.get("slim"))


if __name__ == "__main__":
    unittest.main()
