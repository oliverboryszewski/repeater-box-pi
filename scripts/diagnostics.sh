#!/bin/bash
# Read-only inventory. It never opens a serial port or sends audio/PTT.
set -u
echo '--- OS ---'
cat /etc/os-release
uname -m
echo '--- Services ---'
systemctl --no-pager --full status repeater-dashboard.service repeater-engine.service repeater-rx-monitor.service
echo '--- Network (no saved passwords) ---'
nmcli device status
ip -4 address show
echo '--- Audio capture ---'
arecord -l
echo '--- Audio playback ---'
aplay -l
echo '--- Persistent serial identities ---'
ls -l /dev/serial/by-id/ 2>/dev/null
echo '--- USB devices ---'
lsusb
echo '--- Pi power flags ---'
vcgencmd get_throttled
echo '--- Recent engine log ---'
journalctl -u repeater-engine.service -n 35 --no-pager
echo '--- Recent receive-monitor log ---'
journalctl -u repeater-rx-monitor.service -n 35 --no-pager
