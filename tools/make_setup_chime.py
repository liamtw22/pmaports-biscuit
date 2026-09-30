#!/usr/bin/env python3
"""Generate biscuit-setup-chime.wav: the sound the setup page's "Play a sound on
the device" button makes when the owner's own earcons are not installed yet.

That button is the setup page's check against a lookalike access point: only
the real Echo in the room can make a sound when it is pressed. On a fresh
install no earcon exists - pmOS ships none of Amazon's, and the owner's are
imported later - so the check was silent and could not be passed honestly.
This chime is ours, generated here, so it can ship in the core package.

Deterministic: the same script always writes the same bytes.

    python3 make_setup_chime.py OUT.wav
"""
import math
import struct
import sys
import wave

RATE = 48000
PEAK = 10 ** (-6 / 20)            # -6 dBFS


def note(freq, start, length, tau):
    """A struck tone: 5 ms attack, exponential decay, a quiet octave above."""
    out = []
    for i in range(int(length * RATE)):
        t = i / RATE
        env = min(1.0, t / 0.005) * math.exp(-t / tau)
        out.append(env * (math.sin(2 * math.pi * freq * t)
                          + 0.25 * math.sin(2 * math.pi * 2 * freq * t)))
    return int(start * RATE), out


def main():
    total = int(0.70 * RATE)
    mix = [0.0] * total
    for at, samples in (note(783.99, 0.00, 0.30, 0.10),     # G5
                        note(1174.66, 0.16, 0.54, 0.18)):   # D6, a fifth up
        for i, v in enumerate(samples):
            if at + i < total:
                mix[at + i] += v
    scale = PEAK / max(abs(v) for v in mix)
    fade = int(0.02 * RATE)                                  # no click at the end
    for i in range(fade):
        mix[total - fade + i] *= 1 - i / fade
    with wave.open(sys.argv[1], "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(round(v * scale * 32767))) for v in mix))


if __name__ == "__main__":
    main()
