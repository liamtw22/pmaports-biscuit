/*
 * biscuit-cvsd.h - the Bluetooth CVSD codec, split out so it can be tested
 * without a device or ALSA.
 *
 * Validated against a real call captured from hw:0,3. Lag-1 sample correlation
 * is 0.503 read as PCM (neither speech nor noise) and 0.961 decoded, which is
 * the speech range; the listener confirmed the decoded audio as the call.
 */
#ifndef BISCUIT_CVSD_H
#define BISCUIT_CVSD_H

#include <math.h>

#define CVSD_OVERSAMPLE   8       /* 64 kHz bitstream -> 8 kHz PCM */

/*
 * A one-bit delta modulator with syllabic companding: the step grows when J
 * consecutive bits agree (the signal is moving faster than the step can
 * follow) and decays otherwise.
 *
 * ON CVSD_H. This is 1 - 1/32, the specified value. I briefly changed it to
 * 1 - 1/1024 because one quiet voicemail sample decoded louder that way, and
 * that was wrong twice over: the sample was quiet because nobody was speaking,
 * and on real audio the slower leak lets the accumulator drift into the rails.
 * Worse, the fuzz that prompted the change was not a decoder fault at all -
 * the input was mSBC being fed to a CVSD decoder. Measured on a real call:
 * 1-1/32 clips 0.00%, 1-1/1024 clips 32.23%.
 *
 * An alternating 0101 bitstream is SILENCE in CVSD, so a quiet capture decodes
 * quietly - that is correct behaviour, not a broken decoder. A real call's
 * bitstream had a mean run length of 1.12.
 */
#define CVSD_DELTA_MIN    10.0f
#define CVSD_DELTA_MAX    1280.0f
#define CVSD_H            0.96875f    /* 1 - 1/32, accumulator decay          */
#define CVSD_BETA         0.9990234f  /* 1 - 1/1024, step decay               */
#define CVSD_J            4

/*
 * Anti-alias filter before decimation: two biquads, a 4th-order Butterworth
 * low pass at 3.4 kHz for a 64 kHz stream.
 *
 * Honest note: this measured no better than the box average it replaces
 * (peak 10679 vs 10621, identical lag1) on the one call tested, so it is not
 * the fix for anything observed. It is kept because a box average has almost
 * no stopband and delta-modulation noise lives at high frequencies, so it is
 * the right shape for the job even where this sample could not show it.
 */
struct cvsd_bq {
	float b0, b1, b2, a1, a2, x1, x2, y1, y2;
};

static void cvsd_bq_init(struct cvsd_bq *f, float fc, float fs, float q)
{
	float w = 2.0f * (float)M_PI * fc / fs;
	float c = cosf(w), s = sinf(w), al = s / (2.0f * q);
	float a0 = 1.0f + al;

	f->b0 = (1.0f - c) / 2.0f / a0;
	f->b1 = (1.0f - c) / a0;
	f->b2 = f->b0;
	f->a1 = (-2.0f * c) / a0;
	f->a2 = (1.0f - al) / a0;
	f->x1 = f->x2 = f->y1 = f->y2 = 0.0f;
}

static float cvsd_bq_run(struct cvsd_bq *f, float x)
{
	float y = f->b0 * x + f->b1 * f->x1 + f->b2 * f->x2
		  - f->a1 * f->y1 - f->a2 * f->y2;

	f->x2 = f->x1;
	f->x1 = x;
	f->y2 = f->y1;
	f->y1 = y;
	return y;
}

struct cvsd {
	float accum;
	float previous_input;
	float delta;
	unsigned hist;      /* last J bits, LSB most recent */
	int nbits;
	struct cvsd_bq lp1, lp2;
};

static void cvsd_init(struct cvsd *c)
{
	c->accum = 0.0f;
	c->previous_input = 0.0f;
	c->delta = CVSD_DELTA_MIN;
	c->hist = 0;
	c->nbits = 0;
	/* Butterworth Q values for a 4th-order cascade. */
	cvsd_bq_init(&c->lp1, 3400.0f, 64000.0f, 0.5412f);
	cvsd_bq_init(&c->lp2, 3400.0f, 64000.0f, 1.3066f);
}

/* Shared by both directions: the step rule must be identical in encoder and
 * decoder or the two ends drift apart. */
static void cvsd_step(struct cvsd *c, int bit)
{
	c->hist = ((c->hist << 1) | (bit & 1)) & ((1u << CVSD_J) - 1);
	if (c->nbits < CVSD_J)
		c->nbits++;

	if (c->nbits >= CVSD_J &&
	    (c->hist == 0 || c->hist == ((1u << CVSD_J) - 1))) {
		c->delta += CVSD_DELTA_MIN;
		if (c->delta > CVSD_DELTA_MAX)
			c->delta = CVSD_DELTA_MAX;
	} else {
		c->delta *= CVSD_BETA;
		if (c->delta < CVSD_DELTA_MIN)
			c->delta = CVSD_DELTA_MIN;
	}

	c->accum = c->accum * CVSD_H + (bit ? c->delta : -c->delta);
	if (c->accum > 32767.0f)
		c->accum = 32767.0f;
	else if (c->accum < -32768.0f)
		c->accum = -32768.0f;
}

/* in: CVSD bytes. out: 8 kHz S16 samples, one per 8 bits. */
static int cvsd_decode(struct cvsd *c, const unsigned char *in, int nbytes,
		       short *out)
{
	int n = 0, sub = 0;

	for (int i = 0; i < nbytes; i++) {
		for (int b = 7; b >= 0; b--) {
			float v;

			cvsd_step(c, (in[i] >> b) & 1);
			v = cvsd_bq_run(&c->lp2, cvsd_bq_run(&c->lp1, c->accum));
			if (++sub == CVSD_OVERSAMPLE) {
				out[n++] = (short)(v > 32767.0f ? 32767 :
						   (v < -32768.0f ? -32768 : v));
				sub = 0;
			}
		}
	}
	return n;
}

/* in: 8 kHz S16 samples. out: CVSD bytes, one bit per 64 kHz sample. */
static int cvsd_encode(struct cvsd *c, const short *in, int nsamples,
		       unsigned char *out)
{
	int nbytes = 0, nbit = 0;
	unsigned char cur = 0;
	float prev = c->previous_input;

	for (int i = 0; i < nsamples; i++) {
		float target = (float)in[i];

		for (int k = 0; k < CVSD_OVERSAMPLE; k++) {
			/* Linear interpolation up to 64 kHz. The encoder tracks
			 * the same accumulator the far end will build, so the
			 * bit is chosen by comparing against it. */
			float want = prev + (target - prev) *
				     ((float)(k + 1) / CVSD_OVERSAMPLE);
			int bit = (want >= c->accum) ? 1 : 0;

			cvsd_step(c, bit);
			cur = (unsigned char)((cur << 1) | bit);
			if (++nbit == 8) {
				out[nbytes++] = cur;
				cur = 0;
				nbit = 0;
			}
		}
		prev = target;
	}
	c->previous_input = prev;
	return nbytes;
}

#endif /* BISCUIT_CVSD_H */
