#!/bin/sh
# Make the Echo usable as a Bluetooth speaker, unattended, from boot.
#
# Four independent things must all be true or audio silently fails, and each one
# fails in a way that looks like the others. All four were hand-repaired after
# every flash before this service existed:
#
#   1. The adapter must present as a LOUDSPEAKER. Class 0x240414 is set in
#      /etc/bluetooth/main.conf by the package's post-install, because BlueZ
#      takes the device class only from there. Without it the adapter reports
#      major class 0 (Miscellaneous) and a phone connects, fails to classify it
#      as a media sink, and drops the link within seconds.
#
#   2. A pairing agent must be registered, or BlueZ has nothing to answer a
#      pairing request with and the phone reports "incorrect PIN or passkey"
#      while never showing a prompt. NoInputNoOutput is correct for a device
#      with no keypad and no display: pairing is Just Works, no code either end.
#
#   3. The peer must be TRUSTED, not merely paired. Pairing always succeeded -
#      btmon showed Simple Pairing Complete, link key and encryption change -
#      but BlueZ then asks the agent to authorise the A2DP *service*, nothing
#      answers, the request times out and the link drops. A trusted device is
#      never asked. This is the one that cost the most time to find.
#
#   4. Something must move the decoded stream to the speaker. bluealsa exposes
#      the A2DP stream as a PCM; it does not play it. bluealsa-aplay does.
#      --mixer-name=PCM because this card has no 'Master' control.
#
# Bluetooth is brought up late on this device (wifi.start writes bt_now once
# wlan0 exists, because the WMT BT function-on during CONSYS bring-up breaks
# Wi-Fi firmware start), so hci0 does not exist at boot. This waits for it
# rather than assuming.
#
# Auto-trusting whatever pairs is a deliberate trade: the device has no display
# or keypad to confirm on. Pairing still requires the adapter to be pairable and
# a person physically initiating it from the phone.

PATH=/usr/sbin:/usr/bin:/sbin:/bin
LOG=/var/log/biscuit-btaudio.log

log() { echo "$(date '+%H:%M:%S') $*" >> "$LOG"; }

btctl() { timeout 10 bluetoothctl "$@" >/dev/null 2>&1; }

# 0. Make sure the adapter will advertise as a loudspeaker.
#
# The package's post-install tries this too, but cannot be relied on: if
# device-amazon-biscuit is installed before bluez, /etc/bluetooth/main.conf does
# not exist yet and the patch silently does nothing - which is exactly what
# happened. Doing it here as well is order-independent, and it is the same
# self-heal pattern wifi.start uses for wpa_supplicant.conf.
#
# Class 0x240414 = Audio/Video major, Loudspeaker minor. Without it the adapter
# reports major class 0 (Miscellaneous) and phones connect then drop. Only
# main.conf carries the device class - there is no bluetoothctl command for it -
# so bluetoothd has to be restarted if this changes anything.
CONF=/etc/bluetooth/main.conf
CLASS="0x240414"
# The device name, which biscuit-persist has already written into main.conf
# before bluetoothd started; used here only if that line has to be recreated.
NAME=$(cat /etc/hostname 2>/dev/null)
case "$NAME" in ""|*[!A-Za-z0-9-]*) NAME="Echo Dot" ;; esac
if [ -f "$CONF" ] && ! grep -qE "^[[:space:]]*Class[[:space:]]*=[[:space:]]*$CLASS" "$CONF"; then
	if grep -qE '^[[:space:]]*#?[[:space:]]*Class[[:space:]]*=' "$CONF"; then
		sed -i "s|^[[:space:]]*#\?[[:space:]]*Class[[:space:]]*=.*|Class = $CLASS|" "$CONF"
	else
		printf '\nClass = %s\n' "$CLASS" >> "$CONF"
	fi
	if grep -qE '^[[:space:]]*#?[[:space:]]*Name[[:space:]]*=' "$CONF"; then
		sed -i "s|^[[:space:]]*#\?[[:space:]]*Name[[:space:]]*=.*|Name = $NAME|" "$CONF"
	else
		printf 'Name = %s\n' "$NAME" >> "$CONF"
	fi
	log "patched $CONF with Class=$CLASS; restarting bluetooth"
	rc-service bluetooth restart >/dev/null 2>&1
	sleep 5
