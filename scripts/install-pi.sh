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
for required in dashboard/server.py dashboard/radio_programming.py dashboard/radio_editor.py dashboard/dtmf_control.py dashboard/static/index.html dashboard/static/radio-editor.html scripts/program-radio.py scripts/radio_plan.py scripts/configure-channel-pairs.py scripts/configure-dtmf.py scripts/dtmf-client.py scripts/repeater-box-events.tcl scripts/run-svxlink.py scripts/build-dtmf-prompts.py scripts/install-chirp.sh config/svxlink-bench.conf systemd/repeater-dashboard.service systemd/repeater-engine.service; do
  [[ -f "$bundle_dir/$required" ]] || { echo "Missing bundle file: $required" >&2; exit 1; }
done
# Serialize manual installs with browser updates and radio programming.
install -d -o root -g root -m 755 /opt/repeater-box
exec 9>/opt/repeater-box/.maintenance.lock
chmod 600 /opt/repeater-box/.maintenance.lock
flock -n 9 || { echo 'A radio programming or software maintenance task is running. Retry when it finishes.' >&2; exit 1; }
if systemctl is-active --quiet svxlink.service; then
  echo 'An existing SvxLink service is running. Stop here and review that installation before using this fresh-build installer.' >&2
  exit 1
fi
# Capture legacy station choices privately before replacing the old helper.
# This runs under fd 9's maintenance lock and never opens a radio connection.
getent group repeaterdash >/dev/null || groupadd --system repeaterdash
install -d -o root -g root -m 755 /etc/repeater-box
/usr/bin/python3 - "$bundle_dir" <<'CHANNEL_CATALOG_PY'
import grp
import importlib.util
from pathlib import Path
import sys
path = Path(sys.argv[1]) / 'scripts' / 'configure-channel-pairs.py'
spec = importlib.util.spec_from_file_location('channel_pair_migration', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.migrate_installed(group_id=grp.getgrnam('repeaterdash').gr_gid)
CHANNEL_CATALOG_PY
rx_monitor_active=0
if systemctl is-active --quiet repeater-rx-monitor.service; then rx_monitor_active=1; fi
rx_monitor_enabled=0
if systemctl is-enabled --quiet repeater-rx-monitor.service; then rx_monitor_enabled=1; fi
# Prevent package installation from starting the distributor's example RF setup.
# It remains masked: our separate service uses only the hardware-free bench file.
systemctl mask svxlink.service
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-serial curl ca-certificates alsa-utils usbutils avahi-daemon network-manager dnsmasq-base wpasupplicant svxlink-server espeak-ng sox
bash "$bundle_dir/scripts/install-chirp.sh"
getent group repeater-recordings >/dev/null || groupadd --system repeater-recordings
getent group repeaterdash >/dev/null || groupadd --system repeaterdash
getent group repeater-control >/dev/null || groupadd --system repeater-control
id repeaterdash >/dev/null 2>&1 || useradd --system --gid repeaterdash --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin repeaterdash
usermod -a -G repeater-recordings,audio,repeater-control repeaterdash
usermod -a -G repeater-recordings,audio,dialout,repeater-control svxlink
# vcgencmd is read-only here; video grants access on Pi OS versions that require it.
if getent group video >/dev/null; then usermod -a -G video repeaterdash; fi
install -d -o root -g root -m 755 /opt/repeater-box/dashboard/static /opt/repeater-box/scripts /etc/repeater-box
install -m 644 "$bundle_dir/dashboard/"{server.py,controls.py,audio_hub.py,admin_password.py,wifi_visibility.py,software_updates.py,radio_programming.py,radio_editor.py,dtmf_control.py} /opt/repeater-box/dashboard/
install -m 644 "$bundle_dir/scripts/apply-wifi-visibility.py" /opt/repeater-box/scripts/apply-wifi-visibility.py
install -m 644 "$bundle_dir/scripts/apply-software-update.py" /opt/repeater-box/scripts/apply-software-update.py
install -m 644 "$bundle_dir/scripts/"{program-radio.py,radio_plan.py,configure-channel-pairs.py,configure-radios.py,configure-pair-switching.py} /opt/repeater-box/scripts/
install -m 644 "$bundle_dir/scripts/"{configure-dtmf.py,dtmf-client.py,repeater-box-events.tcl,run-svxlink.py,build-dtmf-prompts.py,check-bench-config.py} /opt/repeater-box/scripts/
install -d -o root -g root -m 755 /opt/repeater-box/prompts
/usr/bin/python3 /opt/repeater-box/scripts/build-dtmf-prompts.py
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
# Enable only the restricted decoder path in an already-configured RX monitor.
# TX=NONE and NO_REPEAT=1 remain mandatory; no RF service or PTT is created.
/usr/bin/python3 - <<'DTMF_MONITOR_PY'
import importlib.util
spec = importlib.util.spec_from_file_location('dtmf_monitor_migration', '/opt/repeater-box/scripts/configure-dtmf.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.migrate_monitor()
DTMF_MONITOR_PY
install -m 644 "$bundle_dir/dashboard/static/"{index.html,app.js,style.css} /opt/repeater-box/dashboard/static/
install -m 644 "$bundle_dir/dashboard/static/"{audio-visualizer.js,audio-visualizer.css} /opt/repeater-box/dashboard/static/
install -m 644 "$bundle_dir/dashboard/static/"{radio-editor.html,radio-editor.js,radio-editor.css} /opt/repeater-box/dashboard/static/
# Never overwrite a locally edited bench configuration on a later rerun.
if [[ ! -e /etc/repeater-box/svxlink-bench.conf ]]; then
  install -m 644 "$bundle_dir/config/svxlink-bench.conf" /etc/repeater-box/svxlink-bench.conf
fi
install -d -o svxlink -g repeater-recordings -m 2770 /var/lib/svxlink/recordings
install -d -o repeaterdash -g repeaterdash -m 750 /var/lib/repeater-dashboard
install -d -o root -g repeaterdash -m 750 /var/lib/repeater-radio-backups
# Re-project legacy pending uploads for the new repair controls while the
# installer holds the maintenance lock. No radio adapter is constructed.
/usr/bin/python3 - <<'RADIO_STATE_PY'
import grp
import importlib.util
spec = importlib.util.spec_from_file_location('radio_state_refresh', '/opt/repeater-box/scripts/program-radio.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.Worker(None, group_id=grp.getgrnam('repeaterdash').gr_gid).publish()
RADIO_STATE_PY
if [[ ! -e /etc/repeater-box/radio-pair.json ]]; then
  /usr/bin/python3 - <<'PY'
import grp, json, os
path = '/etc/repeater-box/radio-pair.json'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
with os.fdopen(fd, 'w') as stream:
    os.fchown(stream.fileno(), 0, grp.getgrnam('repeaterdash').gr_gid)
    json.dump({'schema': 1, 'enabled': False}, stream)
    stream.write('\n')
    stream.flush()
    os.fsync(stream.fileno())
PY
fi
install -m 644 "$bundle_dir/systemd/"{repeater-dashboard.service,repeater-engine.service,repeater-rx-monitor.service} /etc/systemd/system/
install -m 644 "$bundle_dir/systemd/"{repeater-wifi-visibility.service,repeater-wifi-visibility.path} /etc/systemd/system/
install -m 644 "$bundle_dir/systemd/"{repeater-software-update.service,repeater-software-update.path} /etc/systemd/system/
install -m 644 "$bundle_dir/systemd/"{repeater-radio-programming.service,repeater-radio-programming.path} /etc/systemd/system/
install -m 644 "$bundle_dir/systemd/"{repeater-dtmf-code.service,repeater-dtmf-code.path} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now avahi-daemon.service
systemctl enable --now repeater-wifi-visibility.path
systemctl enable --now repeater-software-update.path
systemctl enable --now repeater-radio-programming.path
systemctl enable --now repeater-dtmf-code.path
systemctl enable repeater-dashboard.service
systemctl restart repeater-dashboard.service
# Only start the bench engine when all three hardware-off guards are present.
/usr/bin/python3 "$bundle_dir/scripts/check-bench-config.py" /etc/repeater-box/svxlink-bench.conf
radio_verification_pending=$(/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
path = Path('/var/lib/repeater-radio-backups/state.json')
try:
    state = json.loads(path.read_text())
    pending = not isinstance(state, dict) or any(
        isinstance(item, dict) and item.get('verification_required')
        for item in state.values())
except FileNotFoundError:
    pending = False
except (OSError, ValueError):
    pending = True
print('1' if pending else '0')
PY
)
if [[ $radio_verification_pending == 1 ]]; then
  echo 'An unfinished radio upload needs verification; radio services were not started.'
elif [[ $rx_monitor_active == 1 || $rx_monitor_enabled == 1 ]]; then
  /usr/bin/python3 /opt/repeater-box/check-bench-config.py /etc/repeater-box/svxlink-rx-monitor.conf --rx-monitor
  if [[ $rx_monitor_active == 1 ]]; then systemctl restart repeater-rx-monitor.service; fi
  echo 'Preserved receive-only monitor mode with the restricted DTMF decoder; TX remains NONE.'
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
echo 'On-demand programming requires local AIOC bindings: sudo python3 scripts/configure-radios.py --help'
echo 'Configure private channel pairs locally: sudo python3 /opt/repeater-box/scripts/configure-channel-pairs.py'
echo 'Optional separate RF control code: sudo python3 /opt/repeater-box/scripts/configure-dtmf.py'
