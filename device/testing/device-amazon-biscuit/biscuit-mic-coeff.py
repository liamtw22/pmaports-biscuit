#!/usr/bin/env python3
"""Make sure the microphone ADC filter coefficients actually reached the chips.

The converters only accept coefficient memory (page 4) while the ADC is FULLY
POWERED DOWN. Page 0 reg 36, ADC_FLAG, is the gate: at 0x00 page 4 reads and
writes normally, and at anything else reads return 0 and WRITES ARE SILENTLY
DROPPED. During capture all four read 0xcc; a chip can also sit at 0x04 while
otherwise idle, and that state is sticky.

So a chip that happens to be busy when the UCM fires misses its filter for the
whole session, and nothing in ALSA can tell:

  * amixer reports the regmap CACHE. The dropped write still updated the cache,
    so every control reads back the value the chip never took.
  * the driver sees no error, because the chip ACKs the write and discards it.

Observed twice on real hardware: Mic D on 2026-08-21, Mic C on 2026-08-22.

This reads the chips directly, compares against what the UCM asks for, and
rewrites any chip that did not take it - waiting for each to go idle first.
Raw I2C is deliberate: going back through ALSA would be a no-op, because
regmap already believes the value is in place and would skip the write.

  biscuit-mic-coeff.py            verify, repair, log
  biscuit-mic-coeff.py --check    verify only, exit 1 if anything is wrong
"""

import ctypes
import fcntl
import os
import re
import sys
import time

I2C_BUS = "/dev/i2c-0"
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001

# Mic A..D. sound-name-prefix in the device tree maps these to 0x18..0x1b.
CHIPS = (("A", 0x18), ("B", 0x19), ("C", 0x1a), ("D", 0x1b))

UCM = "/usr/share/alsa/ucm2/MediaTek/mt8163_biscuit/HiFi.conf"
LOG = "/var/log/biscuit-mic-coeff.log"

PRB_REG = 61            # page 0
ADC_FLAG_REG = 36       # page 0, read-only status; 0x00 means safe to write
BIQUAD = {"Left": 14, "Right": 78}   # page 4, 5 coefficients, MSB first

# Used only if the UCM cannot be parsed. Second order high pass, fc 24.9 Hz,
# Q 0.72 at 16 kHz, Q15 with N1 and D1 stored halved (that is how TI fits
# |a1| = 1.99 into a signed 16 bit field).
FALLBACK_PRB = 2
FALLBACK_BIQUAD = [32546, 32990, 32546, 32545, 33210]

# At boot nothing has captured yet, so every converter is already idle and none
# of this waiting happens. The budget only bites when a chip is stuck, and it is
# capped ACROSS THE WHOLE RUN rather than per chip: four stuck converters at a
# generous per-chip timeout would otherwise hold up the boot for minutes.
UNLOCK_TIMEOUT = 8.0    # seconds to wait for any one chip
RUN_BUDGET = 20.0       # seconds of waiting allowed across all four
POLL = 0.25


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class Msg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16), ("buf", ctypes.POINTER(ctypes.c_uint8))]


class Data(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(Msg)), ("nmsgs", ctypes.c_uint32)]


def _p(buf):
    return ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))


class Bus:
    def __init__(self, path=I2C_BUS):
        self.fd = os.open(path, os.O_RDWR)

    def close(self):
        os.close(self.fd)

    def _xfer(self, msgs):
        arr = (Msg * len(msgs))(*msgs)
        fcntl.ioctl(self.fd, I2C_RDWR, Data(arr, len(msgs)))

    def read(self, addr, page, reg, count):
        """Page-select, set the pointer and read in ONE atomic transaction.

        Split into separate transfers this races the codec driver, which does
        its own page switching - the page can move between the select and the
        read, and the values that come back are from whatever page won.
        """
        sel = (ctypes.c_uint8 * 2)(0, page)
        ptr = (ctypes.c_uint8 * 1)(reg)
        out = (ctypes.c_uint8 * count)()
        self._xfer([Msg(addr, 0, 2, _p(sel)),
                    Msg(addr, 0, 1, _p(ptr)),
                    Msg(addr, I2C_M_RD, count, _p(out))])
        return list(out)

    def write(self, addr, page, reg, values):
        sel = (ctypes.c_uint8 * 2)(0, page)
        msgs = [Msg(addr, 0, 2, _p(sel))]
        bufs = [sel]
        for offset, value in enumerate(values):
            buf = (ctypes.c_uint8 * 2)(reg + offset, value & 0xff)
            bufs.append(buf)                      # keep alive until the ioctl
            msgs.append(Msg(addr, 0, 2, _p(buf)))
        self._xfer(msgs)


