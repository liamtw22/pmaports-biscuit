/* SPDX-License-Identifier: MIT
 *
 * biscuit-tool: the few things the biscuit TWRP zips need that TWRP's shell
 * cannot do. Built static for TWRP's armv7 userspace, and for the build
 * host so every command can be checked against real device files.
 *
 *   biscuit-tool gpt-kind   DISK                   print the layout kind
 *   biscuit-tool gpt-merge  DISK HEAD.bin TAIL.bin the pmOS table for DISK
 *   biscuit-tool carve-fpga BOOT.img OUT.bin       the FPGA bitstream from a
 *                                                  stock Fire OS boot image
 *   biscuit-tool owner-boot BOOT.img OUT.img SRC=DEST...
 *                                                  append files to the boot
 *                                                  image's initramfs
 *
 * DISK may be the eMMC block device or a whole-disk image. Nothing here
 * writes to DISK: the zip scripts write the files this produces, with
 * busybox dd, and read them back.
 *
 * gpt-merge is the stock amonet v2 table with system_a, system_b, cache and
 * userdata replaced by one userdata from system_a's first sector, followed by
 * 1 MiB stubs named system_a and system_b at the very end of the disk. The
 * stubs are required: amonet v2's Fire OS 6 bootloader looks system_a up by
 * name after loading the boot image and hangs without it.
 */
#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <zlib.h>

#define SECTOR 512
#define ENTRIES 128
#define ENTRY_SIZE 128
#define MERGED_FIRST 294912ULL
#define STUB 2048ULL
#define BOOT_SLOT (16u << 20)
#define MTK_MAGIC 0x58881688u

static void die(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fputs("biscuit-tool: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
    exit(1);
}

static uint32_t le32(const unsigned char *p) {
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t le64(const unsigned char *p) { return le32(p) | (uint64_t)le32(p + 4) << 32; }
static void put32(unsigned char *p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = (unsigned char)(v >> (8 * i)); }
static void put64(unsigned char *p, uint64_t v) { put32(p, (uint32_t)v); put32(p + 4, (uint32_t)(v >> 32)); }

static unsigned char *slurp(const char *path, size_t *len) {
    FILE *f = fopen(path, "rb");
    if (!f) die("%s: %s", path, strerror(errno));
    if (fseeko(f, 0, SEEK_END) != 0) die("%s: cannot seek", path);
    off_t n = ftello(f);
    if (n < 0 || n > (off_t)256 << 20) die("%s: unexpected size", path);
    rewind(f);
    unsigned char *buf = malloc((size_t)n + 1);
    if (!buf || fread(buf, 1, (size_t)n, f) != (size_t)n) die("%s: read failed", path);
    fclose(f);
    *len = (size_t)n;
    return buf;
}

static void spit(const char *path, const void *data, size_t n) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) die("%s: %s", path, strerror(errno));
    const unsigned char *p = data;
    while (n) {
        ssize_t w = write(fd, p, n);
        if (w <= 0) die("%s: write failed", path);
        p += w;
        n -= (size_t)w;
    }
    if (fsync(fd) != 0 || close(fd) != 0) die("%s: sync failed", path);
}

/* ------------------------------------------------------------------ GPT */

/* 0FC63DAF-8483-4772-8E79-3D69D8477DE4, as stored. */
static const unsigned char LINUX_DATA[16] = {0xAF,0x3D,0xC6,0x0F,0x83,0x84,0x72,0x47,
                                             0x8E,0x79,0x3D,0x69,0xD8,0x47,0x7D,0xE4};
struct span { const char *name; uint64_t first, last; };
static const struct span PREFIX[12] = {
    {"kb", 2048, 4095}, {"dkb", 4096, 6143}, {"lk_a", 32768, 34815},
    {"tee1", 49152, 59391}, {"lk_b", 65536, 67583}, {"tee2", 81920, 92159},
    {"expdb", 98304, 118783}, {"misc", 118784, 119808}, {"persist", 131072, 163839},
    {"boot_a", 163840, 196607}, {"boot_b", 196608, 229375}, {"recovery", 229376, 262143},
};
static const struct span STOCK_DATA[3] = {
    {"system_a", 294912, 1867775}, {"system_b", 1867776, 3440639}, {"cache", 3440640, 5046271},
};

