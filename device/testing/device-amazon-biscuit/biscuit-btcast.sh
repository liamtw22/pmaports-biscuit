#!/bin/sh
# Play this device's audio through a paired Bluetooth speaker.
#
# WHAT THIS IS, AND WHY IT IS A TAP RATHER THAN A ROUTE
# ----------------------------------------------------
# Everything that makes sound here targets the PipeWire sink `biscuit_speaker`
# by name: the assistant is launched with `--audio-output-device
# pulse/biscuit_speaker`, Music Assistant and the earcon player end up there
# too. There is no default-sink to switch, and repointing every one of those
# would mean restarting the assistant to change speakers.
#
# So the audio is taken from that sink's MONITOR, which carries exactly what
# applications wrote, and fed to bluealsa:
#
#   apps -> biscuit_speaker -+-> hw:Loopback,0,0 -> biscuit-dsp -> codec
#                            |                     (muted while casting)
#                            `-> .monitor -> pw-record | aplay -> bluealsa -> BT
#
# Nothing upstream has to know this is happening, and switching speakers costs
# nothing but starting and stopping this bridge.
#
# THE MONITOR TAP IS ALSO THE CORRECT SIGNAL. biscuit-dsp applies a 1024-tap FIR
# that inverts THIS DEVICE'S driver response - about +24 dB at 120-300 Hz - plus
# a multiband compressor tuned to the same driver. Sending that to somebody
# else's speaker would be badly wrong. The monitor is upstream of all of it, so
# the Bluetooth speaker gets flat audio and applies its own correction, which is
# what it expects to be given.
#
# SILENCING THE INTERNAL SPEAKER
# ------------------------------
# `cast on` down biscuit-dsp's control FIFO, which zeroes its INPUT. That is
# deliberate: the DSP's existing idle path then sees silence, counts up, and
# releases the codec, so the external amplifier powers down instead of sitting
# energised behind a muted stream. This device has no audio thermal protection,
# so an idle powered amp is not a neutral state.
#
# Volume stays with biscuit-audio, which remains the single owner - this only
# FOLLOWS the level it publishes and applies it to bluealsa's own mixer.
#
# CLOCK DRIFT IS REAL AND NOT SOLVED HERE. PipeWire's monitor runs off this
# device's clock; the Bluetooth speaker runs off its own. Over a long session
# the two diverge and bluealsa's back-pressure eventually shows up as a dropped
# period. It is inaudible in ordinary use and honest to write down.

set -u

PERSIST=/opt/persist/btcast
RUNDIR=/run/biscuit-btcast
STATE=$RUNDIR/active
DSP_FIFO=/run/biscuit-dsp/control
VOLUME_FILE=/run/biscuit-audio/volume
# The PipeWire NODE to tap, plus the property that makes the capture take its
# monitor rather than an input.
#
# `--target biscuit_speaker.monitor` looks right and is not: that is the
# PulseAudio-style source name, which pw-record does not match, so it silently
# fell back to a default source and captured perfect silence at exactly the
# right byte rate. Everything downstream looked healthy - transport Running,
# aplay writing 192 kB/s - while nothing at all was being sent. Only measuring
# the peak of the captured samples showed it.
SINK_NODE=biscuit_speaker
CAPTURE_PROP="stream.capture.sink=true"
POLL=3

export XDG_RUNTIME_DIR=/run/pipewire

bridge_pid=""
casting=""
last_volume=""

log() {
	echo "[btcast] $*" >&2
}

# One line to the DSP. The FIFO having no reader is a normal condition -
# biscuit-dsp may be restarting - not an error worth exiting over.
#
# `timeout` rather than a detached subshell: biscuit-dsp holds the FIFO O_RDWR,
# so a write normally returns at once, but if the service is genuinely down then
# opening a FIFO for writing BLOCKS until a reader appears. Backgrounding that
# only hides it - one stuck writer per poll would accumulate for as long as the
# DSP stayed down.
dsp() {
	[ -p "$DSP_FIFO" ] || return 0
	timeout 2 sh -c 'printf "%s\n" "$1" > "$2"' _ "$1" "$DSP_FIFO" 2>/dev/null ||
		log "DSP did not accept '$1'"
	return 0
}

target_device() {
	[ -r "$PERSIST" ] || return 1
	tr -d ' \t\n\r' < "$PERSIST"
}

is_connected() {
	bluetoothctl info "$1" 2>/dev/null | grep -q 'Connected: yes'
}

# bluealsa names its mixer control after the peer, so it cannot be hard-coded.
# Failing to find one is not fatal: the speaker still has its own volume.
apply_volume() {
	pct=$1
	ctl=$(amixer -D bluealsa scontrols 2>/dev/null |
	      sed -n "s/.*Simple mixer control '\(.*\)',0/\1/p" |
	      grep -i 'A2DP' | head -1)
	[ -n "$ctl" ] || return 0
	amixer -D bluealsa -q sset "$ctl" "${pct}%" 2>/dev/null || true
}

