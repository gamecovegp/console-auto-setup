# Zero-touch boot grant — pre-write the su policy at boot (no Magisk dialog, ever)

**Date:** 2026-07-13
**Status:** Design — awaiting review
**Area:** provisioning / root flow
**Supersedes the manual step in:** `2026-07-02-zero-touch-shell-root-grant-design.md`

## Problem

Rooting a fresh unit is otherwise fully automated, but the **first MagiskSU grant to the adb
shell still requires a human tap on the device**. In the field the operator has to pick up the
unit and tap **Grant** in the Magisk Superuser dialog. The July auto-tap (`grant_shell_root()`,
`cas/provision.py`) was meant to answer that dialog with `uiautomator`, but it is failing on
current units (air-x), so the human is back in the loop.

Root cause of *why a dialog exists at all*: on a `user` build the very first `su` trips
MagiskSU's grant prompt, because there is no policy row for the shell UID (2000) yet. The July
spec correctly concluded you cannot pre-seed that policy **from the PC** before the first root —
`/data/adb/magisk.db` is root-owned and no root exists yet (chicken-and-egg).

The gap the July spec left open: it only considered writing the policy **from adb after boot**.
It never considered writing it **from a root context that already runs at boot inside the device**
— which is exactly what a boot script baked into the patched image gives us.

## Goal

Make the shell grant **fully zero-touch and prompt-free from the very first boot** — no dialog
appears, so there is nothing for a human (or `uiautomator`) to answer — while keeping genuine
unconfined Magisk root so every downstream `adb.su` call in CAS works unchanged.

## Approach

Break the chicken-and-egg by writing the policy from **inside the device at boot, as root**,
before adb ever calls `su`. Magisk's `overlay.d` mechanism runs scripts from the boot ramdisk as
root on every boot. We bake a tiny boot service into the Magisk-patched `init_boot` that writes
the same policy `grant-persist.sh` already writes:

```
REPLACE INTO policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)   # shell = ALLOW
REPLACE INTO settings (key,value) VALUES('root_access',3)                             # adb + apps
```

By the time CAS boots the unit and calls `su`, the policy already says ALLOW → **no dialog, first
time or ever** → and it is real unconfined Magisk `su`, so nothing downstream changes. It
re-asserts every boot, so it also removes any post-reboot re-prompt (this folds in the "Approach C"
the July spec deferred).

### Why not `ro.debuggable` → `adb root` (the mechanism first floated and rejected)

Spoofing `ro.debuggable=1` so `adb root` works *does* remove the dialog, but on a `user` build the
resulting uid-0 shell lands in a **SELinux-confined `adbd` domain**. It is root by uid yet still
blocked from the operations CAS actually performs (writing `/data/adb`, mounting, `pm`/`appops`,
restoring the golden payload) — Download would start failing on permission denials. It also tears
down and restarts adbd (a reconnect dance), requires a seal-time guard so units never ship
adb-rootable, and touches nothing CAS already uses. Rejected in favour of overlay.d, which yields
the same "no dialog" outcome with genuine unconfined root and no downstream churn.

### Why not keep leaning on the auto-tap

UI automation across models and Magisk versions is inherently fragile (the current failure is
proof). It stays only as a **fallback layer**, never the primary path.

## Design

### Data flow (the Root step)

```
patch init_boot (boot_patch.sh)  ─►  magiskboot inject overlay.d  ─►  flash  ─►  reboot
                                                                                    │
                          magiskinit runs overlay.d service AS ROOT ◄───────────────┘
                          cas-grant.sh: REPLACE policy shell=ALLOW, root_access=3
                                                                                    │
CAS: is_root()? ──► su id ──► policy already ALLOW ──► uid=0, unconfined ──► ✅ no dialog, ever
        └─ false (overlay.d ignored) ──► auto-tap fallback ──► manual (last resort)
```

### New files (committed to the repo, **LF-only bytes**)

These are consumed by the device's `init` / shell. CRLF would break them (same class of bug as the
CRLF device-manifest incident). They are static committed files, so committing them LF is
sufficient; nothing writes them at runtime.

- **`provision/root/overlay/cas-grant.sh`** — the boot-time policy writer. Contents mirror
  `grant-persist.sh`'s two `magisk --sqlite` writes, wrapped in a **bounded daemon-ready retry
  loop** (magiskd / `magisk.db` may not be up the instant the service fires): try up to ~10 times
  with a short sleep until `magisk --sqlite "SELECT ..."` responds, then write the policy. Resolves
  the applet via `/data/adb/magisk/magisk` (present at boot), exactly like `grant-persist.sh`.
  Self-contained — no reliance on PATH.

- **`provision/root/overlay/init.cas-grant.rc`** — an init snippet declaring a **oneshot root
  service** that runs `cas-grant.sh`, started at `on property:sys.boot_completed=1`, with
  `seclabel u:r:magisk:s0` so it runs in Magisk's unconfined domain and can talk to magiskd. Late
  trigger (boot_completed) guarantees `/data` is decrypted and the daemon is up.

> The exact on-device placement of overlay.d files (`/overlay.d/…` vs `/sbin/…`) and the working
> `seclabel` are the parts `magiskinit` version behaviour decides — pinned by the bench spike
> (below), not assumed here.

