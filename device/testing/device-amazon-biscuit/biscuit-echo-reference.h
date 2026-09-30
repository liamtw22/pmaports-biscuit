#ifndef BISCUIT_ECHO_REFERENCE_H
#define BISCUIT_ECHO_REFERENCE_H
#include <stdint.h>
#define ER_FRAME 128
#define ER_HISTORY 512
#define ER_RING 131072
struct er_writer;
struct er_reader;
struct er_writer *er_writer_open(const char *path);
void er_writer_reset(struct er_writer *w);
void er_writer_push(struct er_writer *w, const int16_t *stereo, unsigned frames);
void er_writer_close(struct er_writer *w);
struct er_reader *er_reader_open(const char *path);
/* 1: synchronized software reference, 0: caller must use hardware reference.
 * changed is true when the canceller must discard its previous alignment. */
int er_reader_process(struct er_reader *r, const float hardware[ER_FRAME],
                      float filtered[ER_FRAME], int *changed);
/* Offline replay advances in sample time; disable wall-clock retry spacing. */
void er_reader_replay_mode(struct er_reader *r);
void er_reader_close(struct er_reader *r);
void er_reader_stats(struct er_reader *r);
#endif
