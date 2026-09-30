/*
 * Real-time superdirective beamformer for the Amazon Echo Dot 2 (biscuit).
 *
 * Reads interleaved 8-channel S24_3LE at 16 kHz on stdin, writes mono S16_LE
 * at 16 kHz on stdout, so it drops into a pipeline:
 *
 *   arecord -D hw:0,2 -f S24_3LE -c 8 -r 16000 -t raw - \
 *     | biscuit-beamform -w biscuit-beam-weights.bin \
 *     | wyoming-satellite ...
 *
 * Weights come from biscuit-superdirective.py and are computed from measured
 * array geometry plus a diffuse-field noise model - no vendor data - so this
 * whole path is distributable. tools/beamform/biscuit-superdirective.py in
 * pmaports-biscuit derives them.
 *
 * Design notes:
 *
 *  - Weighted overlap-add, 1024-point FFT, hop 512, Hann. sqrt-Hann on both
 *    analysis and synthesis would also work; plain Hann with 50% overlap sums
 *    to a constant, so no normalisation pass is needed.
 *
 *  - No external FFT library. A 1024-point radix-2 complex FFT is about forty
 *    lines and removes a dependency from a device that has to cross-compile
 *    into a postmarketOS package. Two real FFTs are packed into one complex
 *    transform, so seven mics cost four transforms rather than seven.
 *
 *  - Cost per hop is roughly 300 kflop, about 10 Mflop/s at 16 kHz. The A53 in
 *    this device does that without noticing; the bottleneck is I/O, not maths.
 *
 *  - Beam selection is by per-beam SNR over the speech band, not by raw
 *    output energy. See pick_beam() for why the obvious energy rule is
 *    actively harmful here. -b pins a beam instead, for testing, and -E
 *    restores the old energy rule so the difference stays measurable.
 *
 * Provenance of the optional stock-profile paths (-S, -T, -U, -V, -R):
 *
 *  - They reimplement behaviour observed in Fire OS's audio front end on the
 *    owner's own device. No stock code and no stock table is included in this
 *    file. Their coefficient files (the Fire OS 5 filterbank prototype and
 *    fixed-beamformer set) are imported by each owner from their own device
 *    and read at run time; this package never ships them.
 *
 *  - The few scalar parameters named STOCK_* below (smoothing factors,
 *    thresholds, hangovers, band limits) were read from the owner's AFE.cfg or
 *    measured from the stock implementation's behaviour. Where a comment gives
 *    a hex number such as 0x43bd0, it is the location in the stock libasp.so
 *    where that behaviour was observed, kept as a research reference.
 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdint.h>
#include <ctype.h>
#include <unistd.h>
#include <pthread.h>

/*
 * M_PI is X/Open, not POSIX, so _POSIX_C_SOURCE above hides it. Define it
 * rather than widening the feature test macro - this file needs nothing else
 * from _GNU_SOURCE and musl and glibc should build it identically.
 */
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define MAGIC "BBFW"
/* One calibration value per capture channel; hw:0,2 presents nine. */
#define MAX_CAL_CHANS 16
#define BYTES_PER_SAMPLE 3	/* S24_3LE: 24 bits packed into 3 bytes */

typedef struct { float re, im; } cpx;

/*
 * Per-mic DC blocker, and it is not optional.
 *
 * These TLV320ADC3101s carry a large per-channel DC offset - measured 0.11
 * full-scale on the centre mic, roughly -19 dBFS of pure DC, and every mic's
 * offset differs. The beamformer's weights are complex and frequency
 * dependent, so a constant per-mic offset does not cancel: it becomes
 * low-frequency energy. Measured cost of leaving it in was +16.5 dB of added
 * noise at 100-200 Hz and +4 to +10 dB up to 2 kHz - i.e. beamforming made the
 * speech band *worse*.
 *
 * Stock hits the same thing on the same silicon and ships libaudiodcrflt.so
 * (DC removal filter) for it.
 *
 * Standard one-pole/one-zero blocker: y[n] = x[n] - x[n-1] + R*y[n-1].
 * R = 1 - 2*pi*fc/fs with fc = 60 Hz, comfortably below speech and well above
 * the offset.
 */
#define DCBLOCK_R 0.9764f	/* ~60 Hz corner at 16 kHz */

struct dcblock { float x1, y1; };

static inline float dcblock_step(struct dcblock *d, float x)
{
	float y = x - d->x1 + DCBLOCK_R * d->y1;
	d->x1 = x;
	d->y1 = y;
	return y;
}

/*
 * The stock 16 kHz ASR path follows the downsampler with this exact three
 * section 80 Hz high-pass.  Unlike the compact 60 Hz DC blocker above this
 * has a well-defined speech-band response, so the stock profile uses it on
 * every microphone and on the DAC-loopback reference before AEC.  The values
 * are settings, not the vendor's implementation, and are published in the
 * device AFE configuration we are interoperating with.
 */
struct hpf80 { float x1[3], x2[3], y1[3], y2[3]; };

static inline float hpf80_step(struct hpf80 *s, float x)
{
	static const float a1[3] = {
		-1.978964742877471f, -1.920823752121581f, -1.995031240858690f
	};
	static const float a2[3] = {
		 0.980148787818626f,  0.923195725460049f,  0.995935784603137f
	};
	static const float b0[3] = {
		 2.061764283274676f, 12.481101519210988f,  0.036465860122281f
	};
	static const float b1[3] = {
		-4.122862292740000f,-24.961562229700000f, -0.072912904280000f
	};
	static const float b2[3] = {
		 2.061764283274676f, 12.481101519210988f,  0.036465860122281f
	};
	for (int i = 0; i < 3; i++) {
		/* Coefficients use the standard denominator 1 + a1*z^-1 +
		 * a2*z^-2, hence the minus signs in the recurrence. */
		float y = b0[i] * x + b1[i] * s->x1[i] + b2[i] * s->x2[i]
			- a1[i] * s->y1[i] - a2[i] * s->y2[i];
		s->x2[i] = s->x1[i]; s->x1[i] = x;
		s->y2[i] = s->y1[i]; s->y1[i] = y;
		x = y;
	}
	return x;
}

static inline cpx cpx_add(cpx a, cpx b)
{
	cpx r = { a.re + b.re, a.im + b.im };
	return r;
}

static inline cpx cpx_sub(cpx a, cpx b)
{
	cpx r = { a.re - b.re, a.im - b.im };
	return r;
}

static inline cpx cpx_mul(cpx a, cpx b)
{
	cpx r = { a.re * b.re - a.im * b.im, a.re * b.im + a.im * b.re };
	return r;
}

static inline cpx cpx_mul_conj_right(cpx a, cpx b)
{
	cpx r = { a.re * b.re + a.im * b.im, a.im * b.re - a.re * b.im };
	return r;
}

static inline float cpx_power(cpx a)
{
	return a.re * a.re + a.im * a.im;
}

/* The stock Biscuit ASR path is a 128-point, hop-64 analysis/synthesis bank
 * using a 640-tap Knight prototype.  Those taps are NOT in this package: each
 * owner imports the file from their own device (biscuit-import-assets, the
 * fireos5 profile) and it is never read from a stock partition at run time,
 * so a repartitioned pmOS installation remains whole.
 *
 * Do not replace this parser with a "similar" window: the actual prototype
 * determines inter-band leakage, group delay, and the spectral domain seen by
 * the fixed beamformer and AEC. */
#define STOCK_FILTERBANK_FILE "/usr/share/biscuit/biscuit-stock-filterbank-640.cfg"
#define STOCK_FBF_FILE "/usr/share/biscuit/biscuit-stock-fbf.cfg"

static int init_stock_filterbank(float *prototype, float *normalizer,
				 int nfft, int hop)
{
	const int len = 5 * nfft;
	char line[128];
	int count = 0;
	FILE *f;

	if (nfft != 128 || hop != 64) {
		fprintf(stderr, "beamform: stock filterbank needs FFT 128 / hop 64\n");
		return -1;
	}
	f = fopen(STOCK_FILTERBANK_FILE, "r");
	if (!f) {
		fprintf(stderr, "beamform: cannot open %s\n", STOCK_FILTERBANK_FILE);
		return -1;
	}
	while (fgets(line, sizeof(line), f)) {
		char *p = line, *end;
		float v;
		while (isspace((unsigned char)*p))
			p++;
		if (*p == '\0' || *p == '/')
			continue;       /* blank or comment line */
		v = strtof(p, &end);
		if (end == p)
			continue;
		while (isspace((unsigned char)*end))
			end++;
		if (*end != '\0' && *end != ',')
			continue;       /* never mistake a prose/date line for a tap */
		if (count == len) {
			fprintf(stderr, "beamform: too many stock filterbank taps\n");
			fclose(f);
			return -1;
		}
		prototype[count++] = v;
	}
	fclose(f);
	if (count != len) {
		fprintf(stderr, "beamform: stock filterbank has %d taps, need %d\n",
			count, len);
		return -1;
	}

	/* Match the stock analysis/synthesis energy at each 64-sample phase. */
	for (int phase = 0; phase < hop; phase++) {
		float e = 0.0f;
		for (int n = phase; n < len; n += hop)
			e += prototype[n] * prototype[n];
		normalizer[phase] = e > 1e-12f ? 1.0f / e : 1.0f;
	}
	return 0;
}

/* The stock fixed beamformer is a four-frame complex FIR for every one of
 * 64 subbands, six look directions, and seven microphones.  Its text source
 * groups four adjacent bands as four real then four imaginary values, with
 * coefficients written in 3..0 order.  Keep that layout in the parser rather
 * than flattening it by hand: the source file is the auditable recovered
 * artifact and makes an accidental tap/order change obvious. */
static int load_stock_fbf(cpx *coefs, int nfft, int nbeams, int nmics,
			  int reverse_taps, int conjugate)
{
	char line[128];
	float group[8];
	size_t values = 0, blocks = 0;
	const size_t expected = (size_t)(nfft / 2) * nbeams * 4 * nmics * 2;
	const size_t expected_blocks = expected / 8;
	FILE *f;

	if (nfft != 128 || nbeams != 6 || nmics != 7) {
		fprintf(stderr, "beamform: stock FBF needs 128 FFT / 6 beams / 7 mics\n");
		return -1;
	}
	f = fopen(STOCK_FBF_FILE, "r");
	if (!f) {
		fprintf(stderr, "beamform: cannot open %s\n", STOCK_FBF_FILE);
		return -1;
	}
	while (fgets(line, sizeof(line), f)) {
		char *p = line, *end;
		float v;
		while (isspace((unsigned char)*p))
			p++;
		if (*p == '\0' || *p == '/')
			continue;
		v = strtof(p, &end);
		if (end == p)
			continue;
		while (isspace((unsigned char)*end))
			end++;
		if (*end != '\0' && *end != ',')
			continue;
		if (values == expected) {
			fprintf(stderr, "beamform: too many stock FBF values\n");
			fclose(f);
			return -1;
		}
		group[values % 8] = v;
		values++;
		if (values % 8 == 0) {
			int mic = (int)(blocks % nmics);
			int file_tap = 3 - (int)((blocks / nmics) % 4);
			int tap = reverse_taps ? 3 - file_tap : file_tap;
			int beam = (int)((blocks / (nmics * 4)) % nbeams);
			int band0 = (int)(blocks / (nmics * 4 * nbeams)) * 4;
			for (int j = 0; j < 4; j++) {
				cpx *h = coefs +
					((((size_t)beam * (nfft / 2) + band0 + j) * 4 + tap) * nmics + mic);
				/* Knight normalises analysis spectra by N; our radix-2 FFT is intentionally unnormalised in the forward direction. Keep the recovered FBF coefficients in the stock representation before selection and ABF. */
				h->re = group[j] / (float)nfft;
				h->im = (conjugate ? -group[j + 4] : group[j + 4]) /
					(float)nfft;
			}
			blocks++;
		}
	}
	fclose(f);
	if (values != expected || blocks != expected_blocks) {
		fprintf(stderr, "beamform: stock FBF has %zu values / %zu groups, need %zu / %zu\n",
			values, blocks, expected, expected_blocks);
		return -1;
	}
	return 0;
}

static cpx apply_stock_fbf(const cpx *coefs, const cpx *history, int beam,
				   int band, int nmics, int nbins)
{
	cpx sum = { 0.0f, 0.0f };
	for (int tap = 0; tap < 4; tap++) {
		for (int mic = 0; mic < nmics; mic++) {
			const cpx h = coefs[(((size_t)beam * 64 + band) * 4 + tap) * nmics + mic];
			const cpx x = history[((size_t)tap * nmics + mic) * nbins + band];
			sum = cpx_add(sum, cpx_mul(h, x));
		}
	}
	return sum;
}

/* ------------------------------------------------------------------ FFT -- */

static void fft_radix2(cpx *a, int n, int inverse)
{
	for (int i = 1, j = 0; i < n; i++) {
		int bit = n >> 1;
		for (; j & bit; bit >>= 1)
			j ^= bit;
		j ^= bit;
		if (i < j) {
			cpx t = a[i];
			a[i] = a[j];
			a[j] = t;
		}
	}
	for (int len = 2; len <= n; len <<= 1) {
		double ang = 2 * M_PI / len * (inverse ? 1 : -1);
		cpx wl = { (float)cos(ang), (float)sin(ang) };
		for (int i = 0; i < n; i += len) {
			cpx w = { 1.0f, 0.0f };
			for (int j = 0; j < len / 2; j++) {
				cpx u = a[i + j];
				cpx v = { a[i + j + len / 2].re * w.re - a[i + j + len / 2].im * w.im,
					  a[i + j + len / 2].re * w.im + a[i + j + len / 2].im * w.re };
				a[i + j].re = u.re + v.re;
				a[i + j].im = u.im + v.im;
				a[i + j + len / 2].re = u.re - v.re;
				a[i + j + len / 2].im = u.im - v.im;
				cpx nw = { w.re * wl.re - w.im * wl.im,
					   w.re * wl.im + w.im * wl.re };
				w = nw;
			}
		}
	}
	if (inverse)
		for (int i = 0; i < n; i++) {
			a[i].re /= n;
			a[i].im /= n;
		}
}

/*
 * Two real signals through one complex FFT. x = a + j*b, then
 *   A[k] = (X[k] + conj(X[N-k]))/2      B[k] = (X[k] - conj(X[N-k]))/(2j)
 * Halves the transform count, which matters more than it looks: the mic FFTs
 * dominate the per-hop cost.
 */
static void fft_two_real(const float *a, const float *b, int n,
			 cpx *A, cpx *B, cpx *scratch)
{
	for (int i = 0; i < n; i++) {
		scratch[i].re = a[i];
		scratch[i].im = b[i];
	}
	fft_radix2(scratch, n, 0);
	int nb = n / 2 + 1;
	for (int k = 0; k < nb; k++) {
		int m = (n - k) % n;
		cpx xk = scratch[k], xm = scratch[m];
		A[k].re = 0.5f * (xk.re + xm.re);
		A[k].im = 0.5f * (xk.im - xm.im);
		B[k].re = 0.5f * (xk.im + xm.im);
		B[k].im = -0.5f * (xk.re - xm.re);
	}
}

/* -------------------------------------------------------------- weights -- */

struct weights {
	uint32_t fftlen, hop, rate, nmics, nbeams, nbins, refchan;
	float *gains;      /* [nmics] */
	int32_t *chanmap;  /* [nmics] */
	float *w;          /* [nbeams][nbins][nmics][2], already conjugated */
};

static int load_weights(const char *path, struct weights *W)
{
	FILE *f = fopen(path, "rb");
	if (!f) {
		fprintf(stderr, "beamform: cannot open %s\n", path);
		return -1;
	}
	char magic[4];
	uint32_t hdr[8];
	if (fread(magic, 1, 4, f) != 4 || memcmp(magic, MAGIC, 4) ||
	    fread(hdr, sizeof(uint32_t), 8, f) != 8) {
		fprintf(stderr, "beamform: %s is not a weights file\n", path);
		fclose(f);
		return -1;
	}
	if (hdr[0] != 1) {
		fprintf(stderr, "beamform: weights version %u unsupported\n", hdr[0]);
		fclose(f);
		return -1;
	}
	W->fftlen = hdr[1]; W->hop = hdr[2]; W->rate = hdr[3];
	W->nmics = hdr[4]; W->nbeams = hdr[5]; W->nbins = hdr[6];
	W->refchan = hdr[7];

	if (W->nbins != W->fftlen / 2 + 1 || (W->fftlen & (W->fftlen - 1))) {
		fprintf(stderr, "beamform: bad geometry in weights file\n");
		fclose(f);
		return -1;
	}

	W->gains = malloc(W->nmics * sizeof(float));
	W->chanmap = malloc(W->nmics * sizeof(int32_t));
	size_t nw = (size_t)W->nbeams * W->nbins * W->nmics * 2;
	W->w = malloc(nw * sizeof(float));
	if (!W->gains || !W->chanmap || !W->w) {
		fprintf(stderr, "beamform: out of memory\n");
		fclose(f);
		return -1;
	}
	if (fread(W->gains, sizeof(float), W->nmics, f) != W->nmics ||
	    fread(W->chanmap, sizeof(int32_t), W->nmics, f) != W->nmics ||
	    fread(W->w, sizeof(float), nw, f) != nw) {
		fprintf(stderr, "beamform: weights file truncated\n");
		fclose(f);
		return -1;
	}
	fclose(f);
	return 0;
}

