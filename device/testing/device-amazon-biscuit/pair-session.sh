#!/bin/sh
# Start a bluetoothctl pairing session whose stdin is a FIFO, so answers can be
# injected on demand:  echo yes > /run/biscuit-pair/btpipe   (as root)
#
# $1 = peer address to unpair first (optional)
# $2 = agent capability (default NoInputNoOutput)
#
# NoInputNoOutput makes the SSP association model Just Works: neither side is
# asked to confirm a passkey. Confirming live over SSH proved too slow to beat
# the pairing timeout, so the default is now no-prompt. Pass KeyboardDisplay
# instead if a model that needs an answer is wanted.
set -e

# This was /tmp/btpipe, a FIXED name in a world-writable directory, and further
# down it was explicitly chmod 666. That FIFO is the standard input of a
# bluetoothctl running as root: anything that can write to it can issue
# arbitrary bluetoothctl commands - trust an address, pair a device, power the
# adapter - not merely answer a prompt. Any local process could.
#
# Nothing needed that. A sweep of this repository and of the device found no
# other reader or writer: the only writers are the two root-run blocks in this
# script. The 666 supported hand-injecting answers over SSH, and the header
# above already records that this was abandoned as too slow to beat the pairing
# timeout - which is why the default capability is NoInputNoOutput, whose Just
# Works model is never asked to confirm anything.
#
# /run is root-owned and matches every other runtime path here
# (/run/biscuit-ring, /run/biscuit-mic, /run/biscuit-peripheral). The directory
# is created 0700 so the FIFO cannot be pre-created underneath us either.
PAIR_DIR=/run/biscuit-pair
PIPE="$PAIR_DIR/btpipe"
mkdir -p "$PAIR_DIR"
chmod 700 "$PAIR_DIR"
# The agent's own output.
#
# THIS WAS a path under a developer's home directory, which does not
# exist on any device. The account here is `user`, so the redirect failed with
# "can't create .../pair.log: nonexistent directory", the backgrounded
# `bluetoothctl --agent` never started, and every command written to the pipe
# after it went nowhere. So `pairable on` and `discoverable on` were never
# applied and no agent was registered to answer a pairing request - which is the
# failure biscuit-btaudio.sh documents as "the phone reports incorrect PIN or
# passkey". The window looked open (the marker below is still written) and
# nothing could actually pair through it.
#
# /var/log matches every other service here, and the directory always exists.
LOG=/var/log/biscuit-pair-session.log
PEER="$1"
CAP="${2:-NoInputNoOutput}"

# The ring already shows connect/disconnect; pairing had no binding at all.
# Stopped on every exit path below: an animation left looping on a blank frame
# outranks everything under it and kills the ring.
RING_FIFO=/run/biscuit-ring/control

# Ring animations are user-customisable from the settings page. biscuit-settings
# writes /opt/persist/led-anim.env with one LED_<activity> per activity, already
# resolved to a concrete animation name. Sourced rather than parsed so no Python
# lands on the boot path, and every use falls back to OUR act_ animation so a
# missing or partial file can never leave the ring dark. The fallbacks used to
# name stock animations; those are no longer shipped, so a fallback to one
# would have left the ring dark in exactly the case it exists to cover.
#
# The `if` form is deliberate: `[ -r f ] && . f` evaluates false when the file is
# absent, which aborts any caller running under `set -e`.
if [ -r /opt/persist/led-anim.env ]; then
	. /opt/persist/led-anim.env
fi
# Exported because the watchdog below runs in a single-quoted `sh -c`, which does
# not expand it at write time - the child shell reads it from the environment.
# Two `stop` sites there used to hardcode "btpair-setup"; with the name now
# user-selectable that would have left a chosen animation spinning forever.
# How long the device advertises. Long enough to find it in a phone's list and
# tap it, short enough that walking away does not leave it open.
WINDOW_S=180
# Exported for the same reason PAIR_ANIM is: the watchdog below runs in a
# single-quoted `setsid sh -c`, so it reads this from the environment. Unexported
# it is empty there, `[ "$i" -lt "" ]` errors, the loop falls straight through
# and its cleanup closes the window it just opened.
export WINDOW_S

