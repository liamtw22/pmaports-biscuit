#!/bin/sh
# Keep the supervised voice-assistant service alive, but dormant, while the
# configured network interface has no IPv4 address.  Starting LVA without one
# reaches inet_aton(None), exits, and used to make supervise-daemon relaunch the
# entire Python/TFLite stack every five seconds for as long as Wi-Fi was absent.
#
# This wrapper deliberately has no logging in the wait loop: an unplugged or
# roaming hotspot is normal, and a periodic log write would merely create a
# second unattended-workload problem.  Once the address exists, exec preserves
# supervise-daemon's normal process ownership and crash recovery.

set -u

iface=${1:?missing network interface}
shift

while :; do
	addr=$(ip -4 -o addr show "$iface" 2>/dev/null || true)
	[ -z "$addr" ] || exec "$@"
	sleep 15
done
