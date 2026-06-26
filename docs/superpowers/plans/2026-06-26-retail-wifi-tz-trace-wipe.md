# Retail WiFi + Asia/Manila TZ + Trace-Wipe — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-join shop WiFi + pin Asia/Manila timezone during `provision`, and on `seal` forget the WiFi (radio stays ON) and wipe every test fingerprint (recents, saves/states, media/logs, Android traces) while keeping ROMs, emulators, BIOS/firmware/keys.

**Architecture:** Additive — no new pipeline stage. New `Adb.wifi_join`/`wifi_forget_all` + `apply_timezone()` hook into `provision()`; a canonical on-device `provision/retail-clean.sh` (single source of truth for the wipe) is pushed+run by a new `clean_for_retail()` at the top of `seal()` (while still rooted, before the un-root flash). All new behavior is a **no-op when its config is absent**, preserving today's offline-ship default.

**Tech Stack:** Python 3 (`cas/` package, injectable adb runner for tests), POSIX `sh` device scripts (pure sed/grep/tr — no awk/python, so they run in `rish`), `unittest` with the existing `FakeRunner` harness.

## Global Constraints

- **Test command (run from repo root):** `python3 -m unittest discover -s tests -p 'test_*.py' -t .`
- **Device scripts: pure `sed`/`grep`/`tr` only** — NO `awk`/`python` (must run in on-device `rish`/Shizuku shell). Convention from `provision/lib.sh:63`.
- **Root invocation:** root commands go through `adb.su(cmd)` → `adb shell /debug_ramdisk/su -c <cmd>` (`cas/adb.py:7,104`).
- **Best-effort wipe:** every delete tolerates a missing path (`rm -rf … 2>/dev/null`) and **must never abort `seal()`** — a clean failure cannot leave a unit half-un-rooted.
- **WiFi radio stays ON after forget** — never `svc wifi disable`.
- **Timezone default = `Asia/Manila`** (UTC+8).
- **Preserve allowlist is authoritative:** BIOS/firmware/keys, NAND *system*, `Sys/`, controller profiles, emulator settings, ROMs, ES-DE config are NEVER deleted. When a path is uncertain, it is kept.
- **No-op gating:** `provision()` runs WiFi/TZ only when `not dry_push`; WiFi join only when `wifi_config()` is non-None.
- **Config file:** `cas-config.json` (gitignored, not tracked) — safe to hold real shop WiFi creds, consistent with the existing obfuscated `nas_pw`.
- **Branch:** do this work on a new branch `feat/retail-wifi-tz-clean` (the current `fix/cas-self-update` branch already carries the design spec commit `5d7ff14`).

---

### Task 0: Branch

- [ ] **Step 1: Create the feature branch from the spec commit**

```bash
git checkout -b feat/retail-wifi-tz-clean
```

Note: the working tree has pre-existing unrelated modifications (`cas/*.py`, etc.) and an untracked `cas/gui.py.tmp.*` file. Leave them alone — do not stage or commit them in any task below. Each task's `git add` lists exact paths only.

---

### Task 1: Config accessors (`wifi_config`, `device_timezone`, `retail_clean_flags`)

**Files:**
- Modify: `cas/config.py` (append after `set_es_media_src`, ~line 99)
- Test: `tests/test_cas.py` (add methods to `class TestConfig`, ~line 266)

**Interfaces:**
- Produces:
  - `wifi_config() -> dict | None` — `{"ssid": str, "password": str, "security": str, "hidden": bool}` from config key `wifi`, or `None` when absent or `ssid` is empty. `security` defaults to `"wpa2"`, `hidden` to `False`.
  - `device_timezone() -> str` — config key `timezone`, default `"Asia/Manila"`. `CAS_TZ` env overrides.
  - `retail_clean_flags() -> dict` — `{"recents": bool, "saves": bool, "media": bool, "android_traces": bool}` from config key `retail_clean`, every flag defaulting to `True`.

- [ ] **Step 1: Write the failing tests**

Add to `class TestConfig` in `tests/test_cas.py`. These follow the existing `_with_config` pattern in that class (writes a temp `cas-config.json` and points `CAS_CONFIG` at it). If no such helper exists, use this self-contained form:

```python
    def test_wifi_config_absent_is_none(self):
        with tempfile.TemporaryDirectory() as t:
            cf = pathlib.Path(t) / "cas-config.json"
            cf.write_text("{}")
            old = os.environ.get("CAS_CONFIG")
            os.environ["CAS_CONFIG"] = str(cf)
            try:
                from cas import config as C
                self.assertIsNone(C.wifi_config())
                self.assertEqual(C.device_timezone(), "Asia/Manila")
                self.assertEqual(C.retail_clean_flags(),
                                 {"recents": True, "saves": True, "media": True, "android_traces": True})
            finally:
                os.environ.pop("CAS_CONFIG", None)
                if old is not None:
                    os.environ["CAS_CONFIG"] = old

    def test_wifi_and_clean_config_parsed(self):
        with tempfile.TemporaryDirectory() as t:
            cf = pathlib.Path(t) / "cas-config.json"
            cf.write_text(json.dumps({
                "wifi": {"ssid": "Luxium-Shop", "password": "secret", "security": "wpa3", "hidden": True},
                "timezone": "Asia/Manila",
                "retail_clean": {"saves": False},
            }))
            old = os.environ.get("CAS_CONFIG")
            os.environ["CAS_CONFIG"] = str(cf)
            try:
                from cas import config as C
                self.assertEqual(C.wifi_config(),
                                 {"ssid": "Luxium-Shop", "password": "secret",
                                  "security": "wpa3", "hidden": True})
                self.assertEqual(C.device_timezone(), "Asia/Manila")
                # a partial retail_clean dict: explicit False honored, missing keys default True
                f = C.retail_clean_flags()
                self.assertFalse(f["saves"])
                self.assertTrue(f["recents"] and f["media"] and f["android_traces"])
            finally:
                os.environ.pop("CAS_CONFIG", None)
                if old is not None:
                    os.environ["CAS_CONFIG"] = old
```

