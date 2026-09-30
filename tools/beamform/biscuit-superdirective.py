#!/usr/bin/env python3
"""Superdirective (MVDR) beamformer weights for the Biscuit array.

Why this exists: delay-and-sum was measured on hardware and gives 0.4 dB of
directivity below 2.4 kHz, because a 72 mm aperture is roughly a fifth of a
wavelength at 1 kHz. That is where most speech energy is, so plain
delay-and-sum is not usable here. Superdirective weights get directivity from
an aperture smaller than the wavelength, which is the whole reason stock ships
64-band complex coefficients rather than delays.

**These weights are computed, not copied.** The inputs are the measured array
geometry and a standard diffuse-field noise model; no stock coefficient is read
or fitted to. Stock's files are used only as an independent check on the
result, never as a source for it, so the output is ours to distribute.

Method - textbook MVDR under a spherically isotropic (diffuse) noise field:

    Gamma_ij(f) = sinc(k * d_ij)          coherence, k = 2*pi*f/c
    a_i(f)      = exp(+j*k*(p_i . u))     steering vector toward u
    w(f)        = Gamma^-1 a / (a^H Gamma^-1 a)

The +j in the steering vector is load-bearing; -j points the beam 180 degrees
wrong and the mistake is invisible on synthetic broadband noise unless you
check the peak direction. See steering().

Diagonal loading (`eps`) trades directivity against robustness. Without it a
superdirective design demands impossible precision from the array and amplifies
uncorrelated mic noise enormously - the classic failure mode. The white noise
gain is reported per band so that trade is visible rather than implicit.
"""
import argparse
import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ARRAY = os.path.join(HERE, "biscuit-mic-array.json")
C = 343.0


def load_geometry(path=ARRAY):
    with open(path) as f:
        a = json.load(f)
    mics = sorted(a["microphones"], key=lambda m: m["channel"])
    pos = np.array([[m["x_mm"] / 1000.0, m["y_mm"] / 1000.0, 0.0] for m in mics])
    return pos, [m["label"] for m in mics]


def coherence(pos, freqs, c=C):
    """Diffuse-field coherence, (nfreq, M, M). sinc(k*d) with numpy's pi-sinc."""
    d = np.linalg.norm(pos[:, None, :] - pos[None, :, :], axis=-1)
    k = 2 * np.pi * freqs[:, None, None] / c
    return np.sinc(k * d[None] / np.pi)


def steering(pos, phi_deg, freqs, c=C):
    """(nfreq, M) for a plane wave arriving from phi in the array plane.

    Sign convention, which is easy to get backwards and gives a beam pointing
    180 degrees wrong when you do: a mic at p receives the wavefront EARLY by
    tau = (p . u)/c, so x_i(t) = s(t + tau_i) and X_i(f) = S(f)*exp(+j*2*pi*f*tau_i).
    The steering vector is therefore +j, not -j. Output is formed as w^H x, so
    the conjugation in the beamformer undoes this phase and aligns the mics.
    """
    phi = np.radians(phi_deg)
    u = np.array([np.cos(phi), np.sin(phi), 0.0])
    tau = pos @ u / c                       # early arrival, seconds
    return np.exp(+2j * np.pi * freqs[:, None] * tau[None, :])


def mvdr_weights(pos, phi_deg, freqs, eps=1e-2, c=C):
    """w: (nfreq, M). eps is diagonal loading relative to the trace."""
    m = pos.shape[0]
    G = coherence(pos, freqs, c)
    a = steering(pos, phi_deg, freqs, c)
    G = G + eps * np.eye(m)[None]
    Gia = np.linalg.solve(G, a[..., None])[..., 0]
    denom = np.einsum("fm,fm->f", a.conj(), Gia)
    denom = np.where(np.abs(denom) < 1e-12, 1e-12, denom)
    return Gia / denom[:, None]


