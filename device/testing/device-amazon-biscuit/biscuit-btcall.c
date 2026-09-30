/*
 * biscuit-btcall - carry Bluetooth hands-free call audio to this device's
 * speaker, with optional microphone transmit.
 *
 * WHY THIS IS NOT alsaloop
 *
 * hw:0,3 is not a PCM source. It is a window onto the SCO air stream that the
 * BT firmware leaves in a shared SRAM bank, and it carries a raw CVSD
 * BITSTREAM - one bit per sample at 64 kHz, 8000 bytes/s. Proved on hardware:
 * the bitstream is 50.0% ones with a mean run length of 1.12, its lag-1 sample
 * correlation read as PCM is 0.503 (neither speech nor noise) and 0.933 after
 * CVSD decoding, which is the speech range. The Kconfig said so plainly -
 * "transferring/receiving BT encoded data to/from BT firmware" - and it took an
 * evening to take that literally.
 *
 * Three constraints came out of that work, all measured:
 *
 *   1. THE ALSA PERIOD MUST FIT THE DRIVER'S RING. The ring is 32 bytes x 64
 *      packets and the irq handler drops data past 58, so usable depth is 928
 *      frames. The driver still advertises buffer_bytes_max = 24 KB, so ALSA
 *      picks a 1000-frame period by default - larger than the ring can hold. A
 *      period that cannot complete means copy() is never called, packet_r never
 *      advances, and pointer() freezes: a deadlock that looks exactly like dead
 *      hardware. Measured: periods of 96/160/192 capture a full call; the
 *      default 1000 captures nothing but a WAV header.
 *
 *   2. EVERY 32-BYTE PACKET IS 30 BYTES OF AUDIO PLUS A 2-BYTE VALIDITY FLAG.
 *      Left in, the flag becomes a near-zero sample every 16 - a 500 Hz buzz.
 *      Measured: sample position 15 of 16 has rms 1.0, the other fifteen carry
 *      the audio.
 *
 *   3. OPEN "btsco", NEVER "hw:0,3" DIRECTLY. Playback on the raw device
 *      CRASHED THE MACHINE every time - four attempts out of four - and it
 *      was nothing to do with Bluetooth: it reproduces with no call, with
 *      nothing written, and with the BTCVSD interrupt having fired zero times.
 *      alsa-lib pads the end of a drain with silence unless told otherwise and
 *      asks the kernel for it through SW_PARAMS; the core fills that silence
 *      into runtime->dma_area, which mtk-btcvsd never allocates because it
 *      moves data with a .copy callback, and ASoC has no fill_silence op to
 *      intercept it. The write lands on NULL under snd_pcm_stream_lock_irq(),
 *      so one core dies with interrupts off and the mtk watchdog reboots the
 *      board 31 s later having flushed nothing - which is exactly why this
 *      looked for so long like a crash with no cause.
 *
 *      asound.conf defines "btsco" as the same device with drain_silence 0,
 *      which declines the padding. Verified: drain then returns -EIO honestly
 *      and the board survives, three runs out of three.
 *
 *      An earlier note here blamed the driver masking the firmware's SRAM
 *      offset to 64 KB while the devicetree maps 32 KB. That mismatch is real
 *      but innocent: stock's own MT8163 kernel ships the identical mask and
 *      the identical mapping with working transmit, and this board's offsets
 *      measure 0x20dc and 0x229c - nowhere near the 0x8000 limit.
 *
 * One 192-byte read is 6 packets = one interrupt: 180 bytes of CVSD = 1440 bits
 * = 180 PCM samples at 8 kHz = 22.5 ms, matching the driver's own interrupt
 * period exactly.
 */

#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>
#include <unistd.h>

/* Driver geometry. Not tunables - this is what mtk-btcvsd.c does. */
#define RX_PACKET_BYTES   32          /* 30 audio + 2 validity                */
#define RX_AUDIO_BYTES    30
#define PACKETS_PER_IRQ    6          /* btsco_packet_info: 180/30            */
#define RX_CHUNK_BYTES    (RX_PACKET_BYTES * PACKETS_PER_IRQ)   /* 192        */
#define RX_AUDIO_CHUNK    (RX_AUDIO_BYTES * PACKETS_PER_IRQ)    /* 180        */
#define PCM_PER_CHUNK     180         /* samples at 8 kHz per chunk           */
#define SCO_RATE          8000

/*
 * mSBC - wideband call audio.
 *
 * The air rate is the same 64 kbit/s as CVSD, but the packing is completely
 * different: mSBC is a transform codec at 16 kHz, and each SCO frame carries
 * one 60-byte unit - a 2-byte H2 header, a 57-byte SBC frame, and a byte of
 * padding. 120 samples per frame at 16 kHz is 7.5 ms, so the 180 payload
 * bytes the driver hands us every 22.5 ms are exactly three frames and 360
 * samples. The validity bytes are stripped first, the same as for CVSD; the
 * difference is entirely in what the remaining bytes mean.
 *
 * This matters beyond call quality. Windows 11 will not build a complete HFP
 * endpoint pair unless mSBC is on offer - with it refused, it creates a
 * capture endpoint and no render endpoint, and never establishes SCO at all.
 * So wideband is what makes the device usable as a computer microphone over
 * Bluetooth, not merely what makes calls sound better.
 *
 * Decoding is libsbc's, which is already on the device because bluez-alsa
 * links it. Writing an SBC decoder here would be a lot of subtly wrong code
 * to maintain for no gain.
 */
#define MSBC_FRAME_BYTES   60         /* H2(2) + SBC(57) + pad(1)             */
#define MSBC_SBC_BYTES     57
#define MSBC_H2_SYNC     0x01         /* first H2 byte                        */
#define MSBC_SBC_SYNC    0xAD         /* mSBC syncword, first SBC byte        */
#define MSBC_SAMPLES      120         /* 15 blocks x 8 subbands               */
#define MSBC_RATE       16000
#define MSBC_PCM_PER_CHUNK (MSBC_SAMPLES * 3)                   /* 360        */

