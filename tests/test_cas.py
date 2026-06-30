"""Unit tests for the cas package — mock adb runner, no real device. Run from project root:
    python3 -m unittest discover -s tests -p 'test_*.py' -t .
"""
import os
import sys
import tarfile
import tempfile
import unittest
import pathlib

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas.adb import Adb, Fastboot, list_devices
from cas import profiles as P
from cas import provision as PV


class FakeRunner:
    """Records calls; returns canned (rc, out, err) shaped like the real adb."""

    def __init__(self, model="Odin2 Mini", golden=False, root=True, boot="1", sd=True,
                 push_ok=True, pull_ok=True, su_blocked=False, slot="_a", first_api="33",
                 device_owner=False, do_set_ok=True, do_restrict=True, release_clears=True):
        self.calls = []
        self.model, self.golden, self.root, self.boot, self.sd = model, golden, root, boot, sd
        self.push_ok, self.pull_ok = push_ok, pull_ok
        # su_blocked models a real device whose MagiskSU grant prompt was never tapped: EVERY `su` call
        # hangs until the runner's timeout fires, which subprocess_runner reports as (124, "", "timeout…").
        self.su_blocked = su_blocked
        # slot = ro.boot.slot_suffix ('_a'/'_b', or '' for A-only); first_api = ro.product.first_api_level
        # (>=33 means the unit LAUNCHED on Android 13+ and so has an init_boot partition). Defaults model an
        # A/B A13 unit on slot A — so the detected flash target stays 'init_boot_a' (existing assertions hold).
        self.slot, self.first_api = slot, first_api
        self._owner = device_owner          # current device-owner state (mutated by a release broadcast)
        self.do_set_ok, self.do_restrict, self.release_clears = do_set_ok, do_restrict, release_clears

    def __call__(self, args, input_text=None, timeout=900):
        self.calls.append(list(args))
        if args[-1] == "devices":
            return 0, "List of devices attached\nABC123\tdevice\nDEF456\tunauthorized\n", ""
        if args[-1] == "reboot":
            return 0, "", ""
        if "push" in args:
            return (0 if self.push_ok else 1), "", ""
        if "pull" in args:
            return (0 if self.pull_ok else 1), "", ""
        if "shell" in args:
            if "/debug_ramdisk/su" in args:
                if self.su_blocked:
                    return 124, "", f"timeout after {timeout}s"
                cmd = args[-1]
                if cmd == "id":
                    return (0, "uid=0(root)\n", "") if self.root else (1, "", "Permission denied")
                if ".cas_golden" in cmd:
                    return 0, ("CAS_GOLD\n" if self.golden else "CAS_NOTGOLD\n"), ""
                if "restore.sh" in cmd:
                    return 0, "[ok] restored apps\n[ok] RESTORE complete\n", ""
                if "capture.sh" in cmd:
                    return 0, "[ok] GOLDEN captured\n", ""
                if "/storage/*-*" in cmd:
                    return 0, ("/storage/9C33-6BBD\n" if self.sd else ""), ""
                return 0, "", ""
            tail = args[-1]
            if tail.startswith("dpm list-owners"):
                return 0, ("Device owner: com.gamecove.gamecove_companion\n" if self._owner
                           else "No device owner.\n"), ""
            if tail.startswith("dpm set-device-owner"):
                if self.do_set_ok:
                    self._owner = True
                    return 0, "Success: Device owner set to package\n", ""
                return 255, "", "java.lang.IllegalStateException: Not allowed to set the device owner\n"
            if tail.startswith("dumpsys device_policy"):
                return 0, ("no_factory_reset no_safe_boot\n" if (self._owner and self.do_restrict)
                           else "\n"), ""
            if tail.startswith("am broadcast") and "action.RELEASE" in tail:
                if self.release_clears and "gc-release-7f3a9c2e" in tail:
                    self._owner = False
                return 0, "Broadcast completed: result=0\n", ""
            if tail.startswith("am start"):
                return 0, "Starting: Intent\n", ""
            if "boot_patch.sh" in tail:                 # on-device Magisk patch -> stdout sentinel
                return 0, "- Patching ramdisk\n- Repacking boot image\nCAS_PATCH_OK\n", ""
            if tail.startswith("getprop"):
                key = tail.split()[-1]
                val = {"ro.product.model": self.model, "sys.boot_completed": self.boot,
                       "ro.boot.slot_suffix": self.slot,
                       "ro.product.first_api_level": self.first_api}.get(key, "")
                return 0, val + "\n", ""
            if "CAS_XOK" in tail:                       # box-art tar unpack confirmation sentinel
                return 0, "CAS_XOK\n", ""
            return 0, "", ""
        return 0, "", ""

    def cmds(self):
        return [" ".join(c) for c in self.calls]


def make_profile(tmp, name="odin2mini", model="Odin2 ?Mini", apps=None):
    apps = apps or ["org.es_de.frontend", "dev.eden.eden_emulator", "org.citra.emu"]
    d = pathlib.Path(tmp) / name
    pay = d / "golden_root_payload"
    pay.mkdir(parents=True)
    (d / "profile.meta").write_text(
        f"model_match={model}\nfrontend=es-de\ncaptured=2026-06-16\n"
        f"stock_init_boot=provision/root/firmware/odin2_20231201/init_boot.img\n")
    (pay / "pkglist.txt").write_text("\n".join(apps) + "\n")
    (pay / "global.meta").write_text("golden_serial=9C33-6BBD\n")
    for a in apps:                                  # a VALID payload: each app has an apk + data.tar
        (pay / a / "apk").mkdir(parents=True)
        (pay / a / "apk" / "base.apk").write_text("x")
        (pay / a / "data.tar").write_text("x")
    P.save_manifest(d / "manifest", apps, {"settings": "on", "hardening": "on"},
                    header=f"# {name}")
    return P.Profile(d)


def _seed_store(store, pkg, label, content="apk"):
    """Write a single-APK store entry directly (no put_store_apk) so read-accessor tests are self-contained."""
    d = pathlib.Path(store) / pkg
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{label}.apk").write_text(content)
    (d / "meta").write_text(f"current={label}\n")


_mk = make_profile  # alias for brevity in tests


class TestAdb(unittest.TestCase):
    def test_getprop_and_root(self):
        a = Adb(runner=FakeRunner())
        self.assertEqual(a.getprop("ro.product.model"), "Odin2 Mini")
        self.assertTrue(a.is_root())
        self.assertFalse(a.is_golden())
        self.assertTrue(a.boot_completed())

    def test_not_root(self):
        self.assertFalse(Adb(runner=FakeRunner(root=False)).is_root())

    def test_boot_flash_target_init_boot_ab(self):
        # A unit launched on Android 13+ (first_api>=33) is A/B -> flash 'init_boot_<active slot>'.
        self.assertEqual(Adb(runner=FakeRunner(first_api="33", slot="_a")).boot_flash_target(), "init_boot_a")
        self.assertEqual(Adb(runner=FakeRunner(first_api="33", slot="_b")).boot_flash_target(), "init_boot_b")

    def test_boot_flash_target_legacy_boot(self):
        # A unit that did NOT launch on 13 (first_api<33) keeps the patchable ramdisk in 'boot', not init_boot.
        self.assertEqual(Adb(runner=FakeRunner(first_api="31", slot="_a")).boot_flash_target(), "boot_a")

    def test_boot_flash_target_a_only(self):
        # A-only device (empty slot_suffix) -> no slot suffix appended.
        self.assertEqual(Adb(runner=FakeRunner(first_api="33", slot="")).boot_flash_target(), "init_boot")

    def test_list_devices(self):
        devs = list_devices(runner=FakeRunner())
        self.assertEqual(devs, [("ABC123", "device"), ("DEF456", "unauthorized")])

    def test_serial_scoping(self):
        r = FakeRunner()
        Adb(serial="XYZ", runner=r).getprop("ro.product.model")
        self.assertIn("-s", r.calls[0])
        self.assertIn("XYZ", r.calls[0])

    def test_sd_info(self):
        self.assertIn("9C33-6BBD", Adb(runner=FakeRunner(sd=True)).sd_info())
        self.assertEqual(Adb(runner=FakeRunner(sd=False)).sd_info(), "no SD")

    def test_pull_with_progress_fallback(self):
        # an injected (test) runner has no real process to poll -> one blocking pull; success follows pull_ok
        self.assertTrue(Adb(runner=FakeRunner(pull_ok=True))
                        .pull_with_progress("/d/src", "/d/dst", 100, lambda m: None))
        self.assertFalse(Adb(runner=FakeRunner(pull_ok=False))
                         .pull_with_progress("/d/src", "/d/dst", 100, lambda m: None))

    def test_dir_size_kb_missing_dir(self):
        from cas import adb as A
        # pull hasn't created the dir yet -> 0, not a crash (best-effort sizing for the progress bar)
        self.assertEqual(A._dir_size_kb("/no/such/path/cas-test"), 0)

    def test_subprocess_runner_suppresses_console_window(self):
        # cas-gui.exe is a GUI app; adb/fastboot calls must pass creationflags so Windows
        # doesn't flash a console window per call (0 off-Windows; CREATE_NO_WINDOW on it).
        from cas import adb as A
        from unittest import mock
        seen = {}

        class _R:
            returncode, stdout, stderr = 0, "ok", ""

        def fake_run(args, **kw):
            seen.update(kw)
            return _R()

        with mock.patch.object(A.subprocess, "run", fake_run):
            rc, out, _ = A.subprocess_runner(["adb", "devices"])
        self.assertEqual((rc, out), (0, "ok"))
        self.assertIn("creationflags", seen)


class TestFastboot(unittest.TestCase):
    def test_remaps_to_present_device_when_fastboot_serial_differs(self):
        # MANGMI reports a DIFFERENT serial in fastboot (357451cb) than adb (MQ66…). Without remap,
        # `fastboot -s MQ66… flash` hangs forever; the flash must target the serial actually present.
        calls = []

        def runner(args, input_text=None, timeout=900):
            calls.append(list(args))
            if args[-1] == "devices":
                return 0, "357451cb\t fastboot\n", ""
            return 0, "", ""
        fb = Fastboot(serial="MQ66142509130541", runner=runner)
        self.assertTrue(fb.wait(timeout=2))
        self.assertEqual(fb.resolve(), "357451cb")
        self.assertTrue(fb.flash("init_boot_b", "/tmp/x.img"))
        flash_cmd = [c for c in calls if "flash" in c][-1]
        self.assertIn("357451cb", flash_cmd)                 # targets the real fastboot serial
        self.assertNotIn("MQ66142509130541", flash_cmd)      # NOT the adb serial (would hang)

    def test_keeps_requested_serial_when_present(self):
        def runner(args, input_text=None, timeout=900):
            if args[-1] == "devices":
                return 0, "SAME123\t fastboot\n", ""
            return 0, "", ""
        fb = Fastboot(serial="SAME123", runner=runner)
        self.assertTrue(fb.wait(timeout=2))
        self.assertEqual(fb.resolve(), "SAME123")            # Retroid/AYN: serial matches, unchanged

    def test_ambiguous_keeps_requested_serial(self):
        # several units in fastboot, requested serial absent -> don't guess; keep requested (fails loudly).
        def runner(args, input_text=None, timeout=900):
            if args[-1] == "devices":
                return 0, "aaa\t fastboot\nbbb\t fastboot\n", ""
            return 0, "", ""
        fb = Fastboot(serial="MQ66", runner=runner)
        self.assertEqual(fb.resolve(), "MQ66")


class TestManifestAxes(unittest.TestCase):
    def _write(self, text):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "manifest").write_text(text)
        return d / "manifest"

    def test_bare_line_is_both_axes(self):
        m = self._write("# h\ncom.foo\n")
        self.assertEqual(P.manifest_axes(m), {"com.foo": (True, True)})

    def test_apk_only_and_config_only(self):
        m = self._write("com.bar apk\nxyz.aethersx2.android config\n")
        ax = P.manifest_axes(m)
        self.assertEqual(ax["com.bar"], (True, False))
        self.assertEqual(ax["xyz.aethersx2.android"], (False, True))

    def test_both_tokens_order_insensitive_and_flags_ignored(self):
        m = self._write("com.baz config apk\n@settings on\n")
        self.assertEqual(P.manifest_axes(m), {"com.baz": (True, True)})