Add `import json` at the top of `tests/test_cas.py` if not already imported (it is not in the current imports — check lines 4-9 and add it).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas.TestConfig.test_wifi_config_absent_is_none tests.test_cas.TestConfig.test_wifi_and_clean_config_parsed -v`
Expected: FAIL with `AttributeError: module 'cas.config' has no attribute 'wifi_config'`.

- [ ] **Step 3: Implement the accessors**

Append to `cas/config.py` (after `set_es_media_src`, before the `--- per-device profile memory ---` block):

```python
def wifi_config():
    """Shop WiFi to auto-join during provisioning, or None to skip (ship offline, today's default).
    From cas-config.json 'wifi': {ssid, password, security?, hidden?}. Returns None if absent or no ssid.
    security defaults to 'wpa2' (one of open|wpa2|wpa3); hidden defaults to False."""
    w = load_config().get("wifi")
    if not isinstance(w, dict) or not w.get("ssid"):
        return None
    return {
        "ssid": str(w["ssid"]),
        "password": str(w.get("password") or ""),
        "security": str(w.get("security") or "wpa2"),
        "hidden": bool(w.get("hidden", False)),
    }


def device_timezone():
    """Timezone to pin on every unit. CAS_TZ env > config 'timezone' > 'Asia/Manila' (UTC+8)."""
    return os.environ.get("CAS_TZ") or load_config().get("timezone") or "Asia/Manila"


def retail_clean_flags():
    """Which fingerprint categories the seal-time wipe clears. From cas-config.json 'retail_clean';
    every flag defaults True (a missing or partial dict still wipes everything not explicitly disabled)."""
    raw = load_config().get("retail_clean")
    raw = raw if isinstance(raw, dict) else {}
    return {k: bool(raw.get(k, True)) for k in ("recents", "saves", "media", "android_traces")}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cas.TestConfig.test_wifi_config_absent_is_none tests.test_cas.TestConfig.test_wifi_and_clean_config_parsed -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add cas/config.py tests/test_cas.py
git commit -m "feat(cas): config accessors for wifi, timezone, retail_clean"
```

---

### Task 2: `Adb.wifi_join` + `Adb.wifi_forget_all`

**Files:**
- Modify: `cas/adb.py` (add two methods to `class Adb`, after `sd_info`, ~line 245)
- Test: `tests/test_cas.py` (add to `class TestAdb`, ~line 97)

**Interfaces:**
- Consumes: `self.su(cmd)` (root), `self.shell(cmd)`.
- Produces:
  - `Adb.wifi_join(ssid, password="", security="wpa2", hidden=False) -> bool` — enables the radio and connects. Returns True on rc==0.
  - `Adb.wifi_forget_all() -> int` — forgets every saved network, returns how many it forgot. Radio left ON.

- [ ] **Step 1: Write the failing tests**

Add a small canned-output runner and two tests to `tests/test_cas.py` (place the helper just above `class TestAdb`):

```python
class WifiRunner:
    """Records calls; returns a canned 'cmd wifi list-networks' table so forget-all has ids to walk."""

    def __init__(self, networks=("0", "1")):
        self.calls = []
        self.networks = networks

    def __call__(self, args, input_text=None, timeout=900):
        self.calls.append(list(args))
        cmd = args[-1]
        if "list-networks" in cmd:
            rows = "\n".join(f"{i}    SSID{i}    PSK" for i in self.networks)
            return 0, "Network Id     SSID    Security type\n" + rows + "\n", ""
        return 0, "", ""

    def cmds(self):
        return [" ".join(c) for c in self.calls]
