#!/bin/sh
# Beamformed mono 16 kHz microphone stream on stdout.
#
# Wraps the two prerequisites that are easy to forget and fail obscurely:
#
#   1. UCM must enable the Mic device. alsactl restores control *values* but
#      never re-fires DAPM, so the mixer can look completely correct while the
#      capture path is unpowered.
#   2. A playback stream must be running. The dough FPGA aggregates the TDM
#      mics onto SPI and gates capture on its own DAC reference. /dev/zero is
#      silent, so it costs nothing acoustically.
#
# Without either, arecord fails with a bare EIO that names neither cause.
#
# Usage:
#   biscuit-mic-stream.sh            stock-grid chain (DEFAULT): HPF,
#                                    filterbank, AEC, fixed + adaptive beam
#   biscuit-mic-stream.sh -beam      our own beam weights, auto selection
#   biscuit-mic-stream.sh -raw       raw 8-channel, for diagnostics
#   biscuit-mic-stream.sh -centre    centre mic only (MK7/ch6), the baseline
#   biscuit-mic-stream.sh -stock     stock-grid AEC/ABF/filterbank trial
#   biscuit-mic-stream.sh -stock-centre  stock-PGA single-mic control
#   biscuit-mic-stream.sh -compose   settings from biscuit-mic-pump/HA
#
# Feeds a Wyoming satellite directly:
#   biscuit-mic-stream.sh | wyoming-satellite --mic-command 'cat' ...
set -e

# ALSA's human-readable card name has a hyphen, but `amixer -c` and
# `alsaucm -c` require the card index (or the hyphen-less card ID).  Card 0 is
# the physical MT8163 codec; card 1 is the snd-aloop device.
CARD=0
DEV=hw:0,2
RATE=16000
CHANS=8
# /usr/bin, not /usr/local: abuild rejects /usr/local outright, so a package
# that installs there fails the build rather than shipping something odd.
BEAMFORM=/usr/bin/biscuit-beamform
WEIGHTS=/usr/share/biscuit/biscuit-beam-weights.bin
STOCK_WEIGHTS=/usr/share/biscuit/biscuit-beam-weights-stock.bin
GAIN=${BISCUIT_MIC_GAIN:-1.0}
# AFE.cfg's fixed ASR Output Gain/AFE Out Gain is +7.2 dB after selection.
# Keep it separate from BISCUIT_MIC_GAIN so the optional trim composes instead
# of replacing a stock stage.  10^(7.2/20), rounded to the replayed value.
STOCK_OUTPUT_GAIN=2.290868
# Stock's +7.2 dB alone leaves the S16 output peaking at tens of integer counts
# on a far/quiet capture, which throws away speech resolution before the wake
# model ever sees it.  The paired-capture representation correction is +18.867
# dB, i.e. 10^(18.867/20); 2.290868 * 8.777078852 = 20.107129075, the exact
# operating point
# every r108 acceptance test was run at.  Kept separate from STOCK_OUTPUT_GAIN
# so stock's own stage stays exact, and separate from MIC_GAIN so the user trim
# still composes.
STOCK_REPRESENTATION_GAIN=8.777078852
PGA_DB=${BISCUIT_MIC_PGA_DB:-20.0}
# Per-capsule factory calibration, produced by biscuit-miccal.py from IDME.
# Absent means uncalibrated, which is what shipped before this existed.
CAL=/run/biscuit-miccal/gains

# Realtime priority for the capture path, mirroring what biscuit-dsp.initd does
# for playback. Measured on a 4-core device under four SCHED_OTHER CPU hogs:
#
#                      SCHED_OTHER      SCHED_FIFO
#   beamformer wait      19.535%          0.012%      (361.8 us -> 0.4 us/slice)
#   arecord wait          1.232%          0.000%
#   arecord overruns        +3              +0        (reproduced twice)
#
# biscuit-dsp, already at FIFO 50, was unaffected in every run - so the same
# load either drops microphone audio or does not, purely on scheduling policy.
#
# Both stay BELOW the DSP's 50: a device short of CPU should lose microphone
# latency before it loses the codec, which is the failure the user can hear.
# arecord sits above the beamformer because it is the one with a hard period
# deadline and it costs 0.3% CPU, so it cannot starve anything; the beamformer
# runs 70-76% of a core and can catch up out of the FIFO.
RT_PRIO_CAPTURE=48
RT_PRIO_BEAMFORM=45

# Never fatal. Run by hand as a non-root user this fails, and a diagnostic
# capture at the wrong priority is still a useful diagnostic capture.
#
# The main thread is set FIRST and the task sweep second, because the ARA
# workers are created during startup and neither step covers them alone:
# a worker created after the main thread is FIFO inherits it, and one created
# before is caught by the sweep. Reversing the order leaves a real hole.
_apply_rt() {
	_prio=$1
	_pid=$2
	[ -n "${_pid}" ] || return 0
	chrt -f -p "${_prio}" "${_pid}" 2>/dev/null || return 0
	for _t in /proc/"${_pid}"/task/*; do
		[ -e "${_t}" ] || continue
		chrt -f -p "${_prio}" "${_t##*/}" 2>/dev/null || true
	done
	return 0
}