/* Buffers are sized for whichever codec needs more. */
#define MAX_PCM_PER_CHUNK  MSBC_PCM_PER_CHUNK

#include <sbc/sbc.h>
#include "biscuit-cvsd.h"

/* ------------------------------------------------------------------ mSBC */

struct msbc {
	sbc_t sbc;
	int ready;
	unsigned char acc[MSBC_FRAME_BYTES * 4];
	int acclen;
	unsigned long framed, resyncs;
};

/*
 * Payload detection is a fallback when the negotiated codec is unavailable.
 * Require two framed headers; the first idle payload alone can be misleading.
 */
static int msbc_looks_like(const unsigned char *b, int len)
{
	/* Two consecutive framed headers avoid interpreting random CVSD bytes
	 * as wideband. A caller can override detection for transport diagnostics. */
	for (int i = 0; i + MSBC_FRAME_BYTES + 3 <= len && i < MSBC_FRAME_BYTES; i++) {
		int a = b[i + 1], c = b[i + MSBC_FRAME_BYTES + 1];
		if (b[i] == MSBC_H2_SYNC && b[i + 2] == MSBC_SBC_SYNC &&
		    b[i + MSBC_FRAME_BYTES] == MSBC_H2_SYNC &&
		    b[i + MSBC_FRAME_BYTES + 2] == MSBC_SBC_SYNC &&
		    (a == 0x08 || a == 0x38 || a == 0xc8 || a == 0xf8) &&
		    (c == 0x08 || c == 0x38 || c == 0xc8 || c == 0xf8))
			return 1;
	}
	return 0;
}

/* An idle mSBC receive stream can contain CVSD-style 0x55 filler before the
 * first framed audio arrives. Prefer the control channel's negotiated codec.
 * Only accept an unambiguous set of connected HFP-HF PCM codecs; explicit
 * --codec remains available when more than one peer is connected. */
static int negotiated_codec(void)
{
	char paths[8][256], line[512];
	int count = 0, selected = -1;
	FILE *list = popen("timeout 1 bluealsa-cli list-pcms 2>/dev/null", "r");
	if (!list) return -1;
	while (fgets(line, sizeof(line), list)) {
		line[strcspn(line, "\r\n")] = 0;
		size_t len = strlen(line);
		if (len >= sizeof(paths[0]) || len < 13 || count == 8 ||
		    strcmp(line + len - 13, "/hfphf/source")) continue;
		int safe = !strncmp(line, "/org/bluealsa/", 13);
		for (size_t i = 0; i < len; i++)
			if (!isalnum((unsigned char)line[i]) && line[i] != '/' && line[i] != '_')
				safe = 0;
		if (safe) strcpy(paths[count++], line);
	}
	pclose(list);
	for (int i = 0; i < count; i++) {
		char command[384];
		snprintf(command, sizeof(command), "timeout 1 bluealsa-cli codec %.255s 2>/dev/null", paths[i]);
		FILE *info = popen(command, "r");
		if (!info) return -1;
		int codec = -1;
		while (fgets(line, sizeof(line), info)) {
			if (!strcmp(line, "Selected codec: mSBC\n")) codec = 1;
			if (!strcmp(line, "Selected codec: CVSD\n")) codec = 0;
		}
		pclose(info);
		if (codec < 0 || (selected >= 0 && selected != codec)) return -1;
		selected = codec;
	}
	return selected;
}

static void msbc_init(struct msbc *m)
{
	memset(m, 0, sizeof(*m));
	sbc_init_msbc(&m->sbc, 0);
	m->sbc.endian = SBC_LE;
	m->ready = 1;
}

static void msbc_free(struct msbc *m)
{
	if (m->ready) {
		sbc_finish(&m->sbc);
		m->ready = 0;
	}
}

/*
 * Decode whole frames out of a running byte stream.
 *
 * Frames do not have to align with the 180-byte chunks the driver delivers,
 * so bytes accumulate here and are consumed a frame at a time. On a lost or
 * corrupt frame the accumulator is advanced one byte and the sync is searched
 * for again rather than assuming the next 60 bytes are a frame - resyncs are
 * counted, because silently mis-framing a transform codec produces noise that
 * is easy to mistake for a hardware fault.
 */
static int msbc_decode(struct msbc *m, const unsigned char *in, int inlen,
		       short *out, int outmax)
{
	int samples = 0;

	if (inlen > 0) {
		if (m->acclen + inlen > (int)sizeof(m->acc))
			m->acclen = 0;                  /* hopelessly behind */
		memcpy(m->acc + m->acclen, in, (size_t)inlen);
		m->acclen += inlen;
	}

	while (m->acclen >= MSBC_FRAME_BYTES) {
		int at = -1;

		for (int i = 0; i + 3 <= m->acclen; i++) {
			if (m->acc[i] == MSBC_H2_SYNC &&
			    m->acc[i + 2] == MSBC_SBC_SYNC) {
				at = i;
				break;
			}
		}
		if (at < 0) {                           /* keep a frame's tail */
			int keep = MSBC_FRAME_BYTES - 1;

			if (m->acclen > keep) {
				memmove(m->acc, m->acc + m->acclen - keep,
					(size_t)keep);
				m->acclen = keep;
			}
			break;
		}
		if (at) {
			m->resyncs++;
			memmove(m->acc, m->acc + at, (size_t)(m->acclen - at));
			m->acclen -= at;
		}
		if (m->acclen < MSBC_FRAME_BYTES)
			break;
		if (samples + MSBC_SAMPLES > outmax)
			break;

		size_t written = 0;
		ssize_t used = sbc_decode(&m->sbc, m->acc + 2, MSBC_SBC_BYTES,
					  out + samples,
					  (size_t)(outmax - samples) *
					  sizeof(short), &written);

		if (used <= 0) {
			m->resyncs++;
			memmove(m->acc, m->acc + 1, (size_t)(m->acclen - 1));
			m->acclen--;
			continue;
		}
		samples += (int)(written / sizeof(short));
		m->framed++;
		memmove(m->acc, m->acc + MSBC_FRAME_BYTES,
			(size_t)(m->acclen - MSBC_FRAME_BYTES));
		m->acclen -= MSBC_FRAME_BYTES;
	}
	return samples;
}

