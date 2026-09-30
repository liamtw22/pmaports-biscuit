"""Drive the light ring from Music Assistant's visualiser and colour roles.

All of the logic lives HERE rather than in sendspin, so the patch applied to
its vendored daemon is three lines: declare the roles, and call attach().

Why the roles have to be on the player and not on a client of our own: a
standalone VISUALIZER+COLOR client is legal and Music Assistant will grant it
both roles, but it never receives a stream/start, so
`_current_visualizer_config` stays None and every frame is dropped before the
callback. Measured - the standalone client got colour and zero frames, while
the player got loudness, spectrum and beats. Visualiser frames belong to a
playback session and only the player is in one.

Output is 12 RGB triples written to /run/biscuit-ring/live; biscuit-ring picks
them up at the music activity's priority, so voice and mute still overlay it.
"""

import asyncio
import colorsys
import logging
import math
import os
import struct
import time

_LOGGER = logging.getLogger("biscuit-viz")
# The daemon runs at WARNING, so everything this module logged at INFO was
# invisible - which hid a sweep that could never fire and a range reset
# nobody could confirm. Rather than log informational lines at WARNING, or
# turn on sendspin's whole INFO output, give this one logger a handler of
# its own and stop it propagating to the root.
if not _LOGGER.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    _LOGGER.addHandler(_handler)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False

RUN_DIR = "/run/biscuit-ring"
LIVE = os.path.join(RUN_DIR, "live")
SEGMENTS = 12

# The bins are uint16 against a FIXED full scale: sendspin's own reference
# renderer divides by 65535 and nothing else (sendspin/tui/visualizer.py,
# "Periodic values are uint16 (0-65535)"). An earlier comment here claimed
# the scale was undocumented, which is what motivated the adaptive
# normalisation below; `rawscale` selects the reference behaviour instead,
# so the two can be compared rather than argued about.
_FULL_SCALE = 65535.0
_DECAY = 0.995
_FLOOR = 800.0

# MEASURED on the live ring: normalising every bin against the single
# loudest one left segments 2-10 sitting at 0.63-0.83 of full with a
# standard deviation of about 0.10 - a ring that is almost fully lit and
# barely moves, which reads as random flicker rather than an equaliser.
# Music Assistant's log bins are far flatter than a raw FFT, so each bin
# now gets its own slowly-decaying maximum and slowly-rising minimum and
# is displayed across THAT range. Overall brightness still follows the
# music, through the global gain below, so a quiet passage is still quiet.
_BAND_RATE = 0.004      # per frame, ~8 s to forget a peak at 30 Hz
_MIN_RANGE_FRAC = 0.03  # a band flatter than this is noise, not signal
# The old downbeat boost added 0.35 to levels that were already at 0.8, so
# every beat slammed the whole ring to full. It now lifts the global gain
# instead, and by less.
_BEAT_BOOST = 0.15

# Frames arrive while the audio they describe is still in the player's
# buffer, so rendering on arrival runs the ring AHEAD of the sound. Each
# frame carries the server timestamp of the audio it belongs to, and the
# client can convert that to local play time - the same conversion the
# audio path uses - so a frame is held until its audio is actually heard.
_MAX_LEAD_S = 5.0       # beyond this the clock is wrong; draw immediately
# A frame waiting to be drawn belongs to the stream it arrived on. Without
# that, a callback scheduled up to _MAX_LEAD_S ago still fires after the
# stream ends - drawing the old track over the new one, and writing the
# band ranges that stream start has just reset. The pending handles are
# held so they can be cancelled outright, and the generation is checked
# anyway for the one that is already running when the stream changes.
# 30 Hz against a 5 s lead is about 150 outstanding, so 256 looked ample -
# and it was reached in normal playback within minutes, because the server
# sends frames in bursts rather than in real time. The number has to be
# far above the working maximum, since this exists only to stop unbounded
# growth if the clock is wrong. A handle is tiny; 4096 costs nothing.
_MAX_PENDING = 4096