### Injection step (`cas/provision.py`)

Extend `patch_init_boot_on_device()`. After `boot_patch.sh` yields `new-boot.img` and **before**
pulling it, run one **isolated `magiskboot` pass** on the device workdir:

```
magiskboot unpack new-boot.img
magiskboot cpio ramdisk.cpio "mkdir 0755 overlay.d" \
    "add 0644 overlay.d/init.cas-grant.rc <pushed rc>" \
    "add 0755 overlay.d/cas-grant.sh       <pushed sh>"
magiskboot repack new-boot.img
```

- Magisk's own `boot_patch.sh` is **not edited** — the inject is a separate pass, so it survives
  Magisk version bumps.
- **Best-effort:** if push / `cpio` / `repack` fails, log a warning and fall through with the
  plain patched image. Root still works via the auto-tap fallback — never worse than today.
- The two overlay files are pushed from `provision/root/overlay/` into the existing on-device
  patch workdir (`DEV_PATCH`).

### `root()` reorder (`cas/provision.py`)

Make pre-authorized-at-boot the happy path. After the flash + reboot + boot:

1. `adb.is_root()` → `su id` now returns `uid=0` with no dialog → log
   `"✓ ROOTED — shell pre-authorized at boot (zero-touch, no prompt)."` and return.
2. Only if not root **and** `auto_grant_shell` is on → existing `grant_shell_root()` auto-tap
   fallback. (`bake_boot_grant` gates the injection only; `auto_grant_shell` gates this fallback.)
3. Only if that also fails → existing manual instruction.

Three graceful layers; each strictly better than the one below it.

### Config (`cas/config.py`)

- New `bake_boot_grant()` → default `True`. Gates the overlay.d injection so it can be switched off
  independently (`"bake_boot_grant": false` in `cas-config.json`) if it ever misbehaves on a model.
- Keep `auto_grant_shell()` as the fallback toggle (unchanged semantics).

### Seal / shipping — unchanged, no new guard

Seal already re-flashes **stock** `init_boot` (no overlay.d) and uninstalls the Magisk app, so a
shipped unit carries none of this. The boot script exists only while the Magisk-patched image is
flashed. (This is the concrete advantage over `ro.debuggable`, which would have required a
seal-time guard so units never ship adb-rootable.) Seal's existing post-un-root `is_root()` check
still confirms root is gone.

## Testing

**Unit (pytest, `tests/test_cas.py`):**
- Injection builds the correct `magiskboot cpio "add …"` argv for both overlay files.
- Inject failure is best-effort: `patch_init_boot_on_device` still returns the (plain) patched
  image and logs a warning rather than aborting.
- `cas-grant.sh` contains the exact policy SQL (`policies … VALUES(2000,2,0,0,0)`,
  `root_access,3`) and a bounded retry (no unbounded loop).
- `root()` returns success on a pre-authorized (already-root) boot **without** invoking the
  auto-tap; falls back to auto-tap only when not root.
- `bake_boot_grant` default is `True`; `false` skips injection.

**LF/CRLF guard:** add `provision/root/overlay/*` to the device-script LF-only guard (the guard
introduced for the CRLF manifest fix), so a CRLF regression is caught in CI.

**On-device bench spike (the gate — tests cannot prove overlay.d fires):**
Target unit: **MANGMI air-x** (bengal/SM6115, worst-case proof — if overlay.d is flaky anywhere
it is likeliest here).
1. Build a Magisk-patched `init_boot` for the air-x with a **trivial** overlay.d service (writes
   the real policy, or `touch`es a marker) injected via the `magiskboot` pass.
2. Flash, reboot.
3. Check `adb shell /debug_ramdisk/su -c id` returns `uid=0` **with no dialog on the device**, and
   the marker/policy is present.
- **Green** → build the full `cas-grant.sh` + rc + inject step + reorder + tests.
- **Red** (overlay.d ignored on this magiskinit) → stop; the layered fallback means CAS is no worse
  than today, and we redirect that effort into hardening the auto-tap instead. One bench cycle
  spent, not five.

## Order of work

1. **Bench spike** — prove overlay.d fires at all on the air-x (above). Gates everything.
2. Real `cas-grant.sh` + `init.cas-grant.rc` (LF-only) + the `magiskboot` inject step in
   `patch_init_boot_on_device()`.
3. `root()` reorder (pre-authorized happy path → auto-tap fallback → manual).
4. `config.bake_boot_grant()` + unit tests + LF guard.

## Non-goals

- Changing `seal()` beyond what already un-roots the unit.
- `userdebug`/engineering builds (already adb-rootable; out of scope).
- Removing the auto-tap fallback — kept deliberately as the middle layer.

## Risks / open questions

- **overlay.d honored on init_boot GKI units?** The core unknown; the spike answers it before any
  fleet reliance.
- **Working `seclabel` for the boot service** (`u:r:magisk:s0` vs `u:r:su:s0`) — pinned by the spike.
- **Double magiskboot repack** of an already-patched image — standard magiskboot operation, but
  confirmed by the spike producing a bootable image.
