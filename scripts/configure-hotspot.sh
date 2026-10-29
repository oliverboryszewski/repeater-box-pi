#!/bin/bash
# Run from Ethernet SSH or a directly attached keyboard/monitor, ON THE PI.
set -euo pipefail
export LC_ALL=C
if [[ $EUID -ne 0 ]]; then
  echo 'Run: sudo bash scripts/configure-hotspot.sh' >&2
  exit 1
fi
command -v nmcli >/dev/null || { echo 'NetworkManager is required.' >&2; exit 1; }
if [[ -n ${SSH_CONNECTION:-} ]]; then
  read -r client_ip client_port server_ip server_port <<< "$SSH_CONNECTION"
  route=$(ip route get "$client_ip")
  if [[ " $route " == *' dev wlan0 '* ]]; then
    echo 'This SSH connection uses wlan0. Switch to Ethernet or a local keyboard before changing wlan0 to a hotspot.' >&2
    exit 1
  fi
fi
if nmcli -g connection.id connection show repeater-ap >/dev/null 2>&1; then
  echo 'The repeater-ap profile already exists. It has not been changed.'
  echo 'Activate it with: sudo nmcli connection up repeater-ap'
  exit 0
fi
if ! nmcli -t -f DEVICE,TYPE device | grep -q '^wlan0:wifi$'; then
  echo 'No wlan0 Wi-Fi interface found. Check the wireless country and radio state.' >&2
  exit 1
fi
if ip -4 address show | grep -q 'inet 10\.42\.0\.'; then
  echo '10.42.0.0/24 already appears on this Pi. Review the subnet before creating this profile.' >&2
  exit 1
fi
echo 'Creates Repeater-Box Wi-Fi at 10.42.0.1, using the Pi built-in radio.'
echo 'Set the wireless country to US in Imager or raspi-config first.'
read -r -s -p 'New Wi-Fi password (8-63 printable ASCII characters): ' hotspot_pass
echo
if [[ ${#hotspot_pass} -lt 8 || ${#hotspot_pass} -gt 63 || "$hotspot_pass" == *[!\ -\~]* ]]; then
  echo 'Password must contain 8-63 printable ASCII characters.' >&2
  exit 1
fi
read -r -s -p 'Repeat password: ' hotspot_again
echo
[[ "$hotspot_pass" == "$hotspot_again" ]] || { echo 'Passwords do not match.' >&2; exit 1; }
unset hotspot_again
# Add the profile inactive. No disconnection occurs until the final activation.
nmcli connection add type wifi ifname wlan0 con-name repeater-ap ssid Repeater-Box connection.autoconnect no
# Remove only this newly created, still-inactive profile if setup fails midway.
# A failed run must not leave an incomplete profile for accidental activation.
profile_complete=0
cleanup_profile() {
  unset hotspot_pass
  if [[ $profile_complete == 0 ]]; then
    nmcli connection delete repeater-ap >/dev/null 2>&1 || true
  fi
}
trap cleanup_profile EXIT
nmcli connection modify repeater-ap \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 802-11-wireless.hidden no \
  802-11-wireless-security.key-mgmt wpa-psk \
  802-11-wireless-security.proto rsn \
  802-11-wireless-security.pairwise ccmp \
  802-11-wireless-security.group ccmp \
  ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled \
  connection.autoconnect-priority 100 \
  802-11-wireless-security.psk "$hotspot_pass"
profile_complete=1
unset hotspot_pass
nmcli radio wifi on
nmcli connection modify repeater-ap connection.autoconnect yes
nmcli connection up repeater-ap
echo
echo 'Wi-Fi: Repeater-Box'
echo 'Dashboard: http://10.42.0.1'
echo 'Choose Stay connected / Use without internet on your phone if prompted.'