def u16_pairs(raw):
    return [(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw), 2)]


def to_bytes(values):
    out = []
    for v in values:
        v &= 0xffff
        out += [(v >> 8) & 0xff, v & 0xff]
    return out


def parse_ucm(path=UCM):
    """Read the wanted values out of the UCM so there is one source of truth.

    The UCM is what a capture actually applies; duplicating the numbers here
    would let the two drift apart silently.
    """
    prb, biquad = None, {}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as exc:
        log("cannot read %s (%s); using built-in defaults" % (path, exc))
        return FALLBACK_PRB, {s: list(FALLBACK_BIQUAD) for s in BIQUAD}

    m = re.search(r"name='Mic [ABCD] ADC Processing Block'\s+(\d+)", text)
    if m:
        prb = int(m.group(1))
    for side in BIQUAD:
        m = re.search(r"name='Mic [ABCD] %s ADC Biquad A Coefficients[^']*'\s+"
                      r"([0-9,\s]+)\"" % side, text)
        if m:
            biquad[side] = [int(x) for x in m.group(1).split(",")]

    if prb is None or len(biquad) != len(BIQUAD) or \
            any(len(v) != 5 for v in biquad.values()):
        log("UCM did not yield a full set; using built-in defaults")
        return FALLBACK_PRB, {s: list(FALLBACK_BIQUAD) for s in BIQUAD}
    return prb, biquad


def wait_idle(bus, addr, run_deadline):
    """Page 4 is only writable at ADC_FLAG == 0x00. Anything else drops writes."""
    deadline = min(time.monotonic() + UNLOCK_TIMEOUT, run_deadline)
    flag = bus.read(addr, 0, ADC_FLAG_REG, 1)[0]
    while flag != 0x00 and time.monotonic() < deadline:
        time.sleep(POLL)
        flag = bus.read(addr, 0, ADC_FLAG_REG, 1)[0]
    return flag == 0x00, flag


def check_chip(bus, addr, want_prb, want_biquad):
    """Return (ok, details). Only meaningful when the chip is idle."""
    bad = []
    prb = bus.read(addr, 0, PRB_REG, 1)[0]
    if prb != want_prb:
        bad.append("PRB=%d want %d" % (prb, want_prb))
    for side, reg in BIQUAD.items():
        got = u16_pairs(bus.read(addr, 4, reg, 10))
        if got != [v & 0xffff for v in want_biquad[side]]:
            bad.append("%s biquad=%s" % (side, got))
    return (not bad), bad


def repair_chip(bus, addr, want_prb, want_biquad):
    bus.write(addr, 0, PRB_REG, [want_prb])
    for side, reg in BIQUAD.items():
        bus.write(addr, 4, reg, to_bytes(want_biquad[side]))


def main():
    check_only = "--check" in sys.argv
    want_prb, want_biquad = parse_ucm()

    try:
        bus = Bus()
    except OSError as exc:
        log("FATAL: cannot open %s: %s" % (I2C_BUS, exc))
        return 1

    failures = 0
    repaired = 0
    run_deadline = time.monotonic() + RUN_BUDGET
    try:
        for name, addr in CHIPS:
            idle, flag = wait_idle(bus, addr, run_deadline)
            if not idle:
                # Not fatal on its own: the chip may simply be capturing. But
                # its coefficients cannot be read, let alone trusted - and
                # rewriting one blind would be a no-op we would then report as
                # a repair.
                log("Mic %s (0x%02x): busy, ADC_FLAG=0x%02x - cannot verify"
                    % (name, addr, flag))
                failures += 1
                continue

            ok, bad = check_chip(bus, addr, want_prb, want_biquad)
            if ok:
                log("Mic %s (0x%02x): ok" % (name, addr))
                continue

            log("Mic %s (0x%02x): WRONG - %s" % (name, addr, "; ".join(bad)))
            if check_only:
                failures += 1
                continue

            repair_chip(bus, addr, want_prb, want_biquad)
            ok, bad = check_chip(bus, addr, want_prb, want_biquad)
            if ok:
                log("Mic %s (0x%02x): repaired" % (name, addr))
                repaired += 1
            else:
                log("Mic %s (0x%02x): REPAIR FAILED - %s"
                    % (name, addr, "; ".join(bad)))
                failures += 1
    finally:
        bus.close()

    log("done: %d repaired, %d unresolved" % (repaired, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