fi

# 1. Wait for the controller. It arrives ~25 s in, after Wi-Fi.
i=0
while [ ! -d /sys/class/bluetooth/hci0 ]; do
	i=$((i + 1))
	if [ "$i" -gt 120 ]; then
		log "hci0 never appeared after 120 s; giving up"
		exit 1
	fi
	sleep 1
done
log "hci0 present after ${i}s"

# 2. Adapter identity and state. The class itself comes from main.conf.
btctl power on
# No system-alias here. The Bluetooth name is the device name, from main.conf,
# unless the owner set a different one on the settings page, which BlueZ keeps
# as an alias. Until r287 this forced "Echo-Dot-PMOS" here and in every pairing
# window, so neither the name chosen in setup nor one set on the settings page
# ever lasted; biscuit-persist removes that old alias before bluetoothd starts.

# Closed at boot, on purpose.
#
# This used to set `pairable on`, `discoverable-timeout 0` and `discoverable on`
# here, which left the device advertising and accepting pairings for as long as
# it was switched on. Pairing is a BOUNDED WINDOW now - opened deliberately by a
# button hold, by Home Assistant or by :8080, and closed again after three
# minutes - because auto-trust is gated on that window: this device has no
# screen to confirm on, so "is a window open" is the only thing standing between
# a passer-by and a trusted pairing.
#
# Already-paired devices are unaffected: they reconnect on their own, which is
# what `trusted` is for. Only NEW pairings need the window.
btctl pairable off
btctl discoverable off
log "adapter configured, pairing closed until a window is opened"

# 3. The playback bridge.
if ! pidof bluealsa-aplay >/dev/null 2>&1; then
	start-stop-daemon --start --background --make-pidfile \
		--pidfile /run/bluealsa-aplay.pid \
		--exec /usr/bin/bluealsa-aplay -- --pcm=default --mixer-name=PCM
	sleep 2
	if pidof bluealsa-aplay >/dev/null 2>&1; then
		log "bluealsa-aplay started"
	else
		log "bluealsa-aplay FAILED to start"
	fi
fi

# 4. A registered agent, held for as long as this service runs, plus trust for
#    anything that pairs. bluetoothctl only holds the agent while it lives, so
#    the session is kept open deliberately rather than exiting after issuing the
#    commands.
{
	echo "agent NoInputNoOutput"
	sleep 1
	echo "default-agent"
	while true; do sleep 3600; done
} | bluetoothctl >> "$LOG" 2>&1 &
log "pairing agent registered (NoInputNoOutput, Just Works)"

# Trust any paired device that is not yet trusted, and keep the adapter
# advertising. Cheap enough to run every 15 s and it needs no events.
while true; do
	for addr in $(timeout 10 bluetoothctl devices Paired 2>/dev/null | awk '{print $2}'); do
		if ! timeout 10 bluetoothctl info "$addr" 2>/dev/null | grep -q "Trusted: yes"; then
			btctl trust "$addr"
			log "trusted $addr"
		fi
	done
	# Some phones turn discoverability off as a side effect of connecting, so
	# it is re-asserted - but ONLY while a pairing window is actually open.
	#
	# This used to run unconditionally, which quietly defeated the whole bounded
	# window: biscuit-pair-session (or Home Assistant, or :8080) would set
	# `discoverable off` to close pairing, and this loop turned it straight back
	# on within fifteen seconds. That is the "turns off for a second then opens
	# back up" behaviour, and between windows it left the device advertising to
	# the world permanently, which is exactly what the bounded window exists to
	# prevent.
	if [ -e /run/biscuit-pairing ]; then
		timeout 10 bluetoothctl show 2>/dev/null | grep -q "Discoverable: yes" || btctl discoverable on
	fi
	sleep 15
done