start_bridge() {
	mac=$1
	[ -z "$bridge_pid" ] || return 0

	# Raw rather than WAV: a WAV header carries a length that a stream does not
	# have, and aplay reading one from a pipe has to guess. Explicit format on
	# both sides removes the guess entirely.
	#
	# plug because the speaker chooses the A2DP sample rate - 44.1 kHz is as
	# common as 48 - and without it the open fails on any speaker that did not
	# pick ours.
	#
	# The {SLAVE=...} form is not decoration. `plug:bluealsa:DEV=...` makes ALSA
	# read everything after the second colon as arguments TO plug, and it dies
	# with "Unknown parameter bluealsa:DEV" - which is what stopped the first
	# working bridge from starting. The inner PCM has to be a quoted slave.
	#
	# BOTH halves at SCHED_FIFO 15. At SCHED_OTHER this bridge is starved: with
	# the capture chain holding FIFO 44-50 and the load at 4 on 4 cores,
	# pw-record measured a 7.03 wait:run ratio - seven seconds queued for every
	# second run - so it could not keep the pipe fed and aplay underran. The
	# symptom is audio that cuts out and reads exactly like a bad Bluetooth
	# link, which is what it was mistaken for.
	#
	# 15 sits above the assistant's audio thread (10) and below btcall (20) and
	# everything in the capture chain: a dropout here is worse than a late LED
	# and less bad than a broken call or a deaf microphone.
	chrt -f 15 pw-record -P "$CAPTURE_PROP" --target "$SINK_NODE" --rate 48000 --channels 2 --format s16 --raw - 2>/dev/null |
		chrt -f 15 aplay -D "plug:{SLAVE=\"bluealsa:DEV=$mac,PROFILE=a2dp\"}" \
		      -f S16_LE -r 48000 -c 2 -t raw -q - 2>/dev/null &
	bridge_pid=$!
	# Give the pipeline a moment to fail if it is going to - a bad MAC or a
	# transport that went away shows up immediately.
	sleep 1
	if ! kill -0 "$bridge_pid" 2>/dev/null; then
		wait "$bridge_pid" 2>/dev/null
		bridge_pid=""
		log "bridge to $mac did not start"
		return 1
	fi
	log "casting to $mac"
	return 0
}

stop_bridge() {
	[ -n "$bridge_pid" ] || return 0
	# The pipeline is two processes in a job; killing the job's process group
	# takes both. Killing only $! leaves aplay holding the Bluetooth transport,
	# which then refuses the next open.
	kill "$bridge_pid" 2>/dev/null
	pkill -f "aplay -D plug:{SLAVE" 2>/dev/null
	pkill -f "pw-record -P $CAPTURE_PROP" 2>/dev/null
	wait "$bridge_pid" 2>/dev/null
	bridge_pid=""
	log "bridge stopped"
}

begin_cast() {
	mac=$1
	start_bridge "$mac" || return 1
	dsp "cast on"
	casting=$mac
	mkdir -p "$RUNDIR"
	printf '%s\n' "$mac" > "$STATE"
	last_volume=""            # force the level to be re-applied
	return 0
}

end_cast() {
	[ -n "$casting" ] || return 0
	stop_bridge
	dsp "cast off"
	casting=""
	rm -f "$STATE"
	log "internal speaker restored"
}

cleanup() {
	end_cast
	rmdir "$RUNDIR" 2>/dev/null
	exit 0
}
trap cleanup INT TERM

mkdir -p "$RUNDIR"
rm -f "$STATE"
# A cast left running when the service died would otherwise leave the DSP muted
# with nothing feeding Bluetooth - silence with no way to explain it.
dsp "cast off"
log "watching for a cast target in $PERSIST"

while :; do
	want=$(target_device || true)

	if [ -z "$want" ]; then
		end_cast
	elif [ -n "$casting" ] && [ "$casting" != "$want" ]; then
		log "target changed to $want"
		end_cast
	fi

	if [ -n "$want" ]; then
		if is_connected "$want"; then
			if [ -z "$casting" ]; then
				begin_cast "$want" || sleep 5
			elif [ -n "$bridge_pid" ] && ! kill -0 "$bridge_pid" 2>/dev/null; then
				# The speaker went to sleep, or the transport dropped.
				# Tear the whole cast down and let the next pass rebuild
				# it, rather than leaving the DSP muted behind a dead pipe.
				log "bridge exited; restarting"
				bridge_pid=""
				end_cast
			fi
		elif [ -n "$casting" ]; then
			log "$want disconnected"
			end_cast
		fi
	fi

	# Follow the device volume onto the speaker. biscuit-audio stays the owner;
	# this only mirrors what it publishes.
	if [ -n "$casting" ] && [ -r "$VOLUME_FILE" ]; then
		vol=$(tr -d ' \t\n\r' < "$VOLUME_FILE" 2>/dev/null)
		case $vol in
			''|*[!0-9]*) : ;;
			*) if [ "$vol" != "$last_volume" ]; then
				apply_volume "$vol"
				last_volume=$vol
			   fi ;;
		esac
	fi

	sleep "$POLL"
done