# How much later than sendspin's play time the sound is actually heard.
# The player is NOT naive about this: sendspin/audio.py calibrates
# PortAudio's outputBufferDacTime against the loop clock and schedules
# the stream start from it, so everything PipeWire reports - including
# its 180 ms write-ahead into snd-aloop - is already compensated.
# What is NOT compensated is the part after PipeWire, because that is a
# separate process: biscuit-dsp reads the other end of the loopback and
# writes the codec, whose queue MEASURED 1548/1320/1280 frames at
# 48 kHz. That, and only that, is what the ring has to wait for.
#
# Getting this wrong is cheap to do and expensive to spot: r205 delayed
# by the whole 212 ms on the assumption that nothing downstream was
# compensated, and put the ring 0.17 s LATE.
_OUTPUT_DELAY_S = 0.03
try:
    # Tunable without a rebuild: a different output path - Bluetooth,
    # say - has a different queue in front of it.
    _OUTPUT_DELAY_S = float(os.environ["BISCUIT_VIZ_DELAY_MS"]) / 1000.0
except (KeyError, ValueError):
    pass

# MEASURED on r204: each band moved 0.121 of full scale between
# consecutive frames at 30 Hz, with a lag-1 autocorrelation of 0.72-0.93.
# That is what reads as chaotic - the bands are following the music, but
# they twitch every frame. Fast attack keeps transients, slow release
# lets a band fall back smoothly.
_ATTACK = 0.6           # of the way up per frame, ~2 frames to arrive
_RELEASE = 0.15         # of the way down per frame, ~200 ms to fall

# The two colours do not sweep evenly round the ring. They meet abruptly
# at one point and blend across the opposite side, which biscuit-ring
# then rotates so the hard edge lands on the volume-down button. A plain
# linear sweep put an equal amount of blend everywhere, so neither colour
# ever appeared on its own.
# Where the hard edge sits, in segments. biscuit-ring rotates by whole
# segments, which was one LED short one way and one too far the other:
# the wanted position is ON an LED, not between two. A half segment
# here supplies that, and it is the COLOUR that shifts - the bands stay
# mapped one to one onto the LEDs, so no spectral detail is smeared.
_SPLIT_PHASE = 0.0


def _mix_table(top, bottom):
    """How much of the far colour each segment carries.

    Position runs round the ring from the split at the bottom. There
    are two edges - the split itself, and where the colours meet
    opposite it - and each has its own width.
    """
    top = max(0.02, min(1.0, top))
    bottom = max(0.02, min(1.0, bottom))
    out = []
    steps = 16
    for i in range(SEGMENTS):
        total = 0.0
        for k in range(steps):
            # Average over the arc the LED covers rather than sampling
            # its centre, so an edge running through an LED comes out
            # part way by itself.
            u = ((i + (k + 0.5) / steps - _SPLIT_PHASE) % SEGMENTS) / SEGMENTS
            d_top = u - 0.5                      # signed, + past the top
            d_bot = u if u < 0.5 else u - 1.0    # signed, + past the split
            if abs(d_bot) < abs(d_top):
                x = 1.0 - (d_bot + bottom / 2.0) / bottom
            else:
                x = (d_top + top / 2.0) / top
            x = max(0.0, min(1.0, x))
            total += x * x * (3.0 - 2.0 * x)   # smoothstep
        out.append(total / steps)
    return tuple(out)



# Music Assistant sends a UI palette, not two display colours. A real
# example: primary=(169,65,56) a strong red, accent=(34,36,36) a near-black
# NEUTRAL, on_dark=(225,223,209) near-white. Interpolating primary->accent
# therefore runs the ring from red through grey, which reads as washed-out
# white with no visible second colour - which is exactly how the first
# version looked.
#
# So the ring derives its own pair from the DOMINANT colour: primary with
# its saturation lifted, and a second end rotated round the hue wheel.
# accent is used for that second end only when it is actually a colour -
# saturated enough, and a different hue - rather than a neutral.
# Saturation near full, not merely high: the ring is behind a diffuser, so
# any white component in the mix reads as washed out long before it looks
# pale on screen. primary=(169,65,56) is only a 2.6:1 channel ratio and
# came out white on the hardware; at 0.92 the weakest channel is ~5%.
_MIN_SAT = 0.92
_HUE_SPREAD = 0.15      # ~54 degrees, enough to read as two colours
_ACCENT_MIN_SAT = 0.30
_ACCENT_MIN_HUE_GAP = 0.06

# Fallback palette until a colour message arrives: the same blue-to-cyan the
# static music animation uses, so the ring does not change character just
# because a track has no colour yet.
_DEFAULT_PRIMARY = (0x00, 0x50, 0xC8)
_DEFAULT_ACCENT = (0x00, 0xE0, 0xFF)


