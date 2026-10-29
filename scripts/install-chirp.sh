#!/bin/bash
# Install only the pinned upstream library needed by the programming worker.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo 'Run this through sudo bash scripts/install-pi.sh.' >&2
  exit 1
fi
chirp_commit=0b4076d9b19a2a133d11f31546cff8153b39eb89
chirp_sha256=b40d6a6ae77e580bb43db134e54af12bc85e1bcb9b8fb9f544c0abc4465ff191
vendor_dir=/opt/repeater-box/vendor
if [[ -r "$vendor_dir/chirp/PINNED_COMMIT" ]] && [[ $(cat "$vendor_dir/chirp/PINNED_COMMIT") == "$chirp_commit" ]]; then
  PYTHONPATH="$vendor_dir/chirp" /usr/bin/python3 -c 'import serial; from chirp.drivers.baofeng_wp970i import BF5RXRadio'
  exit 0
fi
install -d -o root -g root -m 755 "$vendor_dir"
chirp_stage=$(mktemp -d "$vendor_dir/.chirp-install-XXXXXXXX")
trap 'rm -rf -- "$chirp_stage"' EXIT
curl --fail --location --proto '=https' --proto-redir '=https' --max-time 120 \
  "https://codeload.github.com/kk7ds/chirp/tar.gz/$chirp_commit" -o "$chirp_stage/source.tar.gz"
echo "$chirp_sha256  $chirp_stage/source.tar.gz" | sha256sum --check --status
mkdir "$chirp_stage/source"
tar --extract --gzip --file "$chirp_stage/source.tar.gz" --strip-components=1 \
  --directory "$chirp_stage/source" --no-same-owner --no-same-permissions
PYTHONPATH="$chirp_stage/source" /usr/bin/python3 -c 'import serial; from chirp.drivers.baofeng_wp970i import BF5RXRadio'
printf '%s\n' "$chirp_commit" > "$chirp_stage/source/PINNED_COMMIT"
# Keep the library, license and provenance; test images and the GUI are unused.
mkdir "$chirp_stage/library"
cp -R "$chirp_stage/source/chirp" "$chirp_stage/library/chirp"
cp "$chirp_stage/source/COPYING" "$chirp_stage/source/PINNED_COMMIT" "$chirp_stage/library/"
find "$chirp_stage/library" -type d -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$chirp_stage/library"
chmod -R go-w "$chirp_stage/library"
rm -rf -- "$vendor_dir/chirp.previous"
if [[ -e "$vendor_dir/chirp" ]]; then mv "$vendor_dir/chirp" "$vendor_dir/chirp.previous"; fi
mv "$chirp_stage/library" "$vendor_dir/chirp"
echo 'Installed pinned CHIRP library for the Baofeng 5RX profile.'
