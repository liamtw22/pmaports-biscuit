#!/usr/bin/env python3
"""Turn the factory per-microphone calibration into a gain vector.

The seven MEMS capsules are not identical. Amazon measures each one at the
factory and stores the result in IDME, which the bootloader hands to us as
device-tree nodes:

    /proc/device-tree/idme/miccal.0 .. miccal.6   ->  one value per capsule

On the development unit those seven values spread about **2.9 dB** between
the smallest and largest coefficient (the unit's own values are not recorded
here). A second unit reads a different set, so this is genuinely per unit and
not a constant we could bake in.

Stock's only consumer of miccal is libasp.so. Examining the stock AArch64
binary established both the encoding and direction: its static initializer
fills the gain array with 0x4000 (16384, Q14 unity), checkMiccal() promotes a
valid Q14 value to the Q30 representation used by the DSP, and the processing
path multiplies microphone N by miccal.N. We therefore apply the same
coefficient, miccal.N / 16384, to capture channel N, reimplementing that
observed behaviour. These are calibration gain coefficients, not sensitivity
measurements to invert or normalise to their mean.

Factory mode is the default now that the direction has been validated. An
explicit mode already stored in /opt/persist/miccal-mode still wins, so this
does not silently change an existing user's selection.

MODES
-----
  factory   the capsule's stock Q14 coefficient (miccal.N / 16384)
  default   all ones - every capsule treated as nominal
  manual    per-capsule offsets in dB from MANUAL_FILE, for anyone who wants to
            trim by ear or by their own measurement

Channel order is the capture order of hw:0,2, which
BISCUIT_MIC_CHANNEL_MAP fixes as ch0-5 = mics A/B/C left+right and ch6 = mic D
left, the centre. miccal.N is taken to correspond to channel N.
"""
import os
import sys

IDME = "/proc/device-tree/idme"
NMICS = 7

RUN_DIR = "/run/biscuit-miccal"
GAINS_OUT = os.path.join(RUN_DIR, "gains")
STATUS_OUT = os.path.join(RUN_DIR, "status")

MODE_FILE = "/opt/persist/miccal-mode"
MANUAL_FILE = "/opt/persist/miccal-manual"
DEFAULT_MODE = "factory"
MODES = ("factory", "default", "manual")

# A coefficient more than this far from Q14 unity means the data is not what we
# think it is - a corrupt IDME, a different encoding, a unit we have never seen.
# Refuse rather than apply a wild correction to someone's microphones.
MAX_TRIM_DB = 6.0
Q14_UNITY = 16384.0


def log(msg):
    sys.stderr.write("biscuit-miccal: %s\n" % msg)


def read_idme(key):
    """One IDME value, as text. The DT exports it NUL-padded."""
    try:
        with open(os.path.join(IDME, key, "value"), "rb") as f:
            return f.read().decode("ascii", "replace").replace("\0", "").strip()
    except OSError:
        return None


def read_factory():
    """The seven capsule values, or None if any is missing or unusable."""
    vals = []
    for n in range(NMICS):
        raw = read_idme("miccal.%d" % n)
        if raw is None:
            log("miccal.%d is not present in %s" % (n, IDME))
            return None
        try:
            v = float(raw)
        except ValueError:
            log("miccal.%d is not a number: %r" % (n, raw))
            return None
        if v <= 0:
            log("miccal.%d is %g, which cannot be a gain coefficient" % (n, v))
            return None
        vals.append(v)
    return vals


def db(ratio):
    import math
    return 20.0 * math.log10(ratio)


def factory_gains():
    """Convert stock's per-channel Q14 coefficients to linear gains."""
    vals = read_factory()
    if vals is None:
        return None, "factory calibration unavailable"
    gains = [v / Q14_UNITY for v in vals]
    worst = max(abs(db(g)) for g in gains)
    if worst > MAX_TRIM_DB:
        log("refusing: coefficient %.1f dB from unity, over the %.1f dB limit"
            % (worst, MAX_TRIM_DB))
        return None, "factory values out of range (%.1f dB)" % worst
    spread = db(max(vals) / min(vals))
    return gains, "factory, %.2f dB spread, max trim %.2f dB" % (spread, worst)


def manual_gains():
    """Per-capsule dB offsets, one per line, blank or '#' ignored."""
    trims = []
    try:
        with open(MANUAL_FILE) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    trims.append(float(line))
    except (OSError, ValueError) as err:
        log("manual trims unusable (%s); falling back to default" % err)
        return None, "manual trims unreadable"
    if len(trims) != NMICS:
        log("manual trims: expected %d values, got %d" % (NMICS, len(trims)))
        return None, "manual trims wrong length"
    if max(abs(t) for t in trims) > MAX_TRIM_DB:
        log("manual trims exceed the %.1f dB limit" % MAX_TRIM_DB)
        return None, "manual trims out of range"
    return [10.0 ** (t / 20.0) for t in trims], "manual"


def read_mode():
    try:
        with open(MODE_FILE) as f:
            mode = f.read().strip().lower()
        if mode in MODES:
            return mode
        log("unknown mode %r in %s; using %s" % (mode, MODE_FILE, DEFAULT_MODE))
    except OSError:
        pass
    return DEFAULT_MODE


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else read_mode()
    if mode not in MODES:
        log("usage: biscuit-miccal.py [%s]" % "|".join(MODES))
        return 2

    gains, note = None, ""
    if mode == "factory":
        gains, note = factory_gains()
    elif mode == "manual":
        gains, note = manual_gains()

    if gains is None:
        # Every failure lands here, and unity is always safe: it is exactly the
        # behaviour we shipped before this existed.
        if mode != "default":
            log("%s; using unity gains" % note)
        gains = [1.0] * NMICS
        note = note or "default (all capsules nominal)"

    os.makedirs(RUN_DIR, exist_ok=True)
    tmp = GAINS_OUT + ".tmp"
    with open(tmp, "w") as f:
        for g in gains:
            f.write("%.6f\n" % g)
    os.replace(tmp, GAINS_OUT)

    with open(STATUS_OUT + ".tmp", "w") as f:
        f.write("%s\n%s\n" % (mode, note))
    os.replace(STATUS_OUT + ".tmp", STATUS_OUT)

    log("%s -> %s" % (note, " ".join("%.3f" % g for g in gains)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