/* ------------------------------------------------------------------ ALSA */

static volatile sig_atomic_t stop_flag;

/*
 * Seconds since boot, for the log.
 *
 * Wall-clock time is not usable here. The RTC clamps to 2010-01-01 on a cold
 * boot and chrony corrects it later, so anything logged in the first minute
 * carries a 2010 date - which is exactly how the three captured sessions were
 * eventually placed: the file mtime read 2010-01-01 00:01:07, so all three had
 * happened inside the first 67 seconds of some boot, before time sync. That was
 * deducible only from the file's mtime, and only for the LAST line in it.
 */
static double uptime_s(void)
{
	double up = -1.0;
	FILE *f = fopen("/proc/uptime", "r");
	if (f) {
		if (fscanf(f, "%lf", &up) != 1) up = -1.0;
		fclose(f);
	}
	return up;
}
static void on_signal(int s) { (void)s; stop_flag = 1; }

static int pcm_open_unlocked(snd_pcm_t **pcm, const char *dev, snd_pcm_stream_t dir,
		    unsigned rate, snd_pcm_uframes_t period,
		    snd_pcm_uframes_t buffer, const char *what)
{
	snd_pcm_hw_params_t *hw;
	int err;

	err = snd_pcm_open(pcm, dev, dir, 0);
	if (err < 0) {
		fprintf(stderr, "btcall: open %s (%s): %s\n", dev, what,
			snd_strerror(err));
		return err;
	}

	snd_pcm_hw_params_alloca(&hw);
	#define HW_CHECK(call) do { err = (call); if (err < 0) goto failed; } while (0)
	HW_CHECK(snd_pcm_hw_params_any(*pcm, hw));
	HW_CHECK(snd_pcm_hw_params_set_access(*pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED));
	HW_CHECK(snd_pcm_hw_params_set_format(*pcm, hw, SND_PCM_FORMAT_S16_LE));
	HW_CHECK(snd_pcm_hw_params_set_channels(*pcm, hw, 1));
	HW_CHECK(snd_pcm_hw_params_set_rate(*pcm, hw, rate, 0));
	/* Exact, not "near": on the BT device a period ALSA rounds up past the
	 * driver's 928-frame ring deadlocks the stream with no error to say
	 * why. Failing loudly here is better. */
	HW_CHECK(snd_pcm_hw_params_set_period_size(*pcm, hw, period, 0));
	HW_CHECK(snd_pcm_hw_params_set_buffer_size(*pcm, hw, buffer));

	err = snd_pcm_hw_params(*pcm, hw);
failed:
	#undef HW_CHECK
	if (err < 0) {
		fprintf(stderr,
			"btcall: hw_params %s (%s, period %lu, buffer %lu): %s\n",
			dev, what, (unsigned long)period,
			(unsigned long)buffer, snd_strerror(err));
		snd_pcm_close(*pcm);
		*pcm = NULL;
		return err;
	}
	return 0;
}

/* The PipeWire ALSA plugin calls getpwuid() while creating its context.
 * On musl that uses shared storage: simultaneous speaker/microphone opens
 * produced a reproducible malloc crash inside getdelim()/getpwuid(). Keep
 * context creation serialized, including speaker reopens during a call.
 * Streaming on the independent handles remains concurrent. */
static pthread_mutex_t pcm_open_lock = PTHREAD_MUTEX_INITIALIZER;

static int pcm_open(snd_pcm_t **pcm, const char *dev, snd_pcm_stream_t dir,
		    unsigned rate, snd_pcm_uframes_t period,
		    snd_pcm_uframes_t buffer, const char *what)
{
	pthread_mutex_lock(&pcm_open_lock);
	int err = pcm_open_unlocked(pcm, dev, dir, rate, period, buffer, what);
	pthread_mutex_unlock(&pcm_open_lock);
	return err;
}

/* -------------------------------------------------------------- transmit */

/* TX has eighteen 60-byte slots. Unlike RX it has no validity words.
 * ALSA calls each two bytes a frame even though these are encoded bytes.
 * A 180-byte write is 90 ALSA frames and 22.5 ms of either codec. */
#define TX_BYTES 180
#define TX_FRAMES (TX_BYTES / 2)
#define TX_BUFFER_FRAMES (18 * 60 / 2)

struct tx_args {
	const char *micdev;
	int wideband;
	int tone;
	atomic_int done;
	unsigned long chunks, xruns;
	int error;
};

static int tx_encode(struct cvsd *c, sbc_t *sbc, int wideband,
		     unsigned *sequence, const short *pcm, unsigned char *out)
{
	static const unsigned char h2[] = { 0x08, 0x38, 0xc8, 0xf8 };
	if (!wideband)
		return cvsd_encode(c, pcm, PCM_PER_CHUNK, out);
	for (int i = 0; i < 3; i++) {
		ssize_t written = 0;
		unsigned char *frame = out + i * MSBC_FRAME_BYTES;
		frame[0] = MSBC_H2_SYNC;
		frame[1] = h2[(*sequence)++ % 4];
		ssize_t used = sbc_encode(sbc, pcm + i * MSBC_SAMPLES,
			MSBC_SAMPLES * sizeof(short), frame + 2, MSBC_SBC_BYTES,
			&written);
		if (used != MSBC_SAMPLES * (ssize_t)sizeof(short) ||
		    written != MSBC_SBC_BYTES)
			return -EIO;
		frame[59] = 0;
	}
	return TX_BYTES;
}

