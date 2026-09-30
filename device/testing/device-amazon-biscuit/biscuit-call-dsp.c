/* SPDX-License-Identifier: MIT
 * Selected S24_3LE capsule + aligned rendered speaker reference -> S16 mono.
 * SpeexDSP implementation; imported stock settings are NOT vendor algorithm parity.
 * Channel 7 is the synchronous timing pilot and hardware fallback. The bounded
 * speaker history supplies a filtered reference only after matching that pilot.
 */
#define _POSIX_C_SOURCE 200809L
#include <speex/speex_echo.h>
#include <speex/speex_preprocess.h>
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "biscuit-echo-reference.h"

#define FRAME 128
#define RATE 16000
#define CHANNELS 8
struct highpass { float x, y; };
static float hp(struct highpass *s, float x) {
    float y = x - s->x + 0.9690724263f * s->y; /* 80 Hz at 16 kHz */
    s->x = x; s->y = y; return y;
}
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


static int32_t sample24(const uint8_t *p) {
    uint32_t v = (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16;
    return (int32_t)(v ^ 0x800000U) - 0x800000;
}
static int16_t s16(float x, unsigned long *clips) {
    if (!isfinite(x)) return 0;
    if (x > 32767) { x = 32767; (*clips)++; }
    if (x < -32768) { x = -32768; (*clips)++; }
    return (int16_t)lrintf(x);
}
static double number(const char *s, double low, double high) {
    char *end; errno = 0; double v = strtod(s, &end);
    if (errno || !*s || *end || !isfinite(v) || v < low || v > high) {
        fprintf(stderr, "call-dsp: invalid numeric argument: %s\n", s); exit(2);
    }
    return v;
}
static int integer(const char *s, int low, int high) {
    double v = number(s, low, high);
    if (v != floor(v)) { fprintf(stderr,"call-dsp: integer required\n"); exit(2); }
    return (int)v;
}
/*
 * RESIDUAL ECHO SUPPRESSOR.
 *
 * Measured 2026-09-27 on a hands-free call with a far-end voice at volume 100:
 * the far end rendered at -11.3 dBFS, its echo reached the mic at -29.6, and
 * SpeexDSP's canceller left -48.3 - 18.7 dB of ERLE, a reasonable result. The
 * AGC then sat at its +30 dB ceiling, because the near end is quiet at this
 * distance and 82% of the echo frames read as speech, and handed the residue
 * straight back: -19.6 dBFS sent, so the caller heard themselves only 8.4 dB
 * below their own voice. Hands-free standards ask for 40 or more. SpeexDSP's
 * own residual-echo suppression never engaged: --echo-suppress-active-db -40
 * produced output bit-identical to -25, because it scales by the canceller's
 * leak estimate, which does not see a residue dominated by the loudspeaker's
 * distortion.
 *
 * So this predicts the residue from the reference instead. env follows the
 * reference power with a room tail; beta tracks the 95th percentile of
 * residue/env while the far end plays; wherever the canceller's output is not
 * clearly above margin * beta * env, the frame is echo and is attenuated, down
 * to floor. With no reference there is nothing to predict and the frame
 * passes untouched. Offline on the recorded call: 46.5 dB of coupling loss on
 * far-end speech (was 8.4) and 42 dB on beeps (was 5.7); near-end-only speech
 * and words spoken in the far end's pauses unchanged (at most 0.9 dB). The
 * cost is double talk at high volume: when the echo at the mic is 15 dB above
 * the talker, the talker is suppressed with it until the far end pauses.
 */
#define RES_ACTIVE (32768.0 * 32768.0 * 1e-5)      /* far end playing: reference over -50 dBFS */
struct res {
    double env, beta, up, down, decay, margin;
    float gain, floor, release;
    unsigned long suppressed;
};
static void res_init(struct res *r, float margin_db, float floor_db, float tail_ms) {
    const double frame_ms = 1000.0 * FRAME / RATE, step_db = 0.5, quantile = 0.95;
    r->env = 0;
    r->beta = pow(10, -25 / 10.0);                   /* cautious until it has measured */
    r->up = pow(10, step_db * quantile / 10);
    r->down = pow(10, -step_db * (1 - quantile) / 10);
    r->decay = pow(10, -60 / 10.0 * frame_ms / tail_ms);
    r->margin = pow(10, margin_db / 10);
    r->floor = powf(10.f, floor_db / 20.f);
    r->release = 1.f - expf(-(float)frame_ms / 40.f);
    r->gain = 1.f;
    r->suppressed = 0;
}
/* Returns the gain for this frame; call with the canceller's input reference
 * and output, before the preprocessor overwrites the output. */
static float res_frame(struct res *r, const spx_int16_t *ref, const spx_int16_t *aec) {
    double pref = 0, pe = 0;
    for (int i = 0; i < FRAME; i++) {
        pref += (double)ref[i] * ref[i];
        pe += (double)aec[i] * aec[i];
    }
    pref /= FRAME; pe /= FRAME;
    r->env = fmax(pref, r->decay * r->env);
    if (pref > RES_ACTIVE) {
        r->beta *= pe / r->env > r->beta ? r->up : r->down;
        r->beta = fmin(fmax(r->beta, 1e-6), 0.1);
    }
    float target = 1.f;
    if (r->env > RES_ACTIVE * 1e-3) {
        double t = 1.0 - r->margin * r->beta * r->env / (pe + 1e-3);
        target = t < r->floor ? r->floor : (float)t;
    }
    r->gain = target < r->gain ? target : r->gain + (target - r->gain) * r->release;
    if (r->gain < 0.5f) r->suppressed++;
    return r->gain;
}
/*
 * CALL AGC AND LIMITER.
 *
 * SpeexDSP's own AGC never regulated on this hardware. Replaying the
 * 2026-09-27 call, its gain sat at the --max-gain-db ceiling in 91% of
 * near-end speech frames whatever the target: -20, -26 and -36 dBFS all sent
 * the same level. Its loudness estimate reads this microphone far below the
 * RMS, so it was a fixed +30 dB, applied inside the library, whose int16
 * output saturates. 36 of 121 near-end speech slices reached full scale and
 * the far end called it static. A clamp after the library cannot undo that,
 * and a lower ceiling only trades it for a quieter talker across the room.
 *
 * So the gain is ours, in float, after the preprocessor, which still does
 * the noise suppression and supplies the speech probability. The active
 * speech level is the mean power of the frames it calls speech while no
 * far-end audio, nor its room tail by the suppressor's envelope, is about,
 * so echo cannot pull the gain up and double talk does not move it. The gain
 * slews toward target - level at the profile's rise and fall rates, from
 * where that call's talker needed it. A peak limiter replaces the clamp. It
 * looks one millisecond ahead, so no sample crosses -1 dBFS, and its gain
 * ramps; it steps only down, and only where a frame opens on a peak.
 *
 * Replayed on that call, with the talker also 6, 10 and 15 dB quieter as if
 * further away: near-end speech sent at -21.0, -22.9, -24.5 and -29.9 dBFS
 * (a fixed +20 dB sent the first three at -21.3, -26.9 and -31.0), no
 * clamped samples (277 before), 9 dB less room noise between words, and
 * 7 dB more echo loss.
 */
/* Frames under -55 dBFS are not measured. A quiet room's own sounds, which
 * the preprocessor often calls speech, reach -57 to -64 before any gain; at
 * -65 they set the gain to its ceiling before the call began. A talker
 * 10 dB quieter still had 65% as many frames measured. */
#define AGC_FLOOR (32768.0 * 32768.0 * 3.2e-6)
#define AGC_MEMORY 125                              /* level averaged over 1 s of speech */
#define AGC_TALKER_DBFS -40.f                       /* the talker at MK7 on the 2026-09-27 call */
struct agc {
    double level;
    float gain_db, gain, target_db, max_db, min_db, rise, fall;
    unsigned long speech;
};
static void agc_init(struct agc *a, float target_db, float max_db, float rise_db_s, float fall_db_s) {
    const float frame_s = (float)FRAME / RATE;
    a->level = 0;
    a->speech = 0;
    a->target_db = target_db;
    a->max_db = max_db;
    a->min_db = -12.f;
    a->rise = rise_db_s * frame_s;
    a->fall = fall_db_s * frame_s;
    a->gain_db = fminf(fmaxf(target_db - AGC_TALKER_DBFS, a->min_db), max_db);
    a->gain = powf(10.f, a->gain_db / 20.f);
}
/* Returns the gain for this frame; x is the preprocessor's output. */
static float agc_frame(struct agc *a, const spx_int16_t *x, int speech) {
    double p = 0;
    for (int i = 0; i < FRAME; i++) p += (double)x[i] * x[i];
    p /= FRAME;
    if (!speech || p < AGC_FLOOR) return a->gain;
    a->speech++;
    a->level += (p - a->level) / (a->speech < AGC_MEMORY ? a->speech : AGC_MEMORY);
    float want = a->target_db - 10.f * log10f((float)(a->level / (32768.0 * 32768.0)));
    want = fminf(fmaxf(want, a->min_db), a->max_db);
    a->gain_db += fmaxf(-a->fall, fminf(a->rise, want - a->gain_db));
    a->gain = powf(10.f, a->gain_db / 20.f);
    return a->gain;
}
#define LIM_CEILING 29204.f                         /* -1 dBFS */
#define LIM_SUB 16                                  /* 1 ms at 16 kHz */
struct lim {
    float gain, release;
    unsigned long frames;
};
static void lim_init(struct lim *l) {
    l->gain = 1.f;
    l->release = 1.f - expf(-1000.f * LIM_SUB / RATE / 50.f);   /* 50 ms */
    l->frames = 0;
}
/* Each 1 ms block ramps from the previous block's gain to the lowest its own
 * and the next block's peaks allow, so every sample stays under the ceiling.
 * Only the first block of a frame can step, and only down. */
static void lim_frame(struct lim *l, float *x) {
    enum { BLOCKS = FRAME / LIM_SUB };
    float need[BLOCKS + 1];
    for (int k = 0; k < BLOCKS; k++) {
        float peak = 0;
        for (int i = 0; i < LIM_SUB; i++) peak = fmaxf(peak, fabsf(x[k * LIM_SUB + i]));
        need[k] = peak > LIM_CEILING ? LIM_CEILING / peak : 1.f;
    }
    need[BLOCKS] = 1.f;
    float g = fminf(l->gain, need[0]), lowest = g;
    for (int k = 0; k < BLOCKS; k++) {
        float t = fminf(fminf(need[k], need[k + 1]), g + (1.f - g) * l->release);
        for (int i = 0; i < LIM_SUB; i++) x[k * LIM_SUB + i] *= g + (t - g) * (i + 1) / LIM_SUB;
        g = t;
        lowest = fminf(lowest, g);
    }
    l->gain = g;
    if (lowest < 0.999f) l->frames++;
}

static void ctl(SpeexPreprocessState *p, int request, void *value) {
    if (speex_preprocess_ctl(p, request, value) != 0) {
        fprintf(stderr,"call-dsp: unsupported preprocessor control %d\n",request); exit(2);
    }
}
int main(int argc, char **argv) {
    int mic = 6, tail = 3840, maxgain = 30, rise = 3, fall = 6;
    /* residual_active is the residual-echo floor while the near end looks
     * active, blended with residual by SpeexDSP's speech probability. Loud
     * echo leaking through raises that probability, so at high volume this is
     * the floor the far end actually gets. -12 (weaker than Speex's own -15)
     * left the far end hearing themselves at full volume on a 2026-09-23
     * call, and "much better" at volume 50 - about 10 dB less echo - so -25.
     * The cost is ducking of the near end during double talk. */
    int noise = -15, residual = -40, residual_active = -25;
    int bypass = 0, stock = 0, rate = RATE, hardware_only = 0, reference_filtered = 0;
    int use_res = 1, res_margin = 8, res_floor = -40, res_tail = 800;
    float target_db = -20;
    const char *calpath = NULL, *mutepath = NULL;
    for (int i=1; i<argc; i++) {
        if (!strcmp(argv[i],"--reference-filtered")) { reference_filtered=1; continue; }
        if (!strcmp(argv[i],"--hardware-reference")) { hardware_only=1; continue; }
        if (!strcmp(argv[i],"--bypass")) { bypass=1; continue; }
        if (!strcmp(argv[i],"--stock-hpf")) { stock=1; continue; }
        if (!strcmp(argv[i],"--no-res")) { use_res=0; continue; }
        if (i+1 >= argc) { fprintf(stderr,"call-dsp: missing argument\n"); return 2; }
        const char *key=argv[i], *v=argv[++i];
        if (!strcmp(key,"--mic")) mic=integer(v,0,6);
        else if (!strcmp(key,"--tail")) tail=integer(v,512,8192);
        else if (!strcmp(key,"--target-db")) target_db=(float)number(v,-40,-10);
        else if (!strcmp(key,"--max-gain-db")) maxgain=integer(v,0,40);
        else if (!strcmp(key,"--rise-db")) rise=integer(v,1,12);
        else if (!strcmp(key,"--fall-db")) fall=integer(v,1,24);
        else if (!strcmp(key,"--echo-suppress-db")) residual=integer(v,-80,0);
        else if (!strcmp(key,"--echo-suppress-active-db")) residual_active=integer(v,-40,0);
        else if (!strcmp(key,"--res-margin-db")) res_margin=integer(v,0,20);
        else if (!strcmp(key,"--res-floor-db")) res_floor=integer(v,-60,0);
        else if (!strcmp(key,"--res-tail-ms")) res_tail=integer(v,100,2000);
        else if (!strcmp(key,"--cal")) calpath=v;
        else if (!strcmp(key,"--mute-file")) mutepath=v;
        else { fprintf(stderr,"call-dsp: unknown argument %s\n",key); return 2; }
    }
    if(reference_filtered && !hardware_only){fprintf(stderr,"filtered reference is offline-only; requires hardware-reference input\n");return 2;}
    if (tail % FRAME) { fprintf(stderr,"call-dsp: tail must be a multiple of 128\n"); return 2; }
    float calibration=1;
    if (calpath) {
        FILE *f=fopen(calpath,"r"); float v;
        if (f) {
            for (int n=0;n<=mic;n++) {
                if (fscanf(f,"%f",&v)!=1) break;
                if (n==mic && isfinite(v) && v>=0.5f && v<=2.f) calibration=v;
            }
            fclose(f);
        }
    }
    SpeexEchoState *echo=speex_echo_state_init(FRAME,tail);
    SpeexPreprocessState *pre=speex_preprocess_state_init(FRAME,RATE);
    if (!echo || !pre) { fprintf(stderr,"call-dsp: allocation failed\n"); return 1; }
    speex_echo_ctl(echo,SPEEX_ECHO_SET_SAMPLING_RATE,&rate);
    int yes=1, no=0;
    ctl(pre,SPEEX_PREPROCESS_SET_ECHO_STATE,echo);
    ctl(pre,SPEEX_PREPROCESS_SET_DENOISE,&yes);
    ctl(pre,SPEEX_PREPROCESS_SET_NOISE_SUPPRESS,&noise);
    ctl(pre,SPEEX_PREPROCESS_SET_ECHO_SUPPRESS,&residual);
    ctl(pre,SPEEX_PREPROCESS_SET_ECHO_SUPPRESS_ACTIVE,&residual_active);
    ctl(pre,SPEEX_PREPROCESS_SET_AGC,&no);      /* ours: see CALL AGC above */
    ctl(pre,SPEEX_PREPROCESS_SET_DEREVERB,&no);
    /* Read back from SpeexDSP rather than echo our own variables, so the log
     * shows what the library applied, not what we asked for. */
    int res_idle=0, res_active=0;
    ctl(pre,SPEEX_PREPROCESS_GET_ECHO_SUPPRESS,&res_idle);
    ctl(pre,SPEEX_PREPROCESS_GET_ECHO_SUPPRESS_ACTIVE,&res_active);
    fprintf(stderr,"call-dsp: mic=MK%d, SpeexDSP, tail=%d, target=%.1fdBFS, maxgain=%ddB, res=%d/%ddB, %s\n",
            mic+1,tail,target_db,maxgain,res_idle,res_active,bypass?"bypassed":"AEC+RES+NR+AGC");
    struct res sup;
    res_init(&sup,(float)res_margin,(float)res_floor,(float)res_tail);
    struct agc agc;
    agc_init(&agc,target_db,(float)maxgain,(float)rise,(float)fall);
    struct lim lim;
    lim_init(&lim);
    if (!bypass) {
        fprintf(stderr,"call-dsp: echo suppressor %s (margin %d dB, floor %d dB, tail %d ms)\n",
                use_res?"on":"off",res_margin,res_floor,res_tail);
        fprintf(stderr,"call-dsp: agc from %+.1f dB, %+.0f..%+d dB, rise %d fall %d dB/s; limiter -1 dBFS\n",
                agc.gain_db,agc.min_db,maxgain,rise,fall);
    }
    setvbuf(stdout,NULL,_IONBF,0);
    uint8_t raw[FRAME*CHANNELS*3];
    spx_int16_t input[FRAME], reference[FRAME], output[FRAME];
    struct highpass mh={0}, rh={0}; struct hpf80 ms={0}, rs={0};
    unsigned long frames=0, micclips=0, refclips=0, outclips=0;
    int was_muted=0, result=0;
    struct er_reader *render_ref=hardware_only?NULL:er_reader_open(getenv("BISCUIT_ECHO_REFERENCE_PATH"));
    float hardware_ref[FRAME], software_ref[FRAME];
    const char *trace_path=getenv("BISCUIT_CALL_TRACE");
    FILE *trace=trace_path?fopen(trace_path,"wb"):NULL;
    if(trace_path && !trace) { perror("call trace"); return 1; }
    int16_t traced[FRAME*4];
    const char *gain_trace_path=getenv("BISCUIT_CALL_GAIN_TRACE");
    FILE *gain_trace=gain_trace_path?fopen(gain_trace_path,"wb"):NULL;
    if(gain_trace_path && !gain_trace){perror("gain trace");return 1;}
    for (;;) {
        size_t n=fread(raw,1,sizeof(raw),stdin);
        if (!n) { if(ferror(stdin)) result=1; break; }
        if(n!=sizeof(raw)) { fprintf(stderr,"call-dsp: truncated capture frame\n"); result=1; break; }
        int muted=0;
        if(mutepath) {
            FILE *f=fopen(mutepath,"r");
            if(f) { muted=fgetc(f)!='0'; fclose(f); }
            else muted=1; /* Without the core mute state, fail closed. */
        }
        if(muted!=was_muted) {
            speex_echo_state_reset(echo);
            memset(&mh,0,sizeof(mh)); memset(&rh,0,sizeof(rh));
            memset(&ms,0,sizeof(ms)); memset(&rs,0,sizeof(rs));
            was_muted=muted;
        }
        for(int i=0;i<FRAME;i++) hardware_ref[i]=(float)sample24(raw+(i*CHANNELS+7)*3)/256.f;
        int ref_changed=0;
        int software=er_reader_process(render_ref,hardware_ref,software_ref,&ref_changed);
        if(ref_changed) {
            fprintf(stderr,"call-reference: frame=%lu software=%d reset\n",frames,software);
            speex_echo_state_reset(echo);
            memset(&rh,0,sizeof(rh));memset(&rs,0,sizeof(rs));
        }
        for(int i=0;i<FRAME;i++) {
            float m=(float)sample24(raw+(i*CHANNELS+mic)*3)/256.f*calibration;
            float r=software?software_ref[i]:hardware_ref[i];
            m=stock?hpf80_step(&ms,m):hp(&mh,m);
            if(!reference_filtered) r=stock?hpf80_step(&rs,r):hp(&rh,r);
            input[i]=s16(m,&micclips); reference[i]=s16(r,&refclips);
            /* Bypass is the prior fixed-makeup single-mic baseline. */
            if(bypass) output[i]=s16(m*10.f,&outclips);
        }
        if(muted) memset(output,0,sizeof(output));
        else if(!bypass) {
            speex_echo_cancellation(echo,input,reference,output);
            for(int i=0;i<FRAME;i++) {
                traced[4*i]=input[i];traced[4*i+1]=reference[i];traced[4*i+2]=output[i];
            }
            float res_prev=sup.gain;
            float res_gain=res_frame(&sup,reference,output);
            speex_preprocess_run(pre,output);
            int prob=0;
            ctl(pre,SPEEX_PREPROCESS_GET_PROB,&prob);
            float agc_prev=agc.gain;
            float agc_gain=agc_frame(&agc,output,prob>=50 && sup.env<RES_ACTIVE);
            if(gain_trace)
                fprintf(gain_trace,"%lu,%d,%.1f,%.1f\n",frames,prob,agc.gain_db,
                        use_res?20*log10f(fmaxf(res_gain,1e-6f)):0.f);
            /* Both gains ramp across the frame so a change never steps. The
             * suppressor's gain applies to the AGC's output, so the AGC cannot
             * hand the echo back. */
            float y[FRAME];
            for(int i=0;i<FRAME;i++) {
                float t=(float)(i+1)/FRAME, g=agc_prev+(agc_gain-agc_prev)*t;
                if(use_res) g*=res_prev+(res_gain-res_prev)*t;
                y[i]=output[i]*g;
            }
            lim_frame(&lim,y);
            /* The limiter holds -1 dBFS; this only counts what it missed. */
            for(int i=0;i<FRAME;i++) {
                float v=fminf(fmaxf(y[i],-LIM_CEILING),LIM_CEILING);
                if(v!=y[i]) outclips++;
                output[i]=(spx_int16_t)lrintf(v);
            }
        }
        if(trace && !muted && !bypass) {
            for(int i=0;i<FRAME;i++)traced[4*i+3]=output[i];
            if(fwrite(traced,1,sizeof(traced),trace)!=sizeof(traced)){result=1;break;}
        }
        if(fwrite(output,1,sizeof(output),stdout)!=sizeof(output)) { result=1; break; }
        if(++frames%1250==0) fprintf(stderr,"call-dsp: frames=%lu input_clips=%lu ref_clips=%lu limited=%lu res_beta=%.1fdB res_suppressed=%lu agc=%.1fdB agc_speech=%lu limiter=%lu\n",
                                     frames,micclips,refclips,outclips,10*log10(sup.beta),sup.suppressed,
                                     agc.gain_db,agc.speech,lim.frames);
    }
    if(gain_trace && fclose(gain_trace)!=0)result=1;
    if(trace && fclose(trace)!=0)result=1;
    er_reader_close(render_ref);
    speex_preprocess_state_destroy(pre); speex_echo_state_destroy(echo);
    return result;
}