# Everything below is OFF by default, so the shipped look is the one that
# was measured and agreed. Each is live: the file is re-read once a
# second, so an option can be changed while music plays - restarting the
# player drops the stream, which makes A/B comparison painful otherwise.
_CONF_FILES = ("/opt/persist/viz.conf", "/run/biscuit-viz.conf")
_DEFAULTS = {
    # Off hands the ring back to the static music animation, so it still
    # shows that something is playing.
    "enabled": 1.0,
    "motion": 0.5,     # strength of the sweep a bar boundary sends round
    # MEASURED over three paired cycles: the swing in overall brightness
    # is the same either way (sd 0.272/0.265/0.267 against 0.272/0.280/
    # 0.293), because the spectrum's own maximum tracks the same thing.
    # Kept because another server's loudness may behave differently.
    "loudness": 0.0,   # take overall level from the loudness frame
    "punch": 0.5,      # transient lift from peak_strength
    "fpeak": 0.0,      # hue push from the dominant frequency, in hue units
    "rawscale": 0.0,   # normalise bins by 65535 like the reference client
    # How much the OVERALL level drives brightness, as opposed to each
    # band's own movement. MEASURED on the ring: 83% of all frame-to-
    # frame change was the whole ring moving together, 74% of it even
    # with the transient lift and the sweep switched off - which is what
    # reads as chaotic flicker rather than as a spectrum. 0.75 reproduces
    # the old fixed floor of 0.25; 0 holds brightness steady and lets the
    # bands speak for themselves.
    # MEASURED, and it does NOT do what it was built for: the whole-ring
    # share stayed at 73-77% across 0.75, 0.4, 0.2 and 0. The common
    # movement comes from the music, not from this gain. Left in because
    # it is a legitimate control over how much level drives brightness.
    "pump": 0.75,
    # Subtract the ring's own moving average, so what shows is the
    # spectrum's SHAPE rather than its level. This is the one that
    # targets the 76% of movement that is every band rising together.
    "relative": 0.5,
    # 0 keeps the second colour a fixed rotation from the first, which is
    # the pairing chosen on the hardware (red to yellow). 1 picks the
    # rotation whose brightness MATCHES the first instead, which makes an
    # even split read evenly - red to magenta at a luminance ratio of
    # 1.01 against 3.06 - at the cost of the colours you get.
    "balance": 0.0,
    # How much of the circumference is blend rather than either colour
    # on its own. MEASURED with a camera on the ring: at 0.5 the blend is
    # half the circle, and because a hue path from blue to magenta spends
    # most of its length in violets that read as the magenta side, the
    # two colours came out at about 100 and 220 degrees of arc rather
    # than 180 each. Narrowing it trades the length of the fade for a
    # more even-looking split.
    # One width per edge, because the two edges are not the same thing:
    # the split at the bottom wants to be hard and the meeting at the top
    # soft, and a single symmetric width cannot say that.
    "fade_top": 0.05,
    "fade_bottom": 0.05,
    "reset": 1.0,      # rescale the bands when a new track starts
    # Chosen on the hardware against 1.0, 1.9 and 2.2: 1.6 adds contrast
    # without going dark - it actually reads BRIGHTER on average than no
    # curve at all (mean 0.46 against 0.39), because a curve below 2
    # lifts the mid-range before it darkens the bottom.
    "gamma": 1.6,      # output curve; 1 is linear
    # Superseded: the far end is now CHOSEN to match the first in
    # brightness, which costs nothing, where this dimmed the brighter
    # half and still left the split looking uneven on the hardware.
    "lummatch": 0.0,   # equalise the two ends' luminance, 0 to 1
}
_SWEEP_S = 0.45         # how long a sweep takes to go round
# MEASURED: Music Assistant sends peak_strength but NEVER is_downbeat.
# At motion=2.0, where a sweep saturates whatever segment it crosses,
# not one frame in 601 was pinned - so a beat-triggered sweep could
# never fire, and _BEAT_BOOST has been dead code since r204. A strong
# transient launches it instead; the downbeat path stays for a server
# that does send them.
_SWEEP_TRIGGER = 0.45   # normalised peak_strength that launches a sweep
# Beats arrive, but nothing marks a bar: is_downbeat is present on every
# beat frame and never True, which is what tracks_downbeats exists to
# declare. Firing on every beat filled the ring (85-90% of frames at full
# against 42-44%); firing only on a True that never comes fired nothing at
# all. Counting beats locally gives bar-like spacing either way, and a
# real downbeat takes over the moment one appears.
_SWEEP_EVERY = 4        # beats between sweeps when the server marks no bars
_MEAN_RATE = 0.01       # how fast the ring's reference level follows the mix