# There used to be a fallback here to /tmp/biscuit-beamform.arm64 and a pair of
# /tmp weight files, "so the script still works on a device where the package has
# not been reflashed yet". It is removed, for three reasons.
#
# It is dead: the package builds biscuit-beamform and installs both weight blobs,
# and all three are present on the device. The fallback can only fire when
# packaging is broken - which is exactly when this should fail loudly instead.
#
# It hid that breakage. A hand-pushed build in /tmp satisfying a service is how
# this tree came to have files the device ran and the package never installed.
#
# And /tmp is world-writable, so a root-run service resolving its executable and
# its DSP coefficients there is a privilege escalation waiting for the packaged
# copy to go missing.
#
# The missing-weights guards further down already print a real error, and they
# only work once nothing quietly substitutes a path under them.

# The UCM establishes stock's 20 dB PGA default.  The composed HA control may
# override it, in 0.5 dB codec steps, after UCM has powered the capture path.
set_pga() {
	# cset is required: these controls are not simple ALSA `sset` controls.
	for mic in A B C D; do
		amixer -c "$CARD" cset "name=Mic $mic PGA Capture Volume" "$1,$1" \
			>/dev/null
	done
}

set_pga_db() {
	# Accept only the range exposed in HA (10..30 dB, half-dB increments).
	# An invalid persisted value must fail safe to stock's 20 dB rather than
	# leaving a stale or unexpectedly hot analogue gain in the converters.
	raw=$(awk -v db="$PGA_DB" 'BEGIN {
		if (db !~ /^[0-9]+([.][0-9]+)?$/) exit 1
		n = int(db * 2 + 0.5)
		if (n < 20 || n > 60 || (db - n / 2) > 0.001 || (n / 2 - db) > 0.001)
			exit 1
		print n
	}') || raw=
	case "$raw" in
	''|*[!0-9]*)
		echo "biscuit-mic-stream: invalid BISCUIT_MIC_PGA_DB=$PGA_DB; using 20.0 dB" >&2
		raw=40
		;;
	esac
	set_pga "$raw"
}

if [ "${BISCUIT_MIC_INPUT:-hardware}" != stdin ]; then
# Refresh the per-capsule calibration for whichever mode is selected.
# Done here rather than in a service of its own: this is the one place
# that runs immediately before every capture, so a mode change takes
# effect on the next capture with nothing to keep in sync. It never
# fails the run - biscuit-miccal falls back to unity on its own.
[ -x /usr/bin/biscuit-miccal.py ] && /usr/bin/biscuit-miccal.py 2>/dev/null

alsaucm -c "$CARD" set _verb HiFi set _enadev Mic >/dev/null 2>&1 || {
	echo "biscuit-mic-stream: UCM enable failed; capture will not work" >&2
	exit 1
}
set_pga_db
fi

# The FPGA's DAC reference. Keep every child PID so an interrupted trial does
# not strand aplay, arecord, or the beamformer with the PGAs still at 40.
# BISCUIT_DAC_REF=0 skips this keepalive, and the mic pump MUST set it.
# biscuit-dsp owns hw:0,0, and whichever of the two opens the codec first keeps
# it. At boot the pump wins, biscuit-dsp then cannot write the codec, its
# loopback input goes to XRUN, and ALL playback is silent - while every service
# still reports "started" and PipeWire still shows a live sink-input.
# biscuit-dsp's own playback already satisfies the FPGA's capture gate.
REF=
if [ "${BISCUIT_MIC_INPUT:-hardware}" != stdin ] && [ "${BISCUIT_DAC_REF:-1}" != "0" ]; then
	aplay -D hw:0,0 -f S16_LE -c 2 -r 48000 /dev/zero >/dev/null 2>&1 &
	REF=$!
fi
CAP=
DSP=
FIFO=

cleanup() {
	[ -z "$CAP" ] || kill "$CAP" 2>/dev/null || true
	[ -z "$DSP" ] || kill "$DSP" 2>/dev/null || true
	kill "$REF" 2>/dev/null || true
	[ -z "$FIFO" ] || rm -f "$FIFO"
}

trap cleanup EXIT
trap 'exit 143' INT TERM
# Only hardware capture needs the settling delay. An independently restarted
# stdin processor must consume the live hub feed immediately; sleeping here
# fills its bounded input queue and prevents recovery.
if [ "${BISCUIT_MIC_INPUT:-hardware}" != stdin ]; then sleep 1; fi

# Every hardware capture opens a fresh kernel reader. Apply policy to that
# exact ALSA owner; stdin-only branch restarts must not touch shared hardware.
_apply_capture_policy() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "biscuit-mic-stream: non-root diagnostic; audio policy not applied" >&2
        return 0
    fi
    /usr/bin/biscuit-audio-policy.py --capture-pid "$CAP" >&2
}

run_raw() {
	# Feed four 8 ms DSP blocks per capture period instead of a 125 ms burst.
	# Retain roughly 500 ms of hardware buffering for scheduling headroom.
	arecord -D "$DEV" -f S24_3LE -c "$CHANS" -r "$RATE" --period-size=512 --buffer-size=8192 -t raw - &
	CAP=$!
	_apply_rt "$RT_PRIO_CAPTURE" "$CAP"
	_apply_capture_policy
	wait "$CAP"
	CAP=
}

