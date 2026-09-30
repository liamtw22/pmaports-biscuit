#!/bin/sh
# Route sendspin's volume to THE device volume.
#
# Without this sendspin falls back to its own software volume control - its
# initd already documents the "No volume control available" warning - which
# attenuates inside its own PipeWire stream and never tells biscuit-audio.
# Two visible consequences:
#
#   * the volume ring does not light, because biscuit-audio plays that and
#     never hears about the change;
#   * there are two independent attenuations in series, sendspin's and the
#     device's, so the Music Assistant slider and the device volume disagree
#     and the DSP's per-volume loudness compensation is applied for the wrong
#     level. biscuit-audio is meant to be the ONLY thing that attenuates.
#
# sendspin passes the effective volume 0-100 as the first argument; some
# builds also export it, so both are accepted.
#
# Deliberately silent and always successful: this runs on a media path where
# a failure must never propagate back into the player.

VOL="${1:-$SENDSPIN_VOLUME}"

# Digits only, 0-100. The value comes from a network peer by way of sendspin,
# so it is not trusted into a printf format or a shell word.
case "$VOL" in
	""|*[!0-9]*) exit 0 ;;
esac
[ "$VOL" -le 100 ] 2>/dev/null || exit 0

CTL=/run/biscuit-audio/control
[ -p "$CTL" ] || exit 0

# Non-blocking: opening a FIFO for writing blocks until a reader exists, and
# biscuit-audio being down is a normal condition for a cosmetic path, not a
# reason to wedge the player's volume handler.
( timeout 2 sh -c "printf 'volume %s\n' '$VOL' > '$CTL'" ) >/dev/null 2>&1 &
exit 0
