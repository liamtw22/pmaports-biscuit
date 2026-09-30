#!/usr/bin/env python3
"""Export computed beamformer weights to a flat binary the C runtime can mmap.

Deliberately dumb format: a fixed header then raw little-endian floats, so the
runtime needs no parser, no allocator surprises and no dependency. Everything
the C side needs to configure itself is in the header, so a weights file and a
binary that disagree fail loudly rather than producing quiet nonsense.

    magic   "BBFW"          4 bytes
    u32     version         1
    u32     fftlen          1024
    u32     hop             512
    u32     rate            16000
    u32     nmics           7
    u32     nbeams          6
    u32     nbins           fftlen/2 + 1
    u32     refchan         7   (DAC loopback, not a mic)
    f32     gains[nmics]        miccal, already divided by 16384
    i32     chanmap[nmics]      capture channel for each mic
    f32     w[nbeams][nbins][nmics][2]   complex, real then imag

Weights are stored conjugated, because the runtime forms w^H x and doing the
conjugation once here saves it per frame forever.
"""
import argparse
import json
import os
import struct

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

ap = argparse.ArgumentParser()
ap.add_argument("--weights", default=os.path.join(HERE, "biscuit-beam-weights.npz"))
ap.add_argument("--array", default=os.path.join(HERE, "biscuit-mic-array.json"))
ap.add_argument("--out", default=os.path.join(HERE, "biscuit-beam-weights.bin"))
args = ap.parse_args()

z = np.load(args.weights)
w = z["weights"]                     # (nbeams, nbins, nmics)
nbeams, nbins, nmics = w.shape
fftlen = int(z["fftlen"])
hop = int(z["hop"])
rate = int(z["rate"])

with open(args.array) as f:
    arr = json.load(f)
mics = sorted(arr["microphones"], key=lambda m: m["channel"])
chanmap = [m["channel"] for m in mics]
refchan = arr["reference_channel"]["channel"]

cal = arr.get("idme_calibration", {})
if cal.get("verified"):
    gains = [cal["gains"]["ch%d_%s" % (m["channel"], m["label"])] for m in mics]
else:
    gains = [1.0] * nmics
    print("warning: calibration not marked verified, using unity gains")

assert len(chanmap) == nmics, "array json has %d mics, weights have %d" % (
    len(chanmap), nmics)
assert nbins == fftlen // 2 + 1

with open(args.out, "wb") as f:
    f.write(b"BBFW")
    f.write(struct.pack("<8I", 1, fftlen, hop, rate, nmics, nbeams, nbins,
                        refchan))
    f.write(np.asarray(gains, dtype="<f4").tobytes())
    f.write(np.asarray(chanmap, dtype="<i4").tobytes())
    # Conjugate now: the runtime computes sum(conj(w) * X), so store conj(w)
    # and let it do a plain complex multiply-accumulate.
    wc = np.conj(w).astype(np.complex64)
    inter = np.empty((nbeams, nbins, nmics, 2), dtype="<f4")
    inter[..., 0] = wc.real
    inter[..., 1] = wc.imag
    f.write(inter.tobytes())

size = os.path.getsize(args.out)
print("wrote %s" % args.out)
print("  %d beams x %d bins x %d mics, fft %d hop %d @ %d Hz"
      % (nbeams, nbins, nmics, fftlen, hop, rate))
print("  channel map %s, reference ch%d" % (chanmap, refchan))
print("  gains %s" % " ".join("%.4f" % g for g in gains))
print("  %d bytes" % size)