run_processed() {
    if [ "${BISCUIT_MIC_INPUT:-hardware}" = stdin ]; then
        if [ -f "$CAL" ]; then exec "$BEAMFORM" -k "$CAL" "$@"; fi
        exec "$BEAMFORM" "$@"
    fi
	# This FIFO carries the live microphone, in the clear, from arecord to the
	# beamformer. It was /tmp/biscuit-mic-stream.$$.fifo: a world-readable
	# path whose name is just our pid, in a world-writable directory. Anything
	# local could open it and read the room, or pre-create the name to make
	# mkfifo fail.
	#
	# (An earlier version of this comment claimed the script "sets neither -e nor
	# -u". That is WRONG - line 27 is `set -e`. The claim mattered, because it is
	# the same errexit that later silently swallowed the fireos6 asset fallback
	# below until it was wrapped in an if-condition.)
	#
	# /run/biscuit-mic is root-owned 0750 and is where the rest of the mic
	# runtime already lives; the hub creates it before spawning us, and
	# mkdir -p covers a standalone run.
	mkdir -p /run/biscuit-mic
	chmod 750 /run/biscuit-mic
	FIFO="/run/biscuit-mic/stream.$$.fifo"
	rm -f "$FIFO"
	mkfifo -m 600 "$FIFO"
	arecord -D "$DEV" -f S24_3LE -c "$CHANS" -r "$RATE" --period-size=512 --buffer-size=8192 -t raw - >"$FIFO" &
	CAP=$!
	# Calibration is injected here, not at the six call sites, so every mode
	# - including the centre-mic baseline - is calibrated identically. A
	# baseline calibrated differently from the array would make any
	# comparison between them meaningless.
	if [ -f "$CAL" ]; then
		"$BEAMFORM" -k "$CAL" "$@" <"$FIFO" &
	else
		"$BEAMFORM" "$@" <"$FIFO" &
	fi
	DSP=$!
	_apply_rt "$RT_PRIO_CAPTURE" "$CAP"
	_apply_capture_policy
	_apply_rt "$RT_PRIO_BEAMFORM" "$DSP"
	wait "$CAP"
	CAP=
	wait "$DSP"
	DSP=
	rm -f "$FIFO"
	FIFO=
}

# BISCUIT_MIC_ prefix on purpose: biscuit-mic-hub.py:267 passes exactly that
# subset of mic.env through to the branch, so an override named anything else is
# silently ignored. A hash test that set BISCUIT_ASSET_MANIFEST therefore proved
# nothing and reported a pass.
ASSET_MANIFEST=${BISCUIT_MIC_ASSET_MANIFEST:-/usr/share/biscuit/biscuit-profile-assets.json}

assets_verify() {
	# Verify one profile's owner-imported vendor files by CONTENT.
	#
	# This used to compare sizes, on the reasoning that hashing 646 KB on every
	# branch restart would be wasted work. That was wrong by three orders of
	# magnitude: hashing all four Fire OS 6 files costs 5 ms in process and 31 ms
	# through one busybox fork, against the ten seconds a branch restart already
	# costs. And a size check passes the two things worth catching - a file
	# truncated to exactly the right length, and a file from the WRONG GENERATION.
	# The second is not bookkeeping: the two chains are disjoint, so a mismatched
	# coefficient set is loaded as coefficients and yields silence or noise rather
	# than an error anyone can read.
	#
	# Silent on success. On failure it says which file and why, because that is the
	# message explaining a fallback to pmos in profile.json.
	_ap=$1
	[ -r "$ASSET_MANIFEST" ] || {
		echo "biscuit-mic-stream: no asset manifest at $ASSET_MANIFEST" >&2
		return 1
	}
	python3 - "$ASSET_MANIFEST" "$_ap" <<-'PYEOF'
		import hashlib, json, os, sys
		manifest, profile = sys.argv[1], sys.argv[2]
		try:
		    spec = json.load(open(manifest))["profiles"][profile]
		except (OSError, ValueError, KeyError) as err:
		    print("biscuit-mic-stream: unusable manifest (%s)" % err, file=sys.stderr)
		    sys.exit(1)
		bad = 0
		for item in spec["files"]:
		    path = os.path.join(spec["directory"], item["name"])
		    try:
		        raw = open(path, "rb").read()
		    except OSError:
		        if item.get("required", True):
		            print("biscuit-mic-stream: %s is not imported" % item["name"],
		                  file=sys.stderr)
		            bad += 1
		        continue
		    # An entry may accept several contents: the Bluetooth ROM patches
		    # differ between Fire OS 5 and 6. Reading bytes/sha256 as scalars
		    # raises KeyError on those, so accept either shape.
		    variants = item.get("variants") or [
		        {"bytes": item["bytes"], "sha256": item["sha256"]}]
		    got = hashlib.sha256(raw).hexdigest()
		    if any(len(raw) == v["bytes"] and got == v["sha256"] for v in variants):
		        continue
		    if not any(len(raw) == v["bytes"] for v in variants):
		        print("biscuit-mic-stream: %s is %d bytes, expected %s"
		              % (item["name"], len(raw),
		                 " or ".join(str(v["bytes"]) for v in variants)),
		              file=sys.stderr)
		    else:
		        print("biscuit-mic-stream: %s is the right size but the wrong file "
		              "(%s..., expected %s...)"
		              % (item["name"], got[:16],
		                 " or ".join(v["sha256"][:16] for v in variants)),
		              file=sys.stderr)
		    bad += 1
		sys.exit(1 if bad else 0)
	PYEOF
}

fireos5_assets_present() {
	assets_verify fireos5
}