class TestProfiles(unittest.TestCase):
    def test_manifest_parse(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            self.assertEqual(prof.pkgs(),
                             ["org.es_de.frontend", "dev.eden.eden_emulator", "org.citra.emu"])
            self.assertEqual(prof.flags(), {"settings": "on", "hardening": "on"})

    def test_manifest_tolerates_non_utf8_bytes(self):
        # NAS profiles authored on Windows can carry cp1252 bytes (e.g. em-dash 0x97). Strict UTF-8 used to
        # crash the whole GUI on startup; the readers must decode tolerantly and still find ASCII pkgs/flags.
        with tempfile.TemporaryDirectory() as t:
            m = pathlib.Path(t) / "manifest"
            m.write_bytes(b"# Retroid \x97 ESDE build\norg.es_de.frontend\n@settings on\n")
            self.assertEqual(P.manifest_pkgs(m), ["org.es_de.frontend"])
            self.assertEqual(P.manifest_flags(m), {"settings": "on"})
            meta = pathlib.Path(t) / "profile.meta"
            meta.write_bytes(b"frontend=es\x97de\nmodel_match=Foo\n")
            self.assertEqual(P._read_meta(meta)["model_match"], "Foo")   # no crash; other keys parse

    def test_set_meta_key_updates_existing_and_adds_new(self):
        # The GUI's 'Root images' Browse writes stock_init_boot / magisk_apk into profile.meta in place,
        # leaving other keys + comments intact.
        with tempfile.TemporaryDirectory() as t:
            m = pathlib.Path(t) / "profile.meta"
            m.write_text("# header comment\nmodel_match=Foo\nfrontend=es-de\n")
            P.set_meta_key(m, "frontend", "retroarch")                                  # update in place
            P.set_meta_key(m, "stock_init_boot", "provision/root/firmware/x/init_boot.img")  # append new
            meta = P._read_meta(m)
            self.assertEqual(meta["frontend"], "retroarch")
            self.assertEqual(meta["model_match"], "Foo")                                # untouched
            self.assertEqual(meta["stock_init_boot"], "provision/root/firmware/x/init_boot.img")
            self.assertIn("# header comment", m.read_text())                            # comments preserved

    def test_resolve_asset_prefers_profile_local_then_appdir(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, "p", "M")
            (prof.path / "patched_init_boot.img").write_bytes(b"x")         # captured: lives in the profile
            # profile-local file wins even though appdir has no such file
            self.assertEqual(P.resolve_asset(prof, t, "patched_init_boot.img"),
                             prof.path / "patched_init_boot.img")
            # a name that only exists appdir-relative (the shared firmware library) falls back to appdir
            (pathlib.Path(t) / "shared.img").write_bytes(b"x")
            self.assertEqual(P.resolve_asset(prof, t, "shared.img"), pathlib.Path(t) / "shared.img")

    def test_match_profile(self):
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "odin2mini", "Odin2 ?Mini")
            make_profile(t, "mangmi-airx-256", "Air ?X|Mangmi")
            self.assertEqual(P.match_profile("Odin2 Mini", t).name, "odin2mini")
            self.assertEqual(P.match_profile("Mangmi Air X", t).name, "mangmi-airx-256")
            self.assertIsNone(P.match_profile("Retroid Pocket 6", t))

    def test_match_profile_ambiguous_and_empty(self):
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "a", "Odin2 ?Mini")
            make_profile(t, "b", "Odin2 Mini")          # both end-match "Odin2 Mini"
            self.assertIsNone(P.match_profile("Odin2 Mini", t))   # ambiguous -> refuse to guess
            self.assertIsNone(P.match_profile("", t))             # blank model -> None
            self.assertIsNone(P.match_profile(None, t))

    def test_parse_sd_gb(self):
        self.assertEqual(P.parse_sd_gb("9C33-6BBD · 477G"), 477.0)
        self.assertEqual(P.parse_sd_gb("238G"), 238.0)
        self.assertEqual(P.parse_sd_gb("1T"), 1024.0)
        self.assertIsNone(P.parse_sd_gb("no SD"))
        self.assertIsNone(P.parse_sd_gb(""))

    def test_match_by_name_similarity_no_regex(self):
        # model_match BLANK -> matched purely by NAME similarity (no regex to write)
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "retroid-pocket-6-512", model="")
            self.assertEqual(P.match_profile("Retroid Pocket 6", t).name, "retroid-pocket-6-512")
            self.assertIsNone(P.match_profile("Odin2 Mini", t))          # different model -> no match

    def test_match_tier_by_sd_size(self):
        # two capacity tiers, no regex -> the device's SD size chooses the closest one
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "retroid-pocket-6-512", model="")
            make_profile(t, "retroid-pocket-6-256", model="")
            self.assertEqual(P.match_profile("Retroid Pocket 6", t, sd_gb=477).name, "retroid-pocket-6-512")
            self.assertEqual(P.match_profile("Retroid Pocket 6", t, sd_gb=238).name, "retroid-pocket-6-256")
            self.assertIsNone(P.match_profile("Retroid Pocket 6", t))    # no size -> ambiguous, assign manually

    def test_golden_presence_and_size(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)                              # writes global.meta + per-app data.tar
            self.assertTrue(prof.has_golden())
            self.assertGreater(prof.golden_size(), 0)
            d = pathlib.Path(t) / "empty"
            d.mkdir()
            (d / "profile.meta").write_text("model_match=\n")
            (d / "manifest").write_text("# empty\n")
            empty = P.Profile(d)
            self.assertFalse(empty.has_golden())               # no payload -> no golden
            self.assertEqual(empty.golden_size(), 0)

    def test_internal_for(self):
        self.assertEqual(P.internal_for("org.es_de.frontend"), "ES-DE")
        self.assertEqual(P.internal_for("com.retroarch.aarch64"), "RetroArch")
        self.assertIsNone(P.internal_for("dev.eden.eden_emulator"))

    def test_archive_is_soft_delete(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, "doomed")
            dst = P.archive_profile(prof, "20260616")
            self.assertFalse((pathlib.Path(t) / "doomed").exists())   # moved, not present
            self.assertTrue(dst.exists())                              # recoverable in _archive
            self.assertIn("_archive", str(dst))

    def test_capture_manifest_accessors_and_emulator_set(self):
        import tempfile, pathlib
        from cas import profiles as P
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "profile.meta").write_text("frontend=es-de\n")
        (d / "capture-manifest").write_text("# cap\ncom.foo\nbar.app config\n@gamelauncher on\n")
        prof = P.Profile(d)
        self.assertEqual(prof.capture_pkgs(), ["com.foo", "bar.app"])
        self.assertEqual(prof.capture_axes(), {"com.foo": (True, True), "bar.app": (False, True)})
        self.assertEqual(prof.capture_flags().get("gamelauncher"), "on")
        self.assertIn("com.retroarch.aarch64", P.EMULATOR_PKGS)        # the known emulator set exists

    def test_default_capture_checks_emulators_and_game_launcher(self):
        from cas import profiles as P
        device_apps = ["com.retroarch.aarch64", "org.ppsspp.ppsspp", "com.random.note", "com.foo.bar"]
        checked = P.default_capture_selection(device_apps, game_launcher="com.handheld.launcher")
        # emulators + game launcher on; unrelated apps off
        self.assertEqual(checked["com.retroarch.aarch64"], (True, True))
        self.assertEqual(checked["org.ppsspp.ppsspp"], (True, True))
        self.assertEqual(checked["com.random.note"], (False, False))
        self.assertEqual(checked["com.handheld.launcher"], (False, True))   # launcher = config-only

    def test_store_read_accessors(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.cocoon.app", "cocoon-1.5.0", content="bytes")
            self.assertEqual(P.store_current_label(store, "org.cocoon.app"), "cocoon-1.5.0")
            files = P.store_apk_files(store, "org.cocoon.app")
            self.assertEqual([f.name for f in files], ["cocoon-1.5.0.apk"])
            self.assertEqual(P.list_store_apks(store),
                             [{"pkg": "org.cocoon.app", "label": "cocoon-1.5.0",
                               "nfiles": 1, "bytes": len("bytes")}])

    def test_store_split_label_returns_all_apks(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"; d = store / "com.split" / "v2"; d.mkdir(parents=True)
            (d / "base.apk").write_text("a"); (d / "split_config.apk").write_text("b")
            (store / "com.split" / "meta").write_text("current=v2\n")
            self.assertEqual(sorted(f.name for f in P.store_apk_files(store, "com.split")),
                             ["base.apk", "split_config.apk"])

    def test_store_empty_and_missing(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            self.assertEqual(P.list_store_apks(store), [])                 # missing dir -> []
            self.assertIsNone(P.store_current_label(store, "nope"))
            self.assertEqual(P.store_apk_files(store, "nope"), [])

    def test_list_store_apks_sorted_by_pkg(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.zeta", "v1")
            _seed_store(store, "com.alpha", "v1")
            self.assertEqual([a["pkg"] for a in P.list_store_apks(store)], ["com.alpha", "org.zeta"])

    def test_put_defaults_label_and_sets_current(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            src = pathlib.Path(t) / "cocoon-1.5.0.apk"; src.write_text("v15")
            label = P.put_store_apk(store, "org.cocoon.app", src)
            self.assertEqual(label, "cocoon-1.5.0")                          # default label = filename stem
            self.assertEqual(P.store_current_label(store, "org.cocoon.app"), "cocoon-1.5.0")
            self.assertEqual((store / "org.cocoon.app" / "cocoon-1.5.0.apk").read_text(), "v15")

    def test_second_put_repoints_current_and_retains_prior(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            a = pathlib.Path(t) / "cocoon-1.4.0.apk"; a.write_text("v14")
            b = pathlib.Path(t) / "cocoon-1.5.0.apk"; b.write_text("v15")
            P.put_store_apk(store, "org.cocoon.app", a)
            P.put_store_apk(store, "org.cocoon.app", b)
            self.assertEqual(P.store_current_label(store, "org.cocoon.app"), "cocoon-1.5.0")
            self.assertTrue((store / "org.cocoon.app" / "cocoon-1.4.0.apk").is_file())   # prior label kept

    def test_reused_label_archives_prior_bytes(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            old = pathlib.Path(t) / "old.apk"; old.write_text("old")
            new = pathlib.Path(t) / "new.apk"; new.write_text("new")
            P.put_store_apk(store, "p", old, label="v1")
            P.put_store_apk(store, "p", new, label="v1")                     # re-use label
            self.assertEqual((store / "p" / "v1.apk").read_text(), "new")
            arch = list((store / "p" / "_archive").glob("v1.apk*"))
            self.assertEqual([a.read_text() for a in arch], ["old"])

    def test_remove_clears_current_but_keeps_files(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            src = pathlib.Path(t) / "cocoon-1.5.0.apk"; src.write_text("v15")
            P.put_store_apk(store, "org.cocoon.app", src)
            P.remove_store_apk(store, "org.cocoon.app")
            self.assertIsNone(P.store_current_label(store, "org.cocoon.app"))
            self.assertTrue((store / "org.cocoon.app" / "cocoon-1.5.0.apk").is_file())   # bytes retained
            self.assertEqual(P.list_store_apks(store), [])                               # not listed

    def test_resolve_prefers_payload_then_store_then_bundle(self):
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "p", apps=["com.captured"])                  # captured app has a payload apk
            store = pathlib.Path(t) / "store"
            self.assertEqual([f.name for f in P.resolve_app_apk("com.captured", prof, store)], ["base.apk"])
            _seed_store(store, "org.cocoon.app", "v1")
            self.assertEqual([f.name for f in P.resolve_app_apk("org.cocoon.app", prof, store)], ["v1.apk"])
            b = pathlib.Path(t) / "kit.apk"; b.write_text("x")
            self.assertEqual(P.resolve_app_apk("com.kit", prof, store, bundle_fallback=b), [b])
            self.assertIsNone(P.resolve_app_apk("com.absent", prof, store))

    def test_resolve_split_store_returns_list(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"; d = store / "com.split" / "v2"; d.mkdir(parents=True)
            (d / "base.apk").write_text("a"); (d / "split_config.apk").write_text("b")
            (store / "com.split" / "meta").write_text("current=v2\n")
            files = P.resolve_app_apk("com.split", None, store)
            self.assertEqual(sorted(f.name for f in files), ["base.apk", "split_config.apk"])

    def test_download_rows_appends_store_only_apk_on_config_off(self):
        rows = P.download_rows(["a", "b"], ["b", "store1"], saved={"a": (True, False)})
        self.assertEqual(rows, {"a": (True, False), "b": (True, True), "store1": (True, False)})


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES", "XDG_RUNTIME_DIR")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_falls_back_to_local_when_nas_unreachable(self):
        from cas import config as C, APPDIR
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")  # no config file
            os.environ.pop("CAS_PROFILES", None)
            saved = C.nas_default_path
            try:
                C.nas_default_path = lambda: str(pathlib.Path(t) / "no-nas-here")  # NAS not mounted
                self.assertEqual(C.library_root(), APPDIR / "data" / "profiles")
            finally:
                C.nas_default_path = saved

    def test_nas_default_used_when_reachable(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")  # no config file
            os.environ.pop("CAS_PROFILES", None)
            nas = pathlib.Path(t) / "nas-lib"; nas.mkdir()
            saved = C.nas_default_path
            try:
                C.nas_default_path = lambda: str(nas)
                self.assertEqual(C.library_root(), nas)          # a mounted NAS default is used by default
            finally:
                C.nas_default_path = saved

    def test_config_library_wins_over_default(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ.pop("CAS_PROFILES", None)
            # compare Path objects (not str): str(Path) renders \ on Windows, / on POSIX
            self.assertEqual(pathlib.Path(C.set_library("/mnt/nas/CAS Profiles")),
                             pathlib.Path("/mnt/nas/CAS Profiles"))
            self.assertEqual(C.load_config().get("library"), "/mnt/nas/CAS Profiles")

    def test_nas_credentials_roundtrip_and_default(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            cfgp = pathlib.Path(t) / "cas-config.json"
            os.environ["CAS_CONFIG"] = str(cfgp)
            # nothing saved -> the shipped default app account (so a fresh PC auto-connects)
            self.assertEqual(C.get_nas_credentials(), (C.NAS_DEFAULT_USER, C.NAS_DEFAULT_PW))
            # a saved account OVERRIDES the default, round-trips, and isn't written in the clear
            C.set_nas_credentials("cas_app", "P@ss w0rd!")
            self.assertEqual(C.get_nas_credentials(), ("cas_app", "P@ss w0rd!"))
            self.assertNotIn("P@ss w0rd!", cfgp.read_text())
            # clearing reverts to the shipped default
            C.set_nas_credentials("", "")
            self.assertEqual(C.get_nas_credentials(), (C.NAS_DEFAULT_USER, C.NAS_DEFAULT_PW))

    def test_device_profiles_persist(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            self.assertEqual(C.get_device_profiles(), {})
            C.set_device_profile("2ee078bd", "retroid-pocket-6-512", manual=False)   # remembered auto-match
            C.set_device_profile("ABC123", "odin2-mini", manual=True)                # operator override
            dp = C.get_device_profiles()
            self.assertEqual(dp["2ee078bd"], {"profile": "retroid-pocket-6-512", "manual": False})
            self.assertEqual(dp["ABC123"], {"profile": "odin2-mini", "manual": True})
            C.set_device_profile("ABC123", None)                                     # forget one
            self.assertNotIn("ABC123", C.get_device_profiles())
            self.assertIn("2ee078bd", C.get_device_profiles())                        # the other survives

    def test_download_stats_average_and_tracking(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            self.assertIsNone(C.download_mbps())                 # nothing recorded yet
            C.record_download(100 * 1048576, 10, profile="rp6-512", serial="2ee078bd", model="Retroid Pocket 6")
            C.record_download(100 * 1048576, 10, profile="rp6-512", serial="2ee078bd")
            self.assertAlmostEqual(C.download_mbps(), 10.0, places=3)
            # the record carries which profile + device
            rec = C.download_stats()[-1]
            self.assertEqual(rec["profile"], "rp6-512")
            self.assertEqual(rec["serial"], "2ee078bd")
            # a slower sample for ANOTHER profile -> per-profile avg prefers the matching profile's history
            C.record_download(100 * 1048576, 50, profile="odin2-mini", serial="ABC123")  # 2 MB/s
            self.assertAlmostEqual(C.download_mbps("rp6-512"), 10.0, places=3)
            self.assertAlmostEqual(C.download_mbps("odin2-mini"), 2.0, places=3)
            C.record_download(0, 5)                              # ignored (no bytes)
            C.record_download(50 * 1048576, 0)                   # ignored (no time)
            self.assertAlmostEqual(C.download_mbps("rp6-512"), 10.0, places=3)

    def test_nas_share_root(self):
        from cas import config as C
        self.assertEqual(C.nas_share_root(), r"\\192.168.100.227\01 GAMECOVE")

    def test_env_wins_over_config(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            C.set_library("/mnt/nas/lib")
            os.environ["CAS_PROFILES"] = "/tmp/override"
            self.assertEqual(pathlib.Path(C.library_root()), pathlib.Path("/tmp/override"))

    def test_corrupt_config_is_empty(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            cfg = pathlib.Path(t) / "cas-config.json"
            cfg.write_text("{ this is not json")
            os.environ["CAS_CONFIG"] = str(cfg)
            self.assertEqual(C.load_config(), {})

    def test_library_reachable(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ["CAS_PROFILES"] = t
            self.assertTrue(C.library_reachable())
            os.environ["CAS_PROFILES"] = str(pathlib.Path(t) / "nope")
            self.assertFalse(C.library_reachable())

    def test_es_media_src_set_get_clear(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ.pop("CAS_MEDIA", None)
            self.assertIsNone(C.es_media_src())                # default => SD mode (no push)
            C.set_es_media_src("/pc/ES-DE")
            self.assertEqual(C.es_media_src(), "/pc/ES-DE")
            C.set_es_media_src(None)                           # clear => back to SD mode
            self.assertIsNone(C.es_media_src())
            os.environ["CAS_MEDIA"] = "/env/wins"              # env overrides config
            C.set_es_media_src("/pc/ES-DE")
            self.assertEqual(C.es_media_src(), "/env/wins")
            os.environ.pop("CAS_MEDIA", None)

    def test_nas_share_name_and_subpath(self):
        from cas import config as C
        self.assertEqual(C.nas_share_name(), "01 GAMECOVE")
        self.assertEqual(C.nas_subpath(), "[03] SETUP/CAS Profiles")

    def test_nas_mountpoint_linux_gvfs(self):
        from cas import config as C
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            os.environ["XDG_RUNTIME_DIR"] = t
            try:
                gv = pathlib.Path(t) / "gvfs" / "smb-share:server=192.168.100.227,share=01 gamecove"
                with mock.patch.object(C.sys, "platform", "linux"):
                    self.assertIsNone(C.nas_mountpoint())     # not mounted yet
                    gv.mkdir(parents=True)
                    self.assertEqual(C.nas_mountpoint(), str(gv))
            finally:
                os.environ.pop("XDG_RUNTIME_DIR", None)

    def test_nas_mountpoint_macos_volumes(self):
        from cas import config as C
        from unittest import mock
        with mock.patch.object(C.sys, "platform", "darwin"), \
             mock.patch.object(C.pathlib.Path, "is_dir", lambda self: str(self) == "/Volumes/01 GAMECOVE"):
            self.assertEqual(C.nas_mountpoint(), "/Volumes/01 GAMECOVE")

    def test_nas_default_path_follows_mountpoint(self):
        from cas import config as C
        from unittest import mock
        with mock.patch.object(C, "nas_mountpoint", lambda: "/mnt/x/01 GAMECOVE"):
            self.assertEqual(C.nas_default_path(), "/mnt/x/01 GAMECOVE/[03] SETUP/CAS Profiles")
        with mock.patch.object(C, "nas_mountpoint", lambda: None):
            self.assertIsNone(C.nas_default_path())

    def test_library_root_local_when_nas_unmounted(self):
        from cas import config as C, APPDIR
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")
            os.environ.pop("CAS_PROFILES", None)
            with mock.patch.object(C, "nas_default_path", lambda: None):     # NEW: None case
                self.assertEqual(C.library_root(), APPDIR / "data" / "profiles")

    def test_firmware_dir_ignores_stale_override(self):
        from cas import config as C
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            cfgp = pathlib.Path(t) / "cas-config.json"
            cfgp.write_text('{"firmware_dir": "/mnt/gamecove/does-not-exist/_firmware"}')
            os.environ["CAS_CONFIG"] = str(cfgp)
            os.environ.pop("CAS_PROFILES", None)
            lib = pathlib.Path(t) / "lib"; lib.mkdir()
            with mock.patch.object(C, "library_root", lambda: lib):
                self.assertEqual(C.firmware_dir(), lib / "_firmware")     # stale override ignored

    def test_firmware_dir_honors_existing_override(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            cfgp = pathlib.Path(t) / "cas-config.json"
            real = pathlib.Path(t) / "fw"; real.mkdir()
            cfgp.write_text('{"firmware_dir": %s}' % __import__("json").dumps(str(real)))
            os.environ["CAS_CONFIG"] = str(cfgp)
            self.assertEqual(C.firmware_dir(), real)

    def _connect_cmd(self, platform):
        # Run nas_connect on a faked OS with NAS 'reachable' but share not yet mounted, capturing the
        # subprocess command it would run. nas_mountpoint() returns None (share not mounted) while
        # library_reachable() returns True (local library exists) — proving the new guard no longer
        # short-circuits on local-library presence. Returns the argv (list) of the mount command.
        from cas import config as C
        from unittest import mock
        captured = {}
        def fake_run(args, *a, **k):
            captured["argv"] = args
            class R: returncode = 0; stdout = ""; stderr = ""
            return R()
        with mock.patch.object(C.sys, "platform", platform), \
             mock.patch.object(C, "get_nas_credentials", lambda: ("u", "p w")), \
             mock.patch.object(C, "nas_reachable", lambda timeout=1.5: True), \
             mock.patch.object(C, "nas_mountpoint", lambda: None), \
             mock.patch.object(C, "library_reachable", lambda: True), \
             mock.patch.object(C.subprocess, "run", fake_run), \
             mock.patch.object(C.pathlib.Path, "mkdir", lambda self, **kw: None):
            C.nas_connect()
        return captured.get("argv")

    def test_nas_connect_attempts_mount_despite_local_library(self):
        # Regression: a local library makes library_reachable() True, but nas_connect must still try the NAS.
        argv = self._connect_cmd("linux")
        self.assertIsNotNone(argv)                      # a mount command WAS issued
        self.assertEqual(argv[:2], ["gio", "mount"])

    def test_nas_connect_macos_mounts_share(self):
        argv = self._connect_cmd("darwin")
        self.assertEqual(argv[0], "mount_smbfs")
        self.assertTrue(any("01%20GAMECOVE" in str(x) for x in argv))   # share, URL-encoded
        self.assertTrue(any(str(x).endswith("/Volumes/01 GAMECOVE") for x in argv))

    def test_nas_connect_linux_mounts_share_not_subpath(self):
        argv = self._connect_cmd("linux")
        self.assertEqual(argv[:2], ["gio", "mount"])
        self.assertEqual(argv[2], "smb://192.168.100.227/01%20GAMECOVE")  # share only, no subpath

    def test_apk_store_defaults_under_library(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")
            os.environ["CAS_PROFILES"] = str(pathlib.Path(t) / "lib")
            self.assertEqual(C.apk_store_dir(), pathlib.Path(t) / "lib" / "_apks")

    def test_apk_store_override_honored_only_if_exists(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ["CAS_PROFILES"] = str(pathlib.Path(t) / "lib")
            store = pathlib.Path(t) / "store"; store.mkdir()
            C.set_apk_store(str(store))
            self.assertEqual(C.apk_store_dir(), store)                       # exists -> honored
            C.set_apk_store(str(pathlib.Path(t) / "gone"))                   # nonexistent override
            self.assertEqual(C.apk_store_dir(), pathlib.Path(t) / "lib" / "_apks")  # ignored


class TestReleaseToken(unittest.TestCase):
    def test_default_token_when_no_override(self):
        from cas import config as C
        orig = C.load_config
        C.load_config = lambda: {}
        try:
            self.assertEqual(C.get_release_token(), C.RELEASE_TOKEN_DEFAULT)
            self.assertEqual(C.RELEASE_TOKEN_DEFAULT, "gc-release-7f3a9c2e")
        finally:
            C.load_config = orig

    def test_operator_override_wins(self):
        from cas import config as C
        orig = C.load_config
        C.load_config = lambda: {"release_token": "custom-xyz"}
        try:
            self.assertEqual(C.get_release_token(), "custom-xyz")
        finally:
            C.load_config = orig


class TestProvision(unittest.TestCase):
    def test_provision_runs_restore_and_reboot(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner()
            logs = []
            ok = PV.provision(Adb(runner=r), prof, log=logs.append, dry_push=True)
            self.assertTrue(ok)
            joined = "\n".join(r.cmds())
            self.assertIn("restore.sh", joined)
            self.assertIn("CAS_MANIFEST", joined)
            self.assertTrue(any(c[-1] == "reboot" for c in r.calls))

    def test_provision_sd_media_is_default(self):
        # no es_media_src => 'sd' mode: restore gets CAS_ES_MEDIA=sd and NO box-art push happens.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)                              # includes org.es_de.frontend
            r = FakeRunner()
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)
            self.assertTrue(ok)
            joined = "\n".join(r.cmds())
            self.assertIn("CAS_ES_MEDIA=sd", joined)
            self.assertIn("restore.sh", joined)

    def test_provision_internal_media_sets_mode(self):
        # es_media_src set => 'internal' mode: restore gets CAS_ES_MEDIA=internal.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner()
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True,
                              es_media_src=str(pathlib.Path(t) / "ES-DE"))
            self.assertTrue(ok)
            self.assertIn("CAS_ES_MEDIA=internal", "\n".join(r.cmds()))

    def test_provision_refuses_golden(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner(golden=True)
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)
            self.assertFalse(ok)
            self.assertNotIn("restore.sh", "\n".join(r.cmds()))

    def test_provision_requires_root(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            ok = PV.provision(Adb(runner=FakeRunner(root=False)), prof, log=lambda m: None, dry_push=True)
            self.assertFalse(ok)

    def test_provision_blocked_su_reports_no_root_not_golden(self):
        # Real-world failure: Magisk's grant prompt was never tapped, so every `su` BLOCKS -> timeout (124).
        # is_golden() is fail-closed, so if it runs FIRST it wrongly cries "golden lock". The user must see
        # the real cause — no root — so they grant Magisk/root instead of thinking the unit is sealed.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            logs = []
            ok = PV.provision(Adb(runner=FakeRunner(su_blocked=True)), prof, log=logs.append, dry_push=True)
            self.assertFalse(ok)
            blob = " ".join(logs).lower()
            self.assertIn("no root", blob)
            self.assertNotIn("golden", blob)

    def test_provision_all_detail_is_real_reason_not_profile_name(self):
        # The mini-report's per-device detail must carry the ACTUAL failure reason, not just the profile
        # name (which told the user nothing about WHY it failed).
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            res = PV.provision_all(lambda s: Adb(runner=FakeRunner(su_blocked=True)),
                                   [("ABC123", "device")], profile=prof, log=lambda m: None)
            status, detail = res["ABC123"]
            self.assertEqual(status, "fail")
            self.assertNotEqual(detail, prof.name)
            self.assertIn("no root", detail.lower())

    def test_provision_refuses_no_sd(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner(sd=False)
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)
            self.assertFalse(ok)
            self.assertNotIn("restore.sh", "\n".join(r.cmds()))   # never reached restore

    def test_provision_refuses_invalid_payload(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            (pathlib.Path(prof.payload) / "global.meta").unlink()       # corrupt the payload
            r = FakeRunner()
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)
            self.assertFalse(ok)
            self.assertNotIn("restore.sh", "\n".join(r.cmds()))

    def test_provision_aborts_on_push_failure(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner(push_ok=False)                              # a push fails -> must abort
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)  # real push path
            self.assertFalse(ok)
            self.assertNotIn("restore.sh", "\n".join(r.cmds()))        # never reached restore

    def test_push_es_media_packs_one_archive(self):
        # Universal fast path: pack downloaded_media into ONE tar, push that single file, unpack it on the
        # device, delete it. Verifies we do NOT do a slow per-file directory push and that both the
        # device-side archive AND the PC-side archive are cleaned up.
        with tempfile.TemporaryDirectory() as t:
            media = pathlib.Path(t) / "ES-DE" / "downloaded_media"
            (media / "nes" / "covers").mkdir(parents=True)
            (media / "nes" / "covers" / "game.jpg").write_bytes(b"img")
            r = FakeRunner()
            ok = PV.push_es_media(Adb(runner=r), log=lambda m: None, media_src=str(media))
            self.assertTrue(ok)
            joined = "\n".join(r.cmds())
            self.assertIn("tar -xf /data/local/tmp/cas_es_media.tar", joined)   # unpacked on device
            self.assertIn("rm -f /data/local/tmp/cas_es_media.tar", joined)     # device archive removed
            pushes = [c for c in r.calls if "push" in c]
            self.assertEqual(len(pushes), 1)                                    # ONE file, not a tree
            self.assertTrue(pushes[0][-2].endswith(".tar"))                     # src is the archive
            self.assertEqual(pushes[0][-1], "/data/local/tmp/cas_es_media.tar")
            self.assertEqual(list(pathlib.Path(t).rglob("cas_es_media_*.tar")), [])  # PC archive cleaned up

    def test_push_es_media_pushes_premade_archive(self):
        # If the PC source is ALREADY a single archive, push it AS-IS (no on-the-fly packing), pick the
        # gzip unpack flag for .tar.gz, and never delete the operator's stored file.
        with tempfile.TemporaryDirectory() as t:
            md = pathlib.Path(t) / "downloaded_media"
            (md / "nes").mkdir(parents=True)
            (md / "nes" / "a.jpg").write_bytes(b"x")
            arc = pathlib.Path(t) / "es-de-media.tar.gz"
            with tarfile.open(arc, "w:gz") as tar:
                tar.add(str(md), arcname="downloaded_media")
            r = FakeRunner()
            ok = PV.push_es_media(Adb(runner=r), log=lambda m: None, media_src=str(arc))
            self.assertTrue(ok)
            joined = "\n".join(r.cmds())
            self.assertIn("tar -xzf /data/local/tmp/cas_es_media.tar", joined)  # gzip unpack flag
            pushes = [c for c in r.calls if "push" in c]
            self.assertEqual(len(pushes), 1)                                    # pushed ONE file
            self.assertTrue(pushes[0][-2].endswith(".tar.gz"))                  # the archive itself, as-is
            self.assertTrue(arc.exists())                                       # operator's file untouched

    def test_push_es_media_skips_when_present(self):
        # A re-provision must NOT re-pack/re-push 12 GB if the device already has the box art.
        with tempfile.TemporaryDirectory() as t:
            media = pathlib.Path(t) / "downloaded_media"
            (media / "nes").mkdir(parents=True)
            (media / "nes" / "a.jpg").write_bytes(b"x")

            class Present(FakeRunner):
                def __call__(self, args, input_text=None, timeout=900):
                    if "shell" in args and args[-1].startswith("ls /storage"):
                        return 0, "/storage/emulated/0/ES-DE/downloaded_media\n", ""
                    return super().__call__(args, input_text, timeout)
            r = Present()
            ok = PV.push_es_media(Adb(runner=r), log=lambda m: None, media_src=str(media))
            self.assertTrue(ok)
            self.assertFalse([c for c in r.calls if "push" in c])              # nothing pushed
            self.assertNotIn("tar -xf", "\n".join(r.cmds()))                   # nothing unpacked

    def test_provision_all_isolates_exception(self):
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "odin2mini", "Odin2 ?Mini")

            def boom(s):
                raise RuntimeError("device fault")
            res = PV.provision_all(boom, [("ABC123", "device")], root=t, log=lambda m: None)
            self.assertEqual(res["ABC123"][0], "error")                # isolated, not raised

    def test_batch_auto_matches_and_skips_unauthorized(self):
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "odin2mini", "Odin2 ?Mini")
            shared = FakeRunner()
            res = PV.provision_all(
                lambda s: Adb(serial=s, runner=shared),
                [("ABC123", "device"), ("DEF456", "unauthorized")],
                root=t, log=lambda m: None,
            )
            self.assertEqual(res["ABC123"], ("ok", "odin2mini"))
            self.assertEqual(res["DEF456"][0], "skip")

    def test_download_run_logged_to_library(self):
        # the WHOLE Download is recorded (total length + each device/profile) to the library, every run
        import json
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")   # isolate: no log_dir override
            try:
                make_profile(t, "odin2mini", "Odin2 ?Mini")
                res = PV.provision_all(lambda s: Adb(serial=s, runner=FakeRunner()),
                                       [("ABC123", "device")], root=t, log=lambda m: None,
                                       profile_map={"ABC123": P.Profile(pathlib.Path(t) / "odin2mini")})
                self.assertEqual(res["ABC123"][0], "ok")
                hist = pathlib.Path(t) / "download-history.jsonl"
                self.assertTrue(hist.exists())              # written into the library dir (no log_dir set)
                rec = json.loads(hist.read_text().splitlines()[-1])
                self.assertEqual(rec["ok"], 1)
                self.assertIn("total_bytes", rec)
                self.assertIn("total_secs", rec)
                self.assertEqual(rec["devices"][0]["serial"], "ABC123")
                self.assertEqual(rec["devices"][0]["profile"], "odin2mini")
            finally:
                os.environ.pop("CAS_CONFIG", None)

    def test_append_history_writes_jsonl(self):
        # the shared run-history appender used by BOTH download-history.jsonl and save-history.jsonl
        import json
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")   # isolate: no log_dir set
            try:
                PV._append_history(t, "save-history.jsonl", {"profile": "rp6-512", "bytes": 123}, log=lambda m: None)
                PV._append_history(t, "save-history.jsonl", {"profile": "odin2", "bytes": 456}, log=lambda m: None)
                lines = (pathlib.Path(t) / "save-history.jsonl").read_text().splitlines()
                self.assertEqual(len(lines), 2)
                self.assertEqual(json.loads(lines[0])["profile"], "rp6-512")
                self.assertEqual(json.loads(lines[1])["bytes"], 456)
            finally:
                os.environ.pop("CAS_CONFIG", None)

    def test_append_history_routes_to_log_dir(self):
        # A configured + reachable shared log_dir (e.g. the NAS) receives the run history, NOT the library
        # root — so logs centralize across benches while goldens stay on a fast LOCAL library. An unreachable
        # log_dir falls back to the library root so a run is never lost.
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            lib = pathlib.Path(t) / "lib"; lib.mkdir()
            nas = pathlib.Path(t) / "nas"; nas.mkdir()
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            try:
                C.set_log_dir(str(nas))
                PV._append_history(str(lib), "download-history.jsonl", {"ok": 1}, log=lambda m: None)
                self.assertTrue((nas / "download-history.jsonl").exists())     # landed on the NAS log dir
                self.assertFalse((lib / "download-history.jsonl").exists())    # NOT the library root
                C.set_log_dir(str(pathlib.Path(t) / "gone"))                   # unreachable -> graceful fallback
                PV._append_history(str(lib), "save-history.jsonl", {"ok": 1}, log=lambda m: None)
                self.assertTrue((lib / "save-history.jsonl").exists())         # fell back to the library root
            finally:
                os.environ.pop("CAS_CONFIG", None)

    def test_capture_to_pc_invokes_capture(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t,
                             log=lambda m: None, dry_pull=True)
            self.assertIn("capture.sh", "\n".join(r.cmds()))
            self.assertIn("CAS_OUT=/data/local/tmp", "\n".join(r.cmds()))

    def test_capture_to_pc_passes_capture_manifest_when_present(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            pdir = pathlib.Path(t) / "newprof"; pdir.mkdir()
            (pdir / "capture-manifest").write_text("# cap\ncom.foo\n")
            PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t, log=lambda m: None, dry_pull=True)
            joined = "\n".join(r.cmds())
            self.assertIn("CAS_MANIFEST=/data/local/tmp/cas_scripts/capture-manifest", joined)

    def test_capture_to_pc_no_manifest_captures_all(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t, log=lambda m: None, dry_pull=True)
            self.assertNotIn("CAS_MANIFEST=", "\n".join(r.cmds()))   # back-compat: capture-all

    def test_seed_default_manifest_populates_empty_placeholder(self):
        # A 'New profile' leaves a placeholder manifest with NO app lines; after the first capture the
        # selection must be seeded from the captured app set (both axes on) so the Apps tab shows the
        # downloaded apps ticked — and Download has apps to restore.
        with tempfile.TemporaryDirectory() as t:
            pdir = pathlib.Path(t) / "mangmi-air-x-256"
            (pdir / "golden_root_payload").mkdir(parents=True)
            (pdir / "golden_root_payload" / "pkglist.txt").write_text("com.a\ncom.b\n")
            (pdir / "manifest").write_text("# mangmi-air-x-256 (empty — capture a golden to populate)\n")
            PV.seed_default_manifest(pdir, "mangmi-air-x-256")
            self.assertEqual(P.manifest_pkgs(pdir / "manifest"), ["com.a", "com.b"])
            self.assertEqual(P.manifest_axes(pdir / "manifest"),
                             {"com.a": (True, True), "com.b": (True, True)})

    def test_seed_default_manifest_preserves_existing_selection(self):
        # If the operator already refined a real selection (manifest has app lines), seeding must NOT
        # clobber it on a re-capture.
        with tempfile.TemporaryDirectory() as t:
            pdir = pathlib.Path(t) / "prof"
            (pdir / "golden_root_payload").mkdir(parents=True)
            (pdir / "golden_root_payload" / "pkglist.txt").write_text("com.a\ncom.b\n")
            (pdir / "manifest").write_text("# prof\ncom.a config\n@settings on\n")
            PV.seed_default_manifest(pdir, "prof")
            self.assertEqual(P.manifest_pkgs(pdir / "manifest"), ["com.a"])          # unchanged
            self.assertEqual(P.manifest_axes(pdir / "manifest"), {"com.a": (False, True)})

    def test_patch_init_boot_on_device_pushes_toolkit_and_pulls(self):
        # On-device patch: push the aarch64 toolkit + the stock init_boot, run boot_patch.sh, pull the
        # patched new-boot.img back to the PC. No root needed (it only rewrites the image file).
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            stock = pathlib.Path(t) / "stock.img"
            stock.write_bytes(b"x")
            ok = PV.patch_init_boot_on_device(Adb(runner=r), stock, pathlib.Path(t) / "patched.img",
                                              log=lambda m: None)
        self.assertTrue(ok)
        cmds = "\n".join(r.cmds())
        self.assertIn("boot_patch.sh", cmds)              # ran Magisk's patcher on the device
        self.assertIn("push", cmds)                       # pushed toolkit + stock
        self.assertIn("pull", cmds)                       # pulled new-boot.img back

    def test_patch_init_boot_on_device_fails_without_sentinel(self):
        # No CAS_PATCH_OK in boot_patch.sh output -> treat as failure (don't flash a non-patched image).
        class _NoPatch(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                if "shell" in args and "boot_patch.sh" in args[-1]:
                    return 1, "- aborting\n", "magiskboot: not executable"
                return super().__call__(args, input_text, timeout)
        with tempfile.TemporaryDirectory() as t:
            stock = pathlib.Path(t) / "stock.img"
            stock.write_bytes(b"x")
            ok = PV.patch_init_boot_on_device(Adb(runner=_NoPatch()), stock,
                                              pathlib.Path(t) / "patched.img", log=lambda m: None)
        self.assertFalse(ok)


class FbRunner:
    def __init__(self, flash_ok=True):
        self.calls = []
        self.flash_ok = flash_ok

    def __call__(self, args, input_text=None, timeout=900):
        self.calls.append(list(args))
        if args[-1] == "devices":
            return 0, "ABC123 fastboot\n", ""
        if "flash" in args:
            return (0, "OKAY\n", "") if self.flash_ok else (1, "", "flash failed")
        return 0, "OKAY\n", ""

    def cmds(self):
        return [" ".join(c) for c in self.calls]


class TestSeal(unittest.TestCase):
    def test_seal_sequence(self):
        ra, fb = FakeRunner(), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertTrue(ok)
        a = "\n".join(ra.cmds())
        self.assertIn("development_settings_enabled 0", a)        # dev mode off
        self.assertIn("pm uninstall com.topjohnwu.magisk", a)     # Magisk removed
        self.assertIn("adb_enabled 0", a)                         # USB debugging off (last)
        self.assertIn("flash init_boot_a", "\n".join(fb.cmds()))  # un-rooted via stock init_boot
        # USB-debugging-off must be the LAST adb command issued
        self.assertIn("adb_enabled 0", ra.cmds()[-1])

    def test_seal_flashes_detected_active_slot(self):
        # Un-root must flash STOCK to the ACTIVE slot — init_boot_b on a slot-B unit, not a hardcoded _a.
        ra, fb = FakeRunner(slot="_b"), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertTrue(ok)
        self.assertIn("flash init_boot_b", "\n".join(fb.cmds()))
        self.assertNotIn("flash init_boot_a", "\n".join(fb.cmds()))

    def test_seal_hides_dev_options_even_without_root(self):
        # Regression: Developer Options must be hidden even when is_root() is False (a flaky su grant
        # used to skip the dev-options disable, leaving it visible — user-reported). It now runs via the
        # shell uid at the final lockdown step, unconditionally.
        ra, fb = FakeRunner(root=False), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertTrue(ok)
        self.assertIn("development_settings_enabled 0", "\n".join(ra.cmds()))  # hidden despite no root
        self.assertIn("adb_enabled 0", ra.cmds()[-1])                          # USB debugging still last

    def test_seal_refuses_golden(self):
        ra, fb = FakeRunner(root=True, golden=True), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertFalse(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))   # golden master is NEVER un-rooted/sealed

    def test_seal_aborts_if_stock_missing(self):
        ok = PV.seal(Adb(runner=FakeRunner()), Fastboot(runner=FbRunner()),
                     "/nonexistent/init_boot.img", log=lambda m: None, wait=False)
        self.assertFalse(ok)

    def test_seal_refuses_model_mismatch(self):
        ra, fb = FakeRunner(model="Odin2 Mini"), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock,
                         log=lambda m: None, wait=False, model_match="Mangmi|Air ?X")
        finally:
            os.unlink(stock)
        self.assertFalse(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))   # never flashed the wrong-model image

    def test_seal_no_strand_on_flash_fail(self):
        ra, fb = FakeRunner(), FbRunner(flash_ok=False)
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertFalse(ok)                              # failed flash -> not sealed
        self.assertIn("reboot", "\n".join(fb.cmds()))     # rebooted out of fastboot (not stranded)
        self.assertNotIn("adb_enabled 0", "\n".join(ra.cmds()))  # USB debugging NOT disabled


class TestRoot(unittest.TestCase):
    def _pc_assets(self):
        """A Magisk-patched init_boot + a Magisk apk sitting on the PC."""
        pf = tempfile.NamedTemporaryFile(suffix=".img", delete=False); pf.write(b"x"); pf.close()
        af = tempfile.NamedTemporaryFile(suffix=".apk", delete=False); af.write(b"x"); af.close()
        return pf.name, af.name

    def test_root_flashes_patched_and_installs_magisk_from_pc(self):
        ra, fb = FakeRunner(root=False), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertTrue(ok)
        self.assertIn("flash init_boot_a", "\n".join(fb.cmds()))     # flashed the PATCHED init_boot
        self.assertIn("reboot bootloader", "\n".join(ra.cmds()))     # entered fastboot
        a = "\n".join(ra.cmds())
        self.assertIn("install", a)                                  # installed the Magisk app...
        self.assertIn(apk, a)                                        # ...from the PC apk path (not the SD)

    def test_root_installs_magisk_before_patching(self):
        # Magisk-first: the app install must happen BEFORE the on-device init_boot patch (and the flash).
        ra, fb = FakeRunner(root=False), FbRunner()
        stock, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), stock, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(stock); os.unlink(apk)
        self.assertTrue(ok)
        cmds = ra.cmds()
        install_i = next(i for i, c in enumerate(cmds) if "install" in c)
        patch_i = next(i for i, c in enumerate(cmds) if "boot_patch.sh" in c)
        self.assertLess(install_i, patch_i)                         # Magisk app installed FIRST
        self.assertIn("flash init_boot_a", "\n".join(fb.cmds()))    # then flashed the on-device-patched img

    def test_root_refuses_when_stock_missing(self):
        # No stock init_boot on the PC -> can't patch -> refuse, and never flash.
        ra, fb = FakeRunner(root=False), FbRunner()
        _, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), "/nonexistent/stock.img",
                         magisk_apk=apk, log=lambda m: None, wait=False)
        finally:
            os.unlink(apk)
        self.assertFalse(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))

    def test_root_flashes_detected_active_slot(self):
        # On a unit booted to slot B, root MUST flash init_boot_b — a hardcoded _a would patch the IDLE
        # slot and leave the unit unrooted. The target is detected from the device before fastboot.
        ra, fb = FakeRunner(root=False, slot="_b"), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertTrue(ok)
        self.assertIn("flash init_boot_b", "\n".join(fb.cmds()))
        self.assertNotIn("flash init_boot_a", "\n".join(fb.cmds()))

    def test_root_refuses_model_mismatch(self):
        ra, fb = FakeRunner(model="Retroid Pocket 6", root=False), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False, model_match="Odin2 ?Mini")
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertFalse(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))              # never flashed the wrong-model image

    def test_root_force_proceeds_on_mismatch(self):
        ra, fb = FakeRunner(model="Retroid Pocket 6", root=False), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False, model_match="Odin2 ?Mini", force=True)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertTrue(ok)
        self.assertIn("flash init_boot_a", "\n".join(fb.cmds()))     # FORCED past mismatch -> still flashed

    def test_root_no_strand_on_flash_fail(self):
        ra, fb = FakeRunner(root=False), FbRunner(flash_ok=False)
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertFalse(ok)
        self.assertIn("reboot", "\n".join(fb.cmds()))                # rebooted out of fastboot (not stranded)

    def test_root_refuses_golden(self):
        ra, fb = FakeRunner(root=True, golden=True), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertFalse(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))              # golden is never re-flashed

    def test_root_skips_flash_if_already_rooted(self):
        ra, fb = FakeRunner(root=True, golden=False), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertTrue(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))              # already rooted -> no re-flash
        self.assertNotIn("install", "\n".join(ra.cmds()))            # and NO Magisk re-install (fast no-op)


class TestBatch(unittest.TestCase):
    def _profile_with_imgs(self, t, name="odin2mini", model="Odin2 ?Mini"):
        prof = make_profile(t, name, model)
        meta = pathlib.Path(t) / name / "profile.meta"
        meta.write_text(meta.read_text() + "patched_init_boot=p.img\nmagisk_apk=m.apk\n")
        (pathlib.Path(t) / "p.img").write_bytes(b"x")
        (pathlib.Path(t) / "m.apk").write_bytes(b"x")
        sf = pathlib.Path(t) / "provision" / "root" / "firmware" / "odin2_20231201" / "init_boot.img"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_bytes(b"x")
        return prof

    def test_root_all_auto_matches_and_skips_unauthorized(self):
        with tempfile.TemporaryDirectory() as t:
            self._profile_with_imgs(t)
            res = PV.root_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Odin2 Mini", root=True)),
                lambda s: Fastboot(serial=s, runner=FbRunner()),
                [("ABC123", "device"), ("DEF456", "unauthorized")],
                profiles_root=t, appdir=t, log=lambda m: None)
            self.assertEqual(res["ABC123"], ("ok", "odin2mini"))    # matched -> rooted (already-rooted path)
            self.assertEqual(res["DEF456"][0], "skip")              # unauthorized skipped

    def test_root_all_uses_default_images_when_profile_unset(self):
        # A profile with NO stock_init_boot/magisk_apk must NOT skip — it falls back to the bundled default
        # kit, so ⓪ Root works fleet-wide with no per-profile picking.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, "p", "Odin2 ?Mini")
            (prof.path / "profile.meta").write_text("model_match=Odin2 ?Mini\n")   # no root images set
            res = PV.root_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Odin2 Mini", root=True)),
                lambda s: Fastboot(serial=s, runner=FbRunner()),
                [("ABC123", "device")], profiles_root=t, appdir=t, log=lambda m: None)
            self.assertEqual(res["ABC123"][0], "ok")        # used the default kit — did NOT skip 'no-init_boot'

    def test_default_root_images_exist(self):
        # The bundled default kit images must actually ship (guard against a rename/move breaking ⓪ Root).
        from cas import APPDIR
        self.assertTrue((pathlib.Path(APPDIR) / PV.DEFAULT_STOCK_INIT_BOOT).exists(), PV.DEFAULT_STOCK_INIT_BOOT)
        self.assertTrue((pathlib.Path(APPDIR) / PV.DEFAULT_MAGISK_APK).exists(), PV.DEFAULT_MAGISK_APK)

    def test_provision_all_uses_selected_profile_over_model(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, "odin2mini", "Odin2 ?Mini")     # matches Odin, NOT Retroid
            # device reports a model that would NOT auto-match — the SELECTED profile must win anyway.
            res = PV.provision_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Retroid Pocket 6", root=True, sd=True)),
                [("ABC123", "device")], root=t, log=lambda m: None, profile=prof)
            self.assertEqual(res["ABC123"], ("ok", "odin2mini"))   # used the selected profile, ignored model

    def test_root_all_parallel_processes_every_device(self):
        with tempfile.TemporaryDirectory() as t:
            self._profile_with_imgs(t)
            # two 'device'-state units, each its OWN runner (already-rooted -> root() early-returns ok);
            # parallel=True (default) must process BOTH.
            res = PV.root_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Odin2 Mini", root=True)),
                lambda s: Fastboot(serial=s, runner=FbRunner()),
                [("DEV1", "device"), ("DEV2", "device")], profiles_root=t, appdir=t, log=lambda m: None)
            self.assertEqual(set(res), {"DEV1", "DEV2"})            # both handled in parallel
            self.assertTrue(all(v[0] == "ok" for v in res.values()))

    def test_seal_all_auto_matches_and_attempts_flash(self):
        with tempfile.TemporaryDirectory() as t:
            self._profile_with_imgs(t)
            fbs = {}

            def mkfb(s):
                fbs[s] = FbRunner()
                return Fastboot(serial=s, runner=fbs[s])
            PV.seal_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Odin2 Mini", root=True)),
                mkfb, [("ABC123", "device")], profiles_root=t, appdir=t, log=lambda m: None)
            self.assertIn("flash init_boot_a", "\n".join(fbs["ABC123"].cmds()))  # matched -> stock flash issued

    def test_seal_all_uses_default_init_boot_when_profile_unset(self):
        # A profile with NO stock_init_boot must NOT skip Lock — it falls back to the bundled default kit
        # (mirroring Root), so ③ Lock un-roots fleet-wide with no per-profile picking. Regression guard for
        # the root_all/seal_all asymmetry that skipped 'no-init_boot' on default-kit profiles.
        from cas import APPDIR
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, "p", "Odin2 ?Mini")
            (prof.path / "profile.meta").write_text("model_match=Odin2 ?Mini\n")   # no root images set
            fbs = {}

            def mkfb(s):
                fbs[s] = FbRunner()
                return Fastboot(serial=s, runner=fbs[s])
            res = PV.seal_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Odin2 Mini", root=True)),
                mkfb, [("ABC123", "device")], profiles_root=t, appdir=APPDIR, log=lambda m: None)
            self.assertNotEqual(res["ABC123"][0], "no-init_boot")               # did NOT skip
            self.assertIn("flash init_boot", "\n".join(fbs["ABC123"].cmds()))   # used default kit -> stock flash

    def test_root_all_profile_map_routes_per_device_and_skips_none(self):
        # profile_map overrides auto-match: a device whose MODEL matches nothing still gets the mapped
        # profile; a serial mapped to None is skipped (not auto-matched).
        with tempfile.TemporaryDirectory() as t:
            from cas import profiles as P
            self._profile_with_imgs(t, name="odin2mini", model="Odin2 ?Mini")
            pm = {"DEV1": P.Profile(pathlib.Path(t) / "odin2mini"), "DEV2": None}
            res = PV.root_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Nomatch", root=True)),
                lambda s: Fastboot(serial=s, runner=FbRunner()),
                [("DEV1", "device"), ("DEV2", "device")],
                profiles_root=t, appdir=t, log=lambda m: None, profile_map=pm)
            self.assertEqual(res["DEV1"][0], "ok")           # mapped profile used despite model not matching
            self.assertEqual(res["DEV2"][0], "no-profile")   # None in the map -> skipped


