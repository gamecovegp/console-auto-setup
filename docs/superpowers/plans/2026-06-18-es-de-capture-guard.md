# ES-DE Capture Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop CAS from silently shipping an SD-tier whose ES-DE home failed to capture, by guarding the capture at two layers (PC-side authoritative gate + device-side visibility).

**Architecture:** ES-DE home now lives on internal storage (`/storage/emulated/0/ES-DE`) and is carried by the existing capture→restore pipeline as `internal_ES-DE.tar`. The only failure this plan closes: a golden captured while ES-DE is still on the SD produces NO `internal_ES-DE.tar` (capture skips empty internal dirs, silently), so every unit from that profile ships with no ES-DE. We add (1) a pure `_verify_capture()` gate in `cas/provision.py` that blocks the good-profile overwrite when ES-DE is captured without its tar, and (2) a warn/`CFAIL` check in `provision/root/capture.sh`.

**Tech Stack:** Python 3 (stdlib `unittest`, no third-party test deps), POSIX `sh` (device-side, toybox). adb is mocked in tests via `FakeRunner`.

## Global Constraints

- **Test runner is `unittest`, not pytest.** Run the suite from the project root: `python3 -m unittest discover -s tests -p 'test_*.py' -t .`. Filter to a class with `-k`, e.g. `... -t . -k CaptureGate -v`.
- **No third-party imports** in `cas/` or tests — stdlib only (matches the existing code).
- **This project is NOT a git repo.** Commit steps below assume you ran `git init` once first; if you are not using git, skip the `git commit` steps (they are the only thing that needs git).
- **Device-side scripts are POSIX `sh` / toybox** — no bashisms. `[ -s FILE ]` = exists-and-non-empty. `internal_for` is provided by `provision/root/lib-root.sh` (already sourced by `capture.sh`).
- **ES-DE is the only hard failure.** Citra (`org.citra.emu`) and RetroArch (`com.retroarch.aarch64`) internal dirs may legitimately be empty → warn only, never fail.
- The canonical ES-DE values: package `org.es_de.frontend`, internal dir `ES-DE`, tar filename `internal_ES-DE.tar`.

---

### Task 1: PC-side capture gate (`_verify_capture`)

The authoritative gate. `capture_to_pc` already verifies `global.meta` + `pkglist.txt` inline before rotating the freshly-pulled payload over the good one (`cas/provision.py:161-165`). Extract that into a pure function and extend it with the ES-DE rule so a bad capture can never overwrite a good profile.

**Files:**
- Modify: `cas/provision.py` (add `_verify_capture` after `_validate_payload`, ~line 38; replace the inline check at `cas/provision.py:161-165`)
- Test: `tests/test_cas.py` (append a `TestCaptureGate` class)

