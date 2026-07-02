# Zero-touch shell superuser grant (auto-grant MagiskSU)

**Date:** 2026-07-02
**Status:** Design — awaiting review
**Area:** provisioning / root flow

## Problem

`root()` (`cas/provision.py:812`) fully automates rooting a fresh unit: install the Magisk
app, patch the unit's own stock `init_boot` on-device (Magisk 30.7), flash the patched image,
reboot. But the **superuser grant to the adb shell is still a manual, one-time-per-unit tap**.
After boot, `root()` step 4 calls `adb.is_root()` — which runs `su id` (`cas/adb.py:266`, path
`/debug_ramdisk/su`). On a fresh unit that `su` trips MagiskSU's on-device grant prompt and
**blocks**; the 30s timeout reads as "not root," and `root()` gives up with a manual instruction
(`provision.py:899-902`): *"open Magisk → Superuser → enable the 'Shell' toggle, then retry."*

That manual tap is the only non-automatic step in the whole root→provision→seal pipeline, and it
has to be repeated on every fresh unit.

### Why the first grant is a chicken-and-egg

These are `user` builds (not `userdebug`), so:
- `adb root` cannot restart adbd as root.
- The MagiskSU policy DB (`/data/adb/magisk.db`) is root-owned, so we cannot pre-seed an
  "allow shell" policy **until we already have root once**.

On a `user` build the *only* path to the first root is MagiskSU, and MagiskSU gates the first
`su` behind a UI prompt. So the first grant cannot be made headless by seeding state — it must be
obtained by answering the prompt.

## Goal

Make the shell superuser grant **fully zero-touch** — no human touches the device — and
**permanent** so it never prompts again. Scope of the grant (per decision):

- **Shell uid (2000): explicit ALLOW** — load-bearing; this is what CAS provisioning/verify/seal
  need headless.
- **Global auto-allow** — Magisk configured so any app requesting root during the provisioning
  window is auto-approved rather than prompting.

## Non-goals

- Changing `seal()`. Retail units are un-rooted at seal (stock `init_boot` re-flashed, Magisk app
  uninstalled), so no auto-grant state matters on a shipped unit. See "seal interaction."
- Rooting `userdebug`/engineering builds differently — out of scope.
- A persistent boot-time Magisk module (Approach C) — noted as optional future hardening, not built.

## Approach

**Chosen: A — auto-tap the Magisk grant prompt, then persist via `magisk --sqlite`.**

Break the chicken-and-egg by *answering* the first prompt with UI automation instead of a human:
trigger `su` in the background (it blocks on the prompt), drive `uiautomator` to find and tap the
**"Grant"** control, confirm root, then write a permanent policy + global auto-allow as root.

The tap is **text/content-desc based, not pixel-based**, and the Magisk prompt is the same app UI
on every device — so this is *model-independent* (Odin / Retroid / MANGMI all show the same
`com.topjohnwu.magisk` request UI). This is more robust than the existing SAF-picker automation,
which varies per emulator.

**Rejected: B — pre-seed `magisk.db` before the first `su`.** Impossible on `user` builds (DB is
root-owned; no root exists yet).

**Deferred: C — ship a Magisk module whose `post-fs-data.sh` re-asserts the policy at boot.**
Installing the module itself needs root once, so it can't stand alone; it only adds reboot-insurance
on top of A. The DB policy row already persists across reboots (it lives on `/data`), so C is YAGNI
unless we observe policies being wiped in the field.

## Design

### 1. New self-contained uiautomator helper in `cas/`

`uiauto.sh` is standalone bash under `scripts/` and is not bundled into the packaged exe, so the
grant logic must not depend on it. Port the minimal dump→parse→tap primitives into Python inside
`cas/` (a new small module, e.g. `cas/uiauto.py`), mirroring `scripts/uiauto.sh`:

- `dump(adb)` — `adb shell uiautomator dump /sdcard/ui.xml` then read it back; return the XML text.
- `find(xml, pattern)` — regex over `text`/`content-desc` node attrs; return the tap center
  `(cx, cy)` of the first match (from `bounds`), or `None`.
- `tap(adb, pattern)` — dump, find, `adb shell input tap cx cy`; return whether a control matched.
- `has(adb, pattern)` — dump + find, bool.

Uses only `adb.shell` (already available). Kept tiny and independently testable (parsing is a pure
function over an XML string — unit-testable with no device).

### 2. `grant_shell_root(adb, log=print, attempts=3, ui_timeout=15)` in `cas/provision.py`

New helper that obtains and persists the grant. Sequence:

1. If `adb.is_root()` already → return `True` (idempotent; e.g. the prompt's "remember" already
   created a policy on a retry).
2. Fire the trigger on a background thread: `adb.su("id", timeout=…)` — this blocks on the prompt.
   Reuse `concurrent.futures` (already imported in `provision.py`).
3. Foreground: poll for the prompt for up to `ui_timeout` seconds — `uiauto.has(adb, "Grant")` then
   `uiauto.tap(adb, "Grant")`. Handles the prompt's spawn delay / countdown timer.