class _State:
    def __init__(self):
        self.peak = _FLOOR
        self.primary = _DEFAULT_PRIMARY
        self.accent = _DEFAULT_ACCENT
        self.beat = 0.0
        self.hi = [_FLOOR] * SEGMENTS
        self.shown = [0.0] * SEGMENTS
        self.lo = [_FLOOR] * SEGMENTS
        self.client = None
        self.palette_key = None
        self.palette = None
        self.loop = None
        self.generation = 0
        self.pending = []
        self.skipped = 0
        self.conf = dict(_DEFAULTS)
        self.conf_at = -99.0
        self.sweep_at = None
        self.punch = 0.0
        self.loud = None
        self.loud_peak = 1.0
        self.fpeak = None
        self.seen = set()
        self.seen_logged = False
        self.track = None
        self.first_frame = None
        self.seen_beat = False
        self.beats = 0
        self.mean_slow = None
        self.downbeats = 0
        self.started = time.monotonic()


_S = _State()


def _conf():
    """The live options, re-read at most once a second."""
    now = time.monotonic()
    if now - _S.conf_at < 1.0:
        return _S.conf
    _S.conf_at = now
    values = dict(_DEFAULTS)
    # The persistent file holds what the owner chose; the one in /run
    # overrides it, which is how a setting can be tried out live.
    for path in _CONF_FILES:
        try:
            with open(path, "r") as handle:
                for line in handle:
                    key, _, raw = line.split("#", 1)[0].partition("=")
                    key = key.strip()
                    if key in values:
                        try:
                            values[key] = float(raw)
                        except ValueError:
                            pass
        except OSError:
            pass
    if values != _S.conf:
        _LOGGER.info("visualiser options: %s",
                     " ".join("%s=%g" % kv for kv in sorted(values.items())))
        _S.palette_key = None
    _S.conf = values
    return values


