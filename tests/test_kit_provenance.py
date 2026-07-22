# tests/test_kit_provenance.py
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import firmware as FW

FP = "MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/eng.hxh.20260507.141302:user/release-keys"


def _make_kit(root, fw_id="air-x", version="20260507-165105", fingerprint=""):
    """Minimal on-disk kit: meta.json + versions/<v>/version.meta.json."""
    p = pathlib.Path(root) / fw_id
    (p / "versions" / version).mkdir(parents=True, exist_ok=True)
    (p / "meta.json").write_text(json.dumps({"id": fw_id, "current": version}))
    (p / "versions" / version / "version.meta.json").write_text(
        json.dumps({"fingerprint": fingerprint}))
    return p


class TestBuildFingerprint(unittest.TestCase):
    def test_blank_fingerprint_reads_as_none(self):
        with tempfile.TemporaryDirectory() as td:
            _make_kit(td, fingerprint="")
            fw = FW.find("air-x", td)
            self.assertIsNone(FW.build_fingerprint(fw, "20260507-165105"))

    def test_whitespace_fingerprint_reads_as_none(self):
        with tempfile.TemporaryDirectory() as td:
            _make_kit(td, fingerprint="   ")
            fw = FW.find("air-x", td)
            self.assertIsNone(FW.build_fingerprint(fw, "20260507-165105"))

    def test_set_then_read_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            _make_kit(td)
            FW.set_build_fingerprint("air-x", td, "20260507-165105", FP)
            fw = FW.find("air-x", td)
            self.assertEqual(FW.build_fingerprint(fw, "20260507-165105"), FP)

    def test_set_preserves_other_version_meta_keys(self):
        with tempfile.TemporaryDirectory() as td:
            p = _make_kit(td)
            vm = p / "versions" / "20260507-165105" / "version.meta.json"
            vm.write_text(json.dumps({"fingerprint": "", "dev_code": "MQ66", "storage": "emmc"}))
            FW.set_build_fingerprint("air-x", td, "20260507-165105", FP)
            meta = json.loads(vm.read_text())
            self.assertEqual(meta["dev_code"], "MQ66")
            self.assertEqual(meta["storage"], "emmc")
            self.assertEqual(meta["fingerprint"], FP)

    def test_unknown_firmware_id_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                FW.set_build_fingerprint("nope", td, "v1", FP)


if __name__ == "__main__":
    unittest.main()