struct entry { char name[37]; uint64_t first, last, attrs; const unsigned char *raw; };
struct table {
    uint64_t total, last_usable;
    int count;
    struct entry e[ENTRIES];
    unsigned char head[34 * SECTOR], tail[33 * SECTOR];
};

static void read_disk(const char *path, struct table *t) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) die("%s: %s", path, strerror(errno));
    off_t size = lseek(fd, 0, SEEK_END);
    if (size <= 0 || size % SECTOR) die("%s: size is not a whole number of sectors", path);
    t->total = (uint64_t)size / SECTOR;
    if (pread(fd, t->head, sizeof t->head, 0) != (ssize_t)sizeof t->head ||
        pread(fd, t->tail, sizeof t->tail, (off_t)((t->total - 33) * SECTOR)) != (ssize_t)sizeof t->tail)
        die("%s: short read of the GPT", path);
    close(fd);
}

static const char *check_header(const unsigned char *h, uint64_t my, uint64_t alt, uint64_t array,
                                uint64_t total, const unsigned char *arr) {
    if (memcmp(h, "EFI PART", 8)) return "no GPT signature";
    uint32_t hsize = le32(h + 12);
    if (hsize < 92 || hsize > SECTOR) return "bad header size";
    unsigned char tmp[SECTOR];
    memcpy(tmp, h, hsize);
    put32(tmp + 16, 0);
    if (crc32(0, tmp, hsize) != le32(h + 16)) return "bad header CRC";
    if (le64(h + 24) != my || le64(h + 32) != alt || le64(h + 72) != array) return "bad header geometry";
    if (le64(h + 40) != 34 || le64(h + 48) != total - 34) return "bad usable bounds";
    if (le32(h + 80) != ENTRIES || le32(h + 84) != ENTRY_SIZE) return "bad entry geometry";
    if (crc32(0, arr, ENTRIES * ENTRY_SIZE) != le32(h + 88)) return "bad entry array CRC";
    return NULL;
}

static const char *parse(struct table *t) {
    const unsigned char *mbr = t->head, *ph = t->head + SECTOR, *parr = t->head + 2 * SECTOR;
    const unsigned char *barr = t->tail, *bh = t->tail + 32 * SECTOR;
    static const unsigned char amonet_pmbr[16] = {0x00,0x00,0x02,0x00,0xee,0xff,0xff,0xff,
                                                  0x01,0x00,0x00,0x00,0xff,0xff,0xff,0xff};
    const unsigned char *rec = mbr + 446;
    uint64_t pm_size = t->total - 1 > 0xffffffffULL ? 0xffffffffULL : t->total - 1;
    int standard = rec[0] == 0 && rec[4] == 0xee && le32(rec + 8) == 1 && le32(rec + 12) == pm_size;
    if (mbr[510] != 0x55 || mbr[511] != 0xaa) return "no protective MBR";
    if (!standard && memcmp(rec, amonet_pmbr, 16)) return "unexpected protective MBR";
    for (int i = 16; i < 64; i++) if (rec[i]) return "hybrid MBR";
    const char *err;
    if ((err = check_header(ph, 1, t->total - 1, 2, t->total, parr))) return err;
    if ((err = check_header(bh, t->total - 1, 1, t->total - 33, t->total, barr))) return err;
    if (memcmp(parr, barr, ENTRIES * ENTRY_SIZE)) return "primary and backup entry arrays differ";
    if (memcmp(ph + 56, bh + 56, 16)) return "primary and backup disk GUIDs differ";
    t->last_usable = le64(ph + 48);
    t->count = 0;
    int empty = 0;
    for (int i = 0; i < ENTRIES; i++) {
        const unsigned char *e = parr + i * ENTRY_SIZE;
        int used = 0;
        for (int j = 0; j < 16; j++) used |= e[j];
        if (!used) {
            for (int j = 0; j < ENTRY_SIZE; j++) if (e[j]) return "nonzero unused entry";
            empty = 1;
            continue;
        }
        if (empty) return "hole in the entry array";
        struct entry *x = &t->e[t->count++];
        x->raw = e;
        x->first = le64(e + 32);
        x->last = le64(e + 40);
        x->attrs = le64(e + 48);
        for (int j = 0; j < 36; j++) {
            unsigned c = e[56 + 2 * j] | e[57 + 2 * j] << 8;
            if (c && (c < 32 || c > 126)) return "bad partition name";
            x->name[j] = (char)c;
        }
        x->name[36] = 0;
        if (!x->name[0]) return "empty partition name";
        if (x->first < 34 || x->last < x->first || x->last > t->last_usable) return "partition out of bounds";
    }
    for (int i = 0; i < t->count; i++)
        for (int j = i + 1; j < t->count; j++) {
            if (!strcmp(t->e[i].name, t->e[j].name)) return "duplicate partition name";
            if (!memcmp(t->e[i].raw + 16, t->e[j].raw + 16, 16)) return "duplicate partition GUID";
            if (t->e[i].first <= t->e[j].last && t->e[j].first <= t->e[i].last) return "overlapping partitions";
        }
    return NULL;
}

