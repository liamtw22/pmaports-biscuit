#!/usr/bin/env python3
"""Ambient-light auto-dimming for the Echo Dot 2 (biscuit) light ring.

Stock dims the ring to match the room. This does the same, using stock's own
sensor configuration and stock's own lux equation, both recovered from the
vendor kernel (drivers/misc/tsl2583.c in the 3.18 tree).

Provenance: this is our own code. It reimplements stock's observed behaviour;
the numeric parameters below (sensor settings, the response-curve
coefficients and the brightness ladder) were measured from, or read out of,
the stock software on the owner's own device. No stock code is included.

What is taken from stock, exactly
---------------------------------
The vendor driver initialises the part we actually have - a TSL2584TSV at i2c
0x29 - like this:

    chip->taos_settings.als_time      = 688;   /* max integration */
    chip->taos_settings.als_gain      = 3;     /* MAX gain */
    chip->taos_settings.als_gain_trim = 1000;

and computes lux with an equation specific to the 2584TSV, which is *not* the
table-driven one mainline's tsl2583 driver uses:

    lux_millilux = COFF_1 * (MILILUX_COFF * ch0 - CH1_COFF * ch1)
                   / (gain * als_time)

with COFF_1 = 105, MILILUX_COFF = 1000, CH1_COFF = 1190 (biscuit is not
CONFIG_sbc123, so it takes the #else branch), gain = 111 at index 3, and a
noise floor of ch0 + ch1 <= 6 meaning zero. All of that is reproduced below.

That equation's output is NOT lux, despite the variable being called that. It
is an uncalibrated raw scale, and stock converts it with a per-unit factory
coefficient held in IDME at /idme/alscal:

    " ams_0_0=0,0.000,0 ams_400_0=220,246.000,34"

taos_calibrated_lux_show() parses the second group as ams_400_0=%d,%d and takes
the second field - 246 on Device #3 - as the raw value this particular unit
produced under 400 lux at the factory. Then

    calibrated_lux = ALS_MAX_LUX * raw / coeff,  clamped to [0, 400]

which is what the engine consumes. The coefficient is per unit, so it is read
at startup rather than hardcoded; without it the scale is meaningless. On
Device #3, 246 raw units correspond to 400 lux, so one raw unit is about
1.6 lux.

Why the mainline driver's own lux output is not used: it is bound as `tsl2583`
and applies the 2583's coefficient table, which is for a different part. Its
raw channels are correct, so this reads those and does stock's arithmetic.

The response curve, also stock
------------------------------
Recovered by disassembling /system/bin/ledcontroller and libled_hal.so pulled
from Device #3. Stock's auto-dim is amazon::AmbientLightEngine, constructed as
AmbientLightEngineConfig(3, 20, 100, 0, 0), which the setter order in the
constructor identifies as (responseTime=3, sampleRate=20, brightnessCap=100,
luxCapMin=0, luxCapMax=0). Zero for either lux cap means "use the sensor's own
bounds", and IssiAmbientLightSensor reports minLux 0.0 and maxLux 400.0.

AmbientLightEngine::responseCurve(double) evaluates a cubic by Horner's method
over the normalised lux, with coefficients {0, 915, 252, -168} and a divisor of
1000:

    x        = (lux - 0) / (400 - 0)
    response = (915x + 252x^2 - 168x^3) / 1000        response(1.0) = 0.999

getLedGain() multiplies that by brightnessCap/100 and clamps to [0.005, 1.0].
IssiLedDevice::setBrightness() then clamps the gain-as-percent to 0..100 and
uses it to index a 101-entry ladder, each entry a (duty-cycle multiplier,
led_current) pair, where led_current is a divider: 0 = IMAX, 3 = IMAX/4. So the
real light output of a step is mult / (current + 1), which runs monotonically
from 0.0323 to 0.8750. STOCK_LADDER below holds those 101 measured values,
so the steps match stock's.

One deviation, and one thing we cannot reproduce
------------------------------------------------
1. Integration time is 650 ms, not stock's 688 ms. Mainline's
   tsl2583_write_raw() clamps to 50..650 in steps of 50, so 688 cannot be set
   through sysfs - and neither can stock's own driver, whose
   integration_time_available also stops at 650; 688 is its compiled default.
   This does not change the computed lux: the equation divides by als_time, so
   it is normalised out. It costs about 5% of the signal.

2. Stock steps the chip's global current as well as the PWM duty cycle, which
   is how it gets a 27x range out of a ladder whose multipliers only span 7x.
   Mainline's is31fl32xx driver exposes no current control at all - only
   per-channel brightness - so the four current bands have to be collapsed into
   PWM alone. RING_LADDER_PWM below does that by using the effective output,
   mult / (current + 1), which preserves stock's curve shape and its 0.875
   ceiling exactly, but not its per-band current stepping. The visible
   difference should be resolution at the dim end, where stock has real current
   headroom and we only have 8 bits of PWM.

Ring self-illumination
----------------------
The ring lights its own sensor. Measured at gain 111 / 650 ms with all 36
channels driven:

    off        ch0=0    ch1=0
    64/255     ch0=9    ch1=2
    255/255    ch0=38   ch1=6

which is linear in total drive, so the ring's own contribution is subtracted
before computing ambient lux. Without this the loop oscillates: a bright ring
raises the reading, which dims the ring, which lowers the reading, which
brightens it. In a dark room, where ambient reads zero counts, the ring is the
*only* thing the sensor sees, so this is not a small correction.

This service does not touch the LEDs at all. It decides *how bright* the ring
should be and publishes that; biscuit-ring decides *what is shown* and is the
only writer. Stock splits it the same way - AmbientLightEngine computes a gain,
IssiLedDevice applies it in setFrame - and it matters here because two writers
racing on 36 sysfs files would fight.

The one thing that has to flow back the other way is the ring's own light,
which reaches the sensor (11.65). biscuit-ring publishes its total output to
/run/biscuit-ring/output and this subtracts the corresponding counts before
computing ambient lux. Without that the loop measures itself: in a dark room
the ring is the only thing the sensor can see.
"""
import argparse
import math
import os
import re
import signal
import sys
import time