run_fireos6() {
	# The Fire OS 6 derived assistant DSP, as a generation of the composed chain.
	#
	# Same contract as run_processed's stdin path: raw 8-channel S24_3LE on stdin,
	# mono S16_LE on stdout, one 128-sample hop at a time. The hub has already
	# applied taskset -c 1-3 and chrt -f 45 before exec, so this inherits
	# production placement and priority; the frontend's own --rt-priority is for
	# standalone use and is not needed here.
	_src=$1
	_aec=$2
	_beam=$3

	# Refuses the hardware path deliberately: this generation exists to replace the
	# hub's assistant branch, not to open the capture device itself.
	if [ "${BISCUIT_MIC_INPUT:-hardware}" != stdin ]; then
		echo "biscuit-mic-stream: the fireos6 profile is a hub branch path only" >&2
		return 1
	fi
	# The frontend is an eight-channel array processor with no single-capsule mode,
	# so this combination is not merely untuned, it is inexpressible. Refusing here
	# keeps the settings layer honest rather than silently ignoring the choice.
	if [ "$_src" = single ]; then
		echo "biscuit-mic-stream: the fireos6 profile has no single-microphone path" >&2
		return 1
	fi
	if [ ! -x "${BISCUIT_MIC_FIREOS6_FRONTEND:-}" ]; then
		echo "biscuit-mic-stream: BISCUIT_MIC_FIREOS6_FRONTEND not executable" >&2
		return 1
	fi
	# Owner-imported Fire OS 6 vendor configuration. These are not shipped:
	# profiles/import_profile_assets.py verifies them by hash and installs them
	# here, and assets_verify re-checks the same hashes on every branch restart -
	# an import that was correct once can still be replaced, truncated or lost to a
	# flash afterwards, and 31 ms is not a reason to find that out later.
	_assets="${BISCUIT_MIC_FIREOS6_ASSETS:-/usr/share/biscuit/fireos6}"
	if [ ! -d "$_assets" ]; then
		echo "biscuit-mic-stream: Fire OS 6 assets not imported: $_assets" >&2
		return 1
	fi
	assets_verify fireos6 || return 1

	# mic_beam is the adaptive cleanup after the fixed beamformer, as on the
	# other generations. The per-microphone echo canceller is NOT a separate
	# choice any more: it runs exactly when the adaptive stage does not.
	#
	# With the adaptive stage on, the canceller is redundant for echo and
	# harmful to speech. Measured on 2026-09-25 (162 prompts, held out,
	# through the wake-input AGC): 159/162 without it against 140/162 with
	# it - 19 gained, none lost, p < 0.0001 - and 35/36 against 31/36 live.
	# The adaptive stage alone leaves the same music behind (-46.8 against
	# -46.7 dBFS) because its references are the microphones, which carry
	# what the speaker really emits, distortion included. With the canceller
	# in front, the talker comes out 1-3 dB weaker against that residual.
	# None of the ways ours differs from stock recovers the loss: Amazon's
	# own step table, the 400-2500 Hz band, a reference delay, or a second
	# reference (qual-captures/aec-probe/).
	#
	# Without the adaptive stage the canceller is the only thing removing
	# echo - 16 dB of it on a fixed beam - so there it stays. mic_aec is
	# therefore not applicable to this profile, and the settings grey it out.
	set -- --allow-unqualified --assets "$_assets"
	[ "$_beam" = on ] && set -- "$@" --no-aec

	# Which detector gates adaptation.
	#
	# Stock's DNN (vad_lite.tflite) is a proprietary Amazon file from system_a,
	# which the merged v2 conversion destroys, so it is OWNER-IMPORTED and never
	# shipped. When it is absent the energy prototype runs instead: that is a
	# working chain, not a failure, and the two are qualified separately.
	#
	# Measured closed-loop on the three captures at gain 13:
	#   energy prototype  19 / 19 / 15 = 53   (level-matched 53)
	#   stock DNN         17 / 20 / 17 = 54   (level-matched 55)
	# The DNN wins barge-in outright and the interferer by two, and loses the
	# quiet room by two - which is why mic_vad is a user choice, not automatic.
	_vad=${BISCUIT_MIC_FIREOS6_VAD:-auto}
	_vad_model="$_assets/vad_lite.tflite"
	_vad_flags="--vad-energy"
	if [ "$_vad" != energy ] && [ -r "$_vad_model" ]; then
		# libtensorflowlite_c.so needs libtensorflow-lite.so, which needs
		# libfarmhash, libcpuinfo, libpthreadpool, libfft2d_* and several
		# libabsl_* from the same directory. None are on the default search
		# path, and musl fixes that path at process start - so it must be set
		# HERE, before exec. Preloading them from inside the frontend was tried
		# and cannot work: musl resolves DT_NEEDED by name and these libraries
		# carry no SONAME.
		_tfl=${BISCUIT_MIC_FIREOS6_TFLITE_DIR:-/usr/lib/biscuit-tflite}
		if [ -r "$_tfl/libtensorflowlite_c.so" ]; then
			LD_LIBRARY_PATH="$_tfl${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
			export LD_LIBRARY_PATH
			_vad_flags="--dnn-vad $_vad_model --dnn-lib $_tfl --dnn-threshold ${BISCUIT_MIC_FIREOS6_VAD_THRESHOLD:-0.50}"
		else
			echo "biscuit-mic-stream: $_tfl/libtensorflowlite_c.so missing; using the energy VAD" >&2
		fi
	fi
	# The adaptive stage, and the two things it needs to be useful.
	#
	# --adapt-mask defaults to 0 in the frontend, so the echo canceller has never
	# adapted a single kernel in production. Enabling it alone takes barge-in from
	# 1/20 to 18/20 and costs the quiet room 19/20 -> 15/20, because the canceller
	# then learns the near-end speech and subtracts it.
	#
	# --freeze-on-speech is the double-talk answer: stop adapting while the user is
	# talking. It is NOT optional - without it the chain scores 38/60 against
	# 54/60, losing 11 detections under the interferer alone.
	#
	# The speech confidence comes from whichever detector the block above chose.
	# The energy prototype is unqualified and catches roughly a quarter of the
	# speech; stock's DNN is the qualified answer and is used when imported.
	# $_vad_flags is deliberately unquoted: it is a flag LIST, and word
	# splitting is how sh passes one. Its contents are built above from fixed
	# strings and validated paths, never from a bare setting value.
	if [ "$_beam" = on ]; then
		set -- "$@" --adaptive --adapt-mask 255 $_vad_flags \
			--freeze-on-speech
	fi
	# Stock's configured output gain for this generation: AFE.cfg gives
	# OutputGain BoostdB 9.5586 and MicsPostAECOutputGain BoostdB 4, together
	# 13.56 dB = 4.768 linear. Measured as exactly the point where as-shipped
	# detection reaches its level-matched ceiling (47/60 -> 54/60 across the
	# three captures); nothing above it helps and nothing clips, with about
	# 16 dB of headroom left. Unity - the previous default - cost 7 detections.
	# Re-derived for the DNN-gated chain. Gap 3's 4.768 was fitted to the
	# ENERGY-gated one; a gate that freezes ~25% of hops rather than ~5% leaves
	# less adaptive gain and a quieter output. 13 is the lowest value that
	# reaches the ceiling (53 -> 54 as shipped), peaks at -7.20 dBFS and clips
	# nothing. The open-loop study said 6.5 - it was measuring a different
	# system. Anything up to 26 was also clip-free, so the headroom is real.
	set -- "$@" --gain "${BISCUIT_MIC_FIREOS6_GAIN:-13}"
	# The hub passes only settings whose key starts with BISCUIT_MIC_ into the
	# branch environment, so these must carry that prefix. Naming them
	# BISCUIT_FIREOS6_* silently dropped them and the guard above fired 647 times.
	# Calibration is the same per-unit Q14 file every other profile uses, so a
	# comparison against the other generations is not confounded by calibration.
	[ -f "$CAL" ] && set -- "$@" --cal "$CAL"
	exec "$BISCUIT_MIC_FIREOS6_FRONTEND" "$@"
}

