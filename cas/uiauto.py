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