IIO = "/sys/bus/iio/devices/iio:device1"
IIO_NAME_EXPECTED = "tsl2583"

RUN_DIR = "/run/biscuit-als"
LUX_OUT = os.path.join(RUN_DIR, "lux")
BRIGHTNESS_OUT = os.path.join(RUN_DIR, "brightness")

# Written by biscuit-ring: the total of all 36 channels it is driving.
RING_OUTPUT = "/run/biscuit-ring/output"

# Stock's sensor configuration (vendor tsl2583.c). See module docstring for the
# 688 -> 650 deviation.
STOCK_GAIN = 111          # als_gain index 3, the maximum
ALS_TIME_MS = 650         # stock is 688; mainline sysfs clamps at 650

# Stock's lux equation constants, for the TSL2584TSV.
COFF_1 = 105
MILILUX_COFF = 1000
CH1_COFF = 1190
NOISE_THRESHOLD = 6

# als_saturation = als_count * 922, als_count = round(650*100/270) = 241.
ALS_SATURATION = 241 * 922

# Ring self-illumination at full drive on all 36 channels, in raw counts, at
# the gain and integration time above. Linear in total drive.
RING_SELF_CH0 = 38.0
RING_SELF_CH1 = 6.0
RING_CHANNELS = 36
RING_MAX = 255

# Per-unit factory calibration, from IDME. See module docstring.
ALSCAL_PATH = "/proc/device-tree/idme/alscal"
ALS_MAX_LUX = 400.0
ALS_MIN_LUX = 0.0

