"""Profile library: list / match-by-model / manifest parse+save / archive (soft-delete).

A profile is a directory under `profiles/<name>/` with:
  profile.meta            key=value (model_match, frontend, notes, captured)
  manifest                app names (one per line) + "@flag value" + "#" comments
  golden_root_payload/    the captured payload (per-app modules + internal_*.tar + grants + settings)
"""
import re
import pathlib
import shutil

# pkg -> shared internal-storage dir it owns (mirror of lib-root.sh:internal_for). Restored only if the
# app is in the manifest.
INTERNAL_FOR = {
    "org.es_de.frontend": "ES-DE",
    "org.citra.emu": "citra-emu",
    "com.retroarch.aarch64": "RetroArch",
}


def internal_for(pkg):
    return INTERNAL_FOR.get(pkg)


def _read_meta(path):
    meta = {}
    p = pathlib.Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                meta[k.strip()] = v.strip()
    return meta


def manifest_pkgs(manifest_path):
    """App names from a manifest (comments + @flag lines stripped)."""
    p = pathlib.Path(manifest_path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("@"):
            continue
        out.append(line.split()[0])
    return out


def manifest_flags(manifest_path):
    """{flag: value} from @flag lines (value defaults to 'on' if omitted)."""
    p = pathlib.Path(manifest_path)
    flags = {}
    if not p.exists():
        return flags
    for line in p.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("@"):
            parts = line[1:].split()
            if parts:
                flags[parts[0]] = parts[1] if len(parts) > 1 else "on"
    return flags


def save_manifest(manifest_path, pkgs, flags, header="# manifest"):
    lines = [header]
    lines += list(pkgs)
    lines += [f"@{k} {v}" for k, v in flags.items()]
    pathlib.Path(manifest_path).write_text("\n".join(lines) + "\n")


class Profile:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.name = self.path.name
        self.meta = _read_meta(self.path / "profile.meta")
        self.manifest_path = self.path / "manifest"
        self.payload = self.path / "golden_root_payload"

    def pkgs(self):
        return manifest_pkgs(self.manifest_path)

    def flags(self):
        return manifest_flags(self.manifest_path)

    def all_pkgs(self):
        """Every app the payload contains (from pkglist.txt) — the full toggle set for the UI."""
        pl = self.payload / "pkglist.txt"
        if pl.exists():
            return [l.strip() for l in pl.read_text().splitlines() if l.strip()]
        return self.pkgs()

    def __repr__(self):
        return f"<Profile {self.name} frontend={self.meta.get('frontend')}>"


def list_profiles(root="profiles"):
    root = pathlib.Path(root)
    if not root.exists():
        return []
    return [Profile(p) for p in sorted(root.iterdir())
            if p.is_dir() and (p / "profile.meta").exists()]


def match_profile(model, root="profiles"):
    """The profile whose model_match regex matches `model`. END-ANCHORED so a loose pattern can't
    hijack the wrong variant. Returns None if the model is blank, nothing matches, or MORE THAN ONE
    matches (ambiguous -> caller must pass an explicit profile)."""
    model = (model or "").strip()
    if not model:
        return None
    matches = []
    for prof in list_profiles(root):
        pat = prof.meta.get("model_match")
        if pat and re.search(f"(?:{pat})$", model):
            matches.append(prof)
    if len(matches) != 1:
        return None                      # 0 = no match; >1 = ambiguous, refuse to guess
    return matches[0]


def archive_profile(profile, stamp, archive_root=None):
    """Soft-delete: MOVE the profile dir to profiles/_archive/<name>_<stamp>. Never rm. Returns dest."""
    src = pathlib.Path(profile.path)
    if archive_root is None:
        archive_root = src.parent / "_archive"
    dst = pathlib.Path(archive_root) / f"{profile.name}_{stamp}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst
