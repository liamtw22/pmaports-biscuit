/*
 * biscuit-dsp — playback DSP for the Echo Dot 2 speaker.
 *
 * Sits between everything that plays audio and the codec:
 *
 *     apps -> pcm."default" -> hw:Loopback,0,0  ==(snd-aloop)==  hw:Loopback,1,0
 *                                                                     |
 *                                        biscuit-dsp reads it, filters, and writes
 *                                                                     v
 *                                                                  hw:0,0
 *
 * Because everything writes to "default" - aplay, bluealsa-aplay, biscuit-audio.py -
 * one insertion point catches all audio without touching any of them.
 *
 * The chain mirrors stock's own order, which AFE.cfg spells out as
 * AVL -> UserEQ -> EQ (Equalizer FIR) -> MBCL:
 *

 *     stereo -> mono sum
 *       -> user EQ        parametric biquads, /etc/biscuit/user-eq.conf, ours
 *       -> speaker FIR    1024 taps, one curve - the owner's, never shipped
 *       -> volume push    extra gain at high volume, stock's per-volume tables
 *       -> 4-band MBCL    compressor + per-band limiter
 *       -> full-band limiter at -0.1 dBFS
 *
 * THERE IS ONLY ONE CORRECTION CURVE. Stock ships six per-volume EQ files and
 * they look like per-volume voicing. They are not: measured, all four distinct
 * files are the SAME magnitude shape to within 0.000 dB, differing only by a
 * scalar - -0.85 dB at 1 kHz for EQ_50/60/70, then +2.68, +6.70 and +13.70 for
 * EQ_80/90/100. So the per-volume files are a loudness push at high volume,
 * not a tonal change, and they are reproduced here as a gain table rather than
 * four convolutions. The shape itself is +26.6 dB at 170 Hz and -12.5 dB at
 * 2.5 kHz relative to 1 kHz.
 *
 * THE CURVE IS NOT PART OF THIS PACKAGE. It is Amazon's file, so each owner
 * imports their own copy (Fire OS's EQ_50.cfg, identical on Fire OS 5 and 6)
 * from the backup of their own Echo, and it is read from the owner-asset store
 * as it is - see load_curves(). With no curve imported the FIR is a unity
 * impulse and the speaker plays flat, behind the same compressor and limiter.
 *
 * WHY THE LIMITER IS NOT OPTIONAL. The speaker correction applies up to +24 dB
 * between 120 and 300 Hz. Without the compressor and limiter behind it that is
 * enough excursion to damage the driver. The two must always ship together;
 * bypassing MBCL while the FIR is active is not a supported configuration, and
 * -b bypasses both.
 *
 * WHAT IS AND IS NOT STOCK. The correction curve is the owner's imported
 * EQ_50.cfg, never a copy of it in this source. The MBCL parameters and the
 * per-volume push are a handful of numbers measured from the stock tuning
 * data and restated here; this code reimplements the observed stock chain, it
 * does not contain Amazon's. The compressor ATTACK times are not in that
 * data - stock's MBCL.cfg documents only release - so the attack constants
 * below are ours, chosen to be fast enough to catch transients without
 * audible pumping. The user EQ stage is entirely ours.
 *
 * The crossover uses successive subtraction rather than a textbook
 * Linkwitz-Riley tree, deliberately: the four bands then sum back to exactly
 * the input when no band is compressing, so the whole MBCL stage is provably
 * transparent at low level. A phase-imperfect band split matters far less here
 * than a guarantee that quiet material passes through unchanged.
 *
 * Build: cc -O2 -ffast-math -o biscuit-dsp biscuit-dsp.c -lasound -lm
 */

#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "biscuit-echo-reference.h"
static struct er_writer *render_reference;

#define RATE          48000
#define CHANNELS      2
#define PERIOD        256          /* 5.3 ms - low enough to stay responsive */
#define PERIODS       4
#define OUTPUT_PERIODS 8           /* 42.7 ms: absorb graph scheduling jitter */
#define FIR_TAPS      1024
#define NUM_STEPS     4
#define NUM_BANDS     4
#define MAX_USER_EQ   16
#define GAIN_SLEW_MS  120.0f       /* volume-push ramp */
#define LOOKAHEAD     192          /* 4 ms: stock has a dedicated 48 kHz MBC buffer */
/* Stock's libasp exposes LookAhead_ms and HoldTime_ms for MBLimiter but its
 * public MBCL.cfg exposes only threshold and release.  Sixteen milliseconds
 * is deliberately longer than the lowest band-2 waveform cycle (8.7 ms), so
 * a sustained note gets a stable envelope instead of audio-rate gain ripple.
 * It is an implementation default, not an invented tuning value. */
#define BAND_LIM_HOLD_MS 16.0f
#define COMP_HOLD_MS     16.0f
#define FULL_LIM_HOLD_MS 16.0f     /* keep a waveform peak, not its cycles */
/* ~10 s of silence before releasing the codec.
 *
 * Opening the codec powers the external amplifier and closing it powers it
 * down, and each transition is an audible pop, so the release wants to be lazy.
 * Two seconds cycled the amp after every short pause - a gap in a podcast, the
 * space between tracks on some players. Ten seconds covers those while still
 * letting the amp go quiet when the device is genuinely idle, which matters
 * because nothing else protects it thermally. */
/* A run of digital silence this long resets the dynamics - not the whole chain,
 * which IDLE_PERIODS still does when the codec is released.
 *
 * The bound is the shortest silence inside a sound we must NOT cut in half. The
 * earcons are 263 ms of audio whose longest internal run of exact zeros is
 * 0.4 ms, so 10 ms clears them by 25x while still catching the gap between two
 * fast button presses. Measured over presses spaced 400 down to 0 ms: at 200 ms
 * eight of eighteen came out with the wrong timbre, at 50 ms four, at 10 ms two
 * - and those two are presses with no gap at all, which no silence detector can
 * separate. Going below 10 ms buys nothing. */
#define DYN_RESET_SAMPLES 480      /* 10 ms */
#define IDLE_PERIODS  1875
/* How long a single "call on" holds the codec open without being refreshed.
 *
 * This is a WATCHDOG, not the call length. biscuit-btcall refreshes it every
 * few seconds for as long as the call is up, so a btcall that is killed, or
 * that dies mid-call, cannot leave the external amplifier powered forever -
 * which is the one outcome the idle release exists to prevent, on a board with
 * no audio thermal protection. Twenty seconds is four missed refreshes.
 */
#define CALL_HOLD_S   20.0

#define CTRL_FIFO     "/run/biscuit-dsp/control"
/* The speaker correction curve, in the order it is looked for:
 *
 *   EQ_OVERRIDE_FIR   a hand-placed curve, and the unity impulse biscuit_eq.py
 *                     writes for the "Off" mode. Wins over everything.
 *   EQ_FACTORY_CURVE  the owner's own Fire OS EQ_50.cfg, imported from their
 *                     backup (biscuit-import-assets, profile "speaker") and
 *                     read here unconverted. Never shipped in the package.
 *   neither           a unity impulse: flat, still behind MBCL and the limiter.
 *
 * Overridable at build time only so the loader can be tested off the device. */
#ifndef EQ_OVERRIDE_FIR
#define EQ_OVERRIDE_FIR  "/etc/biscuit/eq/speaker.fir"
#endif
#ifndef EQ_FACTORY_CURVE
#define EQ_FACTORY_CURVE "/opt/persist/biscuit/assets/speaker/EQ_50.cfg"
#endif
#define USER_EQ_CONF  "/etc/biscuit/user-eq.conf"
#define MBCL_CONF     "/etc/biscuit/mbcl.conf"

/* Stock's per-volume push, measured from its own EQ files and expressed
 * relative to the <=70 curve (EQ_50.cfg). Editable at
 * /etc/biscuit/mbcl.conf via push_80_db and friends. */
static const int   push_min_vol[NUM_STEPS] = { 0, 75, 85, 95 };
static float       push_db[NUM_STEPS]      = { 0.0f, 3.53f, 7.55f, 14.55f };

static volatile sig_atomic_t running = 1;
static int verbose;
static unsigned long xruns_in, xruns_out;

static void on_signal(int s) { (void)s; running = 0; }

static void logmsg(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	fflush(stderr);
}

/* ------------------------------------------------------------------ biquad */

typedef struct {
	float b0, b1, b2, a1, a2;
	float z1, z2;
} biquad;

static inline float bq_run(biquad *f, float x)
{
	/* Transposed direct form II: fewer state updates, better numerically at
	 * the low corner frequencies the bass band needs. */
	float y = f->b0 * x + f->z1;
	f->z1 = f->b1 * x - f->a1 * y + f->z2;
	f->z2 = f->b2 * x - f->a2 * y;
	return y;
}

static void bq_lowpass(biquad *f, float fc, float q)
{
	float w = 2.0f * (float)M_PI * fc / RATE;
	float alpha = sinf(w) / (2.0f * q);
	float c = cosf(w);
	float a0 = 1.0f + alpha;
	f->b0 = ((1.0f - c) * 0.5f) / a0;
	f->b1 = (1.0f - c) / a0;
	f->b2 = f->b0;
	f->a1 = (-2.0f * c) / a0;
	f->a2 = (1.0f - alpha) / a0;
	f->z1 = f->z2 = 0.0f;
}

/* Second-order allpass: unity magnitude, the exact phase of an LR4 crossover
 * pair.  Needed because LP4 + HP4 = AP2, not 1: see split_bands(). */
static void bq_allpass(biquad *f, float fc, float q)
{
	float w = 2.0f * (float)M_PI * fc / RATE;
	float alpha = sinf(w) / (2.0f * q);
	float c = cosf(w);
	float a0 = 1.0f + alpha;
	f->b0 = (1.0f - alpha) / a0;
	f->b1 = (-2.0f * c) / a0;
	f->b2 = 1.0f;
	f->a1 = (-2.0f * c) / a0;
	f->a2 = (1.0f - alpha) / a0;
	f->z1 = f->z2 = 0.0f;
}

static void bq_peaking(biquad *f, float fc, float q, float gain_db)
{
	float A = powf(10.0f, gain_db / 40.0f);
	float w = 2.0f * (float)M_PI * fc / RATE;
	float alpha = sinf(w) / (2.0f * q);
	float c = cosf(w);
	float a0 = 1.0f + alpha / A;
	f->b0 = (1.0f + alpha * A) / a0;
	f->b1 = (-2.0f * c) / a0;
	f->b2 = (1.0f - alpha * A) / a0;
	f->a1 = f->b1;
	f->a2 = (1.0f - alpha / A) / a0;
	f->z1 = f->z2 = 0.0f;
}

/* -------------------------------------------------------------- compressor */

typedef struct {
	float thresh_db, ratio, gain_min_db;
	float lim_thresh_db;
	float atk_c, rel_c;          /* envelope coefficients */
	float lim_atk_c, lim_rel_c;
	float env;                   /* detector, linear */
	float gain;                  /* smoothed gain, linear */
	int   env_hold;
	float lim_gain;
	float lim_env;               /* held peak for the multiband limiter */
	float la_sig[LOOKAHEAD];     /* MBLimiter's 48 kHz look-ahead buffer */
	int   la_pos, lim_hold;
} compband;

static float ms_to_coef(float ms)
{
	if (ms <= 0.0f)
		return 0.0f;
	return expf(-1.0f / (RATE * (ms / 1000.0f)));
}

static inline float db_to_lin(float db) { return powf(10.0f, db / 20.0f); }

static inline float lin_to_db(float x)
{
	return 20.0f * log10f(x < 1e-9f ? 1e-9f : x);
}

/* One band compressor.  The stock limiter is deliberately a separate stage:
 * it owns a look-ahead buffer and a held peak envelope, and cannot be safely
 * represented by multiplying the current waveform sample. */
static inline float comp_run(compband *c, float x)
{
	float a = fabsf(x);

	/* A peak held over more than one waveform cycle.  The old detector
	 * decayed between every 170 Hz peak, so even its slow one-pole envelope
	 * carried a 340 Hz ripple into the gain calculation.  Stock libasp exposes
	 * a HoldTime_ms control alongside level attack/release; this is the
	 * equivalent missing stage.  The separate MBLimiter still provides the
	 * safety-critical look-ahead for an abrupt transient. */
	if (a >= c->env) {
		c->env = a;
		c->env_hold = (int)ceilf(COMP_HOLD_MS * RATE / 1000.0f);
	} else if (a >= c->env * 0.999f) {
		c->env_hold = (int)ceilf(COMP_HOLD_MS * RATE / 1000.0f);
	} else if (c->env_hold > 0) {
		c->env_hold--;
	} else {
		c->env = a + c->rel_c * (c->env - a);
	}

	float env_db = lin_to_db(c->env);
	float want_db = 0.0f;

	if (env_db > c->thresh_db) {
		float over = env_db - c->thresh_db;
		want_db = -(over - over / c->ratio);
		if (want_db < c->gain_min_db)
			want_db = c->gain_min_db;
	}

	float want = db_to_lin(want_db);
	/* Smooth the gain itself, not just the detector: stepping gain per
	 * sample is what makes a compressor sound like distortion. */
	if (want < c->gain)
		c->gain = want + c->atk_c * (c->gain - want);
	else
		c->gain = want + c->rel_c * (c->gain - want);

	return x * c->gain;
}

/* Stock libasp names this stage MBLimiter/MBC48k.  A held peak detector is
 * applied to a 4 ms delayed band signal.  That gives its gain enough time to
 * settle before a peak reaches the output, while the hold prevents each sine
 * cycle from changing the gain.  All four bands use the same delay so their
 * phase relationship remains intact when recombined. */
static inline float mbl_run(compband *c, float x)
{
	float lim = db_to_lin(c->lim_thresh_db);
	float aim = lim * 0.98f;
	float a = fabsf(x);

	if (a >= c->lim_env) {
		c->lim_env = a;
		c->lim_hold = (int)ceilf(BAND_LIM_HOLD_MS * RATE / 1000.0f);
	} else if (a >= c->lim_env * 0.999f) {
		c->lim_hold = (int)ceilf(BAND_LIM_HOLD_MS * RATE / 1000.0f);
	} else if (c->lim_hold > 0) {
		c->lim_hold--;
	} else {
		c->lim_env = a + c->lim_rel_c * (c->lim_env - a);
	}

	float target = (c->lim_env > aim && c->lim_env > 1e-9f)
		? aim / c->lim_env : 1.0f;
	c->la_sig[c->la_pos] = x;
	c->la_pos = (c->la_pos + 1) % LOOKAHEAD;
	float delayed = c->la_sig[c->la_pos];

	if (target <= c->lim_gain)
		c->lim_gain = target + c->lim_atk_c * (c->lim_gain - target);
	else
		c->lim_gain = target + c->lim_rel_c * (c->lim_gain - target);
	return delayed * c->lim_gain;
}