def _rgb(value):
    """A protocol colour to an (r, g, b) tuple, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        r, g, b = (int(c) for c in tuple(value)[:3])
    except (TypeError, ValueError):
        return None
    if not all(0 <= c <= 255 for c in (r, g, b)):
        return None
    return (r, g, b)


def _ends_hls():
    """The two ends of the gradient, as (hue, saturation)."""
    ph, _pl, psat = colorsys.rgb_to_hls(*[c / 255.0 for c in _S.primary])
    ah, _al, asat = colorsys.rgb_to_hls(*[c / 255.0 for c in _S.accent])
    near = (ph, max(psat, _MIN_SAT))

    candidates = []
    gap = abs(ah - ph)
    gap = min(gap, 1.0 - gap)
    if asat >= _ACCENT_MIN_SAT and gap >= _ACCENT_MIN_HUE_GAP:
        delta = (ah - ph + 0.5) % 1.0 - 0.5
        # An accent only 22 degrees from the primary clears the gate but
        # does not read as a second colour, so keep its DIRECTION and push
        # it out to the separation the derived pair gets.
        if abs(delta) < _HUE_SPREAD:
            delta = _HUE_SPREAD if delta >= 0 else -_HUE_SPREAD
        candidates.append((ph + delta, max(asat, _MIN_SAT)))
    far_sat = max(psat, _MIN_SAT)
    candidates.append((ph + _HUE_SPREAD, far_sat))
    candidates.append((ph - _HUE_SPREAD, far_sat))

    # Pick the end that MATCHES THE FIRST IN BRIGHTNESS. Two colours of very
    # different luminance never read as an even split: rotating +54 degrees
    # from Music Assistant's red lands on yellow at 224 against the red's 73,
    # and the bright half looks like it owns two thirds of the ring. Rotating
    # the other way lands near magenta at 74 - a ratio of 1.01 - and the
    # split reads where it actually is. Which direction wins depends on the
    # colour (blue prefers +, green -), so it has to be chosen, not fixed.
    # This is what the `lummatch` option was trying to patch up afterwards,
    # and it costs no brightness at all.
    if _conf()["balance"] <= 0.0:
        return near, candidates[0]
    near_luma = _luma(_rgb_of(near))
    def mismatch(end):
        far_luma = _luma(_rgb_of(end))
        return max(near_luma, far_luma) / max(1.0, min(near_luma, far_luma))
    return near, min(candidates, key=mismatch)


def _rgb_of(end):
    """An (hue, saturation) end as 0-255 RGB."""
    return tuple(c * 255.0 for c in colorsys.hls_to_rgb(end[0] % 1.0, 0.5, end[1]))


def _luma(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _match_luminance(stops, amount):
    """Pull the stops towards a common brightness, by dimming only."""
    lo, hi = _luma(stops[0]), _luma(stops[-1])
    if lo <= 0.0 or hi <= 0.0:
        return stops
    # Yellow at this saturation carries about 2.5x the luminance of the red
    # it is paired with, and the brighter end dominates the ring - which is
    # what makes a fade look off-centre even when the geometry is exact.
    # The target is the geometric mean, and stops are only ever dimmed:
    # they are already at full chroma, so brightening would wash them out.
    target = (lo * hi) ** 0.5
    out = []
    for rgb in stops:
        y = _luma(rgb)
        scale = 1.0 if y <= 0.0 else min(1.0, target / y)
        scale = 1.0 + (scale - 1.0) * amount
        out.append(tuple(c * scale for c in rgb))
    return tuple(out)


def _palette(shift, lummatch):
    """The 12 base colours, one per segment."""
    conf = _conf()
    top, bottom = conf["fade_top"], conf["fade_bottom"]
    key = (_S.primary, _S.accent, round(shift, 3), round(lummatch, 2),
           conf["balance"] > 0.0, round(top, 3), round(bottom, 3))
    if _S.palette_key == key:
        return _S.palette
    (h0, s0), (h1, s1) = _ends_hls()
    h0 += shift
    h1 += shift
    out = []
    mix = _mix_table(top, bottom)
    for t in mix:
        # Interpolate the HUE, not the RGB. MEASURED on the real palette:
        # lerping red (245,29,10) to yellow (245,240,10) in RGB is 59% of
        # the way round the hue wheel at its own midpoint, because only the
        # green channel moves. The blend therefore looks off-centre - the
        # fade appeared to sit at 11 o'clock rather than 12 - even though
        # the geometry is exactly opposite the split.
        #
        # Lightness stays 0.5: the spectrum supplies brightness, so the
        # palette must not also dim itself or quiet bands vanish entirely.
        rgb = colorsys.hls_to_rgb((h0 + (h1 - h0) * t) % 1.0, 0.5,
                                  s0 + (s1 - s0) * t)
        out.append(tuple(c * 255.0 for c in rgb))
    if lummatch > 0.0:
        out = _match_luminance(tuple(out), lummatch)
    _S.palette_key = key
    _S.palette = tuple(out)
    return _S.palette


def _write(frame):
    """Publish one frame. Never raises: this is on the player's callback."""
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        tmp = LIVE + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(struct.pack("%dB" % (SEGMENTS * 3),
                                     *[c for rgb in frame for c in rgb]))
        os.replace(tmp, LIVE)
    except OSError as err:
        _LOGGER.debug("live frame not written: %s", err)


def on_color(payload):
    """server/state carrying a palette derived from the current audio."""
    colour = getattr(payload, "color", None)
    if colour is None:
        return
    primary = _rgb(getattr(colour, "primary", None))
    accent = _rgb(getattr(colour, "accent", None))
    # A field that is absent or explicitly null means "unchanged" rather than
    # "black", so only replace what actually arrived.
    if primary:
        # A palette change is the most reliable sign of a new track that
        # reaches this module; a queue can move on without a new stream.
        if primary != _S.primary and _conf()["reset"] > 0.0:
            reset_ranges("new palette")
        _S.primary = primary
    if accent:
        _S.accent = accent or _S.accent


