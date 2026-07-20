"""Per-build store of each unit's OWN factory init_boot, captured at root time and restored at seal.

Pure module: no adb/config/firmware imports. Callers pass `store_root` (a Path), so it stays trivially
unit-testable and free of import cycles. Layout: <store_root>/<slug(fingerprint)>/init_boot.img + meta.json
"""
import bz2
import gzip
import json
import lzma
import os
import pathlib
import re
import struct
import time
import uuid
import zlib

# Markers of a Magisk-patched ramdisk. The binary ones (MAGISKINIT/MAGISKPOLICY) appear in magiskinit
# itself; the path ones are the files Magisk ADDS to the ramdisk cpio and are what a real patched image
# actually shows (see the Banners FW189 RP5 image: .backup/.magisk, .backup/init.xz,
# overlay.d/sbin/magisk.xz). Path markers are the reliable ones — they survive across Magisk versions.
_MAGISK_MARKERS = (b"MAGISKINIT", b"MAGISKPOLICY", b".magisk", b"magiskinit",
                   b"overlay.d/sbin/magisk", b"magisk.xz")

PATCHED, CLEAN, UNKNOWN = "patched", "clean", "unknown"

_LZ4_LEGACY_MAGIC = b"\x02\x21\x4c\x18"      # 0x184C2102, little-endian

# Ramdisk codecs, by leading magic. gzip is what Magisk uses on these units; lz4/zstd need third-party
# modules, so they are attempted only when importable and otherwise report UNKNOWN (never CLEAN).
_DECOMPRESSORS = (
    (b"\x1f\x8b", lambda b: gzip.decompress(b)),
    (b"\xfd7zXZ", lambda b: lzma.decompress(b)),
    (b"\x5d\x00\x00", lambda b: lzma.decompress(b, format=lzma.FORMAT_ALONE)),
    (b"BZh", lambda b: bz2.decompress(b)),
    (b"\x78", lambda b: zlib.decompress(b)),
    (b"070701", lambda b: b),          # uncompressed cpio — already plain
    (b"070702", lambda b: b),
    (_LZ4_LEGACY_MAGIC, lambda b: _lz4_legacy_decompress(b)),
)