/* ------------------------------------------------------------------ engine */

typedef struct {
	/* speaker correction: one curve, plus the per-volume loudness push */
	float fir[FIR_TAPS];
	int   have_fir;
	float hist[FIR_TAPS * 2];    /* doubled so the convolution never wraps */
	int   hpos;
	float push, push_target;     /* linear gain, slewed */
	float push_slew;

	/* user EQ */
	biquad user_eq[MAX_USER_EQ];
	int    n_user_eq;

	/* MBCL */
	int      mbcl_bypass;
	biquad   split[3][2];        /* 2nd-order Butterworth, cascaded pairs */
	biquad   split_ap[3];        /* AP2 matching each LR4 split */
	/* Phase-alignment allpasses.  A biquad carries state, so each of these
	 * filters exactly one signal: [0] and [1] align band 1 through the two
	 * crossovers above it, [2] aligns band 2 through the one above it. */
	biquad   align_b1_mid, align_b1_high, align_b2_high;
	compband band[NUM_BANDS];
	float    full_lim_thresh_db, full_lim_atk_c, full_lim_rel_c, full_lim_gain;
	float    full_lim_env;
	int      full_lim_hold;
	float    la_sig[LOOKAHEAD];  /* delayed signal */
	int      la_pos;
	float    in_gain;
	float    post_gain;          /* final stock-match loudness trim */
	float    vol_post, vol_post_target;  /* volume taper, applied after the limiter */
	int      vol_after_lim;

	int volume;
	int silent_periods;
	int zero_run, dyn_reset_done;
	int wake_request;            /* open the codec before audio arrives */
	int cast_mute;               /* audio is going to Bluetooth instead */
	double call_until;           /* monotonic deadline: hold the codec open */
} engine;

static void split_init(engine *e, int i, float fc)
{
	/* Two cascaded Q=0.707 sections = 4th-order Butterworth lowpass. */
	bq_lowpass(&e->split[i][0], fc, 0.70710678f);
	bq_lowpass(&e->split[i][1], fc, 0.70710678f);
	bq_allpass(&e->split_ap[i], fc, 0.70710678f);
}

static inline float split_lp(engine *e, int i, float x)
{
	return bq_run(&e->split[i][1], bq_run(&e->split[i][0], x));
}

/* Read a curve file: one float per line, optional trailing comma, '#' comments.
 * That is both the format of Fire OS's EQ_50.cfg ("8.135e-01," per line) and
 * of a hand-made override, so the imported file is read as it is, with no
 * conversion step and no second copy of it anywhere.
 *
 * `exact` is for the imported file: it must hold exactly `taps` finite values,
 * or it is refused and the caller falls through to flat. The importer already
 * checked its SHA-256, so this only catches a file damaged since. An override
 * is someone's deliberate choice and stays lenient: a short curve is padded
 * with zeros rather than rejected, which is what makes the one-line unity
 * impulse work.
 *
 * Parsed into a scratch buffer, so a rejected file never leaves half a curve
 * in `dst`. Returns 0 on success, -1 if the file is absent, -2 if refused. */
static int load_fir(const char *path, float *dst, int taps, int exact)
{
	FILE *f = fopen(path, "r");
	if (!f)
		return -1;
	float tmp[FIR_TAPS];
	char line[128];
	int n = 0, extra = 0, bad = 0;
	if (taps > FIR_TAPS)
		taps = FIR_TAPS;
	while (fgets(line, sizeof line, f)) {
		char *p = line, *end;
		while (*p == ' ' || *p == '\t')
			p++;
		if (*p == '#' || *p == '\n' || *p == '\r' || *p == '\0')
			continue;
		if (n >= taps) {
			extra++;
			if (!exact)
				break;
			continue;
		}
		float v = strtof(p, &end);
		/* Finite by the exponent bits, not isfinite(): this is built with
		 * -ffast-math, which lets the compiler assume isfinite() is true. */
		uint32_t bits;
		memcpy(&bits, &v, sizeof bits);
		if (end == p || (bits & 0x7f800000u) == 0x7f800000u)
			bad++;
		tmp[n++] = v;
	}
	fclose(f);
	if (n == 0 || bad || (exact && (n != taps || extra)))
		return -2;
	while (n < taps)
		tmp[n++] = 0.0f;
	memcpy(dst, tmp, (size_t)taps * sizeof *dst);
	return 0;
}

static void load_curves(engine *e)
{
	int r = load_fir(EQ_OVERRIDE_FIR, e->fir, FIR_TAPS, 0);
	if (r == 0) {
		e->have_fir = 1;
		logmsg("eq: %s (override)", EQ_OVERRIDE_FIR);
		return;
	}
	if (r == -2)
		logmsg("eq: %s has no usable coefficients - ignored", EQ_OVERRIDE_FIR);
	r = load_fir(EQ_FACTORY_CURVE, e->fir, FIR_TAPS, 1);
	if (r == 0) {
		e->have_fir = 1;
		logmsg("eq: %s (the owner's factory curve)", EQ_FACTORY_CURVE);
		return;
	}
	/* No curve: unity impulse, i.e. pass through. A missing correction file
	 * must never mean silence, and it must never mean an unfiltered +24 dB
	 * either - passing through is the safe failure. */
	memset(e->fir, 0, sizeof e->fir);
	e->fir[0] = 1.0f;
	e->have_fir = 0;
	if (r == -2)
		logmsg("eq: %s is not %d coefficients - refused, playing flat",
		       EQ_FACTORY_CURVE, FIR_TAPS);
	else
		logmsg("eq: no factory curve imported (%s) - playing flat",
		       EQ_FACTORY_CURVE);
}

/* user-eq.conf:  peak <freq_hz> <q> <gain_db>   (one per line) */
static void load_user_eq(engine *e)
{
	e->n_user_eq = 0;
	FILE *f = fopen(USER_EQ_CONF, "r");
	if (!f)
		return;
	char line[256];
	while (fgets(line, sizeof line, f) && e->n_user_eq < MAX_USER_EQ) {
		float fc, q, g;
		if (sscanf(line, " peak %f %f %f", &fc, &q, &g) == 3) {
			if (fc <= 0.0f || fc >= RATE / 2 || q <= 0.0f)
				continue;
			bq_peaking(&e->user_eq[e->n_user_eq++], fc, q, g);
		}
	}
	fclose(f);
	if (e->n_user_eq)
		logmsg("user eq: %d band(s)", e->n_user_eq);
}

