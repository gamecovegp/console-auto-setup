"""Thin adb wrapper. The `runner` is injectable so tests can mock it (no real device needed)."""
import os
import subprocess
import sys
import time

SU = "/debug_ramdisk/su"   # MagiskSU path on these units (plain `su` isn't on the adb PATH)

# cas-gui.exe is a GUI (windowed) app; without this, every adb/fastboot subprocess pops a black
# console window on Windows. CREATE_NO_WINDOW suppresses it. 0 elsewhere — the conditional never
# evaluates the (Windows-only) attribute off-Windows, and creationflags=0 is a no-op on POSIX.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0


def subprocess_runner(args, input_text=None, timeout=900):
    """Default runner: run a command, return (returncode, stdout, stderr).
    On timeout returns (124, "", "timeout…") instead of raising — so a hung device (e.g. a MagiskSU grant
    prompt nobody tapped) fails FAST and never blocks a batch for the full timeout window."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, input=input_text,
                           timeout=timeout, creationflags=_NO_WINDOW)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def subprocess_stream(args, on_line, input_text=None):
    """Run a command, calling on_line(text) for EACH output line as it arrives (stdout+stderr merged).
    Splits on BOTH '\\n' and '\\r' so adb's carriage-return progress updates ('[ 42%] file') surface
    live instead of only at the end. Returns the process exit code. Used for long jobs (restore/capture
    scripts, multi-GB pulls) so the UI can show realtime activity + transfer percentages."""
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
    return p.wait()


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


class Adb:
    """adb scoped to one device serial (or the only device if serial is None)."""

    def __init__(self, serial=None, adb="adb", runner=subprocess_runner):
        self.serial = serial
        self.adb = adb
        self.runner = runner

    def _base(self):
        return [self.adb] + (["-s", self.serial] if self.serial else [])

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
            return subprocess_stream(args, on_line)
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
            return subprocess_stream(args, on_line) == 0
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
            return subprocess_stream(args, on_line) == 0
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
            if self.boot_completed():
                return True
            if on_tick and i and i % 5 == 0:
                on_tick(i * 2)
            time.sleep(2)
        return False


class Fastboot:
    """fastboot scoped to one device (or the only one in bootloader). Runner is injectable."""

    def __init__(self, serial=None, fastboot="fastboot", runner=subprocess_runner):
        self.serial = serial
        self.fb = fastboot
        self.runner = runner

    def _base(self):
        return [self.fb] + (["-s", self.serial] if self.serial else [])

    def devices(self):
        return self.runner(self._base() + ["devices"])[1]

    def wait(self, timeout=60, on_tick=None):
        """Wait until a device appears in fastboot. Returns True if seen.
        on_tick(seconds) is called ~every 10s so a UI can show progress during the wait."""
        for i in range(max(1, timeout // 2)):
            if self.devices().strip():
                return True
            if on_tick and i and i % 5 == 0:
                on_tick(i * 2)
            time.sleep(2)
        return False

    def flash(self, partition, img):
        return self.runner(self._base() + ["flash", partition, str(img)])[0] == 0

    def reboot(self):
        return self.runner(self._base() + ["reboot"])[0] == 0