static int matches(const struct table *t, const struct span *want, int n) {
    if (t->count != n) return 0;
    for (int i = 0; i < n; i++) {
        const struct entry *x = &t->e[i];
        if (strcmp(x->name, want[i].name) || x->first != want[i].first || x->last != want[i].last) return 0;
        if (memcmp(x->raw, LINUX_DATA, 16) || x->attrs) return 0;
    }
    return 1;
}

static const char *kind(const struct table *t) {
    struct span want[16];
    uint64_t last = t->last_usable;
    memcpy(want, PREFIX, sizeof PREFIX);
    memcpy(want + 12, STOCK_DATA, sizeof STOCK_DATA);
    want[15] = (struct span){"userdata", 5046272, last};
    if (matches(t, want, 16)) return "v2_stock_geometry";
    want[12] = (struct span){"userdata", MERGED_FIRST, last - 2 * STUB};
    want[13] = (struct span){"system_a", last - 2 * STUB + 1, last - STUB};
    want[14] = (struct span){"system_b", last - STUB + 1, last};
    if (matches(t, want, 15)) return "v2_merged_geometry";
    want[12] = (struct span){"userdata", MERGED_FIRST, last};
    if (matches(t, want, 13)) return "v2_merged_without_stubs";
    for (int i = 0; i < t->count; i++) {
        size_t l = strlen(t->e[i].name);
        if (l > 2 && !strcmp(t->e[i].name + l - 2, "_x")) return "legacy_or_intermediate_refused";
    }
    return "unknown_geometry";
}

static void load(const char *path, struct table *t) {
    read_disk(path, t);
    const char *err = parse(t);
    if (err) die("%s: invalid partition table: %s", path, err);
}

static int cmd_gpt_kind(char **argv) {
    struct table t;
    load(argv[0], &t);
    printf("%s %llu %llu\n", kind(&t), (unsigned long long)t.total, (unsigned long long)t.last_usable);
    return 0;
}

