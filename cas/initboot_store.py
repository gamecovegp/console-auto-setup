"""Per-build store of each unit's OWN factory init_boot, captured at root time and restored at seal.

Pure module: no adb/config/firmware imports. Callers pass `store_root` (a Path), so it stays trivially
unit-testable and free of import cycles. Layout: <store_root>/<slug(fingerprint)>/init_boot.img + meta.json
"""
import json
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


def _dir(store_root, fingerprint):
    return pathlib.Path(store_root) / slug(fingerprint)


def has(store_root, fingerprint):
    return (_dir(store_root, fingerprint) / "init_boot.img").is_file()


def get(store_root, fingerprint):
    p = _dir(store_root, fingerprint) / "init_boot.img"
    return p if p.is_file() else None


def put(store_root, fingerprint, img_path, meta):
    """Store `img_path` as this build's factory init_boot. Idempotent: if one already exists for this
    build, keep it (first clean capture wins) and return it — never overwrite a good capture."""
    d = _dir(store_root, fingerprint)
    dest = d / "init_boot.img"
    if dest.is_file():
        return dest
    d.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(pathlib.Path(img_path).read_bytes())
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dest
