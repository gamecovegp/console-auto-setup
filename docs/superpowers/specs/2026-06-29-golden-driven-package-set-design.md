# Golden-driven package set for diag/verify scripts

- **Date:** 2026-06-29
- **Status:** Design — awaiting review
- **Area:** `provision/root/lib-root.sh`, `provision/root/verify.sh`, `provision/root/diag/{bios_audit.sh,serial_audit.sh,dryrun.sh}`, `provision/root/restore.sh`

## 1. Background

CAS clones a golden by capturing **every third-party app on the golden** (`user_pkgs` → `pkglist.txt`),
bundling each one's version-exact APK + private data + external BIOS/keys, and reinstalling that exact
payload on each unit. The restored set (`RPKGS`) is derived from the golden's captured `pkglist.txt` via
the manifest — so the **actual clone is already golden-driven**: whatever package a role is fulfilled by on
the golden (e.g. the PS2 runner as `xyz.aethersx2.android` *or* `xyz.aethersx2.tturnip`) is exactly what
every unit receives.

**The gap:** several **diag/verify scripts** still iterate a *static* package list (`PKGS` in `lib-root.sh`,
or inline literals) instead of the golden's captured set. When an operator swaps an app's build on a golden
— the motivating case is installing standard NetherSX2 (`xyz.aethersx2.android`) so a MANGMI OEM launcher
recognizes it, instead of the Turnip repack (`xyz.aethersx2.tturnip`) — the clone still works, but these
scripts go **stale**: they check for a package that is no longer present and miss the one that is. The
result is misleading "missing BIOS"/"missing app" output, not a functional break.

This spec makes those scripts read the **golden's captured package set** so they follow the golden
automatically. It is the robustness cleanup chosen in brainstorming; the broader "role/equivalence registry"
idea is explicitly out of scope (the `payload_pkgs` helper here is the foundation it would build on later).

## 2. Goals / Non-goals

**Goals**
- A single source of truth for "the set of packages CAS cloned": the golden's `pkglist.txt`.
- Point the diag/verify/audit scripts at that source so they never go stale when a golden uses a different
  build/variant of an app.
- Keep a safe fallback for contexts where no payload is present.

**Non-goals**
- No change to capture/restore clone behavior (already golden-driven — untouched semantically).
- No role/equivalence metadata layer (`ps2-runner → <pkg>`). Future round.
- No new emulator-specific knowledge; the per-emulator BIOS *paths* in `verify.sh` stay as-is except where a
  package is variant-prone.

## 3. Key finding — only diag/verify carry the staleness

| File | Where | Hardcoded today | After |
|---|---|---|---|
| `lib-root.sh` | `PKGS=` (line 3) | static 11-pkg list | **kept as documented FALLBACK only** |
| `serial_audit.sh` | `for pkg in $PKGS` | static list | live golden set (`user_pkgs`) |
| `dryrun.sh` | §1 `for pkg in $PKGS` | static list | captured set (`payload_pkgs`) |
| `bios_audit.sh` | §3 literal 5-pkg list | inline literals | captured set (`payload_pkgs`) |
| `verify.sh` | ownership loop (l.13) + PS2 BIOS line (l.22) | inline literals incl. `…aethersx2.tturnip` | captured set + variant-resolved runner |
| `restore.sh` | l.29 fallback | inline `cat pkglist \|\| PKGS` | `payload_pkgs` (no behavior change) |

Prior art to mirror: `bios_audit.sh` §2 and `restore.sh` already read `pkglist.txt`. `verify.sh:28`'s
deviceidle regex matches the `aethersx2` substring, so it already covers both variants — **no change**.

## 4. Component design

### 4.1 `provision/root/lib-root.sh`
- **`payload_pkgs [payload_dir]`** — new helper. Echoes the newline-separated packages from
  `<payload_dir>/pkglist.txt` when that file exists and is non-empty; otherwise falls back to `$PKGS`.
  `payload_dir` defaults to the same resolution the scripts already use (`${CAS_OUT:-$(detect_sd)/golden_root_payload}`),
  so callers can pass nothing and get the right payload. Pure file IO — no adb, no root state — so it is
  locally testable.
- **`PKGS`** — value unchanged; comment demoted to: *"FALLBACK default only. The golden's captured
  `pkglist.txt` is authoritative — see `payload_pkgs`. Used when no payload is on hand."*