/* mbcl.conf: flat key=value, defaults are stock's MBCL.cfg values. */
static void load_mbcl(engine *e)
{
	float fc[3] = { 115.0f, 500.0f, 7500.0f };
	float thresh[NUM_BANDS]   = { -50.0f, -10.0f, -10.0f, -10.0f };
	float ratio[NUM_BANDS]    = {  20.0f,   2.0f,   2.0f,   2.0f };
	float gmin[NUM_BANDS]     = { -40.0f, -40.0f, -40.0f, -40.0f };
	float limth[NUM_BANDS]    = {  -8.0f,   0.0f,   0.0f,   0.0f };
	float limrel[NUM_BANDS]   = { 200.0f,   1.0f,   1.0f,   1.0f };
	float full_th = -0.1f, full_rel = 200.0f, in_gain = 0.0f;
	int   vol_after_lim = 1;
	/* Stock gain staging uses a neutral terminal trim.  Keep all available
	 * headroom at the codec/amplifier instead of compensating with a post-MBCL
	 * attenuation. */
	float output_trim_db = 0.0f;
	int bypass = 0;

	FILE *f = fopen(MBCL_CONF, "r");
	if (f) {
		char k[64];
		float v;
		char line[256];
		while (fgets(line, sizeof line, f)) {
			if (sscanf(line, " %63[a-z_0-9] = %f", k, &v) != 2)
				continue;
			if (!strcmp(k, "bypass"))            bypass = (int)v;
			else if (!strcmp(k, "in_gain_db"))   in_gain = v;
			else if (!strcmp(k, "output_trim_db")) output_trim_db = v;
			else if (!strcmp(k, "fc1"))          fc[0] = v;
			else if (!strcmp(k, "fc2"))          fc[1] = v;
			else if (!strcmp(k, "fc3"))          fc[2] = v;
			else if (!strcmp(k, "full_lim_thresh_db")) full_th = v;
			else if (!strcmp(k, "full_lim_release_ms")) full_rel = v;
			else if (!strcmp(k, "volume_after_limiter")) vol_after_lim = (int)v;
			else if (!strcmp(k, "push_80_db"))   push_db[1] = v;
			else if (!strcmp(k, "push_90_db"))   push_db[2] = v;
			else if (!strcmp(k, "push_100_db"))  push_db[3] = v;
			else if (!strncmp(k, "band", 4) && strlen(k) > 6) {
				int b = k[4] - '1';
				const char *p = k + 6;
				if (b < 0 || b >= NUM_BANDS)
					continue;
				if (!strcmp(p, "thresh_db"))       thresh[b] = v;
				else if (!strcmp(p, "ratio"))      ratio[b] = v;
				else if (!strcmp(p, "gain_min_db")) gmin[b] = v;
				else if (!strcmp(p, "lim_thresh_db")) limth[b] = v;
				else if (!strcmp(p, "lim_release_ms")) limrel[b] = v;
			}
		}
		fclose(f);
		logmsg("mbcl: %s", MBCL_CONF);
	}

	e->mbcl_bypass = bypass;
	e->in_gain = db_to_lin(in_gain);
	e->post_gain = db_to_lin(output_trim_db);
	e->vol_after_lim = vol_after_lim;
	for (int i = 0; i < 3; i++)
		split_init(e, i, fc[i]);
	/* Band 1 must traverse the allpass of every crossover above it and
	 * band 2 the one above it, so all four bands arrive phase-aligned
	 * and sum to a flat magnitude. */
	bq_allpass(&e->align_b1_mid,  fc[1], 0.70710678f);
	bq_allpass(&e->align_b1_high, fc[2], 0.70710678f);
	bq_allpass(&e->align_b2_high, fc[2], 0.70710678f);

	for (int b = 0; b < NUM_BANDS; b++) {
		compband *c = &e->band[b];
		memset(c, 0, sizeof *c);
		c->thresh_db     = thresh[b];
		c->ratio         = ratio[b] < 1.0f ? 1.0f : ratio[b];
		c->gain_min_db   = gmin[b];
		c->lim_thresh_db = limth[b];
		/* Time constants are floored by the band's own lowest frequency.
		 *
		 * A detector faster than the waveform it is watching tracks
		 * individual CYCLES instead of the envelope, and the gain then
		 * modulates at the signal frequency - which is distortion, not
		 * compression. Stock's band 2 release is 1 ms while band 2
		 * starts at 115 Hz, an 8.7 ms period; taken literally it
		 * measured 3.5% THD at 120 Hz and 5.2% at 170 Hz, against
		 * 0.004% bypassed. Band 1 was clean because its release is
		 * 200 ms, which is what pointed at the cause.
		 *
		 * So: attack at least one cycle, release at least three, of the
		 * lowest frequency the band carries. Stock's values are kept
		 * wherever they are already slower than that, which is every
		 * band above 500 Hz.
		 */
		float f_ref = (b == 0) ? 40.0f : (b == 1) ? fc[0]
			    : (b == 2) ? fc[1] : fc[2];
		float period_ms = 1000.0f / f_ref;
		/* Three cycles attack, ten release. One cycle is not enough: a
		 * peak detector fed a steady sine ripples at twice the signal
		 * frequency, and that ripple becomes gain modulation. One cycle
		 * only got 170 Hz from 5.15% THD down to 2.61%; the ripple has
		 * to be smoothed over several cycles to disappear. Slow bass
		 * compression is correct anyway - there is no transient in a
		 * 170 Hz tone worth catching in 9 ms. */
		float atk_ms = 3.0f * period_ms;
		float rel_ms = limrel[b] > 10.0f * period_ms ? limrel[b]
							     : 10.0f * period_ms;
		c->atk_c     = ms_to_coef(atk_ms);
		c->rel_c     = ms_to_coef(rel_ms);
		/* With the 4 ms look-ahead buffer, a sub-millisecond attack reaches
		 * the requested reduction before the delayed peak is emitted. */
		c->lim_atk_c = ms_to_coef(0.5f);
		c->lim_rel_c = ms_to_coef(rel_ms);
		c->env = 0.0f;
		c->gain = 1.0f;
		c->env_hold = 0;
		c->lim_gain = 1.0f;
		c->lim_env = 0.0f;
		c->lim_hold = 0;
		c->la_pos = 0;
		for (int i = 0; i < LOOKAHEAD; i++)
			c->la_sig[i] = 0.0f;
	}

	/* Band 1 must traverse the allpass of every crossover above it, band 2
	 * the one above it, so all four bands arrive phase-aligned and sum flat. */
	bq_allpass(&e->align_b1_mid,  fc[1], 0.70710678f);
	bq_allpass(&e->align_b1_high, fc[2], 0.70710678f);
	bq_allpass(&e->align_b2_high, fc[2], 0.70710678f);

	e->full_lim_thresh_db = full_th;
	/* Settles in roughly LOOKAHEAD/5 * 5 samples, i.e. inside the lookahead
	 * window, so the reduction is complete before the peak arrives. */
	e->full_lim_atk_c = ms_to_coef((LOOKAHEAD / 5.0f) * 1000.0f / RATE);
	e->full_lim_rel_c = ms_to_coef(full_rel);
	e->full_lim_gain = 1.0f;
	e->full_lim_env = 0.0f;
	e->full_lim_hold = 0;
	for (int i = 0; i < LOOKAHEAD; i++) {
		e->la_sig[i] = 0.0f;
	}
	e->la_pos = 0;
}

/*
 * Total pre-MBCL gain for a volume percentage.
 *
 * Two separate things are combined here:
 *
 *   push_db[NUM_STEPS-1]  is calibration, not volume.  Our single correction
 *      curve is the owner's imported EQ_50; stock's EQ_100 is the same filter scaled by
 *      5.3348 (+14.54 dB, measured: the six files are pure scalars of one
 *      another, residual 0.00000).  Applying it always makes our EQ stage
 *      equal stock's at full volume.
 *
 *   the taper is the volume control itself.  Loudness roughly halves per
 *      10 dB, so 10*log2(fraction) makes the slider proportional to perceived
 *      loudness: half reads half, a quarter reads a quarter.  Stock is far
 *      steeper than this (its own half is about a fifth) and the old codec
 *      taper here was steeper still, which is why most of the useful range
 *      was bunched into the top of the slider.
 *
 * This is applied BEFORE MBCL, which is the point.  Attenuating after the DSP
 * - as the codec register used to - left the compressor seeing a full-scale
 * signal at every volume, so quiet listening was squashed exactly as hard as
 * loud listening.  Stock attenuates pre-MBCL and leaves its codec at unity;
 * its HP driver gain register reads 0x00 at every volume.
 */