```

```python
    def test_wifi_join_wpa2(self):
        r = WifiRunner()
        ok = Adb(runner=r).wifi_join("Luxium-Shop", "secret", "wpa2")
        self.assertTrue(ok)
        c = "\n".join(r.cmds())
        self.assertIn("svc wifi enable", c)
        self.assertIn('cmd wifi connect-network Luxium-Shop wpa2 secret', c)

    def test_wifi_join_open_omits_password(self):
        r = WifiRunner()
        Adb(runner=r).wifi_join("FreeWifi", "", "open")
        c = "\n".join(r.cmds())
        self.assertIn("cmd wifi connect-network FreeWifi open", c)
        self.assertNotIn("connect-network FreeWifi open  ", c)  # no stray empty password slot

    def test_wifi_join_hidden_adds_flag(self):
        r = WifiRunner()
        Adb(runner=r).wifi_join("Hidden", "pw", "wpa2", hidden=True)
        self.assertIn("-h", "\n".join(r.cmds()))

    def test_wifi_forget_all_forgets_each_and_keeps_radio_on(self):
        r = WifiRunner(networks=("0", "1", "2"))
        n = Adb(runner=r).wifi_forget_all()
        c = "\n".join(r.cmds())
        self.assertEqual(n, 3)
        self.assertIn("cmd wifi forget-network 0", c)
        self.assertIn("cmd wifi forget-network 1", c)
        self.assertIn("cmd wifi forget-network 2", c)
        self.assertNotIn("svc wifi disable", c)  # radio stays ON for the customer
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas.TestAdb -v -k wifi`
Expected: FAIL with `AttributeError: 'Adb' object has no attribute 'wifi_join'`.

- [ ] **Step 3: Implement the two methods**

Add to `class Adb` in `cas/adb.py` (after `sd_info`, ~line 245). `_q` quotes an SSID/password so spaces survive the `adb shell` word-split:

```python
    @staticmethod
    def _q(s):
        """Quote one token for the device shell (SSID/password may contain spaces)."""
        return "'" + str(s).replace("'", "'\\''") + "'"

    def wifi_join(self, ssid, password="", security="wpa2", hidden=False):
        """Enable WiFi and connect to <ssid>. security ∈ {open,wpa2,wpa3}; open omits the password.
        Runs as root (works pre-OOBE / on a fresh unit). Returns True on success."""
        self.su("svc wifi enable")
        parts = ["cmd", "wifi", "connect-network", self._q(ssid), security]
        if security != "open":
            parts.append(self._q(password))
        if hidden:
            parts.append("-h")
        rc, _, _ = self.su(" ".join(parts))
        return rc == 0

    def wifi_forget_all(self):
        """Forget every saved network (so the shop SSID/password never ships). Radio left ON.
        Parses 'cmd wifi list-networks' (rows begin with an integer id). Returns count forgotten."""
        out = self.su("cmd wifi list-networks")[1]
        ids = []
        for line in out.splitlines():
            tok = line.split()
            if tok and tok[0].isdigit():
                ids.append(tok[0])
        for nid in ids:
            self.su("cmd wifi forget-network " + nid)
        return len(ids)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cas.TestAdb -v -k wifi`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add cas/adb.py tests/test_cas.py
git commit -m "feat(cas): Adb.wifi_join + wifi_forget_all (radio stays on)"
```

---

### Task 3: `apply_timezone()`

**Files:**
- Modify: `cas/provision.py` (add a module-level function, after `provision()`, ~line 312)
- Test: `tests/test_cas.py` (add to `class TestProvision`)

**Interfaces:**
- Consumes: `adb.su(cmd)`, `adb.shell(cmd)`.
- Produces: `apply_timezone(adb, tz="Asia/Manila", log=print) -> None` — pins TZ, disables network TZ override, enables NTP clock sync.

- [ ] **Step 1: Write the failing test**

```python
    def test_apply_timezone_pins_manila_and_ntp(self):
        r = FakeRunner()
        PV.apply_timezone(Adb(runner=r), "Asia/Manila", log=lambda m: None)
        c = "\n".join(r.cmds())
        self.assertIn("setprop persist.sys.timezone Asia/Manila", c)
        self.assertIn("auto_time_zone 0", c)   # pin: don't let the network re-resolve the zone
        self.assertIn("auto_time 1", c)        # NTP clock sync (unit is online during setup)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cas.TestProvision.test_apply_timezone_pins_manila_and_ntp -v`
Expected: FAIL with `AttributeError: module 'cas.provision' has no attribute 'apply_timezone'`.

- [ ] **Step 3: Implement**

Add after `provision()` in `cas/provision.py`:

```python
def apply_timezone(adb, tz="Asia/Manila", log=print):
    """Pin the unit's timezone (default Asia/Manila = UTC+8) so it sticks even after WiFi is forgotten
    at seal. auto_time_zone OFF = don't let the network re-resolve the zone; auto_time ON = NTP sync the
    wall clock while the unit is online during setup. setprop persist.* needs root (we are rooted here)."""
    adb.su(f"setprop persist.sys.timezone {tz}")
    adb.shell("settings put global auto_time_zone 0; settings put global auto_time 1")
    log(f"timezone pinned to {tz} (NTP clock sync on; zone won't be network-overridden).")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cas.TestProvision.test_apply_timezone_pins_manila_and_ntp -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(cas): apply_timezone pins Asia/Manila + NTP clock sync"
```

---

### Task 4: Canonical wipe script `provision/retail-clean.sh`

**Files:**
- Create: `provision/retail-clean.sh`
- Test: `tests/test_cas.py` (new `class TestRetailClean` — a STATIC safety test on the script text)

**Interfaces:**
- Produces: a device-side script sourcing `lib.sh`, gated by env flags `CLEAN_RECENTS`/`CLEAN_SAVES`/`CLEAN_MEDIA`/`CLEAN_TRACES` (default `1`) and `CLEAN_DRYRUN` (default `0`). Every deletion goes through one `wipe()` helper. Preserve allowlist (bios/keys/nand-system/Sys/firmware) is never a `wipe` target.

- [ ] **Step 1: Write the failing safety test**

