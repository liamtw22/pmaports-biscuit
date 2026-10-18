/*
 * biscuit-tick - the volume tone, restarted on every key press.
 *
 * Usage: biscuit-tick <sound file>
 *
 * Reads commands from stdin, one per line:
 *
 *   play     start the tone from the top, now
 *
 * EOF on stdin ends the program, so it dies with biscuit-audio, which owns it.
 *
 * Why a program of its own: spawning a player per press cost about 190 ms
 * before the first sample reached the speaker sink - connecting to PipeWire,
 * the sink resuming from suspend, and 45 ms of silence at the head of the
 * stock tone - and biscuit-audio then spaced the tones 0.45 s apart so they
 * would not pile up. A quick run of presses was heard well behind the fingers.
 *
 * Here the file is decoded once, its leading silence dropped, and the stream
 * stays connected, playing silence between ticks. A press only sets a flag;
 * the next process() call starts the tone, so a tick reaches the sink one
 * graph cycle later. A tone still playing is faded out over 2 ms underneath
 * the new one rather than cut, which would click.
 *
 * Played into a speaker amplifier that is still off, the tick loses its start
 * and the amplifier is heard clicking on part way through: after ten seconds
 * of silence biscuit-dsp releases the codec, and the amplifier comes back about
 * 275 ms after the next open. So a press first reads biscuit-dsp's amplifier
 * state, and unless it says "ready" asks for a wake and holds the tick until it
 * does. That costs the first press after a quiet spell about 0.3 s; while the
 * amplifier is up, which it is for ten seconds after any sound, a press is
 * still heard at once.
 *
 * Feeding pw-cat through a pipe was tried first and is NOT equivalent: pw-cat
 * reads its input inside the process callback, any wait there misses the
 * cycle, and the sink pads with a 256-frame run of silence - a click inside
 * the tone, several times in a quick run of presses.
 */
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <sndfile.h>
#include <spa/param/audio/format-utils.h>
#include <pipewire/pipewire.h>

/* The file plays at its own rate and PipeWire converts it to the graph's.
 * Requiring 48 kHz silenced every tick whenever the volume sound was set to
 * one of the assistant's 22.05 kHz files: biscuit-tick refused it at start,
 * and biscuit-audio, which had just started it, took the press as played. */
#define RATE_MIN      8000
#define RATE_MAX      192000
#define CHANNELS      2
#define FADE_MS       2
#define LEAD_MS       1          /* kept before the onset */
#define SILENT_BELOW  64         /* the head of the file counts as silent */
#define AMP_STATE     "/run/biscuit-dsp/amp"
#define DSP_CONTROL   "/run/biscuit-dsp/control"
#define AMP_POLL_MS   10
#define AMP_WAIT_MS   600        /* play anyway after this: never lose a tick */
/* Exit status for "this file cannot be the tick" - unreadable, silent, or a
 * format refused below. biscuit-audio stops restarting a program that exits
 * with it and plays the sound its old way instead. Every other failure - no
 * PipeWire yet at boot, a PipeWire restart - exits 1 and is simply retried. */
#define EXIT_UNPLAYABLE 3

struct tick {
	struct pw_main_loop *loop;
	struct pw_stream *stream;
	struct spa_source *input;
	struct spa_source *amp_timer;
	int amp_waited_ms;           /* -1 when no tick is waiting */

	short *pcm;                  /* interleaved stereo, onset first */
	long frames;
	int rate;                    /* the file's */
	int fade_frames;             /* FADE_MS at that rate */

	long pos;                    /* frame playing, or -1 */
	long fade_pos;               /* frame of a tone being faded out, or -1 */
	int fade_left;
	int start;                   /* set by a press, taken by process() */

	char line[64];
	size_t used;
};

static int load(struct tick *t, const char *path)
{
	SF_INFO info;
	memset(&info, 0, sizeof(info));
	SNDFILE *f = sf_open(path, SFM_READ, &info);
	if (!f) {
		fprintf(stderr, "biscuit-tick: %s: %s\n", path, sf_strerror(NULL));
		return -1;
	}
	if (info.samplerate < RATE_MIN || info.samplerate > RATE_MAX ||
	    info.channels < 1 || info.channels > 2) {
		fprintf(stderr, "biscuit-tick: %s: need %d-%d Hz mono or stereo, "
			"got %d Hz %d channels\n", path, RATE_MIN, RATE_MAX,
			info.samplerate, info.channels);
		sf_close(f);
		return -1;
	}
	short *raw = calloc((size_t)info.frames * info.channels, sizeof(short));
	if (!raw) {
		sf_close(f);
		return -1;
	}
	sf_count_t got = sf_readf_short(f, raw, info.frames);
	sf_close(f);

	long first = 0;
	while (first < got) {
		int loud = 0;
		for (int c = 0; c < info.channels; c++)
			if (abs(raw[first * info.channels + c]) > SILENT_BELOW)
				loud = 1;
		if (loud)
			break;
		first++;
	}
	if (first >= got) {
		fprintf(stderr, "biscuit-tick: %s is silent\n", path);
		free(raw);
		return -1;
	}
	t->rate = info.samplerate;
	t->fade_frames = t->rate * FADE_MS / 1000;
	long lead = (long)t->rate * LEAD_MS / 1000;
	first = first > lead ? first - lead : 0;

	t->frames = got - first;
	t->pcm = calloc((size_t)t->frames * CHANNELS, sizeof(short));
	if (!t->pcm) {
		free(raw);
		return -1;
	}
	for (long i = 0; i < t->frames; i++)
		for (int c = 0; c < CHANNELS; c++)
			t->pcm[i * CHANNELS + c] =
				raw[(first + i) * info.channels
				    + (info.channels == 1 ? 0 : c)];
	free(raw);
	fprintf(stderr, "biscuit-tick: %s, %ld ms of leading silence dropped\n",
		path, first * 1000 / t->rate);
	return 0;
}

