# tests/test_initboot_store.py
import gzip
import json
import lzma
import os
import struct
import sys
import pathlib
import tempfile
import threading
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


def make_boot_img(ramdisk_payload, compress="gzip", hdr_version=2):
    """A minimal but REAL Android boot image (v2 or v3 layout) whose ramdisk is compressed — the shape
    every shipping boot.img actually has. Markers inside a compressed ramdisk are invisible to a raw
    byte scan, which is exactly the blind spot this fixture exists to prove."""
    if compress == "gzip":
        rd = gzip.compress(ramdisk_payload)
    elif compress == "xz":
        rd = lzma.compress(ramdisk_payload)
    elif compress == "none":
        rd = ramdisk_payload
    else:
        # A codec we have no decompressor for. XOR-obfuscated so the markers are genuinely unreadable —
        # a plaintext tail would be found by the raw scan and never exercise the UNKNOWN path.
        rd = b"\x99\x99" + bytes(b ^ 0x5A for b in ramdisk_payload)
    page, kernel = 4096, b"\x01" * 5000
    hdr = bytearray(page)
    hdr[0:8] = b"ANDROID!"
    if hdr_version >= 3:
        struct.pack_into("<II", hdr, 8, len(kernel), len(rd))          # kernel_size, ramdisk_size
    else:
        struct.pack_into("<I", hdr, 8, len(kernel))                    # kernel_size
        struct.pack_into("<I", hdr, 16, len(rd))                       # ramdisk_size
        struct.pack_into("<I", hdr, 36, page)                          # page_size
    struct.pack_into("<I", hdr, 40, hdr_version)                       # header_version (both layouts)

    def pad(b):
        return b + b"\x00" * (-len(b) % page)
    return bytes(hdr) + pad(kernel) + pad(rd)


# The literal entries found in the real Banners FW189 OC image that broke RP5 Lock on 2026-07-20.
PATCHED_RAMDISK = b"070701...\x00.backup/.magisk\x00.backup/init.xz\x00overlay.d/sbin/magisk.xz\x00"
CLEAN_RAMDISK = b"070701...\x00init\x00fstab.qcom\x00avb/q-gsi.avbpubkey\x00system/bin/e2fsck\x00"


class TestCompressedRamdiskDetection(unittest.TestCase):
    """A Magisk-patched boot image gzips its ramdisk, so the patch markers never appear in the raw
    bytes. Scanning only the raw image reports a rooted image as CLEAN — the defect that made ③ Lock
    flash a rooted 'stock' image at the RP5 and leave it rooted."""

    def test_gzip_ramdisk_magisk_is_detected(self):
        img = make_boot_img(PATCHED_RAMDISK, "gzip")
        self.assertNotIn(b".magisk", img, "fixture must hide the marker from a raw scan")
        self.assertTrue(IBS.contains_magisk(img))

    def test_gzip_ramdisk_clean_is_not_flagged(self):
        self.assertFalse(IBS.contains_magisk(make_boot_img(CLEAN_RAMDISK, "gzip")))

    def test_xz_ramdisk_magisk_is_detected(self):
        self.assertTrue(IBS.contains_magisk(make_boot_img(PATCHED_RAMDISK, "xz")))

    def test_uncompressed_ramdisk_magisk_is_detected(self):
        self.assertTrue(IBS.contains_magisk(make_boot_img(PATCHED_RAMDISK, "none")))

    def test_header_v3_layout_is_parsed(self):
        self.assertTrue(IBS.contains_magisk(make_boot_img(PATCHED_RAMDISK, "gzip", hdr_version=3)))

    def test_scan_reports_patched_clean_and_unknown(self):
        self.assertEqual(IBS.magisk_scan(make_boot_img(PATCHED_RAMDISK, "gzip")), IBS.PATCHED)
        self.assertEqual(IBS.magisk_scan(make_boot_img(CLEAN_RAMDISK, "gzip")), IBS.CLEAN)
        # An unreadable codec must NEVER read as CLEAN — that is the failure mode we are removing.
        self.assertEqual(IBS.magisk_scan(make_boot_img(PATCHED_RAMDISK, "opaque")), IBS.UNKNOWN)

    def test_non_boot_image_is_unknown(self):
        self.assertEqual(IBS.magisk_scan(b"not-a-boot-image" * 32), IBS.UNKNOWN)