```python
class TestRetailClean(unittest.TestCase):
    """Static guardrails on the device-side wipe script — the wipe runs on real units, so we assert the
    preserve-allowlist holds and the expected fingerprints are targeted, without needing a device."""

    def _script(self):
        p = pathlib.Path(__file__).resolve().parent.parent / "provision" / "retail-clean.sh"
        return p.read_text()

    def test_script_exists_and_sources_lib(self):
        s = self._script()
        self.assertIn("detect_sd", s)
        self.assertIn("wipe()", s)            # all deletes funnel through one helper

    def test_preserve_allowlist_never_wiped(self):
        # No wipe/rm line may target BIOS/keys/firmware/system. Check every line that deletes.
        forbidden = ("/bios", "prod.keys", "/keys", "nand/system", "/Sys/", "dc_boot.bin",
                     "dc_flash.bin", "firmware.bin")
        for line in self._script().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("wipe ") or " rm -rf" in stripped or stripped.startswith("rm -rf"):
                for tok in forbidden:
                    self.assertNotIn(tok, stripped, f"preserve token {tok!r} in delete line: {stripped}")

    def test_targets_expected_fingerprints(self):
        s = self._script()
        for tok in ("content_history.lpl", "[Recent]", "lastplayed", "saves", "states",
                    "screenshots", "usagestats"):
            self.assertIn(tok, s, f"expected wipe target {tok!r} missing from retail-clean.sh")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas.TestRetailClean -v`
Expected: FAIL with `FileNotFoundError` (script not created yet).

- [ ] **Step 3: Create `provision/retail-clean.sh`**

```sh
# retail-clean.sh — wipe every "this unit was tested" fingerprint, KEEPING ROMs/emulators/BIOS/keys.
# Sourced by cleanup.sh (on-device) and pushed+run by cas.provision.clean_for_retail (PC, at seal).
# Runs as ROOT (Mupen recents + Android usage stats live in /data/data). Pure sed/grep/tr (rish-safe).
# Flags (default on): CLEAN_RECENTS CLEAN_SAVES CLEAN_MEDIA CLEAN_TRACES. CLEAN_DRYRUN=1 -> log, don't delete.
# Preserve allowlist (NEVER deleted): bios/firmware/keys, NAND system, Sys/, controller profiles, settings.
hdr "retail-clean  (erasing test fingerprints — keeping ROMs/emulators/BIOS/keys)"
detect_sd
DRY="${CLEAN_DRYRUN:-0}"
EXT=/sdcard/Android/data            # external per-app data root (== /storage/emulated/0/Android/data)

wipe(){ # $1=path : delete a single fingerprint target, best-effort, dry-run-aware
  [ "$DRY" = 1 ] && { log "DRYRUN would remove: $1"; return 0; }
  SH "rm -rf \"$1\" 2>/dev/null" && log "removed $1"; }

strip_block(){ # $1=ini : delete the [Recent] section (header + entries up to the next [section])
  exists "$1" || return 0
  [ "$DRY" = 1 ] && { log "DRYRUN would strip [Recent] from $1"; return 0; }
  SH "sed -i '/^\[Recent\]/,/^\[/{/^\[Recent\]/d;/^\[/!d;}' \"$1\"" && log "stripped [Recent] from ${1##*/}"; }

# ---------- A. recents / history ----------
if [ "${CLEAN_RECENTS:-1}" = 1 ]; then
  # PPSSPP: recent-ISO list lives in the [Recent] section of ppsspp.ini (memstick = ROMs/psp)
  strip_block "$SDPATH/ROMs/psp/PSP/SYSTEM/ppsspp.ini"
  # RetroArch: history + favorites playlists
  for f in content_history content_favorites content_image_history content_music_history content_video_history; do
    wipe "$EXT/com.retroarch.aarch64/files/$f.lpl"
  done
  # Mupen64Plus FZ: recently-played gallery cache (internal data — root). Path verified on-device; rm is a
  # harmless no-op if absent.
  wipe "/data/data/org.mupen64plusae.v3.fzurita/files/mupen64plus_data/gallery"
  # ES-DE: drop <lastplayed> and zero <playcount> in every gamelist (empties the auto "Last Played" set)
  if [ "$DRY" = 1 ]; then log "DRYRUN would clear lastplayed/playcount in $SDPATH/ES-DE/gamelists/*/gamelist.xml"
  else SH "for g in \"$SDPATH\"/ES-DE/gamelists/*/gamelist.xml; do [ -f \"\$g\" ] && sed -i 's#<lastplayed>[^<]*</lastplayed>##g; s#<playcount>[^<]*</playcount>#<playcount>0</playcount>#g' \"\$g\"; done" && log "cleared ES-DE lastplayed/playcount"; fi
fi

# ---------- B. saves & save-states (KEEP bios/keys/firmware/NAND-system) ----------
if [ "${CLEAN_SAVES:-1}" = 1 ]; then
  wipe "$EXT/com.retroarch.aarch64/files/saves"
  wipe "$EXT/com.retroarch.aarch64/files/states"
  wipe "$SDPATH/ROMs/psp/PSP/SAVEDATA"
  wipe "$SDPATH/ROMs/psp/PSP/PPSSPP_STATE"
  wipe "$EXT/com.github.stenzek.duckstation/files/memcards"
  wipe "$EXT/com.github.stenzek.duckstation/files/savestates"
  wipe "$EXT/dev.eden.eden_emulator/files/nand/user/save"          # KEEP nand/system + keys
  wipe "$EXT/xyz.aethersx2.tturnip/files/memcards"
  wipe "$EXT/xyz.aethersx2.tturnip/files/sstates"                  # KEEP bios
  wipe "$EXT/org.dolphinemu.dolphinemu/files/StateSaves"           # KEEP Sys/
  wipe "$EXT/org.citra.emu/files/states"                           # KEEP system NAND + aes_keys
fi

# ---------- C. screenshots / thumbnails / logs ----------
if [ "${CLEAN_MEDIA:-1}" = 1 ]; then
  wipe "$EXT/com.retroarch.aarch64/files/screenshots"
  wipe "$EXT/com.retroarch.aarch64/files/thumbnails"
  wipe "$EXT/com.retroarch.aarch64/files/logs"
  wipe "$SDPATH/ROMs/psp/PSP/SCREENSHOT"
  wipe "$EXT/com.github.stenzek.duckstation/files/screenshots"
  wipe "$EXT/dev.eden.eden_emulator/files/screenshots"
  wipe "$EXT/org.dolphinemu.dolphinemu/files/ScreenShots"
  wipe "/sdcard/Pictures/Screenshots"
  SH "rm -f \"$SDPATH\"/ES-DE/es_log*.txt 2>/dev/null"; log "removed ES-DE logs"
fi

# ---------- D. Android usage traces (root; regenerate clean after the seal reboot) ----------
if [ "${CLEAN_TRACES:-1}" = 1 ]; then
  [ "$DRY" = 1 ] || SH "logcat -c 2>/dev/null"; log "cleared logcat"
  wipe "/data/system/usagestats"
  wipe "/data/system_ce/0/recent_tasks"
  wipe "/data/system_ce/0/recent_images"
fi

ok "retail-clean done (DRYRUN=$DRY). ROMs/emulators/BIOS/keys preserved."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cas.TestRetailClean -v`
Expected: PASS (3 tests). If `test_preserve_allowlist_never_wiped` flags a line, the line is targeting a preserve path — fix the script, not the test.

