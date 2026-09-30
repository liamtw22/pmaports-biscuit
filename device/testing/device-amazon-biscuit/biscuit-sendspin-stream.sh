#!/bin/sh
# sendspin stream start/stop -> the rest of the device.
#
# Invoked as: biscuit-sendspin-stream start|stop
# sendspin passes SENDSPIN_* env vars; none are needed here.
#
# TWO jobs, both of which sendspin cannot do itself:
#
# 1. WAKE THE CODEC. biscuit-dsp releases the codec when nothing is playing -
#    36 release events against 37 opens in one sample - and a released codec
#    swallows the beginning of the next sound. That is a known problem here,
#    already solved for earcons with the DSP's `wake` command. A track starting
#    after a quiet period has exactly the same exposure, and --hook-start fires
#    at the right moment to cover it.
#
# 2. SHOW IT ON THE RING. Everything else the device does lights the ring;
#    music was the one thing that did not.
#
# Deliberately silent and always successful: this runs on the player's own
# start path and must never be the reason a track fails to play.

ACTION="${1:-start}"
RING=/run/biscuit-ring/control
DSP=/run/biscuit-dsp/control

# The animation is user-selectable like every other activity.
if [ -r /opt/persist/led-anim.env ]; then
	. /opt/persist/led-anim.env
fi
ANIM="${LED_music:-act_music}"
case "$ANIM" in
	*[!A-Za-z0-9_-]*|"") ANIM=act_music ;;
esac

case "$ACTION" in
start)
	# Wake first, so the codec is up before the first samples arrive rather
	# than racing them.
	[ -p "$DSP" ] && ( timeout 2 sh -c "printf 'wake\n' > '$DSP'" ) >/dev/null 2>&1 &
	[ -p "$RING" ] && ( timeout 2 sh -c "printf 'play %s\n' '$ANIM' > '$RING'" ) >/dev/null 2>&1 &
	;;
stop)
	[ -p "$RING" ] && ( timeout 2 sh -c "printf 'stop %s\n' '$ANIM' > '$RING'" ) >/dev/null 2>&1 &
	;;
esac
exit 0