/* ----------------------------------------------------------------- main -- */

static void usage(void)
{
	fprintf(stderr,
"usage: biscuit-beamform -w weights.bin [-k miccal] [-c CHANS] [-b BEAM] [-q]\n"
"  -k FILE   per-capsule factory calibration from biscuit-miccal.py\n"
"  -w FILE   weights from export-beam-weights.py (required)\n"
"  -c N      input channel count (default from weights file + 1 for the ref)\n"
"  -b N      pin to beam N; default selects per-beam SNR over 300-3500 Hz\n"
"  -g G      output gain, linear (default 0.25 = -12 dB headroom)\n"
"  -G G      fixed post-selection output-stage gain (default 1.0)\n"
"  -I G      digital input gain before DSP (default 1; stock PGA path: 10)\n"
"  -a        160 ms, DAC-loopback-reference AEC (experimental; measure it)\n"
"  -A        adaptive beamformer cleanup (two blocking references per beam)\n"
"  -n        spectral noise reduction (off by default; stock ASR bypasses NR)\n"
"  -S        stock ASR profile: 128/64 weights, 80 Hz HPF, PGA make-up, AEC\n"
"  -X        with -S, bypass AEC (filterbank/fixed-beam A/B control)\n"
"  -Y        with -S, bypass adaptive beam cleanup (AEC-only A/B control)\n"
"  -O        stock order: run ABF on all beams before VAD/selection\n"
"  -V        recovered per-bin GenericCanceler VSS (AEC and ARA)\n"
"  -v        diagnostic: recovered GenericCanceler VSS for AEC only\n"
"  -W        diagnostic: recovered GenericCanceler VSS for ARA only\n"
"  -R        recovered ABF VSS plus one-reference-per-hop round robin\n"
"  -T        recovered stock RefBeam/SNR selector state machines\n"
"  -U        recovered ARA and exact ABF/ARA smoothed energy vectors\n"
"  -Q        recovery-safe ARA: playback gate plus AEC-VSS double-talk freeze\n"
"  -Z        parity candidate: always-on ARA, ABF VSS, playback-edge reset,\n"
"            double-talk freeze, and the rescaled selector validity gate\n"
"  -t DB     selector validity threshold in dB (diagnostic; default stock 6.5)\n"
"  -N        with -Z, score beams by their rise above their own long-term\n"
"            level instead of the recovered stock signal/noise metric\n"
"  -P        EXPERIMENTAL: enable -O, -V, -R, -T and -U (not a default)\n"
"  -D FILE   write numeric per-hop DSP diagnostics as TSV ('-' = stderr)\n"
"  -C        centre mic only (no beamforming) - the A/B baseline\n"
"  -m N      diagnostic passthrough of mic index N (0..6)\n"
"  -J        diagnostic: reverse the four stock FBF frame taps\n"
"  -K        diagnostic: conjugate the stock FBF coefficients\n"
"  -E        legacy max-energy beam selection (steers at noise; for A/B only)\n"
"  -q        no periodic status on stderr\n"
"\n"
"reads S24_3LE interleaved on stdin, writes mono S16_LE on stdout\n");
}

/*
 * Beam selection.
 *
 * The obvious rule - take the beam with the most output energy - is wrong on
 * this device, and measurably so. Two reasons:
 *
 *   1. It is a max-of-six statistic. With no one speaking it does not pick a
 *      neutral beam, it picks whichever beam happens to be noisiest, every
 *      frame. Measured on 8 s of room ambient: the six beams spanned 10.5 dB
 *      at 100-500 Hz and the energy rule selected the loudest of them in 100%
 *      of frames, which was also the single worst beam in that band.
 *
 *   2. Low frequency dominates the energy sum, and the loudest low-frequency
 *      source here is the device's own amplifier, directly under the mic
 *      board and well inside the near field. So the energy rule spends its
 *      time steering at the speaker rather than at the room.
 *
 * Both are fixed by asking the right question. What matters is not which beam
 * is loudest but which beam has most risen above its own recent floor, over
 * the band where speech lives. Each beam tracks its own noise floor - falls
 * fast, creeps up slowly, the usual min-statistics shape - and selection is
 * argmax of energy over floor.
 *
 * Hysteresis matters more than it looks: the weights differ per beam, so
 * switching mid-utterance puts a discontinuity in the output. A challenger
 * must beat the incumbent by SWITCH_MARGIN_DB before it takes over, which
 * costs nothing when a talker is clearly located and prevents thrash when
 * nobody is.
 */
#define SEL_LO_HZ	300.0f	/* below this is amp hum and room rumble */
#define SEL_HI_HZ	3500.0f	/* above this the mics roll off and add little */
#define FLOOR_DOWN	0.30f	/* weight on a new minimum: track down fast */
#define FLOOR_UP	1.004f	/* per frame, ~0.5 dB/s: creep up slowly */
#define SWITCH_MARGIN_DB 2.0f

static int pick_beam(const float *bandE, double *floorE, int B, int cur,
		     int energy_rule)
{
	if (energy_rule) {
		int best = 0;
		for (int b = 1; b < B; b++)
			if (bandE[b] > bandE[best])
				best = b;
		return best;
	}

	int best = cur < 0 ? 0 : cur;
	float bestSnr = -1e30f;
	for (int b = 0; b < B; b++) {
		double e = bandE[b];
		if (floorE[b] <= 0.0)
			floorE[b] = e;
		else if (e < floorE[b])
			floorE[b] += FLOOR_DOWN * (e - floorE[b]);
		else
			floorE[b] *= FLOOR_UP;

		float snr = 10.0f * log10f((float)(e / (floorE[b] + 1e-30)) + 1e-30f);
		if (b == cur)
			snr += SWITCH_MARGIN_DB;   /* incumbent's advantage */
		if (snr > bestSnr) { bestSnr = snr; best = b; }
	}
	return best;
}

/*
 * Stock's SNR selector constants come from the owner's AFE.cfg.  The update,
 * floor, threshold, hangover, ten-frame plurality vote, and circular tie-break
 * below are our reimplementation of the behaviour observed in stock's SNR beam
 * selector (libasp.so, around 0x43bd0 and 0x43ae6).
 * Keep the recovered path isolated behind -T; the r95 selector above remains
 * the no-flag rollback path.
 */
#define STOCK_SEL_FAST             0.95f
#define STOCK_SEL_SLOW             0.987f
#define STOCK_SEL_ENERGY_RATIO     1.2f
#define STOCK_SEL_NOISE_ADAPT      1.001f
#define STOCK_SEL_BUFFER           10
#define STOCK_SEL_HANGOVER         15
#define STOCK_SEL_SNR_THRESHOLD_DB 6.5f
/*
 * The recovered stock constant is 6.5 dB, but stock's own wake diagnostics
 * report SNR 18-24 at an accepted wake while this implementation measures
 * 1.5-13 on the same acoustics.  The threshold and the metric are therefore
 * not in the same units, so the validity gate never opens in difficult
 * conditions and the selector freezes on whichever beam it last held.
 * Held at the stock value by default so -T and the shipped -O -T path stay
 * byte-identical; -Z and -t rescale it to this implementation's own metric.
 * Measured on the retained difficult-condition array capture: 4/6 accepted
 * at 6.5 dB, 6/6 at 2.0 dB, background false accepts 0 at both.
 */
#define CANDIDATE_SEL_SNR_THRESHOLD_DB 2.0f
static float sel_snr_threshold_db = STOCK_SEL_SNR_THRESHOLD_DB;

/*
 * Selector score: how far a beam is above its OWN long-term level.
 *
 * The recovered stock metric is smoothed signal over a slowly-rising noise
 * floor.  Under self-playback that ranks the beams backwards: measured on a
 * full-volume barge-in capture, the beam pointing away from the speaker had the
 * LOWEST absolute energy (0.15 of the maximum) and the best wake recall
 * (10/10 pinned), while the metric scored it lowest of all and parked the
 * selector on a beam worth 6/10.  A beam that is rejecting the echo is quiet in
 * absolute terms but rises sharply when the talker speaks; a beam full of steady
 * echo does not.  Ranking by that rise reproduced the pinned-beam oracle exactly
 * on both retained captures.
 *
 * The slow constant is ~40 s at the 8 ms update period: long enough to be a
 * room average rather than a speech tracker.  Shorter (0.9995, ~16 s) cost a
 * difficult-condition phrase; longer (0.9999) cost two.
 */
#define SELECTOR_CONTRAST_FAST      0.951f      /* ~160 ms */
#define SELECTOR_CONTRAST_SLOW      0.9998f     /* ~40 s   */
/*
 * Off by default: the recovered stock metric is the shipped scorer.  With the
 * optimised AEC and its 320 ms tail it reaches 9/10 on the retained
 * full-volume barge-in capture, against 10/10 for the contrast score, and it
 * has no startup transient and no known weakness against an intermittent
 * directional interferer.  -N selects the contrast score for anyone who wants
 * the last detection back.
 *
 * A previous revision gated the contrast score on the playback reference. It
 * verified byte-exact offline and then scored 3/10 live - the gate never
 * engaged - so it is deliberately not repeated here.
 */
static int sel_use_contrast;
#define STOCK_SEL_MAX_BEAMS        16
#define STOCK_SEL_UPDATE_PERIOD    2

/*
 * Stock's Knight analysis FFT is unnormalised and its FixedBeamFormer consumes
 * coefs_FBF.cfg exactly as stored.  The current synthesis-safe pmOS FBF loader
 * divides those coefficients by N=128, so its complex beam amplitudes are 128x
 * smaller and its power vectors are N^2 smaller than the units expected by the
 * selector's absolute 1e-7 noise step and 1e-6 signal gate.  Restore only the
 * decision units here.  Do not raise the audio output by 42.14 dB until the
 * AEC/headroom path has a separate clipping gate.
 */
#define STOCK_FBF_AMPLITUDE_UNIT_SCALE 128.0f
#define STOCK_FBF_POWER_UNIT_SCALE \
	(STOCK_FBF_AMPLITUDE_UNIT_SCALE * STOCK_FBF_AMPLITUDE_UNIT_SCALE)
#define STOCK_FBF_POWER2_UNIT_SCALE \
	(STOCK_FBF_POWER_UNIT_SCALE * STOCK_FBF_POWER_UNIT_SCALE)

/*
 * ABF, ARA, and their VSS blocks run after the same synthesis-safe /128 FBF.
 * Preserve the recovered stock constants, but express every absolute power
 * quantity in the retained pmOS representation.  Ratios and dimensionless
 * gates need no conversion.  Cross-power state has power units; a product of
 * two powers (the coherence denominator) has power-squared units.
 */
#define STOCK_POST_FBF_POWER(v)  ((v) / STOCK_FBF_POWER_UNIT_SCALE)
#define STOCK_POST_FBF_POWER2(v) ((v) / STOCK_FBF_POWER2_UNIT_SCALE)

/*
 * RefBeamSelector constants and transition shape, measured from the behaviour
 * of stock's implementation (libasp.so, around 0x4457e) and reimplemented
 * here.  The "indexes" are ranks after a
 * descending sort of VSBABF's per-beam corrEner values, not physical beam
 * numbers.  Its two outputs are reference directions which the downstream
 * SNR selector masks out.  In particular, they are not candidate beams.
 */
#define STOCK_REF_SECOND_RANK       1
#define STOCK_REF_THRESHOLD_RANK    3
#define STOCK_REF_RATIO             0.4f
#define STOCK_REF_SMOOTH_FAST       0.83f
#define STOCK_REF_SMOOTH_SLOW       0.87f
#define STOCK_REF_TRANSITION        80
/* Kept only so the already-tested -O -T candidate remains a byte-for-byte
 * rollback point.  The completed stock trace shows that this old assumption
 * was not the RefBeamSelector metric; -U enables the exact metric below. */
#define LEGACY_REF_POWER_LO_HZ      200.0f
#define LEGACY_REF_POWER_HI_HZ      7000.0f

/* Stock 128-point/16-kHz power-vector stages recovered at 0x54464/0x544e8.
 * VSBABF feeds RefBeamSelector with its K=23/24 power over bins 3..48.
 * ARA feeds SNRBeamSelector with K=0.9875 power over bins 0..63. */
#define STOCK_ABF_POWER_SMOOTH      0.9583333333333333f
#define STOCK_ABF_POWER_BIN_LO      3
#define STOCK_ABF_POWER_BIN_HI      48
#define STOCK_ARA_POWER_SMOOTH      0.9875f
#define STOCK_ARA_POWER_BIN_LO      0
#define STOCK_ARA_POWER_BIN_HI      63

struct stock_ref_rank {
	float metric;
	int beam;
};

struct stock_ref_selector_state {
	float smoothed_threshold;
	int run[STOCK_SEL_MAX_BEAMS];
	int output[2];
	int previous[2];
	int output_count;
	int initialized;
};

static void stock_ref_selector_update(struct stock_ref_selector_state *s,
				      const float *metric, int B,
				      unsigned char *excluded)
{
	struct stock_ref_rank rank[STOCK_SEL_MAX_BEAMS];
	if (B > STOCK_SEL_MAX_BEAMS)
		B = STOCK_SEL_MAX_BEAMS;
	for (int b = 0; b < B; b++) {
		rank[b].metric = metric[b];
		rank[b].beam = b;
	}
	/* Stock uses the equivalent stable insertion sort, descending. */
	for (int i = 1; i < B; i++) {
		struct stock_ref_rank v = rank[i];
		int j = i;
		while (j > 0 && v.metric > rank[j - 1].metric) {
			rank[j] = rank[j - 1];
			j--;
		}
		rank[j] = v;
	}

	int threshold_rank = STOCK_REF_THRESHOLD_RANK < B ?
		STOCK_REF_THRESHOLD_RANK : B - 1;
	int second_rank = STOCK_REF_SECOND_RANK < B ?
		STOCK_REF_SECOND_RANK : 0;
	float threshold = rank[threshold_rank].metric;
	if (!s->initialized) {
		/* The stock vectors are zero-filled; beam zero is consequently the
		 * provisional reference until the first 80-frame transition. */
		s->smoothed_threshold = 0.0f;
		s->output[0] = s->output[1] = 0;
		s->previous[0] = s->previous[1] = 0;
		s->initialized = 1;
	}
	float alpha = threshold > s->smoothed_threshold ?
		STOCK_REF_SMOOTH_FAST : STOCK_REF_SMOOTH_SLOW;
	s->smoothed_threshold = alpha * s->smoothed_threshold +
		(1.0f - alpha) * threshold;

	int top = rank[0].beam;
	s->run[top]++;
	if (s->run[top] >= STOCK_REF_TRANSITION) {
		int next[2] = { top, s->output[1] };
		memset(s->run, 0, sizeof(s->run));
		if (rank[second_rank].metric * STOCK_REF_RATIO >=
		    rank[threshold_rank].metric) {
			next[1] = rank[second_rank].beam;
			s->output_count = 2;
		} else {
			/* Stock leaves the second vector element intact in this branch,
			 * while recording that only one fresh reference was found. */
			s->output_count = 1;
		}
		/* Do not reverse an otherwise unchanged reference pair. */
		if (s->previous[0] == next[1] && s->previous[1] == next[0]) {
			next[0] = s->previous[0];
			next[1] = s->previous[1];
		} else {
			s->previous[0] = next[0];
			s->previous[1] = next[1];
		}
		s->output[0] = next[0];
		s->output[1] = next[1];
	}

	memset(excluded, 0, (size_t)B);
	if (s->output[0] >= 0 && s->output[0] < B)
		excluded[s->output[0]] = 1;
	if (s->output[1] >= 0 && s->output[1] < B)
		excluded[s->output[1]] = 1;
}