class TestEsMedia(unittest.TestCase):
    """The shared ES-DE box-art layer (downloaded_media) pushes from the PC, kept out of the golden."""
    class _MR(FakeRunner):
        def __init__(self, has_media=False, **kw):
            super().__init__(**kw)
            self.has_media = has_media

        def __call__(self, args, **kw):
            if "shell" in args and args[-1].startswith("ls ") and "downloaded_media" in args[-1]:
                return 0, ("downloaded_media\n" if self.has_media else ""), ""
            return super().__call__(args, **kw)

    def _media(self, t):
        m = pathlib.Path(t) / "downloaded_media" / "gba"
        m.mkdir(parents=True)
        (m / "x.png").write_bytes(b"x")
        return pathlib.Path(t) / "downloaded_media"

    def test_pushes_when_present_and_device_empty(self):
        with tempfile.TemporaryDirectory() as t:
            src = self._media(t)
            r = self._MR(has_media=False)
            self.assertTrue(PV.push_es_media(Adb(runner=r), log=lambda m: None, media_src=str(src)))
            # new transfer: pack -> push ONE .tar -> unpack on device (not a per-file push of the src dir)
            self.assertTrue(any("push" in c for c in r.calls))
            self.assertTrue(any("tar -xf" in " ".join(c) for c in r.calls))

    def test_skips_when_device_already_has_media(self):
        with tempfile.TemporaryDirectory() as t:
            src = self._media(t)
            r = self._MR(has_media=True)
            self.assertTrue(PV.push_es_media(Adb(runner=r), log=lambda m: None, media_src=str(src)))
            self.assertFalse(any("push" in c for c in r.calls))     # already there -> no 12 GB re-push

    def test_noop_when_no_source(self):
        with tempfile.TemporaryDirectory() as t:
            r = self._MR()
            self.assertFalse(PV.push_es_media(Adb(runner=r), log=lambda m: None,
                                              media_src=str(pathlib.Path(t) / "nope")))
            self.assertFalse(any("push" in c for c in r.calls))


