"""Thin adb wrapper. The `runner` is injectable so tests can mock it (no real device needed)."""
import glob
import os
import pathlib
import shutil
import subprocess
import sys
import time

SU = "/debug_ramdisk/su"   # MagiskSU path on these units (plain `su` isn't on the adb PATH)

# cas-gui.exe is a GUI (windowed) app; without this, every adb/fastboot subprocess pops a black
# console window on Windows. CREATE_NO_WINDOW suppresses it. 0 elsewhere — the conditional never
# evaluates the (Windows-only) attribute off-Windows, and creationflags=0 is a no-op on POSIX.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0

CANCELLED = 130    # rc for a child stopped by Cancel (128 + SIGINT) — distinguishes an abort from a real fail


def is_cancelled(rc):
    """True if `rc` is the cancelled sentinel (so callers/report can show ⏹ cancelled, not ❌ failed)."""
    return rc == CANCELLED


def subprocess_runner(args, input_text=None, timeout=900, cancel=None):
    """Default runner: run a command, return (returncode, stdout, stderr).
    On timeout returns (124, "", "timeout…") instead of raising — so a hung device (e.g. a MagiskSU grant
    prompt nobody tapped) fails FAST and never blocks a batch for the full timeout window.
    If `cancel` (a threading.Event) is given, poll it ~5x/s and, when set, terminate→kill the child and
    return (CANCELLED, output-so-far, 'cancelled'). Output goes to a temp FILE (not a PIPE) so a verbose
    child like fh_loader can't deadlock on a full pipe buffer while we poll."""
    if cancel is None:
        try:
            p = subprocess.run(args, capture_output=True, text=True, input=input_text,
                               timeout=timeout, creationflags=_NO_WINDOW)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {timeout}s"
    import tempfile
    with tempfile.TemporaryFile(mode="w+") as outf:
        try:
            p = subprocess.Popen(args, stdout=outf, stderr=subprocess.STDOUT,
                                 stdin=(subprocess.PIPE if input_text is not None else None),
                                 text=True, creationflags=_NO_WINDOW)
        except OSError as e:
            return 1, "", str(e)
        if input_text is not None:
            try:
                p.stdin.write(input_text)
                p.stdin.close()
            except (BrokenPipeError, ValueError):
                pass
        t0 = time.monotonic()
        while True:
            rc = p.poll()
            if rc is not None:
                outf.seek(0)
                return rc, outf.read(), ""
            if cancel.is_set():
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                outf.seek(0)
                return CANCELLED, outf.read(), "cancelled"
            if time.monotonic() - t0 > timeout:
                p.kill()
                outf.seek(0)
                return 124, outf.read(), f"timeout after {timeout}s"
            time.sleep(0.2)


def subprocess_stream(args, on_line, input_text=None, cancel=None):
    """Run a command, calling on_line(text) for EACH output line as it arrives (stdout+stderr merged).
    Splits on BOTH '\\n' and '\\r' so adb's carriage-return progress updates ('[ 42%] file') surface
    live instead of only at the end. Returns the process exit code (CANCELLED if `cancel` was set). Used
    for long jobs (restore/capture scripts, multi-GB pulls) so the UI can show realtime activity.
    `cancel` (a threading.Event), when set, kills the child — checked each loop, which fires while output
    flows (the streaming case); the bytes-polled pull_with_progress covers the long silent-copy phase."""
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=(subprocess.PIPE if input_text is not None else None),
                         text=True, bufsize=1, creationflags=_NO_WINDOW)
    if input_text is not None:
        try:
            p.stdin.write(input_text)
            p.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
    buf = ""
    while True:
        if cancel is not None and cancel.is_set():
            p.kill()
            break
        ch = p.stdout.read(1)          # 1 char at a time: lets us break on '\r' (progress) too
        if not ch:
            break
        if ch in "\r\n":
            line = buf.strip()
            if line:
                on_line(line)
            buf = ""
        else:
            buf += ch
    tail = buf.strip()
    if tail:
        on_line(tail)
    rc = p.wait()
    return CANCELLED if (cancel is not None and cancel.is_set()) else rc