run_pmos8() {
	# pmOS's 8-beam chain. It is the Fire OS 6 frontend's architecture - a
	# 256-point filterbank stepping every 8 ms, eight fixed beams, and an
	# adaptive stage that cleans every beam from the two beams behind it -
	# running on coefficients the frontend designs itself at startup
	# (--open-coefficients: a Kaiser-windowed-sinc prototype and diffuse-field
	# superdirective beams from the measured capsule positions). No vendor file
	# is read, so, like pmos, it is always available.
	#
	# Its output is the plain mean of the eight cleaned beams (--merge-all).
	# Nothing steers, so it cannot sit on a beam that faces away from the
	# talker or steer at an interferer. A fixed beam lost three prompts in a
	# quiet room where it faced away from the talker, and selection driven
	# by the energy VAD steered towards an off-axis talker.
	#
	# Measured through the assistant's wake-input AGC on three sessions -
	# 09-16 (20 utterances per condition), 09-24 (50), and 09-25 (162, at
	# another location), in qual-captures/sit-0925/ and fos6-host/:
	#
	#                  barge-in 09-16/24/25        quiet 09-16, 09-24 1/4/8 m   interferer
	#   pmos       15/20   31/50   134/162        19/20   50/50/49             10/20
	#   pmos8      19/20   50/50   160/162        19/20   50/50/49             12/20
	#
	# False accepts: 1 (09-16 barge-in) against 0. The 09-25 session was
	# recorded as the held-out test of the fixed-beam version, which scored
	# 143/162 there (p = 0.18 against pmos). The mean output was chosen after
	# seeing it, so its 160/162 (p = 3e-8) is strong but not held-out
	# evidence.
	#
	# --clean-on-echo: the adaptive stage adapts, and subtracts, only while
	# playback is active. In a quiet room there is no echo to remove, and
	# adapting there erodes the talker (39/45/43 of 50 on a fixed beam without
	# this). Filters learned on the last song are kept for the next one.
	#
	# The per-microphone echo canceller is deliberately OFF. On the same audio
	# Fire OS 6 scored 41/50 with it and 49/50 without it (p = 0.008), so
	# mic_aec does not apply here and the settings grey it out. Nor is there a
	# VAD: the open chain lost nothing without one.
	_src=$1
	_beam=$3
	if [ "${BISCUIT_MIC_INPUT:-hardware}" != stdin ]; then
		echo "biscuit-mic-stream: the pmos8 profile is a hub branch path only" >&2
		return 1
	fi
	if [ "$_src" = single ]; then
		echo "biscuit-mic-stream: the pmos8 profile has no single-microphone path" >&2
		return 1
	fi
	_frontend=${BISCUIT_MIC_FIREOS6_FRONTEND:-/usr/bin/biscuit-mic-fireos6}
	if [ ! -x "$_frontend" ]; then
		echo "biscuit-mic-stream: $_frontend is not executable" >&2
		return 1
	fi
	set -- --allow-unqualified --open-coefficients
	# mic_beam is the adaptive stage, as on every other generation. Off leaves
	# the fixed beam, and then - as in run_fireos6 - the per-microphone
	# canceller runs instead, because it is the only thing left removing echo.
	if [ "$_beam" = on ]; then
		set -- "$@" --no-aec --adaptive --adapt-mask 255 --clean-on-echo --merge-all
	fi
	# The open coefficients are scaled to the level Fire OS 6's gain was tuned
	# at, so the same 13 applies.
	set -- "$@" --gain "${BISCUIT_MIC_FIREOS6_GAIN:-13}"
	[ -f "$CAL" ] && set -- "$@" --cal "$CAL"
	exec "$_frontend" "$@"
}