class TestCompanionInstall(unittest.TestCase):
    """The GameCove Companion is a normal golden app; when it's in the manifest the PC-side install
    (adb install) refreshes it to the current PC build after restore. Not in the manifest -> skipped."""

    def test_installs_from_pc_when_apk_present(self):
        with tempfile.TemporaryDirectory() as t:
            apk = pathlib.Path(t) / "gamecove-companion.apk"
            apk.write_bytes(b"x")
            r = FakeRunner()
            self.assertTrue(PV.install_companion(Adb(runner=r), log=lambda m: None, apk_src=str(apk)))
            a = "\n".join(r.cmds())
            self.assertIn("install", a)            # adb install ...
            self.assertIn(str(apk), a)             # ...from the PC apk path (never the SD)

    def test_noop_when_apk_absent(self):
        r = FakeRunner()
        self.assertFalse(PV.install_companion(
            Adb(runner=r), log=lambda m: None, apk_src="/no/such/companion.apk"))
        self.assertNotIn("install", "\n".join(r.cmds()))

    def test_provision_installs_companion_when_in_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.es_de.frontend", PV.COMPANION_PKG])  # Companion ticked
            apk = pathlib.Path(t) / "gamecove-companion.apk"
            apk.write_bytes(b"x")
            os.environ["CAS_COMPANION_APK"] = str(apk)
            try:
                r = FakeRunner()
                ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)   # full (non-dry) path
            finally:
                os.environ.pop("CAS_COMPANION_APK", None)
            self.assertTrue(ok)
            a = "\n".join(r.cmds())
            self.assertIn("install", a)            # companion installed during provisioning
            self.assertIn(str(apk), a)             # ...from the current PC build

    def test_provision_skips_companion_when_not_in_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)                 # default apps: Companion NOT ticked / not in manifest
            apk = pathlib.Path(t) / "gamecove-companion.apk"
            apk.write_bytes(b"x")
            os.environ["CAS_COMPANION_APK"] = str(apk)
            try:
                r = FakeRunner()
                ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)
            finally:
                os.environ.pop("CAS_COMPANION_APK", None)
            self.assertTrue(ok)
            self.assertNotIn(str(apk), "\n".join(r.cmds()))        # not in manifest -> NOT installed