4. Join the background call and re-check `adb.is_root()`.
5. If still not root, retry the whole cycle up to `attempts` times (the prompt can be slow to appear
   on first boot).
6. On success, **persist as root** (see §3). Return `True`.
7. On failure after `attempts`, fall back to today's manual instruction and return `False` — never
   leave the unit half-rooted silently.

### 3. Persistence + global auto-allow (run as root, once granted)

- **Shell = allow, permanent, silent** (load-bearing, deterministic — does not rely on the prompt's
  "remember" checkbox):
  ```
  magisk --sqlite "REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)"
  ```
  `uid=2000` shell, `policy=2` allow, `until=0` forever, logging/notification off. This DB row lives
  on `/data`, so it survives reboots — the grant never prompts again.
- **Global auto-allow** — set Magisk's automatic response to *Grant* so any other app requesting
  root during provisioning is auto-approved. In Magisk this is the app-side auto-response setting
  (Magisk shared-prefs), not a `magisk --sqlite settings` key; `root_access` (DB) governs whether
  adb/apps may request root at all. The implementation plan will confirm, against Magisk **30.7**
  source, the exact prefs key/value (historically `su_auto_response`) and the `root_access` value
  (`3` = apps and adb), and write them as root. The shell-policy row above is the load-bearing part;
  global auto-allow is best-effort convenience on top.
- Each write is verified (re-read the row / setting) so a silent failure surfaces in the log rather
  than passing quietly — consistent with restore.sh's "declared-but-unconfirmed = FAIL" pattern.

### 4. Integration into `root()`

Replace the dead-end at `provision.py:893-902`. After confirmed boot:

```
if adb.is_root():
    log("✓ ROOTED — adb shell su works.")
    return True
log("shell not granted yet — auto-granting via the on-device Magisk prompt (zero-touch)…")
if grant_shell_root(adb, log=log):
    log("✓ ROOTED — shell auto-granted and made permanent. Ready to '② Download'.")
    return True
# grant_shell_root already logged the manual fallback instruction
return False
```

`root()`'s "already rooted → return fast" branch (`:835`) is unchanged.

### 5. `seal()` interaction — no change

`seal()` un-roots for retail by re-flashing stock `init_boot` and uninstalling the Magisk app. The
`magisk.db` policy row is left on `/data` but is inert (no Magisk to read it) and is wiped by the
factory reset that precedes resale. No code change; documented so a reviewer doesn't expect a scrub
step.

### 6. Config toggle (optional)

Add `auto_grant_shell` to `cas-config.json` defaulting to `true`. When `false`, `root()` keeps the
old manual-instruction behavior. Cheap escape hatch if the auto-tap ever misbehaves on a new model;
default keeps the workflow zero-touch.

## Edge cases

- **Prompt never appears / "Grant" not found** — bounded retries, then manual fallback + `return
  False`. Same failure surface as today, just reached only after auto-attempts.
- **Prompt localized / different button label** — match a small alternation (e.g.
  `Grant|Allow|授权`); confirm the 30.7 English label in the plan. Devices here are English-locale,
  so low risk.
- **`uiautomator dump` fails while an animation/foreground app is up** — retry the dump inside the
  poll loop (uiauto.sh already tolerates a failed dump by returning empty).
- **Background `su` returns before the tap** (already-remembered policy) — step 1's `is_root()`
  short-circuits; harmless.
- **Batch provisioning** — `grant_shell_root` operates on one `adb` (one serial) exactly like the
  rest of `root()`; the existing per-device worker model is unchanged.

## Testing

Follows existing patterns (`tests/test_cas.py` fake-adb, `tests/test_grant_appops.sh` style).

- **uiauto parse unit test** (Python) — feed a captured `uiautomator dump` XML containing a Magisk
  "Grant" node; assert `find()` returns the correct center; assert a no-match returns `None`. Pure
  function, no device.
- **`grant_shell_root` happy path** — fake adb that starts in "su blocks" state and flips to granted
  after a simulated tap; assert it returns `True`, that the `magisk --sqlite` policy write was issued,
  and `is_root()` becomes true.
- **`grant_shell_root` fallback** — fake adb where the prompt never resolves; assert bounded attempts,
  `return False`, and that the manual instruction is logged (reuse the `su_blocked` model already in
  `test_cas.py`).
- **`root()` end-to-end** — extend the existing root test so a booted-but-ungranted unit now ends
  rooted via the auto-grant path instead of returning the manual message.

## Open items for the implementation plan

1. Confirm, against Magisk **30.7** source, the exact global auto-allow mechanism: the app-side
   auto-response prefs key/value and the `root_access` DB value, and where each is written as root.
2. Confirm the 30.7 grant-prompt control label(s) and that `SuRequestActivity` is dumpable by
   `uiautomator` (vs. a secure/overlay window).
3. Confirm the background-`su` timeout so a genuinely stuck prompt still fails fast within
   `attempts × ui_timeout` and doesn't hang batch provisioning.