# Stock's AmbientLightEngine, from libled_hal.so. See module docstring.
LUX_MIN = 0.0             # IssiAmbientLightSensor::minLux()
LUX_MAX = 400.0           # IssiAmbientLightSensor::maxLux()
RESP_COEF = (0, 915, 252, -168)   # AmbientLightEngineConfig respCoefDefs
RESP_DIVISOR = 1000.0
BRIGHTNESS_CAP = 100      # percent
GAIN_MIN = 0.005
GAIN_MAX = 1.0
RESPONSE_TIME_S = 3.0     # AmbientLightEngineConfig responseTime

# IssiLedDevice's 101-entry brightness ladder: the parameter values observed
# in stock's libled_hal.so (at offset 0x10fa0), reimplemented here as
# (duty cycle multiplier, led_current divider index).
STOCK_LADDER = [
    (0.129, 3), (0.137, 3), (0.149, 3), (0.161, 3), (0.173, 3),
    (0.184, 3), (0.196, 3), (0.204, 3), (0.216, 3), (0.227, 3),
    (0.239, 3), (0.251, 3), (0.263, 3), (0.275, 3), (0.282, 3),
    (0.294, 3), (0.306, 3), (0.318, 3), (0.329, 3), (0.341, 3),
    (0.353, 3), (0.361, 3), (0.373, 3), (0.384, 3), (0.396, 3),
    (0.408, 3), (0.325, 2), (0.345, 2), (0.361, 2), (0.380, 2),
    (0.396, 2), (0.416, 2), (0.431, 2), (0.451, 2), (0.467, 2),
    (0.486, 2), (0.502, 2), (0.522, 2), (0.537, 2), (0.557, 2),
    (0.576, 2), (0.592, 2), (0.612, 2), (0.627, 2), (0.647, 2),
    (0.663, 2), (0.682, 2), (0.698, 2), (0.718, 2), (0.733, 2),
    (0.753, 2), (0.514, 1), (0.533, 1), (0.553, 1), (0.569, 1),
    (0.588, 1), (0.608, 1), (0.624, 1), (0.643, 1), (0.663, 1),
    (0.678, 1), (0.698, 1), (0.718, 1), (0.733, 1), (0.753, 1),
    (0.773, 1), (0.788, 1), (0.808, 1), (0.827, 1), (0.843, 1),
    (0.863, 1), (0.882, 1), (0.898, 1), (0.918, 1), (0.937, 1),
    (0.957, 1), (0.494, 0), (0.510, 0), (0.525, 0), (0.541, 0),
    (0.557, 0), (0.573, 0), (0.588, 0), (0.604, 0), (0.620, 0),
    (0.635, 0), (0.651, 0), (0.667, 0), (0.682, 0), (0.698, 0),
    (0.714, 0), (0.729, 0), (0.745, 0), (0.761, 0), (0.776, 0),
    (0.792, 0), (0.808, 0), (0.824, 0), (0.839, 0), (0.855, 0),
    (0.875, 0),
]

# Collapsed to PWM only, because we have no current control: effective output
# is mult / (current + 1), scaled to 0-255. Index 100 lands on 223, not 255,
# which is deliberate - it is stock's actual ceiling of 0.875.
RING_LADDER_PWM = [
    int(round(RING_MAX * mult / (current + 1)))
    for mult, current in STOCK_LADDER
]

SAMPLE_INTERVAL_S = 2.0

# A deadband alone stops flicker but never converges: once the error is smaller
# than the band the loop stops correcting and parks there. Measured doing
# exactly that - it settled at 16 with the room back to 0 lux, where the
# correct value was 12, and stayed. So small errors are accepted too, just
# slowly: they have to persist for SETTLE_SAMPLES before being applied.
BRIGHTNESS_DEADBAND = 6
SETTLE_SAMPLES = 5

_running = True


def log(msg):
    sys.stdout.write("%s\n" % msg)
    sys.stdout.flush()


def read_int(path, default=None):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return default


