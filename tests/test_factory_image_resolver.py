# tests/test_factory_image_resolver.py
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import provision as PV
from cas import initboot_store as IBS

FP = "MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/eng.hxh.20260507.141302:user/release-keys"


def _f(td, name, data=b"ANDROID!" + b"\x00" * 64):
    p = pathlib.Path(td) / name
    p.write_bytes(data)
    return str(p)


class TestResolver(unittest.TestCase):
    def test_capture_wins_when_no_kit_is_proven(self):
        """RP6/Thor REGRESSION GUARD: their kits are a different build and are never proven."""
        with tempfile.TemporaryDirectory() as td:
            lib, cap = _f(td, "lib.img"), _f(td, "cap.img", b"ANDROID!" + b"\x11" * 64)
            path, prov = PV.resolve_factory_init_boot(lib, cap, None, FP, log=lambda m: None)
            self.assertEqual(path, cap)
            self.assertEqual(prov, "captured")

    def test_proven_kit_wins_over_a_disagreeing_capture(self):
        with tempfile.TemporaryDirectory() as td:
            lib = _f(td, "lib.img")
            cap = _f(td, "cap.img", b"ANDROID!" + b"\x11" * 64)
            kit = _f(td, "kit.img", b"ANDROID!" + b"\x22" * 64)
            path, prov = PV.resolve_factory_init_boot(lib, cap, kit, FP, log=lambda m: None)
            self.assertEqual(path, kit)
            self.assertEqual(prov, "proven-kit")

    def test_agreeing_capture_is_not_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            same = b"ANDROID!" + b"\x33" * 64
            lib = _f(td, "lib.img")
            cap = _f(td, "cap.img", same)
            kit = _f(td, "kit.img", same)
            store = pathlib.Path(td) / "store"
            (IBS._dir(store, FP)).mkdir(parents=True, exist_ok=True)
            PV.resolve_factory_init_boot(lib, cap, kit, FP, log=lambda m: None, store_root=store)
            self.assertTrue(IBS._dir(store, FP).is_dir(), "identical capture must be kept")

    def test_disagreeing_capture_is_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            lib = _f(td, "lib.img")
            cap = _f(td, "cap.img", b"ANDROID!" + b"\x11" * 64)
            kit = _f(td, "kit.img", b"ANDROID!" + b"\x22" * 64)
            store = pathlib.Path(td) / "store"
            (IBS._dir(store, FP)).mkdir(parents=True, exist_ok=True)
            PV.resolve_factory_init_boot(lib, cap, kit, FP, log=lambda m: None, store_root=store)
            self.assertFalse(IBS._dir(store, FP).is_dir(), "contradicted capture must be moved aside")

    def test_neither_kit_nor_capture_is_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            lib = _f(td, "lib.img")
            path, prov = PV.resolve_factory_init_boot(lib, None, None, FP, log=lambda m: None)
            self.assertEqual(path, lib)
            self.assertEqual(prov, "unverified")

    def test_resolve_seal_stock_signature_unchanged(self):
        """Existing callers/tests must keep working: same args, same single return value."""
        with tempfile.TemporaryDirectory() as td:
            lib, cap = _f(td, "lib.img"), _f(td, "cap.img")
            self.assertEqual(PV.resolve_seal_stock(lib, cap, FP, log=lambda m: None), cap)
            self.assertEqual(PV.resolve_seal_stock(lib, None, FP, log=lambda m: None), lib)


if __name__ == "__main__":
    unittest.main()