#define VOL_DB_PER_HALVING  10.0f

static float volume_taper_db(int vol)
{
	if (vol <= 0)
		return -120.0f;
	return VOL_DB_PER_HALVING * (logf((float)vol / 100.0f) / logf(2.0f));
}

static float push_for_volume(int vol, int after_lim)
{
	float cal = push_db[NUM_STEPS - 1];
	if (vol <= 0 && !after_lim)
		return -120.0f;
	/* With the taper after the limiter the pre-MBCL gain is calibration
	 * only, so the compressors always see the same level and the taper
	 * cannot be handed back by them. */
	if (after_lim)
		return cal;
	return cal + volume_taper_db(vol);
}

static void set_volume(engine *e, int vol)
{
	if (vol < 0) vol = 0;
	if (vol > 100) vol = 100;
	e->volume = vol;
	float prev_post = e->vol_post_target;
	float db = push_for_volume(vol, e->vol_after_lim);
	e->vol_post_target = e->vol_after_lim
		? db_to_lin(volume_taper_db(vol)) : 1.0f;
	if (e->vol_after_lim && vol <= 0)
		e->vol_post_target = 0.0f;
	float want = db_to_lin(db);
	/* Report on any change, not just a change of push. With the taper after
	 * the limiter the push is constant, so keying the message off it alone
	 * made every volume change silent in the log. */
	int changed = (want != e->push_target) || (prev_post != e->vol_post_target);
	e->push_target = want;
	if (verbose && changed)
		logmsg("volume %d -> push %+.2f dB, taper %+.2f dB", vol, db,
		       e->vol_after_lim ? volume_taper_db(vol) : 0.0f);
}

static inline float fir_apply(engine *e, int pos)
{
	const float *c = e->fir;
	const float *h = &e->hist[pos];
	float acc = 0.0f;
	for (int i = 0; i < FIR_TAPS; i++)
		acc += c[i] * h[-i];
	return acc;
}

/* Clear every filter's state. Used when the codec is released: the FIR tail,
 * the compressor envelopes and the limiter's delay line all refer to audio that
 * is now seconds old, and carrying them into the next stream would produce a
 * burst of stale signal at its start. */
/* Just the dynamics: every compressor and limiter detector, its smoothed gain
 * and its look-ahead buffer. The compressors rest at 0 dB but operate around
 * -33 dB, so the first sound after a silence is fuller and about 1.6 dB quieter
 * than the same sound repeated - which is audible, and was reported as the
 * second beep sounding thinner than the first. Recovering that state naturally
 * takes about four seconds; this makes any gap longer than DYN_RESET_SAMPLES
 * start from the same place, so repeated earcons sound identical.
 *
 * The FIR history and the crossover biquads are deliberately left alone: their
 * tails have already decayed to nothing after 200 ms of zeros, and truncating a
 * tail that had not would be an audible edge. */
static void reset_dynamics(engine *e)
{
	for (int b = 0; b < NUM_BANDS; b++) {
		e->band[b].env = 0.0f;
		e->band[b].gain = 1.0f;
		e->band[b].env_hold = 0;
		e->band[b].lim_gain = 1.0f;
		e->band[b].lim_env = 0.0f;
		e->band[b].lim_hold = 0;
		e->band[b].la_pos = 0;
		for (int i = 0; i < LOOKAHEAD; i++)
			e->band[b].la_sig[i] = 0.0f;
	}
	for (int i = 0; i < LOOKAHEAD; i++)
		e->la_sig[i] = 0.0f;
	e->la_pos = 0;
	e->full_lim_gain = 1.0f;
	e->full_lim_env = 0.0f;
	e->full_lim_hold = 0;
}

static void reset_state(engine *e)
{
	memset(e->hist, 0, sizeof e->hist);
	e->hpos = 0;
	for (int i = 0; i < e->n_user_eq; i++)
		e->user_eq[i].z1 = e->user_eq[i].z2 = 0.0f;
	for (int i = 0; i < 3; i++)
		e->split[i][0].z1 = e->split[i][0].z2 =
		e->split[i][1].z1 = e->split[i][1].z2 = 0.0f;
	for (int b = 0; b < NUM_BANDS; b++) {
		e->band[b].env = 0.0f;
		e->band[b].gain = 1.0f;
		e->band[b].env_hold = 0;
		e->band[b].lim_gain = 1.0f;
		e->band[b].lim_env = 0.0f;
		e->band[b].lim_hold = 0;
		e->band[b].la_pos = 0;
		for (int i = 0; i < LOOKAHEAD; i++)
			e->band[b].la_sig[i] = 0.0f;
	}
	for (int i = 0; i < LOOKAHEAD; i++)
		e->la_sig[i] = 0.0f;
	e->la_pos = 0;
	e->full_lim_gain = 1.0f;
	e->full_lim_env = 0.0f;
	e->full_lim_hold = 0;
}