struct stock_selector_state {
	float signal[STOCK_SEL_MAX_BEAMS];
	float noise[STOCK_SEL_MAX_BEAMS];
	float cfast[STOCK_SEL_MAX_BEAMS];
	float cslow[STOCK_SEL_MAX_BEAMS];
	float snr_db[STOCK_SEL_MAX_BEAMS];
	int queue[STOCK_SEL_BUFFER];
	int queue_pos;
	int current, residency, changes;
	float current_snr_smooth;
	int hangover;
	int force_accept;
	int update_counter;
	int vad;
	int initialized;
};

static int circular_beam_distance(int a, int b, int B)
{
	int direct = abs(a - b);
	int around = B - direct;
	return direct < around ? direct : around;
}

static int stock_selector_plurality(const struct stock_selector_state *s,
				    int B)
{
	int counts[STOCK_SEL_MAX_BEAMS] = {0};
	for (int i = 0; i < STOCK_SEL_BUFFER; i++)
		if (s->queue[i] >= 0 && s->queue[i] < B)
			counts[s->queue[i]]++;

	int most = 0;
	for (int b = 0; b < B; b++)
		if (counts[b] > most)
			most = counts[b];

	/* 0x43ae6 resolves equal vote counts by the shortest circular distance
	 * from the incumbent, then by physical beam index. */
	int best = 0, best_distance = 100;
	for (int b = 0; b < B; b++) {
		if (counts[b] != most)
			continue;
		int distance = circular_beam_distance(b, s->current, B);
		if (distance < best_distance) {
			best = b;
			best_distance = distance;
		}
	}
	return best;
}

static int stock_selector_pick(struct stock_selector_state *s,
			       const float *bandE, const unsigned char *excluded,
			       int B, int *vad_out)
{
	if (!s->initialized) {
		/* The stock constructor zero-fills every state vector and starts on
		 * physical beam zero.  In particular, it does not seed beam one from
		 * wake observations. */
		s->current = 0;
		s->initialized = 1;
	}

	/* The live Biscuit object stores an update period of two at +0x10.
	 * 0x43bd0 increments that counter and returns before touching any selector
	 * state on the intervening 4 ms hop. */
	s->update_counter++;
	if (s->update_counter < STOCK_SEL_UPDATE_PERIOD) {
		if (vad_out)
			*vad_out = s->vad;
		return s->current;
	}
	s->update_counter = 0;

	float best_snr = -100.0f;
	int best = s->current;
	if (excluded && excluded[s->current]) {
		/* Active Biscuit passes RefBeamSelector's two outputs as a pre-built
		 * exclusion mask.  0x43bd0 invalidates an excluded incumbent and
		 * clears its hangover, but leaves the queue to age out naturally. */
		s->snr_db[s->current] = -100.0f;
		s->hangover = 0;
	}

	for (int b = 0; b < B; b++) {
		if (excluded && excluded[b])
			continue;
		float e = fmaxf(bandE[b], 1e-30f);
		float alpha = e > STOCK_SEL_ENERGY_RATIO * s->signal[b] ?
			STOCK_SEL_FAST : STOCK_SEL_SLOW;
		float signal = alpha * s->signal[b] + (1.0f - alpha) * e;
		float noise_ceiling = fmaxf(
			s->noise[b] * STOCK_SEL_NOISE_ADAPT,
			s->noise[b] + 1e-7f);
		float noise = fminf(signal, noise_ceiling);
		s->signal[b] = signal;
		s->noise[b] = noise;
		float legacy_db = (signal >= 0.0f && noise >= 0.0f) ?
			10.0f * log10f(signal / fmaxf(noise, 1e-30f)) : 0.0f;
		if (sel_use_contrast) {
			if (s->cslow[b] <= 0.0f) {
				s->cslow[b] = e;
				s->cfast[b] = e;
			}
			s->cfast[b] = SELECTOR_CONTRAST_FAST * s->cfast[b] +
				(1.0f - SELECTOR_CONTRAST_FAST) * e;
			s->cslow[b] = SELECTOR_CONTRAST_SLOW * s->cslow[b] +
				(1.0f - SELECTOR_CONTRAST_SLOW) * e;
		}
		s->snr_db[b] = sel_use_contrast ? 10.0f * log10f(
			fmaxf(s->cfast[b], 1e-30f) /
			fmaxf(s->cslow[b], 1e-30f)) : legacy_db;
		if (s->snr_db[b] > best_snr) {
			best_snr = s->snr_db[b];
			best = b;
		}
	}

	float current_snr = s->snr_db[s->current];
	s->current_snr_smooth = 0.5f *
		(s->current_snr_smooth + current_snr);
	if (current_snr >= best_snr) {
		best = s->current;
		best_snr = current_snr;
	}
	s->queue[s->queue_pos] = best;

	int valid = best_snr > sel_snr_threshold_db &&
		s->signal[best] >= 1e-6f;
	if (!valid) {
		s->hangover = 1;
		s->force_accept = 1;
	} else if (s->hangover > 0) {
		s->hangover--;
	} else {
		int next = s->current;
		int plurality_update = 0;
		if (s->force_accept) {
			s->force_accept = 0;
			next = best;
		} else if (best != s->current &&
			   best_snr >= s->current_snr_smooth) {
			next = stock_selector_plurality(s, B);
			plurality_update = 1;
		}
		if (next != s->current) {
			s->current = next;
			s->changes++;
			s->residency = 0;
		}
		/* Stock resets this IIR only after the plurality helper.  A forced
		 * post-silence accept changes the beam without this assignment. */
		if (plurality_update)
			s->current_snr_smooth = s->snr_db[s->current];
		s->hangover = STOCK_SEL_HANGOVER;
	}

	s->queue_pos++;
	if (s->queue_pos == STOCK_SEL_BUFFER)
		s->queue_pos = 0;
	int vad = valid;
	s->vad = vad;
	if (vad_out)
		*vad_out = vad;

	s->residency++;
	return s->current;
}

/*
 * Diagnostic-only cross-spectral state.  A single-bin complex product is
 * another coherence identity, so -D instead smooths the auto- and
 * cross-spectra independently for about 100 ms before forming coherence.
 * Nothing in this state feeds AEC, ABF, beam selection, or output samples.
 */
#define AEC_DIAG_SMOOTH    0.96f

/* GenericCanceler VSS, recovered from 0x58e28.  State and coefficient gates
 * are per input/reference/frequency bin.  The cross state is intentionally
 * initialized to 1+0j by stock (0x58c1a); the stored gate starts at zero and
 * the decision made after one hop controls adaptation on the next hop. */
#define STOCK_GENERIC_VSS_SMOOTH   0.9875f
#define STOCK_GENERIC_VSS_COH_CAP  0.999f
#define STOCK_GENERIC_VSS_EPS      1e-20f
#define STOCK_GENERIC_VSS_PWR_EPS  1e-8f
#define STOCK_GENERIC_VSS_MIN      0.1f
#define STOCK_GENERIC_VSS_MAX      0.99f
#define STOCK_GENERIC_VSS_REF_THD  1e-6f
#define STOCK_GENERIC_VSS_REL_EPS  1e-10f
#define STOCK_GENERIC_VSS_ORDER1   5.0f
#define STOCK_GENERIC_VSS_ORDER2   5.0f

struct stock_generic_vss {
	int inputs, refs, nbins;
	/* AEC consumes pre-FBF spectra in stock units. ARA consumes the retained
	 * /128 FBF spectra, so its power and cross-power states use 1/128^2 of
	 * the stock unit. Keep this per instance because the updater is shared. */
	float power_unit;
	float *input_power;              /* [input][bin] */
	float *ref_power;                /* [reference][bin] */
	cpx *cross;                      /* [input][reference][bin] */
	float *step;                     /* per-reference order statistic */
	float *combined;                 /* capped next-hop [input][bin] gate */
};

static void stock_generic_vss_init(struct stock_generic_vss *s)
{
	size_t pairs = (size_t)s->inputs * s->refs * s->nbins;
	for (size_t i = 0; i < pairs; i++)
		s->cross[i].re = s->power_unit;
}

static void stock_generic_vss_reset(struct stock_generic_vss *s)
{
	memset(s->input_power, 0,
		(size_t)s->inputs * s->nbins * sizeof(*s->input_power));
	memset(s->ref_power, 0,
		(size_t)s->refs * s->nbins * sizeof(*s->ref_power));
	memset(s->cross, 0,
		(size_t)s->inputs * s->refs * s->nbins * sizeof(*s->cross));
	memset(s->step, 0,
		(size_t)s->inputs * s->refs * s->nbins * sizeof(*s->step));
	memset(s->combined, 0,
		(size_t)s->inputs * s->nbins * sizeof(*s->combined));
	stock_generic_vss_init(s);
}

static void stock_generic_vss_update_refs(struct stock_generic_vss *s,
					  const cpx *refs)
{
	const float keep = STOCK_GENERIC_VSS_SMOOTH;
	const float add = 1.0f - keep;
	for (int r = 0; r < s->refs; r++)
		for (int k = 0; k < s->nbins; k++) {
			size_t i = (size_t)r * s->nbins + k;
			s->ref_power[i] = keep * s->ref_power[i] +
				add * cpx_power(refs[i]);
		}
}

static void stock_generic_vss_update_input_bin(struct stock_generic_vss *s,
					       int input, int k, cpx look,
					       const cpx *refs)
{
	const float keep = STOCK_GENERIC_VSS_SMOOTH;
	const float add = 1.0f - keep;
	size_t xi = (size_t)input * s->nbins + k;
	s->input_power[xi] = keep * s->input_power[xi] +
		add * cpx_power(look);
	float max_ref = -1.0f;
	for (int r = 0; r < s->refs; r++) {
		float p = s->ref_power[(size_t)r * s->nbins + k];
		if (p > max_ref) max_ref = p;
	}

	float combined = 0.0f;
	for (int r = 0; r < s->refs; r++) {
		size_t ri = (size_t)r * s->nbins + k;
		size_t pi = ((size_t)input * s->refs + r) * s->nbins + k;
		float pr = s->ref_power[ri];
		if (pr > STOCK_GENERIC_VSS_REF_THD * s->power_unit) {
			cpx instant = cpx_mul_conj_right(look, refs[ri]);
				s->cross[pi].re = keep * s->cross[pi].re + add * instant.re;
				s->cross[pi].im = keep * s->cross[pi].im + add * instant.im;
		}
		float coherence = cpx_power(s->cross[pi]) /
				(s->input_power[xi] * pr + STOCK_GENERIC_VSS_EPS *
				 s->power_unit * s->power_unit);
		if (coherence > STOCK_GENERIC_VSS_COH_CAP)
			coherence = STOCK_GENERIC_VSS_COH_CAP;

		/* Same scalar shape as observed at 0x59036..0x590d8. */
		float order = ((1.0f + coherence *
				(STOCK_GENERIC_VSS_ORDER1 - 1.0f)) *
				(pr + STOCK_GENERIC_VSS_PWR_EPS * s->power_unit)) /
				(pr + STOCK_GENERIC_VSS_PWR_EPS * s->power_unit +
				 STOCK_GENERIC_VSS_ORDER2 * (1.0f - coherence) * pr);
		if (order > STOCK_GENERIC_VSS_ORDER1)
			order = STOCK_GENERIC_VSS_ORDER1;
		float relative = pr /
			(max_ref + STOCK_GENERIC_VSS_REL_EPS * s->power_unit);
		if (relative < 0.5f) relative = 0.5f;
		float gate = fmaxf(order, STOCK_GENERIC_VSS_MIN) *
				relative * order;
		s->step[pi] = gate;
		combined += gate;
	}
	if (combined > STOCK_GENERIC_VSS_MAX)
		combined = STOCK_GENERIC_VSS_MAX;
	/* 0x590ea stores this at input-state +0x50.  The adaptive core loads that
	 * exact vector at 0x599b6; the larger per-reference value above is an
	 * internal order statistic, not a coefficient step. */
	s->combined[xi] = combined;
}

static void stock_generic_vss_update_input(struct stock_generic_vss *s,
					   int input, const cpx *look,
					   const cpx *refs)
{
	for (int k = 0; k < s->nbins; k++)
		stock_generic_vss_update_input_bin(s, input, k, look[k], refs);
}

/* Stock AFE::process enables ARA only while the smoothed physical playback
 * reference exceeds 1e-8 (or while its explicit TTS flag is set).  The
 * detector at 0x259ec is sample-domain power with K=0.999.  The old -U path
 * omitted this gate and consequently trained ARA on near-end speech even in
 * silence, then excluded those learned directions from SNR selection.
 *
 * Stock also disables ARA adaptation while the application reports attenuated
 * playback.  pmOS has no equivalent flag at this DSP boundary, so -Q derives
 * a conservative freeze from the already-recovered AEC GenericCanceler VSS.
 * Thresholds/hangover are fixed from the retained full-volume trace: they
 * froze 97.1% of reference-active labelled speech hops and 19.9% of matched
 * no-speech control hops.  This controller remains opt-in and its decision is
 * consumed on the following hop, like stock VSS. */
/* NLMS step safety margin below stock's nominal 0.2 maximum; see the AEC
 * adaptation step for why this is not the partition count.  Measured on the
 * retained full-volume playback capture, 2 gave the best steady-state ERLE
 * (19.0 dB against a 21-24 dB optimal-linear ceiling) with fewer divergence
 * candidates per hop than stock's flat 0.2. */
#define AEC_STEP_PARTITION_MARGIN     2.0f

/* The AEC may adapt a bin only while the playback reference could plausibly
 * explain what that microphone hears: the reference energy across the whole
 * tail must be at least this multiple of the bin's microphone power.
 *
 * Before this, the only gate was "reference not exactly zero".  Through the
 * fade at the end of a song the codec stays open and the reference falls ~70
 * dB without reaching zero, so every bin kept taking full-size NLMS steps -
 * normalised by a reference that was almost nothing - and fitted whatever the
 * room was saying to it.  On the 2026-09-24 barge-in capture the filter gain
 * rose ~35 dB in eight seconds, and at the next track's onset ERLE went to
 * -27 dB: the canceller injected echo, for ~20 s, and pmOS lost six of ten
 * wake words from a converged state.  The adaptive beamformer's canceller has
 * always carried the same relative form (rpower > 0.15 * |y|^2).
 *
 * The recovered-VSS path (-V, and -Z through it) keeps its own adaptation
 * control and is left byte-stable as the rollback.
 *
 * 1.0 is measured, on the five takes replayed back to back.  It is the least
 * gating that removes the injection: take 5's worst one-second ERLE goes from
 * -24.6 dB to +0.2 dB, its output drops 12 dB, and the canceller is back above
 * 9 dB within ~5 s of the next track instead of ~20 s - while the converged
 * takes are untouched (12.9 dB ERLE either way).  2-5 buy another 0.5 dB on
 * take 5; 10 and 100 starve convergence (adaptation falls to 80-97% of bins
 * during music, and the cold take loses its early prompts).  With no playback
 * the gate never matters: output is byte-identical on the quiet takes. */
#define AEC_REF_PRESENCE              1.0f

/*
 * AEC echo tail.  Stock configures 2,560 samples (160 ms), but its AEC also
 * varies the tail per frequency bin (m_bandBasedTailLen, confirmed in
 * libasp.so) so the long tails cost only where the echo path is long.  We do
 * not have that table, so we run a uniform tail and buy the same headroom from
 * the optimised inner loop instead.
 *
 * A fullband least-squares fit says 640 taps captures the echo path to within
 * 1.4 dB of 2,560 - but the 640-tap analysis prototype smears that path in the
 * subband domain, so shorter subband tails measure worse and longer ones
 * better: 160 ms gives 16.3 dB steady-state ERLE and 320 ms gives 19.0 dB.
 * With the optimised loop this costs RT 0.77 under playback, against 0.82 for
 * the old loop at half the tail.
 */
#define AEC_TAIL_SAMPLES              5120
#define STOCK_REF_ACTIVITY_KEEP       0.999f
#define STOCK_REF_ACTIVITY_THRESHOLD  1e-8f
#define RECOVERY_DTD_ATTACK_THRESHOLD  0.30f
#define RECOVERY_DTD_RELEASE_THRESHOLD 0.50f
#define RECOVERY_DTD_ATTACK_HOPS       8
#define RECOVERY_DTD_HANGOVER_HOPS     25