- [ ] **Step 5: Commit**

```bash
git add provision/retail-clean.sh tests/test_cas.py
git commit -m "feat(provision): retail-clean.sh — wipe test fingerprints, keep bios/keys"
```

---

### Task 5: `clean_for_retail()` — forget WiFi + push/run the wipe

**Files:**
- Modify: `cas/provision.py` (add module-level function near `seal`, and a path const near the other `BUNDLE/...` consts ~line 56)
- Test: `tests/test_cas.py` (add to a new `class TestCleanForRetail` or `TestSeal`)

**Interfaces:**
- Consumes: `Adb.wifi_forget_all()` (Task 2), `adb.push(src, dst)`, `adb.su(cmd)`; `RETAIL_CLEAN = BUNDLE / "provision" / "retail-clean.sh"`, existing `LIB = BUNDLE / "provision" / "lib.sh"`, `DEV` workdir.
- Produces: `clean_for_retail(adb, flags=None, log=print) -> None` — forgets all WiFi, then pushes `lib.sh` + `retail-clean.sh` to the device and runs the wipe as root with `CLEAN_*` envs from `flags` (None ⇒ all-on). Best-effort; never raises.

- [ ] **Step 1: Write the failing test**

```python
class TestCleanForRetail(unittest.TestCase):
    def test_forgets_wifi_pushes_script_and_runs_with_flag_envs(self):
        r = WifiRunner(networks=("0",))          # one saved network to forget
        PV.clean_for_retail(Adb(runner=r),
                            {"recents": True, "saves": False, "media": True, "android_traces": True},
                            log=lambda m: None)
        c = "\n".join(r.cmds())
        self.assertIn("cmd wifi forget-network 0", c)     # WiFi forgotten
        self.assertIn("retail-clean.sh", c)               # canonical script pushed + run
        self.assertIn("CLEAN_SAVES=0", c)                 # flags -> envs (saves disabled)
        self.assertIn("CLEAN_RECENTS=1", c)

    def test_never_raises_on_push_failure(self):
        r = FakeRunner(push_ok=False)
        # must not raise — a clean failure cannot abort the caller (seal)
        PV.clean_for_retail(Adb(runner=r), None, log=lambda m: None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas.TestCleanForRetail -v`
Expected: FAIL with `AttributeError: module 'cas.provision' has no attribute 'clean_for_retail'`.

- [ ] **Step 3: Implement**

Add the path const near the other `BUNDLE/...` consts in `cas/provision.py` (the file already defines `RESTORE`/`LIBROOT` at ~line 56; add alongside):

```python
RETAIL_CLEAN = BUNDLE / "provision" / "retail-clean.sh"   # canonical seal-time fingerprint wipe
LIB = BUNDLE / "provision" / "lib.sh"                      # helpers retail-clean.sh sources (detect_sd/SH/log)
```

Add the function just above `seal()`:

```python
def clean_for_retail(adb, flags=None, log=print):
    """SEAL-time retail clean (run while still rooted, BEFORE un-root): forget every saved WiFi network
    (radio stays ON) and run the canonical retail-clean.sh to erase test fingerprints. flags is the
    retail_clean dict ({recents,saves,media,android_traces}); None => all on. Best-effort and total: a
    failure here WARNS but never raises, so it can never abort the seal that follows."""
    flags = flags or {"recents": True, "saves": True, "media": True, "android_traces": True}
    try:
        n = adb.wifi_forget_all()
        log(f"forgot {n} saved WiFi network(s) (radio left on).")
    except Exception as e:
        log(f"warning: WiFi forget failed: {e}")
    try:
        workdir = f"{DEV}_clean"
        adb.su(f"rm -rf {workdir}; mkdir -p {workdir}")
        if not (adb.push(LIB, f"{workdir}/lib.sh") and adb.push(RETAIL_CLEAN, f"{workdir}/retail-clean.sh")):
            log("warning: could not push retail-clean.sh — skipping fingerprint wipe (seal continues).")
            return
        dry = 1 if os.environ.get("CLEAN_DRYRUN") == "1" else 0
        env = (f"CLEAN_RECENTS={1 if flags.get('recents', True) else 0} "
               f"CLEAN_SAVES={1 if flags.get('saves', True) else 0} "
               f"CLEAN_MEDIA={1 if flags.get('media', True) else 0} "
               f"CLEAN_TRACES={1 if flags.get('android_traces', True) else 0} "
               f"CLEAN_DRYRUN={dry}")
        adb.su_stream(f"cd {workdir} && {env} CAS_MODE=local sh -c '. ./lib.sh; . ./retail-clean.sh'", log)
        adb.su(f"rm -rf {workdir}")
    except Exception as e:
        log(f"warning: retail-clean failed: {e} (seal continues).")
```

`os` is already imported at the top of `cas/provision.py` (line 8). Folding `CLEAN_DRYRUN` into the `env` here means Task 8's `--dry-run` works through the same path — so in Task 8, Step 3 you do NOT need a separate env addition; the note there about "also OR it into the env string" is already satisfied by this line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cas.TestCleanForRetail -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(cas): clean_for_retail — forget wifi + run canonical wipe (best-effort)"
```

---

### Task 6: Wire WiFi join + TZ into `provision()`

**Files:**
- Modify: `cas/provision.py` (`provision()`, just before `adb.reboot()` at ~line 309)
- Test: `tests/test_cas.py` (add to `class TestProvision`)

**Interfaces:**
- Consumes: `wifi_config()`, `device_timezone()` (Task 1), `Adb.wifi_join` (Task 2), `apply_timezone` (Task 3).

- [ ] **Step 1: Write the failing tests**

These drive `provision()` with `dry_push=False` so the new (gated on `not dry_push`) block runs. `make_profile` + `FakeRunner` already make a real run succeed. WiFi is controlled via a temp `CAS_CONFIG`:

```python
    def test_provision_pins_timezone_when_not_dry(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            cf = pathlib.Path(t) / "cas-config.json"
            cf.write_text("{}")                          # no wifi -> join skipped, TZ still pinned
            old = os.environ.get("CAS_CONFIG"); os.environ["CAS_CONFIG"] = str(cf)
            try:
                r = FakeRunner()
                ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)   # dry_push=False
                self.assertTrue(ok)
                c = "\n".join(r.cmds())
                self.assertIn("persist.sys.timezone Asia/Manila", c)
                self.assertNotIn("connect-network", c)   # no wifi configured -> no join
            finally:
                os.environ.pop("CAS_CONFIG", None)
                if old is not None: os.environ["CAS_CONFIG"] = old

    def test_provision_joins_wifi_when_configured(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            cf = pathlib.Path(t) / "cas-config.json"
            cf.write_text(json.dumps({"wifi": {"ssid": "Luxium-Shop", "password": "pw"}}))
            old = os.environ.get("CAS_CONFIG"); os.environ["CAS_CONFIG"] = str(cf)
            try:
                r = FakeRunner()
                ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)
                self.assertTrue(ok)
                self.assertIn("connect-network Luxium-Shop wpa2 pw", "\n".join(r.cmds()))
            finally:
                os.environ.pop("CAS_CONFIG", None)
                if old is not None: os.environ["CAS_CONFIG"] = old

    def test_provision_dry_push_skips_wifi_and_tz(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            cf = pathlib.Path(t) / "cas-config.json"
            cf.write_text(json.dumps({"wifi": {"ssid": "X", "password": "y"}}))
            old = os.environ.get("CAS_CONFIG"); os.environ["CAS_CONFIG"] = str(cf)
            try:
                r = FakeRunner()
                PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)
                c = "\n".join(r.cmds())
                self.assertNotIn("connect-network", c)            # dry run touches no device state
                self.assertNotIn("persist.sys.timezone", c)
            finally:
                os.environ.pop("CAS_CONFIG", None)
                if old is not None: os.environ["CAS_CONFIG"] = old
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_cas.TestProvision -v -k "timezone or joins_wifi or dry_push_skips"`
Expected: FAIL (`persist.sys.timezone` / `connect-network` not found).

- [ ] **Step 3: Implement the wiring**

In `cas/provision.py`, in `provision()`, replace the tail (the existing `if not dry_push: adb.su(f"rm -rf {DEV}")` + `adb.reboot()` at ~lines 307-310) so the WiFi/TZ block runs before the reboot:

```python
    if not dry_push:
        adb.su(f"rm -rf {DEV}")
        # Shop WiFi + Asia/Manila TZ for setup/QA (forgotten again at seal). Both persist the reboot below.
        from . import config as _cfg
        wifi = _cfg.wifi_config()
        if wifi:
            log(f"joining shop WiFi '{wifi['ssid']}'...")
            if not adb.wifi_join(wifi["ssid"], wifi["password"], wifi["security"], wifi["hidden"]):
                log("warning: WiFi join failed (bad creds / no AP?) — provisioning continues offline.")
        apply_timezone(adb, _cfg.device_timezone(), log=log)
    adb.reboot()
    log(f"==> provisioned '{profile.name}'. Rebooting; verify on device after boot.")
    return True
