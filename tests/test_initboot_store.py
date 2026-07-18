# tests/test_initboot_store.py
import json
import os
import sys
import pathlib
import tempfile
import unittest
from unittest import mock

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
        IBS.put(self.root, FP, self.img, {"fingerprint": FP, "size": self.img.stat().st_size})
        other = pathlib.Path(self.tmp) / "other.img"
        other.write_bytes(b"ANDROID!" + b"\xff" * 2048)
        IBS.put(self.root, FP, other, {"fingerprint": FP, "size": other.stat().st_size})
        self.assertEqual(IBS.get(self.root, FP).read_bytes(), self.img.read_bytes())

    def test_put_then_get_meta_size_matches(self):
        # Round-trip: meta["size"] as written by the caller must equal the stored image's actual size —
        # this is exactly what get()'s integrity gate checks on every read.
        p = IBS.put(self.root, FP, self.img, {"fingerprint": FP, "size": self.img.stat().st_size})
        self.assertEqual(p.stat().st_size, self.img.stat().st_size)
        got = IBS.get(self.root, FP)
        self.assertEqual(got, p)
        meta = json.loads((self.root / IBS.slug(FP) / "meta.json").read_text())
        self.assertEqual(meta["size"], got.stat().st_size)

    def test_store_root_helper(self):
        fw_root = pathlib.Path(self.tmp) / "library" / "_firmware"
        self.assertEqual(IBS.store_root(fw_root), fw_root.parent / "_init_boot_factory")


class TestIntegrityGate(unittest.TestCase):
    """get()/has() must treat a truncated/incomplete capture as a MISS, never as 'already captured' —
    both capture's idempotent-skip and seal's lookup rely on that to never trust a broken image."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_init_boot_factory"
        self.d = self.root / IBS.slug(FP)
        self.d.mkdir(parents=True)

    def test_img_without_meta_is_a_miss(self):
        (self.d / "init_boot.img").write_bytes(b"ANDROID!" + b"\x00" * 100)
        self.assertIsNone(IBS.get(self.root, FP))
        self.assertFalse(IBS.has(self.root, FP))

    def test_truncated_image_size_mismatch_is_a_miss(self):
        data = b"ANDROID!" + b"\x00" * 1000
        (self.d / "init_boot.img").write_bytes(data[:100])              # truncated on-disk
        (self.d / "meta.json").write_text(json.dumps({"fingerprint": FP, "size": len(data)}))  # claims full size
        self.assertIsNone(IBS.get(self.root, FP))
        self.assertFalse(IBS.has(self.root, FP))

    def test_valid_put_get_round_trip_still_works(self):
        img = pathlib.Path(self.tmp) / "src.img"
        img.write_bytes(b"ANDROID!" + b"\x00" * 2048)
        p = IBS.put(self.root, FP, img, {"fingerprint": FP, "size": img.stat().st_size})
        got = IBS.get(self.root, FP)
        self.assertEqual(got, p)
        self.assertTrue(IBS.has(self.root, FP))
        meta = json.loads((self.d / "meta.json").read_text())
        self.assertEqual(meta["size"], got.stat().st_size)

    def test_put_after_corrupt_entry_recaptures_good_image(self):
        # Corrupt entry already on disk (image present, meta missing) — must NOT be treated as captured.
        (self.d / "init_boot.img").write_bytes(b"\x00" * 50)
        self.assertFalse(IBS.has(self.root, FP))
        good = pathlib.Path(self.tmp) / "good.img"
        good_bytes = b"ANDROID!" + b"\xaa" * 4096
        good.write_bytes(good_bytes)
        p = IBS.put(self.root, FP, good, {"fingerprint": FP, "size": len(good_bytes)})
        self.assertEqual(p.read_bytes(), good_bytes)             # the corrupt image was overwritten
        self.assertTrue(IBS.has(self.root, FP))
        self.assertEqual(IBS.get(self.root, FP).read_bytes(), good_bytes)


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_init_boot_factory"
        self.img = pathlib.Path(self.tmp) / "src.img"
        self.img.write_bytes(b"ANDROID!" + b"\x00" * 1024)

    def test_put_never_leaves_a_bare_tmp_file_in_place_of_dest(self):
        # dest must appear via a single atomic rename — no window where a partially-written init_boot.img
        # sits at the real path, and no leftover .tmp file once put() returns successfully.
        p = IBS.put(self.root, FP, self.img, {"fingerprint": FP, "size": self.img.stat().st_size})
        leftovers = [f for f in p.parent.iterdir() if f.name != "init_boot.img" and f.name != "meta.json"]
        self.assertEqual(leftovers, [])

    def test_put_raises_and_leaves_no_dest_when_replace_fails(self):
        # os.replace failing (disk full / permission denied) must not leave a corrupt/partial dest behind,
        # and must not silently swallow the error — capture_factory_init_boot (tested separately) is what
        # turns this into a non-fatal skip; put() itself propagates.
        with mock.patch.object(IBS.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                IBS.put(self.root, FP, self.img, {"fingerprint": FP, "size": self.img.stat().st_size})
        d = self.root / IBS.slug(FP)
        self.assertFalse((d / "init_boot.img").exists())
        self.assertFalse((d / "meta.json").exists())
        self.assertFalse(IBS.has(self.root, FP))


if __name__ == "__main__":
    unittest.main()