struct recovery_controller {
	float reference_power;
	int reference_active;
	int reference_edge;
	int dtd_attack_hops;
	int dtd_hangover_hops;
	int freeze;
	long reference_onsets;
	long freeze_events;
};

static void recovery_update_reference(struct recovery_controller *s,
				      const float *reference, int samples)
{
	int was_active = s->reference_active;
	s->reference_edge = 0;
	for (int i = 0; i < samples; i++)
		s->reference_power = STOCK_REF_ACTIVITY_KEEP * s->reference_power +
			(1.0f - STOCK_REF_ACTIVITY_KEEP) * reference[i] * reference[i];
	s->reference_active =
		s->reference_power >= STOCK_REF_ACTIVITY_THRESHOLD;
	if (was_active != s->reference_active) {
		/* Both edges matter.  r101 proved that plain ARA adapts into a
		 * destructive state during playback and keeps cancelling wake words
		 * in the silence afterwards, so the falling edge needs the same clean
		 * start as the rising one. */
		s->reference_edge = 1;
		if (s->reference_active)
			s->reference_onsets++;
	}
	if (!s->reference_active) {
		s->dtd_attack_hops = 0;
		s->dtd_hangover_hops = 0;
		s->freeze = 0;
	}
}

static void recovery_update_doubletalk(struct recovery_controller *s,
				       float aec_vss_mean)
{
	if (!s->reference_active || aec_vss_mean < 0.0f) {
		s->dtd_attack_hops = 0;
		s->dtd_hangover_hops = 0;
		s->freeze = 0;
		return;
	}
	if (aec_vss_mean <= RECOVERY_DTD_ATTACK_THRESHOLD) {
		if (s->dtd_attack_hops < RECOVERY_DTD_ATTACK_HOPS)
			s->dtd_attack_hops++;
	} else if (aec_vss_mean >= RECOVERY_DTD_RELEASE_THRESHOLD)
		s->dtd_attack_hops = 0;

	if (s->dtd_attack_hops >= RECOVERY_DTD_ATTACK_HOPS) {
		if (!s->freeze)
			s->freeze_events++;
		s->dtd_hangover_hops = RECOVERY_DTD_HANGOVER_HOPS;
	} else if (s->dtd_hangover_hops > 0) {
		s->dtd_hangover_hops--;
	}
	s->freeze = s->dtd_hangover_hops > 0;
}

/* VSBABF VSS and round-robin state recovered from 0x56f74 and 0x56f1c. */
#define STOCK_ABF_VSS_BIN_LO        8       /* 1000 Hz */
#define STOCK_ABF_VSS_BIN_HI        48      /* 6000 Hz */
#define STOCK_ABF_VSS_RATIO_MIN     0.0316227766f
#define STOCK_ABF_VSS_RATIO_MAX     31.6227766f
#define STOCK_ABF_VSS_MIDPOINT      0.75f
#define STOCK_ABF_VSS_SLOPE         30.0f
#define STOCK_ABF_VSS_STATE_KEEP    0.93f
#define STOCK_ABF_VSS_EPS           STOCK_POST_FBF_POWER(2.5e-4f)
#define STOCK_POST_FBF_MIN_POWER     STOCK_POST_FBF_POWER(1e-10f)
#define STOCK_ABF_REGULARIZER        STOCK_POST_FBF_POWER(2.5e-4f)
#define STOCK_ARA_REGULARIZER        STOCK_POST_FBF_POWER(1e-5f)

struct stock_abf_vss {
	float *look_power;                /* [beam][bin] */
	float *ref_power;                 /* [beam][reference][bin] */
	float *ratio_state;               /* [beam][reference] */
	float *step;                      /* next-hop [beam][reference][bin] */
	int round_robin_ref;
};

static void stock_abf_vss_update(struct stock_abf_vss *s, const cpx *look,
				  const cpx *history, int beams, int parts,
				  int nbins, int history_pos)
{
	const float keep = STOCK_ABF_POWER_SMOOTH;
	const float add = 1.0f - keep;
	for (int b = 0; b < beams; b++) {
		for (int k = 0; k < nbins; k++) {
			size_t li = (size_t)b * nbins + k;
			s->look_power[li] = keep * s->look_power[li] +
				add * cpx_power(look[li]);
			for (int r = 0; r < 2; r++) {
				const cpx *hist = history +
					((size_t)(b * 2 + r) * 2 * parts * nbins) +
					(size_t)(history_pos + 1) * nbins;
				size_t ri = ((size_t)b * 2 + r) * nbins + k;
				s->ref_power[ri] = keep * s->ref_power[ri] + add *
					cpx_power(hist[(size_t)(parts - 1) * nbins + k]);
			}
		}
		for (int r = 0; r < 2; r++) {
			double mean = 0.0;
			for (int k = STOCK_ABF_VSS_BIN_LO;
			     k <= STOCK_ABF_VSS_BIN_HI && k < nbins; k++) {
				float lp = s->look_power[(size_t)b * nbins + k];
				float rp = s->ref_power[((size_t)b * 2 + r) * nbins + k];
				float ratio = lp / (rp + STOCK_ABF_VSS_EPS);
				if (ratio < STOCK_ABF_VSS_RATIO_MIN)
					ratio = STOCK_ABF_VSS_RATIO_MIN;
				if (ratio > STOCK_ABF_VSS_RATIO_MAX)
					ratio = STOCK_ABF_VSS_RATIO_MAX;
				mean += ratio;
			}
			mean /= (STOCK_ABF_VSS_BIN_HI - STOCK_ABF_VSS_BIN_LO + 1);
			size_t si = (size_t)b * 2 + r;
			s->ratio_state[si] = STOCK_ABF_VSS_STATE_KEEP * s->ratio_state[si] +
				(1.0f - STOCK_ABF_VSS_STATE_KEEP) * (float)mean;
			float x = STOCK_ABF_VSS_SLOPE *
				(s->ratio_state[si] - STOCK_ABF_VSS_MIDPOINT);
			float gate = 0.5f * (1.0f - x / (1.0f + fabsf(x)));
			if (gate < 0.0f) gate = 0.0f;
			if (gate > 1.0f) gate = 1.0f;
			for (int k = 0; k < nbins; k++) {
				float rp = s->ref_power[si * nbins + k];
				s->step[si * nbins + k] = gate *
					rp / (rp + STOCK_ABF_VSS_EPS);
			}
		}
	}
	/* One reference is allowed to adapt on the following hop. Prediction
	 * continues to use both references. */
	s->round_robin_ref ^= 1;
}

static void update_stock_power_vector(float *smooth, const cpx *spectra,
				      float *metric, int beams, int nbins,
				      float keep, int k0, int k1)
{
	float add = 1.0f - keep;
	if (k1 >= nbins) k1 = nbins - 1;
	for (int b = 0; b < beams; b++) {
		double sum = 0.0;
		for (int k = 0; k < nbins; k++) {
			size_t i = (size_t)b * nbins + k;
			smooth[i] = keep * smooth[i] + add * cpx_power(spectra[i]);
			if (k >= k0 && k <= k1)
				sum += smooth[i];
		}
		metric[b] = (float)sum;
	}
}

static void process_abf_beam(const cpx *look, cpx *cleaned, int beam,
			     cpx *history, cpx *filters, int parts,
			     int nbins, int history_pos, int nfft, int sample_rate,
			     int correct_fbf_units, int use_vss,
			     const float *vss_step, int active_ref,
			     long *adapted_total, long *adapted_hop)
{
	int k0 = (int)(200.0f * nfft / sample_rate + 0.5f);
	int k1 = (int)(7000.0f * nfft / sample_rate + 0.5f);
	/* Keep the accepted r98 -O -T rollback byte-stable. The exact unit
	 * conversion is enabled only for the experimental ARA/ABF-VSS branches
	 * whose absolute post-FBF constants it repairs. */
	float min_power = correct_fbf_units ?
		STOCK_POST_FBF_MIN_POWER : 1e-10f;
	float regularizer = correct_fbf_units ?
		STOCK_ABF_REGULARIZER : 2.5e-4f;
	for (int k = 0; k < nbins; k++)
		cleaned[k] = look[k];
	for (int k = k0; k <= k1 && k < nbins; k++) {
		cpx y = look[k], estimate = { 0.0f, 0.0f };
		float rpower = 0.0f;
		for (int r = 0; r < 2; r++) {
			cpx *hist = history +
				((size_t)(beam * 2 + r) * 2 * parts * nbins) +
				(size_t)(history_pos + 1) * nbins;
			cpx *hh = filters +
				((size_t)(beam * 2 + r) * parts * nbins);
			for (int p = 0; p < parts; p++) {
				cpx ref = hist[(size_t)p * nbins + k];
				estimate = cpx_add(estimate,
					cpx_mul(hh[(size_t)p * nbins + k], ref));
				rpower += cpx_power(ref);
			}
		}
		cpx e = cpx_sub(y, estimate);
		int adapt = use_vss ? rpower > min_power :
			rpower > cpx_power(y) * 0.15f &&
			rpower > min_power;
		if (adapt) {
			for (int r = 0; r < 2; r++) {
				float scale = use_vss ? vss_step[(size_t)r * nbins + k] : 1.0f;
				if (use_vss && (r != active_ref || scale <= 0.001f))
					continue;
				float step = 0.1f * scale /
					(2.0f * parts * (rpower + regularizer));
				cpx *hist = history +
					((size_t)(beam * 2 + r) * 2 * parts * nbins) +
					(size_t)(history_pos + 1) * nbins;
				cpx *hh = filters +
					((size_t)(beam * 2 + r) * parts * nbins);
				for (int p = 0; p < parts; p++) {
					cpx grad = cpx_mul_conj_right(e,
						hist[(size_t)p * nbins + k]);
					hh[(size_t)p * nbins + k].re += step * grad.re;
					hh[(size_t)p * nbins + k].im += step * grad.im;
				}
				(*adapted_total)++;
				(*adapted_hop)++;
			}
		}
		cleaned[k] = e;
	}
}

/* Acoustic Reference Arbitrator.  Despite its name this is a second
 * two-reference GenericCanceler: RefBeamSelector chooses two ABF outputs,
 * then ARA removes those directions from every beam before SNR selection.
 * Its 2,560-sample tail, 0.2 step and 1e-5 regularizer are from AFE.cfg. */
static void calculate_ara_reference_power(float *reference_power,
					  cpx *history, int parts, int nbins,
					  int history_pos)
{
	/* The two ARA references are shared by every input beam, so their NLMS
	 * denominator is identical for all six beams. Preserve the original
	 * reference/partition accumulation order exactly. */
	for (int k = 0; k < nbins - 1; k++) {
		float power = 0.0f;
		for (int r = 0; r < 2; r++) {
			cpx *hist = history + (size_t)r * 2 * parts * nbins +
				(size_t)(history_pos + 1) * nbins;
			for (int p = 0; p < parts; p++)
				power += cpx_power(hist[(size_t)p * nbins + k]);
		}
		reference_power[k] = power;
	}
}

static long process_ara_beam_range(const cpx *look, cpx *cleaned,
				   int beam_first, int beam_end,
				   cpx *history, cpx *filters, int parts,
				   int nbins, int history_pos, int use_vss,
				   int adapt_allowed,
				   const struct stock_generic_vss *vss,
				   const float *reference_power)
{
	long adapted = 0;
	for (int b = beam_first; b < beam_end; b++) {
		for (int k = 0; k < nbins; k++)
			cleaned[(size_t)b * nbins + k] = look[(size_t)b * nbins + k];
		/* Stock has 64 complex bands; the allocated 65th real-FFT Nyquist bin
		 * is deliberately outside the GenericCanceler. */
		for (int k = 0; k < nbins - 1; k++) {
			cpx y = look[(size_t)b * nbins + k];
			cpx estimate = { 0.0f, 0.0f };
			float rpower = reference_power[k];
			for (int r = 0; r < 2; r++) {
				/* ARA's two selected references are common to all six
				 * inputs. Only the adaptive filters are input-specific. */
				cpx *hist = history + (size_t)r * 2 * parts * nbins +
					(size_t)(history_pos + 1) * nbins;
				cpx *hh = filters +
					((size_t)(b * 2 + r) * parts * nbins);
				for (int p = 0; p < parts; p++) {
					cpx ref = hist[(size_t)p * nbins + k];
					estimate = cpx_add(estimate,
						cpx_mul(hh[(size_t)p * nbins + k], ref));
				}
			}
			cpx e = cpx_sub(y, estimate);
			if (!adapt_allowed) {
				cleaned[(size_t)b * nbins + k] = e;
				continue;
			}
			for (int r = 0; r < 2; r++) {
				float scale = 1.0f;
				if (use_vss) {
					scale = vss->combined[(size_t)b * nbins + k];
					if (scale <= 0.001f ||
					    rpower <= STOCK_POST_FBF_MIN_POWER)
						continue;
				} else if (!(rpower > cpx_power(y) * 0.15f &&
					     rpower > STOCK_POST_FBF_MIN_POWER)) {
					continue;
				}
				float step = 0.2f * scale /
					(2.0f * parts * (rpower + STOCK_ARA_REGULARIZER));
				cpx *hist = history + (size_t)r * 2 * parts * nbins +
					(size_t)(history_pos + 1) * nbins;
				cpx *hh = filters +
					((size_t)(b * 2 + r) * parts * nbins);
				for (int p = 0; p < parts; p++) {
					cpx grad = cpx_mul_conj_right(e,
						hist[(size_t)p * nbins + k]);
					hh[(size_t)p * nbins + k].re += step * grad.re;
					hh[(size_t)p * nbins + k].im += step * grad.im;
				}
				adapted++;
			}
			cleaned[(size_t)b * nbins + k] = e;
		}
	}
	return adapted;
}

static void process_ara_beams(const cpx *look, cpx *cleaned, int beams,
			      cpx *history, cpx *filters, int parts,
			      int nbins, int history_pos, int use_vss,
			      int adapt_allowed,
			      const struct stock_generic_vss *vss,
			      long *adapted_total, long *adapted_hop)
{
	float reference_power[nbins];
	calculate_ara_reference_power(reference_power, history, parts, nbins,
		history_pos);
	long adapted = process_ara_beam_range(look, cleaned, 0, beams,
		history, filters, parts, nbins, history_pos, use_vss,
		adapt_allowed, vss,
		reference_power);
	*adapted_total += adapted;
	*adapted_hop += adapted;
}

/* The stock AFE ran on dedicated DSP resources. On pmOS the exact six-beam
 * ARA runs on the application CPUs, so divide only the independent beam
 * filters across persistent workers. Barriers publish one hop at a time;
 * no worker can observe reference/VSS changes from the following stages. */
#define ARA_PARALLEL_WORKERS 2

struct ara_parallel;

struct ara_worker {
	struct ara_parallel *owner;
	int id;
};

struct ara_parallel {
	pthread_t threads[ARA_PARALLEL_WORKERS];
	pthread_barrier_t start;
	pthread_barrier_t done;
	struct ara_worker workers[ARA_PARALLEL_WORKERS];
	int ready;
	int stop;
	const cpx *look;
	cpx *cleaned;
	cpx *history;
	cpx *filters;
	int beams;
	int parts;
	int nbins;
	int history_pos;
	int use_vss;
	int adapt_allowed;
	const struct stock_generic_vss *vss;
	float *reference_power;
	long adapted[ARA_PARALLEL_WORKERS];
};

static void *ara_parallel_worker(void *opaque)
{
	struct ara_worker *worker = opaque;
	struct ara_parallel *p = worker->owner;
	for (;;) {
		pthread_barrier_wait(&p->start);
		if (p->stop)
			break;
		int first = worker->id * p->beams / (ARA_PARALLEL_WORKERS + 1);
		int end = (worker->id + 1) * p->beams /
			(ARA_PARALLEL_WORKERS + 1);
		p->adapted[worker->id] = process_ara_beam_range(p->look,
			p->cleaned, first, end, p->history, p->filters, p->parts,
			p->nbins, p->history_pos, p->use_vss, p->adapt_allowed,
			p->vss,
			p->reference_power);
		pthread_barrier_wait(&p->done);
	}
	return NULL;
}

