# tests/test_initboot_quarantine.py
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import initboot_store as IBS

FP = "MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/eng.hxh.20260507.141302:user/release-keys"
IMG = b"ANDROID!" + b"\x00" * 2048


def _store_with_capture(root):
    d = IBS._dir(root, FP)
    d.mkdir(parents=True, exist_ok=True)
    (d / "init_boot.img").write_bytes(IMG)
    (d / "meta.json").write_text(json.dumps({"fingerprint": FP, "size": len(IMG)}))
    return d


class TestQuarantine(unittest.TestCase):
    def test_moves_capture_aside_and_get_misses(self):
        with tempfile.TemporaryDirectory() as td:
            _store_with_capture(td)
            self.assertIsNotNone(IBS.get(td, FP))
            q = IBS.quarantine(td, FP, "contradicted by proven kit")
            self.assertIsNotNone(q)
            self.assertTrue(pathlib.Path(q).is_dir())
            self.assertIsNone(IBS.get(td, FP), "quarantined capture must read as a MISS")

    def test_records_the_reason(self):
        with tempfile.TemporaryDirectory() as td:
            _store_with_capture(td)
            q = IBS.quarantine(td, FP, "contradicted by proven kit")
            note = json.loads((pathlib.Path(q) / "quarantine.json").read_text())
            self.assertEqual(note["reason"], "contradicted by proven kit")
            self.assertEqual(note["fingerprint"], FP)

    def test_preserves_the_image_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            _store_with_capture(td)
            q = IBS.quarantine(td, FP, "why")
            self.assertEqual((pathlib.Path(q) / "init_boot.img").read_bytes(), IMG)

    def test_nothing_to_quarantine_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(IBS.quarantine(td, FP, "why"))

    def test_second_quarantine_does_not_collide(self):
        with tempfile.TemporaryDirectory() as td:
            _store_with_capture(td)
            q1 = IBS.quarantine(td, FP, "first")
            _store_with_capture(td)
            q2 = IBS.quarantine(td, FP, "second")
            self.assertNotEqual(q1, q2)
            self.assertTrue(pathlib.Path(q1).is_dir())
            self.assertTrue(pathlib.Path(q2).is_dir())


if __name__ == "__main__":
    unittest.main()
