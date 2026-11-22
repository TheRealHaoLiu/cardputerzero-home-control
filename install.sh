#!/bin/sh
# Install or update the app on the CardputerZero. Run on the device as root, from this folder:
#   sudo sh install.sh
# It copies the app and its device files to /opt and adds a launcher tile. No new packages: it
# checks for python3-pil, python3-numpy, ir-ctl, pw-record and openssl, which the stock image has.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root: sudo sh install.sh" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
APPL=/usr/share/APPLaunch
DEST=/opt/cardputerzero-home-control
[ "$HERE" != "$DEST" ] || { echo "run this from a copy of the repo, not from $DEST" >&2; exit 1; }
[ -d "$APPL/applications" ] || { echo "no launcher at $APPL: is this a CardputerZero?" >&2; exit 1; }
need() { command -v "$1" >/dev/null || { echo "$1 is missing ($2)" >&2; exit 1; }; }
need python3 "package python3"
need ir-ctl "package v4l-utils"
need pw-record "PipeWire's tools"
need openssl "package openssl"
python3 -c 'import PIL, numpy' 2>/dev/null ||
    { echo "python3 cannot import PIL and numpy (packages python3-pil, python3-numpy)" >&2; exit 1; }

[ -d /opt/cardputer-zero-home-control ] && [ ! -e "$DEST" ] && mv /opt/cardputer-zero-home-control "$DEST"   # the name before 2026-11-22
install -d -m 755 "$DEST/homectl" "$DEST/devices"
rm -f "$DEST"/homectl/*.py                     # a module an older version had must not linger
rm -rf "$DEST/homectl/__pycache__"
install -m 644 "$HERE"/homectl/*.py "$DEST/homectl/"
rm -f "$DEST"/devices/*.json
install -m 644 "$HERE"/devices/*.json "$DEST/devices/"
install -m 755 "$HERE/packaging/home-control" "$APPL/bin/home-control"
python3 "$HERE/packaging/make_icon.py" "$APPL/share/images/home-control.png"
install -m 644 "$HERE/packaging/home-control.desktop" "$APPL/applications/home-control.desktop"
python3 -m compileall -q "$DEST/homectl"
echo "installed. The Home tile appears in the launcher (restart the launcher if it does not)."