```

(The original code had `if not dry_push:` guarding only the `rm -rf {DEV}`; this merges the WiFi/TZ steps into that same guard. Confirm there is exactly one `if not dry_push:` block at the tail after the change.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_cas.TestProvision -v`
Expected: PASS (all TestProvision tests, including the 3 new ones and the pre-existing dry_push ones).

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(cas): provision joins shop WiFi + pins Asia/Manila TZ"
```

---

### Task 7: Wire `clean_for_retail` into `seal()`

**Files:**
- Modify: `cas/provision.py` (`seal()`, after `target = adb.boot_flash_target()` ~line 648, before the Magisk-uninstall block ~line 650)
- Test: `tests/test_cas.py` (add to `class TestSeal`)

**Interfaces:**
- Consumes: `clean_for_retail` (Task 5), `retail_clean_flags()` (Task 1).

- [ ] **Step 1: Write the failing test**

```python
    def test_seal_wipes_and_forgets_wifi_before_unroot(self):
        ra, fb = WifiRunner(networks=("0",)), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertTrue(ok)
        cmds = ra.cmds()
        joined = "\n".join(cmds)
        self.assertIn("cmd wifi forget-network 0", joined)         # WiFi forgotten
        self.assertIn("retail-clean.sh", joined)                   # fingerprint wipe ran
        # the wipe/forget must happen BEFORE the un-root's Magisk uninstall
        wipe_i = next(i for i, c in enumerate(cmds) if "retail-clean.sh" in c)
        magisk_i = next(i for i, c in enumerate(cmds) if "uninstall com.topjohnwu.magisk" in c)
        self.assertLess(wipe_i, magisk_i)
        self.assertIn("adb_enabled 0", cmds[-1])                   # lockdown still LAST
```

Note: `WifiRunner` returns `0,"",""` for the golden probe and `id`/`getprop` calls (its default branch), so `seal()`'s `is_golden()` reads NOT-golden and `is_root()` reads non-root — fine for this path (seal still flashes stock + locks down). The `model_match` is None here so no mismatch refusal.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cas.TestSeal.test_seal_wipes_and_forgets_wifi_before_unroot -v`
Expected: FAIL (`retail-clean.sh` / `forget-network` not in the recorded commands).

- [ ] **Step 3: Implement the wiring**

In `seal()`, right after `target = adb.boot_flash_target()` (~line 648) and before the `if adb.is_root():` Magisk-uninstall block (~line 650), insert:

```python
    # Retail clean BEFORE un-root: forget shop WiFi + erase test fingerprints while root is still present
    # (Mupen recents + Android usage stats need /data/data root). Best-effort — never aborts the seal.
    from . import config as _cfg
    clean_for_retail(adb, _cfg.retail_clean_flags(), log=log)
```

- [ ] **Step 4: Run the FULL seal suite to verify the new test passes and none regress**

Run: `python3 -m unittest tests.test_cas.TestSeal -v`
Expected: PASS (all of TestSeal — the new test plus the 7 existing ones; the existing `assertIn`/`cmds()[-1]` assertions still hold because the clean runs before the lockdown and uses adb, not fastboot).

- [ ] **Step 5: Commit**

```bash
git add cas/provision.py tests/test_cas.py
git commit -m "feat(cas): seal runs clean_for_retail before un-root (wifi forget + wipe)"
```

---

### Task 8: CLI `clean` subcommand + on-device `cleanup.sh` mirror

**Files:**
- Modify: `cas/cli.py` (add a `clean` subparser + handler)
- Modify: `provision/cleanup.sh` (source `retail-clean.sh` before uninstalling Shizuku)
- Test: `tests/test_cas.py` (add a CLI test to a new `class TestCli` or extend an existing one)

**Interfaces:**
- Consumes: `clean_for_retail` (Task 5), `retail_clean_flags()` (Task 1).
- Produces: `python -m cas.cli clean [--serial S] [--dry-run]` — runs (or previews) the retail clean on one device without un-rooting.

- [ ] **Step 1: Write the failing test**

```python
class TestCli(unittest.TestCase):
    def test_clean_subcommand_runs_retail_clean(self):
        from cas import cli
        calls = {}
        def fake_clean(adb, flags, log=print):
            calls["ran"] = True
            calls["dry"] = "1" if getattr(adb, "_dry", False) else "0"
        old = PV.clean_for_retail
        PV.clean_for_retail = fake_clean
        try:
            rc = cli.main(["clean", "--serial", "ABC123"])
        finally:
            PV.clean_for_retail = old
        self.assertEqual(rc, 0)
        self.assertTrue(calls.get("ran"))
```