def _dir_size_kb(path):
    """Total size (KB) of all files under `path`. Tolerates a missing dir (pull hasn't created it yet)
    and files that vanish/grow mid-walk (adb is writing into it) — best-effort sizing for a progress bar."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total // 1024


def list_devices(adb="adb", runner=subprocess_runner):
    """Return [(serial, state)] from `adb devices`."""
    rc, out, _ = runner([adb, "devices"])
    devs = []
    for line in out.splitlines()[1:]:
        line = line.rstrip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        devs.append((serial.strip(), state.strip()))
    return devs


def _parse_bootloader_state(props):
    """Map a {prop: value} dict to 'locked' | 'unlocked' | 'unknown' (pure, best-effort).
    Prefer the explicit vbmeta device_state; fall back to verified-boot color (orange = unlocked,
    green/yellow = locked). 'unknown' when neither is readable — callers must NOT hard-block on unknown."""
    ds = (props.get("ro.boot.vbmeta.device_state") or "").strip().lower()
    if ds in ("locked", "unlocked"):
        return ds
    vbs = (props.get("ro.boot.verifiedbootstate") or "").strip().lower()
    if vbs == "orange":
        return "unlocked"
    if vbs in ("green", "yellow"):
        return "locked"
    return "unknown"


class Adb:
    """adb scoped to one device serial (or the only device if serial is None)."""

    def __init__(self, serial=None, adb="adb", runner=subprocess_runner, cancel=None):
        self.serial = serial
        self.adb = adb
        self.runner = runner
        self.cancel = cancel        # threading.Event to abort long ops mid-flight; None = not cancelable

    def _base(self):
        return [self.adb] + (["-s", self.serial] if self.serial else [])

    def _runner_kw(self):
        """Pass cancel= only to the real subprocess_runner (injected test runners have a fixed signature)."""
        return ({"cancel": self.cancel}
                if (self.cancel is not None and self.runner is subprocess_runner) else {})

    def raw(self, *args):
        """Run `adb [-s serial] <args>` -> (rc, out, err)."""
        return self.runner(self._base() + list(args))

    def shell(self, cmd):
        """Run `adb shell <cmd>` (cmd is one string)."""
        return self.runner(self._base() + ["shell", cmd])

    def su(self, cmd, timeout=900):
        """Run <cmd> as root: `adb shell /debug_ramdisk/su -c <cmd>`."""
        return self.runner(self._base() + ["shell", SU, "-c", cmd], timeout=timeout)

    def su_stream(self, cmd, on_line):
        """Run <cmd> as root, streaming each output line to on_line() LIVE; returns rc.
        Real streaming only on the default runner; an injected (test) runner falls back to one
        blocking call whose output lines are still emitted, so behavior/asserts are unchanged."""
        args = self._base() + ["shell", SU, "-c", cmd]
        if self.runner is subprocess_runner:
            return subprocess_stream(args, on_line, cancel=self.cancel)
        rc, out, err = self.runner(args)
        for ln in (out or "").splitlines():
            on_line(ln)
        if err and err.strip():
            on_line(err.strip())
        return rc

    def pull_stream(self, src, dst, on_line):
        """adb pull, streaming progress lines ('[ NN%] ...') to on_line(); True on success."""
        args = self._base() + ["pull", str(src), str(dst)]
        if self.runner is subprocess_runner:
            return subprocess_stream(args, on_line, cancel=self.cancel) == 0
        return self.runner(args)[0] == 0

    def pull_with_progress(self, src, dst, total_kb, on_line, poll=3.0):
        """adb pull a big tree, emitting synthetic '[ NN%] pulled X/Y MB (R MB/s)' lines.

        adb only renders its own '[ NN%]' transfer progress to a TTY; with stdout on a pipe (how we run
        every adb call) it stays SILENT for the whole multi-GB pull — which made 'Save device' look frozen
        for minutes. Instead we launch the pull and poll the bytes landing in `dst`, turning that into the
        SAME '[ NN%]' lines the GUI already parses (cas/gui.py _maybe_progress). Cross-platform — no PTY.
        `total_kb` is the device-side payload size (du -sk); 0/unknown -> emit MB-only lines (bar stays
        marching). Returns True on success. An injected (test) runner has no real process to poll, so it
        falls back to one blocking pull."""
        args = self._base() + ["pull", str(src), str(dst)]
        if self.runner is not subprocess_runner:
            return self.runner(args)[0] == 0
        total_kb = int(total_kb or 0)
        p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW)
        t0 = time.monotonic()
        last_pct, last_emit = -1, 0.0
        while True:
            try:
                rc, finished = p.wait(timeout=poll), True
            except subprocess.TimeoutExpired:
                rc, finished = None, False
            if not finished and self.cancel is not None and self.cancel.is_set():
                p.kill()
                on_line("⏹ cancelled")
                return False
            got_kb = _dir_size_kb(dst)
            now = time.monotonic()
            rate = (got_kb / 1024.0) / max(0.001, now - t0)          # MB/s
            if total_kb > 0:
                pct = 100 if finished else min(99, got_kb * 100 // total_kb)
                if finished or pct != last_pct or now - last_emit >= 10:
                    on_line(f"[ {pct}%] pulled {got_kb // 1024} / {total_kb // 1024} MB ({rate:.1f} MB/s)")
                    last_pct, last_emit = pct, now
            elif finished or now - last_emit >= 5:                   # unknown total: MB + rate heartbeat
                on_line(f"pulled {got_kb // 1024} MB ({rate:.1f} MB/s)")
                last_emit = now
            if finished:
                return rc == 0

    def push_stream(self, src, dst, on_line):
        """adb push, streaming progress lines to on_line(); True on success."""
        args = self._base() + ["push", str(src), str(dst)]
        if self.runner is subprocess_runner:
            return subprocess_stream(args, on_line, cancel=self.cancel) == 0
        return self.runner(args)[0] == 0

    def getprop(self, key):
        return self.shell("getprop " + key)[1].strip()

    def push(self, src, dst):
        return self.raw("push", str(src), str(dst))[0] == 0

    def pull(self, src, dst):
        return self.raw("pull", str(src), str(dst))[0] == 0

    def reboot(self):
        return self.raw("reboot")[0] == 0

    def is_root(self):
        # short timeout: a real su replies instantly; a su that BLOCKS (MagiskSU grant prompt) must not
        # hang a batch — fail fast as "not root" after the timeout.
        return "uid=0" in self.su("id", timeout=30)[1]

    def boot_completed(self):
        return self.getprop("sys.boot_completed") == "1"

    def slot_suffix(self):
        """A/B active-slot suffix ('_a'/'_b') to target the LIVE slot, or '' on A-only devices. getprop,
        no root."""
        return self.getprop("ro.boot.slot_suffix").strip()

    def boot_partition(self):
        """The partition holding the Magisk-patchable ramdisk: 'init_boot' on units LAUNCHED with Android
        13+ (ro.product.first_api_level >= 33 — where Google split the generic ramdisk into its own
        partition), else 'boot' (older or merely-upgraded units keep the ramdisk in boot). getprop, no
        root — so we can read it on a fresh stock unit before touching fastboot."""
        try:
            api = int(self.getprop("ro.product.first_api_level") or 0)
        except ValueError:
            api = 0
        return "init_boot" if api >= 33 else "boot"

    def boot_flash_target(self):
        """The exact fastboot partition to flash this unit's (patched/stock) ramdisk to — e.g.
        'init_boot_a' (A/B, A13), 'init_boot' (A-only, A13), or 'boot_a' (legacy A/B). DETECTED, never
        assumed: hardcoding 'init_boot_a' would flash the IDLE slot on a slot-B unit (leaving it unrooted/
        unsealed) or the wrong partition on a pre-init_boot unit. Read it while adb is still up — it's lost
        once the unit drops to fastboot."""
        return self.boot_partition() + self.slot_suffix()

    def bootloader_state(self):
        """Best-effort 'locked' | 'unlocked' | 'unknown' from getprop (no root). Used only to WARN before
        a flash — never raises, and 'unknown' (the common case on units that don't expose it) never blocks."""
        try:
            props = {
                "ro.boot.vbmeta.device_state": self.getprop("ro.boot.vbmeta.device_state"),
                "ro.boot.verifiedbootstate": self.getprop("ro.boot.verifiedbootstate"),
            }
        except Exception:
            return "unknown"
        return _parse_bootloader_state(props)

    def is_golden(self):
        """True if the device carries the golden lock. FAIL-CLOSED: ambiguous/empty/errored su output
        is treated as GOLDEN (so provisioning refuses) — we never wipe a device we can't clear."""
        rc, out, _ = self.su("[ -e /data/adb/.cas_golden ] && echo CAS_GOLD || echo CAS_NOTGOLD", timeout=30)
        return not (rc == 0 and "CAS_NOTGOLD" in out and "CAS_GOLD" not in out)

    def has_sd(self):
        """True if an external SD volume (/storage/XXXX-XXXX) is mounted (carries ROMs + the serial)."""
        return bool(self.su("ls -d /storage/*-* 2>/dev/null")[1].strip())

    def sd_info(self, timeout=15):
        """Short descriptor of the external SD card for the UI: 'C89C-53BE · 238G', or 'no SD'.
        Tries shell first (listing /storage usually needs no root), falls back to su."""
        out = self.shell("ls -d /storage/*-* 2>/dev/null")[1].strip()
        if not out:
            out = self.su("ls -d /storage/*-* 2>/dev/null", timeout=timeout)[1].strip()
        if not out:
            return "no SD"
        path = out.split()[0]
        serial = path.rsplit("/", 1)[-1]
        size = ""
        for line in self.shell(f"df -h {path} 2>/dev/null")[1].splitlines():
            if path in line:
                f = line.split()
                if len(f) >= 2 and not f[1].endswith("%"):
                    size = f[1]
                break
        return serial + (f" · {size}" if size else "")

    def wait_boot(self, timeout=180, on_tick=None):
        """Wait for the device to reconnect and finish booting. Returns True if booted.
        on_tick(seconds) is called ~every 10s so a UI can show 'still booting…' during the wait."""
        self.raw("wait-for-device")
        for i in range(max(1, timeout // 2)):
            if self.cancel is not None and self.cancel.is_set():
                return False
            if self.boot_completed():
                return True
            if on_tick and i and i % 5 == 0:
                on_tick(i * 2)
            time.sleep(2)
        return False


class Fastboot:
    """fastboot scoped to one device. Some units (e.g. MANGMI) report a DIFFERENT serial in fastboot than
    in adb, so `fastboot -s <adb-serial> flash` HANGS forever ('waiting for <serial>') and the flash never
    runs. We therefore REMAP the requested (adb) serial to the device actually present in fastboot: the
    requested serial if it's there, else — when exactly one device is in fastboot — that one. Resolved on
    wait()/resolve(); ambiguous cases keep the requested serial so a wrong call fails loudly, never flashes
    the wrong unit. Runner is injectable."""

    def __init__(self, serial=None, fastboot="fastboot", runner=subprocess_runner, cancel=None):
        self.serial = serial            # requested serial (usually the adb serial)
        self._eff = serial              # effective fastboot serial actually used (resolved on wait/resolve)
        self.fb = fastboot
        self.runner = runner
        self.cancel = cancel            # threading.Event to abort a flash mid-write; None = not cancelable

    def _base(self):
        return [self.fb] + (["-s", self._eff] if self._eff else [])

    def _runner_kw(self):
        return ({"cancel": self.cancel}
                if (self.cancel is not None and self.runner is subprocess_runner) else {})

    def _list(self):
        """Serials currently in fastboot, from `fastboot devices` (UNSCOPED — fastboot ignores -s here)."""
        out = self.runner([self.fb, "devices"])[1]
        return [ln.split()[0] for ln in out.splitlines() if ln.strip() and "fastboot" in ln]

    def resolve(self):
        """Lock onto the device to talk to: the requested serial if present in fastboot; else, when exactly
        one device is in fastboot, that one (handles a serial that differs between adb and fastboot). Several
        present and the requested one absent -> keep the requested serial (fail loudly, don't guess). Returns
        the effective serial."""
        devs = self._list()
        if self.serial and self.serial in devs:
            self._eff = self.serial
        elif len(devs) == 1:
            self._eff = devs[0]
        else:
            self._eff = self.serial
        return self._eff

    def devices(self):
        return self.runner([self.fb, "devices"])[1]

    def wait(self, timeout=60, on_tick=None):
        """Wait until a device appears in fastboot, then RESOLVE the effective serial. Returns True if seen.
        on_tick(seconds) is called ~every 10s so a UI can show progress during the wait."""
        for i in range(max(1, timeout // 2)):
            if self.cancel is not None and self.cancel.is_set():
                return False
            if self._list():
                self.resolve()             # remap onto the present device (serial may differ from adb)
                return True
            if on_tick and i and i % 5 == 0:
                on_tick(i * 2)
            time.sleep(2)
        return False

    def flash(self, partition, img):
        return self.runner(self._base() + ["flash", partition, str(img)], **self._runner_kw())[0] == 0

    def reboot(self):
        return self.runner(self._base() + ["reboot"])[0] == 0


class Edl:
    """Flash a partition via Qualcomm EDL / Firehose (Sahara loads a programmer, fh_loader writes). For
    devices whose BOOTLOADER fastboot can't flash (e.g. MANGMI: `Writing… FAILED (remote: 'unknown
    command')`). The device must already be in EDL (`adb reboot edl`) — it then appears as /dev/ttyUSB*,
    which needs read/write access (the `dialout` group, or run CAS via sudo). The two tools + the Firehose
    programmer come from the device's firmware build. Runner is injectable for tests; success is detected
    from OUTPUT, not return code (QSaharaServer exits 0 even when it can't open the port).

    geometry (per init_boot_<slot>, parsed from the firmware's rawprogram) is a dict with:
      sector_size, num_sectors, partition (physical_partition_number), start_sector, start_byte_hex."""

    def __init__(self, qsahara, fh_loader, programmer, memoryname="eMMC", runner=subprocess_runner,
                 cancel=None):
        self.qsahara = str(qsahara)
        self.fh = str(fh_loader)
        self.programmer = str(programmer)
        self.memoryname = memoryname
        self.runner = runner
        self.cancel = cancel

    def _runner_kw(self):
        return ({"cancel": self.cancel}
                if (self.cancel is not None and self.runner is subprocess_runner) else {})

    def find_port(self, timeout=60, on_tick=None, pattern="/dev/ttyUSB*"):
        """Poll for the EDL serial port (created when the device enters EDL). Returns the path or None."""
        for i in range(max(1, timeout // 2)):
            if self.cancel is not None and self.cancel.is_set():
                return None
            ports = sorted(glob.glob(pattern))
            if ports:
                return ports[0]
            if on_tick and i and i % 5 == 0:
                on_tick(i * 2)
            time.sleep(2)
        return None

    @staticmethod
    def rawprogram_xml(label, image_name, geometry):
        """A one-entry Firehose rawprogram writing `image_name` to `label` at the firmware's geometry."""
        return (
            '<?xml version="1.0" ?>\n<data>\n'
            f'<program SECTOR_SIZE_IN_BYTES="{geometry["sector_size"]}" file_sector_offset="0" '
            f'filename="{image_name}" label="{label}" num_partition_sectors="{geometry["num_sectors"]}" '
            f'partofsingleimage="false" physical_partition_number="{geometry["partition"]}" '
            f'readbackverify="false" size_in_KB="0.0" sparse="false" '
            f'start_byte_hex="{geometry["start_byte_hex"]}" start_sector="{geometry["start_sector"]}" />\n'
            '</data>\n')

    def _staged_exec(self, src, workdir):
        """Return a locally-EXECUTABLE copy of a bundled tool. The firmware library lives on a CIFS/NAS
        mount that forces file_mode=0664 (non-executable), so exec'ing QSaharaServer/fh_loader straight off
        it fails with EACCES. Copy into the local workdir + chmod +x. Falls back to the original path if the
        copy can't be made (e.g. a mocked test path that doesn't exist) — harmless, the runner is mocked."""
        try:
            dst = pathlib.Path(workdir) / pathlib.Path(src).name
            if not dst.exists():
                shutil.copy2(src, dst)
                dst.chmod(0o755)
            return str(dst)
        except OSError:
            return str(src)

    def flash_partition(self, port, label, image, geometry, workdir, log=print):
        """Sahara-load the programmer, then Firehose-write `image` to `label`. Returns True on success.
        `workdir` is a writable LOCAL dir; the tools + image are staged there (the NAS mount is noexec) and
        a rawprogram is generated so fh_loader's --search_path finds the image by basename. Success is parsed
        from tool OUTPUT (QSaharaServer exits 0 even on a port-open failure)."""
        workdir = pathlib.Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        qsahara = self._staged_exec(self.qsahara, workdir)         # local + executable (CIFS is noexec)
        fh = self._staged_exec(self.fh, workdir)
        img_name = pathlib.Path(image).name
        staged = workdir / img_name
        if pathlib.Path(image).resolve() != staged.resolve():
            staged.write_bytes(pathlib.Path(image).read_bytes())
        xml = workdir / f"rawprogram_{label}.xml"
        xml.write_text(self.rawprogram_xml(label, img_name, geometry))

        log(f"EDL: loading Firehose programmer via Sahara on {port}...")
        rc, out, err = self.runner([qsahara, "-p", port, "-s", "13:" + self.programmer],
                                   **self._runner_kw())
        blob = (out or "") + (err or "")
        if "Could not connect" in blob or "Sahara protocol completed" not in blob:
            log(f"EDL: Sahara failed (port permission? add user to 'dialout' or run as root). {err.strip()}")
            return False

        log(f"EDL: Firehose writing {label}...")
        rc, out, err = self.runner([fh, "--port=" + port, "--sendxml=" + xml.name,
                                    "--search_path=" + str(workdir), "--memoryname=" + self.memoryname,
                                    "--noprompt", "--showpercentagecomplete"], **self._runner_kw())
        blob = (out or "") + (err or "")
        if "All Finished Successfully" not in blob and "{SUCCESS}" not in blob:
            log(f"EDL: Firehose write of {label} FAILED. {err.strip()}")
            return False
        return True

    def reset(self, port, workdir):
        """Reboot the device out of EDL (Firehose <power reset>). `workdir` is a local dir to stage fh_loader
        (the NAS copy is noexec). Best-effort — used to recover a unit so a failed flash never strands it."""
        fh = self._staged_exec(self.fh, workdir)
        try:
            return self.runner([fh, "--port=" + port, "--reset", "--noprompt"])[0] == 0
        except OSError:
            return False