class TestUpdater(unittest.TestCase):
    """Self-update check against the public GitHub Release latest.json. Network is injected."""

    def _opener(self, manifest):
        import io, json
        data = json.dumps(manifest).encode()
        return lambda url, timeout=0: io.BytesIO(data)

    def test_is_newer_semver(self):
        from cas import updater as U
        self.assertTrue(U.is_newer("0.2.0", "0.1.0"))
        self.assertTrue(U.is_newer("1.0.0", "0.9.9"))
        self.assertTrue(U.is_newer("v0.2.0", "0.1.0"))   # tolerate a leading v
        self.assertFalse(U.is_newer("0.1.0", "0.1.0"))
        self.assertFalse(U.is_newer("0.1.0", "0.2.0"))

    def test_check_returns_update_for_this_os_when_newer(self):
        from cas import updater as U
        man = {"version": "0.2.0", "notes": "new",
               "assets": {"windows": {"url": "http://x/cas-windows.zip", "sha256": "win"},
                          "linux": {"url": "http://x/cas-linux.zip", "sha256": "lin"},
                          "macos": {"url": "http://x/cas-macos.zip", "sha256": "mac"}}}
        up = U.check("0.1.0", opener=self._opener(man), os_name="linux")
        self.assertEqual(up["version"], "0.2.0")
        self.assertEqual(up["url"], "http://x/cas-linux.zip")
        self.assertEqual(up["sha256"], "lin")

    def test_check_none_when_not_newer(self):
        from cas import updater as U
        man = {"version": "0.1.0", "assets": {"linux": {"url": "u", "sha256": "s"}}}
        self.assertIsNone(U.check("0.1.0", opener=self._opener(man), os_name="linux"))

    def test_check_none_when_no_asset_for_os(self):
        from cas import updater as U
        man = {"version": "0.2.0", "assets": {"windows": {"url": "u", "sha256": "s"}}}
        self.assertIsNone(U.check("0.1.0", opener=self._opener(man), os_name="linux"))

    def test_check_never_raises_on_network_error(self):
        from cas import updater as U
        def boom(url, timeout=0):
            raise OSError("offline")
        self.assertIsNone(U.check("0.1.0", opener=boom, os_name="linux"))

    # ---- download progress (issue #2: a real progress indicator) ----
    def test_download_and_verify_reports_progress(self):
        """download_and_verify streams and calls progress(done, total) so the GUI can show a real %."""
        import io
        from cas import updater as U
        payload = b"x" * (1 << 17)            # 128 KiB -> two 64 KiB chunks
        class _Resp(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}
            def __enter__(self): return self
            def __exit__(self, *a): self.close()
        seen = []
        dest = os.path.join(tempfile.mkdtemp(), "u.zip")
        out = U.download_and_verify("http://x/u.zip", dest,
                                    opener=lambda url, timeout=0: _Resp(payload),
                                    progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(out, dest)
        self.assertTrue(seen, "progress callback was never called")
        self.assertEqual(seen[-1], (len(payload), len(payload)))   # ends at 100%
        self.assertTrue(all(t == len(payload) for _, t in seen))   # total reported every time

    # ---- stage_and_relaunch must not silently no-op when there is nothing to swap ----
    def test_stage_refuses_when_no_executable_to_swap(self):
        """Source checkout / wrong layout: appdir has no cas-gui[.exe]. The old code launched a helper
        and returned True (silent no-op -> 'updates but stays the same'). It must now fail loudly."""
        from cas import updater as U
        appdir = tempfile.mkdtemp()                       # NO cas-gui here (mimics `python -m cas`)
        zp = os.path.join(tempfile.mkdtemp(), "cas.zip")
        import zipfile
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("cas/cas-gui", "NEW")
        launched = []
        ok = U.stage_and_relaunch(zp, appdir=appdir, log=lambda *a: None,
                                  platform="linux", launch=lambda *a, **k: launched.append(a))
        self.assertFalse(ok, "must not claim success when there is no executable to replace")
        self.assertEqual(launched, [], "must not launch a swap helper when there's nothing to swap")

    # ---- the Windows helper must be robust when run console-less (the real bench bug) ----
    def test_windows_helper_does_not_use_console_dependent_timeout(self):
        """The helper is launched DETACHED from a windowed (console=False) exe, so `timeout` aborts with
        'Input redirection is not supported' and the wait loop misbehaves. Use a console-independent wait."""
        from cas import updater as U
        script = U._write_helper(pathlib.Path(r"C:\app\dist\cas"),
                                 pathlib.Path(r"C:\stage\cas"), platform="win32", log=lambda *a: None)
        text = pathlib.Path(script).read_text()
        self.assertNotIn("timeout ", text, "timeout needs a console; fails when launched detached")
        self.assertIn("ping", text.lower(), "expected a console-independent delay (ping -n)")

    def test_windows_helper_relaunches_gui_and_logs(self):
        """The Windows helper relaunches cas-gui.exe, copies the new bundle, and writes a diagnostic log
        so a failed swap is no longer a silent black box."""
        from cas import updater as U
        script = U._write_helper(pathlib.Path(r"C:\app\dist\cas"),
                                 pathlib.Path(r"C:\stage\cas"), platform="win32", log=lambda *a: None)
        text = pathlib.Path(script).read_text()
        self.assertIn("cas-gui.exe", text)               # relaunches the GUI exe
        self.assertIn("robocopy", text.lower())          # still an overwrite-copy (never purge siblings)
        self.assertIn("cas-update.log", text)            # leaves a breadcrumb we can read on the bench

    # ---- end-to-end swap via the real unix helper (regression guard for the part that works) ----
    @unittest.skipIf(sys.platform.startswith("win"), "unix helper")
    def test_unix_helper_swaps_bundle_and_preserves_siblings(self):
        from cas import updater as U
        import subprocess, time, zipfile
        root = tempfile.mkdtemp()
        appdir = pathlib.Path(root, "appdir"); (appdir / "_internal").mkdir(parents=True)
        (appdir / "cas-gui").write_text("OLD"); (appdir / "_internal" / "V").write_text("0.1.0")
        (appdir / "data" / "profiles").mkdir(parents=True); (appdir / "data" / "profiles" / "keep").write_text("precious")
        zp = pathlib.Path(root, "cas.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("cas/cas-gui", "NEW"); z.writestr("cas/_internal/V", "0.2.1")
        dead = subprocess.Popen(["true"]); dead.wait()
        captured = {}
        orig = os.getpid; os.getpid = lambda: dead.pid
        try:
            ok = U.stage_and_relaunch(str(zp), appdir=str(appdir), log=lambda *a: None, platform="linux",
                                      launch=lambda helper, **k: captured.setdefault("h", helper))
        finally:
            os.getpid = orig
        self.assertTrue(ok)
        subprocess.run(["/bin/sh", str(captured["h"])], timeout=15)   # run helper directly (pid already dead)
        self.assertEqual((appdir / "_internal" / "V").read_text(), "0.2.1")   # swap took
        self.assertEqual((appdir / "cas-gui").read_text(), "NEW")
        self.assertEqual((appdir / "data" / "profiles" / "keep").read_text(), "precious")  # sibling untouched


class TestEdl(unittest.TestCase):
    GEOM = {"sector_size": "512", "num_sectors": "16384", "partition": "0",
            "start_sector": "16449552", "start_byte_hex": "0x1f6002000"}

    def _runner(self, sahara_ok=True, fh_ok=True):
        calls = []

        def runner(args, input_text=None, timeout=900):
            calls.append(list(args))
            if args[0].endswith("QSaharaServer"):
                return (0, "Sahara protocol completed\nFile transferred successfully\n", "") if sahara_ok \
                    else (0, "ERROR: Could not connect to /dev/ttyUSB0\n", "")
            if args[0].endswith("fh_loader"):
                return (0, "{All Finished Successfully}\n", "") if fh_ok else (0, "FAILED\n", "")
            return 0, "", ""
        return runner, calls

    def test_rawprogram_xml_has_geometry(self):
        from cas.adb import Edl
        xml = Edl.rawprogram_xml("init_boot_b", "patched.img", self.GEOM)
        self.assertIn('label="init_boot_b"', xml)
        self.assertIn('filename="patched.img"', xml)
        self.assertIn('start_sector="16449552"', xml)
        self.assertIn('physical_partition_number="0"', xml)

    def test_flash_partition_success_targets_real_args(self):
        from cas.adb import Edl
        runner, calls = self._runner()
        with tempfile.TemporaryDirectory() as td:
            img = pathlib.Path(td) / "patched.img"; img.write_bytes(b"x" * 32)
            wd = pathlib.Path(td) / "wd"
            edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/prog.elf", runner=runner)
            self.assertTrue(edl.flash_partition("/dev/ttyUSB0", "init_boot_b", str(img), self.GEOM, str(wd)))
            xml = (wd / "rawprogram_init_boot_b.xml").read_text()
            self.assertIn('start_sector="16449552"', xml)
            self.assertTrue((wd / "patched.img").exists())                 # image staged for --search_path
            fh = [c for c in calls if c[0].endswith("fh_loader")][0]
            self.assertIn("--port=/dev/ttyUSB0", fh)
            self.assertIn("--memoryname=eMMC", fh)

    def test_flash_partition_fails_when_sahara_cannot_connect(self):
        from cas.adb import Edl
        runner, _ = self._runner(sahara_ok=False)
        with tempfile.TemporaryDirectory() as td:
            img = pathlib.Path(td) / "p.img"; img.write_bytes(b"x")
            edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/prog.elf", runner=runner)
            self.assertFalse(edl.flash_partition("/dev/ttyUSB0", "init_boot_b", str(img), self.GEOM, td))

    def test_staged_exec_makes_a_local_executable_copy(self):
        # The fix: NAS/CIFS forces file_mode=0664 (non-exec), so tools must be copied local + chmod +x.
        import os
        from cas.adb import Edl
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "QSaharaServer"; src.write_text("#!/bin/sh\n"); src.chmod(0o644)
            wd = pathlib.Path(td) / "wd"; wd.mkdir()
            out = Edl(str(src), "/x/fh_loader", "/x/p.elf")._staged_exec(str(src), wd)
            self.assertEqual(pathlib.Path(out).parent, wd)        # staged into the local workdir
            self.assertTrue(os.access(out, os.X_OK))              # and now executable


class TestFlashers(unittest.TestCase):
    def test_fastboot_flasher_success_and_failure(self):
        from cas.adb import Adb, Fastboot
        from cas import provision as PV

        def fb_ok(args, input_text=None, timeout=900):
            return (0, "SER\t fastboot\n", "") if args[-1] == "devices" else (0, "", "")

        def fb_flashfail(args, input_text=None, timeout=900):
            if args[-1] == "devices":
                return 0, "SER\t fastboot\n", ""
            return (1, "", "FAILED (remote: 'unknown command')") if "flash" in args else (0, "", "")
        adb = Adb(runner=FakeRunner())
        self.assertTrue(PV.fastboot_flasher(Fastboot(serial="SER", runner=fb_ok))(
            adb, "init_boot_a", "/tmp/p.img", lambda *a: None))
        self.assertFalse(PV.fastboot_flasher(Fastboot(serial="SER", runner=fb_flashfail))(
            adb, "init_boot_a", "/tmp/p.img", lambda *a: None))

    def test_edl_flasher_success(self):
        from cas.adb import Adb, Edl
        from cas import provision as PV

        def runner(args, input_text=None, timeout=900):
            if args[0].endswith("QSaharaServer"):
                return 0, "Sahara protocol completed\n", ""
            if args[0].endswith("fh_loader"):
                return 0, "{All Finished Successfully}\n", ""
            return 0, "", ""
        edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/p.elf", runner=runner)
        edl.find_port = lambda timeout=60, on_tick=None, pattern="/dev/ttyUSB*": "/dev/ttyUSB0"
        geom = {"sector_size": "512", "num_sectors": "1", "partition": "0",
                "start_sector": "1", "start_byte_hex": "0x0"}
        flasher = PV.edl_flasher(edl, geom)
        with tempfile.TemporaryDirectory() as td:
            img = pathlib.Path(td) / "p.img"; img.write_bytes(b"x")
            self.assertTrue(flasher(Adb(runner=FakeRunner()), "init_boot_b", str(img), lambda *a: None))

    def test_root_dispatches_to_provided_flasher(self):
        from cas.adb import Adb, Fastboot
        from cas import provision as PV
        orig = PV.patch_init_boot_on_device
        PV.patch_init_boot_on_device = lambda adb, stock, out, log=print: True   # isolate dispatch
        try:
            seen = {}

            def fake_flasher(adb, target, image, log):
                seen["target"] = target
                return True
            with tempfile.TemporaryDirectory() as td:
                stock = pathlib.Path(td) / "init_boot.img"; stock.write_bytes(b"x")
                ok = PV.root(Adb(runner=FakeRunner(root=False)), Fastboot(runner=lambda *a, **k: (0, "", "")),
                             str(stock), magisk_apk=None, log=lambda *a: None, wait=False, flasher=fake_flasher)
                self.assertTrue(ok)
                self.assertEqual(seen.get("target"), "init_boot_a")    # FakeRunner: first_api 33, slot _a
        finally:
            PV.patch_init_boot_on_device = orig

    def test_seal_dispatches_to_provided_flasher_with_stock_image(self):
        from cas.adb import Adb, Fastboot
        from cas import provision as PV
        seen = {}

        def fake_flasher(adb, target, image, log):
            seen["target"] = target
            seen["image"] = image
            return True
        with tempfile.TemporaryDirectory() as td:
            stock = pathlib.Path(td) / "init_boot.img"; stock.write_bytes(b"x")
            ok = PV.seal(Adb(runner=FakeRunner(root=False)), Fastboot(runner=lambda *a, **k: (0, "", "")),
                         str(stock), log=lambda *a: None, wait=False, flasher=fake_flasher)
            self.assertTrue(ok)
            self.assertEqual(seen.get("target"), "init_boot_a")
            self.assertEqual(seen.get("image"), str(stock))     # seal un-roots by flashing STOCK


class TestCancel(unittest.TestCase):
    def test_subprocess_runner_cancel_kills_and_returns_CANCELLED(self):
        import threading
        from cas.adb import subprocess_runner, CANCELLED, is_cancelled
        ev = threading.Event(); ev.set()                 # pre-set → first poll aborts immediately
        rc, out, err = subprocess_runner([sys.executable, "-c", "import time; time.sleep(30)"], cancel=ev)
        self.assertEqual(rc, CANCELLED)
        self.assertTrue(is_cancelled(rc))

    def test_subprocess_runner_without_cancel_is_unchanged(self):
        from cas.adb import subprocess_runner
        rc, out, _ = subprocess_runner([sys.executable, "-c", "print('hi')"])
        self.assertEqual((rc, out.strip()), (0, "hi"))

    def test_wait_loops_bail_on_cancel(self):
        import threading
        from cas.adb import Adb, Fastboot, Edl
        ev = threading.Event(); ev.set()
        self.assertFalse(Adb(runner=FakeRunner(), cancel=ev).wait_boot(timeout=10))
        self.assertFalse(Fastboot(runner=lambda *a, **k: (0, "", ""), cancel=ev).wait(timeout=10))
        self.assertIsNone(Edl("/x/q", "/x/f", "/x/p", cancel=ev).find_port(timeout=10))

    def test_root_all_worker_reports_cancelled(self):
        import threading
        from cas import provision as PV
        from cas.adb import Adb, Fastboot
        ev = threading.Event(); ev.set()
        res = PV.root_all(lambda s: Adb(serial=s, runner=FakeRunner(), cancel=ev),
                          lambda s: Fastboot(serial=s, runner=lambda *a, **k: (0, "", ""), cancel=ev),
                          [("S", "device")], log=lambda *a: None)
        self.assertEqual(res["S"][0], "cancelled")

    def test_fastboot_flasher_brackets_the_critical_write(self):
        from cas.adb import Adb, Fastboot
        from cas import provision as PV
        events = []

        def fb_ok(args, input_text=None, timeout=900):
            return (0, "SER\t fastboot\n", "") if args[-1] == "devices" else (0, "", "")
        PV.fastboot_flasher(Fastboot(serial="SER", runner=fb_ok), on_critical=events.append)(
            Adb(runner=FakeRunner()), "init_boot_a", "/tmp/p.img", lambda *a: None)
        self.assertEqual(events, [True, False])          # marked entering + leaving the partition write


class TestProvisionLockdown(unittest.TestCase):
    def _profile(self, tmp, flags):
        apps = ["org.es_de.frontend", PV.COMPANION_PKG]
        d = pathlib.Path(tmp) / "p"
        pay = d / "golden_root_payload"
        pay.mkdir(parents=True)
        (d / "profile.meta").write_text("model_match=Odin2 ?Mini\nfrontend=es-de\ncaptured=2026-06-16\n")
        (pay / "pkglist.txt").write_text("\n".join(apps) + "\n")
        (pay / "global.meta").write_text("golden_serial=9C33-6BBD\n")
        for a in apps:
            (pay / a / "apk").mkdir(parents=True)
            (pay / a / "apk" / "base.apk").write_text("x")
            (pay / a / "data.tar").write_text("x")
        P.save_manifest(d / "manifest", apps, flags, header="# p")
        return P.Profile(d)

    def test_download_sets_device_owner_when_lockdown_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile(tmp, {"settings": "on", "lockdown": "on"})
            r = FakeRunner()
            self.assertTrue(PV.provision(Adb(runner=r), prof, log=lambda *_: None))
            self.assertTrue(any("dpm set-device-owner" in c for c in r.cmds()))

    def test_download_skips_device_owner_when_lockdown_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile(tmp, {"settings": "on", "lockdown": "off"})
            r = FakeRunner()
            self.assertTrue(PV.provision(Adb(runner=r), prof, log=lambda *_: None))
            self.assertFalse(any("dpm set-device-owner" in c for c in r.cmds()))

    def test_download_succeeds_even_if_lockdown_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile(tmp, {"settings": "on", "lockdown": "on"})
            r = FakeRunner(do_set_ok=False)          # device not fresh -> lockdown fails
            logs = []
            self.assertTrue(PV.provision(Adb(runner=r), prof, log=logs.append))  # still provisions
            self.assertTrue(any("UN-LOCKED" in m for m in logs))                 # LOUD operator warning fired


class TestDeviceOwner(unittest.TestCase):
    def _adb(self, **kw):
        return Adb(runner=FakeRunner(**kw))

    def test_set_device_owner_success(self):
        a = self._adb(device_owner=False, do_set_ok=True, do_restrict=True)
        self.assertTrue(PV.set_device_owner(a, log=lambda *_: None))
        self.assertTrue(any("dpm set-device-owner" in c for c in a.runner.cmds()))

    def test_set_device_owner_idempotent_when_already_owner(self):
        r = FakeRunner(device_owner=True, do_restrict=True)
        a = Adb(runner=r)
        self.assertTrue(PV.set_device_owner(a, log=lambda *_: None))
        self.assertFalse(any("dpm set-device-owner" in c for c in r.cmds()))  # did not re-set

    def test_set_device_owner_fails_when_not_fresh(self):
        a = self._adb(device_owner=False, do_set_ok=False)
        self.assertFalse(PV.set_device_owner(a, log=lambda *_: None))

    def test_set_device_owner_fails_when_restrictions_missing(self):
        # Monkeypatch sleep to a no-op: the polling loop would otherwise sleep ~3s on the failure path.
        orig_sleep = PV.time.sleep
        PV.time.sleep = lambda *_a, **_k: None
        try:
            a = Adb(runner=FakeRunner(do_set_ok=True, do_restrict=False))
            self.assertFalse(PV.set_device_owner(a, log=lambda *_: None))
        finally:
            PV.time.sleep = orig_sleep

    def test_release_sends_token_broadcast_and_confirms_cleared(self):
        r = FakeRunner(device_owner=True, release_clears=True)
        a = Adb(runner=r)
        self.assertTrue(PV.release(a, log=lambda *_: None))
        self.assertTrue(any("am broadcast" in c and "action.RELEASE" in c for c in r.cmds()))
        self.assertTrue(any("gc-release-7f3a9c2e" in c for c in r.cmds()))  # token on the wire

    def test_release_fails_when_owner_not_cleared(self):
        a = Adb(runner=FakeRunner(device_owner=True, release_clears=False))
        self.assertFalse(PV.release(a, log=lambda *_: None))

    def test_release_noop_when_not_owner(self):
        a = Adb(runner=FakeRunner(device_owner=False))
        self.assertTrue(PV.release(a, log=lambda *_: None))


class TestSaveManifestAxes(unittest.TestCase):
    def test_axes_roundtrip(self):
        d = pathlib.Path(tempfile.mkdtemp())
        m = d / "manifest"
        P.save_manifest(m, ["com.foo", "com.bar", "xyz.aethersx2.android"],
                        {"settings": "on"},
                        axes={"com.foo": (True, True), "com.bar": (True, False),
                              "xyz.aethersx2.android": (False, True)})
        self.assertEqual(P.manifest_axes(m),
                         {"com.foo": (True, True), "com.bar": (True, False),
                          "xyz.aethersx2.android": (False, True)})
        self.assertEqual(P.manifest_pkgs(m),
                         ["com.foo", "com.bar", "xyz.aethersx2.android"])

    def test_no_axes_writes_bare_lines(self):
        d = pathlib.Path(tempfile.mkdtemp())
        m = d / "manifest"
        P.save_manifest(m, ["com.foo"], {"settings": "on"})
        self.assertEqual(P.manifest_axes(m), {"com.foo": (True, True)})
        self.assertIn("\ncom.foo\n", "\n" + m.read_text())

    def test_gamelauncher_flag_roundtrips(self):
        import tempfile, pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        m = d / "manifest"
        P.save_manifest(m, ["com.foo"], {"gamelauncher": "on", "homescreen": "on"})
        self.assertEqual(P.manifest_flags(m).get("gamelauncher"), "on")

    def test_initial_capture_selection_round_trips_launcher_flags(self):
        from cas import profiles as P
        apps = ["com.retroarch.aarch64", "com.note.app"]
        # saved: an unticked game launcher (config off), HOME on, a package axis override
        sel = P.initial_capture_selection(
            apps, {"com.note.app": (True, False)},
            {"gamelauncher": "off", "homescreen": "on"},
            game_launcher="com.handheld.launcher", home_launcher="com.android.launcher3")
        self.assertEqual(sel["com.retroarch.aarch64"], (True, True))     # emulator default
        self.assertEqual(sel["com.note.app"], (True, False))             # package axis override applied
        self.assertEqual(sel["com.handheld.launcher"], (False, False))   # saved gamelauncher=off honored
        self.assertEqual(sel["com.android.launcher3"], (False, True))    # saved homescreen=on honored

    def test_launchers_config_on_by_default(self):
        """No saved flags: BOTH the game launcher AND the HOME launcher default to config-ON, so the
        homescreen + emulator picks are captured unless the operator unticks them."""
        from cas import profiles as P
        sel = P.initial_capture_selection(
            [], {}, {}, game_launcher="com.handheld.launcher", home_launcher="com.android.launcher3")
        self.assertEqual(sel["com.handheld.launcher"], (False, True))    # @gamelauncher default on
        self.assertEqual(sel["com.android.launcher3"], (False, True))    # @homescreen default on (was off)
        # default_capture_selection agrees on the HOME launcher default
        self.assertEqual(P.default_capture_selection([], home_launcher="com.android.launcher3")
                         ["com.android.launcher3"], (False, True))


class TestProfileLauncherAndAxes(unittest.TestCase):
    def test_all_pkgs_includes_launcher_meta(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "golden_root_payload").mkdir(parents=True)
        (d / "golden_root_payload" / "pkglist.txt").write_text("com.foo\n")
        (d / "profile.meta").write_text("launcher_pkg=com.handheld.launcher\n")
        prof = P.Profile(d)
        self.assertIn("com.handheld.launcher", prof.all_pkgs())
        self.assertIn("com.foo", prof.all_pkgs())

    def test_axes_reads_manifest_tokens(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "manifest").write_text("com.foo\nxyz.aethersx2.android config\n")
        prof = P.Profile(d)
        self.assertEqual(prof.axes(), {"com.foo": (True, True),
                                       "xyz.aethersx2.android": (False, True)})


class TestSealScrub(unittest.TestCase):
    def test_seal_runs_scrub_before_unroot(self):
        ra, fb = FakeRunner(), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertTrue(ok)
        cmds = ra.cmds()
        scrub_i = next((i for i, c in enumerate(cmds) if "scrub.sh" in c), None)
        magisk_i = next((i for i, c in enumerate(cmds) if "uninstall com.topjohnwu.magisk" in c), None)
        self.assertIsNotNone(scrub_i, "scrub.sh was never run during seal")
        self.assertIsNotNone(magisk_i)
        # scrub runs BEFORE the Magisk uninstall, which itself precedes the un-root flash
        self.assertLess(scrub_i, magisk_i, "scrub must run before the un-root steps")
        self.assertIn("flash init_boot", "\n".join(fb.cmds()))


class TestRunChain(unittest.TestCase):
    def _app(self):
        from cas.gui import App
        app = App.__new__(App)                            # bypass Tk __init__
        app.adb_bin = app.fb_bin = None
        app.profiles_root = "."
        app.assigned = {"S1": "p", "S2": "p"}
        app.assigned_manual = set()                       # required by _profile_map
        app.log = lambda m: None                          # required by _run_chain_core logging
        app.cancel_event = type("E", (), {"is_set": lambda self: False})()
        app._stage_calls = []
        def fake_stage(step, serials, pm, force, cev):
            app._stage_calls.append((step, list(serials)))
            # S1 fails 'root'; everything else ok
            return {s: (("fail" if (step == "root" and s == "S1") else "ok"),) for s in serials}
        app._stage = fake_stage
        return app

    def test_failed_root_drops_from_download(self):
        app = self._app()
        survivors = app._run_chain_core(["root", "download", "lock"], ["S1", "S2"], None)
        # stages run in order; download/lock only see S2 (S1 dropped after failing root)
        self.assertEqual(app._stage_calls,
                         [("root", ["S1", "S2"]), ("download", ["S2"]), ("lock", ["S2"])])
        self.assertEqual(survivors, ["S2"])

    def test_save_step_root_fails_no_indexerror(self):
        """Bug-repro (fix #1): survivors==[] after root failure must NOT raise IndexError on survivors[0]."""
        from unittest.mock import patch
        import cas.provision as PV_mod
        app = self._app()
        # override _stage so root ALWAYS fails for S1 (our only device)
        def fake_stage_all_fail(step, serials, pm, force, cev):
            app._stage_calls.append((step, list(serials)))
            return {s: ("fail",) for s in serials}
        app._stage = fake_stage_all_fail
        captured = []
        with patch.object(PV_mod, "capture_to_pc", lambda *a, **kw: captured.append(True) or True):
            # Before fix #1 this raises IndexError; after fix it must return []
            survivors = app._run_chain_core(["root", "save"], ["S1"], "testprof")
        self.assertEqual(survivors, [])
        self.assertEqual(captured, [])  # capture_to_pc must never be invoked

    def test_save_step_root_succeeds_invokes_capture(self):
        """When root succeeds for the single device, capture_to_pc must be called on that device."""
        from unittest.mock import patch
        import cas.provision as PV_mod
        app = self._app()
        # override _stage so root SUCCEEDS for S1
        def fake_stage_ok(step, serials, pm, force, cev):
            app._stage_calls.append((step, list(serials)))
            return {s: ("ok",) for s in serials}
        app._stage = fake_stage_ok
        captured_serials = []
        def fake_capture(adb, name, stamp, root, log):
            captured_serials.append(adb.serial)
            return True
        with patch.object(PV_mod, "capture_to_pc", fake_capture):
            survivors = app._run_chain_core(["root", "save"], ["S1"], "testprof")
        self.assertEqual(survivors, ["S1"])
        self.assertEqual(captured_serials, ["S1"])


class TestResolveChain(unittest.TestCase):
    def _r(self, **t):
        from cas.gui import App                          # the main window class
        return App._resolve_chain(None, t)               # pure: no self state used

    def test_orders_unit_chain(self):
        self.assertEqual(self._r(lock=True, root=True, download=True), (["root", "download", "lock"], None))

    def test_golden_chain(self):
        self.assertEqual(self._r(root=True, save=True), (["root", "save"], None))

    def test_save_excludes_download_lock(self):
        steps, err = self._r(save=True, download=True)
        self.assertEqual(steps, [])
        self.assertIn("Save", err)

    def test_nothing_ticked_is_error(self):
        steps, err = self._r()
        self.assertEqual(steps, [])
        self.assertTrue(err)


class TestModalManifestTransforms(unittest.TestCase):
    """Pure axes→manifest transforms behind the run-time app-pick modal (no Tk)."""

    def test_manifest_from_axes_includes_when_either_axis_on(self):
        from cas.gui import _manifest_from_axes
        axes = {"a": (True, True), "b": (False, True), "c": (True, False), "d": (False, False)}
        pkgs, sub = _manifest_from_axes(axes)
        self.assertEqual(pkgs, ["a", "b", "c"])            # d (both off) excluded
        self.assertEqual(sub, {"a": (True, True), "b": (False, True), "c": (True, False)})

    def test_capture_manifest_folds_launchers_into_flags(self):
        from cas.gui import _capture_manifest_from_axes
        axes = {"emu": (True, True), "gl": (False, True), "hl": (False, False), "off": (False, False)}
        pkgs, sub, flags = _capture_manifest_from_axes(
            axes, {"settings": "on"}, game_launcher="gl", home_launcher="hl")
        self.assertEqual(pkgs, ["emu"])                    # launchers fold to flags; 'off' excluded
        self.assertEqual(sub, {"emu": (True, True)})
        self.assertEqual(flags["gamelauncher"], "on")      # gl Config on  -> @gamelauncher on
        self.assertEqual(flags["homescreen"], "off")       # hl Config off -> @homescreen off
        self.assertEqual(flags["settings"], "on")          # base flag preserved


class TestPickDownloads(unittest.TestCase):
    """_pick_downloads: one modal per DISTINCT assigned profile, write-after-all, cancel aborts clean."""

    def _profile(self, root, name, pkgs):
        d = pathlib.Path(root) / name
        (d / "golden_root_payload").mkdir(parents=True)
        (d / "golden_root_payload" / "pkglist.txt").write_text("\n".join(pkgs) + "\n")
        (d / "profile.meta").write_text("")
        return d

    def _app(self, root):
        from cas.gui import App
        app = App.__new__(App)                             # bypass Tk __init__
        app.profiles_root = str(root)
        app.log = lambda m: None
        return app

    def test_one_modal_per_distinct_profile_writes_after_all(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self._profile(root, "p", ["com.foo", "com.bar"])
        app = self._app(root)
        app.assigned = {"S1": "p", "S2": "p"}              # two devices, one shared profile
        calls = []
        def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None):
            calls.append(title)
            return ({pk: (True, False) for pk in rows}, {"settings": "on"})
        app._app_pick_modal = fake_modal
        self.assertTrue(app._pick_downloads(["S1", "S2"]))
        self.assertEqual(len(calls), 1)                    # ONE modal for the shared profile
        m = root / "p" / "manifest"
        self.assertEqual(P.manifest_pkgs(m), ["com.foo", "com.bar"])

    def test_cancel_aborts_with_no_writes(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self._profile(root, "p1", ["com.foo"])
        self._profile(root, "p2", ["com.baz"])
        app = self._app(root)
        app.assigned = {"S1": "p1", "S2": "p2"}            # two distinct profiles
        seen = []
        def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None):
            seen.append(title)
            return None if len(seen) == 2 else ({pk: (True, True) for pk in rows}, {})
        app._app_pick_modal = fake_modal
        self.assertFalse(app._pick_downloads(["S1", "S2"]))   # cancel on the 2nd modal
        # write-after-all: a late cancel leaves NEITHER profile's manifest written
        self.assertFalse((root / "p1" / "manifest").exists())
        self.assertFalse((root / "p2" / "manifest").exists())

    def test_skips_unassigned_and_no_match(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self._profile(root, "p", ["com.foo"])
        app = self._app(root)
        app.assigned = {"S1": "p", "S2": "(no match)"}     # S3 has no entry at all
        calls = []
        def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None):
            calls.append(title)
            return ({pk: (True, True) for pk in rows}, {})
        app._app_pick_modal = fake_modal
        self.assertTrue(app._pick_downloads(["S1", "S2", "S3"]))
        self.assertEqual(len(calls), 1)                    # only the real profile prompts


class TestDetectLaunchers(unittest.TestCase):
    """_detect_device_launchers pushes lib-root.sh first (modal opens before capture pushes it), resolves
    HOME without root and the GAME frontend with root, falling back to a plain shell when su is blocked."""

    class _FakeAdb:
        def __init__(self, serial=None, adb=None, home="com.android.launcher3",
                     game_su="com.handheld.launcher", game_shell="", push_ok=True):
            self._home, self._game_su, self._game_shell, self._push_ok = home, game_su, game_shell, push_ok
            self.calls = []
        def shell(self, cmd):
            self.calls.append(("shell", cmd))
            if "home_launcher" in cmd:
                return (0, self._home + "\n", "")
            if "game_launcher" in cmd:
                return (0, (self._game_shell + "\n") if self._game_shell else "", "")
            return (0, "", "")                         # mkdir
        def su(self, cmd, timeout=900):
            self.calls.append(("su", cmd))
            if "game_launcher" in cmd and self._game_su is not None:
                return (0, self._game_su + "\n", "")
            return (124, "", "timeout")                # su blocked
        def push(self, src, dst):
            self.calls.append(("push", str(src)))
            return self._push_ok

    def _run(self, fake):
        from cas import gui as G
        from unittest.mock import patch
        app = G.App.__new__(G.App); app.adb_bin = None
        with patch.object(G, "Adb", lambda serial=None, adb=None: fake):
            return app._detect_device_launchers("S1"), fake.calls

    def test_pushes_libroot_then_resolves_both(self):
        fake = self._FakeAdb()
        (game, home), calls = self._run(fake)
        self.assertEqual((game, home), ("com.handheld.launcher", "com.android.launcher3"))
        self.assertTrue(any(c[0] == "push" and c[1].endswith("lib-root.sh") for c in calls))  # pushed first
        self.assertTrue(any(c[0] == "shell" and "home_launcher" in c[1] for c in calls))       # home no-root

    def test_game_falls_back_to_shell_when_su_blocked(self):
        fake = self._FakeAdb(game_su=None, game_shell="com.handheld.launcher")  # su yields nothing
        (game, home), calls = self._run(fake)
        self.assertEqual(game, "com.handheld.launcher")                          # curated shell fallback hit
        self.assertTrue(any(c[0] == "su" and "game_launcher" in c[1] for c in calls))

    def test_push_failure_returns_none(self):
        fake = self._FakeAdb(push_ok=False)
        (game, home), _ = self._run(fake)
        self.assertEqual((game, home), (None, None))

    def test_no_serial_returns_none(self):
        from cas import gui as G
        app = G.App.__new__(G.App); app.adb_bin = None
        self.assertEqual(app._detect_device_launchers(""), (None, None))


class TestApkStoreDeploy(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("CAS_CONFIG", "CAS_PROFILES", "CAS_COMPANION_APK")}

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_split_manifest_apps(self):
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "p", apps=["com.captured"])                     # captured -> payload module exists
            pay = prof.payload
            pkgs = ["com.captured", "org.cocoon.app", "com.cfgonly"]
            axes = {"com.captured": (True, True), "org.cocoon.app": (True, False),
                    "com.cfgonly": (False, True)}
            payload, managed = PV._split_manifest_apps(pay, pkgs, axes)
            self.assertEqual(payload, ["com.captured"])
            self.assertEqual(managed, ["org.cocoon.app"])                 # apk-axis, no module; cfgonly excluded

    def test_install_apk_single_and_split(self):
        fr = FakeRunner(); adb = Adb(runner=fr)
        self.assertTrue(PV._install_apk(adb, "p", [pathlib.Path("/x/base.apk")], log=lambda *a: None))
        self.assertTrue(any(c[-1] == "/x/base.apk" and "install" in c for c in fr.calls))
        fr2 = FakeRunner(); adb2 = Adb(runner=fr2)
        PV._install_apk(adb2, "p", [pathlib.Path("/x/base.apk"), pathlib.Path("/x/split.apk")],
                        log=lambda *a: None)
        self.assertTrue(any("install-multiple" in c for c in fr2.calls))

    def test_provision_installs_managed_store_app(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "cocoon", apps=["org.es_de.frontend"])          # one captured app
            P.save_manifest(prof.manifest_path, ["org.es_de.frontend", "org.cocoon.app"],
                            {"settings": "on"}, header="# cocoon",
                            axes={"org.es_de.frontend": (True, True), "org.cocoon.app": (True, False)})
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.cocoon.app", "v1", content="apkbytes")
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            C.set_apk_store(str(store))
            fr = FakeRunner(model="Retroid Pocket 6"); adb = Adb(runner=fr)
            ok = PV.provision(adb, P.Profile(prof.path), log=lambda *a: None)
            self.assertTrue(ok, f"provision failed; calls={fr.cmds()}")
            self.assertTrue(any("install" in c and any("v1.apk" in x for x in c) for c in fr.calls),
                            f"expected managed-app install; calls={fr.cmds()}")


if __name__ == "__main__":
    unittest.main()