static void process(engine *e, float *buf, int frames)
{
	for (int n = 0; n < frames; n++) {
		if (buf[n] == 0.0f) {
			if (e->zero_run < DYN_RESET_SAMPLES)
				e->zero_run++;
			if (e->zero_run >= DYN_RESET_SAMPLES && !e->dyn_reset_done) {
				reset_dynamics(e);
				e->dyn_reset_done = 1;
			}
		} else {
			e->zero_run = 0;
			e->dyn_reset_done = 0;
		}
		float x = buf[n] * e->in_gain;

		for (int i = 0; i < e->n_user_eq; i++)
			x = bq_run(&e->user_eq[i], x);

		/* History is kept twice so the FIR loop can walk backwards
		 * without a modulo on every tap. */
		e->hist[e->hpos] = x;
		e->hist[e->hpos + FIR_TAPS] = x;

		float y = fir_apply(e, e->hpos + FIR_TAPS);

		/* Volume push, slewed. Stepping it would click, and at the top
		 * step it is a 14.6 dB jump. */
		e->push += (e->push_target - e->push) * e->push_slew;
		y *= e->push;

		if (++e->hpos >= FIR_TAPS)
			e->hpos = 0;

		if (!e->mbcl_bypass) {
			/* Linkwitz-Riley tree.  The previous code took each
			 * residual as a plain subtraction, y - LP4(y).  That is
			 * NOT the complementary highpass: for LR4 the identity is
			 * LP4 + HP4 = AP2, so the plain residual overshoots and
			 * the individual bands come out LARGER than the input -
			 * measured 1.598x (+4.1 dB) at 350 Hz.  Each band's
			 * detector then saw a signal several dB hotter than
			 * reality and compressed that much too early, costing
			 * about 8 dB at 350 Hz and making the transfer function
			 * non-monotonic.
			 *
			 * Subtracting from the matching allpass instead gives the
			 * true HP4.  The four bands now sum to an allpass rather
			 * than to y exactly: magnitude is flat, phase is not, and
			 * no band ever exceeds the input.  The lower bands are
			 * passed through the later crossovers' allpasses so the
			 * sum stays flat. */
			float lo1 = split_lp(e, 0, y);
			float hi1 = bq_run(&e->split_ap[0], y) - lo1;
			float lo2 = split_lp(e, 1, hi1);
			float hi2 = bq_run(&e->split_ap[1], hi1) - lo2;
			float lo3 = split_lp(e, 2, hi2);
			float b4  = bq_run(&e->split_ap[2], hi2) - lo3;

			float b1 = bq_run(&e->align_b1_high,
					  bq_run(&e->align_b1_mid, lo1));
			float b2 = bq_run(&e->align_b2_high, lo2);
			float b3 = lo3;

			y = mbl_run(&e->band[0], comp_run(&e->band[0], b1))
			  + mbl_run(&e->band[1], comp_run(&e->band[1], b2))
			  + mbl_run(&e->band[2], comp_run(&e->band[2], b3))
			  + mbl_run(&e->band[3], comp_run(&e->band[3], b4));

			/* Final limiter: 4 ms of lookahead plus a held peak envelope.
			 * The hold is longer than the slowest waveform cycle, so a steady
			 * tone produces one stable gain rather than audio-rate gain ripple.
			 * A new peak is still seen before its delayed sample reaches the
			 * output, while the 200 ms release preserves stock's recovery. */
			/* Aim 2% under the ceiling. A one-pole never quite reaches
			 * its target, so aiming exactly at the ceiling settles
			 * just above it - measured -0.03 dBFS against a -0.1
			 * target. Aiming slightly under puts the settled value
			 * below the ceiling, and the clamp further down stays
			 * inactive rather than doing the work. */
			float lim = db_to_lin(e->full_lim_thresh_db);
			float aim = lim * 0.98f;
			float ay = fabsf(y);
			if (ay >= e->full_lim_env) {
				e->full_lim_env = ay;
				e->full_lim_hold = (int)ceilf(FULL_LIM_HOLD_MS * RATE / 1000.0f);
			} else if (ay >= e->full_lim_env * 0.999f) {
				e->full_lim_hold = (int)ceilf(FULL_LIM_HOLD_MS * RATE / 1000.0f);
			} else if (e->full_lim_hold > 0) {
				e->full_lim_hold--;
			} else {
				e->full_lim_env = ay + e->full_lim_rel_c *
						 (e->full_lim_env - ay);
			}
			float target = (e->full_lim_env > aim && e->full_lim_env > 1e-9f)
				? aim / e->full_lim_env : 1.0f;

			e->la_sig[e->la_pos] = y;
			e->la_pos = (e->la_pos + 1) % LOOKAHEAD;

			/* Oldest entry is the one now due for output. */
			float delayed = e->la_sig[e->la_pos];


			/* The lookahead gives this attack enough time to settle before the
			 * newly detected peak is read from the delay line. */
			if (target <= e->full_lim_gain)
				e->full_lim_gain = target + e->full_lim_atk_c *
						   (e->full_lim_gain - target);
			else
				e->full_lim_gain = target + e->full_lim_rel_c *
						   (e->full_lim_gain - target);

			y = delayed * e->full_lim_gain;
			/* Keep the volume correction after all stock-equivalent dynamics.
			 * Putting it before MBCL would change when the bands compress, which
			 * is not a volume calibration. */
			y *= e->post_gain;
			/* The volume taper lives here, after every stock-equivalent
			 * dynamics stage. Applied before MBCL it was simply handed
			 * back by the compressors: at volume 100 the chain sits
			 * 11.5 dB into limiting, so dropping the pre-MBCL gain by
			 * 10 dB moved an earcon by 0.84 dB and the top half of the
			 * slider did nothing. Here it is a plain scalar, so every
			 * halving is exactly VOL_DB_PER_HALVING on any material.
			 * The cost is that the dynamics no longer ease off at low
			 * volume - measured about 2 dB of crest factor on mid-level
			 * content. Set volume_after_limiter = 0 to get the old
			 * behaviour back. */
			e->vol_post += (e->vol_post_target - e->vol_post) * e->push_slew;
			y *= e->vol_post;
		}

		/* Belt and braces. The limiter should make this unreachable, but
		 * a hand-edited config could raise the ceiling above 0 dBFS and
		 * wrapping is far worse than clipping. */
		if (y > 1.0f) y = 1.0f;
		if (y < -1.0f) y = -1.0f;

		buf[n] = y;
	}
}

/* ------------------------------------------------------------------- alsa */

static int open_pcm(snd_pcm_t **pcm, const char *dev, snd_pcm_stream_t dir)
{
	int err = snd_pcm_open(pcm, dev, dir, 0);
	if (err < 0) {
		logmsg("open %s: %s", dev, snd_strerror(err));
		return err;
	}
	snd_pcm_hw_params_t *hw;
	snd_pcm_hw_params_alloca(&hw);
	snd_pcm_hw_params_any(*pcm, hw);
	snd_pcm_hw_params_set_access(*pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED);
	snd_pcm_hw_params_set_format(*pcm, hw, SND_PCM_FORMAT_S16_LE);
	snd_pcm_hw_params_set_channels(*pcm, hw, CHANNELS);
	unsigned rate = RATE;
	snd_pcm_hw_params_set_rate_near(*pcm, hw, &rate, 0);
	snd_pcm_uframes_t period = PERIOD;
	snd_pcm_hw_params_set_period_size_near(*pcm, hw, &period, 0);
	unsigned periods = dir == SND_PCM_STREAM_PLAYBACK ? OUTPUT_PERIODS : PERIODS;
	snd_pcm_hw_params_set_periods_near(*pcm, hw, &periods, 0);
	err = snd_pcm_hw_params(*pcm, hw);
	if (err < 0) {
		logmsg("hw_params %s: %s", dev, snd_strerror(err));
		snd_pcm_close(*pcm);
		return err;
	}
	if (rate != RATE)
		logmsg("warning: %s runs at %u Hz, not %d - the correction curves "
		       "are designed for %d and will be shifted", dev, rate, RATE, RATE);

	/* A playback PCM defaults to start_threshold=1.  That starts the codec on
	 * the FIRST silence period below, before the rest of the intended 7-period
	 * cushion is queued, and was the direct cause of recurring output xruns.
	 * Start only once the buffer is nearly full.  It adds at most 37 ms at a
	 * stream start, but prevents the DAC from being scheduled with an empty
	 * buffer.  Capture keeps ALSA's normal start policy. */
	if (dir == SND_PCM_STREAM_PLAYBACK) {
		snd_pcm_sw_params_t *sw;
		snd_pcm_uframes_t buf_frames, actual_period;
		snd_pcm_sw_params_alloca(&sw);
		snd_pcm_hw_params_get_buffer_size(hw, &buf_frames);
		snd_pcm_hw_params_get_period_size(hw, &actual_period, 0);
		snd_pcm_sw_params_current(*pcm, sw);
		snd_pcm_uframes_t start = buf_frames > actual_period
			? buf_frames - actual_period : actual_period;
		snd_pcm_sw_params_set_start_threshold(*pcm, sw, start);
		snd_pcm_sw_params_set_avail_min(*pcm, sw, actual_period);
		err = snd_pcm_sw_params(*pcm, sw);
		if (err < 0) {
			logmsg("sw_params %s: %s", dev, snd_strerror(err));
			snd_pcm_close(*pcm);
			return err;
		}
		if (verbose)
			logmsg("%s: period=%lu buffer=%lu start=%lu", dev,
			       (unsigned long)actual_period, (unsigned long)buf_frames,
			       (unsigned long)start);
	}
	return 0;
}