static void *tx_thread(void *arg)
{
	struct tx_args *ta = arg;
	snd_pcm_t *mic = NULL, *bt = NULL;
	snd_pcm_sw_params_t *sw;
	struct cvsd enc;
	sbc_t sbc;
	unsigned sequence = 0;
	int sbc_ready = 0, err = 0, rate = ta->wideband ? MSBC_RATE : SCO_RATE;
	int samples = ta->wideband ? MSBC_PCM_PER_CHUNK : PCM_PER_CHUNK;
	short pcm[MAX_PCM_PER_CHUNK];
	unsigned char encoded[TX_BYTES];
	double phase = 0;
	/* Declared here, before the first exit, and set by every one of them.  It
	 * used to be declared below the setup exits, so a failed open jumped past
	 * its initialiser and the "TX ended ... at %s" line read an unset pointer
	 * - on exactly the path this instrument exists to report. */
	const char *where = "setup";

	cvsd_init(&enc);
	if (ta->wideband) {
		err = sbc_init_msbc(&sbc, 0);
		if (err < 0) { where = "msbc-init"; goto out; }
		sbc.endian = SBC_LE;
		sbc_ready = 1;
	}
	if (!ta->tone) {
		err = pcm_open(&mic, ta->micdev, SND_PCM_STREAM_CAPTURE,
			rate, samples, samples * 8, "microphone");
		if (err < 0) { where = "mic-open"; goto out; }
		snd_pcm_nonblock(mic, 1);
	}
	/* Deliberately no arbitrary raw-device override: btsco disables the
	 * drain-silence SW_PARAMS path which crashes this driver. */
	err = pcm_open(&bt, "btsco", SND_PCM_STREAM_PLAYBACK, SCO_RATE,
		TX_FRAMES, TX_BUFFER_FRAMES, "bt transmit");
	if (err < 0) { where = "bt-open"; goto out; }
	snd_pcm_sw_params_alloca(&sw);
	if ((err = snd_pcm_sw_params_current(bt, sw)) < 0 ||
	    (err = snd_pcm_sw_params_set_start_threshold(bt, sw, TX_FRAMES * 3)) < 0 ||
	    (err = snd_pcm_sw_params_set_avail_min(bt, sw, TX_FRAMES)) < 0 ||
	    (err = snd_pcm_sw_params(bt, sw)) < 0) {
		where = "bt-swparams";
		goto out;
	}
	snd_pcm_nonblock(bt, 1);
	fprintf(stderr, "btcall: [up=%.1fs] TX opened (%s)\n", uptime_s(),
		ta->tone ? "test tone" : ta->micdev);

	/*
	 * WHERE the transmit stopped, not just which errno it stopped with.
	 *
	 * Five exits below can end as -EIO and every one printed the same
	 * "Input/output error": the mic read, the bt write, either snd_pcm_wait,
	 * either snd_pcm_recover, and the encoder returning the wrong byte count.
	 * Three captured sessions all ended that way after ~200 good chunks, and
	 * the log could not say which arm fired - so the one fact needed to act
	 * on it was the one fact missing.
	 */
	where = "clean";
	while (!stop_flag && !atomic_load(&ta->done)) {
		int filled = 0, idle = 0;
		while (filled < samples && !stop_flag && !atomic_load(&ta->done)) {
			if (ta->tone) {
				pcm[filled++] = (short)(3000.0 * sin(phase));
				phase += 2.0 * M_PI * ta->tone / rate;
				if (phase >= 2.0 * M_PI) phase -= 2.0 * M_PI;
				continue;
			}
			snd_pcm_sframes_t n = snd_pcm_readi(mic, pcm + filled, samples - filled);
			if (n == -EAGAIN || n == 0) {
				err = snd_pcm_wait(mic, 100);
				if (err < 0) { where = "mic-wait"; goto out; }
				if (++idle >= 20) { err = -ETIMEDOUT; where = "mic-idle"; goto out; }
				continue;
			}
			if (n == -EPIPE || n == -ESTRPIPE) {
				ta->xruns++;
				err = snd_pcm_recover(mic, (int)n, 1);
				if (err < 0) { where = "mic-recover"; goto out; }
				filled = 0;
				continue;
			}
			if (n < 0) { err = (int)n; where = "mic-read"; goto out; }
			filled += (int)n;
			idle = 0;
		}
		if (stop_flag || atomic_load(&ta->done)) break;
		err = tx_encode(&enc, &sbc, ta->wideband, &sequence, pcm, encoded);
		if (err != TX_BYTES) { err = -EIO; where = "encode"; goto out; }
		int sent = 0;
		idle = 0;
		while (sent < TX_FRAMES && !stop_flag && !atomic_load(&ta->done)) {
			snd_pcm_sframes_t n = snd_pcm_writei(bt, encoded + sent * 2, TX_FRAMES - sent);
			if (n == -EAGAIN || n == 0) {
				err = snd_pcm_wait(bt, 100);
				if (err < 0) { where = "bt-wait"; goto out; }
				if (++idle >= 20) { err = -ETIMEDOUT; where = "bt-idle"; goto out; }
				continue;
			}
			if (n == -EPIPE || n == -ESTRPIPE) {
				ta->xruns++;
				err = snd_pcm_recover(bt, (int)n, 1);
				if (err < 0) { where = "bt-recover"; goto out; }
				continue;
			}
			if (n < 0) { err = (int)n; where = "bt-write"; goto out; }
			/* The driver's copy callback truncates anything below 60 bytes. */
			if (n % 30) { err = -EPROTO; where = "bt-short"; goto out; }
			sent += (int)n;
			idle = 0;
		}
		if (sent == TX_FRAMES) ta->chunks++;
	}
	err = 0;
out:
	if (bt) { snd_pcm_drop(bt); snd_pcm_close(bt); }
	if (mic) { snd_pcm_drop(mic); snd_pcm_close(mic); }
	if (sbc_ready) sbc_finish(&sbc);
	ta->error = err;
	fprintf(stderr, "btcall: [up=%.1fs] TX ended: %lu chunks, %lu xruns, %s at %s\n",
		uptime_s(), ta->chunks, ta->xruns,
		err < 0 ? snd_strerror(err) : "stopped", where);
	return NULL;
}