static int cmd_gpt_merge(char **argv) {
    static struct table t, check;
    load(argv[0], &t);
    if (strcmp(kind(&t), "v2_stock_geometry")) die("%s is '%s', not the stock amonet v2 layout", argv[0], kind(&t));
    unsigned char arr[ENTRIES * ENTRY_SIZE];
    memset(arr, 0, sizeof arr);
    memcpy(arr, t.head + 2 * SECTOR, 12 * ENTRY_SIZE);                        /* p1-p12 as they are */
    const unsigned char *old = t.head + 2 * SECTOR;
    uint64_t last = t.last_usable;
    memcpy(arr + 12 * ENTRY_SIZE, old + 15 * ENTRY_SIZE, ENTRY_SIZE);         /* userdata */
    memcpy(arr + 13 * ENTRY_SIZE, old + 12 * ENTRY_SIZE, ENTRY_SIZE);         /* system_a */
    memcpy(arr + 14 * ENTRY_SIZE, old + 13 * ENTRY_SIZE, ENTRY_SIZE);         /* system_b */
    put64(arr + 12 * ENTRY_SIZE + 32, MERGED_FIRST);
    put64(arr + 12 * ENTRY_SIZE + 40, last - 2 * STUB);
    put64(arr + 13 * ENTRY_SIZE + 32, last - 2 * STUB + 1);
    put64(arr + 13 * ENTRY_SIZE + 40, last - STUB);
    put64(arr + 14 * ENTRY_SIZE + 32, last - STUB + 1);
    put64(arr + 14 * ENTRY_SIZE + 40, last);
    uint32_t acrc = crc32(0, arr, sizeof arr);

    unsigned char head[34 * SECTOR], tail[33 * SECTOR], h[SECTOR];
    memset(head, 0, sizeof head);
    memset(tail, 0, sizeof tail);
    memcpy(head, t.head, SECTOR);                                             /* protective MBR */
    const unsigned char *oh = t.head + SECTOR;
    for (int which = 0; which < 2; which++) {
        memset(h, 0, sizeof h);
        memcpy(h, oh, 92);
        uint64_t my = which ? t.total - 1 : 1, alt = which ? 1 : t.total - 1;
        uint64_t array = which ? t.total - 33 : 2;
        put64(h + 24, my);
        put64(h + 32, alt);
        put64(h + 72, array);
        put32(h + 88, acrc);
        put32(h + 16, 0);
        put32(h + 16, crc32(0, h, 92));
        if (which) {
            memcpy(tail, arr, sizeof arr);
            memcpy(tail + 32 * SECTOR, h, SECTOR);
        } else {
            memcpy(head + SECTOR, h, SECTOR);
            memcpy(head + 2 * SECTOR, arr, sizeof arr);
        }
    }
    /* Check what we built with the same parser, before anything is written. */
    check.total = t.total;
    memcpy(check.head, head, sizeof head);
    memcpy(check.tail, tail, sizeof tail);
    const char *err = parse(&check);
    if (err) die("internal error: the computed table is invalid: %s", err);
    if (strcmp(kind(&check), "v2_merged_geometry")) die("internal error: the computed table is '%s'", kind(&check));
    if (memcmp(check.head + 2 * SECTOR, t.head + 2 * SECTOR, 12 * ENTRY_SIZE)) die("internal error: p1-p12 changed");
    spit(argv[1], head, sizeof head);
    spit(argv[2], tail, sizeof tail);
    printf("v2_merged_geometry userdata %llu..%llu system_a %llu..%llu system_b %llu..%llu\n",
           (unsigned long long)MERGED_FIRST, (unsigned long long)(last - 2 * STUB),
           (unsigned long long)(last - 2 * STUB + 1), (unsigned long long)(last - STUB),
           (unsigned long long)(last - STUB + 1), (unsigned long long)last);
    return 0;
}

/* --------------------------------------------------------- boot images */

struct bootimg { unsigned char *data; size_t len; uint32_t ksize, rsize, ssize, page, koff, roff, soff; };

static void parse_boot(const char *path, struct bootimg *b) {
    b->data = slurp(path, &b->len);
    if (b->len < 2048 || memcmp(b->data, "ANDROID!", 8)) die("%s is not an Android boot image", path);
    b->ksize = le32(b->data + 8);
    b->rsize = le32(b->data + 16);
    b->ssize = le32(b->data + 24);
    b->page = le32(b->data + 36);
    if (b->page != 2048) die("%s: page size %u, not 2048", path, b->page);
    b->koff = b->page;
    b->roff = b->koff + (b->ksize + b->page - 1) / b->page * b->page;
    b->soff = b->roff + (b->rsize + b->page - 1) / b->page * b->page;
    if ((uint64_t)b->soff + b->ssize > b->len) die("%s is truncated", path);
}