/* Open the playback device and give it a cushion of silence.
 *
 * A freshly opened PCM starts empty, so writing one period at a time underruns
 * repeatedly until the buffer happens to fill - which is audible as a burst of
 * fuzz for the first seconds of every stream. It only became obvious once the
 * codec started being released when idle, because that made every playback a
 * fresh open. Giving it a cushion up front costs a few ms of latency and
 * removes the whole class of problem. */
static int open_output(snd_pcm_t **out, const char *dev, int verbose)
{
	if (open_pcm(out, dev, SND_PCM_STREAM_PLAYBACK) != 0)
		return -1;
	er_writer_reset(render_reference);
	short *sil = calloc(PERIOD * CHANNELS, sizeof(short));
	if (sil) {
		for (unsigned p = 0; p + 1 < OUTPUT_PERIODS; p++) {
			snd_pcm_sframes_t n=snd_pcm_writei(*out, sil, PERIOD);
			if (n>0) er_writer_push(render_reference,sil,(unsigned)n);
		}
		free(sil);
	}
	if (verbose)
		logmsg("codec opened and primed");
	return 0;
}

/* CLOCK_MONOTONIC, never time(). The RTC on this board reads 2069 until
 * chrony corrects it, so a wall-clock deadline set before the correction would
 * sit decades in the future and pin the amplifier on for the whole uptime.
 */
