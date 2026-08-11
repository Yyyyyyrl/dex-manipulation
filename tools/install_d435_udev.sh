#!/usr/bin/env bash
# Install the narrowly scoped D435 USB permission rule used by the live UI.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_RULE="$REPO_ROOT/vendor/realsense/99-dex-d435.rules"
DESTINATION_RULE="/etc/udev/rules.d/99-dex-d435.rules"

[[ -r "$SOURCE_RULE" ]] || {
  printf 'Missing bundled rule: %s\n' "$SOURCE_RULE" >&2
  exit 1
}
command -v sudo >/dev/null 2>&1 || {
  printf 'sudo is required to install the D435 udev rule.\n' >&2
  exit 1
}
command -v udevadm >/dev/null 2>&1 || {
  printf 'udevadm is required to reload the D435 udev rule.\n' >&2
  exit 1
}

if [[ -e "$DESTINATION_RULE" ]]; then
  if cmp -s "$SOURCE_RULE" "$DESTINATION_RULE"; then
    printf 'The D435 udev rule is already installed.\n'
  else
    printf 'Refusing to overwrite a different existing rule: %s\n' "$DESTINATION_RULE" >&2
    exit 1
  fi
else
  printf 'This installs one scoped USB permission rule for Intel 8086:0b07.\n'
  read -r -p 'Type INSTALL D435 RULE to continue: ' confirmation
  [[ "$confirmation" == "INSTALL D435 RULE" ]] || {
    printf 'Installation cancelled.\n' >&2
    exit 1
  }
  sudo install -m 0644 "$SOURCE_RULE" "$DESTINATION_RULE"
fi

sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=8086

printf 'D435 rule installed and udev reloaded.\n'
printf 'Unplug and reconnect the D435 before starting the camera UI again.\n'