static void on_process(void *data)
{
	struct tick *t = data;
	struct pw_buffer *b = pw_stream_dequeue_buffer(t->stream);
	if (!b)
		return;
	struct spa_buffer *buf = b->buffer;
	short *out = buf->datas[0].data;
	if (!out) {
		pw_stream_queue_buffer(t->stream, b);
		return;
	}
	uint32_t stride = sizeof(short) * CHANNELS;
	uint32_t n = buf->datas[0].maxsize / stride;
	if (b->requested && b->requested < n)
		n = b->requested;

	if (__atomic_exchange_n(&t->start, 0, __ATOMIC_ACQ_REL)) {
		if (t->pos >= 0) {
			t->fade_pos = t->pos;
			t->fade_left = t->fade_frames;
		}
		t->pos = 0;
	}

	for (uint32_t i = 0; i < n; i++) {
		for (int c = 0; c < CHANNELS; c++) {
			int v = 0;
			if (t->pos >= 0)
				v += t->pcm[t->pos * CHANNELS + c];
			if (t->fade_left > 0 && t->fade_pos < t->frames)
				v += t->pcm[t->fade_pos * CHANNELS + c]
				     * t->fade_left / (t->fade_frames + 1);
			out[i * CHANNELS + c] = v > 32767 ? 32767
					      : v < -32768 ? -32768 : v;
		}
		if (t->pos >= 0 && ++t->pos >= t->frames)
			t->pos = -1;
		if (t->fade_left > 0) {
			t->fade_left--;
			t->fade_pos++;
		}
	}

	buf->datas[0].chunk->offset = 0;
	buf->datas[0].chunk->stride = stride;
	buf->datas[0].chunk->size = n * stride;
	pw_stream_queue_buffer(t->stream, b);
}

static void on_state(void *data, enum pw_stream_state old,
		     enum pw_stream_state state, const char *error)
{
	struct tick *t = data;
	(void)old;
	if (state == PW_STREAM_STATE_ERROR) {
		/* Exit and let biscuit-audio start a fresh one, rather than
		 * sitting connected to nothing. */
		fprintf(stderr, "biscuit-tick: stream error: %s\n",
			error ? error : "?");
		pw_main_loop_quit(t->loop);
	} else if (state == PW_STREAM_STATE_UNCONNECTED) {
		fprintf(stderr, "biscuit-tick: disconnected\n");
		pw_main_loop_quit(t->loop);
	}
}

static const struct pw_stream_events stream_events = {
	PW_VERSION_STREAM_EVENTS,
	.state_changed = on_state,
	.process = on_process,
};

/* "ready", "warming", "off", or "" when biscuit-dsp is not running. */
static void amp_state(char *buf, size_t len)
{
	buf[0] = 0;
	int fd = open(AMP_STATE, O_RDONLY);
	if (fd < 0)
		return;
	ssize_t got = read(fd, buf, len - 1);
	close(fd);
	buf[got > 0 ? got : 0] = 0;
	buf[strcspn(buf, "\n")] = 0;
}

static void dsp_wake(void)
{
	/* Non-blocking: with biscuit-dsp stopped there is no reader, and a plain
	 * open would hang the tick player. */
	int fd = open(DSP_CONTROL, O_WRONLY | O_NONBLOCK);
	if (fd < 0)
		return;
	ssize_t n = write(fd, "wake\n", 5);
	(void)n;
	close(fd);
}

static void arm_amp_timer(struct tick *t, int ms)
{
	struct timespec value = { 0, ms * 1000000L }, interval = { 0, 0 };
	pw_loop_update_timer(pw_main_loop_get_loop(t->loop), t->amp_timer,
			     &value, &interval, false);
}

/* Start the tone, or wait for the amplifier and start it then. */
static void request_play(struct tick *t)
{
	char state[16];
	amp_state(state, sizeof(state));
	/* No state file: an older biscuit-dsp, or none - nothing to wait for. */
	if (!state[0] || !strcmp(state, "ready")) {
		t->amp_waited_ms = -1;
		__atomic_store_n(&t->start, 1, __ATOMIC_RELEASE);
		return;
	}
	if (!strcmp(state, "off"))
		dsp_wake();
	if (t->amp_waited_ms < 0) {
		t->amp_waited_ms = 0;
		arm_amp_timer(t, AMP_POLL_MS);
	}
	/* A press while already waiting changes nothing: the tick plays once,
	 * when the amplifier is up. */
}

