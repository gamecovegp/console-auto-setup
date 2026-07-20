#!/usr/bin/env bash
# Phase timing: now_s() (lib-root) + the phase totals restore.sh reports.
# Run: bash tests/test_phase_timing.sh
#
# WHY: a Download reports ONE total (measured: 1746 MB in 619s). At the observed 16 MB/s link speed the
# bytes themselves only account for ~110s, so ~80% of the run is unattributed — and nobody can say whether
# it is the APK installs (dexopt) or the data restore without splitting them. install and data are two
# separate loops in restore.sh, so the split is cheap and exact.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/provision/root/lib-root.sh"
fail=0

# === now_s: integer epoch seconds, usable for arithmetic =======================================
a="$(now_s)"
case "$a" in ''|*[!0-9]*) echo "FAIL(1): now_s must return bare digits, got [$a]"; fail=1 ;; esac
b=$(( a + 3 ))
[ "$b" -gt "$a" ] || { echo "FAIL(2): now_s output is not usable in arithmetic"; fail=1; }

# === restore.sh must TIME the two phases and REPORT them =======================================
R="$ROOT/provision/root/restore.sh"

# install loop (loop 1) and data loop (loop 2) each bracketed by a timer
grep -q 'T_INSTALL=' "$R" || { echo "FAIL(3): restore.sh does not time the APK-install phase"; fail=1; }
grep -q 'T_DATA=' "$R"    || { echo "FAIL(4): restore.sh does not time the data-restore phase"; fail=1; }

# and both must be surfaced, or the measurement is invisible to the operator
grep -q 'phase totals' "$R" || { echo "FAIL(5): restore.sh never reports the phase totals"; fail=1; }

# The report has to come BEFORE the failure exit, otherwise a failing restore -- exactly the run you most
# want to profile -- exits 1 and prints nothing.
rep_line="$(grep -n 'phase totals' "$R" | head -1 | cut -d: -f1)"
exit_line="$(grep -n 'exit 1' "$R" | tail -1 | cut -d: -f1)"
if [ -n "$rep_line" ] && [ -n "$exit_line" ]; then
  [ "$rep_line" -lt "$exit_line" ] \
    || { echo "FAIL(6): phase totals (line $rep_line) must be reported BEFORE the failure exit (line $exit_line)"; fail=1; }
fi

[ "$fail" = 0 ] && echo "ok: now_s + restore.sh phase totals (install vs data)" || exit 1