PAIR_ANIM=${LED_bt_pairing:-act_bt_pairing}
export PAIR_ANIM
ring() { [ -p "$RING_FIFO" ] && echo "$1" > "$RING_FIFO" 2>/dev/null; return 0; }
earcon() { /usr/bin/biscuit-earcon "$1" >/dev/null 2>&1 || true; }
# The sound that goes with the animation, resolved from the same
# activity->sound table. Silent no-op and always true, so audio can
# never be the thing that fails a boot, a Wi-Fi bring-up or a setup.
trap 'ring "stop $PAIR_ANIM"; rm -f /run/biscuit-pairing; bluetoothctl discoverable off >/dev/null 2>&1; bluetoothctl pairable off >/dev/null 2>&1' INT TERM

pkill -f pair-window.sh 2>/dev/null || true
pkill bluetoothctl 2>/dev/null || true
sleep 1

rm -f "$PIPE"
mkfifo "$PIPE"
nohup sh -c "sleep 3600 > $PIPE" >/dev/null 2>&1 &

nohup bluetoothctl --agent "$CAP" < "$PIPE" > "$LOG" 2>&1 &
sleep 2

{
	printf 'power on\n'
	# No system-alias here. This used to force "Echo-Dot-PMOS" every time a
	# window opened, overwriting the device name and any Bluetooth name the
	# owner had set; the name is biscuit-btaudio's and the owner's to decide.
	[ -n "$PEER" ] && printf 'remove %s\n' "$PEER"
	printf 'pairable on\n'
	# Bounded by bluetoothd itself as well as by this script, so a crash here
	# cannot leave the device advertising for ever.
	printf 'discoverable-timeout %s\n' "$WINDOW_S"
	printf 'discoverable on\n'
} > "$PIPE"

# A marker other services can see. biscuit-btaudio only auto-trusts while this
# exists, so the window is what authorises a new device rather than mere
# proximity.
: > /run/biscuit-pairing

sleep 2
# Was 666. Root is the only writer - see the PIPE comment above - and this FIFO
# is bluetoothctl's stdin, so anything writable by others is a command channel
# into a privileged process.
chmod 600 "$PIPE"

ring "play $PAIR_ANIM"
earcon bt_pairing

# Trust whatever pairs, for three minutes.
#
# Pairing itself always worked - btmon confirms Simple Pairing Complete, a Link
# Key Notification and Encryption Change, all successful. What failed was the
# *service* authorisation afterwards: BlueZ asks the agent to authorise A2DP,
# this agent is driven by a FIFO with nobody answering, and the request times
# out. The phone shows "connecting" for about a minute, then a hollow
# "connected", and BlueZ drops the link. A trusted device is never asked.
# setsid, and the loop inlined rather than called as a function.
#
# Two things this gets wrong if written the obvious way. Backgrounding alone,
# even with all three streams redirected, leaves the loop in the caller's
# process group, so an ssh invocation of this script hangs for the full three
# minutes instead of returning. And `declare -f` to export the function is
# bash-only; this runs under ash.
setsid sh -c '
	i=0
	while [ "$i" -lt "$WINDOW_S" ]; do
		bluetoothctl devices Paired 2>/dev/null | while read -r _ addr _; do
			[ -n "$addr" ] || continue
			if ! bluetoothctl info "$addr" 2>/dev/null | grep -q "Trusted: yes"; then
				bluetoothctl trust "$addr" >/dev/null 2>&1
				# Paired: the window has done its job, retire the animation
				# and let btconnect take over.
				[ -p /run/biscuit-ring/control ] &&
					echo "stop $PAIR_ANIM" > /run/biscuit-ring/control 2>/dev/null
			fi
		done
		i=$((i + 1))
		sleep 1
	done
	[ -p /run/biscuit-ring/control ] &&
		echo "stop $PAIR_ANIM" > /run/biscuit-ring/control 2>/dev/null
	# Shut the window: stop advertising and stop auto-trusting. Both, because
	# bluetoothd forgetting its own timeout would otherwise leave the device
	# open with nothing to notice.
	# Marker first: biscuit-btaudio only re-asserts discoverability
	# while it exists, so clearing the adapter first would race it.
	rm -f /run/biscuit-pairing
	bluetoothctl discoverable off >/dev/null 2>&1
	bluetoothctl pairable off >/dev/null 2>&1
' </dev/null >/dev/null 2>&1 &

echo "session up with agent $CAP (discoverable and auto-trusting for ${WINDOW_S}s)"