/*
 * Playback underruns are COUNTED, not swallowed.
 *
 * This function used to recover from EPIPE and return 0, which the caller read
 * as success. So every playback underrun was invisible: three calls in a row
 * reported "short 0, xrun 0, over 0, under 0" over 20-30 seconds while the
 * audio crackled, and I read those zeros as proof the output path was fine.
 * They only ever counted BT-side and ring-side losses.
 */
static int pcm_write(snd_pcm_t *pcm, const void *buf, snd_pcm_uframes_t frames,
		     unsigned long *underruns, atomic_int *done)
{
	snd_pcm_uframes_t sent = 0;
	int waits = 0;
	while (sent < frames) {
		if (stop_flag || atomic_load(done)) return -ECANCELED;
		snd_pcm_sframes_t n = snd_pcm_writei(pcm, (const short *)buf + sent, frames - sent);
		if (n == -EAGAIN || n == 0) {
			int err = snd_pcm_wait(pcm, 100);
			if (err < 0) return err;
			if (++waits >= 20) return -ETIMEDOUT;
			continue;
		}
		if (n == -EPIPE || n == -ESTRPIPE) {
			if (underruns) (*underruns)++;
			int err = snd_pcm_recover(pcm, (int)n, 1);
			if (err < 0) return err;
			if (++waits >= 20) return -EIO;
			continue;
		}
		if (n < 0) return (int)n;
		sent += n;
		waits = 0;
	}
	return (int)sent;
}

/* --------------------------------------------------------------- the call */

/*
 * WHY TWO THREADS. The first version read 22.5 ms from BT and then wrote it to
 * PipeWire on the same thread, which couples the two clocks: any stall in the
 * write lets the driver's ring overflow, and because CVSD is a DELTA codec a
 * dropped byte does not merely mute - it desynchronises the decoder from the
 * far end for everything that follows. Reported from a real call as "extreme
 * fuzzing and clicks", while the identical bytes captured to a file decoded
 * cleanly offline. So the BT side now never waits for audio output.
 *
 * Two places used to discard data silently, and in a delta codec that corrupts
 * rather than merely interrupts: a short read (got/RX_PACKET_BYTES truncating
 * the remainder) and every snd_pcm_recover() after an xrun. Both are counted
 * and reported now, because "it sounds bad" is not a measurement and this cost
 * a long night the first time.
 */

#define RING_CHUNKS 64          /* ~1.4 s of slack at 22.5 ms per chunk */

struct ring {
	short buf[RING_CHUNKS][MAX_PCM_PER_CHUNK];
	int len[RING_CHUNKS];
	int head, tail, done;
	pthread_mutex_t lock;
	pthread_cond_t data;
	unsigned long overruns, underruns;
};

static void ring_init(struct ring *r)
{
	memset(r, 0, sizeof(*r));
	pthread_mutex_init(&r->lock, NULL);
	pthread_cond_init(&r->data, NULL);
}

static void ring_push(struct ring *r, const short *s, int n)
{
	int next;

	pthread_mutex_lock(&r->lock);
	next = (r->head + 1) % RING_CHUNKS;
	if (next == r->tail) {
		/* The consumer is behind. Drop the OLDEST chunk so the call
		 * stays near real time instead of drifting further back. */
		r->tail = (r->tail + 1) % RING_CHUNKS;
		r->overruns++;
	}
	memcpy(r->buf[r->head], s, (size_t)n * sizeof(short));
	r->len[r->head] = n;
	r->head = next;
	pthread_cond_signal(&r->data);
	pthread_mutex_unlock(&r->lock);
}

static int ring_pop(struct ring *r, short *out)
{
	struct timespec ts;
	int n = 0;

	pthread_mutex_lock(&r->lock);
	while (r->head == r->tail && !r->done) {
		clock_gettime(CLOCK_REALTIME, &ts);
		ts.tv_nsec += 100000000L;               /* 100 ms */
		if (ts.tv_nsec >= 1000000000L) {
			ts.tv_sec++;
			ts.tv_nsec -= 1000000000L;
		}
		if (pthread_cond_timedwait(&r->data, &r->lock, &ts) != 0) {
			r->underruns++;
			break;
		}
	}
	if (r->head != r->tail) {
		n = r->len[r->tail];
		memcpy(out, r->buf[r->tail], (size_t)n * sizeof(short));
		r->tail = (r->tail + 1) % RING_CHUNKS;
	}
	pthread_mutex_unlock(&r->lock);
	return n;
}

struct play_args {
	struct ring *ring;
	const char *outdev;
	int verbose;
	int rate;               /* 8000 for CVSD, 16000 for mSBC */
	int chunk;              /* samples per chunk at that rate */
	unsigned long spk_underruns;
	unsigned long refills;  /* times the cushion was rebuilt with silence */
	atomic_int done;
};

/*
 * PREFILL. The producer delivers 180 samples every 22.5 ms - exactly real time
 * and not a frame more - so a playback buffer that starts empty stays empty,
 * and any jitter at all underruns it. aplay does not have this problem because
 * it fills the buffer as fast as the file can be read, which is why the same
 * decoded audio played cleanly through this identical path while the live
 * bridge crackled. A cushion of silence up front is what turns "always on the
 * edge of empty" into "a few periods in hand".
 */
#define PREFILL_CHUNKS 8        /* 180 ms of silence before real audio */
#define LOW_CHUNKS     2        /* rebuild the cushion below 45 ms queued */
#define SPK_CHUNKS    16        /* speaker buffer, in chunks */

/*
 * KEEP THE CUSHION, NOT JUST START WITH ONE. The prefill used to happen once,
 * at call start, and nothing ever rebuilt it. Windows opens the microphone
 * first and sends no audio until its own playback stream starts a second or
 * more later, so the cushion drained before the first beep; after that every
 * write went into an empty buffer at exactly real time, and each small jitter
 * underran it again. Measured: 87 speaker underruns in a 26 s call and 17 in a
 * 25 s one that lost only 5 of 3,348 frames - heard as the beeps "not playing
 * cleanly". So top the queue back up with silence whenever it runs low: a gap
 * then costs one controlled insertion of silence instead of a cascade of
 * underruns.
 *
 * Two thresholds, because the two cases are checked at different rates. While
 * audio flows the check runs every 22.5 ms, so 45 ms queued is still safe.
 * When nothing has arrived, ring_pop only wakes every 100 ms - a 45 ms mark
 * would let the queue run dry between checks - so an idle wake always tops up
 * to the full cushion. Only the flowing case counts as a refill: that is the
 * one that inserts silence into audio.
 */