(If `cli.main` imports `clean_for_retail` by name rather than via the `PV.` module attribute, patch the name the handler actually calls — adjust the patched symbol to match the import style chosen in Step 3. Keep the handler calling `PV.clean_for_retail` / `provision.clean_for_retail` so this patch point holds.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cas.TestCli.test_clean_subcommand_runs_retail_clean -v`
Expected: FAIL — `argument cmd: invalid choice: 'clean'`.

- [ ] **Step 3: Implement the CLI handler**

In `cas/cli.py`, add the subparser next to the others (after the `seal` subparser, ~line 53):

```python
    cl = sub.add_parser("clean", help="retail-clean one device (forget WiFi + wipe test fingerprints), no un-root")
    cl.add_argument("--dry-run", action="store_true", help="list what would be wiped without deleting")
```

And add the handler (after the `seal` handler, before `return 2`):

```python
    if a.cmd == "clean":
        from .config import retail_clean_flags
        if a.dry_run:
            os.environ["CLEAN_DRYRUN"] = "1"   # Task 5's clean_for_retail folds this into the device env
        PV.clean_for_retail(adb, retail_clean_flags(), log=print)
        return 0
```

This relies on Task 5 already reading `CLEAN_DRYRUN` from the host env into the device command line (the `dry = ...` line added there), so `--dry-run` makes `retail-clean.sh` log targets and delete nothing. Add `import os` at the top of `cas/cli.py` if it is not already imported (it is not in the current imports — add it alongside `import argparse`).

- [ ] **Step 4: Add the on-device mirror in `provision/cleanup.sh`**

In `provision/cleanup.sh`, add this BEFORE the Termux/Shizuku uninstall (before line ~15, after the provisioning-file deletion in step 1), so the wipe runs while root/rish is still alive:

```sh
# retail clean (forget WiFi + erase test fingerprints) BEFORE we uninstall Shizuku/Termux (which ends root)
if [ -f "$DIR/retail-clean.sh" ]; then ( . "$DIR/retail-clean.sh" ); else warn "retail-clean.sh not found — skipping fingerprint wipe"; fi
```

Verify `$DIR` is defined in `cleanup.sh`/`master.sh` (it is used by `master.sh` to source siblings). If `cleanup.sh` does not already have `$DIR`, derive it once at the top: `DIR="${DIR:-$(dirname "$0")}"`.

- [ ] **Step 5: Run the full suite + commit**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -t .`
Expected: PASS (all tests — target 100+).

```bash
git add cas/cli.py cas/provision.py provision/cleanup.sh tests/test_cas.py
git commit -m "feat(cas): cli 'clean' subcommand + cleanup.sh sources retail-clean.sh"
```

---

### Task 9: Operator config + docs

**Files:**
- Modify: `cas-config.json` (add example keys — this file is gitignored, so it is a LOCAL convenience only; do NOT commit it)
- Modify: `provision/RUNBOOK.md` (document the WiFi/TZ/clean behavior)

**Interfaces:** none (documentation + local config).

- [ ] **Step 1: Add the keys to the local `cas-config.json`**

Open `cas-config.json` and add (fill `password` with the real shop WiFi password locally — it is gitignored):

```json
  "wifi": { "ssid": "Luxium-Shop", "password": "REPLACE_ME", "security": "wpa2", "hidden": false },
  "timezone": "Asia/Manila",
  "retail_clean": { "recents": true, "saves": true, "media": true, "android_traces": true }
```

Verify it stays valid JSON: `python3 -c "import json; json.load(open('cas-config.json')); print('ok')"`
Expected: `ok`.

- [ ] **Step 2: Document the flow in `provision/RUNBOOK.md`**

Add a short section explaining: provision auto-joins shop WiFi (from `cas-config.json` `wifi`) and pins `Asia/Manila`; seal forgets the WiFi (radio stays on) and runs `retail-clean.sh` to wipe recents/saves/media/Android traces while keeping ROMs/emulators/BIOS/keys; `cas.cli clean --dry-run` previews the wipe. Mention that `cas-config.json` is gitignored and holds the shop WiFi password.

- [ ] **Step 3: Commit (docs only — NOT cas-config.json)**

```bash
git add provision/RUNBOOK.md
git commit -m "docs(provision): document retail WiFi/TZ/clean flow"
```

---

## Self-Review

**1. Spec coverage:**
- WiFi auto-join from config → Tasks 1, 2, 6. ✓
- Asia/Manila TZ pin + NTP → Tasks 1, 3, 6. ✓
- Forget WiFi at seal, radio ON → Tasks 2, 5, 7 (and `test_..._keeps_radio_on`). ✓
- Wipe recents/saves/media/Android traces, preserve bios/keys → Task 4 (+ safety test) , 5, 7. ✓
- No-op when config absent → Task 1 defaults + Task 6 gating + `test_provision_dry_push_skips_wifi_and_tz`. ✓
- Best-effort, never abort seal → Task 5 (`test_never_raises_on_push_failure`) + Task 7 ordering test. ✓
- CLI `clean` + `--dry-run` → Task 8. ✓
- On-device cleanup.sh mirror → Task 8. ✓
- Config example + docs → Task 9. ✓
- Tests per spec §Testing (commands, no-disable, preserve allowlist, resilience, no-op) → Tasks 2,4,5,6,7. ✓

**2. Placeholder scan:** No `TBD`/`TODO`/"handle edge cases". The one device-path uncertainty (Mupen64 gallery) is a concrete `rm` with an inline note and is harmless if absent — not a placeholder. The `REPLACE_ME` in Task 9 is an intentional local secret the operator fills (gitignored file). ✓

**3. Type consistency:** `wifi_config()` returns the dict keys `ssid/password/security/hidden` consumed verbatim in Task 6. `retail_clean_flags()` keys `recents/saves/media/android_traces` map to `CLEAN_RECENTS/SAVES/MEDIA/TRACES` consistently in Task 5's spelled-out `env`. `clean_for_retail(adb, flags, log)` signature matches its callers in Tasks 7 and 8. `wifi_forget_all()` returns an int (count) used in Task 5's log and Task 2's assertion. ✓