static int ara_parallel_init(struct ara_parallel *p, int beams, int parts,
			     int nbins, cpx *history, cpx *filters,
			     int use_vss, const struct stock_generic_vss *vss)
{
	memset(p, 0, sizeof(*p));
	p->beams = beams;
	p->parts = parts;
	p->nbins = nbins;
	p->history = history;
	p->filters = filters;
	p->use_vss = use_vss;
	p->vss = vss;
	p->reference_power = calloc((size_t)nbins, sizeof(float));
	if (!p->reference_power)
		return -1;
	if (pthread_barrier_init(&p->start, NULL, ARA_PARALLEL_WORKERS + 1) != 0 ||
	    pthread_barrier_init(&p->done, NULL, ARA_PARALLEL_WORKERS + 1) != 0)
		return -1;
	for (int i = 0; i < ARA_PARALLEL_WORKERS; i++) {
		p->workers[i].owner = p;
		p->workers[i].id = i;
		if (pthread_create(&p->threads[i], NULL, ara_parallel_worker,
				   &p->workers[i]) != 0)
			return -1;
	}
	p->ready = 1;
	return 0;
}

static void ara_parallel_process(struct ara_parallel *p, const cpx *look,
				 cpx *cleaned, int history_pos, int adapt_allowed,
				 long *adapted_total, long *adapted_hop)
{
	calculate_ara_reference_power(p->reference_power, p->history, p->parts,
		p->nbins, history_pos);
	p->look = look;
	p->cleaned = cleaned;
	p->history_pos = history_pos;
	p->adapt_allowed = adapt_allowed;
	pthread_barrier_wait(&p->start);
	int first = ARA_PARALLEL_WORKERS * p->beams /
		(ARA_PARALLEL_WORKERS + 1);
	long adapted = process_ara_beam_range(look, cleaned, first, p->beams,
		p->history, p->filters, p->parts, p->nbins, history_pos,
		p->use_vss, adapt_allowed, p->vss, p->reference_power);
	pthread_barrier_wait(&p->done);
	for (int i = 0; i < ARA_PARALLEL_WORKERS; i++)
		adapted += p->adapted[i];
	*adapted_total += adapted;
	*adapted_hop += adapted;
}

static void ara_parallel_destroy(struct ara_parallel *p)
{
	if (!p->ready)
		return;
	p->stop = 1;
	pthread_barrier_wait(&p->start);
	for (int i = 0; i < ARA_PARALLEL_WORKERS; i++)
		pthread_join(p->threads[i], NULL);
	pthread_barrier_destroy(&p->done);
	pthread_barrier_destroy(&p->start);
	free(p->reference_power);
	p->ready = 0;
}

/*
 * Fold the factory per-capsule calibration into the per-mic gains.
 *
 * The capsules are not identical - about 2.9 dB between the quietest and the
 * loudest on the units measured - and an array whose elements disagree by that
 * much has blunted directivity, because the fixed weights assume matched
 * elements. biscuit-miccal.py turns IDME's miccal.0..6 Q14 coefficients into
 * one stock-direction gain per INPUT CHANNEL (miccal.N / 16384).
 *
 * Folding into W.gains[] rather than scaling in the sample loop is deliberate:
 * both the beamformed path and the centre-mic path already multiply by
 * W.gains[m], so calibration reaches them both with no change to the hot loop
 * and no chance of the two paths disagreeing.
 *
 * Indexed by W.chanmap[m], not by m: the weights file is free to order its
 * mics however it likes, and the calibration is per physical channel.
 */
static int apply_miccal(const char *path, struct weights *W, int quiet)
{
	FILE *f = fopen(path, "r");
	if (!f) {
		fprintf(stderr, "beamform: %s: cannot open, running uncalibrated\n",
			path);
		return -1;
	}
	float cal[MAX_CAL_CHANS];
	int n = 0;
	while (n < MAX_CAL_CHANS && fscanf(f, "%f", &cal[n]) == 1)
		n++;
	fclose(f);
	if (n < 1) {
		fprintf(stderr, "beamform: %s: no values, running uncalibrated\n",
			path);
		return -1;
	}
	int applied = 0;
	for (unsigned m = 0; m < W->nmics; m++) {
		int ch = W->chanmap[m];
		/*
		 * A channel with no calibration keeps unity rather than borrowing
		 * a neighbour's - silently applying the wrong capsule's trim
		 * would be worse than applying none.
		 */
		if (ch < 0 || ch >= n)
			continue;
		if (!(cal[ch] > 0.0f))
			continue;
		W->gains[m] *= cal[ch];
		applied++;
	}
	if (!quiet)
		fprintf(stderr, "beamform: calibration from %s, %d of %u mics\n",
			path, applied, W->nmics);
	return 0;
}