static int keep_cushion(snd_pcm_t *spk, struct play_args *pa, const short *quiet,
			int idle)
{
	snd_pcm_sframes_t avail = snd_pcm_avail(spk);

	if (avail == -EPIPE || avail == -ESTRPIPE) {
		pa->spk_underruns++;
		int err = snd_pcm_recover(spk, (int)avail, 1);
		if (err < 0)
			return err;
		avail = snd_pcm_avail(spk);
	}
	if (avail < 0)
		return (int)avail;

	snd_pcm_sframes_t queued = (snd_pcm_sframes_t)pa->chunk * SPK_CHUNKS - avail;

	if (queued >= (snd_pcm_sframes_t)pa->chunk * (idle ? PREFILL_CHUNKS : LOW_CHUNKS))
		return 0;
	if (!idle)
		pa->refills++;
	for (; queued < (snd_pcm_sframes_t)pa->chunk * PREFILL_CHUNKS; queued += pa->chunk) {
		int err = pcm_write(spk, quiet, pa->chunk, &pa->spk_underruns, &pa->done);
		if (err < 0)
			return err;
	}
	return 0;
}

static void *play_thread(void *arg)
{
	struct play_args *pa = arg;
	snd_pcm_t *spk = NULL;
	short chunk[MAX_PCM_PER_CHUNK];
	short quiet[MAX_PCM_PER_CHUNK];

	if (pcm_open(&spk, pa->outdev, SND_PCM_STREAM_PLAYBACK, pa->rate,
		     pa->chunk, pa->chunk * SPK_CHUNKS, "speaker") < 0)
		return NULL;
	snd_pcm_nonblock(spk, 1);

	memset(quiet, 0, sizeof(quiet));
	for (int i = 0; i < PREFILL_CHUNKS; i++)
		if (pcm_write(spk, quiet, pa->chunk, &pa->spk_underruns, &pa->done) < 0)
			break;

	for (;;) {
		int n = ring_pop(pa->ring, chunk);

		if (atomic_load(&pa->done) || stop_flag)
			break;
		if (keep_cushion(spk, pa, quiet, n <= 0) < 0 && (atomic_load(&pa->done) || stop_flag))
			break;
		if (n <= 0)
			continue;
		if (pcm_write(spk, chunk, n, &pa->spk_underruns, &pa->done) < 0) {
			if (atomic_load(&pa->done) || stop_flag) break;
			/* An earcon taking the output must not end the call -
			 * reported as "the earcon broke the connection,
			 * nothing played after". Reopen and carry on. */
			if (pa->verbose)
				fprintf(stderr,
					"btcall: speaker lost; reopening\n");
			snd_pcm_close(spk);
			spk = NULL;
			if (pcm_open(&spk, pa->outdev, SND_PCM_STREAM_PLAYBACK,
				     pa->rate, pa->chunk,
				     pa->chunk * 16, "speaker") < 0)
				break;
			snd_pcm_nonblock(spk, 1);
		}
	}
	if (spk) {
		snd_pcm_drop(spk);
		snd_pcm_close(spk);
	}
	return NULL;
}

/*
 * The dump exists because four theories in a row were wrong. Every counter
 * reads zero - no short reads, no BT xruns, no ring overruns, no playback
 * underruns - across 18 to 32 second calls that crackle, while the SAME
 * bytes captured with arecord and decoded offline sound like the call. So
 * the next step is not another mechanism: it is to write down exactly what
 * this program produced and compare it against the offline decode.
 */
/*
 * Tell biscuit-dsp a call is up, so it holds the codec open.
 *
 * The DSP releases the codec after ten seconds of silence, and every release
 * and re-open stores a fresh reference generation, which makes biscuit-call-dsp
 * throw away its converged echo-canceller filter. Over a 557 s call that was
 * ten resets - one per conversational pause - and the far end heard themselves
 * for the seconds it took to reconverge each time.
 *
 * O_NONBLOCK is load-bearing: opening a FIFO for writing BLOCKS until a reader
 * appears, so if biscuit-dsp were down this would hang the receive loop and
 * take the call with it. With O_NONBLOCK the open fails ENXIO instead, and a
 * missing DSP simply means no hold - which is the old behaviour, not a fault.
 */
static void dsp_call(int on)
{
	int fd = open("/run/biscuit-dsp/control", O_WRONLY | O_NONBLOCK);

	if (fd < 0)
		return;
	const char *msg = on ? "call on\n" : "call off\n";
	ssize_t unused = write(fd, msg, strlen(msg));

	(void)unused;
	close(fd);
}

/* The call's liveness, for anything that must not restart the audio path
 * mid-call - biscuit-mic-restart.py's guard. Written at call-up, refreshed
 * with the DSP hold every 200 chunks, removed at call end; a reader trusts it
 * only while its mtime is recent, so a btcall that dies mid-call cannot block
 * restarts for ever. The guard's previous signal, the hub's transport_bytes,
 * read 0 through an entire 557 s call and so had never once fired. */
#define CALL_STATE_DIR  "/run/biscuit-btcall"
#define CALL_STATE_FILE CALL_STATE_DIR "/call"

static void publish_call(int on)
{
	if (!on) {
		unlink(CALL_STATE_FILE);
		return;
	}
	mkdir(CALL_STATE_DIR, 0755);
	FILE *f = fopen(CALL_STATE_FILE ".tmp", "w");

	if (!f)
		return;
	fprintf(f, "up %.1f\n", uptime_s());
	if (fclose(f) == 0)
		rename(CALL_STATE_FILE ".tmp", CALL_STATE_FILE);
}

