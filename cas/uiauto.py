"""Minimal uiautomator dump→find→tap, ported from scripts/uiauto.sh so the packaged exe needs no
external shell script. Controls are located by text/content-desc and tapped at their exact bounds
center (rotation-independent — no pixel guessing)."""
import re

_NODE_EL = re.compile(r'<node\b[^>]*?/?>')
_ATTR_LABEL = re.compile(r'(?:text|content-desc)="([^"]+)"')
_ATTR_BOUNDS = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')


def find_control(xml, pattern):
    """Center (cx, cy) of the control whose text/content-desc matches `pattern` (regex, case-
    insensitive). When several nodes match, a CLICKABLE one wins over non-clickable text — so a
    dialog's 'Grant shell root access?' message never steals the tap from its 'Grant' button;
    otherwise the first match wins. Returns None if nothing matches. Pure over a uiautomator dump."""
    rx = re.compile(pattern, re.I)
    first = None
    for el in _NODE_EL.finditer(xml or ""):
        node = el.group(0)
        mb = _ATTR_BOUNDS.search(node)
        if not mb:
            continue
        for label in _ATTR_LABEL.findall(node):
            if label.strip() and rx.search(label):
                a, b, c, d = (int(g) for g in mb.groups())
                center = ((a + c) // 2, (b + d) // 2)
                if 'clickable="true"' in node:
                    return center            # a clickable match wins immediately
                if first is None:
                    first = center           # else remember the first match as fallback
                break
    return first


def dump(adb):
    """uiautomator XML of the current screen ('' on failure)."""
    adb.shell("uiautomator dump /sdcard/cas_ui.xml")
    return adb.shell("cat /sdcard/cas_ui.xml")[1].replace("\r", "")


def has(adb, pattern):
    return find_control(dump(adb), pattern) is not None


def tap(adb, pattern):
    """Tap the first control matching `pattern`. True if one was found and tapped."""
    xy = find_control(dump(adb), pattern)
    if xy is None:
        return False
    adb.shell(f"input tap {xy[0]} {xy[1]}")
    return True


def foreground(adb, timeout=None):
    """Raw `topResumedActivity=…` line from dumpsys (callers substring-match the package, e.g.
    `MAGISK_PKG in foreground(adb)`).

    `timeout` (seconds) bounds THIS call by passing straight through to `adb.shell`. Pass it from inside
    a bounded poll loop (e.g. ③ Warm up) so one wedged `dumpsys activity activities` can't silently
    inherit the runner's 900s default and blow the loop's own wait budget by 60x with no output — exactly
    the class of hang that once froze wait_boot() on a stale transport. None (default) leaves adb.shell's
    own default in effect, unchanged for the existing caller (grant_shell_root)."""
    return adb.shell("dumpsys activity activities | grep -m1 topResumedActivity", timeout=timeout)[1].strip()
