#!/usr/bin/env bash
# One-time Linux setup for CAS on-device flashing.
#
# Installs the udev rule that grants the operator access to the Qualcomm EDL (9008) serial port, so
# EDL flashing (MANGMI / AYN root + firmware) works WITHOUT running CAS as root. Fastboot handhelds
# (Retroid Pocket 6) already work via the distro's android udev rules; this closes the EDL gap so
# root/flash works regardless of device family.
#
# Safe to re-run (idempotent). Needs sudo for the /etc + udev bits.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULE_SRC="$HERE/udev/99-cas-flashing.rules"
RULE_DST="/etc/udev/rules.d/99-cas-flashing.rules"

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
[ -f "$RULE_SRC" ] || { echo "!! rule not found: $RULE_SRC" >&2; exit 1; }

echo ">> installing udev rule -> $RULE_DST"
$SUDO install -m 0644 "$RULE_SRC" "$RULE_DST"

# Group that owns tty/usb serial nodes differs by distro (Arch=uucp, Debian/Ubuntu=dialout). Add the
# invoking user to whichever exists — this is the headless/SSH fallback; local desktop sessions are
# already covered by the rule's TAG+="uaccess".
TARGET_USER="${SUDO_USER:-$USER}"
for grp in uucp dialout; do
  if getent group "$grp" >/dev/null 2>&1; then
    echo ">> adding '$TARGET_USER' to group '$grp' (headless fallback; needs re-login to take effect)"
    $SUDO usermod -aG "$grp" "$TARGET_USER" || true
  fi
done

echo ">> reloading udev + applying to any currently-connected device"
$SUDO udevadm control --reload-rules
$SUDO udevadm trigger --subsystem-match=tty --action=add
$SUDO udevadm trigger --subsystem-match=usb --action=add

echo ">> done."
echo "   A unit already in EDL now grants you /dev/ttyUSB* access (check: getfacl /dev/ttyUSB0)."
echo "   Group membership (headless use) applies after your next login."
