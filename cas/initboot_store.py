"""Per-build store of each unit's OWN factory init_boot, captured at root time and restored at seal.

Pure module: no adb/config/firmware imports. Callers pass `store_root` (a Path), so it stays trivially
unit-testable and free of import cycles. Layout: <store_root>/<slug(fingerprint)>/init_boot.img + meta.json
"""
import json
import os
import pathlib
import re

_MAGISK_MARKERS = (b"MAGISKINIT", b"MAGISKPOLICY", b".magisk")


def looks_like_boot_image(data):
    """True iff `data` is an Android boot image (magic 'ANDROID!' at offset 0). A zeroed/empty inactive
    slot fails this — the reason we never store an unpopulated single-slot-flashed unit's dump."""
    return len(data) >= 8 and data[:8] == b"ANDROID!"


def contains_magisk(data):
    """True iff the image carries Magisk markers (i.e. it's a patched/rooted image, not factory)."""
    return any(m in data for m in _MAGISK_MARKERS)


def slug(fingerprint):
    """Filesystem-safe key from a build fingerprint (keeps alnum and dots, collapses the rest)."""
    return re.sub(r"[^A-Za-z0-9.]+", "_", fingerprint or "").strip("_") or "unknown"


def store_root(firmware_root):
    """The per-build init_boot store dir, sited alongside the firmware library: <firmware_root's
    parent>/_init_boot_factory. ONE helper so cli.py / root_all / seal_all can't drift on the layout."""
    return pathlib.Path(firmware_root).parent / "_init_boot_factory"


def _dir(store_root, fingerprint):
    return pathlib.Path(store_root) / slug(fingerprint)


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
    partial init_boot.img sitting at the real path. meta.json is written only AFTER the image lands, so
    a reader can never observe a meta.json whose image write hasn't finished."""
    d = _dir(store_root, fingerprint)
    dest = d / "init_boot.img"
    if get(store_root, fingerprint) is not None:
        return dest
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".init_boot.img.{os.getpid()}.tmp"
    try:
        tmp.write_bytes(pathlib.Path(img_path).read_bytes())
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dest
