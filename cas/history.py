"""Read the saved run-history .jsonl logs into normalized, human-readable records for the History
viewer — the complete, copy-pasteable record of every Download / Save / firmware event, merged across
every per-machine file in the library.

Pure: filesystem in, plain dicts + strings out (no Tk), so it unit-tests without a display. Each
history file is `<type>-history.<machine>.jsonl` (or a legacy `<type>-history.jsonl`) written by
cas.provision._append_history / cas.firmware.log_event under config.history_dir() (the library root).
"""
import json
import pathlib

# One .jsonl family per kind; the window's Type filter. root/lock/warmup are written by
# provision.log_run (per-device pass/fail + the error reason on a failure).
_RUN_KINDS = ("root", "lock", "warmup")
_TYPES = ("download", "save") + _RUN_KINDS + ("firmware",)


def _mb(nbytes):
    try:
        return f"{int(nbytes) // 1048576} MB"
    except (TypeError, ValueError):
        return "— MB"


def _secs(v):
    try:
        return f"{float(v):.0f}s"
    except (TypeError, ValueError):
        return "—s"


def _dev_line(d):
    """One device's outcome for a run summary: 'S1→profile' / 'S1 ok', or 'S1 FAIL: <reason>' on a
    failure (the error the user asked to see)."""
    s = d.get("serial", "?")
    st = d.get("status", "?")
    if st == "ok":
        return f"{s}→{d['profile']}" if d.get("profile") else f"{s} ok"
    err = d.get("error")
    return f"{s} {st.upper()}" + (f": {err}" if err else "")


def _fmt_download(r):
    who = ", ".join(_dev_line(d) for d in (r.get("devices") or [])) or "—"
    return (f"{r.get('ok', 0)} ok · {r.get('failed', 0)} failed · "
            f"{_mb(r.get('total_bytes'))} in {_secs(r.get('total_secs'))}  ·  {who}")


def _fmt_run(r):
    """root / lock / warmup run: pass/fail counts + each device (with the error reason on a failure)."""
    who = " | ".join(_dev_line(d) for d in (r.get("devices") or [])) or "—"
    return f"{r.get('ok', 0)} ok · {r.get('failed', 0)} failed · {who}"


def _fmt_save(r):
    base = f"{r.get('profile', '?')} ← {r.get('serial') or '?'}"
    if r.get("status") in ("fail", "error") or r.get("error"):
        return base + f" — FAILED: {r.get('error', 'capture failed')}"
    return base + f" · {_mb(r.get('bytes'))} in {_secs(r.get('secs'))}"


def _fmt_firmware(r):
    ver = f" v{r['version']}" if r.get("version") else ""
    tail = " (manual)" if r.get("manual") else ""
    return f"{r.get('action', '?')} {r.get('firmware_id', '?')}{ver} → {r.get('serial') or '—'}{tail}"


_FMT = {"download": _fmt_download, "save": _fmt_save, "firmware": _fmt_firmware,
        "root": _fmt_run, "lock": _fmt_run, "warmup": _fmt_run}


def _machine_of(name, stem):
    """'download-history.archlinux.jsonl' → 'archlinux'; legacy 'download-history.jsonl' → 'legacy'."""
    rem = name[len(stem):].removesuffix(".jsonl").lstrip(".")
    return rem or "legacy"


def _when_key(when):
    """Sortable key from a 'YYYY-MM-DD HH:MM[:SS]' stamp — pad to seconds so firmware (minute-only)
    and download/save (second) stamps compare correctly."""
    w = str(when or "")
    return w if len(w) >= 19 else w + ":00"


def history_records(history_dir):
    """Every run-history event across all per-machine files, normalized and sorted NEWEST FIRST.
    Each record: {type, when, date, machine, text, raw}. Unreadable files and malformed lines are
    skipped (a corrupt line never breaks the viewer)."""
    root = pathlib.Path(history_dir)
    out = []
    if not root.is_dir():
        return out
    for kind in _TYPES:
        stem = f"{kind}-history"
        for f in sorted(root.glob(f"{stem}*.jsonl")):
            machine = _machine_of(f.name, stem)
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except (ValueError, TypeError):
                    continue
                if not isinstance(r, dict):
                    continue
                when = str(r.get("when", ""))
                out.append({
                    "type": kind,
                    "when": when,
                    "date": when[:10],
                    "machine": machine,
                    "text": _FMT[kind](r),
                    "raw": r,
                })
    out.sort(key=lambda e: _when_key(e["when"]), reverse=True)
    return out


def history_dates(records):
    """Distinct YYYY-MM-DD dates present in `records`, newest first (for the date filter dropdown)."""
    return sorted({e["date"] for e in records if e["date"]}, reverse=True)


def is_failure(record):
    """True if a history event is/contains a failure — powers the 'Failures only' filter. Firmware
    assign/update events are never failures."""
    r = record.get("raw") or {}
    t = record.get("type")
    if t in ("download",) + _RUN_KINDS:
        return bool(r.get("failed")) or any(
            d.get("status") in ("fail", "error") for d in (r.get("devices") or []))
    if t == "save":
        return r.get("status") in ("fail", "error") or bool(r.get("error"))
    return False


def filter_records(records, date=None, kinds=None):
    """Records matching an exact `date` (YYYY-MM-DD, None = any) and `kinds` (a set of types, None =
    all)."""
    out = records
    if date:
        out = [e for e in out if e["date"] == date]
    if kinds is not None:
        out = [e for e in out if e["type"] in kinds]
    return out


def render(records):
    """Join records into aligned, copy-pasteable text — one event per line, newest first:
    `<when-19>  <TYPE-8>  <detail>   [machine]`."""
    return "\n".join(
        f"{e['when']:<19}  {e['type'].upper():<8}  {e['text']}   [{e['machine']}]"
        for e in records
    )