def mvdr_wng_constrained(pos, phi_deg, freqs, wng_floor_db=-6.0, c=C,
                         eps_lo=1e-6, eps_hi=10.0, iters=40):
    """MVDR with per-frequency loading chosen to hold white noise gain.

    A single fixed `eps` is the wrong shape for this problem. The array is
    ~a fifth of a wavelength at 1 kHz and half a wavelength at 2.4 kHz, so the
    conditioning of Gamma varies enormously across the band; one loading value
    is either too timid at high frequencies or dangerously aggressive at low
    ones.

    Instead, pick the smallest loading at each frequency that still meets a
    white-noise-gain floor. That is the standard robust-MVDR constraint, and it
    is derived from our own robustness requirement - what mic self-noise we are
    willing to amplify - not from anyone else's coefficients.

    WNG decreases monotonically as eps decreases, so a bisection is safe.
    """
    m = pos.shape[0]
    a = steering(pos, phi_deg, freqs, c)
    G = coherence(pos, freqs, c)
    eye = np.eye(m)[None]

    def weights_for(eps_vec):
        Gl = G + eps_vec[:, None, None] * eye
        Gia = np.linalg.solve(Gl, a[..., None])[..., 0]
        den = np.einsum("fm,fm->f", a.conj(), Gia)
        den = np.where(np.abs(den) < 1e-12, 1e-12, den)
        return Gia / den[:, None]

    def wng_for(w):
        sig = np.abs(np.einsum("fm,fm->f", w.conj(), a)) ** 2
        wn = np.real(np.einsum("fm,fm->f", w.conj(), w))
        return 10 * np.log10(sig / np.where(wn < 1e-20, 1e-20, wn))

    lo = np.full(freqs.shape, eps_lo)
    hi = np.full(freqs.shape, eps_hi)
    for _ in range(iters):
        mid = np.sqrt(lo * hi)                 # geometric: eps spans decades
        ok = wng_for(weights_for(mid)) >= wng_floor_db
        hi = np.where(ok, mid, hi)             # met the floor -> try smaller
        lo = np.where(ok, lo, mid)
    return weights_for(hi), hi


def directivity_index(pos, phi_deg, freqs, eps, c=C):
    """DI in dB: gain toward phi over the diffuse field. The honest metric."""
    w = mvdr_weights(pos, phi_deg, freqs, eps, c)
    a = steering(pos, phi_deg, freqs, c)
    G = coherence(pos, freqs, c)
    sig = np.abs(np.einsum("fm,fm->f", w.conj(), a)) ** 2
    dif = np.real(np.einsum("fm,fmn,fn->f", w.conj(), G, w))
    dif = np.where(dif < 1e-20, 1e-20, dif)
    return 10 * np.log10(sig / dif)


def white_noise_gain(pos, phi_deg, freqs, eps, c=C):
    """WNG in dB. Very negative means the design is amplifying mic noise."""
    w = mvdr_weights(pos, phi_deg, freqs, eps, c)
    a = steering(pos, phi_deg, freqs, c)
    sig = np.abs(np.einsum("fm,fm->f", w.conj(), a)) ** 2
    wn = np.real(np.einsum("fm,fm->f", w.conj(), w))
    return 10 * np.log10(sig / np.where(wn < 1e-20, 1e-20, wn))