def _ramdisk_bytes(data):
    """The raw (still-compressed) ramdisk slice of an Android boot image, or None if `data` isn't one.

    Handles BOTH header layouts, which put ramdisk_size in different places: v0-v2 has
    kernel_size@8 / ramdisk_size@16 / page_size@36, while v3+ has kernel_size@8 / ramdisk_size@12 and a
    fixed 4096 page. header_version sits at offset 40 in both, so we read that first to pick the layout.
    Layout: [header page][kernel, page-aligned][ramdisk, page-aligned]."""
    if len(data) < 64 or data[:8] != b"ANDROID!":
        return None
    try:
        (hdr_version,) = struct.unpack_from("<I", data, 40)
        if hdr_version >= 3:
            kernel_size, ramdisk_size = struct.unpack_from("<II", data, 8)
            page = 4096
        else:
            (kernel_size,) = struct.unpack_from("<I", data, 8)
            (ramdisk_size,) = struct.unpack_from("<I", data, 16)
            (page,) = struct.unpack_from("<I", data, 36)
    except struct.error:
        return None
    if not page or page > len(data) or not ramdisk_size:
        return None
    off = page + -(-kernel_size // page) * page          # header page + page-aligned kernel
    rd = data[off:off + ramdisk_size]
    return rd if len(rd) == ramdisk_size else None


def _lz4_block_decompress(src):
    """Decode one LZ4 block (the raw compression format, no frame header).

    Implemented here because Android's init_boot ramdisks are LZ4 (the whole kalama/RP6 fleet) and the
    Python stdlib ships no LZ4 codec — without this the Magisk guard would report UNKNOWN on exactly
    the images CAS flashes most often. A block is a series of sequences: [token][literal-length
    extension][literals][2-byte match offset][match-length extension]. The high nibble of the token is
    the literal count, the low nibble the match count (minus the 4-byte minimum); a nibble of 15 means
    'add the following 255-terminated byte run'. Matches may OVERLAP the output written so far (that is
    how LZ4 encodes runs), so those bytes are copied one at a time rather than sliced."""
    out = bytearray()
    pos, n = 0, len(src)
    while pos < n:
        token = src[pos]
        pos += 1
        lit_len = token >> 4
        if lit_len == 15:
            while pos < n:
                b = src[pos]
                pos += 1
                lit_len += b
                if b != 255:
                    break
        out += src[pos:pos + lit_len]
        pos += lit_len
        if pos + 2 > n:                       # final sequence is literals-only: no match follows
            break
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(out):
            raise ValueError("bad LZ4 match offset")
        match_len = token & 0x0F
        if match_len == 15:
            while pos < n:
                b = src[pos]
                pos += 1
                match_len += b
                if b != 255:
                    break
        match_len += 4
        start = len(out) - offset
        for i in range(match_len):            # byte-at-a-time: matches legitimately overlap
            out.append(out[start + i])
    return bytes(out)


def _lz4_legacy_decompress(data):
    """Decode an LZ4 'legacy' frame: magic then [u32 block_size][block]... to EOF."""
    out, pos = bytearray(), 4
    while pos + 4 <= len(data):
        (size,) = struct.unpack_from("<I", data, pos)
        pos += 4
        if size == 0 or size > len(data) - pos:
            break
        out += _lz4_block_decompress(data[pos:pos + size])
        pos += size
    return bytes(out)


def _decompress_ramdisk(rd):
    """The ramdisk's PLAIN bytes, or None when no available codec can read it. None means 'I could not
    look', which callers must treat as UNKNOWN — never as clean."""
    for magic, fn in _DECOMPRESSORS:
        if rd.startswith(magic):
            try:
                return fn(rd)
            except Exception:
                return None
    for mod, attr in (("lz4.frame", "decompress"), ("lz4.block", "decompress"), ("zstandard", None)):
        try:                                             # optional third-party codecs, if installed
            m = __import__(mod, fromlist=["x"])
            return m.decompress(rd) if attr else m.ZstdDecompressor().decompressobj().decompress(rd)
        except Exception:
            continue
    return None


def magisk_scan(data):
    """PATCHED / CLEAN / UNKNOWN for an Android boot image.

    A shipping boot image COMPRESSES its ramdisk, so Magisk's markers are invisible to a raw byte scan —
    scanning `data` alone reported the Magisk-patched Banners RP5 image as clean, and ③ Lock flashed it
    to 'un-root' a unit that then booted still rooted. So: locate the ramdisk, decompress it, and scan
    the PLAIN bytes. UNKNOWN (not CLEAN) whenever we cannot actually look — an unparseable header or a
    codec we don't have. Callers decide how strict to be, but nothing may read UNKNOWN as proof of
    cleanliness."""
    # Ramdisk FIRST, raw scan only as a fallback. The ramdisk is a couple of MB while the image can be
    # ~100MB, and the markers only ever live in the ramdisk — scanning the whole image first cost ~11s
    # per call on a 96MB RP5 boot.img, which would land on every seal.
    rd = _ramdisk_bytes(data)
    if rd is not None:
        plain = _decompress_ramdisk(rd)
        if plain is not None:
            return PATCHED if any(m in plain for m in _MAGISK_MARKERS) else CLEAN
    if any(m in data for m in _MAGISK_MARKERS):    # not parseable as a boot image, or codec unavailable
        return PATCHED
    return UNKNOWN

_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF = 0.02


def looks_like_boot_image(data):
    """True iff `data` is an Android boot image (magic 'ANDROID!' at offset 0). A zeroed/empty inactive
    slot fails this — the reason we never store an unpopulated single-slot-flashed unit's dump."""
    return len(data) >= 8 and data[:8] == b"ANDROID!"


def contains_magisk(data):
    """True iff the image is a patched/rooted image (not factory). Thin bool wrapper over magisk_scan()
    for callers that only branch on 'definitely patched'; UNKNOWN reads as False here, so any caller
    that must not treat 'could not look' as clean should use magisk_scan() directly."""
    return magisk_scan(data) == PATCHED


def slug(fingerprint):
    """Filesystem-safe key from a build fingerprint (keeps alnum and dots, collapses the rest)."""
    return re.sub(r"[^A-Za-z0-9.]+", "_", fingerprint or "").strip("_") or "unknown"


def store_root(firmware_root):
    """The per-build init_boot store dir, sited alongside the firmware library: <firmware_root's
    parent>/_init_boot_factory. ONE helper so cli.py / root_all / seal_all can't drift on the layout."""
    return pathlib.Path(firmware_root).parent / "_init_boot_factory"


def _dir(store_root, fingerprint):
    return pathlib.Path(store_root) / slug(fingerprint)


def _unlink_quietly(p):
    try:
        p.unlink()
    except OSError:
        pass


def _replace_retry(tmp, dest):
    """os.replace() that tolerates Windows' transient PermissionError. Returns True iff `tmp` landed.

    POSIX rename() always succeeds over an existing dest. Windows MoveFileEx(REPLACE_EXISTING) instead
    fails with PermissionError('Access is denied') while ANOTHER handle to dest is open -- exactly what
    root_all/seal_all's ThreadPoolExecutor produces when several same-build units reach put() together.
    Retry briefly, then REPORT the outcome instead of raising, so the caller can decide whether a
    competing (byte-identical) writer already satisfied the request."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, dest)
            return True
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                return False
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))
    return False


def _write_meta(d, meta):
    """Write meta.json via the same temp+replace dance as the image: concurrent same-build put()s would
    otherwise interleave into a half-written meta.json that get()'s json.loads reads as corrupt."""
    tmp = d / f".meta.json.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    dest = d / "meta.json"
    try:
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        landed = _replace_retry(tmp, dest)
    except Exception:
        _unlink_quietly(tmp)
        raise
    if not landed:
        _unlink_quietly(tmp)
        if not dest.is_file():
            raise OSError(f"could not write the capture's meta.json: {dest}")


def has(store_root, fingerprint):
    return get(store_root, fingerprint) is not None


def get(store_root, fingerprint):
    """The stored init_boot.img path for `fingerprint`, iff it passes an INTEGRITY GATE: init_boot.img
    exists, meta.json exists, and the image's on-disk size equals meta['size']. A truncated/incomplete
    capture (crash mid-write, killed transfer, corrupt meta) must read as a MISS — not "already
    captured" — else capture()'s idempotent-skip and seal's lookup would both silently trust a broken
    image. None on any failure (missing file, unreadable/corrupt meta.json, size mismatch)."""
    d = _dir(store_root, fingerprint)
    img = d / "init_boot.img"
    meta_p = d / "meta.json"
    if not img.is_file() or not meta_p.is_file():
        return None
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        if img.stat().st_size != meta.get("size"):
            return None
    except (OSError, ValueError):
        return None
    return img


def put(store_root, fingerprint, img_path, meta):
    """Store `img_path` as this build's factory init_boot. Idempotent: if a VALID one already exists for
    this build (per get()'s integrity gate), keep it (first clean capture wins) and return it — never
    overwrite a good capture. A prior entry that FAILS the integrity gate (truncated/corrupt) is not a
    valid capture, so this re-captures over it.

    ATOMIC: the image is written to a temp file in the SAME directory, then moved into place with
    os.replace() (atomic on both POSIX and Windows) — so a crash/kill mid-write can never leave a
    partial init_boot.img sitting at the real path. See _replace_retry() for the Windows-only case where
    a concurrent writer holds dest open. meta.json is written only AFTER the image lands, so a reader
    can never observe a meta.json whose image write hasn't finished.

    The temp filename includes a per-call uuid4 (not just the PID): root_all/seal_all fan out across
    devices with a ThreadPoolExecutor in ONE process, so several threads rooting same-build units can
    race into put() concurrently for the same fingerprint (same slug => same dir) at the same time —
    a PID-only temp name would let their write_bytes() calls (GIL released during multi-MB I/O)
    interleave into a SHARED temp file, corrupting it before either os.replace(). A unique-per-call temp
    means every thread writes its own complete file; since same-build content is identical, whichever
    os.replace() wins still leaves a complete, valid image at dest."""
    d = _dir(store_root, fingerprint)
    dest = d / "init_boot.img"
    if get(store_root, fingerprint) is not None:
        return dest
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".init_boot.img.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(pathlib.Path(img_path).read_bytes())
        landed = _replace_retry(tmp, dest)
    except Exception:
        _unlink_quietly(tmp)
        raise
    if not landed:
        # A concurrent same-build put() kept a handle on dest through every retry (Windows only). Same
        # build => byte-identical image, so the winner's file IS the one we were about to write: a
        # populated dest is success. Only a still-absent dest is a real failure.
        _unlink_quietly(tmp)
        if not dest.is_file():
            raise OSError(f"could not move the captured init_boot into place: {dest}")
    _write_meta(d, meta)
    return dest
