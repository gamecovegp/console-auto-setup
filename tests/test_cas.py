"""Unit tests for the cas package — mock adb runner, no real device. Run from project root:
    python3 -m unittest discover -s tests -p 'test_*.py' -t .
"""
import os
import sys
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
                 push_ok=True, pull_ok=True):
        self.calls = []
        self.model, self.golden, self.root, self.boot, self.sd = model, golden, root, boot, sd
        self.push_ok, self.pull_ok = push_ok, pull_ok

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
            if tail.startswith("getprop"):
                key = tail.split()[-1]
                val = {"ro.product.model": self.model, "sys.boot_completed": self.boot}.get(key, "")
                return 0, val + "\n", ""
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


class TestAdb(unittest.TestCase):
    def test_getprop_and_root(self):
        a = Adb(runner=FakeRunner())
        self.assertEqual(a.getprop("ro.product.model"), "Odin2 Mini")
        self.assertTrue(a.is_root())
        self.assertFalse(a.is_golden())
        self.assertTrue(a.boot_completed())

    def test_not_root(self):
        self.assertFalse(Adb(runner=FakeRunner(root=False)).is_root())

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


class TestProfiles(unittest.TestCase):
    def test_manifest_parse(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            self.assertEqual(prof.pkgs(),
                             ["org.es_de.frontend", "dev.eden.eden_emulator", "org.citra.emu"])
            self.assertEqual(prof.flags(), {"settings": "on", "hardening": "on"})

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


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES")}

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
                self.assertEqual(C.library_root(), APPDIR / "profiles")
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
            self.assertEqual(str(C.set_library("/mnt/nas/CAS Profiles")), "/mnt/nas/CAS Profiles")
            self.assertEqual(C.load_config().get("library"), "/mnt/nas/CAS Profiles")

    def test_nas_credentials_roundtrip_and_obfuscated(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            cfgp = pathlib.Path(t) / "cas-config.json"
            os.environ["CAS_CONFIG"] = str(cfgp)
            self.assertIsNone(C.get_nas_credentials())             # nothing stored yet
            C.set_nas_credentials("cas_app", "P@ss w0rd!")
            self.assertEqual(C.get_nas_credentials(), ("cas_app", "P@ss w0rd!"))
            self.assertNotIn("P@ss w0rd!", cfgp.read_text())       # password not in the clear
            C.set_nas_credentials("", "")                          # clear
            self.assertIsNone(C.get_nas_credentials())

    def test_nas_share_root(self):
        from cas import config as C
        self.assertEqual(C.nas_share_root(), r"\\192.168.100.227\01 GAMECOVE")

    def test_env_wins_over_config(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            C.set_library("/mnt/nas/lib")
            os.environ["CAS_PROFILES"] = "/tmp/override"
            self.assertEqual(str(C.library_root()), "/tmp/override")

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

    def test_capture_to_pc_invokes_capture(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t,
                             log=lambda m: None, dry_pull=True)
            self.assertIn("capture.sh", "\n".join(r.cmds()))
            self.assertIn("CAS_OUT=/data/local/tmp", "\n".join(r.cmds()))


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
            self.assertTrue(any("push" in c and str(src) in " ".join(c) for c in r.calls))

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
    """The shared GameCove Companion app installs from the PC (adb install), kept out of the golden."""

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

    def test_provision_installs_companion_from_pc(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
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
            self.assertIn(str(apk), a)

    def test_provision_skips_companion_when_flag_off(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            P.save_manifest(prof.manifest_path, prof.pkgs(),       # @companion off
                            {"settings": "on", "companion": "off"}, header="# t")
            apk = pathlib.Path(t) / "gamecove-companion.apk"
            apk.write_bytes(b"x")
            os.environ["CAS_COMPANION_APK"] = str(apk)
            try:
                r = FakeRunner()
                ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)
            finally:
                os.environ.pop("CAS_COMPANION_APK", None)
            self.assertTrue(ok)
            self.assertNotIn(str(apk), "\n".join(r.cmds()))        # flag off -> NOT installed

    def test_provision_installs_companion_when_flag_on(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            P.save_manifest(prof.manifest_path, prof.pkgs(),
                            {"settings": "on", "companion": "on"}, header="# t")
            apk = pathlib.Path(t) / "gamecove-companion.apk"
            apk.write_bytes(b"x")
            os.environ["CAS_COMPANION_APK"] = str(apk)
            try:
                r = FakeRunner()
                ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)
            finally:
                os.environ.pop("CAS_COMPANION_APK", None)
            self.assertTrue(ok)
            self.assertIn(str(apk), "\n".join(r.cmds()))           # flag on -> installed


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


if __name__ == "__main__":
    unittest.main()