static double now_monotonic(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static void drain_control(engine *e, int fd)
{
	char buf[256];
	ssize_t n = read(fd, buf, sizeof buf - 1);
	if (n <= 0)
		return;
	buf[n] = '\0';
	char *save = NULL;
	for (char *line = strtok_r(buf, "\n", &save); line;
	     line = strtok_r(NULL, "\n", &save)) {
		int v;
		if (sscanf(line, "volume %d", &v) == 1) {
			set_volume(e, v);
		} else if (!strncmp(line, "wake", 4)) {
			/* Open the codec now, before the sound that prompted this
			 * exists. Releasing it when idle powers the amplifier down,
			 * and bringing it back up takes long enough that the start
			 * of a short earcon was being played into an amplifier that
			 * had not finished settling - which is why the first press
			 * after a quiet spell seemed to do nothing. The caller sends
			 * this as the button is handled, so the player's own start-up
			 * covers the wake. */
			e->wake_request = 1;
		} else if (!strncmp(line, "reload", 6)) {
			load_curves(e);
			load_user_eq(e);
			load_mbcl(e);
			set_volume(e, e->volume);
			logmsg("reloaded");
		} else if (!strncmp(line, "bypass ", 7)) {
			e->mbcl_bypass = atoi(line + 7);
			logmsg("mbcl bypass=%d", e->mbcl_bypass);
		} else if (!strncmp(line, "call ", 5)) {
			/* Hold the codec open for the duration of a call.
			 *
			 * Not for the amplifier's sake - for the echo canceller's.
			 * biscuit-call-dsp resets its whole adaptive filter whenever
			 * the reference changes identity, and er_writer_reset() stores
			 * a fresh generation on every codec open AND every close. The
			 * idle release fires after ten seconds of silence, so every
			 * pause in a conversation longer than that cost two resets: one
			 * when the far end stopped talking, one when they started
			 * again. Measured over a 557 s call that was ten resets, one
			 * every 56 seconds, and a 3840-tap SpeexDSP canceller needs
			 * seconds to reconverge - so the far end heard themselves after
			 * every pause, worst at high volume where the residual is
			 * loudest.
			 *
			 * Holding it open keeps ONE generation for the whole call, so
			 * the canceller converges once and keeps what it learned. The
			 * silence written during a pause is a true zero reference,
			 * which is exactly what the canceller should be subtracting.
			 */
			if (!strncmp(line + 5, "on", 2)) {
				if (e->call_until <= now_monotonic())
					logmsg("call: holding the codec open");
				e->call_until = now_monotonic() + CALL_HOLD_S;
				/* Open now rather than waiting for the first word, so
				 * the one unavoidable reset lands before the
				 * conversation instead of inside it. */
				e->wake_request = 1;
			} else {
				if (e->call_until > now_monotonic())
					logmsg("call: released");
				e->call_until = 0.0;
			}
		} else if (!strncmp(line, "cast ", 5)) {
			/* Silence the internal speaker while biscuit-btcast is
			 * feeding a Bluetooth speaker from the same audio.
			 *
			 * This mutes the INPUT rather than the output, which is
			 * what lets the existing idle path do the rest: the loop
			 * then sees silence, counts up, and releases the codec, so
			 * the external amplifier powers down instead of sitting
			 * energised behind a muted stream. That is the same reason
			 * the idle release exists at all - this device has no audio
			 * thermal protection.
			 *
			 * The cast tap is PipeWire's biscuit_speaker.monitor, which
			 * is upstream of the loopback this reads, so muting here
			 * costs the Bluetooth side nothing.
			 */
			e->cast_mute = !strncmp(line + 5, "on", 2);
			logmsg("cast mute=%d", e->cast_mute);
		}
	}
}

int main(int argc, char **argv)
{
	const char *in_dev = "hw:Loopback,1,0";
	const char *out_dev = "hw:0,0";
	int bypass_all = 0, file_mode = 0, opt;

	while ((opt = getopt(argc, argv, "i:o:bvfh")) != -1) {
		switch (opt) {
		case 'i': in_dev = optarg; break;
		case 'o': out_dev = optarg; break;
		case 'b': bypass_all = 1; break;
		case 'v': verbose = 1; break;
		case 'f': file_mode = 1; break;
		default:
			fprintf(stderr,
				"usage: %s [-i in] [-o out] [-b bypass all DSP] [-v]\n",
				argv[0]);
			return 2;
		}
	}

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	signal(SIGPIPE, SIG_IGN);

	engine *e = calloc(1, sizeof *e);
	if (!e) {
		logmsg("out of memory");
		return 1;
	}
	/* Start at the bottom of the push table, not the top: if nothing ever
	 * tells us the volume we under-drive rather than over-drive. */
	e->volume = 0;
	e->in_gain = 1.0f;
	e->full_lim_gain = 1.0f;
	e->push = e->push_target = 1.0f;
	e->vol_post = e->vol_post_target = 1.0f;
	e->push_slew = 1.0f - expf(-1.0f / (RATE * (GAIN_SLEW_MS / 1000.0f)));
	load_curves(e);
	load_user_eq(e);
	load_mbcl(e);
	set_volume(e, e->volume);
	e->push = e->push_target;
	e->vol_post = e->vol_post_target;
	if (bypass_all) {
		e->mbcl_bypass = 1;
		memset(e->fir, 0, sizeof e->fir);
		e->fir[0] = 1.0f;
		e->n_user_eq = 0;
		e->push = e->push_target = 1.0f;
		e->vol_post = e->vol_post_target = 1.0f;
		logmsg("all DSP bypassed");
	}

	/* Offline mode: same chain, no ALSA. Exists so the filter can be checked
	 * numerically - response, headroom, limiter ceiling - without hardware,
	 * and so a bad curve can be diagnosed off the device. */
	if (file_mode) {
		short fbuf[PERIOD * CHANNELS];
		float fmono[PERIOD];
		size_t got;
		int vol = getenv("BISCUIT_DSP_VOL") ? atoi(getenv("BISCUIT_DSP_VOL")) : 100;
		set_volume(e, vol);
		e->push = e->push_target;
		e->vol_post = e->vol_post_target;
		while ((got = fread(fbuf, sizeof(short) * CHANNELS, PERIOD, stdin)) > 0) {
			for (size_t n = 0; n < got; n++)
				fmono[n] = (fbuf[n * 2] + fbuf[n * 2 + 1]) * (0.5f / 32768.0f);
			process(e, fmono, (int)got);
			for (size_t n = 0; n < got; n++) {
				float v = fmono[n] * 32767.0f;
				short sv = (short)(v < 0.0f ? v - 0.5f : v + 0.5f);
				fbuf[n * 2] = sv;
				fbuf[n * 2 + 1] = sv;
			}
			fwrite(fbuf, sizeof(short) * CHANNELS, got, stdout);
		}
		free(e);
		return 0;
	}

	mkdir("/run/biscuit-dsp", 0755);
	unlink(CTRL_FIFO);
	if (mkfifo(CTRL_FIFO, 0666) < 0 && errno != EEXIST)
		logmsg("mkfifo %s: %s", CTRL_FIFO, strerror(errno));
	/* O_RDWR, not O_RDONLY: keeps a writer on the pipe so reads return 0
	 * instead of EOF-spinning when no one has it open. */
	int ctrl = open(CTRL_FIFO, O_RDWR | O_NONBLOCK);
	if (ctrl < 0)
		logmsg("control fifo unavailable: %s", strerror(errno));

	render_reference=er_writer_open(getenv("BISCUIT_ECHO_REFERENCE_PATH"));
	if (!render_reference) logmsg("software reference unavailable; capture uses hardware fallback");
	snd_pcm_t *in = NULL, *out = NULL;
	short *ibuf = malloc(sizeof(short) * PERIOD * CHANNELS);
	float *mono = malloc(sizeof(float) * PERIOD);
	if (!ibuf || !mono) {
		logmsg("out of memory");
		return 1;
	}

	logmsg("biscuit-dsp: %s -> %s, %d Hz, %d-tap FIR", in_dev, out_dev,
	       RATE, FIR_TAPS);

	while (running) {
		if (!in && open_pcm(&in, in_dev, SND_PCM_STREAM_CAPTURE) < 0) {
			sleep(2);
			continue;
		}
		if (ctrl >= 0)
			drain_control(e, ctrl);

		/* A wake arrives before the audio does, so act on it here rather
		 * than waiting for a non-silent period to force the open. */
		if (e->wake_request) {
			e->wake_request = 0;
			e->silent_periods = 0;
			if (!out)
				open_output(&out, out_dev, verbose);
		}

		snd_pcm_sframes_t got = snd_pcm_readi(in, ibuf, PERIOD);
		if (got < 0) {
			/* The loopback capture side reports an xrun every time the
			 * last writer closes, which is normal, not an error. */
			if (got == -EPIPE || got == -ESTRPIPE) {
				/* The loopback capture reports an xrun every time
				 * the last writer closes, which is normal. Counted
				 * rather than logged so a real storm is still
				 * visible without a line per period. */
				xruns_in++;
				snd_pcm_recover(in, (int)got, 1);
				continue;
			}
			logmsg("read: %s", snd_strerror((int)got));
			snd_pcm_close(in);
			in = NULL;
			continue;
		}
		if (got == 0)
			continue;

		/* Casting: hand this audio to the Bluetooth speaker alone.
		 *
		 * Zeroed here, before the silence test below, precisely so the
		 * codec-release path treats a cast exactly as it treats a quiet
		 * device. See the `cast` control in drain_control().
		 */
		if (e->cast_mute)
			memset(ibuf, 0, (size_t)got * CHANNELS * sizeof ibuf[0]);

		/* Release the codec when nothing is playing.
		 *
		 * Holding hw:0,0 open forever kept the DAC and the external amp
		 * powered around the clock, and made the whole card busy - so
		 * biscuit_direct, the documented bypass, could never be opened
		 * while the DSP was running. Both are worse than a few
		 * milliseconds of open latency at the start of a stream, and on
		 * a device with no audio thermal protection, keeping the amp
		 * powered for nothing is the part that actually matters.
		 *
		 * Nothing is dropped: the period that ends the silence is what
		 * triggers the open, and it is then written normally.
		 */
		int silent = 1;
		for (int n = 0; n < got * CHANNELS; n++) {
			if (ibuf[n] != 0) {
				silent = 0;
				break;
			}
		}

		if (silent) {
			if (e->silent_periods < IDLE_PERIODS)
				e->silent_periods++;

			/* Never open the codec just to push silence into it.
			 *
			 * Opening it powers the external amplifier, and closing
			 * it powers it down; each transition is an audible pop.
			 * The first version of this only skipped work once the
			 * silence counter had MATURED, so at startup it fell
			 * through, opened the codec to write silence, and closed
			 * it two seconds later - two pops at every boot, where
			 * there had previously been none.
			 */
			if (!out)
				continue;

			if (e->silent_periods >= IDLE_PERIODS &&
			    e->call_until <= now_monotonic()) {
				er_writer_reset(render_reference); snd_pcm_close(out);
				out = NULL;
				reset_state(e);
				if (verbose)
					logmsg("idle: codec released");
				continue;
			}

			/* Open, and the gap is short: keep feeding it silence so
			 * the stream does not underrun between tracks. Closing on
			 * every brief gap would pop far more than it saves. */
		} else {
			e->silent_periods = 0;
		}

		/* Sum to mono. The 3.5 mm jack is fed by the left codec output
		 * alone, so the right channel would otherwise be lost - the same
		 * reason asound.conf used to do this with a route table. */
		for (int n = 0; n < got; n++)
			mono[n] = (ibuf[n * 2] + ibuf[n * 2 + 1]) * (0.5f / 32768.0f);

		process(e, mono, (int)got);

		for (int n = 0; n < got; n++) {
			float v = mono[n] * 32767.0f;
			short s = (short)(v < 0.0f ? v - 0.5f : v + 0.5f);
			ibuf[n * 2] = s;
			ibuf[n * 2 + 1] = s;
		}

		if (!out) {
			open_output(&out, out_dev, verbose);
			if (!out) {
				/* Something else holds the codec - most likely a
				 * deliberate biscuit_direct test. Drop this
				 * period rather than spin. */
				usleep(200000);
				continue;
			}
		}

		snd_pcm_sframes_t put = snd_pcm_writei(out, ibuf, got);
		if (put>0) er_writer_push(render_reference,ibuf,(unsigned)put);
		if (put < 0) {
			if (put == -EPIPE || put == -ESTRPIPE) {
				xruns_out++;
				if (verbose || xruns_out % 100 == 1)
					logmsg("output xrun (%lu total)", xruns_out);
				er_writer_reset(render_reference);
				snd_pcm_recover(out, (int)put, 1);
				continue;
			}
			logmsg("write: %s", snd_strerror((int)put));
			er_writer_reset(render_reference); snd_pcm_close(out);
			out = NULL;
		}
	}

	logmsg("exiting (xruns: in=%lu out=%lu)", xruns_in, xruns_out);
	if (in) snd_pcm_close(in);
	if (out) { er_writer_reset(render_reference); snd_pcm_close(out); }
	if (ctrl >= 0) close(ctrl);
	unlink(CTRL_FIFO);
	er_writer_close(render_reference);
	free(ibuf);
	free(mono);
	free(e);
	return 0;
}