run_composed() {
    source=${BISCUIT_MIC_SOURCE:-array}
    [ "$source" != centre ] || source=single
    mic_channel=${BISCUIT_MIC_CHANNEL:-6}
    case "$mic_channel" in [0-6]) ;; *) echo "invalid microphone channel" >&2; return 1;; esac
	tuning=${BISCUIT_MIC_TUNING:-stock}
	aec=${BISCUIT_MIC_AEC:-on}
	beam=${BISCUIT_MIC_BEAM:-on}

	case "$source" in
	array|centre|single) ;;
	*)
		echo "biscuit-mic-stream: invalid BISCUIT_MIC_SOURCE=$source" >&2
		return 1
		;;
	esac
	case "$tuning" in
	stock|pmos) ;;
	*)
		echo "biscuit-mic-stream: invalid BISCUIT_MIC_TUNING=$tuning" >&2
		return 1
		;;
	esac
	case "$aec" in on|off) ;; *) echo "biscuit-mic-stream: invalid BISCUIT_MIC_AEC=$aec" >&2; return 1;; esac
	case "$beam" in on|off) ;; *) echo "biscuit-mic-stream: invalid BISCUIT_MIC_BEAM=$beam" >&2; return 1;; esac

	# Which generation's processing chain to run. BISCUIT_MIC_PROFILE is the
	# canonical field. BISCUIT_MIC_TUNING is still honoured when it is absent so
	# an existing mic.env keeps its exact behaviour on the first boot after an
	# upgrade; the new field wins when both are present.
	case "${BISCUIT_MIC_PROFILE:-}" in
	pmos|pmos8|fireos5|fireos6)
		profile=$BISCUIT_MIC_PROFILE
		;;
	"")
		# An mic.env written before BISCUIT_MIC_PROFILE existed carries
		# BISCUIT_MIC_TUNING=stock, and must keep running the Fire OS 5 chain
		# across the upgrade - that is what this branch is for.
		#
		# But $tuning DEFAULTS to stock, so a device with no mic.env at all -
		# a fresh install, before any setting has ever been saved - used to
		# land here and silently select fireos5 as well. That chain needs the
		# owner-imported Fire OS 5 vendor files, which this package does not
		# ship and most owners cannot obtain, so the one profile a brand new
		# device could not run was the one it chose by default.
		#
		# Explicitly set means an upgrade; absent means a fresh device.
		#
		# A fresh device runs pmOS 8-beam, the owner's default since the
		# 2026-09-27 comparison sitting (biscuit-va-leds.py default_mic_profile
		# has the numbers). An upgraded mic.env naming a tuning keeps exactly
		# what it ran before. If 8-beam cannot run - the single-microphone
		# source, or no hub branch - the pmos8 block below falls back to pmos.
		if [ -n "${BISCUIT_MIC_TUNING:-}" ]; then
			if [ "$tuning" = stock ]; then
				profile=fireos5
			else
				profile=pmos
			fi
		else
			profile=pmos8
		fi
		;;
	*)
		echo "biscuit-mic-stream: invalid BISCUIT_MIC_PROFILE=$BISCUIT_MIC_PROFILE" >&2
		return 1
		;;
	esac
	# fireos5 and pmos are the two chains biscuit-beamform already implements, and
	# they are selected below by $tuning. Keeping $tuning consistent with $profile
	# means every existing branch runs unchanged: this field adds a third chain, it
	# does not re-express the two that work.
	# A profile whose OWNER-IMPORTED assets are absent must degrade, not die.
	#
	# Found on the first clean flash. /opt/persist preserved
	# BISCUIT_MIC_PROFILE=fireos6 exactly as designed - but the Fire OS 6 assets
	# live in /usr/share/biscuit/fireos6, on the ROOTFS, which a flash wipes. The
	# device came back holding a setting it could not honour: run_fireos6 refused
	# (correctly, and said why on stderr), the caller propagated the failure, and
	# the hub sat in a restart-backoff loop reporting frames=0 for the assistant
	# branch. That is not a degraded microphone, it is NO microphone, and nothing
	# visible said why.
	#
	# The asset checks were never the problem. What was missing was anywhere to
	# fall back TO. pmos needs no imported assets, so it is always reachable.
	# PUBLISH WHAT IS ACTUALLY RUNNING, not what was asked for.
	# Every fallback below already explains itself on stderr, which nothing
	# reads afterwards. A qualification run that silently fell back to pmos
	# while mic.env still said fireos6 would be worthless and would look fine.
	# That is not hypothetical: this device sat in exactly that state after a
	# flash took the Fire OS 6 assets with it.
	#
	# Never fails the caller. This runs under `set -e`, and a microphone must
	# not refuse to start because a status file could not be written.
	# The reason is carried, not passed at the end: the final publish below
	# runs on every path including the fallbacks, so hard-coding "ok" there
	# overwrote the very explanation this exists to record. Caught by testing
	# the degraded path, which reported effective=pmos reason=ok.
	_preason=ok
	# What was asked for, fixed before any fallback rewrites $profile: with no
	# BISCUIT_MIC_PROFILE, the default chosen above.
	_prequested=${BISCUIT_MIC_PROFILE:-$profile}
	publish_profile() {
		_pdir=/run/biscuit-mic
		[ -d "$_pdir" ] || return 0
		printf '{"requested":"%s","effective":"%s","reason":"%s","uptime":%s}\n' \
			"$_prequested" "$1" "$2" \
			"$(cut -d' ' -f1 /proc/uptime 2>/dev/null || echo 0)" \
			>"$_pdir/profile.json.tmp" 2>/dev/null &&
			mv "$_pdir/profile.json.tmp" "$_pdir/profile.json" 2>/dev/null || :
	}

	if [ "$profile" = fireos5 ] && ! fireos5_assets_present; then
		echo "biscuit-mic-stream: Fire OS 5 assets absent; using the pmos chain" >&2
		profile=pmos
		_preason="fireos5 assets absent"
		publish_profile pmos "$_preason"
	fi
	if [ "$profile" = pmos ]; then tuning=pmos; else tuning=stock; fi
	if [ "$profile" = fireos6 ]; then
		# run_fireos6 execs on success, so RETURNING means it refused, and it has
		# already printed why.
		#
		# The `if !` is load-bearing, not style. This script runs under `set -e`
		# (line 27), so a bare `run_fireos6 ...` followed by the fallback aborts
		# the whole script the instant the function returns non-zero - which is
		# exactly what happened on the first attempt at this fix: the asset check
		# printed "Fire OS 6 assets not imported" fourteen times and the fallback
		# line never appeared once. A command inside an if-condition is exempt
		# from errexit; a bare one is not.
		# Recorded BEFORE the attempt, because run_fireos6 execs on success
		# and never returns; the fallback below corrects it if it refuses.
		publish_profile fireos6 ok
		if ! run_fireos6 "$source" "$aec" "$beam"; then
			echo "biscuit-mic-stream: fireos6 unavailable; using the pmos chain" >&2
			profile=pmos
			tuning=pmos
			_preason="fireos6 unavailable"
			publish_profile pmos "$_preason"
		fi
	fi
	if [ "$profile" = pmos8 ]; then
		# Same contract as the fireos6 block above: run_pmos8 execs on
		# success, so returning means it refused and said why.
		publish_profile pmos8 ok
		if ! run_pmos8 "$source" "$aec" "$beam"; then
			echo "biscuit-mic-stream: pmos8 unavailable; using the pmos chain" >&2
			profile=pmos
			tuning=pmos
			_preason="pmos8 unavailable"
			publish_profile pmos "$_preason"
		fi
	fi

	publish_profile "$profile" "$_preason"

	# Build argv without eval so persisted settings remain data, never shell.
	if [ "$tuning" = stock ]; then
		[ -f "$STOCK_WEIGHTS" ] || {
			echo "biscuit-mic-stream: missing stock-grid weights: $STOCK_WEIGHTS" >&2
			return 1
		}
		set -- -w "$STOCK_WEIGHTS" -S -G "$STOCK_OUTPUT_GAIN"
		# -Z is -O -T plus always-on ARA, ABF VSS/round robin, the
		# double-talk freeze and an ARA reset on both playback-reference
		# edges.  ARA is what recovers the phrases -O -T loses outright;
		# -Q's reference gate made it byte-identical to -O -T whenever
		# playback was silent, which is exactly where ARA wins.
		# BISCUIT_MIC_ADAPTIVE=off falls back to the r98 -O -T baseline.
		# Centre-only mode has no beam to select.
		if [ "$source" != single ]; then
			if [ "${BISCUIT_MIC_ADAPTIVE:-on}" = off ] || [ "$aec" = off ] || [ "$beam" = off ]; then
				set -- "$@" -O -T
			else
				set -- "$@" -Z
				# -Z scores beams with the recovered stock
				# signal/noise metric by default.  -N switches to
				# the contrast score (rise above the beam's own
				# long-term level), which measured one detection
				# better under self-playback but has a startup
				# transient and is weaker against an intermittent
				# directional interferer.
				[ "${BISCUIT_MIC_SELECTOR:-stock}" = contrast ] &&
					set -- "$@" -N
			fi
		fi
		[ "$aec" = off ] && set -- "$@" -X
		# -Y bypasses the adaptive cleanup while retaining the fixed stock
		# beam; that is the precise meaning of the exposed control.
		[ "$beam" = off ] && set -- "$@" -Y
	else
		set -- -w "$WEIGHTS"
		[ "$aec" = on ] && set -- "$@" -a
		[ "$beam" = on ] && set -- "$@" -A
	fi

	# Centre-only is a real single-capsule path.  Adaptive beam cleanup has no
	# meaningful input there, so callers save it as Off; -C makes the runtime
	# behaviour correct even for a manually edited mic.env.
	[ "$source" = single ] && set -- "$@" -m "$mic_channel"
	# The representation correction compensates the synthesis-safe FBF
	# loader dividing every coefficient by 128.  The centre-only path
	# never traverses the filterbank and already receives stock's +20 dB
	# make-up from -S, so applying it there would only clip.
	if [ "$tuning" = stock ] && [ "$source" != single ]; then
		composed_gain=$(awk -v a="$GAIN" -v b="$STOCK_REPRESENTATION_GAIN" 'BEGIN { printf "%.9f", a * b }')
	else
		composed_gain=$GAIN
	fi
	set -- "$@" -g "$composed_gain" -q -c "$CHANS"
	run_processed "$@"
}

