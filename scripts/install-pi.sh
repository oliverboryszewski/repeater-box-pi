#!/bin/bash
# Run ON a fresh Raspberry Pi OS Lite 64-bit installation, with internet access.
set -euo pipefail
export LC_ALL=C
if [[ $EUID -ne 0 ]]; then
  echo 'Run: sudo bash scripts/install-pi.sh' >&2
  exit 1
fi
if [[ ! -r /proc/device-tree/model ]] || ! grep -q 'Raspberry Pi' /proc/device-tree/model; then
  echo 'This installer is for the Raspberry Pi, not the Mac used to prepare the card.' >&2
  exit 1
fi
if [[ $(dpkg --print-architecture) != arm64 ]]; then
  echo 'Use Raspberry Pi OS Lite (64-bit / arm64) for this build.' >&2
  exit 1
fi
bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for required in dashboard/server.py dashboard/static/index.html config/svxlink-bench.conf systemd/repeater-dashboard.service systemd/repeater-engine.service; do
  [[ -f "$bundle_dir/$required" ]] || { echo "Missing bundle file: $required" >&2; exit 1; }
done
if systemctl is-active --quiet svxlink.service; then
  echo 'An existing SvxLink service is running. Stop here and review that installation before using this fresh-build installer.' >&2
  exit 1
fi
rx_monitor_active=0
if systemctl is-active --quiet repeater-rx-monitor.service; then rx_monitor_active=1; fi
rx_monitor_enabled=0
if systemctl is-enabled --quiet repeater-rx-monitor.service; then rx_monitor_enabled=1; fi
# Prevent package installation from starting the distributor's example RF setup.
# It remains masked: our separate service uses only the hardware-free bench file.
systemctl mask svxlink.service
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 alsa-utils usbutils avahi-daemon network-manager dnsmasq-base wpasupplicant svxlink-server
getent group repeater-recordings >/dev/null || groupadd --system repeater-recordings
getent group repeaterdash >/dev/null || groupadd --system repeaterdash
id repeaterdash >/dev/null 2>&1 || useradd --system --gid repeaterdash --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin repeaterdash
usermod -a -G repeater-recordings,audio repeaterdash
usermod -a -G repeater-recordings,audio,dialout svxlink
# vcgencmd is read-only here; video grants access on Pi OS versions that require it.
if getent group video >/dev/null; then usermod -a -G video repeaterdash; fi
install -d -o root -g root -m 755 /opt/repeater-box/dashboard/static /opt/repeater-box/scripts /etc/repeater-box
install -m 644 "$bundle_dir/dashboard/"{server.py,controls.py,audio_hub.py,admin_password.py,wifi_visibility.py,software_updates.py} /opt/repeater-box/dashboard/
install -m 644 "$bundle_dir/scripts/apply-wifi-visibility.py" /opt/repeater-box/scripts/apply-wifi-visibility.py
install -m 644 "$bundle_dir/scripts/apply-software-update.py" /opt/repeater-box/scripts/apply-software-update.py
install -m 644 "$bundle_dir/software-update.json" /opt/repeater-box/software-update.json
# A manual install may replace a previously tracked GitHub commit. Do not keep
# claiming that old commit is installed; the first browser update establishes it.
/usr/bin/python3 - <<'PY'
import json
import os
from pathlib import Path
import tempfile
root = Path('/opt/repeater-box')
version = json.loads((root / 'software-update.json').read_text())['version']
with tempfile.NamedTemporaryFile(mode='w', dir=root, prefix='.installed-', delete=False) as stream:
    temporary = Path(stream.name)
    json.dump({'version': version, 'commit': None}, stream)
    stream.write('\n')
    stream.flush()
    os.fsync(stream.fileno())
os.chmod(temporary, 0o644)
os.replace(temporary, root / 'update-installed.json')
PY
if [[ ! -e /etc/repeater-box/updates.json ]]; then
  install -o root -g root -m 644 "$bundle_dir/config/updates.json" /etc/repeater-box/updates.json
fi
install -m 644 "$bundle_dir/scripts/check-bench-config.py" /opt/repeater-box/check-bench-config.py
install -m 644 "$bundle_dir/dashboard/static/"{index.html,app.js,style.css} /opt/repeater-box/dashboard/static/
# Never overwrite a locally edited bench configuration on a later rerun.
if [[ ! -e /etc/repeater-box/svxlink-bench.conf ]]; then
  install -m 644 "$bundle_dir/config/svxlink-bench.conf" /etc/repeater-box/svxlink-bench.conf
fi
install -d -o svxlink -g repeater-recordings -m 2770 /var/lib/svxlink/recordings
install -d -o repeaterdash -g repeaterdash -m 750 /var/lib/repeater-dashboard
install -m 644 "$bundle_dir/systemd/"{repeater-dashboard.service,repeater-engine.service,repeater-rx-monitor.service} /etc/systemd/system/
install -m 644 "$bundle_dir/systemd/"{repeater-wifi-visibility.service,repeater-wifi-visibility.path} /etc/systemd/system/
install -m 644 "$bundle_dir/systemd/"{repeater-software-update.service,repeater-software-update.path} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now avahi-daemon.service
systemctl enable --now repeater-wifi-visibility.path
systemctl enable --now repeater-software-update.path
systemctl enable repeater-dashboard.service
systemctl restart repeater-dashboard.service
# Only start the bench engine when all three hardware-off guards are present.
/usr/bin/python3 "$bundle_dir/scripts/check-bench-config.py" /etc/repeater-box/svxlink-bench.conf
if [[ $rx_monitor_active == 1 || $rx_monitor_enabled == 1 ]]; then
  echo 'Preserving the existing receive-monitor mode; bench engine was not started.'
else
  systemctl enable --now repeater-engine.service
fi
echo
echo 'Installed: tabbed dashboard + hardware-free SvxLink bench engine.'
echo 'Dashboard: http://repeater-box.local (or use the Pi IP address).'
echo 'Set the dashboard admin password locally:'
echo 'sudo python3 /opt/repeater-box/dashboard/admin_password.py'
echo 'Next: test the dashboard, then configure the portable Wi-Fi hotspot.'
echo 'The vendor svxlink.service remains masked. No radio hardware has been enabled.'