static void on_amp_timer(void *data, uint64_t expirations)
{
	struct tick *t = data;
	(void)expirations;
	if (t->amp_waited_ms < 0)
		return;
	char state[16];
	amp_state(state, sizeof(state));
	t->amp_waited_ms += AMP_POLL_MS;
	if (!state[0] || !strcmp(state, "ready") || t->amp_waited_ms >= AMP_WAIT_MS) {
		if (state[0] && strcmp(state, "ready"))
			fprintf(stderr, "biscuit-tick: amplifier still %s after %d ms; "
				"playing anyway\n", state, t->amp_waited_ms);
		t->amp_waited_ms = -1;
		__atomic_store_n(&t->start, 1, __ATOMIC_RELEASE);
		return;
	}
	arm_amp_timer(t, AMP_POLL_MS);
}

static void on_input(void *data, int fd, uint32_t mask)
{
	struct tick *t = data;
	if (mask & (SPA_IO_ERR | SPA_IO_HUP) && !(mask & SPA_IO_IN)) {
		pw_main_loop_quit(t->loop);
		return;
	}
	char chunk[256];
	ssize_t got = read(fd, chunk, sizeof(chunk));
	if (got == 0 || (got < 0 && errno != EAGAIN && errno != EINTR)) {
		pw_main_loop_quit(t->loop);
		return;
	}
	for (ssize_t i = 0; i < got; i++) {
		if (chunk[i] != '\n') {
			if (t->used < sizeof(t->line) - 1)
				t->line[t->used++] = chunk[i];
			continue;
		}
		t->line[t->used] = 0;
		t->used = 0;
		if (!strcmp(t->line, "play"))
			request_play(t);
		else if (t->line[0])
			fprintf(stderr, "biscuit-tick: unknown command '%s'\n",
				t->line);
	}
}

static void on_signal(void *data, int sig)
{
	struct tick *t = data;
	(void)sig;
	pw_main_loop_quit(t->loop);
}

int main(int argc, char *argv[])
{
	struct tick t;
	memset(&t, 0, sizeof(t));
	t.pos = -1;
	t.fade_pos = -1;
	t.amp_waited_ms = -1;

	if (argc != 2) {
		fprintf(stderr, "usage: biscuit-tick <sound file>\n");
		return 2;
	}
	if (load(&t, argv[1]) < 0)
		return EXIT_UNPLAYABLE;

	pw_init(&argc, &argv);
	t.loop = pw_main_loop_new(NULL);
	struct pw_loop *loop = pw_main_loop_get_loop(t.loop);
	pw_loop_add_signal(loop, SIGINT, on_signal, &t);
	pw_loop_add_signal(loop, SIGTERM, on_signal, &t);
	t.amp_timer = pw_loop_add_timer(loop, on_amp_timer, &t);
	t.input = pw_loop_add_io(loop, STDIN_FILENO,
				 SPA_IO_IN | SPA_IO_ERR | SPA_IO_HUP, false,
				 on_input, &t);

	/* 512 frames, half the graph's 1024. A press waits at most one buffer.
	 * 256 was measured to put jumps into the tone - a dozen in thirty
	 * quick presses, each a few hundred samples skipped - and 512 and 1024
	 * put none; 512 reaches the sink 6 to 10 ms after the press. */
	t.stream = pw_stream_new_simple(loop, "biscuit-tick",
		pw_properties_new(
			PW_KEY_MEDIA_TYPE, "Audio",
			PW_KEY_MEDIA_CATEGORY, "Playback",
			PW_KEY_MEDIA_ROLE, "Notification",
			PW_KEY_NODE_LATENCY, "512/48000",
			PW_KEY_APP_NAME, "biscuit-tick",
			NULL),
		&stream_events, &t);

	uint8_t pod[1024];
	struct spa_pod_builder bld = SPA_POD_BUILDER_INIT(pod, sizeof(pod));
	const struct spa_pod *params[1];
	params[0] = spa_format_audio_raw_build(&bld, SPA_PARAM_EnumFormat,
		&SPA_AUDIO_INFO_RAW_INIT(.format = SPA_AUDIO_FORMAT_S16,
					 .channels = CHANNELS,
					 .rate = (uint32_t)t.rate));

	if (pw_stream_connect(t.stream, PW_DIRECTION_OUTPUT, PW_ID_ANY,
			      PW_STREAM_FLAG_AUTOCONNECT |
			      PW_STREAM_FLAG_MAP_BUFFERS |
			      PW_STREAM_FLAG_RT_PROCESS,
			      params, 1) < 0) {
		fprintf(stderr, "biscuit-tick: cannot connect\n");
		return 1;
	}

	pw_main_loop_run(t.loop);

	pw_stream_destroy(t.stream);
	pw_main_loop_destroy(t.loop);
	pw_deinit();
	free(t.pcm);
	return 0;
}