case "${1:--compose}" in
-compose)
	run_composed
	;;
-raw)
	run_raw
	;;
-centre)
	# MK7 is ch6 - one mic, no spatial processing. The honest baseline: same
	# capture path, same calibration, same output scaling, run through the
	# same binary, so the only difference is the array processing.
	#
	# NOTE the levels are not equal. Beamformed output peaks ~8 dB higher
	# than the centre mic at the same -g, because the array sums coherently.
	# A wake-word A/B must match levels first or it measures loudness rather
	# than directivity - see BISCUIT_MIC_GAIN.
	GAIN=${BISCUIT_MIC_GAIN:-0.64}
	run_processed -w "$WEIGHTS" -C -g "$GAIN" -q -c "$CHANS"
	;;
-stock|-stock-centre)
	# Stock sets all four analogue PGAs to 40 (=20 dB); preserve that point
	# during this trial and return to the validated 80 setting when it ends.
	# -S replaces the lost analogue gain in floating point before the stock-grid
	# 80 Hz HPF/AEC/ABF chain, so the downstream signal level is comparable.
	[ -f "$STOCK_WEIGHTS" ] || {
		echo "biscuit-mic-stream: missing stock-grid weights: $STOCK_WEIGHTS" >&2
		exit 1
	}
	# gain comes from the UCM; see set_pga above
	if [ "$1" = "-stock-centre" ]; then
		GAIN=${BISCUIT_MIC_GAIN:-0.64}
		run_processed -w "$STOCK_WEIGHTS" -S -C -g "$GAIN" \
			-G "$STOCK_OUTPUT_GAIN" -q -c "$CHANS"
	else
		run_processed -w "$STOCK_WEIGHTS" -S -O -T -g "$GAIN" \
			-G "$STOCK_OUTPUT_GAIN" -q -c "$CHANS"
	fi
	;;