static int run_call(const char *btdev, const char *outdev, int verbose,
		    float gain, const char *dumppcm, const char *dumpraw,
		    const char *micdev, int tx_tone, int codec)
{
	/* APPEND, not truncate: run_call is re-entered by the retry loop in
	 * main() as soon as a call ends, so "wb" reopened these and wiped
	 * them - 814 chunks of a real call recorded, then truncated to zero
	 * bytes a fraction of a second later. */
	FILE *fpcm = dumppcm ? fopen(dumppcm, "ab") : NULL;
	FILE *fraw = dumpraw ? fopen(dumpraw, "ab") : NULL;
	snd_pcm_t *bt_in = NULL;
	unsigned char raw[RX_CHUNK_BYTES];
	unsigned char cvsd_in[RX_AUDIO_CHUNK];
	short pcm_rx[MAX_PCM_PER_CHUNK * 2];
	struct cvsd dec;
	struct msbc mdec;
	int wideband = -1;              /* decided on the first chunk */
	struct ring ring;
	struct play_args pa;
	pthread_t player;
	pthread_t transmitter;
	struct tx_args ta = { .micdev = micdev, .tone = tx_tone };
	int tx_started = 0;
	atomic_init(&ta.done, 0);
	unsigned long chunks = 0, shortreads = 0, recovered = 0;
	int started = 0, err = 0;

	cvsd_init(&dec);
	/* free() and the counters are read unconditionally, so this must be
	 * valid even on a call that never goes wideband. */
	memset(&mdec, 0, sizeof(mdec));
	ring_init(&ring);

	/*
	 * hw:0,3 OPENS even with no call - only the first READ fails. An
	 * earlier version treated a successful open as a call starting, so it
	 * declared one, failed, tore down and repeated twice a second: 371
	 * cycles in a sitting, each churning PipeWire. Nothing else is opened
	 * until a read actually returns data.
	 */
	if (pcm_open(&bt_in, btdev, SND_PCM_STREAM_CAPTURE, SCO_RATE,
		     RX_CHUNK_BYTES / 2, RX_CHUNK_BYTES * 4, "bt capture") < 0) {
		if (fpcm) fclose(fpcm);
		if (fraw) fclose(fraw);
		pthread_cond_destroy(&ring.data);
		pthread_mutex_destroy(&ring.lock);
		return -1;
	}

	memset(&pa, 0, sizeof(pa));
	atomic_init(&pa.done, 0);
	pa.ring = &ring;
	pa.outdev = outdev;
	pa.verbose = verbose;

	while (!stop_flag) {
		snd_pcm_sframes_t n = snd_pcm_readi(bt_in, raw,
						    RX_CHUNK_BYTES / 2);

		if (n == -EPIPE || n == -ESTRPIPE) {
			snd_pcm_recover(bt_in, (int)n, 1);
			recovered++;          /* a delta codec notices this */
			continue;
		}
		if (n < 0) {
			if (verbose && started)
				fprintf(stderr, "btcall: bt read: %s\n",
					snd_strerror((int)n));
			err = (int)n;
			break;
		}
		if (n == 0)
			continue;

		int got = (int)n * 2;

		if (fraw)
			fwrite(raw, 1, (size_t)got, fraw);

		if (got % RX_PACKET_BYTES)
			shortreads++;         /* the remainder is discarded */

		int packets = got / RX_PACKET_BYTES, alen = 0;

		for (int p = 0; p < packets; p++) {
			memcpy(cvsd_in + alen,
			       raw + p * RX_PACKET_BYTES, RX_AUDIO_BYTES);
			alen += RX_AUDIO_BYTES;
		}

		/*
		 * Decide the codec from the payload, before the speaker is
		 * opened - the two run at different rates and reopening a
		 * stream mid-call is exactly the kind of churn that used to
		 * break the audio path.
		 */
		if (wideband < 0) {
			wideband = codec < 0 ? negotiated_codec() : codec;
			if (wideband < 0) wideband = msbc_looks_like(cvsd_in, alen);
			if (wideband)
				msbc_init(&mdec);
			pa.rate = wideband ? MSBC_RATE : SCO_RATE;
			pa.chunk = wideband ? MSBC_PCM_PER_CHUNK
					    : PCM_PER_CHUNK;
		}

		if (!started) {
			if (pthread_create(&player, NULL, play_thread, &pa)) {
				fprintf(stderr, "btcall: cannot start player\n");
				break;
			}
			started = 1;
			if (micdev || tx_tone) {
				ta.wideband = wideband;
				if (pthread_create(&transmitter, NULL, tx_thread, &ta))
					fprintf(stderr, "btcall: cannot start transmitter\n");
				else
					tx_started = 1;
			}
			fprintf(stderr, "btcall: [up=%.1fs] call up, %s (%s)\n", uptime_s(),
				wideband ? "mSBC wideband 16 kHz"
					 : "CVSD narrowband 8 kHz",
				tx_started ? "transmit requested" : "receive only");
			dsp_call(1);
			publish_call(1);
		}

		int samples = wideband
			? msbc_decode(&mdec, cvsd_in, alen, pcm_rx,
				      MAX_PCM_PER_CHUNK * 2)
			: cvsd_decode(&dec, cvsd_in, alen, pcm_rx);

		if (gain != 1.0f) {
			for (int i = 0; i < samples; i++) {
				float v = pcm_rx[i] * gain;

				pcm_rx[i] = (short)(v > 32767.0f ? 32767 :
						    (v < -32768.0f ? -32768 : v));
			}
		}
		if (fpcm && samples > 0)
			fwrite(pcm_rx, sizeof(short), (size_t)samples, fpcm);
		if (samples > 0)
			ring_push(&ring, pcm_rx, samples);

		/*
		 * The increment is NOT inside the && - it used to be
		 * `verbose && (++chunks % 200) == 0`, and && short-circuits, so
		 * with verbose off the counter never ran. The service runs
		 * `--duplex --out default`, no -v, so in production `chunks` was
		 * always zero and so was the duration printed from it
		 * (chunks * 45 / 2000). Every call summary read
		 * "ended: 0 chunks (0 s)" no matter how long it lasted or how
		 * much audio arrived, which made the one line that says whether
		 * the receive path worked say nothing at all.
		 */
		++chunks;
		/* Renew the DSP's call hold. CALL_HOLD_S is a 20 s watchdog so a
		 * btcall that dies cannot pin the amplifier on; 200 chunks is
		 * about 4.5 s, so four refreshes can be missed before it lapses. */
		if ((chunks % 200) == 0) {
			dsp_call(1);
			publish_call(1);
		}
		if (verbose && (chunks % 200) == 0)
			fprintf(stderr,
				"btcall: %lu chunks, short %lu, xrun %lu, over %lu, under %lu, spkunder %lu, refill %lu, frames %lu, resync %lu\n",
				chunks, shortreads, recovered,
				ring.overruns, ring.underruns, pa.spk_underruns, pa.refills,
				mdec.framed, mdec.resyncs);
	}

	/* Only if we actually raised the hold. run_call() returns on every poll
	 * that finds no call, and an unconditional "call off" here cancelled a
	 * hold this process never set - which is exactly what the first test of
	 * this feature caught: the DSP logged "call: holding the codec open"
	 * and then "call: released" a few seconds later with no call anywhere.
	 *
	 * If this is missed - a crash, a kill - the watchdog in biscuit-dsp
	 * lapses within 20 s on its own.
	 */
	if (started) {
		dsp_call(0);
		publish_call(0);
	}

	atomic_store(&ta.done, 1);
	atomic_store(&pa.done, 1);
	if (tx_started) pthread_join(transmitter, NULL);
	if (started) {
		pthread_mutex_lock(&ring.lock);
		ring.done = 1;
		pthread_cond_signal(&ring.data);
		pthread_mutex_unlock(&ring.lock);
		pthread_join(player, NULL);
		fprintf(stderr,
			"btcall: [up=%.1fs] ended: %lu chunks (%lu s), %s, short %lu, xrun %lu, over %lu, under %lu, SPKUNDER %lu, refill %lu, frames %lu, resync %lu\n",
			uptime_s(), chunks, chunks * 45 / 2000,
			wideband > 0 ? "mSBC" : "CVSD", shortreads, recovered,
			ring.overruns, ring.underruns, pa.spk_underruns, pa.refills,
			mdec.framed, mdec.resyncs);
	}
	msbc_free(&mdec);
	if (fpcm)
		fclose(fpcm);
	if (fraw)
		fclose(fraw);
	snd_pcm_close(bt_in);
	pthread_cond_destroy(&ring.data);
	pthread_mutex_destroy(&ring.lock);
	return err;
}