def ds_directivity_index(pos, phi_deg, freqs, c=C):
    """Same metric for plain delay-and-sum, as the baseline to beat."""
    a = steering(pos, phi_deg, freqs, c)
    w = a / pos.shape[0]
    G = coherence(pos, freqs, c)
    sig = np.abs(np.einsum("fm,fm->f", w.conj(), a)) ** 2
    dif = np.real(np.einsum("fm,fmn,fn->f", w.conj(), G, w))
    return 10 * np.log10(sig / np.where(dif < 1e-20, 1e-20, dif))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eps", type=float, default=1e-2,
                    help="fixed diagonal loading (analysis modes only)")
    ap.add_argument("--wng", type=float, default=-1.0,
                    help="white-noise-gain floor in dB for --out (default -1)")
    ap.add_argument("--phi", type=float, default=0.0, help="steer azimuth")
    ap.add_argument("--sweep-eps", action="store_true",
                    help="show the directivity/robustness trade")
    ap.add_argument("--fft", type=int, default=1024,
                    help="runtime FFT/filterbank band count (power of two; default 1024)")
    ap.add_argument("--hop", type=int,
                    help="runtime hop in samples (default FFT/2; stock ASR is 128/64)")
    ap.add_argument("--out", help="write weights as .npz for the runtime")
    args = ap.parse_args()

    pos, labels = load_geometry()
    print("array: %d mics, radius %.1f mm"
          % (len(labels), np.hypot(pos[0, 0], pos[0, 1]) * 1000))

    bands = np.array([250, 500, 1000, 2000, 3000, 4000, 6000])

    if args.sweep_eps:
        print("\ndiagonal loading trade-off, steering %g deg" % args.phi)
        print("  eps      " + "".join("%8d" % f for f in bands) + "   (Hz)")
        for eps in (1e-4, 1e-3, 1e-2, 1e-1, 1.0):
            di = directivity_index(pos, args.phi, bands, eps)
            wng = white_noise_gain(pos, args.phi, bands, eps)
            print("  %-8.0e DI " % eps + "".join("%8.1f" % d for d in di))
            print("           WNG" + "".join("%8.1f" % w for w in wng))
        print("\n  DI  = directivity index, higher is better")
        print("  WNG = white noise gain; below about -10 dB the design is")
        print("        amplifying mic noise faster than it rejects the room")
        return

    ds = ds_directivity_index(pos, args.phi, bands)
    sd = directivity_index(pos, args.phi, bands, args.eps)
    wng = white_noise_gain(pos, args.phi, bands, args.eps)
    print("\nsteering %g deg, eps=%g" % (args.phi, args.eps))
    print("  Hz        " + "".join("%8d" % f for f in bands))
    print("  delay-sum " + "".join("%8.1f" % d for d in ds))
    print("  superdir  " + "".join("%8.1f" % d for d in sd))
    print("  gain      " + "".join("%+8.1f" % (s - d) for s, d in zip(sd, ds)))
    print("  WNG       " + "".join("%8.1f" % w for w in wng))

    if args.out:
        # Weights on the FFT grid the runtime will use. Six beams at 60 degree
        # spacing, matching stock's beam count - those are the directions a
        # six-element ring actually resolves.
        n = args.fft
        hop = args.hop if args.hop is not None else n // 2
        if n < 16 or n & (n - 1) or hop <= 0 or hop > n:
            ap.error("--fft must be a power of two and --hop must be in 1..FFT")
        freqs = np.fft.rfftfreq(n, 1.0 / 16000)
        beams = np.arange(0, 360, 60)
        ws, epss = [], []
        for p in beams:
            w, e = mvdr_wng_constrained(pos, float(p), freqs,
                                        wng_floor_db=args.wng)
            ws.append(w)
            epss.append(e)
        w = np.stack(ws)
        np.savez(args.out, weights=w, beams=beams, freqs=freqs,
                 eps_schedule=np.stack(epss), wng_floor_db=args.wng,
                 fftlen=n, hop=hop, rate=16000, positions=pos)
        di = np.array([directivity_index(pos, 90.0, np.array([f]), e)[0]
                       for f, e in zip(freqs[1:], epss[0][1:])])
        print("\nwrote %s" % args.out)
        print("  %d beams x %d bins x %d mics, WNG floor %+.1f dB"
              % (w.shape[0], w.shape[1], w.shape[2], args.wng))
        print("  directivity index: mean %.1f dB, max %.1f dB"
              % (di.mean(), di.max()))
        print("  computed from measured geometry and a diffuse-field model;")
        print("  contains no stock coefficients and is distributable.")


if __name__ == "__main__":
    main()
