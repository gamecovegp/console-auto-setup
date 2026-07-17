# Run duration in history

**Date:** 2026-07-17
**Status:** approved, pre-implementation
**Scope:** Phase 1 only (per-step wall-clock). Phase 2 (per-unit rollup) is a separate spec — see Future.

## Problem

Run history records *what* happened but not *how long it took*, so there is no way to answer "how long
does it take to provision one unit?" — the number needed to plan a bench day.

The gap is uneven, not total:

| Action | Duration today | Written by | Rendered as |
|---|---|---|---|
| download | ✅ `total_secs` | `_log_download_run(root, results, elapsed, log)` | `X MB in Ys` |
| save | ✅ `secs` | `provision.py:1309` (`time.monotonic() - t0`) | `X MB in Ys` |
| **root** | ❌ none | `log_run()` | pass/fail counts only |
| **lock** | ❌ none | `log_run()` | pass/fail counts only |
| **warmup** | ❌ none | `log_run()` | pass/fail counts only |

So Download and Save already do this correctly. Only the three `log_run()` actions are missing, and
`cas/history.py` already has the `_secs()` formatter they would use.

## Design

**Mirror Download exactly.** It already solved this at `provision.py:846-848`:

```python
t0 = time.monotonic()
...
_log_download_run(root, results, time.monotonic() - t0, log)
```

Three changes:

1. **`log_run(root, action, results, log=print, elapsed=None)`** — new trailing optional param. When
   `elapsed` is not None, the record gains `"total_secs": round(elapsed, 1)`. When None, the field is
   omitted entirely (not written as null).
2. **The three callers measure and pass it** — `warmup_all` (`provision.py:1127`), `root_all`
   (`:1846`), `seal_all` (`:1949`). Each takes `t0 = time.monotonic()` before its `_each_device()`
   fan-out and passes the delta. The measurement must span the whole action, not just the fan-out —
   each of these does real work outside `_each_device()`.
3. **`_fmt_run` renders it** — placed like Download's, i.e. BEFORE the device list, not after it. The
   device list is unbounded (one entry per unit on the bench, with failure reasons), so a duration
   appended after it would be pushed off the end of the line. Compare:

   ```
   download:  f"{ok} ok · {failed} failed · {_mb(...)} in {_secs(...)}  ·  {who}"
   run (new): f"{ok} ok · {failed} failed · {_secs(...)}  ·  {who}"
   ```

   A run has no byte count, so it reads `3 ok · 0 failed · 612s  ·  S1→p | S2→p | S3→p` — the same
   shape as Download minus the `X MB in` clause.

### Rejected alternatives

- **A timing decorator / context manager over the `*_all` functions.** A new abstraction for three
  call sites is YAGNI, and it would obscure the fact that each function chooses what its own span
  covers.
- **Timing inside `_each_device()`.** It is shared plumbing; growing its return contract to carry a
  duration would ripple into every caller. It also measures the wrong span — root/lock/warmup each do
  work before and after the fan-out that belongs in the number.

### What the number means

For root/lock/warmup, `total_secs` is **batch wall-clock**, because these actions fan out across
devices in PARALLEL by design (root is reboot-dominated; running four units at once is the whole
point). Four units rooting together for ten minutes logs `600`, not `2400`.

This is the honest number and the one that predicts a bench day. It is deliberately **not** a per-unit
figure — see Future.

### Field naming

`total_secs`, matching `download` (also a batch action). `save` keeps its per-device `secs`. That
inconsistency predates this spec; matching the batch sibling beats inventing a third name.

## Backward compatibility

Free. `_secs()` already returns `"—s"` on `TypeError`, so every pre-existing history record — with no
`total_secs` key — renders as `—s` with no migration and no crash. Bench history stays readable.

## Testing

- `log_run()` writes `total_secs` when `elapsed` is passed.
- `log_run()` OMITS the key entirely when `elapsed` is None (not `null`).
- `_fmt_run` renders a duration when present.
- `_fmt_run` degrades to `—s` on a record with no `total_secs` (the old-record path).
- Each of the three callers passes a real measured value — mutation: remove a caller's `t0`/argument
  and a test must fail. A caller that silently stopped timing is the failure mode this spec exists to
  prevent, so it must be pinned per call site, not just on `log_run` in isolation.
- Existing `log_run` tests pass unmodified (the new param is optional and trailing).

## Future — Phase 2 (separate spec, not now)

A per-unit rollup: "this RP6 took 45m from Root to Lock." It needs a rule for attributing a parallel
batch's wall-clock to individual units, which is a real design question and not obviously answerable
(divide by N? charge each unit the full batch span?). Phase 1 deliberately stores what Phase 2 needs
and nothing more: `total_secs` plus the `devices` list already in every record, from which the unit
count is derivable. No extra field is added speculatively.

Phase 2 should only be specced once there is real logged data to look at — the shape of the answer
depends on what the numbers turn out to be.