int main(int argc, char **argv)
{
	/*
	 * "btsco", not "hw:0,3" - see note 3 above. It is the same device with
	 * drain_silence 0, defined in the asound.conf this package ships, and
	 * opening the raw name for playback reboots the board.
	 */
	const char *btdev = "btsco";
	const char *outdev = "default";
	const char *dumppcm = NULL, *dumpraw = NULL;
	const char *micdev = NULL;
	int tx_tone = 0, codec = -1;
	int verbose = 0, once = 0;
	/*
	 * Unity. Two earlier builds guessed at this - 60% because a call was
	 * described as loud, then 200% because a decode looked quiet - and both
	 * were reactions to a quiet voicemail sample or to mSBC being decoded as
	 * CVSD. Correctly decoded CVSD from a real call measures about -9 dBFS
	 * with no clipping, which needs no correction at all.
	 */
	float gain = 1.0f;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--bt") && i + 1 < argc)
			btdev = argv[++i];
		else if (!strcmp(argv[i], "--out") && i + 1 < argc)
			outdev = argv[++i];
		else if (!strcmp(argv[i], "--gain") && i + 1 < argc)
			gain = (float)atof(argv[++i]) / 100.0f;
		else if (!strcmp(argv[i], "--dump-pcm") && i + 1 < argc)
			dumppcm = argv[++i];
		else if (!strcmp(argv[i], "--dump-raw") && i + 1 < argc)
			dumpraw = argv[++i];
		else if (!strcmp(argv[i], "--once"))
			once = 1;
		else if (!strcmp(argv[i], "-v"))
			verbose = 1;
		else if (!strcmp(argv[i], "--rx-only"))
			{ micdev = NULL; tx_tone = 0; }
		else if (!strcmp(argv[i], "--duplex"))
			micdev = "default";
		else if (!strcmp(argv[i], "--mic") && i + 1 < argc)
			micdev = argv[++i];
		else if (!strcmp(argv[i], "--tx-tone") && i + 1 < argc) {
			tx_tone = atoi(argv[++i]);
			if (tx_tone < 100 || tx_tone > 3000) return 2;
		}
		else if (!strcmp(argv[i], "--codec") && i + 1 < argc) {
			const char *name = argv[++i];
			if (!strcmp(name, "auto")) codec = -1;
			else if (!strcmp(name, "cvsd")) codec = 0;
			else if (!strcmp(name, "msbc")) codec = 1;
			else return 2;
		}
		else {
			fprintf(stderr,
				"usage: %s [--bt btsco] [--out default] "
				"[--gain 100] [--once] [-v] [--duplex | --mic PCM | --tx-tone HZ] "
				"[--codec auto|cvsd|msbc] [--rx-only]\n", argv[0]);
			return 2;
		}
	}
	if ((micdev || tx_tone) && strcmp(btdev, "btsco")) {
		fprintf(stderr, "btcall: transmit requires the safe btsco PCM\n");
		return 2;
	}

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	while (!stop_flag) {
		run_call(btdev, outdev, verbose, gain, dumppcm, dumpraw,
			 micdev, tx_tone, codec);
		if (once)
			break;
		if (!stop_flag)
			usleep(400000);
	}
	return 0;
}