/* Inflate one gzip member starting at src, up to max bytes; *used is how many
 * input bytes it consumed. Returns NULL if src is not a valid gzip member. */
static unsigned char *gunzip(const unsigned char *src, size_t avail, size_t max, size_t *outlen, size_t *used) {
    z_stream z;
    memset(&z, 0, sizeof z);
    if (inflateInit2(&z, 16 + MAX_WBITS) != Z_OK) return NULL;
    unsigned char *out = malloc(max);
    if (!out) die("out of memory");
    z.next_in = (unsigned char *)src;
    z.avail_in = (uInt)avail;
    z.next_out = out;
    z.avail_out = (uInt)max;
    int r = inflate(&z, Z_FINISH);
    if (r != Z_STREAM_END) {
        inflateEnd(&z);
        free(out);
        return NULL;
    }
    *outlen = z.total_out;
    if (used) *used = z.total_in;
    inflateEnd(&z);
    return out;
}

static const unsigned char FPGA_SIG[9] = {0xff, 0x00, 'L', 'a', 't', 't', 'i', 'c', 'e'};
#define FPGA_PART "Part: iCE40UL1K-SWG16"
#define FPGA_LENGTH 30964

static int cmd_carve_fpga(char **argv) {
    struct bootimg b;
    parse_boot(argv[0], &b);
    const unsigned char *k = b.data + b.koff;
    for (size_t at = 0; at + 3 < b.ksize; at++) {
        if (k[at] != 0x1f || k[at + 1] != 0x8b || k[at + 2] != 0x08) continue;
        size_t n, used;
        unsigned char *img = gunzip(k + at, b.ksize - at, 48u << 20, &n, &used);
        if (!img) continue;
        for (size_t i = 0; i + sizeof FPGA_SIG < n; i++) {
            if (memcmp(img + i, FPGA_SIG, sizeof FPGA_SIG) || img[i + 9] != 0) continue;
            size_t window = n - i < 256 ? n - i : 256;
            if (!memmem(img + i, window, FPGA_PART, strlen(FPGA_PART))) continue;
            if (n - i < FPGA_LENGTH) die("the bitstream is cut short in %s", argv[0]);
            spit(argv[1], img + i, FPGA_LENGTH);
            printf("carved %d bytes at kernel offset %zu (inflated offset %zu)\n", FPGA_LENGTH, at, i);
            return 0;
        }
        free(img);
    }
    die("no FPGA bitstream in %s", argv[0]);
    return 1;
}

/* newc cpio ------------------------------------------------------------- */

struct centry { char *name; uint32_t mode; const unsigned char *data; uint32_t size; };

static uint32_t hex8(const unsigned char *p) {
    char s[9];
    memcpy(s, p, 8);
    s[8] = 0;
    return (uint32_t)strtoul(s, NULL, 16);
}

static int parse_newc(const unsigned char *c, size_t len, struct centry *out, int max) {
    size_t pos = 0;
    int n = 0;
    while (pos + 110 <= len) {
        if (memcmp(c + pos, "070701", 6)) die("the initramfs is not a newc archive");
        uint32_t mode = hex8(c + pos + 14), size = hex8(c + pos + 54), nlen = hex8(c + pos + 94);
        if (pos + 110 + nlen > len) die("the initramfs is truncated");
        const char *name = (const char *)c + pos + 110;
        size_t data = pos + 110 + nlen;
        data += (4 - data % 4) % 4;
        if (!strncmp(name, "TRAILER!!!", 10)) break;
        if (n >= max) die("the initramfs has too many entries");
        out[n].name = strndup(name, nlen ? nlen - 1 : 0);
        out[n].mode = mode;
        out[n].data = c + data;
        out[n].size = size;
        n++;
        pos = data + size;
        pos += (4 - pos % 4) % 4;
    }
    return n;
}

