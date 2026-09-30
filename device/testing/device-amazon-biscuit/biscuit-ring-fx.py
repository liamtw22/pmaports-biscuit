#!/usr/bin/env python3
"""Generate ring animations procedurally, in any colour.

WHY GENERATE RATHER THAN RECOLOUR
---------------------------------
Stock's 283 animations have their gradients, tails and fades baked into the
pixel data, so "the same effect in green" cannot be produced by substituting a
hue. Generating instead makes every effect a function of (pattern, colour,
speed), which is what lets Home Assistant offer one effect list that works in
any colour without a combinatorial explosion of files.

No stock animation is shipped. The ACTIVITY DEFAULTS (wake / listen / think
and friends) are these generated effects too, with colours and motion measured
from the stock animations (see biscuit-va-leds.py). An owner who imports the
animations from their own device can choose those instead.

WHAT THE VOCABULARY IS BASED ON
-------------------------------
Not filenames - filenames describe Alexa product states. These were derived by
classifying all 283 animations on the owner's device by their pixel data; the
effects below are our own code, and no stock frame data is embedded. After removing 14 exact
duplicates and collapsing five numbered families (timer_led_countdown_sec-01..60,
volume_step, OTA_step, boot, anim_start_phase = 109 files), the distinct visual
behaviours are the ones below. Rendered, stock's primitives look like:

    chase                | @ @ @ @ @ @ |  every other segment, shifting
    comet                | :+W   :+W   |  bright heads with fading tails
    3trace               | BBB         |  a 3-segment arc rotating
    scan_cyan            | @           |  a single dot orbiting
    mirror               | W          W|  two dots converging then diverging
    alexa_thinking       | B@BBB=BBBBB |  random sparkles on a base colour
    startup-loading-full | @BBBBBBBBBBB|  full ring, one brighter dot orbiting

OUTPUT FORMAT
-------------
biscuit-ring's own text format, one frame per line:
    <duration_ms>:<seg0>,<seg1>,...,<seg11>
with a bare `loop` marking where playback returns to. Six hex digits per colour
(RGB888). An animation with no `loop` plays once and retires itself.
"""

import colorsys
import math
import os
import random

SEG = 12
FX_DIR = "/run/biscuit-ring/fx"


def _hex(rgb):
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    return "%02X%02X%02X" % (r, g, b)


def _scale(rgb, f):
    return tuple(c * f for c in rgb)


def _wheel(pos):
    """pos 0..1 -> a saturated RGB around the hue circle."""
    r, g, b = colorsys.hsv_to_rgb(pos % 1.0, 1.0, 1.0)
    return (r * 255, g * 255, b * 255)


def _frames_to_text(frames, loop=True):
    out = []
    if loop:
        out.append("loop")
    for ms, px in frames:
        out.append("%d:%s" % (ms, ",".join(_hex(p) for p in px)))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Colourable effects. Each takes an (r, g, b) base and returns frames.
# --------------------------------------------------------------------------

def fx_solid(c, ms=1000, **_):
    return [(ms, [c] * SEG)]


def fx_arc_spin(c, ms=66, width=3, **_):
    """Stock `3trace`: a lit arc of `width` segments rotating."""
    fr = []
    for i in range(SEG):
        px = [(0, 0, 0)] * SEG
        for w in range(width):
            px[(i + w) % SEG] = c
        fr.append((ms, px))
    return fr


def fx_arc_spin_inverse(c, ms=66, gap=3, **_):
    """Stock `3traceinv`: the ring lit except a moving dark gap."""
    fr = []
    for i in range(SEG):
        px = [c] * SEG
        for w in range(gap):
            px[(i + w) % SEG] = (0, 0, 0)
        fr.append((ms, px))
    return fr


def fx_dot_orbit(c, ms=66, **_):
    """Stock `scan_cyan`: one segment orbiting."""
    return fx_arc_spin(c, ms, width=1)


def fx_comet(c, ms=33, heads=2, tail=2, **_):
    """Stock `comet`: bright heads each trailing a fade, evenly spaced.

    heads=2, tail=2 reproduces stock exactly: `:+W   :+W   ` - two comets of
    three lit segments with three dark between them, 6 of 12 lit. Three heads
    with a 3-long tail fills the ring completely and loses the gaps that make
    it read as motion.
    """
    fr = []
    spacing = SEG // heads
    for i in range(SEG):
        px = [(0, 0, 0)] * SEG
        for h in range(heads):
            head = (i + h * spacing) % SEG
            for t in range(tail + 1):
                f = 1.0 - (t / (tail + 1.0))
                idx = (head - t) % SEG
                cur = px[idx]
                new = _scale(c, f)
                px[idx] = tuple(max(a, b) for a, b in zip(cur, new))
        fr.append((ms, px))
    return fr


def fx_theater_chase(c, ms=132, every=2, **_):
    """Stock `chase`: every Nth segment lit, the pattern shifting."""
    fr = []
    for i in range(every):
        px = [(0, 0, 0)] * SEG
        for s in range(SEG):
            if (s + i) % every == 0:
                px[s] = c
        fr.append((ms, px))
    return fr


def fx_highlight_orbit(c, ms=88, dim=0.25, **_):
    """Stock `startup-loading-full`: whole ring dim, one segment full."""
    fr = []
    base = _scale(c, dim)
    for i in range(SEG):
        px = [base] * SEG
        px[i] = c
        fr.append((ms, px))
    return fr


