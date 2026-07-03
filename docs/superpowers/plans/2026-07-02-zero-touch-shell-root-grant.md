# Zero-touch shell superuser grant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `root()` obtain and permanently persist the MagiskSU shell grant with zero human taps, so a fresh unit is fully rooted end-to-end.

**Architecture:** After the Magisk-patched `init_boot` is flashed and booted, `root()` raises the on-device Magisk Superuser prompt (device-side-backgrounded `su`, so the PC never blocks), auto-taps "Grant" via a Python-ported uiautomator helper, confirms root, then runs a bundled root script that writes `shell uid 2000 = allow` into `magisk.db` (permanent) and sets global root access. On failure it falls back to today's manual-toggle instruction.

**Tech Stack:** Python 3 (stdlib only — `re`, `time`, `concurrent.futures` already used), `adb`, MagiskSU (`/debug_ramdisk/su`), `magisk --sqlite`, `uiautomator`. Tests use the existing `FakeRunner` (no device).

## Global Constraints

- Magisk version on these units is **30.7**; builds are **`user`** (so `adb root` is unavailable and the policy DB is root-owned — the first grant MUST come through the MagiskSU prompt).
- MagiskSU binary path is **`/debug_ramdisk/su`**, exposed as `adb.su(cmd, timeout=…)` (`cas/adb.py:10,180`). Never call plain `su`.
- Device-side scripts ship **inside the bundle** under `provision/root/` and are pushed to `/data/local/tmp/…`; success is signalled by a **stdout sentinel**, never the exit code (exit codes are unreliable across these units — same pattern as `boot_patch.sh`'s `CAS_PATCH_OK`).
- Magisk policy encoding: `policies` table columns `(uid, policy, until, logging, notification)`; **`policy=2` = allow**, `uid=2000` = adb shell. `settings` key `root_access=3` = apps + adb.
- Shell-policy row is the **load-bearing** guarantee; global auto-allow is best-effort convenience.
- Tests: headless, via `FakeRunner` injected as `Adb(runner=…)`. On this machine `unittest discover` breaks (py3.14 + a `[07]` path in the tree), so run via MODULE PATHS: `python3 -m unittest tests.test_uiauto tests.test_cas tests.test_firmware tests.test_warnings`. Baseline before this feature: **265 tests, OK**. Do not add new third-party deps.
- `is_root()` uses a 30 s timeout because a fresh-unit `su` blocks on the prompt; inside the grant flow use short (8 s) root re-checks to keep retries fast.

---

### Task 1: `cas/uiauto.py` — uiautomator dump→find→tap (self-contained)

Port the minimal primitives from `scripts/uiauto.sh` into the Python package so the packaged exe needs no external shell script. Parsing is a pure function → unit-testable with a captured XML string, no device.

**Files:**
- Create: `cas/uiauto.py`
- Test: `tests/test_uiauto.py`

**Interfaces:**
- Produces:
  - `find_control(xml: str, pattern: str) -> tuple[int,int] | None` — center `(cx, cy)` of the first node whose `text`/`content-desc` matches `pattern` (regex, case-insensitive), else `None`.
  - `dump(adb) -> str` — current-screen uiautomator XML (`""` on failure).
  - `has(adb, pattern: str) -> bool`.
  - `tap(adb, pattern: str) -> bool` — taps the first match's center; `True` if one was found.
  - `foreground(adb) -> str` — top resumed activity string (for gating taps to the right app).
- Consumes: an `adb` object exposing `.shell(cmd) -> (rc, out, err)` (both `Adb` and `FakeRunner`-backed `Adb`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_uiauto.py
import os, sys, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from cas import uiauto

MAGISK_PROMPT_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<hierarchy rotation=\"0\">"
    "<node index=\"0\" text=\"Superuser Request\" bounds=\"[0,100][1080,220]\" />"
    "<node index=\"1\" text=\"shell\" content-desc=\"\" bounds=\"[40,240][1040,360]\" />"
    "<node index=\"2\" text=\"Deny\" bounds=\"[0,900][540,1010]\" />"
    "<node index=\"3\" text=\"Grant\" bounds=\"[540,900][1080,1010]\" />"
    "</hierarchy>")


class FindControl(unittest.TestCase):
    def test_finds_grant_button_center(self):
        self.assertEqual(uiauto.find_control(MAGISK_PROMPT_XML, r"grant"), (810, 955))

    def test_case_insensitive(self):
        self.assertEqual(uiauto.find_control(MAGISK_PROMPT_XML, r"GRANT"), (810, 955))

    def test_no_match_returns_none(self):
        self.assertIsNone(uiauto.find_control(MAGISK_PROMPT_XML, r"nonexistent"))

    def test_empty_xml_returns_none(self):
        self.assertIsNone(uiauto.find_control("", r"grant"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_uiauto -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cas.uiauto'`.

- [ ] **Step 3: Write the implementation**

```python
# cas/uiauto.py
"""Minimal uiautomator dump→find→tap, ported from scripts/uiauto.sh so the packaged exe needs no
external shell script. Controls are located by text/content-desc and tapped at their exact bounds
center (rotation-independent — no pixel guessing)."""
import re

_NODE = re.compile(
    r'<node[^>]*?(?:text|content-desc)="([^"]*)"[^>]*?'
    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')


def find_control(xml, pattern):
    """Center (cx, cy) of the first node whose text/content-desc matches `pattern` (regex, case-
    insensitive), or None. Pure function over a uiautomator XML dump."""
    rx = re.compile(pattern, re.I)
    for m in _NODE.finditer(xml or ""):
        label = m.group(1)
        a, b, c, d = (int(g) for g in m.groups()[1:])
        if label.strip() and rx.search(label):
            return (a + c) // 2, (b + d) // 2
    return None


def dump(adb):
    """uiautomator XML of the current screen ('' on failure)."""
    adb.shell("uiautomator dump /sdcard/cas_ui.xml")
    return adb.shell("cat /sdcard/cas_ui.xml")[1]


def has(adb, pattern):
    return find_control(dump(adb), pattern) is not None


def tap(adb, pattern):
    """Tap the first control matching `pattern`. True if one was found and tapped."""
    xy = find_control(dump(adb), pattern)
    if xy is None:
        return False
    adb.shell(f"input tap {xy[0]} {xy[1]}")
    return True


def foreground(adb):
    """Top resumed activity string, e.g. 'com.topjohnwu.magisk/.core.su.SuRequestActivity'."""
    return adb.shell("dumpsys activity activities | grep -m1 topResumedActivity")[1].strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_uiauto -v`
Expected: OK (4 tests). Note `(810, 955)`: cx=(540+1080)//2=810, cy=(900+1010)//2=955.

- [ ] **Step 5: Commit**

```bash
git add cas/uiauto.py tests/test_uiauto.py
git commit -m "feat(uiauto): self-contained uiautomator find/tap helper for cas package"
```

---

### Task 2: Grant + persist — `grant-persist.sh`, `_persist_grant`, `grant_shell_root`

The device-side script (permanent policy write) and the two provision functions that drive the zero-touch grant.

**Files:**
- Create: `provision/root/grant-persist.sh`
- Create: `tests/test_grant_persist.sh`
- Modify: `cas/provision.py` (add constants near `MAGISK_PATCH`/`DEV_PATCH` at `:509`; add the two functions immediately before `def seal(` at `:905`)
- Modify: `tests/test_cas.py` (add `GrantRunner` + a `GrantShellRoot` TestCase)

**Interfaces:**
- Consumes: `cas.uiauto.tap/foreground` (Task 1); `adb.su`, `adb.shell`, `adb.push`; `from .adb import SU`.
- Produces:
  - `_persist_grant(adb, log=print) -> bool` — pushes + runs `grant-persist.sh` as root; `True` if the shell-policy read-back is `policy=2`.
  - `grant_shell_root(adb, log=print, attempts=3, ui_timeout=15) -> bool` — obtains + persists the shell grant zero-touch; `True` once the shell holds root.
  - Module constants `GRANT_PERSIST`, `DEV_GRANT`, `GRANT_PROMPT_BTN`, `MAGISK_PKG`.

- [ ] **Step 1: Write the device-side script**

```sh
# provision/root/grant-persist.sh
#!/system/bin/sh
# grant-persist.sh — run AS ROOT (via su) right after the FIRST shell grant. Makes the MagiskSU
# shell grant PERMANENT and sets Magisk's global root access, so no unit ever re-prompts.
# Exit codes are unreliable on these units, so success is a stdout sentinel (like boot_patch.sh).
#
# shell uid 2000 = ALLOW (policy 2), forever (until 0), no logging/notification. All-numeric
# VALUES -> no inner quoting to fight through adb/su.
magisk --sqlite "REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)"
# Global: apps AND adb may hold root (root_access 3). Best-effort; the policy row above is the
# load-bearing guarantee for the adb shell.
magisk --sqlite "REPLACE INTO settings (key,value) VALUES('root_access',3)"
# Read the shell policy back so the PC can confirm it stuck; emit the sentinel + the read-back.
echo "CAS_GRANT $(magisk --sqlite "SELECT policy FROM policies WHERE uid=2000")"
```

- [ ] **Step 2: Write the failing bash test for the script**

```bash
# tests/test_grant_persist.sh
#!/usr/bin/env bash
# Runs grant-persist.sh against a STUB `magisk` and asserts it issues the right --sqlite writes
# and emits the sentinel. No device.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/../provision/root/grant-persist.sh"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
# stub magisk: log every arg line, and answer the SELECT read-back with policy=2
cat > "$tmp/magisk" <<'STUB'
#!/usr/bin/env bash
echo "$@" >> "$MAGISK_LOG"
case "$*" in
  *"SELECT policy FROM policies WHERE uid=2000"*) echo "policy=2" ;;
esac
STUB
chmod +x "$tmp/magisk"
export MAGISK_LOG="$tmp/log"; : > "$MAGISK_LOG"
out="$(PATH="$tmp:$PATH" sh "$script")"
fail=0
grep -q "REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)" "$MAGISK_LOG" \
  || { echo "[X] missing shell allow-policy write"; fail=1; }
grep -q "REPLACE INTO settings (key,value) VALUES('root_access',3)" "$MAGISK_LOG" \
  || { echo "[X] missing root_access=3 write"; fail=1; }
echo "$out" | grep -q "CAS_GRANT policy=2" \
  || { echo "[X] missing/incorrect CAS_GRANT sentinel: $out"; fail=1; }
[ "$fail" = 0 ] && echo "ok: grant-persist.sh" || exit 1
```

- [ ] **Step 3: Run the bash test to verify it fails**

Run: `chmod +x provision/root/grant-persist.sh tests/test_grant_persist.sh; bash tests/test_grant_persist.sh`
Expected: FAIL initially only if the script is wrong; since Step 1 already wrote it, this should PASS. If Step 1 is skipped it fails with "missing shell allow-policy write". (Sanity gate for the script contents.)

- [ ] **Step 4: Add the provision.py constants**

Add after the `DEV_PATCH` line (`cas/provision.py:510`):

```python
GRANT_PERSIST = BUNDLE / "provision" / "root" / "grant-persist.sh"   # permanent shell-grant writer (root)
DEV_GRANT = "/data/local/tmp/cas_grant.sh"                           # where it lands on the device
GRANT_PROMPT_BTN = r"grant"          # MagiskSU su-request "Grant" button (matched case-insensitively)
MAGISK_PKG = "com.topjohnwu.magisk"  # gate taps to the Magisk prompt so we never mis-tap another app
```

- [ ] **Step 5: Write the failing Python test**

Add to `tests/test_cas.py` (after the existing `FakeRunner`, and reusing it):

```python
GRANT_XML = (
    "<hierarchy rotation=\"0\">"
    "<node text=\"Superuser Request\" bounds=\"[0,100][1080,220]\" />"
    "<node text=\"Deny\" bounds=\"[0,900][540,1010]\" />"
    "<node text=\"Grant\" bounds=\"[540,900][1080,1010]\" />"
    "</hierarchy>")


class GrantRunner(FakeRunner):
    """Models the causal chain: raising the prompt shows a Magisk 'Grant' dialog; an `input tap`
    grants root; thereafter `su id` reports uid=0. `never_grants=True` models a prompt that never
    resolves (auto-tap fails -> manual fallback)."""

    def __init__(self, never_grants=False, **kw):
        super().__init__(root=False, su_blocked=False, **kw)
        self.granted = False
        self.never_grants = never_grants

    def __call__(self, args, input_text=None, timeout=900):
        self.calls.append(list(args))
        if "shell" in args:
            tail = args[-1]
            if tail.startswith("uiautomator dump"):
                return 0, "", ""
            if tail.startswith("cat /sdcard/cas_ui.xml"):
                return 0, GRANT_XML, ""
            if "topResumedActivity" in tail:
                return 0, "  topResumedActivity: ActivityRecord{u0 com.topjohnwu.magisk/.SuRequestActivity}\n", ""
            if tail.startswith("input tap"):
                if not self.never_grants:
                    self.granted = True
                return 0, "", ""
            if "/debug_ramdisk/su" in args:
                cmd = args[-1]
                if cmd == "id":
                    return (0, "uid=0(root)\n", "") if self.granted else (1, "", "Permission denied")
                if cmd.startswith("sh /data/local/tmp/cas_grant.sh"):
                    return 0, "CAS_GRANT policy=2\n", ""
        # Everything else — the prompt-raise `su -c id …&` (SU is embedded in the cmd string, so it is
        # NOT a standalone arg and does not enter the su block above), `rm -f`, `boot_patch.sh`,
        # `getprop`, `wait-for-device` — falls through to FakeRunner, whose shell catch-all returns
        # (0, "", "").
        return super().__call__(args, input_text, timeout)


class GrantShellRoot(unittest.TestCase):
    def _adb(self, runner):
        return Adb("ABC123", runner=runner)

    def test_zero_touch_grant_succeeds_and_persists(self):
        r = GrantRunner()
        ok = PV.grant_shell_root(self._adb(r), log=lambda *_: None, ui_timeout=3)
        self.assertTrue(ok)
        # the permanent-policy script was pushed and run as root
        self.assertTrue(any("push" in c and PV.DEV_GRANT in c for c in r.calls))
        self.assertTrue(any("/debug_ramdisk/su" in c and c[-1].startswith("sh " + PV.DEV_GRANT)
                            for c in r.calls))

    def test_failed_autotap_falls_back(self):
        logs = []
        r = GrantRunner(never_grants=True)
        ok = PV.grant_shell_root(self._adb(r), log=logs.append, attempts=2, ui_timeout=1)
        self.assertFalse(ok)
        self.assertTrue(any("open Magisk" in m for m in logs))   # manual fallback surfaced
```

- [ ] **Step 6: Run the Python test to verify it fails**

Run: `python3 -m unittest tests.test_cas.GrantShellRoot -v`
Expected: FAIL — `AttributeError: module 'cas.provision' has no attribute 'grant_shell_root'`.

- [ ] **Step 7: Implement the two functions**

Insert immediately before `def seal(` (`cas/provision.py:905`):

```python
def _persist_grant(adb, log=print):
    """Make the just-obtained shell grant permanent (magisk policy) + set global root access, by
    running the bundled grant-persist.sh AS ROOT. Returns True if the shell policy read-back = allow.
    A False here means the shell is rooted NOW but may re-prompt after a reboot — not fatal."""
    if not adb.push(str(GRANT_PERSIST), DEV_GRANT):
        log("  ⚠ could not push grant-persist.sh — shell is rooted now but may re-prompt after reboot.")
        return False
    rc, out, err = adb.su(f"sh {DEV_GRANT}", timeout=30)
    adb.shell(f"rm -f {DEV_GRANT}")
    if "policy=2" in out:
        log("  ✓ shell root made permanent (magisk policy: shell uid 2000 = allow).")
        return True
    log(f"  ⚠ persistence unconfirmed (rc={rc}): {((out or err) or '').strip()[:160]} — shell is "
        "rooted now but may re-prompt after reboot.")
    return False


def grant_shell_root(adb, log=print, attempts=3, ui_timeout=15):
    """Zero-touch: obtain + persist the MagiskSU shell grant with no human tap. Raises the on-device
    Magisk Superuser prompt (device-side-backgrounded `su`, so the PC never blocks), auto-taps
    'Grant' via uiautomator (gated to the Magisk app so we never mis-tap), confirms root with a short
    re-check, then makes it permanent. Returns True once the shell holds root; on failure logs the
    one-time manual instruction and returns False."""
    from . import uiauto
    from .adb import SU
    if "uid=0" in adb.su("id", timeout=8)[1]:        # already granted (e.g. a remembered policy)
        _persist_grant(adb, log)
        return True
    for i in range(attempts):
        log(f"  auto-grant {i + 1}/{attempts}: raising the Magisk Superuser prompt…")
        adb.shell(f"{SU} -c id >/dev/null 2>&1 &")    # device-side background: returns immediately
        tapped = False
        for _ in range(ui_timeout):
            if MAGISK_PKG in uiauto.foreground(adb) and uiauto.tap(adb, GRANT_PROMPT_BTN):
                tapped = True
                break
            time.sleep(1)
        if tapped and "uid=0" in adb.su("id", timeout=8)[1]:
            log("  ✓ shell auto-granted.")
            _persist_grant(adb, log)
            return True
        log("  prompt not answered yet — retrying." if i + 1 < attempts else "  auto-grant failed.")
    log("init_boot flashed + Magisk installed, but the shell uid could NOT be auto-granted. One-time "
        "per unit: on the device open Magisk → Superuser → enable the 'Shell' / '[SharedUID] Shell' "
        "toggle, then retry. (MagiskSU gates the shell uid until you allow it.)")
    return False
```

- [ ] **Step 8: Run both tests to verify they pass**

Run: `python3 -m unittest tests.test_cas.GrantShellRoot -v && bash tests/test_grant_persist.sh`
Expected: OK (2 tests; then `ok: grant-persist.sh`).

- [ ] **Step 9: Commit**

```bash
git add provision/root/grant-persist.sh tests/test_grant_persist.sh cas/provision.py tests/test_cas.py
git commit -m "feat(provision): zero-touch MagiskSU shell grant + permanent policy"
```

---

### Task 3: Wire into `root()` + `auto_grant_shell` config toggle

Replace the manual-instruction dead-end at the end of `root()` with the auto-grant path, gated by a config toggle that defaults on.

**Files:**
- Modify: `cas/config.py` (add accessor after `es_media_src`/`set_es_media_src`, ~`:151`)
- Modify: `cas/provision.py:896-902` (the `root()` step-4 tail)
- Modify: `tests/test_cas.py` (extend `GrantShellRoot` with two `root()`-level tests)

**Interfaces:**
- Consumes: `grant_shell_root` (Task 2); `config.auto_grant_shell` (this task).
- Produces: `config.auto_grant_shell() -> bool` (default `True`).

- [ ] **Step 1: Write the failing test**

Add to the `GrantShellRoot` TestCase in `tests/test_cas.py`:

```python
    def test_config_toggle_default_on(self):
        from cas import config
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            os.environ["CAS_CONFIG"] = os.path.join(d, "cas-config.json")  # no file -> default
            try:
                self.assertTrue(config.auto_grant_shell())
            finally:
                del os.environ["CAS_CONFIG"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cas.GrantShellRoot.test_config_toggle_default_on -v`
Expected: FAIL — `AttributeError: module 'cas.config' has no attribute 'auto_grant_shell'`.

- [ ] **Step 3: Add the config accessor**

Insert in `cas/config.py` (after `set_es_media_src`, ~`:165`):

```python
def auto_grant_shell():
    """Whether root() auto-grants + persists the MagiskSU shell grant with no human tap (default
    True). Set "auto_grant_shell": false in cas-config.json to fall back to the manual Magisk
    Superuser toggle."""
    return bool(load_config().get("auto_grant_shell", True))
```

- [ ] **Step 4: Rewire root()'s step-4 tail**

Replace `cas/provision.py:896-902` (the `if adb.is_root(): …` / manual-message block) with:

```python
    if adb.is_root():
        log("✓ ROOTED — adb shell su works. Ready to '② Download to selected device'.")
        return True
    from . import config as _cfg
    if _cfg.auto_grant_shell():
        log("shell not granted yet — auto-granting via the on-device Magisk prompt (zero-touch)…")
        if grant_shell_root(adb, log=log):
            log("✓ ROOTED — shell auto-granted and made permanent. Ready to '② Download'.")
            return True
        return False                       # grant_shell_root already logged the manual fallback
    log("init_boot flashed + Magisk installed, but the adb shell uid isn't granted root YET. One-time "
        "per unit: on the device open Magisk → Superuser → enable the 'Shell' / '[SharedUID] Shell' "
        "toggle, then retry. (MagiskSU gates the shell uid until you allow it.)")
    return False
```

- [ ] **Step 5: Write the `root()` end-to-end test**

`root()` reaches the new tail only with `wait=True` (a `wait=False` call returns at `provision.py:894`). `FakeRunner` already drives the whole flow headlessly: `boot_patch.sh` returns `CAS_PATCH_OK`, `sys.boot_completed` is `"1"` so `wait_boot()` returns immediately, and `GrantRunner` grants on the auto-tap. Pass a stub `flasher` so no real fastboot runs. Add to `GrantShellRoot`:

```python
    def test_root_autogrants_when_booted_but_ungranted(self):
        import tempfile, pathlib
        ra, fb = GrantRunner(), FbRunner()
        with tempfile.TemporaryDirectory() as d:
            stock = pathlib.Path(d) / "init_boot.img"
            stock.write_bytes(b"x")                       # PC stock image must exist
            os.environ["CAS_CONFIG"] = str(pathlib.Path(d) / "absent.json")  # missing -> default toggle on
            try:
                ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), stock, magisk_apk=None,
                             log=lambda *_: None, wait=True,
                             flasher=lambda adb, target, img, log: True)
            finally:
                os.environ.pop("CAS_CONFIG", None)
        self.assertTrue(ok)                               # root() returns True via the auto-grant tail
        self.assertTrue(ra.granted)                       # the auto-tap path actually ran
```

- [ ] **Step 6: Run the full suite to verify pass + no regressions**

Run: `python3 -m unittest tests.test_cas tests.test_firmware tests.test_warnings && for t in tests/test_*.sh; do bash "$t" || exit 1; done`
Expected: `OK` (265 baseline + the new tests) and every shell test prints `ok:` / `PASS`.

- [ ] **Step 7: Commit**

```bash
git add cas/config.py cas/provision.py tests/test_cas.py
git commit -m "feat(root): auto-grant shell root in root(), gated by auto_grant_shell (default on)"
```

---

### Task 4: On-device bench gate (verification checklist — no code)

The headless tests cover parsing, control flow, and the persistence contract. These facts can only be confirmed on real hardware; record the results in the branch's PR/notes. If any check fails, open a follow-up rather than silently shipping.

- [ ] **Step 1: Prompt is dumpable + label is "Grant"**
On a freshly-rooted-but-ungranted bench unit, run `SERIAL=<s> ./scripts/uiauto.sh fg` while a `su` prompt is up and confirm the foreground is `com.topjohnwu.magisk/…SuRequestActivity`; run `SERIAL=<s> ./scripts/uiauto.sh list` and confirm a control labelled `Grant` (exact case/label) is present. If the label differs (localisation/theme), widen `GRANT_PROMPT_BTN` (e.g. `grant|allow`) and re-run Task 2/3 tests.

- [ ] **Step 2: End-to-end zero-touch**
Wipe/re-flash a fresh unit, run `⓪ Root` (or the CLI root path) and confirm it reaches `✓ ROOTED — shell auto-granted and made permanent` with **no human tap**.

- [ ] **Step 3: Persistence across reboot**
`adb reboot`; after boot run `adb shell /debug_ramdisk/su -c id` and confirm `uid=0` with **no prompt**. Confirm `magisk --sqlite "SELECT policy FROM policies WHERE uid=2000"` returns `policy=2`.

- [ ] **Step 4: Global auto-allow (best-effort)**
Read the live Magisk prefs to determine 30.7's automatic-response key: `adb shell su -c 'cat /data/data/com.topjohnwu.magisk/shared_prefs/*.xml'`. Confirm whether `root_access=3` alone gives the desired "no prompt for provisioning apps" behaviour; if a per-app prompt still appears for a shipped app that needs root, capture the exact prefs key/value and file a follow-up to set it in `grant-persist.sh`. (Retail units ship un-rooted at seal, so this is convenience, not a blocker.)

- [ ] **Step 5: Toggle**
Set `"auto_grant_shell": false` in `cas-config.json`, re-run root on an ungranted unit, and confirm it prints the manual-toggle instruction instead of auto-tapping.

---

## Self-Review

**Spec coverage:**
- Zero-touch first grant (auto-tap prompt) → Task 2 `grant_shell_root` + Task 4 Step 2. ✓
- Shell uid 2000 = allow, permanent → Task 2 `grant-persist.sh` + `_persist_grant`; verified Task 4 Step 3. ✓
- Global auto-allow → `grant-persist.sh` `root_access=3`; app auto-response deferred to Task 4 Step 4 (spec marks it best-effort). ✓
- Self-contained uiautomator helper (not shell script) → Task 1. ✓
- Wire into `root()` step 4, replacing manual dead-end → Task 3. ✓
- Bounded retries → manual fallback → Task 2 `grant_shell_root` loop + `test_failed_autotap_falls_back`. ✓
- `seal()` unchanged → not touched by any task (documented in spec §5). ✓
- `auto_grant_shell` config toggle, default on → Task 3. ✓
- Model-independence (text-based tap) → Task 1 `find_control`; confirmed Task 4 Step 1. ✓

**Placeholder scan:** No "TBD/TODO"; the only deferred item (30.7 auto-response prefs key) is an explicit on-device discovery step (Task 4 Step 4), consistent with the spec's best-effort framing, and the shipped code's guarantee (shell policy row + `root_access=3`) is fully specified.

**Type consistency:** `find_control(xml, pattern)→(cx,cy)|None`, `tap/has(adb, pattern)→bool`, `foreground(adb)→str`, `grant_shell_root(adb, log, attempts, ui_timeout)→bool`, `_persist_grant(adb, log)→bool`, `auto_grant_shell()→bool` — names/signatures match across Tasks 1–3 and the tests. Constants `DEV_GRANT`, `GRANT_PROMPT_BTN`, `MAGISK_PKG`, `GRANT_PERSIST` defined in Task 2 and referenced consistently.