**Interfaces:**
- Produces: `_verify_capture(incoming, log) -> bool` — `incoming` is a `pathlib.Path` to a pulled payload dir; `log` is a `callable(str)`. Returns `True` if the payload is complete enough to trust, `False` otherwise (and logs why). Does NOT delete anything — the caller owns cleanup.
- Consumes: nothing new (uses `pathlib`, already imported).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cas.py` (the module already does `sys.path.insert` and imports `provision as PV`, `pathlib`, `tempfile`, `unittest`):

```python
class TestCaptureGate(unittest.TestCase):
    def _incoming(self, t, pkgs, es_de_tar=b"data"):
        """Build a minimal pulled-payload dir. es_de_tar=None -> no internal_ES-DE.tar;
        b"" -> present but empty; bytes -> present and non-empty."""
        inc = pathlib.Path(t) / ".incoming"
        inc.mkdir(parents=True)
        (inc / "global.meta").write_text("golden_serial=9C33-6BBD\n")
        (inc / "pkglist.txt").write_text("\n".join(pkgs) + "\n")
        if es_de_tar is not None:
            (inc / "internal_ES-DE.tar").write_bytes(es_de_tar)
        return inc

    def test_accepts_es_de_with_nonempty_tar(self):
        with tempfile.TemporaryDirectory() as t:
            inc = self._incoming(t, ["org.es_de.frontend", "org.citra.emu"])
            self.assertTrue(PV._verify_capture(inc, lambda m: None))

    def test_rejects_es_de_without_tar(self):
        with tempfile.TemporaryDirectory() as t:
            inc = self._incoming(t, ["org.es_de.frontend"], es_de_tar=None)
            self.assertFalse(PV._verify_capture(inc, lambda m: None))

    def test_rejects_es_de_with_empty_tar(self):
        with tempfile.TemporaryDirectory() as t:
            inc = self._incoming(t, ["org.es_de.frontend"], es_de_tar=b"")
            self.assertFalse(PV._verify_capture(inc, lambda m: None))

    def test_accepts_capture_without_es_de(self):
        with tempfile.TemporaryDirectory() as t:
            inc = self._incoming(t, ["com.retroarch.aarch64"], es_de_tar=None)
            self.assertTrue(PV._verify_capture(inc, lambda m: None))

    def test_rejects_missing_pkglist(self):
        with tempfile.TemporaryDirectory() as t:
            inc = self._incoming(t, ["org.es_de.frontend"])
            (inc / "pkglist.txt").unlink()
            self.assertFalse(PV._verify_capture(inc, lambda m: None))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -t . -k CaptureGate -v`
Expected: FAIL — `AttributeError: module 'cas.provision' has no attribute '_verify_capture'`.

- [ ] **Step 3: Add `_verify_capture` to `cas/provision.py`**

Insert immediately AFTER the `_validate_payload` function (i.e. just before `def provision(`, around line 38):

```python
def _verify_capture(incoming, log):
    """Gate a freshly-pulled payload BEFORE it overwrites the good profile. Returns True if OK.
    ES-DE is special: its home now lives on INTERNAL storage, so a capture that lists
    org.es_de.frontend but carries no internal_ES-DE.tar means the home was still on the SD —
    provisioning from it would ship a tier with NO ES-DE. That is a hard failure here."""
    if not (incoming / "global.meta").exists() or not (incoming / "pkglist.txt").exists():
        log("pulled payload incomplete (no global.meta/pkglist) — existing profile untouched.")
        return False
    pkgs = [l.strip() for l in (incoming / "pkglist.txt").read_text().splitlines() if l.strip()]
    if "org.es_de.frontend" in pkgs:
        tar = incoming / "internal_ES-DE.tar"
        if not tar.exists() or tar.stat().st_size == 0:
            log("ES-DE is in the capture but internal_ES-DE.tar is missing/empty — its home was "
                "not on internal storage (/storage/emulated/0/ES-DE) at capture time. Move it to "
                "internal and re-capture. Existing profile untouched.")
            return False
    return True
```

- [ ] **Step 4: Wire it into `capture_to_pc`**

Replace the inline completeness check at `cas/provision.py:161-165`:

```python
        # verify the pulled payload is complete BEFORE we touch the good one
        if not (incoming / "global.meta").exists() or not (incoming / "pkglist.txt").exists():
            log("pulled payload incomplete (no global.meta/pkglist) — existing profile untouched.")
            shutil.rmtree(incoming, ignore_errors=True)
            return False
```

with:

```python
        # verify the pulled payload is complete BEFORE we touch the good one
        if not _verify_capture(incoming, log):
            shutil.rmtree(incoming, ignore_errors=True)
            return False
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -t . -k CaptureGate -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Run the FULL suite to confirm no regression**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -t .`
Expected: OK (all existing tests + the 5 new ones). The existing `test_capture_to_pc_invokes_capture` uses `dry_pull=True`, so it never reaches `_verify_capture` — it must still pass unchanged.

- [ ] **Step 7: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(capture): block profile overwrite when ES-DE captured without internal_ES-DE.tar"
```

---

### Task 2: Device-side capture guard (`capture.sh`)

Make the failure visible at the source, on the device, so the operator sees it the moment a golden is captured wrong — not only on the PC pull. Reuses `CFAIL` (capture already exits non-zero when `CFAIL > 0`, `provision/root/capture.sh:90-93`).

**Files:**
- Modify: `provision/root/capture.sh` (insert a check in the completeness-gate section, after the `global.meta` check at line 89, before the `if [ "$CFAIL" -gt 0 ]` block)

**Interfaces:**
- Consumes: `internal_for` (from `provision/root/lib-root.sh:12`, already sourced at `capture.sh:6`); `$P` (payload dir); `$P/pkglist.txt`; `$CFAIL` (already in scope).
- Produces: nothing new — bumps `$CFAIL` and emits `warn` lines.

- [ ] **Step 1: Add the guard loop**

In `provision/root/capture.sh`, locate the completeness gate (currently):

```sh
[ "$(grep -c . "$P/pkglist.txt")" -gt 0 ] || { warn "pkglist EMPTY — no 3rd-party apps captured"; CFAIL=$((CFAIL+1)); }
[ -s "$P/global.meta" ] || { warn "global.meta missing/empty"; CFAIL=$((CFAIL+1)); }
```

Insert this block immediately AFTER those two lines (before `if [ "$CFAIL" -gt 0 ]; then`):

```sh
# ES-DE home now lives on INTERNAL storage; if ES-DE was captured but no internal_ES-DE.tar was
# produced, its home was still on the SD — provisioning that profile would ship a tier with NO
# ES-DE. Hard-fail on ES-DE; warn-only for other internal-state apps (their dir may be empty).
for pkg in $(cat "$P/pkglist.txt" 2>/dev/null); do
  d="$(internal_for "$pkg")"; [ -n "$d" ] || continue
  [ -s "$P/internal_$d.tar" ] && continue
  if [ "$pkg" = org.es_de.frontend ]; then
    warn "ES-DE captured but internal_ES-DE.tar missing/empty — move its home to /storage/emulated/0/ES-DE then re-capture."
    CFAIL=$((CFAIL+1))
  else
    warn "internal_$d.tar missing/empty for $pkg (OK if that app keeps no internal state)."
  fi
done
```

- [ ] **Step 2: Lint the script (syntax only — no device needed)**

Run: `sh -n provision/root/capture.sh`
Expected: no output, exit 0 (valid POSIX sh).

- [ ] **Step 3: Manual behavior check with a stub harness (no device)**

There is no shell test harness in this repo, so verify the new block in isolation by sourcing the helper and exercising the loop against fixture files:

```bash
mkdir -p /tmp/casg/payload
printf 'org.es_de.frontend\ncom.retroarch.aarch64\n' > /tmp/casg/payload/pkglist.txt
# Case A: ES-DE tar MISSING -> expect a warn + CFAIL=1
sh -c '. provision/root/lib-root.sh
P=/tmp/casg/payload; CFAIL=0
for pkg in $(cat "$P/pkglist.txt"); do
  d="$(internal_for "$pkg")"; [ -n "$d" ] || continue
  [ -s "$P/internal_$d.tar" ] && continue
  if [ "$pkg" = org.es_de.frontend ]; then echo "WARN es-de"; CFAIL=$((CFAIL+1)); else echo "warn $d"; fi
done
echo "CFAIL=$CFAIL"'
```

Expected output includes `WARN es-de` and `CFAIL=1` (the RetroArch line prints `warn RetroArch` but does NOT bump CFAIL).

```bash
# Case B: ES-DE tar PRESENT + non-empty -> expect CFAIL=0
echo data > /tmp/casg/payload/internal_ES-DE.tar
sh -c '. provision/root/lib-root.sh
P=/tmp/casg/payload; CFAIL=0
for pkg in $(cat "$P/pkglist.txt"); do
  d="$(internal_for "$pkg")"; [ -n "$d" ] || continue
  [ -s "$P/internal_$d.tar" ] && continue
  if [ "$pkg" = org.es_de.frontend ]; then echo "WARN es-de"; CFAIL=$((CFAIL+1)); else echo "warn $d"; fi
done
echo "CFAIL=$CFAIL"'
rm -rf /tmp/casg
```

Expected: only `warn RetroArch`, and `CFAIL=0`.

- [ ] **Step 4: Commit**

```bash
git add provision/root/capture.sh
git commit -m "feat(capture.sh): fail golden capture when ES-DE home is not on internal storage"
```

---

## Notes (not code — operational, per the design spec)

Building each tier golden is the manual runbook in `docs/superpowers/specs/2026-06-18-es-de-per-tier-golden-design.md` §"Per-tier golden prep": insert the tier SD, install ES-DE, point its home at `/storage/emulated/0/ES-DE`, scrape to that tier's games, root, then `capture <model>-<tier>`. This guard simply makes step 5 of that runbook fail loudly if step 2 (home on internal) was skipped. No code beyond the two tasks above.

## Self-Review

- **Spec coverage:** Spec §"The one code change" (two-layer guard) → Task 1 (PC-side gate) + Task 2 (device-side). Spec §Testing (guard device-side / PC-side) → Task 1 Steps 1-6, Task 2 Steps 2-3. Spec §"ES-DE hard fail, Citra/RetroArch warn-only" → enforced device-side (Task 2 loop) and PC-side (Task 1 checks only `org.es_de.frontend`). End-to-end manual test → Notes section + spec runbook. No `restore.sh` change required → confirmed (no task touches it). ✅
- **Placeholder scan:** none — every step has concrete code/commands and expected output. ✅
- **Type consistency:** `_verify_capture(incoming, log) -> bool` used identically in Task 1 Step 3 (definition), Step 4 (call site), and all tests. Tar/package/dir names (`org.es_de.frontend`, `internal_ES-DE.tar`, `ES-DE`) consistent across both tasks and match `lib-root.sh`. ✅