### 4.2 `provision/root/diag/serial_audit.sh`
- Replace `for pkg in $PKGS` with `for pkg in $(user_pkgs)`. Rationale: serial_audit runs **on the golden**
  against live `/data/data/<pkg>`, so the live installed set is the truest source (and already excludes host
  tools via `EXCLUDE_PKGS`). The `[ -d /data/data/$pkg ] || continue` guard stays.

### 4.3 `provision/root/diag/dryrun.sh`
- §1 payload-integrity loop: `for pkg in $PKGS` → `for pkg in $(payload_pkgs "$P")`.
- `SMALL` (the curated fast subset for the §3 serial-rewrite scratch test) **stays as-is** — it is a
  deliberate small-app selection, not a "which packages exist" list, and is not variant-affected (it never
  named the PS2 runner). Its per-item `[ -f data.tar ] || continue` guard already tolerates absentees.

### 4.4 `provision/root/diag/bios_audit.sh`
- §3 confirm-in-tars loop: replace the inline 5-pkg literal with `for p in $(payload_pkgs "$P")`. §1/§2
  unchanged (§2 already golden-driven).

### 4.5 `provision/root/verify.sh`
- Ownership loop (l.13): iterate `$(payload_pkgs "$SD/golden_root_payload")` instead of the three literals.
  Apps without an `Android/data` dir simply show an empty `own=` — acceptable and more complete.
- PS2 BIOS line (l.22): resolve the runner from the captured set —
  `PS2="$(payload_pkgs "$SD/golden_root_payload" | grep -m1 aethersx2)"` — and list
  `…/$PS2/files/bios/`, so the check follows `.android` or `.tturnip`. If `$PS2` is empty (no PS2 runner in
  this golden), skip the line.
- eden `prod.keys` and duckstation `bios` lines stay literal — those packages are not variant-prone, and
  hardcoding keeps the per-emulator path knowledge readable. (Generalizing them is YAGNI this round.)

### 4.6 `provision/root/restore.sh`
- Line 29 fallback `RPKGS="$(cat "$P/pkglist.txt" 2>/dev/null)"; [ -n "$RPKGS" ] || RPKGS="$PKGS"` →
  `RPKGS="$(payload_pkgs "$P")"`. Pure consistency refactor; identical behavior (manifest path at l.26 is
  unchanged and still takes precedence).

## 5. Data flow

Operator swaps an app build on the golden (e.g. NetherSX2 `.tturnip` → `.android`) → re-`capture.sh` →
`pkglist.txt` now lists `xyz.aethersx2.android` → `payload_pkgs`/`user_pkgs` return the new set → every
diag/verify/audit tool checks the package the golden actually carries. No hand-editing of any script.

## 6. Error handling

- `payload_pkgs` with no readable, non-empty `pkglist.txt` falls back to `$PKGS` (best-effort default) — it
  never returns empty silently unless `PKGS` itself were emptied. Scripts keep their existing per-item
  guards (`[ -d … ] || continue`, `[ -f …/data.tar ] || …`), so an absent package is skipped/warned, not a
  hard error — unchanged contract.
- All touched scripts are **read-only diagnostics** except `restore.sh`, whose change is behavior-preserving.

## 7. Testing

- **`payload_pkgs` local smoke test** (no device): create a temp dir with a `pkglist.txt`, source
  `lib-root.sh`, assert `payload_pkgs <dir>` echoes those lines; assert the empty/missing-file case falls
  back to `$PKGS`. Runnable on the dev host (pure file IO).
- **Device-side integrations** carry `[VERIFY on device]` markers per existing convention: on a real golden,
  run `dryrun.sh`/`bios_audit.sh`/`serial_audit.sh` and, on a restored unit, `verify.sh` — confirm each
  enumerates the golden's actual package set (and, after a deliberate `.tturnip`→`.android` swap, follows
  it).
- **Python suite** (`tests/test_cas.py`) stays green — it does not exercise these shell scripts, so this is a
  no-op there. Confirm with a run.

## 8. Out of scope / future

- Role/equivalence registry (`@role ps2-runner <pkg>`), which would let tooling and any future OEM-launcher
  config reference a role independent of the exact package. `payload_pkgs` is the substrate for it.
- Generalizing `verify.sh`'s per-emulator BIOS/keys path checks into a package→path map.