-stock-noaec)
	# Stock grid with the AEC bypassed (-X). The A/B control for "is the
	# echo canceller earning its place", which matters most when the
	# speaker is playing: the case the AEC exists for and the one the
	# original trials never covered.
	[ -f "$STOCK_WEIGHTS" ] || {
		echo "biscuit-mic-stream: missing stock-grid weights: $STOCK_WEIGHTS" >&2
		exit 1
	}
	run_processed -w "$STOCK_WEIGHTS" -S -O -T -X -g "$GAIN" \
		-G "$STOCK_OUTPUT_GAIN" -q -c "$CHANS"
	;;
-stock-noadaptive)
	# Stock grid with the adaptive beam cleanup bypassed (-Y), leaving the
	# filterbank, HPF, AEC and fixed beam. The other half of the same A/B.
	[ -f "$STOCK_WEIGHTS" ] || {
		echo "biscuit-mic-stream: missing stock-grid weights: $STOCK_WEIGHTS" >&2
		exit 1
	}
	run_processed -w "$STOCK_WEIGHTS" -S -O -T -Y -g "$GAIN" \
		-G "$STOCK_OUTPUT_GAIN" -q -c "$CHANS"
	;;
-beam)
	# Our own beam weights with per-beam SNR selection. This WAS the
	# default. Measured 96% recall against the centre mic's 100% in a
	# 50-utterance paired trial at 1 m on-axis, so it is not the default
	# any more.
	GAIN=${BISCUIT_MIC_GAIN:-0.25}
	run_processed -w "$WEIGHTS" -g "$GAIN" -q -c "$CHANS"
	;;
*)
	# DEFAULT: the stock-grid chain - 80 Hz HPF, 128/64 filterbank, AEC on
	# the DAC-loopback reference, fixed beam and adaptive cleanup. This is
	# what stock runs, and it is chosen for PARITY rather than on a measured
	# win.
	#
	# Be clear about the evidence. In a 50-utterance paired trial at 1 m,
	# on axis, one talker, clear speech, this chain scored 96-100% against
	# the single centre mic's 100%: no better, sometimes marginally worse.
	# But that envelope is exactly where one microphone should win - there
	# is almost no off-axis interference and little reverberation for a
	# beamformer to reject. The case the array exists for - far field, off
	# axis, competing talkers, a room with reflections - is untested here.
	#
	# So: no claim that this is better today. If a far-field or off-axis
	# trial ever shows the centre mic winning there too, this should go
	# back to -centre and the chain should stay opt-in.
	#
	# -S supplies the +20 dB the analogue stage no longer applies, which is
	# why this only became coherent once the UCM moved to stock's 20 dB.
	[ -f "$STOCK_WEIGHTS" ] || {
		echo "biscuit-mic-stream: missing stock-grid weights: $STOCK_WEIGHTS" >&2
		exit 1
	}
	run_processed -w "$STOCK_WEIGHTS" -S -O -T -g "$GAIN" \
		-G "$STOCK_OUTPUT_GAIN" -q -c "$CHANS"
	;;
esac