def _render(spectrum):
    """Draw one spectrum on the ring."""
    conf = _conf()
    if conf["enabled"] <= 0.0:
        # Stop writing and the live frame goes stale, which hands the ring
        # back to the animation the music activity is showing.
        return
    n = len(spectrum)
    if not n:
        return
    # Tolerate a bin count that is not 12: the server may negotiate down.
    vals = [float(spectrum[i * n // SEGMENTS]) for i in range(SEGMENTS)]

    top = max(vals)
    _S.peak = max(top, _S.peak * _DECAY, _FLOOR)
    if conf["rawscale"] > 0.0:
        # What the reference client does: the bins against their own full
        # scale, no adaptation of any kind.
        out = []
        palette = _palette(0.0, conf["lummatch"])
        gamma = conf["gamma"]
        for i, value in enumerate(vals):
            level = max(0.0, min(1.0, value / _FULL_SCALE))
            was = _S.shown[i]
            rate = _ATTACK if level > was else _RELEASE
            level = was + (level - was) * rate
            _S.shown[i] = level
            shown = level ** gamma if gamma != 1.0 else level
            base = palette[i]
            out.append(tuple(max(0, min(255, int(round(base[c] * shown))))
                             for c in range(3)))
        _write(out)
        return
    if conf["loudness"] > 0.0 and _S.loud is not None:
        # The loudness frame is the server's own measure of level. Its
        # absolute scale is undocumented, so it is normalised the same way
        # the spectrum is rather than assumed to be dB or full scale.
        _S.loud_peak = max(_S.loud, _S.loud_peak * _DECAY, 1.0)
        drive = min(1.0, _S.loud / _S.loud_peak)
    else:
        drive = min(1.0, top / _S.peak)
    gain = 1.0 - conf["pump"] * (1.0 - drive)
    # Transients are added at the END, after the output curve, not folded
    # into the gain. Folded in they were nearly invisible: the gain is
    # already close to 1 through any loud passage, so `min(1.0, gain +
    # boost)` had almost nothing left to give. A curve below 1 is also
    # what MAKES the headroom a transient needs.
    lift = _BEAT_BOOST * _S.beat + conf["punch"] * _S.punch
    quiet = max(_FLOOR, _MIN_RANGE_FRAC * _S.peak)

    shift = 0.0
    if conf["fpeak"] > 0.0 and _S.fpeak:
        # Where the dominant frequency sits in the analysed band, on a log
        # scale, pushes the gradient round the wheel. The note-to-hue
        # mapping this replaces needed the `pitch` type, which Music
        # Assistant does not send - measured, not assumed.
        pos = math.log(max(20.0, min(20000.0, _S.fpeak)) / 20.0)
        shift = conf["fpeak"] * (pos / math.log(1000.0) - 0.5)

    sweep = None
    if conf["motion"] > 0.0 and _S.sweep_at is not None:
        age = time.monotonic() - _S.sweep_at
        if age <= _SWEEP_S:
            sweep = age / _SWEEP_S * SEGMENTS
        else:
            _S.sweep_at = None

    gamma = conf["gamma"]
    palette = _palette(shift, conf["lummatch"])
    levels = []
    for i, value in enumerate(vals):
        hi, lo = _S.hi[i], _S.lo[i]
        span = hi - lo
        hi = max(value, hi - span * _BAND_RATE)
        lo = min(value, lo + span * _BAND_RATE)
        _S.hi[i], _S.lo[i] = hi, lo
        span = hi - lo
        # A band that is not moving is the noise floor; expanding it would
        # turn silence into a full-scale bar.
        level = (value - lo) / span if span >= quiet else 0.0
        level = max(0.0, min(1.0, level)) * gain
        was = _S.shown[i]
        rate = _ATTACK if level > was else _RELEASE
        level = was + (level - was) * rate
        _S.shown[i] = level
        levels.append(level)

    # Common mode. MEASURED: 76% of every frame-to-frame change was the
    # whole ring moving together, and it stayed at 73-77% whatever `pump`
    # was set to - it is the music, not the gain. Subtracting the ring's
    # own SLOW average takes out the pumping and leaves the shape.
    mean_now = sum(levels) / SEGMENTS
    if _S.mean_slow is None:
        _S.mean_slow = mean_now
    else:
        _S.mean_slow += (mean_now - _S.mean_slow) * _MEAN_RATE
    common = conf["relative"] * (mean_now - _S.mean_slow)

    out = []
    for i, level in enumerate(levels):
        shown = max(0.0, min(1.0, level - common))
        # The curve is applied to the OUTPUT, never to the stored level:
        # the smoothing has to keep working in the same units as the levels
        # it is smoothing.
        shown = shown ** gamma if gamma != 1.0 else shown
        if sweep is not None:
            gap = abs(i - sweep)
            gap = min(gap, SEGMENTS - gap)
            # Added after the curve, so the sweep stays visible over a quiet
            # band instead of being crushed with it.
            shown = min(1.0, shown + conf["motion"]
                        * math.exp(-(gap / 1.3) ** 2))
        shown = min(1.0, shown + lift)
        base = palette[i]
        out.append(tuple(max(0, min(255, int(round(base[c] * shown))))
                         for c in range(3)))
    _S.beat *= 0.6
    _S.punch *= 0.5
    _write(out)


def _apply(spectrum, downbeat, extras, generation=None):
    if generation is not None and generation != _S.generation:
        return          # a frame from a stream that has since ended
    loudness, peak, fpeak = extras
    if loudness is not None:
        _S.loud = float(loudness)
    if peak is not None:
        value = float(peak)
        # A byte on the wire, but do not assume it: take anything above 1
        # as 0-255 and anything at or below it as already normalised.
        value = value / 255.0 if value > 1.0 else value
        _S.punch = max(_S.punch, value)
        # Only where the server sends no beats at all: with beats
        # present this fires constantly and drowns the bar boundaries.
        if (not _S.seen_beat and value >= _SWEEP_TRIGGER
                and _S.sweep_at is None and _conf()["motion"] > 0.0):
            _S.sweep_at = time.monotonic()
    if fpeak is not None:
        _S.fpeak = float(fpeak)
    if downbeat is not None:
        _S.seen_beat = True
        _S.beats += 1
        if downbeat:
            _S.downbeats += 1
        # A bar boundary is worth more than an ordinary beat.
        _S.beat = 1.0 if downbeat else 0.5
        due = downbeat if _S.downbeats else (_S.beats % _SWEEP_EVERY == 1)
        if due and _conf()["motion"] > 0.0 and _S.sweep_at is None:
            _S.sweep_at = time.monotonic()
    if spectrum:
        _render(spectrum)


def drop_pending(reason):
    """Cancel frames still waiting to be drawn; they are the old stream's."""
    _S.generation += 1
    dropped = 0
    for handle in _S.pending:
        try:
            handle.cancel()
            dropped += 1
        except Exception:  # noqa: BLE001 - never break the player
            pass
    _S.pending = []
    if dropped or _S.skipped:
        _LOGGER.info("dropped %d frame(s) waiting to be drawn%s: %s",
                     dropped,
                     "" if not _S.skipped else
                     " (and skipped %d over the queue limit)" % _S.skipped,
                     reason)
    _S.skipped = 0


def reset_ranges(reason):
    """Forget the per-band ranges, so a new track is scaled on its own."""
    _S.hi = [_FLOOR] * SEGMENTS
    _S.lo = [_FLOOR] * SEGMENTS
    _S.peak = _FLOOR
    _S.loud_peak = 1.0
    _LOGGER.info("visualiser ranges reset: %s", reason)


def on_server_hello(payload=None):
    """Report what the server granted, so it is never inferred again."""
    # ServerHelloCallback is handed the PAYLOAD, not the message - the
    # stream/start listener taught that the hard way, by silently
    # logging None for a fortnight's worth of streams.
    _LOGGER.info("server %r v%s granted roles: %s",
                 getattr(payload, "name", None),
                 getattr(payload, "version", None),
                 ", ".join(getattr(payload, "active_roles", None) or []) or "none")


def on_metadata(payload=None):
    """Log a track once, when it changes. Proves the METADATA role works."""
    meta = getattr(payload, "metadata", None)
    if meta is None:
        return
    def field(name):
        value = getattr(meta, name, None)
        # Absent fields are an UndefinedField sentinel, not None.
        return value if isinstance(value, str) else None
    title, artist = field("title"), field("artist")
    if not title and not artist:
        return
    now = (title, artist)
    if now == _S.track:
        return
    _S.track = now
    _LOGGER.info("now playing: %s - %s", artist or "?", title or "?")


def on_stream_start(message=None):
    drop_pending("stream start")
    # The server declares here whether it marks bars at all, which is the
    # difference between a sweep that can fire and one that cannot.
    # On the payload, not the message: getattr on the message returned
    # None in silence and the line never printed.
    visual = getattr(getattr(message, "payload", None), "visualizer", None)
    if visual is not None:
        _LOGGER.info("stream visualiser: types=%s tracks_downbeats=%s",
                     getattr(visual, "types", None),
                     getattr(visual, "tracks_downbeats", None))
    if _S.beats:
        _LOGGER.info("previous stream: %d beats, %d of them downbeats",
                     _S.beats, _S.downbeats)
    _S.beats = _S.downbeats = 0
    if _conf()["reset"] > 0.0:
        reset_ranges("stream start")


def _lead_s(frame):
    """How long until the audio this frame describes is heard."""
    client = _S.client
    stamp = getattr(frame, "timestamp_us", None)
    if client is None or not stamp:
        return 0.0
    try:
        return (client.compute_play_time(stamp) - client.now_us()) / 1e6
    except Exception:  # noqa: BLE001 - an unsynced clock is not fatal
        return 0.0


def _note_fields(frame):
    """Report which fields the server actually sends, once."""
    if _S.seen_logged:
        return
    for name in ("loudness", "spectrum", "f_peak_freq", "f_peak_amp",
                 "peak_strength", "pitch_confidence", "pitch_midi_q88",
                 "is_downbeat"):
        # `not in (None, False)` hid is_downbeat=False, an ordinary beat.
        if getattr(frame, name, None) is not None:
            _S.seen.add(name)
    # Timed from the FIRST FRAME, not from import: frames only start
    # when something plays, which is usually long after this module is
    # loaded, and the window had already expired by then - so the
    # report went out after a single frame and listed almost nothing.
    if _S.first_frame is None:
        _S.first_frame = time.monotonic()
        return
    if time.monotonic() - _S.first_frame < 8.0:
        return
    _S.seen_logged = True
    missing = [n for n in ("loudness", "spectrum", "peak_strength",
                           "is_downbeat", "f_peak_freq",
                           "pitch_confidence") if n not in _S.seen]
    _LOGGER.info("visualiser fields: have %s; absent %s",
                    ", ".join(sorted(_S.seen)) or "none",
                    ", ".join(missing) or "none")


def on_frames(frames):
    """One or more visualiser frames: spectrum, loudness, peak or a beat."""
    for frame in frames:
        _note_fields(frame)
        # is_downbeat is None on a frame that is not a beat, False on an
        # ordinary beat and True only at a bar boundary. Testing it for
        # truth threw away every ordinary beat and reported the field as
        # absent, which is how "the server sends no beats" was concluded.
        downbeat = getattr(frame, "is_downbeat", None)
        beat = downbeat is not None
        spectrum = getattr(frame, "spectrum", None)
        extras = (getattr(frame, "loudness", None),
                  getattr(frame, "peak_strength", None),
                  getattr(frame, "f_peak_freq", None))
        if not spectrum and not beat and not any(x is not None for x in extras):
            continue
        # The play time sendspin computes is when IT plays the audio, not
        # when the speaker does; the rest of the chain is _OUTPUT_DELAY_S.
        lead = _lead_s(frame) + _OUTPUT_DELAY_S
        loop = _S.loop
        if loop is None:
            try:
                loop = _S.loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        # Each frame is drawn on its own, at its own time: a callback that
        # carries several used to collapse to the last one.
        if loop is not None and 0.0 < lead <= _MAX_LEAD_S:
            if len(_S.pending) >= _MAX_PENDING:
                # Skip it rather than draw it now. Drawing a frame early
                # shows audio that has not been heard yet, which is a
                # visible desync; one missing frame out of thirty a
                # second is not visible at all.
                _S.skipped += 1
                continue
            handle = loop.call_later(lead, _apply, spectrum, downbeat,
                                     extras, _S.generation)
            _S.pending.append(handle)
            if len(_S.pending) > 32:
                # Drop the ones that have already run, so the list tracks
                # what is actually outstanding.
                _S.pending = [h for h in _S.pending if not h.cancelled()
                              and h.when() > loop.time()]
        else:
            _apply(spectrum, downbeat, extras)


def attach(client):
    """Register on a SendspinClient. Called from the patched daemon."""
    try:
        _S.client = client
        client.add_visualizer_listener(on_frames)
        client.add_color_listener(on_color)
        client.add_server_hello_listener(on_server_hello)
        client.add_metadata_listener(on_metadata)
        client.add_stream_start_listener(on_stream_start)
        client.add_stream_end_listener(lambda *a: drop_pending("stream end"))
        client.add_stream_clear_listener(lambda *a: drop_pending("stream clear"))
        _LOGGER.info("biscuit visualiser attached (%d segments)", SEGMENTS)
    except Exception as err:  # noqa: BLE001 - never break the player
        _LOGGER.warning("could not attach the visualiser: %s", err)