def fx_mirror_scan(c, ms=66, **_):
    """Stock `mirror`: two dots converging and diverging."""
    fr = []
    for i in range(SEG // 2 + 1):
        px = [(0, 0, 0)] * SEG
        px[i % SEG] = c
        px[(SEG - 1 - i) % SEG] = c
        fr.append((ms, px))
    for i in range(SEG // 2 - 1, 0, -1):
        px = [(0, 0, 0)] * SEG
        px[i % SEG] = c
        px[(SEG - 1 - i) % SEG] = c
        fr.append((ms, px))
    return fr


def fx_twinkle(c, ms=66, count=20, **_):
    """Stock `alexa_thinking`: a dim base with random segments flaring."""
    rnd = random.Random(1)
    fr = []
    base = _scale(c, 0.35)
    for _ in range(count):
        px = [base] * SEG
        for s in rnd.sample(range(SEG), rnd.randint(1, 3)):
            px[s] = c
        fr.append((ms, px))
    return fr


def fx_breathe(c, ms=50, steps=24, floor=0.06, **_):
    fr = []
    for i in range(steps):
        f = floor + (1 - floor) * (0.5 - 0.5 * math.cos(2 * math.pi * i / steps))
        fr.append((ms, [_scale(c, f)] * SEG))
    return fr


def fx_blink(c, ms=500, **_):
    return [(ms, [c] * SEG), (ms, [(0, 0, 0)] * SEG)]


def fx_wipe_in(c, ms=60, **_):
    fr = []
    for i in range(SEG + 1):
        px = [c if s < i else (0, 0, 0) for s in range(SEG)]
        fr.append((ms, px))
    return fr


def fx_drain_out(c, ms=60, **_):
    fr = []
    for i in range(SEG, -1, -1):
        px = [c if s < i else (0, 0, 0) for s in range(SEG)]
        fr.append((ms, px))
    return fr


def fx_larson(c, ms=60, tail=3, **_):
    """WLED scanner: a head sweeping back and forth, NOT wrapping.

    Genuinely absent from stock, whose scans all rotate continuously.
    """
    fr = []
    seq = list(range(SEG)) + list(range(SEG - 2, 0, -1))
    for head in seq:
        px = [(0, 0, 0)] * SEG
        for t in range(tail + 1):
            f = 1.0 - (t / (tail + 1.0))
            for idx in (head - t, head + t):
                if 0 <= idx < SEG:
                    cur = px[idx]
                    px[idx] = tuple(max(a, b) for a, b in zip(cur, _scale(c, f)))
        fr.append((ms, px))
    return fr


def fx_running(c, ms=60, waves=2, steps=24, **_):
    """WLED running lights: a sine brightness wave travelling round."""
    fr = []
    for i in range(steps):
        px = []
        for s in range(SEG):
            phase = 2 * math.pi * (waves * s / SEG - i / steps)
            f = 0.15 + 0.85 * (0.5 + 0.5 * math.sin(phase))
            px.append(_scale(c, f))
        fr.append((ms, px))
    return fr


def fx_heartbeat(c, ms=40, **_):
    """WLED heartbeat: two quick beats then a rest."""
    fr = []
    for f in (0.15, 0.6, 1.0, 0.6, 0.25):
        fr.append((ms, [_scale(c, f)] * SEG))
    for f in (0.5, 0.85, 0.5, 0.2):
        fr.append((ms, [_scale(c, f)] * SEG))
    fr.append((700, [_scale(c, 0.08)] * SEG))
    return fr


# --------------------------------------------------------------------------
# Intrinsic-colour effects. The colour IS the effect; a base is ignored.
# --------------------------------------------------------------------------

def fx_rainbow(_c=None, ms=1000, **_):
    return [(ms, [_wheel(s / SEG) for s in range(SEG)])]


def fx_rainbow_spin(_c=None, ms=66, **_):
    fr = []
    for i in range(SEG):
        fr.append((ms, [_wheel(((s + i) % SEG) / SEG) for s in range(SEG)]))
    return fr


def fx_colour_cycle(_c=None, ms=80, steps=36, **_):
    fr = []
    for i in range(steps):
        fr.append((ms, [_wheel(i / steps)] * SEG))
    return fr


def fx_fire(_c=None, ms=70, count=30, **_):
    """WLED fire flicker: warm palette, per-segment noise."""
    rnd = random.Random(7)
    fr = []
    for _ in range(count):
        px = []
        for _s in range(SEG):
            heat = rnd.uniform(0.35, 1.0)
            px.append((255 * heat, 95 * heat * heat, 8 * heat * heat * heat))
        fr.append((ms, px))
    return fr


def fx_candle(_c=None, ms=120, count=24, **_):
    """WLED candle: slow, gentler flicker, whole ring roughly together."""
    rnd = random.Random(11)
    fr = []
    for _ in range(count):
        base = rnd.uniform(0.55, 1.0)
        px = []
        for _s in range(SEG):
            j = base * rnd.uniform(0.9, 1.05)
            px.append((255 * j, 130 * j * j, 30 * j * j * j))
        fr.append((ms, px))
    return fr


# --------------------------------------------------------------------------
# Two-colour
# --------------------------------------------------------------------------

def fx_police(c=(255, 0, 0), c2=(0, 0, 255), ms=120, **_):
    """WLED police: two halves alternating between two colours."""
    a = [c] * (SEG // 2) + [(0, 0, 0)] * (SEG // 2)
    b = [(0, 0, 0)] * (SEG // 2) + [c2] * (SEG // 2)
    return [(ms, a), (ms, b)]


# --------------------------------------------------------------------------


# Fire OS 6 families: paired opening/closing edges, variable-width listening
# spotlight, neighbouring-hue shimmer, and the live-view soft glow. Numbered
# states and colour variants remain parameters rather than separate effects.
def fx_split_wipe(c, ms=32, steps=24, reverse=False, **_):
    frames = []
    for i in range(steps + 1):
        radius = (SEG / 2 + 1) * i / steps
        px = []
        for s in range(SEG):
            distance = min(s, SEG - s)
            px.append(_scale(c, max(0.0, min(1.0, radius - distance))))
        frames.append((ms, px))
    return list(reversed(frames)) if reverse else frames


def fx_split_drain(c, **kw):
    return fx_split_wipe(c, reverse=True, **kw)


def fx_spotlight(c, ms=40, steps=40, width=5, **_):
    """The 40 ca-active-start steps are widths of one fixed listening arc."""
    frames = []
    for i in range(steps):
        radius = 0.5 + width * (1 - math.cos(2 * math.pi * i / steps)) / 2
        frames.append((ms, [_scale(c, 0.2 + 0.8 * max(0.0, min(1.0,
                           radius - min(s, SEG - s)))) for s in range(SEG)]))
    return frames


def fx_colour_shimmer(c, ms=100, count=24, spread=1/6, **_):
    """Deep-thinking changes hue on a lit ring, rather than blinking pixels."""
    h, saturation, value = colorsys.rgb_to_hsv(*(v / 255 for v in c))
    rnd = random.Random(23)
    return [(ms, [tuple(v * 255 for v in colorsys.hsv_to_rgb(
             (h + rnd.uniform(-spread / 2, spread / 2)) % 1, saturation, value))
             for s in range(SEG)]) for i in range(count)]


def fx_soft_orbit(c, ms=32, steps=96, width=1.4, **_):
    """Live-view's diffuse orbit: fractional motion with a rounded glow."""
    frames = []
    for i in range(steps):
        head = SEG * i / steps
        distances = [abs((s - head + SEG / 2) % SEG - SEG / 2)
                     for s in range(SEG)]
        frames.append((ms, [_scale(c, math.exp(-0.5 * (d / width) ** 2))
                            for d in distances]))
    return frames


def fx_soft_bloom(c, ms=24, steps=32, **_):
    """Live-view off's local glow grows across the ring and fades away."""
    frames = []
    for i in range(steps + 1):
        t = i / steps
        level = math.sin(math.pi * t) ** 2
        width = 0.5 + 5 * t
        frames.append((ms, [_scale(c, level * math.exp(
            -0.5 * (min(s, SEG - s) / width) ** 2)) for s in range(SEG)]))
    frames[-1] = (ms, [(0, 0, 0)] * SEG)
    return frames


def fx_volume_ramp(c, level=20, steps=30, **_):
    """Preview frame for the volume ramp - two thirds filled.

    Registered so the settings page can offer it, and so volume_changed has a
    default that previews as what it actually is. The LIVE ramp is not rendered
    from here: generate_volume_ramp() writes all 31 levels, because an effect
    only ever sees a colour and this needs the level too.
    """
    return volume_frames(level, steps, c, (0, 0, 0))


# --------------------------------------------------------------------------
# Ambient looks in the style of stock's zzz_ set.
#
# Amazon ships fifteen zzz_ animations that Fire OS 5 never plays and Fire OS 6
# wires only three of, to events of unknown trigger. These are procedural
# generators: their timings, 4-bit colours and densities were measured from the
# decoded stock files, and no frame data is embedded. Ten of the originals play
# once; every one of these loops, and seamlessly: events are written modulo the
# loop period, so the last frame leads into the first like any other pair.
#
# Several are mirror images about the line through the top of the ring, between
# LEDs 5 and 6, and the bottom, between 11 and 0: LED i pairs with LED 11 - i.
# That is NOT the axis of Split Wipe and Spotlight, which measure from LED 0.
# TOP_RANK is the distance from the top on this axis.
#
# Stock's levels are 4-bit, so its faintest is FAINT, 17/255. The ring scales
# every channel by its brightness ceiling, which rounds that to nothing below a
# ceiling of 15. Where such a level is a steady part of the look - Twin Comets'
# glow, Pulse's trough, Turbo Boost's pad - it is a colour of its own rather
# than a fraction of the main one, as Breathe's floor is, so a palette can
# raise it.
# --------------------------------------------------------------------------

TOP_RANK = [int(abs(s - 5.5)) for s in range(SEG)]      # 5 4 3 2 1 0 0 1 2 3 4 5
FAINT = 1 / 15


def _hsv(h, s, v):
    return tuple(x * 255 for x in colorsys.hsv_to_rgb(h % 1.0, s, v))


def _add(a, b):
    return tuple(min(255, x + y) for x, y in zip(a, b))


def _cyclic_starts(rnd, period, lo, hi):
    """Random event times whose gaps all lie in lo..hi, including the gap
    across the loop seam, so the loop point cannot be seen as a lull or a
    burst.

    Some ranges cannot fill the period (lo == hi == 4 in 10), and a narrow one
    may take many draws, so the search is bounded: after that the events are
    spaced evenly, as near the middle of the range as the period allows."""
    if period < 1:
        return []
    lo = max(1, min(lo, period))
    hi = max(lo, min(hi, period))
    for _attempt in range(1000):
        times = [0]
        while period - times[-1] > hi:
            times.append(times[-1] + rnd.randint(lo, hi))
        if period - times[-1] >= lo:
            offset = rnd.randrange(period)
            return sorted((t + offset) % period for t in times)
    k = max(1, round(period / ((lo + hi) / 2)))
    offset = rnd.randrange(period)
    return sorted((i * period // k + offset) % period for i in range(k))


def fx_twin_comets(c, ms=66, heads=2, tail=3, glow=FAINT, **_):
    """Stock `zzz_comets`: two comets half a ring apart, each with a long tail,
    chasing round a ring that keeps a faint glow of their colour.

    One frame per LED of spacing: after it the pattern has turned onto itself.
    """
    frames, spacing = [], SEG // heads
    for i in range(spacing):
        px = [_scale(c, glow)] * SEG
        for h in range(heads):
            head = (i + h * spacing) % SEG
            for t in range(tail + 1):
                f = 1.0 if t == 0 else 0.8 * (tail + 1 - t) / (tail + 1)
                px[(head - t) % SEG] = _scale(c, max(f, glow))
        frames.append((ms, px))
    return frames


def fx_shooting_stars(c, ms=66, stars=4, run=7, seed=5, **_):
    """Stock `zzz_shooting-stars`: one meteor at a time. It appears, streaks
    `run` LEDs with its tail growing in behind it, then burns out as a single
    ember that fades while creeping on at half speed. Directions alternate.
    """
    rnd = random.Random(seed)
    # Even, so the directions alternate across the seam, and at least two.
    stars = max(2, stars + stars % 2)
    # Each star starts 1-4 LEDs on from where the last burnt out. The last gap
    # is the one that carries the ring back to the first star's start. Gaps of
    # 1 always close (the alternating sum of an odd count of 1s is 1), so they
    # end a search that has been unlucky for too long.
    for _attempt in range(1000):
        gaps = [rnd.randint(1, 4) for _ in range(stars - 1)]
        last = sum(g if n % 2 == 0 else -g for n, g in enumerate(gaps)) % SEG
        if 1 <= last <= 4:
            gaps.append(last)
            break
    else:
        gaps = [1] * stars
    frames, start = [], rnd.randrange(SEG)
    for n in range(stars):
        d = 1 if n % 2 == 0 else -1
        trail = []                                  # LEDs visited, newest last
        for k in range(run + 1):
            head = (start + d * max(0, k - 1)) % SEG   # holds a frame on appearing
            if not trail or trail[-1] != head:
                trail.append(head)
            px = [(0, 0, 0)] * SEG
            for t, f in enumerate((1.0, 0.6, 0.2)):
                if t < len(trail):
                    px[trail[-1 - t]] = _scale(c, f)
            frames.append((ms, px))
        for k, f in enumerate((5 / 15, 4 / 15, 3 / 15, 2 / 15, FAINT)):
            px = [(0, 0, 0)] * SEG
            px[(trail[-1] + d * (1 + k // 2)) % SEG] = _scale(c, f)
            frames.append((ms, px))
        start = (trail[-1] + d * (3 + gaps[n])) % SEG
    return frames


def fx_fireflies(c, spark=None, ms=50, period=200, gap=(6, 14), drift=0.4,
                 lift=0.0, seed=3, **_):
    """Stock `zzz_fireflies`: soft glows swelling and fading at random LEDs,
    some wandering to a neighbour as they peak. The brightest instant flares in
    the spark colour - by default the glow a little whitened.

    `lift` raises every glow towards full, and past full it warms towards the
    spark: that is Blue Fireflies.
    """
    rnd = random.Random(seed)
    spark = spark or _mix(c, (255, 255, 255), FAINT)
    level = [[0.0] * SEG for _ in range(period)]

    def glow(led, start):
        rise, fall = rnd.randint(8, 16), rnd.randint(8, 26)
        for k in range(rise + fall):
            v = (k + 1) / rise if k < rise else 1 - (k - rise + 1) / (fall + 1)
            row = level[(start + k) % period]
            row[led] = max(row[led], v)
        return rise

    # A new glow every 6-14 frames, so they never bunch up or leave the ring
    # empty for long.
    for start in _cyclic_starts(rnd, period, *gap):
        led = rnd.randrange(SEG)
        peak = glow(led, start)
        if rnd.random() < drift:
            glow((led + rnd.choice((-1, 1))) % SEG, start + peak - 2)

    def shade(v):
        if v <= 0.04:
            return (0, 0, 0)
        if v > 0.97:
            return spark
        if v + lift <= 1:
            return _scale(c, v + lift)
        return _mix(c, spark, 0.4 * (v + lift - 1) / lift)

    return [(ms, [shade(v) for v in row]) for row in level]


def fx_blue_fireflies(c=(0, 136, 255), spark=(221, 255, 102), ms=66, lift=0.27, **kw):
    """Stock `zzz_blue-fireflies`: the same fireflies, slower, lifted into a
    glowier azure, flashing yellow-green at each peak."""
    return fx_fireflies(c, spark, ms=ms, lift=lift, **kw)


def fx_disco(_c=None, ms=66, period=100, rate=0.86, fade=0.1, seed=3, **_):
    """Stock `zzz_disco`: random LEDs pop on in random, often pastel colours,
    then fade by losing the same amount from every channel - so each one
    saturates towards its strongest primary as it dies.

    Rendered twice round and the second kept: a pop fades within ten frames,
    so by then every frame carries the fades from the end of the loop.
    """
    rnd = random.Random(seed)
    pops = []
    for _ in range(period):
        n = int(rate) + (rnd.random() < rate - int(rate))
        pops.append([(rnd.randrange(SEG), _hsv(rnd.random(), rnd.uniform(0.35, 1.0),
                                               rnd.uniform(0.55, 0.95))) for _ in range(n)])
    px, frames = [(0, 0, 0)] * SEG, []
    for i in range(2 * period):
        px = [tuple(max(0.0, v - 255 * fade) for v in p) for p in px]
        for led, colour in pops[i % period]:
            px[led] = colour
        if i >= period:
            frames.append((ms, px))
    return frames


def fx_firelight(c=(255, 17, 0), c2=(255, 51, 0), ms=66, count=30, dip=0.93,
                 seed=13, **_):
    """Stock `zzz_fire`: an almost steady red-orange ring with a fine grain,
    each LED independently taking one of three close hues and now and then a
    slight dip. Far gentler than Fire."""
    rnd = random.Random(seed)
    return [(ms, [_scale(_mix(c, c2, rnd.choice((0, 0.5, 1))), rnd.choice((dip, 1, 1, 1)))
                  for _s in range(SEG)]) for _ in range(count)]


def fx_jellyfish(c, ms=66, hold=1000, rest=1000, **_):
    """Stock `zzz_jellyfish`: six alternate LEDs swell and hold, then jiggle
    between the two interleaved sets while they dim, and the ring rests."""
    sets = [[c if (s + k) % 2 == 0 else (0, 0, 0) for s in range(SEG)] for k in (0, 1)]
    frames = [(ms, [_scale(p, f) for p in sets[0]]) for f in (0.2, 0.4, 0.6, 0.8)]
    frames.append((hold, sets[0]))
    for k, f in enumerate((1.0, 0.8, 0.6, 0.4, 0.2)):
        frames += [(ms, [_scale(p, f) for p in sets[(k + 1) % 2]])] * 2
    frames.append((rest, [(0, 0, 0)] * SEG))
    return frames


def fx_lava_flow(hot=(255, 119, 0), molten=(255, 0, 0), crust=(255, 0, 0), ms=66,
                 period=38, gap=(4, 9), seed=17, **_):
    """Stock `zzz_lava-flow`: molten, hottest at the top and red at the bottom,
    with dark crust welling up at the top and flowing down either side,
    reheating as it goes."""
    rnd = random.Random(seed)
    base = [_mix(molten, hot, 0.5 + 0.5 * math.cos(math.pi * min(r, 4) / 4))
            for r in TOP_RANK]
    frames = [list(base) for _ in range(period)]
    for side in (1, -1):
        for t in _cyclic_starts(rnd, period, *gap):
            for k, f in enumerate((2, 2, 3, 4, 5, 6)):
                s = 6 + k if side == 1 else 5 - k
                frames[(t + k) % period][s] = _scale(crust, f / 15)
    return [(ms, px) for px in frames]


def fx_pulse(c, low=None, ms=66, up=14, hold=4, **_):
    """Stock `zzz_magenta-pulse`: the whole ring throbbing on a straight ramp,
    not Breathe's cosine, with a short flat top."""
    low = low or _scale(c, FAINT)
    ramp = [_mix(low, c, i / up) for i in range(up + 1)]
    return [(ms, [p] * SEG) for p in ramp + [c] * (hold - 1) + ramp[-2:0:-1]]


def fx_crossing_comets(c=(0, 0, 255), c2=(0, 255, 0), ms=66, tail=(1.0, 0.6, 0.2), **_):
    """Stock `zzz_overlappers`: two comets leave the bottom in opposite
    directions, pass through each other at the top and meet again at the
    bottom. Where they overlap they add, so each crossing flashes a blend."""
    frames = []
    for i in range(SEG):
        px = [(0, 0, 0)] * SEG
        for t, f in enumerate(tail):
            if i - t >= 0:                      # tails never cross the bottom
                px[i - t] = _add(px[i - t], _scale(c, f))
                px[SEG - 1 - i + t] = _add(px[SEG - 1 - i + t], _scale(c2, f))
        frames.append((ms, px))
    return frames


def fx_paparazzi(flash=(255, 255, 255), glow=(0, 255, 255), ms=66, period=32, lull=2,
                 pair=0.04, seed=21, **_):
    """Stock `zzz_paparazzi`: camera flashes popping round the ring, one a frame,
    each leaving a short tinted after-image, then a brief lull.

    Each flash lands 0-6 LEDs from the last, evenly spread, so now and then the
    same LED fires twice running, like a pre-flash.
    """
    rnd = random.Random(seed)
    cells = [[(0.0, (0, 0, 0))] * SEG for _ in range(period)]

    def put(t, led, level, colour):
        row = cells[t % period]
        if level > row[led][0]:
            row[led] = (level, colour)

    led = rnd.randrange(SEG)
    for t in range(period - lull):
        for _n in range(1 + (rnd.random() < pair)):
            led = (led + rnd.choice((-1, 1)) * rnd.randint(0, 6)) % SEG
            put(t, led, 1.0, flash)
            for k in (1, 2, 3):
                put(t + k, led, (4 - k) / 15, _scale(glow, (4 - k) / 15))
    return [(ms, [colour for _level, colour in row]) for row in cells]


def fx_rainbow_swirl(_c=None, ms=66, steps=30, **_):
    """Stock `zzz_rainbow`: the whole hue wheel round the ring, turning smoothly
    towards lower LED numbers once every `steps` frames - slower and finer than
    Rainbow Spin's whole-LED steps."""
    return [(ms, [_hsv(s / SEG + i / steps, 1, 1) for s in range(SEG)])
            for i in range(steps)]


def fx_tortoise_and_hare(c=(0, 255, 34), c2=(255, 0, 0), ms=66, **_):
    """Stock `zzz_tortoise-hare`: two dots leave the bottom together. The hare
    runs twice as fast, laps the tortoise, and they cross the line together;
    where they share an LED the colours add."""
    frames = []
    for i in range(2 * SEG):
        px = [(0, 0, 0)] * SEG
        px[i // 2] = _add(px[i // 2], c)
        px[i % SEG] = _add(px[i % SEG], c2)
        frames.append((ms, px))
    return frames


def fx_turbo_boost(c=(255, 0, 0), pad=(0, 17, 17), ms=132, pad_at=5, **_):
    """Stock `zzz_turbo-boost`: a dot creeps up one side to a faint boost pad
    at the top, then shoots down the other side with a motion-blur tail,
    slowing back to cruising speed as it reaches the bottom."""
    boost = [ms // 6, ms // 4, ms // 3, ms * 5 // 12, ms // 2, ms // 2]
    frames = []
    for head in range(SEG):
        px = [(0, 0, 0)] * SEG
        px[pad_at] = pad
        for t, f in ((1, 0.6), (2, 0.2)):
            if head == 0 or head - t > pad_at:         # blurred only once boosted
                px[(head - t) % SEG] = _scale(c, f)
        px[head] = c
        if head < pad_at:
            dur = ms
        elif head == pad_at:
            dur = ms // 2
        else:
            dur = boost[min(head - pad_at - 1, len(boost) - 1)]
        frames.append((dur, px))
    return frames


def fx_wave(crest=(0, 136, 255), swell=(0, 34, 255), base=(0, 0, 153), rest=2000,
            ease=(60, 70, 80, 90, 100, 200), **_):
    """Stock `zzz_wave`: after a rest, a bright swell pours from the top down
    both sides, easing out, holds, then drains from the top, easing in. A paler
    crest rides three LEDs behind its leading edge."""
    frames = [(rest, [base] * SEG)]
    n = len(ease)
    for k in range(1, 2 * n + 1):
        front, back = min(k, n), max(0, k - n - 1)
        px = [_mix(swell, crest, 1 - abs(r - (k - 3)) / 3) if back <= r < front else base
              for r in TOP_RANK]
        dur = ease[k - 1] if k <= n else ease[-1] if k == n + 1 else ease[2 * n - k]
        frames.append((dur, px))
    return frames


EFFECTS = {
    "Volume Ramp":             (fx_volume_ramp,      True),
    "Split Wipe":              (fx_split_wipe,       True),
    "Split Drain":             (fx_split_drain,      True),
    "Spotlight Breathe":       (fx_spotlight,        True),
    "Colour Shimmer":          (fx_colour_shimmer,   True),
    "Soft Orbit":              (fx_soft_orbit,       True),
    "Soft Bloom":              (fx_soft_bloom,       True),
    # name                     (function,            colourable)
    "Solid":                   (fx_solid,            True),
    "Arc Spin":                (fx_arc_spin,         True),
    "Arc Spin Inverse":        (fx_arc_spin_inverse, True),
    "Dot Orbit":               (fx_dot_orbit,        True),
    "Comet":                   (fx_comet,            True),
    "Theater Chase":           (fx_theater_chase,    True),
    "Highlight Orbit":         (fx_highlight_orbit,  True),
    "Mirror Scan":             (fx_mirror_scan,      True),
    "Twinkle":                 (fx_twinkle,          True),
    "Breathe":                 (fx_breathe,          True),
    "Blink":                   (fx_blink,            True),
    "Wipe In":                 (fx_wipe_in,          True),
    "Drain Out":               (fx_drain_out,        True),
    "Larson Scanner":          (fx_larson,           True),
    "Running Lights":          (fx_running,          True),
    "Heartbeat":               (fx_heartbeat,        True),
    "Rainbow":                 (fx_rainbow,          False),
    "Rainbow Spin":            (fx_rainbow_spin,     False),
    "Colour Cycle":            (fx_colour_cycle,     False),
    "Fire":                    (fx_fire,             False),
    "Candle":                  (fx_candle,           False),
    "Police":                  (fx_police,           False),
    # The zzz_ ambient looks. Colourable ones take the Home Assistant colour
    # as their main colour; the others keep their own until given a palette.
    "Twin Comets":             (fx_twin_comets,      True),
    "Shooting Stars":          (fx_shooting_stars,   True),
    "Fireflies":               (fx_fireflies,        True),
    "Blue Fireflies":          (fx_blue_fireflies,   False),
    "Disco":                   (fx_disco,            False),
    "Firelight":               (fx_firelight,        False),
    "Jellyfish":               (fx_jellyfish,        True),
    "Lava Flow":               (fx_lava_flow,        False),
    "Pulse":                   (fx_pulse,            True),
    "Crossing Comets":         (fx_crossing_comets,  False),
    "Paparazzi":               (fx_paparazzi,        False),
    "Rainbow Swirl":           (fx_rainbow_swirl,    False),
    "Tortoise and Hare":       (fx_tortoise_and_hare, False),
    "Turbo Boost":             (fx_turbo_boost,      False),
    "Wave":                    (fx_wave,             False),
}

# Direction is applied by the renderer at playback time, including act_* files.
def fx_pointer(colour=(0,255,255), **kw):
    return [(100,[colour]*SEG)]
EFFECTS.update({'Point at speaker':(fx_pointer,True), 'Point at noise':(fx_pointer,True)})

# Effects that play once and retire rather than looping forever.
ONESHOT = {"Wipe In", "Drain Out", "Split Wipe", "Split Drain", "Soft Bloom"}

# Effects that mean something for one activity only. Volume Ramp previews the
# volume level, so pickers for every other activity and the Home Assistant
# effect list leave these out. biscuit-settings.py and biscuit-va-leds.py read
# fx.VOLUME_ONLY directly, with no fallback, so it is required.
VOLUME_ONLY = frozenset({"Volume Ramp"})


# Explicit colour roles. Old one-colour specifications keep their exact output;
# a palette opts into independent colours without adding more effect names.
COLOUR_ROLES = {name: ['Colour', 'Background'] for name, (_, colourable) in EFFECTS.items() if colourable}
COLOUR_ROLES.update({
    'Point at speaker':['Speaker','Background'], 'Point at noise':['Noise','Background'],
    'Solid': ['Colour'], 'Highlight Orbit': ['Highlight', 'Base'],
    'Twinkle': ['Sparkle', 'Base'], 'Mirror Scan': ['Left', 'Right', 'Background'],
    'Spotlight Breathe': ['Spotlight', 'Base'],
    'Split Wipe': ['Fill', 'Edge', 'Background'],
    'Split Drain': ['Fill', 'Edge', 'Background'],
    'Colour Shimmer': ['Colour 1', 'Colour 2', 'Colour 3'],
    'Colour Cycle': ['Colour 1', 'Colour 2', 'Colour 3'],
    'Fire': ['Ember', 'Flame', 'Highlight'], 'Candle': ['Flame', 'Glow'],
    'Police': ['Left', 'Right'], 'Rainbow': [], 'Rainbow Spin': [],
    'Twin Comets': ['Colour', 'Glow'], 'Shooting Stars': ['Star', 'Background'],
    'Fireflies': ['Glow', 'Spark'], 'Blue Fireflies': ['Glow', 'Spark'],
    'Disco': [], 'Firelight': ['Flame', 'Flicker'],
    'Lava Flow': ['Hot', 'Molten', 'Crust'], 'Pulse': ['Colour', 'Background'],
    'Crossing Comets': ['First', 'Second'], 'Paparazzi': ['Flash', 'Afterglow'],
    'Rainbow Swirl': [], 'Tortoise and Hare': ['Tortoise', 'Hare'],
    'Turbo Boost': ['Car', 'Boost pad'], 'Wave': ['Crest', 'Swell', 'Base'],
})
FLOORS = {'Highlight Orbit': .25, 'Twinkle': .35, 'Breathe': .06,
          'Running Lights': .15, 'Heartbeat': .08, 'Spotlight Breathe': .2,
          'Twin Comets': FAINT, 'Pulse': FAINT}

# The ambient looks' own colours, matching stock's. Colourable ones follow a
# primary colour when one is given; the others keep these, as Police does.
AMBIENT_COLOURS = {
    'Twin Comets': [[0,255,255], [0,17,17]], 'Shooting Stars': [[0,255,255], [0,0,0]],
    'Fireflies': [[0,255,0], [17,255,17]], 'Blue Fireflies': [[0,136,255], [221,255,102]],
    'Disco': [], 'Firelight': [[255,17,0], [255,51,0]],
    'Jellyfish': [[255,255,255], [0,0,0]],
    'Lava Flow': [[255,119,0], [255,0,0], [255,0,0]],
    'Pulse': [[255,17,68], [17,17,17]], 'Crossing Comets': [[0,0,255], [0,255,0]],
    'Paparazzi': [[255,255,255], [0,255,255]], 'Rainbow Swirl': [],
    'Tortoise and Hare': [[0,255,34], [255,0,0]], 'Turbo Boost': [[255,0,0], [0,17,17]],
    'Wave': [[0,136,255], [0,34,255], [0,0,153]],
}

def default_colours(name, primary=None):
    if name in AMBIENT_COLOURS and (primary is None or not EFFECTS[name][1]):
        return [list(c) for c in AMBIENT_COLOURS[name]]
    primary = list((51, 153, 255) if primary is None else primary)
    if name == 'Fireflies':
        return [primary, [round(x) for x in _mix(primary, (255, 255, 255), FAINT)]]
    if name in ('Point at speaker','Point at noise'): return [[0,255,255],[0,0,255]]
    if name in ('Rainbow', 'Rainbow Spin'):
        return []
    if name == 'Police':
        return [[255,0,0], [0,0,255]]
    if name == 'Fire':
        return [[120,0,0], [255,80,0], [255,230,120]]
    if name == 'Candle':
        return [[255,180,60], [100,20,0]]
    if name == 'Colour Cycle':
        return [[255,0,0], [0,255,0], [0,0,255]]
    if name == 'Colour Shimmer':
        h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in primary))
        return [[round(x * 255) for x in colorsys.hsv_to_rgb((h + shift) % 1, s, v)]
                for shift in (-1/12, 0, 1/12)]
    if name in ('Split Wipe', 'Split Drain', 'Mirror Scan'):
        return [primary, primary[:], [0,0,0]]
    if name == 'Solid':
        return [primary]
    return [primary, [round(x * FLOORS.get(name, 0)) for x in primary]]

def _mix(a, b, weight):
    weight = max(0, min(1, weight))
    return tuple(x * (1 - weight) + y * weight for x, y in zip(a, b))

def _palette_sample(colours, position, cyclic=False):
    span = len(colours) if cyclic else len(colours) - 1
    pos = (position % 1 if cyclic else max(0, min(1, position))) * span
    index = min(int(pos), len(colours) - 1)
    return _mix(colours[index], colours[(index + 1) % len(colours)], pos - index)

def palette_frames(name, colours, **kw):
    if len(colours) != len(COLOUR_ROLES[name]) or any(len(c) != 3 for c in colours):
        raise ValueError('wrong number of colours for ' + name)
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 255
           for c in colours for v in c):
        raise ValueError('invalid RGB palette')
    colours = [tuple(c) for c in colours]
    if not colours:
        return EFFECTS[name][0](**kw)
    if name in ('Point at speaker','Point at noise'):
        return [(100, [colours[0]]*SEG)]
    if name == 'Police':
        return fx_police(colours[0], colours[1], **kw)
    if name in ('Colour Shimmer', 'Colour Cycle', 'Fire', 'Candle'):
        rnd = random.Random(23)
        frames = []
        for i in range(36):
            if name == 'Colour Cycle':
                px = [_palette_sample(colours, i / 36, True)] * SEG
            elif name == 'Colour Shimmer':
                px = [_palette_sample(colours, rnd.random()) for _ in range(SEG)]
            elif name == 'Fire':
                px = [_scale(_palette_sample(colours, heat), heat)
                      for heat in [rnd.uniform(.35, 1) for _ in range(SEG)]]
            else:
                base = rnd.uniform(.55, 1)
                px = [_mix(colours[1], colours[0], min(1, base * rnd.uniform(.9, 1.05))) for _ in range(SEG)]
            frames.append((kw.get('ms', 100), px))
        return frames
    if name in ('Split Wipe', 'Split Drain'):
        frames = []
        steps = kw.get('steps', 24)
        for i in range(steps + 1):
            radius = (SEG / 2 + 1) * i / steps
            px = []
            for s in range(SEG):
                distance = min(s, SEG - s)
                coverage = max(0, min(1, radius - distance))
                edge = max(0, 1 - abs(radius - distance - 1)) if i < steps else 0
                px.append(_mix(colours[2], _mix(colours[0], colours[1], edge), coverage))
            frames.append((kw.get('ms', 32), px))
        return list(reversed(frames)) if name == 'Split Drain' else frames
    if name == 'Mirror Scan':
        frames = []
        for ms, pixels in fx_mirror_scan((255,255,255), **kw):
            lit = [i for i, pixel in enumerate(pixels) if any(pixel)]
            px = [colours[2]] * SEG
            for i, segment in enumerate(lit):
                px[segment] = colours[i % 2]
            frames.append((ms, px))
        return frames
    if name in ('Fireflies', 'Blue Fireflies', 'Firelight', 'Lava Flow',
                'Crossing Comets', 'Paparazzi', 'Tortoise and Hare', 'Turbo Boost',
                'Wave'):
        # Their roles are their arguments, in order. The generic path below
        # would flatten a second hue into a shade of the first.
        return EFFECTS[name][0](*colours, **kw)
    if name == 'Solid':
        return fx_solid(colours[0], **kw)
    floor = FLOORS.get(name, 0)
    frames = EFFECTS[name][0]((255,255,255), **kw)
    return [(ms, [_mix(colours[1], colours[0], (p[0] / 255 - floor) / (1 - floor))
                  for p in pixels]) for ms, pixels in frames]


def slug(name):
    return "fx_" + name.lower().replace(" ", "_")


# --------------------------------------------------------------------------
# The volume ramp.
#
# Not an EFFECTS entry, and deliberately so: every effect renders a whole
# animation from a colour alone, while this also needs the LEVEL. biscuit-audio
# selects the result BY NAME - volume_step-NN, chosen from the level it just
# applied - rather than by activity, and biscuit-ring-priorities.json already
# orders those names at layer 4. So the names are fixed and only the colours
# come from the volume_changed activity.
#
# Stock shipped 30 of these as artwork. They are a filled arc and nothing more,
# so pmOS generates them instead of redistributing them.
# --------------------------------------------------------------------------

def volume_frames(level, steps=30, colour=(255, 255, 255), background=(0, 0, 0)):
    """One frame showing `level` of `steps` as a filled arc.

    The fill is fractional, so the partly-lit boundary segment is what
    distinguishes 30 levels on 12 LEDs; rounding to whole segments would
    collapse them into 12 indistinguishable steps.

    Stock's arc begins at the LAST LED, index 11, whose edge is the bottom of
    the ring, and then runs 0, 1, 2 ... 10: its level 1 lights index 11 alone
    and levels 3-5 fill index 0. Filling from index 0 instead started every
    level one LED round from the bottom, so the arc ended at about half past
    six instead of six.
    """
    filled = max(0.0, min(1.0, level / float(steps))) * SEG
    order = [SEG - 1] + list(range(SEG - 1))
    pixels = [tuple(background)] * SEG
    for rank, i in enumerate(order):
        part = max(0.0, min(1.0, filled - rank))
        pixels[i] = tuple(round(b + (c - b) * part)
                          for c, b in zip(colour, background))
    return [(2000, pixels)]


def generate_volume_ramp(colours=None, steps=30, out_dir=FX_DIR):
    """Write volume_step-01..NN and volume-muted. Returns the filenames."""
    palette = [tuple(c) for c in (colours or [])]
    colour = palette[0] if palette else (255, 255, 255)
    background = palette[1] if len(palette) > 1 else (0, 0, 0)
    os.makedirs(out_dir, exist_ok=True)
    written = []
    ramp = [("volume_step-%02d" % n, volume_frames(n, steps, colour, background))
            for n in range(1, steps + 1)]
    # Volume zero. A slow pulse rather than a dark ring, so "muted" is visibly
    # different from "the ring happened to be off".
    ramp.append(("volume-muted",
                 [(500, [_scale(colour, 0.25)] * SEG), (500, [background] * SEG)]))
    for name, frames in ramp:
        path = os.path.join(out_dir, name + ".animation")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(_frames_to_text(frames, loop=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        written.append(name + ".animation")
    return written


def generate_blank(out_dir=FX_DIR, name="off"):
    """A single dark frame that plays once and retires. "Off", for one activity.

    The animation IS played: it finds nothing to show and gives its layer back,
    so whatever is underneath keeps the ring rather than the activity blanking
    it. That is why this is a real file and not an empty LED_<activity> - the
    shell consumers expand ${LED_x:-act_x}, and an empty value falls back to
    the default instead of meaning "off".

    Not an EFFECTS entry: it takes no colour, and listing it there would put an
    "Off (fixed colours)" row in every effect menu on the settings page.
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ".animation")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(_frames_to_text([(100, [(0, 0, 0)] * SEG)], loop=False))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return name


def generate(name, colour=(255, 255, 255), out_dir=FX_DIR, **kw):
    """Write one effect at one colour and return the name to `play`."""
    fn, colourable = EFFECTS[name]
    colours = kw.pop("colours", None)
    frames = (palette_frames(name, colours, **kw) if colours is not None else
              (fn(colour, **kw) if colourable else fn(**kw)))
    os.makedirs(out_dir, exist_ok=True)
    anim = slug(name)
    path = os.path.join(out_dir, anim + ".animation")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        if name in ('Point at speaker','Point at noise'):
            palette = colours or default_colours(name, colour)
            import json
            f.write('# direction '+json.dumps({'kind':'speaker' if name=='Point at speaker' else 'noise', 'colours':palette})+'\n')
        f.write(_frames_to_text(frames, loop=name not in ONESHOT))
        # Durable as well as atomic. FX_DIR is tmpfs, where this costs nothing,
        # but biscuit-settings generates into a temp dir and then renames the
        # result onto /opt/persist/led-fx - so the bytes have to be on disk
        # before that rename, or a power cut leaves a named but empty effect.
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)          # atomic: never let the player read a partial file
    return anim


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "./fx"
    print("%-20s %-6s %s" % ("effect", "frames", "colourable"))
    for n in EFFECTS:
        a = generate(n, (255, 40, 0), out_dir=out)
        lines = open(os.path.join(out, a + ".animation")).read().splitlines()
        print("  %-20s %-6d %s" % (n, len([l for l in lines if ":" in l]),
                                   "yes" if EFFECTS[n][1] else "no (intrinsic)"))
