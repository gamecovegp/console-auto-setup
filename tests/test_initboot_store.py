# tests/test_initboot_store.py
import os
import sys
import pathlib
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import initboot_store as IBS

FP = "qti/kalama/kalama:13/TKQ1.231222.001/eng.RP6.20260119.170007:user/release-keys"


class TestGuards(unittest.TestCase):
    def test_looks_like_boot_image_true(self):
        self.assertTrue(IBS.looks_like_boot_image(b"ANDROID!" + b"\x00" * 100))

    def test_looks_like_boot_image_false_on_zeros(self):
        self.assertFalse(IBS.looks_like_boot_image(b"\x00" * 128))

    def test_contains_magisk_true(self):
        self.assertTrue(IBS.contains_magisk(b"ANDROID!....MAGISKINIT....payload"))

    def test_contains_magisk_false(self):
        self.assertFalse(IBS.contains_magisk(b"ANDROID!" + b"\x00" * 256))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_init_boot_factory"
        self.img = pathlib.Path(self.tmp) / "src.img"
        self.img.write_bytes(b"ANDROID!" + b"\x00" * 1024)

    def test_slug_is_filesystem_safe(self):
        s = IBS.slug(FP)
        self.assertNotIn("/", s)
        self.assertNotIn(":", s)
        self.assertIn("eng.RP6.20260119.170007", s)

    def test_put_then_get_and_has(self):
        self.assertFalse(IBS.has(self.root, FP))
        self.assertIsNone(IBS.get(self.root, FP))
        p = IBS.put(self.root, FP, self.img, {"fingerprint": FP, "sha256": "x", "size": 1032})
        self.assertTrue(p.exists())
        self.assertEqual(p.read_bytes(), self.img.read_bytes())
        self.assertTrue(IBS.has(self.root, FP))
        self.assertEqual(IBS.get(self.root, FP), p)

    def test_put_is_idempotent_first_wins(self):
        IBS.put(self.root, FP, self.img, {"fingerprint": FP})
        other = pathlib.Path(self.tmp) / "other.img"
        other.write_bytes(b"ANDROID!" + b"\xff" * 2048)
        IBS.put(self.root, FP, other, {"fingerprint": FP})
        self.assertEqual(IBS.get(self.root, FP).read_bytes(), self.img.read_bytes())


if __name__ == "__main__":
    unittest.main()