static const struct centry *find(const struct centry *e, int n, const char *name) {
    for (int i = 0; i < n; i++) if (!strcmp(e[i].name, name)) return &e[i];
    return NULL;
}

/* Follow the archive's own relative symlinks along path, so a file meant for
 * lib/firmware lands where lib points (usr/lib, on postmarketOS). */
/* snprintf that refuses to truncate: a cut-off path would silently point
 * somewhere else. */
#define fit(buf, size, ...) do { int n_ = snprintf(buf, size, __VA_ARGS__); if (n_ < 0 || (size_t)n_ >= (size_t)(size)) die("path too long"); } while (0)

static void resolve(const struct centry *e, int n, const char *path, char *out, size_t outlen) {
    char todo[1024], done[1024] = "";
    fit(todo, sizeof todo, "%s", path);
    for (int hops = 0; hops < 40 && todo[0]; hops++) {
        char *slash = strchr(todo, '/');
        char part[256];
        size_t pl = slash ? (size_t)(slash - todo) : strlen(todo);
        memcpy(part, todo, pl);
        part[pl] = 0;
        char cand[1024];
        fit(cand, sizeof cand, "%s%s%s", done, done[0] ? "/" : "", part);
        const struct centry *x = find(e, n, cand);
        char rest[1024];
        fit(rest, sizeof rest, "%s", slash ? slash + 1 : "");
        if (x && (x->mode & 0170000) == 0120000) {
            char target[512];
            fit(target, sizeof target, "%.*s", (int)x->size, (const char *)x->data);
            if (target[0] == '/') {
                done[0] = 0;
                fit(todo, sizeof todo, "%s%s%s", target + 1, rest[0] ? "/" : "", rest);
            } else {
                fit(todo, sizeof todo, "%s%s%s", target, rest[0] ? "/" : "", rest);
            }
            continue;
        }
        fit(done, sizeof done, "%s", cand);
        fit(todo, sizeof todo, "%s", rest);
    }
    if (todo[0]) die("symlink loop resolving %s", path);
    fit(out, outlen, "%s", done);
}

static unsigned char *cp;
static size_t cplen, cpcap;
static void emit(const void *p, size_t n) {
    if (cplen + n + 4 > cpcap) {
        cpcap = (cplen + n) * 2 + 4096;
        cp = realloc(cp, cpcap);
        if (!cp) die("out of memory");
    }
    memcpy(cp + cplen, p, n);
    cplen += n;
}
static void pad4(void) { static const unsigned char z[4]; emit(z, (4 - cplen % 4) % 4); }
static void newc_entry(const char *name, uint32_t mode, const unsigned char *data, uint32_t size) {
    static uint32_t ino = 1000;
    char hdr[111];
    snprintf(hdr, sizeof hdr, "070701%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X",
             ino++, mode, 0, 0, 1, 0, size, 0, 0, 0, 0, (uint32_t)strlen(name) + 1, 0);
    emit(hdr, 110);
    emit(name, strlen(name) + 1);
    pad4();
    if (size) emit(data, size);
    pad4();
}

/* SHA-1, for the boot image header's id field ----------------------------- */