int main(int argc, char **argv)
{
	const char *wpath = NULL;
	const char *calpath = NULL;
	const char *diagpath = NULL;
	int pinned = -1, inchans = -1, quiet = 0, c;
	int energy_rule = 0, use_aec = 0, use_abf = 0, use_nr = 0, stock_profile = 0;
	int bypass_aec = 0, bypass_abf = 0;
	int stock_order = 0, use_aec_vss = 0, use_ara_vss = 0;
	int use_abf_vss = 0, stock_selector = 0;
	int use_ara = 0, use_recovery_controller = 0;
	/* -Q gates ARA off whenever the playback reference is idle.  On a silent
	 * reference that makes it byte-identical to -O -T, which is exactly the
	 * condition where ARA is the win, so -Z keeps ARA running always and
	 * relies on the freeze and the edge reset for playback safety instead. */
	int ara_always_active = 0;
	/* Compensates the measured 20 dB stock-PGA operating point in DSP. */
	float input_gain = 1.0f;
	/*
	 * Output headroom. MVDR weights are distortionless toward the look
	 * direction but their gain exceeds unity elsewhere, particularly at low
	 * frequency where the superdirective solution is most aggressive. On a
	 * hot capture the sum overshoots full scale by ~7 dB and clips about a
	 * tenth of all samples, which a wake-word engine hears as distortion.
	 * -12 dB is measured headroom for this array, not a guess; raise it with
	 * -g if the source is quiet, and watch the clip count reported on exit.
	 */
	float gain = 0.25f;
	/* Keep stock's fixed ASR Output Gain distinct from the optional user trim.
	 * The order is algebraically immaterial here, but separate state prevents a
	 * later -g from silently replacing the stock +7.2 dB stage. */
	float output_stage_gain = 1.0f;
	long clipped = 0;
	float peak = 0.0f;

	/*
	 * Centre-mic passthrough: the control for judging whether beamforming
	 * actually helps. It must go through this same binary rather than a
	 * separate arecord, so the capture path, calibration and output scaling
	 * are byte-for-byte identical and the only difference is the array
	 * processing. Comparing against a differently-built baseline would make
	 * any wake-word difference uninterpretable.
	 */
	int centre_only = 0;
	int passthrough_mic = -1;
	int reverse_fbf_taps = 0;
	int conjugate_fbf = 0;

	while ((c = getopt(argc, argv, "w:k:c:b:g:G:I:D:m:t:aAnSXYCEqOVvWRTUQZNPJKh")) != -1) {
		switch (c) {
		case 'w': wpath = optarg; break;
		case 'k': calpath = optarg; break;
		case 'D': diagpath = optarg; break;
		case 'm': passthrough_mic = atoi(optarg); centre_only = 1; break;
		case 'c': inchans = atoi(optarg); break;
		case 'b': pinned = atoi(optarg); break;
		case 'g': gain = strtof(optarg, NULL); break;
		case 'G': output_stage_gain = strtof(optarg, NULL); break;
		case 'I': input_gain = strtof(optarg, NULL); break;
		case 'a': use_aec = 1; break;
		case 'A': use_abf = 1; break;
		case 'n': use_nr = 1; break;
		case 'S': stock_profile = 1; break;
		case 'X': bypass_aec = 1; break;
		case 'Y': bypass_abf = 1; break;
		case 'O': stock_order = 1; break;
		case 'R': use_abf_vss = 1; break;
		case 'V': use_aec_vss = use_ara_vss = 1; break;
		case 'v': use_aec_vss = 1; break;
		case 'W': use_ara_vss = 1; break;
		case 'T': stock_selector = 1; break;
		case 'U': use_ara = stock_order = stock_selector = 1; break;
		case 'Q': use_ara = stock_order = stock_selector =
			use_aec_vss = use_ara_vss = use_recovery_controller = 1; break;
		/* AEC VSS supplies the double-talk signal the recovery controller
		 * consumes, so -Z needs it.  ARA VSS is deliberately absent: it
		 * measured worse than plain ARA on both retained captures. */
		case 'Z': use_ara = stock_order = stock_selector =
			use_aec_vss = use_abf_vss =
			use_recovery_controller = ara_always_active = 1;
			sel_snr_threshold_db = CANDIDATE_SEL_SNR_THRESHOLD_DB;
			break;
		case 'N': sel_use_contrast = 1; break;
		case 't': sel_snr_threshold_db = strtof(optarg, NULL); break;
		case 'P': use_ara = stock_order = use_aec_vss = use_ara_vss =
			use_abf_vss = stock_selector = 1; break;
		case 'C': centre_only = 1; break;
		case 'J': reverse_fbf_taps = 1; break;
		case 'K': conjugate_fbf = 1; break;
		case 'E': energy_rule = 1; break;
		case 'q': quiet = 1; break;
		default: usage(); return 1;
		}
	}
	if (!wpath) { usage(); return 1; }

	struct weights W;
	if (load_weights(wpath, &W))
		return 1;
	/*
	 * Deliberately non-fatal. A missing or malformed calibration file must
	 * degrade to the uncalibrated behaviour we shipped before this existed,
	 * never take the microphone down.
	 */
	if (calpath)
		apply_miccal(calpath, &W, quiet);
	if (inchans < 0)
		inchans = (int)W.refchan + 1;
	if (pinned >= (int)W.nbeams) {
		fprintf(stderr, "beamform: beam %d out of range (%u beams)\n",
			pinned, W.nbeams);
		return 1;
	}
	if (passthrough_mic >= (int)W.nmics) {
		fprintf(stderr, "beamform: passthrough mic %d out of range (%u mics)\n",
			passthrough_mic, W.nmics);
		return 1;
	}

	const int N = W.fftlen, HOP = W.hop, M = W.nmics, NB = W.nbins;
	const int B = W.nbeams;
	if (stock_profile) {
		/* Stock ASR: 128 complex bands, decimated by 64 samples at 16 kHz. */
		if (N != 128 || HOP != 64) {
			fprintf(stderr, "beamform: -S needs 128-point/hop-64 stock weights\n");
			return 1;
		}
		input_gain = 10.0f;  /* +20 dB after setting codec PGA to stock 20 dB */
		use_aec = !bypass_aec;
		use_abf = !bypass_abf;
	}
	if ((stock_order || stock_selector || use_ara) && !stock_profile) {
		fprintf(stderr, "beamform: -O/-T/-U/-Q/-Z/-P require the -S stock profile\n");
		return 1;
	}
	if (use_ara && !use_abf) {
		fprintf(stderr, "beamform: -U/-Q/-Z need ABF; they cannot be combined with -Y\n");
		return 1;
	}
	if (use_recovery_controller && !use_aec) {
		fprintf(stderr, "beamform: -Q/-Z need the physical playback-reference AEC\n");
		return 1;
	}
	if (use_abf_vss && !stock_profile) {
		fprintf(stderr, "beamform: recovered -R requires the -S stock profile\n");
		return 1;
	}
	if (!(sel_snr_threshold_db >= -20.0f && sel_snr_threshold_db <= 40.0f)) {
		fprintf(stderr, "beamform: -t must be between -20 and 40 dB\n");
		return 1;
	}
	if (input_gain <= 0.0f) {
		fprintf(stderr, "beamform: input gain must be positive\n");
		return 1;
	}
	if (use_aec && (W.refchan >= (uint32_t)inchans)) {
		fprintf(stderr, "beamform: AEC needs the DAC reference at input channel %u\n",
			W.refchan);
		return 1;
	}

	const int FB_LEN = stock_profile ? 5 * N : N;
	float *win = malloc(N * sizeof(float));
	for (int i = 0; i < N; i++)          /* periodic Hann: 50% overlap sums flat */
		win[i] = 0.5f * (1.0f - cosf(2.0f * (float)M_PI * i / N));
	float *prototype = stock_profile ? malloc(FB_LEN * sizeof(float)) : NULL;
	float *fb_normalizer = stock_profile ? malloc(HOP * sizeof(float)) : NULL;
	/* Exact stock FBF: [beam][64 bands][4 frame taps][mic]. The history keeps
	 * the current analysis frame at tap 0, then its three predecessors. */
	cpx *stock_fbf = stock_profile ?
		malloc((size_t)B * (N / 2) * 4 * M * sizeof(cpx)) : NULL;
	cpx *stock_fbf_history = stock_profile ?
		calloc((size_t)4 * M * NB, sizeof(cpx)) : NULL;
	cpx *stock_beams = stock_profile ? calloc((size_t)B * NB, sizeof(cpx)) : NULL;

	struct dcblock *dc = calloc(M, sizeof(struct dcblock));
	struct dcblock dc_centre = { 0.0f, 0.0f };
	struct hpf80 *hpf = stock_profile ? calloc(M, sizeof(struct hpf80)) : NULL;
	struct hpf80 hpf_centre = { {0}, {0}, {0}, {0} };
	struct hpf80 hpf_ref = { {0}, {0}, {0}, {0} };
	float *ring = calloc((size_t)M * FB_LEN, sizeof(float)); /* per-mic history */
	float *frame = malloc((size_t)M * N * sizeof(float));
	cpx *spec = malloc((size_t)M * NB * sizeof(cpx));
	cpx *scratch = malloc(N * sizeof(cpx));
	cpx *out = malloc(NB * sizeof(cpx));
	cpx *tdo = malloc(N * sizeof(cpx));
	float *ola = calloc(FB_LEN, sizeof(float));
	uint8_t *inbuf = malloc((size_t)HOP * inchans * BYTES_PER_SAMPLE);
	int16_t *outbuf = malloc(HOP * sizeof(int16_t));

	/* Stock AEC tail is 2,560 samples (160 ms).  This is a frequency-domain
	 * NLMS implementation with one physical reference (the exposed DAC
	 * loopback); the proprietary path has room for two references. */
	const int aec_parts = (AEC_TAIL_SAMPLES + HOP - 1) / HOP;
	int aec_history_pos = aec_parts - 1;
	float *ref_ring = use_aec ? calloc(FB_LEN, sizeof(float)) : NULL;
	float *ref_frame = use_aec ? malloc(N * sizeof(float)) : NULL;
	cpx *refspec = use_aec ? malloc(NB * sizeof(cpx)) : NULL;
	cpx *aec_rhist = use_aec ? calloc((size_t)2 * aec_parts * NB, sizeof(cpx)) : NULL;
	float *aec_ref_power = use_aec ? calloc(NB, sizeof(float)) : NULL;
	cpx *aec_h = use_aec ? calloc((size_t)M * aec_parts * NB, sizeof(cpx)) : NULL;
	/* These smoothed coherence spectra are diagnostic-only.  The recovery
	 * controller consumes the recovered GenericCanceler VSS state directly. */
	int need_aec_metrics = use_aec && diagpath;
	float *aec_diag_x_power = need_aec_metrics ?
		calloc((size_t)M * NB, sizeof(float)) : NULL;
	float *aec_diag_echo_power = need_aec_metrics ?
		calloc((size_t)M * NB, sizeof(float)) : NULL;
	float *aec_diag_error_power = need_aec_metrics ?
		calloc((size_t)M * NB, sizeof(float)) : NULL;
	cpx *aec_diag_x_echo_cross = need_aec_metrics ?
		calloc((size_t)M * NB, sizeof(cpx)) : NULL;
	cpx *aec_diag_error_echo_cross = need_aec_metrics ?
		calloc((size_t)M * NB, sizeof(cpx)) : NULL;
	struct stock_generic_vss aec_vss = {
		M, 1, NB, 1.0f, NULL, NULL, NULL, NULL, NULL
	};
	if (use_aec && use_aec_vss) {
		aec_vss.input_power = calloc((size_t)M * NB, sizeof(float));
		aec_vss.ref_power = calloc(NB, sizeof(float));
		aec_vss.cross = calloc((size_t)M * NB, sizeof(cpx));
		aec_vss.step = calloc((size_t)M * NB, sizeof(float));
		aec_vss.combined = calloc((size_t)M * NB, sizeof(float));
	}
	/* Adaptive beamformer: stock uses a 1,536-sample tail and two blocking
	 * references for each of its six fixed beams. */
	const int abf_parts = (1536 + HOP - 1) / HOP;
	int abf_history_pos = abf_parts - 1;
	cpx *abf_rhist = use_abf ? calloc((size_t)B * 2 * 2 * abf_parts * NB,
		sizeof(cpx)) : NULL;
	cpx *abf_h = use_abf ? calloc((size_t)B * 2 * abf_parts * NB,
		sizeof(cpx)) : NULL;
	cpx *abf_beams = use_abf && stock_order ?
		calloc((size_t)B * NB, sizeof(cpx)) : NULL;
	/* Initial phase 1 means the first post-hop update selects reference zero,
	 * matching the stock cursor's zero-filled startup. */
	struct stock_abf_vss abf_vss = { NULL, NULL, NULL, NULL, 1 };
	if (use_abf && use_abf_vss) {
		abf_vss.look_power = calloc((size_t)B * NB, sizeof(float));
		abf_vss.ref_power = calloc((size_t)B * 2 * NB, sizeof(float));
		abf_vss.ratio_state = calloc((size_t)B * 2, sizeof(float));
		abf_vss.step = calloc((size_t)B * 2 * NB, sizeof(float));
	}
	/* ARA is another two-reference 2,560-sample GenericCanceler, placed after
	 * RefBeamSelector and before SNRBeamSelector in the active stock branch. */
	const int ara_parts = (2560 + HOP - 1) / HOP;
	int ara_history_pos = ara_parts - 1;
	/* Each adaptive history is mirrored around its circular write position.
	 * Readers therefore retain the old contiguous oldest-to-newest partition
	 * order without shifting a complete tail every 4 ms. ARA's references are
	 * also shared by all six inputs instead of keeping six identical copies. */
	cpx *ara_rhist = use_ara ? calloc((size_t)2 * 2 * ara_parts * NB,
		sizeof(cpx)) : NULL;
	cpx *ara_h = use_ara ? calloc((size_t)B * 2 * ara_parts * NB,
		sizeof(cpx)) : NULL;
	cpx *ara_beams = use_ara ? calloc((size_t)B * NB, sizeof(cpx)) : NULL;
	cpx *ara_refs = use_ara ? calloc((size_t)2 * NB, sizeof(cpx)) : NULL;
	float *abf_power_smooth = use_ara ? calloc((size_t)B * NB, sizeof(float)) : NULL;
	float *ara_input_power_smooth = use_ara ?
		calloc((size_t)B * NB, sizeof(float)) : NULL;
	float *ara_output_power_smooth = use_ara ?
		calloc((size_t)B * NB, sizeof(float)) : NULL;
	/* The safe -T rollback omits ARA, but SNRBeamSelector must still receive
	 * the same K=0.9875 corrEner representation rather than instantaneous
	 * post-ABF power.  Keep this state separate from ARA's true output vector
	 * so the two paths cannot accidentally share or double-smooth state. */
	float *selector_power_smooth = stock_selector && !use_ara ?
		calloc((size_t)B * NB, sizeof(float)) : NULL;
	struct stock_generic_vss ara_vss = {
		B, 2, NB, 1.0f / STOCK_FBF_POWER_UNIT_SCALE,
		NULL, NULL, NULL, NULL, NULL
	};
	if (use_ara && use_ara_vss) {
		ara_vss.input_power = calloc((size_t)B * NB, sizeof(float));
		ara_vss.ref_power = calloc((size_t)2 * NB, sizeof(float));
		ara_vss.cross = calloc((size_t)B * 2 * NB, sizeof(cpx));
		ara_vss.step = calloc((size_t)B * 2 * NB, sizeof(float));
		ara_vss.combined = calloc((size_t)B * NB, sizeof(float));
	}
	long *diag_abf_adapt_beam = diagpath ? calloc(B, sizeof(long)) : NULL;
	float *diag_abf_vss_beam = diagpath ? malloc(B * sizeof(float)) : NULL;
	long abf_adapted = 0;
	long ara_adapted = 0;
	long ara_diverged = 0;
	long ara_edge_resets = 0;
	float *nr_floor = use_nr ? malloc(NB * sizeof(float)) : NULL;
	long aec_adapted = 0;
	long aec_diverged = 0;
	struct recovery_controller recovery = {0};
	if (aec_vss.cross)
		stock_generic_vss_init(&aec_vss);
	if (ara_vss.cross)
		stock_generic_vss_init(&ara_vss);
	if (nr_floor)
		for (int k = 0; k < NB; k++) nr_floor[k] = 1e-8f;

	if (!win || !dc || !ring || !frame || !spec || !scratch || !out || !tdo ||
	    !ola || !inbuf || !outbuf || (stock_profile && !hpf) ||
	    (stock_profile && (!prototype || !fb_normalizer || !stock_fbf ||
				       !stock_fbf_history || !stock_beams)) ||
	    (use_aec && (!ref_ring || !ref_frame || !refspec || !aec_rhist || !aec_h)) ||
	    (need_aec_metrics &&
				    (!aec_diag_x_power || !aec_diag_echo_power ||
				     !aec_diag_error_power || !aec_diag_x_echo_cross ||
				     !aec_diag_error_echo_cross)) ||
	    (use_aec && use_aec_vss && (!aec_vss.input_power || !aec_vss.ref_power ||
				      !aec_vss.cross || !aec_vss.step ||
				      !aec_vss.combined)) ||
	    (use_abf && (!abf_rhist || !abf_h)) ||
	    (use_abf && stock_order && !abf_beams) ||
	    (use_abf && use_abf_vss && (!abf_vss.look_power || !abf_vss.ref_power ||
					  !abf_vss.ratio_state || !abf_vss.step)) ||
	    (use_ara && (!ara_rhist || !ara_h || !ara_beams || !ara_refs ||
			   !abf_power_smooth || !ara_input_power_smooth ||
			   !ara_output_power_smooth)) ||
	    (stock_selector && !use_ara && !selector_power_smooth) ||
	    (use_ara && use_ara_vss && (!ara_vss.input_power || !ara_vss.ref_power ||
				      !ara_vss.cross || !ara_vss.step ||
				      !ara_vss.combined)) ||
	    (diagpath && (!diag_abf_adapt_beam || !diag_abf_vss_beam)) ||
	    (use_nr && !nr_floor)) {
		fprintf(stderr, "beamform: out of memory\n");
		return 1;
	}
	if (stock_profile && (init_stock_filterbank(prototype, fb_normalizer, N, HOP) ||
			     load_stock_fbf(stock_fbf, N, B, M,
					    reverse_fbf_taps, conjugate_fbf)))
		return 1;

	if (!quiet)
		fprintf(stderr, "beamform: %d mics, %d beams, fft %d hop %d, "
			"%d input channels, %s%s%s%s%s\n", M, B, N, HOP, inchans,
			centre_only ? "CENTRE MIC ONLY (baseline)" :
			pinned >= 0 ? "pinned beam" :
			energy_rule ? "auto: max energy (legacy)" :
				      "auto: band-limited SNR",
			stock_profile ? ", stock 80 Hz/20 dB PGA profile" : "",
			use_aec ? ", AEC 160 ms" : "",
			use_abf ? ", ABF 96 ms" : "",
			use_nr ? ", NR" : "");
	if (!quiet && (stock_order || use_aec_vss || use_ara_vss || use_abf_vss ||
			     stock_selector || use_ara || use_recovery_controller))
		fprintf(stderr, "beamform: candidate stages%s%s%s%s%s%s%s\n",
			stock_order ? ", all-beam ABF order" : "",
			use_aec_vss ? ", AEC GenericCanceler VSS" : "",
			use_ara_vss ? ", ARA GenericCanceler VSS" : "",
			use_abf_vss ? ", ABF VSS/round-robin" : "",
			stock_selector ? ", stateful selector" : "",
			use_ara ? ", ARA/exact energy vectors" : "",
			use_recovery_controller ? ", playback/DTD recovery controller" : "");

	long frames = 0;
	int beam_hist[16] = {0};

	/* Selection state: band-limited energy per beam and its tracked floor. */
	float *bandE = calloc(B, sizeof(float));
	float *ref_metric = calloc(B, sizeof(float));
	unsigned char *ref_excluded = calloc(B, sizeof(unsigned char));
	double *floorE = calloc(B, sizeof(double));
	float *sel_snr = calloc(B, sizeof(float));
	int cur_beam = -1;
	struct stock_ref_selector_state stock_ref = {0};
	struct stock_selector_state stock_sel = {0};
	int diag_last_beam = -1, diag_residency = 0, diag_changes = 0;
	int sel_k0 = (int)(SEL_LO_HZ * N / W.rate + 0.5f);
	int sel_k1 = (int)(SEL_HI_HZ * N / W.rate + 0.5f);
	int ref_k0 = (int)(LEGACY_REF_POWER_LO_HZ * N / W.rate + 0.5f);
	int ref_k1 = (int)(LEGACY_REF_POWER_HI_HZ * N / W.rate + 0.5f);
	int vss_k0 = sel_k0, vss_k1 = sel_k1;
	if (sel_k0 < 1) sel_k0 = 1;
	if (sel_k1 > NB - 1) sel_k1 = NB - 1;
	if (ref_k0 < 1) ref_k0 = 1;
	if (ref_k1 > NB - 1) ref_k1 = NB - 1;
	if (!bandE || !ref_metric || !ref_excluded || !floorE || !sel_snr) {
		fprintf(stderr, "beamform: out of memory\n");
		return 1;
	}

	FILE *diag = NULL;
	if (diagpath) {
		diag = strcmp(diagpath, "-") == 0 ? stderr : fopen(diagpath, "w");
		if (!diag) {
			fprintf(stderr, "beamform: cannot open diagnostics %s\n", diagpath);
			return 1;
		}
		fprintf(diag, "frame\tseconds\tref_power\taec_input_power\t"
			"aec_echo_power\taec_error_power\t"
			"aec_input_echo_coherence\taec_error_echo_coherence\t"
			"aec_fast_converge\t"
			"aec_vss_mean\taec_residual_echo_ratio\taec_doubletalk_pct\t"
			"aec_adapt_bins\taec_divergence_bins\t"
			"abf_vss_mean\tabf_adapt_bins\t"
			"ara_vss_mean\tara_adapt_bins\tara_divergence\t"
			"recovery_ref_power\tara_active\tara_adapt_allowed\t"
			"dtd_freeze\tdtd_attack_hops\tdtd_hangover_hops\t"
			"vad\tselected\tresidency\tchanges");
		for (int b = 0; b < B; b++)
			fprintf(diag, "\tsnr_b%d", b);
		for (int b = 0; b < B; b++)
			fprintf(diag, "\tenergy_b%d", b);
		for (int b = 0; b < B; b++)
			fprintf(diag, "\tref_metric_b%d", b);
		for (int b = 0; b < B; b++)
			fprintf(diag, "\tref_excluded_b%d", b);
		fprintf(diag, "\tref_primary\tref_secondary\tref_count\tref_threshold_smooth");
		for (int b = 0; b < B; b++)
			fprintf(diag, "\tabf_vss_b%d", b);
		for (int b = 0; b < B; b++)
			fprintf(diag, "\tabf_adapt_b%d", b);
		fprintf(diag, "\n");
	}

	struct ara_parallel ara_parallel = { 0 };
	if (use_ara && ara_parallel_init(&ara_parallel, B, ara_parts, NB,
					  ara_rhist, ara_h, use_ara_vss,
					  &ara_vss) != 0) {
		fprintf(stderr, "beamform: cannot start ARA worker threads\n");
		return 1;
	}

	for (;;) {
		size_t need = (size_t)HOP * inchans * BYTES_PER_SAMPLE;
		size_t got = fread(inbuf, 1, need, stdin);
		if (got < need)
			break;
		long hop_aec_adapted = 0, hop_abf_adapted = 0, hop_ara_adapted = 0;
		long hop_aec_diverged = 0;
		int hop_ara_diverged = 0;
		if (diag) {
			memset(diag_abf_adapt_beam, 0, (size_t)B * sizeof(long));
			for (int b = 0; b < B; b++)
				diag_abf_vss_beam[b] = -1.0f;
		}
		int hop_aec_doubletalk = 0, hop_aec_vss_bins = 0;
		int hop_aec_fast_converge = 0;
		float hop_aec_vss_sum = 0.0f, hop_ref_power = 0.0f;
		float hop_aec_ratio_sum = 0.0f;
		int hop_aec_ratio_mics = 0;
		double hop_aec_input_power = 0.0, hop_aec_echo_power = 0.0;
		double hop_aec_error_power = 0.0;
		double hop_aec_input_echo_coherence = 0.0;
		double hop_aec_error_echo_coherence = 0.0;
		int hop_aec_metric_bins = 0;
		float hop_abf_vss_sum = 0.0f;
		int hop_abf_vss_beams = 0, vad = 0;
		float hop_ara_vss_sum = 0.0f;
		int hop_ara_vss_bins = 0;
		int hop_ara_active = use_ara && !use_recovery_controller;
		int hop_ara_adapt_allowed = use_ara && !use_recovery_controller;
		int hop_dtd_freeze = use_recovery_controller ? recovery.freeze : 0;

		/*
		 * Centre-mic passthrough. Taken before any array processing so
		 * the two modes differ only in what this block does, and share
		 * everything downstream including the output scaling.
		 */
		if (centre_only) {
			int mic = passthrough_mic >= 0 ? passthrough_mic : M - 1;
			int ch = W.chanmap[mic];
			float g = W.gains[mic] / 8388608.0f * input_gain * gain;
			for (int i = 0; i < HOP; i++) {
				const uint8_t *p = inbuf +
					((size_t)i * inchans + ch) * BYTES_PER_SAMPLE;
				int32_t v = (int32_t)p[0] | ((int32_t)p[1] << 8) |
					    ((int32_t)p[2] << 16);
				if (v & 0x800000)
					v -= 0x1000000;
				float s = stock_profile ? hpf80_step(&hpf_centre, v * g) :
					dcblock_step(&dc_centre, v * g);
				if (s > 1.0f) { s = 1.0f; clipped++; }
				else if (s < -1.0f) { s = -1.0f; clipped++; }
				if (fabsf(s) > peak) peak = fabsf(s);
				outbuf[i] = (int16_t)lrintf(s * 32767.0f);
			}
			if (fwrite(outbuf, sizeof(int16_t), HOP, stdout) != (size_t)HOP)
				break;
			frames++;
			continue;
		}

		/* Shift history and append the new hop, per mic, with calibration. */
		for (int m = 0; m < M; m++) {
			float *r = ring + (size_t)m * FB_LEN;
			memmove(r, r + HOP, (FB_LEN - HOP) * sizeof(float));
			int ch = W.chanmap[m];
			float g = W.gains[m] / 8388608.0f * input_gain;
			for (int i = 0; i < HOP; i++) {
				const uint8_t *p = inbuf +
					((size_t)i * inchans + ch) * BYTES_PER_SAMPLE;
				int32_t v = (int32_t)p[0] | ((int32_t)p[1] << 8) |
					    ((int32_t)p[2] << 16);
				if (v & 0x800000)
					v -= 0x1000000;
				float x = v * g;
				r[FB_LEN - HOP + i] = stock_profile ? hpf80_step(&hpf[m], x) :
					dcblock_step(&dc[m], x);
			}
			if (stock_profile) {
				/* Five 128-sample polyphase branches collapse to the
				 * 128-point analysis FFT, decimated every 64 samples. */
				for (int i = 0; i < N; i++) {
					float v = 0.0f;
					for (int p = 0; p < 5; p++)
						v += r[i + p * N] * prototype[i + p * N];
					frame[(size_t)m * N + i] = v;
				}
			} else {
				for (int i = 0; i < N; i++)
					frame[(size_t)m * N + i] = r[i] * win[i];
			}
		}

		/* The eighth capture channel is a hardware DAC loopback, not an
		 * acoustic microphone.  Keep it at physical level (no miccal or
		 * stock-PGA make-up) and filter it exactly like the mic inputs. */
		if (use_aec) {
			memmove(ref_ring, ref_ring + HOP, (FB_LEN - HOP) * sizeof(float));
			for (int i = 0; i < HOP; i++) {
				const uint8_t *p = inbuf +
					((size_t)i * inchans + W.refchan) * BYTES_PER_SAMPLE;
				int32_t v = (int32_t)p[0] | ((int32_t)p[1] << 8) |
					    ((int32_t)p[2] << 16);
				if (v & 0x800000) v -= 0x1000000;
				float x = v / 8388608.0f;
				ref_ring[FB_LEN - HOP + i] = stock_profile ? hpf80_step(&hpf_ref, x) :
					dcblock_step(&dc_centre, x);
			}
			if (stock_profile) {
				for (int i = 0; i < N; i++) {
					float v = 0.0f;
					for (int p = 0; p < 5; p++)
						v += ref_ring[i + p * N] * prototype[i + p * N];
					ref_frame[i] = v;
				}
			} else {
				for (int i = 0; i < N; i++)
					ref_frame[i] = ref_ring[i] * win[i];
			}
			if (use_recovery_controller) {
				recovery_update_reference(&recovery,
					ref_ring + FB_LEN - HOP, HOP);
				hop_ara_active = ara_always_active ? 1 :
					recovery.reference_active;
				hop_ara_adapt_allowed = !hop_dtd_freeze;
			}
		}

		/* Two real FFTs at a time; odd mic out goes through alone. */
		int m = 0;
		for (; m + 1 < M; m += 2)
			fft_two_real(frame + (size_t)m * N, frame + (size_t)(m + 1) * N,
				     N, spec + (size_t)m * NB, spec + (size_t)(m + 1) * NB,
				     scratch);
		if (m < M) {
			for (int i = 0; i < N; i++) {
				scratch[i].re = frame[(size_t)m * N + i];
				scratch[i].im = 0.0f;
			}
			fft_radix2(scratch, N, 0);
			memcpy(spec + (size_t)m * NB, scratch, NB * sizeof(cpx));
		}

		/* Partitioned frequency-domain NLMS AEC.  Five 512-sample
		 * partitions in the legacy profile, or forty 64-sample partitions
		 * in the stock 128/64 profile, both give the stock 2,560-sample
		 * (160 ms) echo tail. r95 is preserved unless -V/-P is requested;
		 * that candidate uses the recovered per-bin GenericCanceler state. */
		if (use_aec) {
			for (int i = 0; i < N; i++) {
				scratch[i].re = ref_frame[i];
				scratch[i].im = 0.0f;
			}
			fft_radix2(scratch, N, 0);
			memcpy(refspec, scratch, NB * sizeof(cpx));
			aec_history_pos++;
			if (aec_history_pos == aec_parts)
				aec_history_pos = 0;
			/* Bin-major mirrored history: each bin owns a contiguous
			 * ring of 2*aec_parts slots, so the partition loops below
			 * walk sequential memory instead of striding NB complex
			 * (520 bytes) per step.  The scatter costs 2*NB writes
			 * once per hop against 7 mics * NB bins * aec_parts
			 * strided reads in each of two loops. */
			for (int k = 0; k < NB; k++) {
				size_t base = (size_t)k * 2 * aec_parts;
				aec_rhist[base + aec_history_pos] = refspec[k];
				aec_rhist[base + aec_history_pos + aec_parts] =
					refspec[k];
			}
			const int aec_hpos = aec_history_pos;
			/* The reference power per bin is the same for every
			 * microphone - it depends only on the shared reference
			 * history - so compute it once rather than seven times. */
			for (int k = 0; k < NB; k++) {
				const cpx *hk = aec_rhist +
					(size_t)k * 2 * aec_parts + aec_hpos + 1;
				float rp = 0.0f;
				for (int p = 0; p < aec_parts; p++)
					rp += cpx_power(hk[p]);
				aec_ref_power[k] = rp;
			}
			for (int k = 0; k < NB; k++) {
				float rp = cpx_power(refspec[k]);
				hop_ref_power += rp;
			}
			if (use_aec_vss)
				stock_generic_vss_update_refs(&aec_vss, refspec);

			for (int mic = 0; mic < M; mic++) {
				cpx *hm = aec_h + (size_t)mic * aec_parts * NB;
				double mic_smoothed_echo = 0.0, mic_smoothed_error = 0.0;
				int mic_metric_bins = 0;
				for (int k = 0; k < NB; k++) {
					cpx x = spec[(size_t)mic * NB + k];
					cpx echo = { 0.0f, 0.0f };
					const float rpower = aec_ref_power[k];
					const cpx *hist_k = aec_rhist +
						(size_t)k * 2 * aec_parts + aec_hpos + 1;
					cpx *h_k = hm + (size_t)k * aec_parts;
					for (int p = 0; p < aec_parts; p++)
						echo = cpx_add(echo,
							cpx_mul(h_k[p], hist_k[p]));
					cpx e = cpx_sub(x, echo);
					float xpower = cpx_power(x);
					float echopower = cpx_power(echo);
					float errorpower = cpx_power(e);
					if (need_aec_metrics && k >= vss_k0 && k <= vss_k1) {
						size_t di = (size_t)mic * NB + k;
						float keep = AEC_DIAG_SMOOTH;
						float add = 1.0f - keep;
						cpx x_echo = cpx_mul_conj_right(x, echo);
						cpx error_echo = cpx_mul_conj_right(e, echo);
						aec_diag_x_power[di] = keep * aec_diag_x_power[di] +
							add * xpower;
						aec_diag_echo_power[di] = keep * aec_diag_echo_power[di] +
							add * echopower;
						aec_diag_error_power[di] = keep * aec_diag_error_power[di] +
							add * errorpower;
						aec_diag_x_echo_cross[di].re =
							keep * aec_diag_x_echo_cross[di].re + add * x_echo.re;
						aec_diag_x_echo_cross[di].im =
							keep * aec_diag_x_echo_cross[di].im + add * x_echo.im;
						aec_diag_error_echo_cross[di].re =
							keep * aec_diag_error_echo_cross[di].re + add * error_echo.re;
						aec_diag_error_echo_cross[di].im =
							keep * aec_diag_error_echo_cross[di].im + add * error_echo.im;
						float px = aec_diag_x_power[di];
						float py = aec_diag_echo_power[di];
						float pe = aec_diag_error_power[di];
						if (px > 1e-12f && py > 1e-12f && pe > 1e-12f) {
							float xecho_coherence = fminf(1.0f,
								cpx_power(aec_diag_x_echo_cross[di]) /
								(px * py + 1e-30f));
							float error_echo_coherence = fminf(1.0f,
								cpx_power(aec_diag_error_echo_cross[di]) /
								(pe * py + 1e-30f));
							mic_smoothed_echo += py;
							mic_smoothed_error += pe;
							mic_metric_bins++;
							if (diag) {
								hop_aec_input_power += px;
								hop_aec_echo_power += py;
								hop_aec_error_power += pe;
								hop_aec_input_echo_coherence += xecho_coherence;
								hop_aec_error_echo_coherence +=
									error_echo_coherence;
								hop_aec_metric_bins++;
							}
						}
					}
					float curpower = cpx_power(refspec[k]);
					if (curpower > 1e-12f && xpower > 1e-12f &&
					    (use_aec_vss ||
					     rpower > AEC_REF_PRESENCE * xpower)) {
						float step_scale = 1.0f;
						if (use_aec_vss) {
							step_scale = aec_vss.combined[(size_t)mic * NB + k];
							hop_aec_vss_sum += step_scale;
							hop_aec_vss_bins++;
							if (step_scale < 0.5f)
								hop_aec_doubletalk++;
						} else {
							/* Preserve the r95 calculation exactly for rollback.
							 * Algebraically this is approximately one, which is
							 * why -V replaces it. */
							cpx xr = cpx_mul_conj_right(x, refspec[k]);
							float instant_corr = cpx_power(xr) /
								(xpower * curpower + 1e-20f);
							step_scale = fminf(1.0f, instant_corr * 4.0f);
						}
						if (use_recovery_controller && hop_dtd_freeze)
							step_scale = 0.0f;
						/* Stock's maximum step is 0.2.  rpower above is
						 * already the reference power summed over every
						 * partition, which is the whole NLMS normaliser, so
						 * the old extra division by aec_parts (40 here)
						 * over-normalised the step by 40x: an effective
						 * maximum of 0.005 against stock's 0.2.  Measured on a
						 * full-volume playback capture, that cost 5.8 dB of
						 * ERLE overall and 8.7 dB in the first two seconds -
						 * the canceller needed about fifteen seconds to reach
						 * what it now reaches inside two.
						 *
						 * The remaining margin is deliberate.  Stock modulates
						 * its step per band (bandBasedStepSize, plus
						 * stepSizeRednScale/stepSizeErrorScale) and we do not,
						 * so running its nominal 0.2 flat across every bin
						 * measured worse than this: on the same capture the
						 * best wake recall was here, with fewer divergence
						 * candidates per hop than a larger step.  With an
						 * exactly silent reference (the codec released) the AEC
						 * does not adapt at all, so this is byte-identical to
						 * the previous behaviour on every no-playback capture.
						 * A FADING reference is not silent: that case is what
						 * AEC_REF_PRESENCE in the gate above exists for. */
						float step = 0.2f * step_scale /
							(AEC_STEP_PARTITION_MARGIN * (rpower + 1e-5f));
						if (step_scale > 0.001f) {
							for (int p = 0; p < aec_parts; p++) {
								cpx grad = cpx_mul_conj_right(e,
									hist_k[p]);
								h_k[p].re += step * grad.re;
								h_k[p].im += step * grad.im;
							}
							aec_adapted++;
							hop_aec_adapted++;
						}
					}
					if (xpower > 1e-8f && echopower > 1e-8f &&
					    cpx_power(e) > 4.0f * xpower) {
						aec_diverged++;
						hop_aec_diverged++;
					}
					spec[(size_t)mic * NB + k] = e;
					/* Stock updates the VSS state after coefficient processing;
					 * this newly formed gate is therefore consumed next hop. */
					if (use_aec_vss)
						stock_generic_vss_update_input_bin(&aec_vss,
							mic, k, x, refspec);
				}
				if (mic_metric_bins) {
					float ratio = (float)(mic_smoothed_error /
						(mic_smoothed_echo + 1e-30));
					if (diag) {
						hop_aec_ratio_sum += ratio;
						hop_aec_ratio_mics++;
					}
				}
			}
		}

		/* Apply the recovered stock four-frame FBF after mic AEC, just as the
		 * AFE.cfg algorithm order specifies. Keeping every beam lets the stock
		 * ABF use its configured beam outputs as blocking references. */
		if (stock_profile) {
			memmove(stock_fbf_history + (size_t)M * NB, stock_fbf_history,
				(size_t)3 * M * NB * sizeof(cpx));
			memcpy(stock_fbf_history, spec, (size_t)M * NB * sizeof(cpx));
			for (int b = 0; b < B; b++) {
				for (int k = 0; k < N / 2; k++)
					stock_beams[(size_t)b * NB + k] =
						apply_stock_fbf(stock_fbf, stock_fbf_history, b, k, M, NB);
				/* The stock FBF provides 64 positive-frequency bands. Keep the
				 * unused Nyquist bin quiet rather than inventing a 65th weight. */
				stock_beams[(size_t)b * NB + N / 2] = (cpx){ 0.0f, 0.0f };
			}
		}

		/* Populate two blocking references for every fixed beam. The pair map
		 * is the stock six-beam topology: [2,4], [3,5], [4,0], [5,1], [0,2],
		 * [1,3]. With the exact stock FBF these are the other beam outputs as
		 * specified by AFE.cfg. The legacy path keeps its open mic-difference
		 * approximation only for non-stock operation. */
		if (use_abf) {
			static const int pairs[6][2] = {
				{2, 4}, {3, 5}, {4, 0}, {5, 1}, {0, 2}, {1, 3}
			};
			abf_history_pos++;
			if (abf_history_pos == abf_parts)
				abf_history_pos = 0;
			for (int b = 0; b < B; b++) {
				int pa = pairs[b % 6][0], pb = pairs[b % 6][1];
				for (int r = 0; r < 2; r++) {
					cpx *hist = abf_rhist +
						((size_t)(b * 2 + r) * 2 * abf_parts * NB);
					for (int k = 0; k < NB; k++) {
						cpx value;
						if (stock_profile) {
							int refbeam = r ? pb : pa;
							value = stock_beams[(size_t)refbeam * NB + k];
						} else {
							cpx a = spec[(size_t)pa * NB + k];
							cpx bb = spec[(size_t)pb * NB + k];
							cpx centre = spec[(size_t)(M - 1) * NB + k];
							if (!r)
								value = cpx_sub(a, bb);
							else {
								cpx mean = { 0.5f * (a.re + bb.re),
									0.5f * (a.im + bb.im) };
								value = cpx_sub(mean, centre);
							}
						}
						hist[(size_t)abf_history_pos * NB + k] = value;
						hist[(size_t)(abf_history_pos + abf_parts) * NB + k] =
							value;
					}
				}
			}
		}

		/* Stock cleans all six fixed beams before VAD and both selectors.  -O
		 * enables that exact structural order; without it, only the selected
		 * beam is cleaned below, preserving r95 byte-for-byte behavior. */
		const cpx *selector_beams = stock_profile ? stock_beams : NULL;
		if (use_abf && stock_order) {
			/* Preserve the accepted rollback's ABF update units whenever stock
			 * would bypass ARA.  During reference-active playback, use the
			 * corrected stock FBF units needed by ARA/VSS. */
			int correct_fbf_units = use_abf_vss ||
				(use_ara && (!use_recovery_controller || hop_ara_active));
			for (int b = 0; b < B; b++) {
				long adapt_before = hop_abf_adapted;
				if (use_abf_vss) {
					float mean = 0.0f;
					const float *steps = abf_vss.step +
						((size_t)b * 2 + abf_vss.round_robin_ref) * NB;
					for (int k = 2; k <= 56 && k < NB; k++)
						mean += steps[k];
					mean /= 55.0f;
					hop_abf_vss_sum += mean;
					hop_abf_vss_beams++;
					if (diag)
						diag_abf_vss_beam[b] = mean;
				}
				process_abf_beam(stock_beams + (size_t)b * NB,
					abf_beams + (size_t)b * NB, b, abf_rhist, abf_h,
					abf_parts, NB, abf_history_pos, N, W.rate,
					correct_fbf_units, use_abf_vss,
					use_abf_vss ? abf_vss.step + (size_t)b * 2 * NB : NULL,
					abf_vss.round_robin_ref,
					&abf_adapted, &hop_abf_adapted);
				if (diag) {
					diag_abf_adapt_beam[b] += hop_abf_adapted - adapt_before;
				}
			}
			/* Stock forms VSS and round-robin decisions after all beams; they
			 * control coefficient adaptation on the following hop. */
			if (use_abf_vss)
				stock_abf_vss_update(&abf_vss, stock_beams, abf_rhist,
					B, abf_parts, NB, abf_history_pos);
			selector_beams = abf_beams;
		}

		/* Completed stock branch:
		 *   all-beam ABF -> smoothed ABF corrEner -> RefBeamSelector
		 *   -> selected-reference ARA -> smoothed ARA corrEner -> SNR selector.
		 * AFE.cfg's nominal list is only approximate; AFE::process at
		 * 0x295be..0x29640 establishes this actual data dependency. */
		if (use_ara) {
			update_stock_power_vector(abf_power_smooth, selector_beams,
				ref_metric, B, NB, STOCK_ABF_POWER_SMOOTH,
				STOCK_ABF_POWER_BIN_LO, STOCK_ABF_POWER_BIN_HI);
			stock_ref_selector_update(&stock_ref, ref_metric, B, ref_excluded);

			for (int r = 0; r < 2; r++) {
				int rb = stock_ref.output[r];
				if (rb < 0 || rb >= B) rb = 0;
				memcpy(ara_refs + (size_t)r * NB,
					selector_beams + (size_t)rb * NB, NB * sizeof(cpx));
			}
			ara_history_pos++;
			if (ara_history_pos == ara_parts)
				ara_history_pos = 0;
			for (int r = 0; r < 2; r++) {
				cpx *hist = ara_rhist + (size_t)r * 2 * ara_parts * NB;
				memcpy(hist + (size_t)ara_history_pos * NB,
					ara_refs + (size_t)r * NB, NB * sizeof(cpx));
				memcpy(hist + (size_t)(ara_history_pos + ara_parts) * NB,
					ara_refs + (size_t)r * NB, NB * sizeof(cpx));
			}

			/* sel_snr is scratch here; stock_selector_pick overwrites it below. */
			update_stock_power_vector(ara_input_power_smooth, selector_beams,
				sel_snr, B, NB, STOCK_ARA_POWER_SMOOTH,
				STOCK_ARA_POWER_BIN_LO, STOCK_ARA_POWER_BIN_HI);
			if (use_ara_vss && hop_ara_active && hop_ara_adapt_allowed)
				for (int b = 0; b < B; b++)
					for (int r = 0; r < 2; r++)
						for (int k = 0; k < NB - 1; k++) {
							hop_ara_vss_sum +=
								ara_vss.combined[(size_t)b * NB + k];
							hop_ara_vss_bins++;
						}
			if (use_recovery_controller && recovery.reference_edge) {
				/* A playback transition invalidates every ARA filter that was
				 * adapted on the other side of it.  Clearing the reference
				 * history as well stops playback-era regressors from driving
				 * the first hops after the edge. */
				memset(ara_h, 0,
					(size_t)B * 2 * ara_parts * NB * sizeof(cpx));
				memset(ara_rhist, 0,
					(size_t)2 * 2 * ara_parts * NB * sizeof(cpx));
				if (use_ara_vss)
					stock_generic_vss_reset(&ara_vss);
				memset(ara_input_power_smooth, 0,
					(size_t)B * NB * sizeof(*ara_input_power_smooth));
				memset(ara_output_power_smooth, 0,
					(size_t)B * NB * sizeof(*ara_output_power_smooth));
				ara_edge_resets++;
			}
			if (!hop_ara_active) {
				memcpy(ara_beams, selector_beams,
					(size_t)B * NB * sizeof(*ara_beams));
			} else if (ara_parallel.ready) {
				ara_parallel_process(&ara_parallel, selector_beams, ara_beams,
					ara_history_pos, hop_ara_adapt_allowed,
					&ara_adapted, &hop_ara_adapted);
			} else {
				process_ara_beams(selector_beams, ara_beams, B, ara_rhist,
					ara_h, ara_parts, NB, ara_history_pos, use_ara_vss,
					hop_ara_adapt_allowed, &ara_vss,
					&ara_adapted, &hop_ara_adapted);
			}
			if (use_ara_vss && hop_ara_active && hop_ara_adapt_allowed) {
				stock_generic_vss_update_refs(&ara_vss, ara_refs);
				for (int b = 0; b < B; b++)
					stock_generic_vss_update_input(&ara_vss, b,
						selector_beams + (size_t)b * NB, ara_refs);
			}
			update_stock_power_vector(ara_output_power_smooth, ara_beams,
				bandE, B, NB, STOCK_ARA_POWER_SMOOTH,
				STOCK_ARA_POWER_BIN_LO, STOCK_ARA_POWER_BIN_HI);
			int diverged = 0;
			for (int b = 0; b < B; b++)
				if (bandE[b] > 3.0f * sel_snr[b])
					diverged = 1;
			if (hop_ara_active && diverged) {
				memset(ara_h, 0,
					(size_t)B * 2 * ara_parts * NB * sizeof(cpx));
				if (use_ara_vss)
					stock_generic_vss_reset(&ara_vss);
				memset(ara_input_power_smooth, 0,
					(size_t)B * NB * sizeof(*ara_input_power_smooth));
				memset(ara_output_power_smooth, 0,
					(size_t)B * NB * sizeof(*ara_output_power_smooth));
				ara_diverged++;
				hop_ara_diverged = 1;
			}
			selector_beams = ara_beams;
		}

		/* The current hop consumed the previous DTD decision.  Publish the
		 * recovered AEC-VSS aggregate now so it controls the following hop. */
		if (use_recovery_controller) {
			float next_aec_vss = hop_aec_vss_bins ?
				hop_aec_vss_sum / hop_aec_vss_bins : -1.0f;
			recovery_update_doubletalk(&recovery, next_aec_vss);
		}

		/* Measure every candidate after the stage selected above. */
		int best = pinned;
		if (best < 0) {
			if (!use_ara) {
				if (stock_selector) {
					/* Stock feeds 0x43bd0 a K=0.9875-smoothed corrEner
					 * vector.  Without -U there is deliberately no ARA, so
					 * smooth the post-ABF candidates in that exact temporal
					 * representation.  This preserves the accepted audio path
					 * while fixing the permanently-invalid decision state. */
					update_stock_power_vector(selector_power_smooth,
						selector_beams, bandE, B, NB,
						STOCK_ARA_POWER_SMOOTH,
						STOCK_ARA_POWER_BIN_LO,
						STOCK_ARA_POWER_BIN_HI);
				} else {
					for (int b = 0; b < B; b++) {
						double e = 0.0;
						for (int k = sel_k0; k <= sel_k1; k++) {
							if (stock_profile) {
								cpx y = selector_beams[(size_t)b * NB + k];
								e += (double)y.re * y.re +
									(double)y.im * y.im;
							} else {
								float sr = 0.0f, si = 0.0f;
								const float *wp = W.w +
									(((size_t)b * NB + k) * M) * 2;
								for (int i = 0; i < M; i++) {
									cpx x = spec[(size_t)i * NB + k];
									sr += wp[i * 2] * x.re -
										wp[i * 2 + 1] * x.im;
									si += wp[i * 2] * x.im +
										wp[i * 2 + 1] * x.re;
								}
								e += (double)sr * sr +
									(double)si * si;
							}
						}
						bandE[b] = (float)e;
					}
				}
			}
			if (stock_selector) {
				/* RefBeamSelector consumes VSBABF corrEner, which is the
				 * smoothed post-ABF beam power over the generic-canceller
				 * power band (200-7000 Hz on biscuit).  Our ABF output is
				 * already in selector_beams; retain a separate wide-band
				 * metric instead of overloading the speech-SNR energy. */
				/* RefBeamSelector's outputs become meaningful exclusions only
				 * after those same spectra have been installed as ARA references.
				 * The old no-ARA approximation used raw wide-band energy here;
				 * it could classify the talker's beam as a reference and force a
				 * destructive mid-word switch.  -U retains the exact stock
				 * RefBeamSelector -> ARA -> exclusion dataflow above. */
				if (!use_ara) {
					memset(ref_metric, 0, (size_t)B * sizeof(float));
					memset(ref_excluded, 0, (size_t)B);
				}
				/* Convert the retained /128 FBF representation back to the
				 * unnormalised stock power units consumed by 0x43bd0. */
				for (int b = 0; b < B; b++)
					bandE[b] *= STOCK_FBF_POWER_UNIT_SCALE;
				best = stock_selector_pick(&stock_sel, bandE,
					use_ara && hop_ara_active ? ref_excluded : NULL, B,
					&vad);
				for (int b = 0; b < B; b++)
					sel_snr[b] = stock_sel.snr_db[b];
			} else {
				best = pick_beam(bandE, floorE, B, cur_beam, energy_rule);
				cur_beam = best;
				for (int b = 0; b < B; b++)
					sel_snr[b] = 10.0f * log10f(bandE[b] /
						(float)(floorE[b] + 1e-30) + 1e-30f);
			}
		} else if (stock_selector) {
			stock_sel.current = best;
			stock_sel.residency++;
		}
		if (best >= 0 && best < 16)
			beam_hist[best]++;
		if (best == diag_last_beam) {
			diag_residency++;
		} else {
			if (diag_last_beam >= 0)
				diag_changes++;
			diag_last_beam = best;
			diag_residency = 1;
		}

		for (int k = 0; k < NB; k++) {
			if (stock_profile) {
				out[k] = selector_beams[(size_t)best * NB + k];
			} else {
				float sr = 0.0f, si = 0.0f;
				const float *wp = W.w + (((size_t)best * NB + k) * M) * 2;
				for (int i = 0; i < M; i++) {
					cpx x = spec[(size_t)i * NB + k];
					sr += wp[i * 2] * x.re - wp[i * 2 + 1] * x.im;
					si += wp[i * 2] * x.im + wp[i * 2 + 1] * x.re;
				}
				out[k].re = sr;
				out[k].im = si;
			}
		}

		if (use_abf && !stock_order) {
			long adapt_before = hop_abf_adapted;
			if (use_abf_vss) {
				float mean = 0.0f;
				const float *steps = abf_vss.step +
					((size_t)best * 2 + abf_vss.round_robin_ref) * NB;
				for (int k = 2; k <= 56 && k < NB; k++)
					mean += steps[k];
				mean /= 55.0f;
				hop_abf_vss_sum += mean;
				hop_abf_vss_beams++;
				if (diag)
					diag_abf_vss_beam[best] = mean;
			}
			process_abf_beam(out, out, best, abf_rhist, abf_h,
				abf_parts, NB, abf_history_pos, N, W.rate,
				use_abf_vss, use_abf_vss,
				use_abf_vss ? abf_vss.step + (size_t)best * 2 * NB : NULL,
				abf_vss.round_robin_ref,
				&abf_adapted, &hop_abf_adapted);
			if (diag) {
				diag_abf_adapt_beam[best] += hop_abf_adapted - adapt_before;
			}
			if (use_abf_vss)
				stock_abf_vss_update(&abf_vss, stock_beams, abf_rhist,
					B, abf_parts, NB, abf_history_pos);
		}

		/* The stock ASR topology does not enable its separate voice-path
		 * magnitude NR (its noiseInputRatio effectively bypasses it), so NR
		 * remains opt-in here.  When requested this is a conservative
		 * minimum-statistics magnitude suppressor: it can reduce stationary
		 * fan/codec noise but cannot turn a quiet word into silence. */
		if (use_nr) {
			for (int k = 0; k < NB; k++) {
				float power = cpx_power(out[k]);
				if (frames == 0)
					nr_floor[k] = power + 1e-12f;
				else if (power < nr_floor[k])
					nr_floor[k] = 0.8f * nr_floor[k] + 0.2f * power;
				else
					nr_floor[k] = fminf(power, nr_floor[k] * 1.005f);
				float gnr = 1.0f - nr_floor[k] / (power + 1e-12f);
				if (gnr < 0.10f) gnr = 0.10f;
				out[k].re *= gnr;
				out[k].im *= gnr;
			}
		}

		/* Hermitian-extend and inverse transform. */
		for (int k = 0; k < NB; k++)
			tdo[k] = out[k];
		for (int k = NB; k < N; k++) {
			tdo[k].re = out[N - k].re;
			tdo[k].im = -out[N - k].im;
		}
		fft_radix2(tdo, N, 1);

		if (stock_profile) {
			for (int i = 0; i < FB_LEN; i++)
				ola[i] += tdo[i % N].re * prototype[i];
		} else {
			for (int i = 0; i < N; i++)
				ola[i] += tdo[i].re * win[i];
		}

		for (int i = 0; i < HOP; i++) {
			float v = ola[i] * gain * output_stage_gain *
				(stock_profile ? fb_normalizer[i] : 1.0f);
			if (v > 1.0f) { v = 1.0f; clipped++; }
			else if (v < -1.0f) { v = -1.0f; clipped++; }
			if (fabsf(v) > peak) peak = fabsf(v);
			outbuf[i] = (int16_t)lrintf(v * 32767.0f);
		}
		if (fwrite(outbuf, sizeof(int16_t), HOP, stdout) != (size_t)HOP)
			break;
		if (diag) {
			float aec_input_power = hop_aec_metric_bins ?
				(float)hop_aec_input_power : -1.0f;
			float aec_echo_power = hop_aec_metric_bins ?
				(float)hop_aec_echo_power : -1.0f;
			float aec_error_power = hop_aec_metric_bins ?
				(float)hop_aec_error_power : -1.0f;
			float aec_input_echo_coherence = hop_aec_metric_bins ?
				(float)(hop_aec_input_echo_coherence / hop_aec_metric_bins) : -1.0f;
			float aec_error_echo_coherence = hop_aec_metric_bins ?
				(float)(hop_aec_error_echo_coherence / hop_aec_metric_bins) : -1.0f;
			float aec_vss_mean = hop_aec_vss_bins ?
				hop_aec_vss_sum / hop_aec_vss_bins : -1.0f;
			float aec_ratio_mean = hop_aec_ratio_mics ?
				hop_aec_ratio_sum / hop_aec_ratio_mics : -1.0f;
			float doubletalk_pct = hop_aec_vss_bins ?
				100.0f * hop_aec_doubletalk / hop_aec_vss_bins : 0.0f;
			float abf_vss_mean = hop_abf_vss_beams ?
				hop_abf_vss_sum / hop_abf_vss_beams : -1.0f;
			float ara_vss_mean = hop_ara_vss_bins ?
				hop_ara_vss_sum / hop_ara_vss_bins : -1.0f;
			fprintf(diag, "%ld\t%.6f\t%.9g\t%.9g\t%.9g\t%.9g\t"
				"%.6f\t%.6f\t%d\t%.6f\t%.6f\t%.3f\t%ld\t%ld\t"
				"%.6f\t%ld\t%.6f\t%ld\t%d\t%.9g\t%d\t%d\t"
				"%d\t%d\t%d\t%d\t%d\t%d\t%d",
				frames, (double)(frames + 1) * HOP / W.rate,
				hop_ref_power, aec_input_power, aec_echo_power, aec_error_power,
				aec_input_echo_coherence,
				aec_error_echo_coherence, hop_aec_fast_converge,
				aec_vss_mean, aec_ratio_mean,
				doubletalk_pct,
				hop_aec_adapted, hop_aec_diverged, abf_vss_mean,
				hop_abf_adapted, ara_vss_mean, hop_ara_adapted,
				hop_ara_diverged,
				use_recovery_controller ? recovery.reference_power : -1.0f,
				hop_ara_active, hop_ara_adapt_allowed, hop_dtd_freeze,
				use_recovery_controller ? recovery.dtd_attack_hops : 0,
				use_recovery_controller ? recovery.dtd_hangover_hops : 0,
				vad, best, diag_residency, diag_changes);
			for (int b = 0; b < B; b++)
				fprintf(diag, "\t%.4f", sel_snr[b]);
			for (int b = 0; b < B; b++)
				fprintf(diag, "\t%.9g", bandE[b]);
			for (int b = 0; b < B; b++)
				fprintf(diag, "\t%.9g", ref_metric[b]);
			for (int b = 0; b < B; b++)
				fprintf(diag, "\t%d",
					use_ara && hop_ara_active && ref_excluded[b] != 0);
			fprintf(diag, "\t%d\t%d\t%d\t%.9g",
				stock_ref.output[0], stock_ref.output[1],
				stock_ref.output_count, stock_ref.smoothed_threshold);
			for (int b = 0; b < B; b++)
				fprintf(diag, "\t%.6f", diag_abf_vss_beam[b]);
			for (int b = 0; b < B; b++)
				fprintf(diag, "\t%ld", diag_abf_adapt_beam[b]);
			fprintf(diag, "\n");
			if ((frames + 1) % 250 == 0)
				fflush(diag);
		}

		memmove(ola, ola + HOP, (FB_LEN - HOP) * sizeof(float));
		memset(ola + (FB_LEN - HOP), 0, HOP * sizeof(float));

		frames++;
		if (!quiet && frames % 300 == 0) {
			fprintf(stderr, "beamform: %lds, beams", frames * HOP / W.rate);
			for (int b = 0; b < B; b++)
				fprintf(stderr, " %d:%d%%", b,
					100 * beam_hist[b] / (int)frames);
			fprintf(stderr, "\n");
		}
	}

	ara_parallel_destroy(&ara_parallel);
	if (!quiet) {
		fprintf(stderr, "beamform: %ld frames (%.1f s), peak %.2f",
			frames, (double)frames * HOP / W.rate, peak);
		if (clipped)
			fprintf(stderr, ", CLIPPED %ld samples (%.2f%%) - lower -g",
				clipped, 100.0 * clipped / (frames * HOP));
		fprintf(stderr, "\n");
		if (use_aec)
			fprintf(stderr, "beamform: AEC adapted %ld frequency-bin frames\n",
				aec_adapted);
		if (use_aec)
			fprintf(stderr, "beamform: AEC divergence candidates %ld\n",
				aec_diverged);
		if (use_abf)
			fprintf(stderr, "beamform: ABF adapted %ld frequency-bin frames\n",
				abf_adapted);
		if (use_ara) {
			fprintf(stderr, "beamform: ARA adapted %ld frequency-bin frames\n",
				ara_adapted);
			fprintf(stderr, "beamform: ARA divergence resets %ld\n",
				ara_diverged);
			if (use_recovery_controller)
				fprintf(stderr, "beamform: ARA playback-edge resets %ld\n",
					ara_edge_resets);
		}
		if (use_recovery_controller)
			fprintf(stderr, "beamform: recovery reference onsets %ld, "
				"double-talk freezes %ld\n", recovery.reference_onsets,
				recovery.freeze_events);
	}
	if (diag && diag != stderr)
		fclose(diag);
	return 0;
}