def write_str(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except OSError:
        return False


def check_sensor():
    """Confirm the ALS is the part we think it is before touching it."""
    try:
        with open(os.path.join(IIO, "name")) as f:
            name = f.read().strip()
    except OSError:
        return "no ALS at %s" % IIO
    if name != IIO_NAME_EXPECTED:
        return "%s is '%s', expected '%s'" % (IIO, name, IIO_NAME_EXPECTED)
    return None


def configure_sensor():
    """Apply stock's gain and integration time.

    Both are volatile: they survive runtime suspend, because the driver
    re-applies them from als_settings on resume, but not a reboot. That is the
    whole reason this has to run at startup rather than being a one-off.

    Note the integration time is written in the units the driver actually
    wants. tsl2583.c stores als_time in milliseconds but declares the channel
    IIO_VAL_INT_PLUS_MICRO, so sysfs is off by 1000x and 650 ms is written as
    "0.000650". That is an upstream bug, not a typo here.
    """
    ok = write_str(os.path.join(IIO, "in_illuminance_calibscale"), STOCK_GAIN)
    if not ok:
        return "failed to set gain to %d" % STOCK_GAIN
    ok = write_str(os.path.join(IIO, "in_illuminance_integration_time"),
                   "0.%06d" % ALS_TIME_MS)
    if not ok:
        return "failed to set integration time to %d ms" % ALS_TIME_MS
    return None


def read_channels():
    ch0 = read_int(os.path.join(IIO, "in_illuminance_both_raw"))
    ch1 = read_int(os.path.join(IIO, "in_illuminance_ir_raw"))
    if ch0 is None or ch1 is None:
        return None, None
    return ch0, ch1


def read_alscal_coeff():
    """The per-unit factory coefficient from IDME, or None.

    Parsed exactly as parse_alscal_idme() does: skip to the second
    space-separated group and take the second field of ams_400_0=%d,%d.
    """
    try:
        with open(ALSCAL_PATH + "/value", "rb") as f:
            raw = f.read().decode("ascii", "replace").replace("\0", "").strip()
    except OSError:
        return None
    m = re.search(r"ams_400_0=(-?\d+),(\d+)", raw)
    if not m:
        return None
    coeff = int(m.group(2))
    return coeff if coeff > 0 else None


def stock_raw(ch0, ch1):
    """Stock's TSL2584TSV equation. Uncalibrated units, not lux."""
    if ch0 >= ALS_SATURATION or ch1 >= ALS_SATURATION:
        return 65535.0
    if ch0 <= 0:
        return 0.0
    if ch0 + ch1 <= NOISE_THRESHOLD:
        return 0.0

    raw = (COFF_1 * (MILILUX_COFF * ch0 - CH1_COFF * ch1)
           / float(STOCK_GAIN * ALS_TIME_MS))
    return 0.0 if raw < 0 else raw


def calibrated_lux(raw, coeff):
    """taos_calibrated_lux_show(): scale by the unit's coefficient and clamp."""
    lux = ALS_MAX_LUX * raw / float(coeff)
    if lux > ALS_MAX_LUX:
        return ALS_MAX_LUX
    if lux < ALS_MIN_LUX:
        return ALS_MIN_LUX
    return lux


def read_ring_output():
    """Total channel output biscuit-ring is currently driving, 0..9180.

    Published by the ring driver rather than measured, because the sensor
    cannot tell the ring's own contribution from the room's.
    """
    try:
        with open(RING_OUTPUT) as f:
            return max(0, min(RING_CHANNELS * RING_MAX, int(f.read().strip())))
    except (OSError, ValueError):
        return 0


def ring_self_illumination(total):
    """Counts the ring contributes to each channel at that total output.

    Linear in total drive, from the measurements in 11.65: all 36 channels at
    full give ch0=38 / ch1=6 at this gain and integration time.
    """
    fraction = total / float(RING_CHANNELS * RING_MAX)
    return RING_SELF_CH0 * fraction, RING_SELF_CH1 * fraction


def response_curve(lux):
    """AmbientLightEngine::responseCurve - the cubic, by Horner as stock does."""
    span = LUX_MAX - LUX_MIN
    x = (lux - LUX_MIN) / span if span > 0 else 0.0
    c0, c1, c2, c3 = RESP_COEF
    return (((x * c3 + c2) * x + c1) * x + c0) / RESP_DIVISOR


def led_gain(lux):
    """AmbientLightEngine::getLedGain - cap applied, clamped to [0.005, 1.0]."""
    gain = (BRIGHTNESS_CAP / 100.0) * response_curve(lux)
    if gain > GAIN_MAX:
        return GAIN_MAX
    if gain < GAIN_MIN:
        return GAIN_MIN
    return gain


def lux_to_brightness(lux):
    """Stock's whole chain: lux -> gain -> 0..100 index -> ladder -> PWM."""
    index = int(round(led_gain(lux) * 100.0))
    index = max(0, min(100, index))
    return RING_LADDER_PWM[index]


def publish(lux, brightness):
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
    except OSError:
        return
    write_str(LUX_OUT, "%.3f" % lux)
    write_str(BRIGHTNESS_OUT, brightness)


def stop(signum, frame):
    global _running
    _running = False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--monitor", action="store_true",
                    help="print raw counts, lux and the brightness stock's "
                         "curve would pick, without driving the ring")
    ap.add_argument("--interval", type=float, default=SAMPLE_INTERVAL_S)
    args = ap.parse_args()

    problem = check_sensor()
    if problem:
        log("biscuit-als: %s" % problem)
        return 1

    problem = configure_sensor()
    if problem:
        log("biscuit-als: %s" % problem)
        return 1
    log("biscuit-als: sensor at gain %d, %d ms (stock settings)"
        % (STOCK_GAIN, ALS_TIME_MS))

    # Per-unit, so it must come from this device's own IDME. Without it the
    # raw scale means nothing and any curve applied to it is invented.
    coeff = read_alscal_coeff()
    if coeff is None:
        log("biscuit-als: no usable ams_400_0 coefficient in %s; "
            "cannot calibrate lux, refusing to guess" % ALSCAL_PATH)
        return 1
    log("biscuit-als: alscal coefficient %d (raw %d = %d lux)"
        % (coeff, coeff, int(ALS_MAX_LUX)))


    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    smoothed = None
    brightness = RING_LADDER_PWM[0]
    settled = 0

    while _running:
        ch0, ch1 = read_channels()
        if ch0 is None:
            log("biscuit-als: sensor read failed")
            time.sleep(args.interval)
            continue

        # Subtract what the ring is contributing at its current output, so the
        # loop measures the room rather than itself.
        self_ch0, self_ch1 = ring_self_illumination(read_ring_output())
        amb0 = max(0.0, ch0 - self_ch0)
        amb1 = max(0.0, ch1 - self_ch1)

        raw = stock_raw(amb0, amb1)
        lux = calibrated_lux(raw, coeff)
        # Stock filters with a FilterEma configured for a 3 s response time at
        # 20 samples/s. We sample far more slowly, so derive alpha from the
        # time constant rather than copying stock's per-sample weight, which
        # would give a wildly different response at our interval.
        alpha = 1.0 - math.exp(-args.interval / RESPONSE_TIME_S)
        smoothed = lux if smoothed is None else (
            alpha * lux + (1.0 - alpha) * smoothed)

        if args.monitor:
            log("ch0=%-6d ch1=%-5d  self=(%.1f,%.1f)  raw=%.1f  lux=%.1f  "
                "smoothed=%.1f  would_set=%d"
                % (ch0, ch1, self_ch0, self_ch1, raw, lux, smoothed,
                   lux_to_brightness(smoothed)))
            time.sleep(args.interval)
            continue

        want = lux_to_brightness(smoothed)
        if abs(want - brightness) >= BRIGHTNESS_DEADBAND:
            brightness = want
            settled = 0
        elif want != brightness:
            # Below the deadband, but real. Accept it once it has persisted,
            # so the loop converges instead of parking near the target.
            settled += 1
            if settled >= SETTLE_SAMPLES:
                brightness = want
                settled = 0
        else:
            settled = 0

        publish(smoothed, brightness)

        time.sleep(args.interval)

    log("biscuit-als: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