struct sha1 { uint32_t h[5]; uint64_t n; unsigned char buf[64]; size_t used; };
#define ROL(x, s) (((x) << (s)) | ((x) >> (32 - (s))))
static void sha1_block(struct sha1 *s, const unsigned char *p) {
    uint32_t w[80], a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3], e = s->h[4];
    for (int i = 0; i < 16; i++) w[i] = (uint32_t)p[4 * i] << 24 | p[4 * i + 1] << 16 | p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 80; i++) w[i] = ROL(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
    for (int i = 0; i < 80; i++) {
        uint32_t f, k;
        if (i < 20) { f = (b & c) | (~b & d); k = 0x5A827999; }
        else if (i < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1; }
        else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDC; }
        else { f = b ^ c ^ d; k = 0xCA62C1D6; }
        uint32_t t = ROL(a, 5) + f + e + k + w[i];
        e = d; d = c; c = ROL(b, 30); b = a; a = t;
    }
    s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d; s->h[4] += e;
}
static void sha1_init(struct sha1 *s) {
    static const uint32_t iv[5] = {0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0};
    memcpy(s->h, iv, sizeof iv);
    s->n = 0;
    s->used = 0;
}
static void sha1_update(struct sha1 *s, const unsigned char *p, size_t n) {
    s->n += n;
    while (n) {
        size_t take = 64 - s->used < n ? 64 - s->used : n;
        memcpy(s->buf + s->used, p, take);
        s->used += take;
        p += take;
        n -= take;
        if (s->used == 64) { sha1_block(s, s->buf); s->used = 0; }
    }
}
static void sha1_final(struct sha1 *s, unsigned char out[20]) {
    uint64_t bits = s->n * 8;
    unsigned char pad = 0x80, zero = 0, len[8];
    sha1_update(s, &pad, 1);
    while (s->used != 56) sha1_update(s, &zero, 1);
    for (int i = 0; i < 8; i++) len[i] = (unsigned char)(bits >> (56 - 8 * i));
    sha1_update(s, len, 8);
    for (int i = 0; i < 5; i++) {
        out[4 * i] = (unsigned char)(s->h[i] >> 24);
        out[4 * i + 1] = (unsigned char)(s->h[i] >> 16);
        out[4 * i + 2] = (unsigned char)(s->h[i] >> 8);
        out[4 * i + 3] = (unsigned char)s->h[i];
    }
}

/* owner-boot ------------------------------------------------------------ */