def lz4_literals(data):
    """Encode `data` as one literals-only LZ4 block (valid, if uncompressed): a token carrying the
    literal length, the 15-escape byte run when it doesn't fit the nibble, then the literals."""
    if len(data) < 15:
        return bytes([len(data) << 4]) + data
    rest, extra = len(data) - 15, b""
    while rest >= 255:
        extra += b"\xff"
        rest -= 255
    return b"\xf0" + extra + bytes([rest]) + data


def lz4_legacy(payload_blocks):
    """An LZ4 'legacy' frame: magic 0x184C2102 then [u32 block_size][block]... — the container Android
    uses for init_boot ramdisks (the RP6/kalama fleet's format)."""
    out = struct.pack("<I", 0x184C2102)
    for b in payload_blocks:
        out += struct.pack("<I", len(b)) + b
    return out


class TestLz4(unittest.TestCase):
    """The kalama fleet's init_boot ramdisks are LZ4, which Python cannot decompress out of the box.
    Without this the Magisk guard reports UNKNOWN on exactly the images CAS flashes most."""

    def test_block_literals_only(self):
        # token: 5 literals (5<<4), no match -> 0x50
        self.assertEqual(IBS._lz4_block_decompress(b"\x50hello"), b"hello")

    def test_block_with_overlapping_match(self):
        # 3 literals "abc", then match offset 3 length 6 -> "abcabcabc"
        block = b"\x32abc" + struct.pack("<H", 3)
        self.assertEqual(IBS._lz4_block_decompress(block), b"abcabcabc")

    def test_block_long_literal_length_escape(self):
        payload = b"x" * 20                       # lit_len 20 -> nibble 15 + extra byte 5
        self.assertEqual(IBS._lz4_block_decompress(b"\xf0\x05" + payload), payload)

    def test_legacy_frame_roundtrip_multiblock(self):
        frame = lz4_legacy([b"\x50hello", b"\x50world"])
        self.assertEqual(IBS._decompress_ramdisk(frame), b"helloworld")

    def test_magisk_marker_inside_lz4_ramdisk_is_detected(self):
        frame = lz4_legacy([lz4_literals(PATCHED_RAMDISK)])
        img = make_boot_img(frame, "none", hdr_version=4)   # frame is the ramdisk, stored as-is
        self.assertTrue(IBS.contains_magisk(img))

    def test_clean_lz4_ramdisk_reads_clean_not_unknown(self):
        """The decisive one: CLEAN (not UNKNOWN) proves the LZ4 frame was actually decoded. Before the
        LZ4 decoder existed this returned UNKNOWN, leaving the whole kalama/RP6 fleet unguarded."""
        frame = lz4_legacy([lz4_literals(CLEAN_RAMDISK)])
        img = make_boot_img(frame, "none", hdr_version=4)
        self.assertEqual(IBS.magisk_scan(img), IBS.CLEAN)


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


class TestConcurrentPut(unittest.TestCase):
    """Fleet-batch regression: root_all/seal_all fan out across devices with a ThreadPoolExecutor in ONE
    process. Several units of the SAME build share a fingerprint => same store slug => same dest path.
    put()'s temp filename must be unique PER CALL (not just per-PID) so concurrent writers never share a
    temp path and interleave their write_bytes() calls into a corrupted-but-same-length blob that the
    size-only integrity gate in get() would otherwise wave through."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_init_boot_factory"
        self.img = pathlib.Path(self.tmp) / "src.img"
        # Multi-KB, non-repeating body so any interleaved/truncated/zero-holed corruption is detectable
        # byte-for-byte -- a same-length-but-wrong-bytes result would slip past a size-only check.
        body = bytes((i * 37 + 11) % 256 for i in range(64 * 1024))
        self.img.write_bytes(b"ANDROID!" + body)
        self.expected = self.img.read_bytes()

    def test_concurrent_put_same_fingerprint_never_corrupts(self):
        n_threads = 8
        barrier = threading.Barrier(n_threads)
        errors = []
        meta = {"fingerprint": FP, "size": len(self.expected)}

        def worker():
            try:
                barrier.wait(timeout=10)  # release all threads together -> max interleave window
                IBS.put(self.root, FP, self.img, meta)
            except Exception as exc:  # surfaced via the errors list, not lost on a background thread
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        got = IBS.get(self.root, FP)
        self.assertIsNotNone(got, "get() must see a valid capture after concurrent put()s")
        self.assertEqual(got.stat().st_size, len(self.expected))
        self.assertEqual(got.read_bytes(), self.expected)  # exact bytes: catches interleaved/holed writes


if __name__ == "__main__":
    unittest.main()
