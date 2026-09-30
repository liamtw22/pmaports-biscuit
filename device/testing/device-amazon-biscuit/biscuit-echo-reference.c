/* SPDX-License-Identifier: MIT
 * A bounded single-writer rendered-PCM history. Audio threads never wait for
 * each other. The paired FPGA reference supplies timing, not the AEC waveform.
 */
#define _GNU_SOURCE
#include "biscuit-echo-reference.h"
#include <stdatomic.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/file.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <errno.h>
#define ER_MAGIC 0x45524632u
#define RADIUS 96
#define TAPS (2*RADIUS+1)
#define DEFAULT_PATH "/run/biscuit-dsp/echo-reference"
struct shared {
    _Atomic uint32_t magic;
    _Atomic uint32_t version, size, rate;
    _Atomic uint64_t generation, written, updated;
    _Atomic int16_t pcm[ER_RING];
};
struct er_writer { int fd; struct shared *s; };
struct er_reader {
    struct shared *s;
    char *path;
    uint64_t generation, retry_at, search_at;
    int64_t position;
    float hardware[ER_HISTORY], fir[TAPS];
    unsigned fill;
    int locked, active, replay;
    uint64_t used, fallback, acquisitions, lost, max_us, phase_shifts;
    uint64_t confidence_losses, history_losses, stale_losses, generation_changes;
    double confidence;
};
static uint64_t now_ns(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);
    return (uint64_t)t.tv_sec*1000000000ull+t.tv_nsec;
}
struct er_writer *er_writer_open(const char *path) {
    struct er_writer *w=calloc(1,sizeof(*w)); if(!w)return NULL;
    w->fd=open(path?path:DEFAULT_PATH,O_RDWR|O_CREAT|O_CLOEXEC|O_NOFOLLOW,0600);
    if(w->fd<0) {free(w);return NULL;}
    struct stat st;
    if(fstat(w->fd,&st)||!S_ISREG(st.st_mode)||st.st_uid!=geteuid()||flock(w->fd,LOCK_EX|LOCK_NB)||
       ftruncate(w->fd,sizeof(struct shared))) goto fail;
    w->s=mmap(NULL,sizeof(struct shared),PROT_READ|PROT_WRITE,MAP_SHARED,w->fd,0);
    if(w->s==MAP_FAILED) {w->s=NULL;goto fail;}
    atomic_store_explicit(&w->s->magic,0,memory_order_release);
    w->s->version=1;w->s->size=sizeof(struct shared);w->s->rate=48000;
    atomic_store(&w->s->generation,now_ns());atomic_store(&w->s->written,0);
    atomic_store(&w->s->updated,0);
    if(!atomic_is_lock_free(&w->s->pcm[0])||!atomic_is_lock_free(&w->s->written))goto fail;
    atomic_store_explicit(&w->s->magic,ER_MAGIC,memory_order_release);
    return w;
fail:
    if(w->s)munmap(w->s,sizeof(struct shared));
    close(w->fd);free(w);return NULL;
}
void er_writer_reset(struct er_writer *w) {
    if(!w)return;
    atomic_store_explicit(&w->s->magic,0,memory_order_release);
    atomic_store(&w->s->written,0);atomic_store(&w->s->updated,0);
    atomic_store(&w->s->generation,now_ns());
    atomic_store_explicit(&w->s->magic,ER_MAGIC,memory_order_release);
}
void er_writer_push(struct er_writer *w,const int16_t *stereo,unsigned frames) {
    if(!w||frames>ER_RING)return;
    uint64_t n=atomic_load_explicit(&w->s->written,memory_order_relaxed);
    for(unsigned i=0;i<frames;i++)atomic_store_explicit(&w->s->pcm[(n+i)&(ER_RING-1)],stereo[2*i],memory_order_relaxed);
    atomic_store_explicit(&w->s->written,n+frames,memory_order_release);
    atomic_store_explicit(&w->s->updated,now_ns(),memory_order_release);
}
void er_writer_close(struct er_writer *w) {
    if(!w)return;
    atomic_store_explicit(&w->s->magic,0,memory_order_release);
    munmap(w->s,sizeof(struct shared));close(w->fd);free(w);
}
struct er_reader *er_reader_open(const char *path) {
    struct er_reader *r=calloc(1,sizeof(*r)); if(!r)return NULL;
    r->path=strdup(path?path:DEFAULT_PATH);if(!r->path){free(r);return NULL;}
    double sum=0;
    for(int i=0;i<TAPS;i++) {
        int k=i-RADIUS; double fc=7000./48000.;
        double v=k?sin(2*M_PI*fc*k)/(M_PI*k):2*fc;
        v*=.42-.5*cos(2*M_PI*i/(TAPS-1))+.08*cos(4*M_PI*i/(TAPS-1));
        r->fir[i]=v;sum+=v;
    }
    for(int i=0;i<TAPS;i++)r->fir[i]/=sum;
    return r;
}
static int map_reader(struct er_reader *r,uint64_t now) {
    if(r->s)return 1;
    if(now<r->retry_at)return 0;
    r->retry_at=now+1000000000ull;
    int fd=open(r->path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return 0;
    struct stat st;
    if(fstat(fd,&st)||!S_ISREG(st.st_mode)||st.st_size!=sizeof(struct shared)||st.st_uid!=geteuid()){close(fd);return 0;}
    void *s=mmap(NULL,sizeof(struct shared),PROT_READ,MAP_SHARED,fd,0);close(fd);
    if(s==MAP_FAILED)return 0;
    r->s=s;return 1;
}
static inline float sample(struct shared *s,int64_t i) {
    return atomic_load_explicit(&s->pcm[(uint64_t)i&(ER_RING-1)],memory_order_relaxed);
}
/* Mean-subtracted correlation: the pilot may have different gain or polarity. */
static double score(struct er_reader *r,int64_t p,int step,int from) {
    double xy=0,xx=0,yy=0,xs=0,ys=0;int n=0;
    for(int i=from;i<ER_HISTORY;i+=step) {
        double x=r->hardware[i],y=sample(r->s,p+3*i);
        xy+=x*y;xx+=x*x;yy+=y*y;xs+=x;ys+=y;n++;
    }
    xy-=xs*ys/n;xx-=xs*xs/n;yy-=ys*ys/n;
    return xy*xy/fmax(1.,xx*yy);
}
static int64_t acquire(struct er_reader *r,int64_t lo,int64_t hi,double *quality) {
    /* First rank on 32 evenly spaced pilot samples; fully verify the best
     * eight candidates. Work/memory are bounded independently of stream age. */
    double best[8]={0};int64_t where[8]={0};
    for(int64_t p=lo;p<=hi;p++) {
        double q=score(r,p,16,0);
        if(q>best[7]) {int j=7;while(j&&q>best[j-1]){best[j]=best[j-1];where[j]=where[j-1];j--;}
            best[j]=q;where[j]=p;}
    }
    double qbest=0;int64_t pbest=lo;
    for(int i=0;i<8;i++) {
        double q=score(r,where[i],1,0);
        /* Old matching samples must not hide a mismatched current block. */
        double tail=score(r,where[i],1,ER_HISTORY-ER_FRAME);
        q=fmin(q,tail);
        if(q>qbest){qbest=q;pbest=where[i];}
    }
    *quality=sqrt(qbest);return pbest;
}
int er_reader_process(struct er_reader *r,const float hardware[ER_FRAME],float filtered[ER_FRAME],int *changed) {
    *changed=0;if(!r)return 0;
    uint64_t begin=now_ns();int old=r->active,ok=0;
    const char *loss_reason="publisher";
    memmove(r->hardware,r->hardware+ER_FRAME,(ER_HISTORY-ER_FRAME)*sizeof(float));
    memcpy(r->hardware+ER_HISTORY-ER_FRAME,hardware,ER_FRAME*sizeof(float));
    if(r->fill<ER_HISTORY)r->fill+=ER_FRAME;
    if(!map_reader(r,begin))goto done;
    struct shared *s=r->s;
    if(atomic_load_explicit(&s->magic,memory_order_acquire)!=ER_MAGIC||s->version!=1||s->size!=sizeof(*s)||s->rate!=48000)goto lost;
    uint64_t gen=atomic_load_explicit(&s->generation,memory_order_acquire);
    uint64_t count=atomic_load_explicit(&s->written,memory_order_acquire);
    uint64_t updated=atomic_load_explicit(&s->updated,memory_order_acquire);
    if(gen!=r->generation){
        r->generation_changes++;
        r->generation=gen;r->locked=0;r->fill=ER_FRAME;r->search_at=0;
        if(old)*changed=1;
    }
    /* The publisher may run after begin was sampled and before this load.
     * Its newer timestamp is fresh, not an unsigned-wrap stale interval. */
    if(!updated||(begin>updated && begin-updated>250000000ull)) {
        if(r->locked)r->stale_losses++;
        loss_reason="stale";goto lost;
    }
    if(r->fill<ER_HISTORY)goto done;
    int64_t lo=count>ER_RING?(int64_t)(count-ER_RING)+RADIUS:RADIUS;
    int64_t hi=(int64_t)count-3*(ER_HISTORY-1)-RADIUS-1;
    if(hi<lo){if(r->locked)r->history_losses++;loss_reason="history";goto lost;}
    double energy=0;for(int i=ER_HISTORY-ER_FRAME;i<ER_HISTORY;i++)energy+=r->hardware[i]*r->hardware[i];
    int64_t position=r->position+3*ER_FRAME;
    if(energy<ER_FRAME*4.) {
        /* A silent pilot cannot establish a new alignment. Keep an existing
         * clock alignment only while it remains within the fresh history. */
        if(!r->locked||position<lo||position>hi){if(r->locked)r->history_losses++;loss_reason="silent-history";goto lost;}
    } else if(r->locked) {
        double best=0;int64_t pbest=position;
        for(int j=-12;j<=12;j++)if(position+j>=lo&&position+j<=hi){double q=score(r,position+j,1,ER_HISTORY-ER_FRAME);if(q>best){best=q;pbest=position+j;}}
        r->confidence=sqrt(best);
        if(best<.65*.65){r->confidence_losses++;loss_reason="pilot-mismatch";goto lost;}
        /* A few 48 kHz samples of drift do not invalidate a 240 ms echo
         * filter. Keep its learned acoustic path instead of cold-resetting it. */
        if(pbest!=position)r->phase_shifts++;
        position=pbest;
    } else {
        if(begin<r->search_at)goto done;
        r->search_at=begin+(r->replay?0:250000000ull);
        if(lo<hi-48000)lo=hi-48000;
        position=acquire(r,lo,hi,&r->confidence);
        if(r->confidence<.65)goto done;
        r->locked=1;r->acquisitions++;*changed=1;
    }
    for(int i=0;i<ER_FRAME;i++) {
        int64_t p=position+3*(ER_HISTORY-ER_FRAME+i);double value=0;
        for(int j=0;j<TAPS;j++) {
            double x=sample(s,p+j-RADIUS),f=r->fir[j];value+=f*x;
        }
        filtered[i]=(float)value;
    }
    /* A concurrent reset or history overwrite invalidates the whole block. */
    if(atomic_load_explicit(&s->magic,memory_order_acquire)!=ER_MAGIC||
       atomic_load_explicit(&s->generation,memory_order_acquire)!=gen||
       atomic_load_explicit(&s->written,memory_order_acquire)>(uint64_t)(position-RADIUS)+ER_RING)goto lost;
    r->position=position;ok=1;goto done;
lost:
    if(r->locked) {
        r->lost++;
        if(r->lost<=8)fprintf(stderr,"echo-reference-loss: block=%llu reason=%s corr=%.4f\n",
            (unsigned long long)(r->used+r->fallback),loss_reason,r->confidence);
    }
    r->locked=0;r->search_at=0;
done:
    r->active=ok;if(old!=ok)*changed=1;
    if(ok)r->used++;else r->fallback++;
    uint64_t us=(now_ns()-begin)/1000;if(us>r->max_us)r->max_us=us;
    return ok;
}
void er_reader_replay_mode(struct er_reader *r) { if(r)r->replay=1; }
void er_reader_stats(struct er_reader *r) {
    if(r)fprintf(stderr,"echo-reference: software=%llu fallback=%llu acquire=%llu lost=%llu corr=%.4f max_us=%llu phase_shifts=%llu confidence_losses=%llu history_losses=%llu stale_losses=%llu generations=%llu\n",
        (unsigned long long)r->used,(unsigned long long)r->fallback,(unsigned long long)r->acquisitions,
        (unsigned long long)r->lost,r->confidence,(unsigned long long)r->max_us,(unsigned long long)r->phase_shifts,
        (unsigned long long)r->confidence_losses,(unsigned long long)r->history_losses,
        (unsigned long long)r->stale_losses,(unsigned long long)r->generation_changes);
}
void er_reader_close(struct er_reader *r) {
    if(!r)return;
    er_reader_stats(r);if(r->s)munmap(r->s,sizeof(struct shared));free(r->path);free(r);
}