static int cmd_owner_boot(int argc, char **argv) {
    struct bootimg b;
    parse_boot(argv[0], &b);
    if (le32(b.data + b.koff) != MTK_MAGIC) die("the kernel has no MediaTek header; the image would bootloop");
    size_t ilen, used;
    unsigned char *initramfs = gunzip(b.data + b.roff, b.rsize, 32u << 20, &ilen, &used);
    if (!initramfs) die("cannot inflate the initramfs");
    static struct centry base[8192];
    int nbase = parse_newc(initramfs, ilen, base, 8192);

    char dirs[64][512];
    int ndirs = 0;
    struct { char dest[512]; unsigned char *data; size_t len; } files[16];
    int nfiles = 0;
    for (int a = 2; a < argc; a++) {
        char *eq = strchr(argv[a], '=');
        if (!eq || nfiles == 16) die("arguments are SRC=DEST, at most 16");
        *eq = 0;
        char target[512];
        resolve(base, nbase, eq + 1, target, sizeof target);
        if (find(base, nbase, target)) die("the initramfs already has %s", target);
        for (char *s = strchr(target, '/'); s; s = strchr(s + 1, '/')) {
            char d[512];
            snprintf(d, sizeof d, "%.*s", (int)(s - target), target);
            const struct centry *x = find(base, nbase, d);
            if (x) {
                if ((x->mode & 0170000) != 0040000) die("%s in the initramfs is not a directory", d);
                continue;
            }
            int seen = 0;
            for (int i = 0; i < ndirs; i++) seen |= !strcmp(dirs[i], d);
            if (!seen) snprintf(dirs[ndirs++], sizeof dirs[0], "%s", d);
        }
        snprintf(files[nfiles].dest, sizeof files[0].dest, "%s", target);
        files[nfiles].data = slurp(argv[a], &files[nfiles].len);
        nfiles++;
    }
    /* The appended archive never contains an entry whose type differs from
     * what the first archive has there: the kernel's unpacker deletes the old
     * entry first, and a plain `lib` directory once replaced pmOS's
     * lib -> usr/lib symlink, taking the musl loader with it. */
    for (int i = 0; i < ndirs; i++) newc_entry(dirs[i], 040755, NULL, 0);
    for (int i = 0; i < nfiles; i++) newc_entry(files[i].dest, 0100644, files[i].data, (uint32_t)files[i].len);
    newc_entry("TRAILER!!!", 0, NULL, 0);

    size_t rpad = (4 - b.rsize % 4) % 4;
    size_t nrsize = b.rsize + rpad + cplen;
    unsigned char *ramdisk = calloc(1, nrsize);
    if (!ramdisk) die("out of memory");
    memcpy(ramdisk, b.data + b.roff, b.rsize);
    memcpy(ramdisk + b.rsize + rpad, cp, cplen);

    unsigned char header[2048];
    memcpy(header, b.data, 2048);
    put32(header + 16, (uint32_t)nrsize);
    /* amonet v2's bootloader starts the kernel in the mode bootopt's third
     * field names, and this kernel is arm64. Same length, so nothing moves. */
    char *cmd = (char *)header + 64;
    char *opt = memmem(cmd, 512, "bootopt=", 8);
    if (!opt || opt + 0x12 + 2 > cmd + 512) die("the boot image command line has no bootopt");
    if (!memcmp(opt + 0x12, "32", 2)) memcpy(opt + 0x12, "64", 2);
    else if (memcmp(opt + 0x12, "64", 2)) die("unrecognised bootopt in the boot image");

    struct sha1 s;
    unsigned char digest[20], len4[4];
    sha1_init(&s);
    const unsigned char *parts[3] = {b.data + b.koff, ramdisk, b.data + b.soff};
    size_t sizes[3] = {b.ksize, nrsize, b.ssize};
    for (int i = 0; i < 3; i++) {
        sha1_update(&s, parts[i], sizes[i]);
        put32(len4, (uint32_t)sizes[i]);
        sha1_update(&s, len4, 4);
    }
    sha1_final(&s, digest);
    memset(header + 576, 0, 32);
    memcpy(header + 576, digest, 20);

    size_t kp = (b.ksize + 2047) / 2048 * 2048, rp = (nrsize + 2047) / 2048 * 2048;
    size_t sp = b.ssize ? (b.ssize + 2047) / 2048 * 2048 : 0;
    size_t total = 2048 + kp + rp + sp;
    if (total > BOOT_SLOT) die("the boot image would be %zu bytes; a slot holds %u", total, BOOT_SLOT);
    unsigned char *out = calloc(1, total);
    if (!out) die("out of memory");
    memcpy(out, header, 2048);
    memcpy(out + 2048, b.data + b.koff, b.ksize);
    memcpy(out + 2048 + kp, ramdisk, nrsize);
    if (b.ssize) memcpy(out + 2048 + kp + rp, b.data + b.soff, b.ssize);
    spit(argv[1], out, total);
    printf("wrote %s: %zu bytes, %d file(s) and %d dir(s) appended to the initramfs\n", argv[1], total, nfiles, ndirs);
    return 0;
}

int main(int argc, char **argv) {
    if (argc >= 3 && !strcmp(argv[1], "gpt-kind")) return cmd_gpt_kind(argv + 2);
    if (argc == 5 && !strcmp(argv[1], "gpt-merge")) return cmd_gpt_merge(argv + 2);
    if (argc == 4 && !strcmp(argv[1], "carve-fpga")) return cmd_carve_fpga(argv + 2);
    if (argc >= 5 && !strcmp(argv[1], "owner-boot")) return cmd_owner_boot(argc - 2, argv + 2);
    fprintf(stderr,
            "usage: biscuit-tool gpt-kind DISK\n"
            "       biscuit-tool gpt-merge DISK HEAD.bin TAIL.bin\n"
            "       biscuit-tool carve-fpga BOOT.img OUT.bin\n"
            "       biscuit-tool owner-boot BOOT.img OUT.img SRC=DEST...\n");
    return 2;
}
