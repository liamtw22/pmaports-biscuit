#!/usr/bin/env python3
"""Install postmarketOS on an amonet-v2 Echo Dot 2 (biscuit), after a verified backup.

Three steps, in order, each refusing to run unless the one before it succeeded:

  1. export       (Echo in TWRP)     Collect this Echo's own firmware and sound
                                     files from its Fire OS system partition and
                                     its stock kernel, verified by hash.
  2. repartition  (Echo in TWRP)     Merge system_a, system_b, cache and userdata
                                     into one userdata for pmOS, zero it, and
                                     make the settings store on persist, holding
                                     the files from step 1. DESTROYS FIRE OS.
  3. flash        (Echo in fastboot) Write pmOS to both boot slots and userdata.

    python biscuit_install.py export      --backup DIR
    python biscuit_install.py repartition --backup DIR
    python biscuit_install.py flash       --backup DIR --boot boot.img --disk pmos.img

DIR is the folder biscuit_backup.py made. Every step checks that the Echo in
front of it is the one that folder belongs to, and repartition checks that the
partition table is still exactly the one that was backed up.

The new partition table is computed here, from the backed-up one, and checked
by layout_check before a byte is written. It keeps partitions 1-12 exactly as
they are (bootloaders, both boot slots, recovery, persist) and replaces 13-16
with one userdata over the same space. Nothing else on the eMMC is touched.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import layout_check  # noqa: E402

SECTOR = 512
MERGED_FIRST = 294912            # system_a's first sector: the merged userdata starts here
STUB_SECTORS = 2048              # 1 MiB system_a and system_b stubs at the end of the disk
STORE_LABEL = "biscuit-opt"      # biscuit-persist mounts /opt by this label
STORE_ASSETS = "persist/biscuit/assets"
MANIFEST = os.path.join(HERE, "biscuit-profile-assets.json")
BOOT_SLOT_BYTES = 16 << 20
MTK_MAGIC = 0x58881688
# TWRP's own dd is a 32-bit toybox without large-file support: it cannot seek
# past 2 GiB ("Invalid argument"), and the backup GPT and most of userdata lie
# beyond that. So the installer brings Alpine's static armv7 busybox (musl,
# 64-bit offsets) and uses it for every positioned read and write.
BUSYBOX_LOCAL = os.path.join(HERE, os.pardir, "zip", "bin", "busybox")
BUSYBOX = "/tmp/busybox"          # must be named busybox: it picks its applet by name
DD = BUSYBOX + " dd"
# Where Fire OS keeps the files the manifest names. Fire OS 6 is system-as-root,
# so its system partition holds a root with system/ inside; Fire OS 5's is the
# system directory itself. Firmware moved from etc/ to vendor/ between them.
SYSTEM_DIRS = ("system/vendor/firmware", "system/vendor/etc/audio-algorithms",
               "system/etc/firmware", "system/etc/audio-algorithms",
               "vendor/firmware", "vendor/etc/audio-algorithms",
               "etc/firmware", "etc/audio-algorithms")
FPGA_SIGNATURE = b"\xff\x00Lattice\x00"
FPGA_PART = b"Part: iCE40UL1K-SWG16"
FPGA_LENGTH = 30964


class Fail(Exception):
    pass


def say(text=""):
    print(text, flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tool(name, explicit=None):
    found = explicit or shutil.which(name)
    if not found:
        raise Fail("%s not found; install Android platform-tools" % name)
    return found


# ---------------------------------------------------------------- the backup

class Backup:
    def __init__(self, path):
        self.path = path
        try:
            with open(os.path.join(path, "manifest.json")) as f:
                self.m = json.load(f)
        except (OSError, ValueError) as e:
            raise Fail("%s is not a biscuit_backup.py folder: %s" % (path, e))
        self.serial = self.m["serial"]

    def file(self, *parts):
        return os.path.join(self.path, *parts)

    def verify(self):
        """Re-hash every file the manifest names. A backup is only a backup if
        it still matches what was read from the Echo."""
        say("Verifying the backup in %s ..." % self.path)
        for name, d in self.m["dumps"].items():
            if sha256_file(self.file(name + ".img")) != d["sha256"]:
                raise Fail("backup file %s.img no longer matches its manifest" % name)
        for p in self.m["gpt"]["partitions"]:
            if sha256_file(self.file("parts", p["name"] + ".img")) != p["sha256"]:
                raise Fail("backup file parts/%s.img no longer matches its manifest" % p["name"])
        say("  all %d files match" % (len(self.m["dumps"]) + len(self.m["gpt"]["partitions"])))


# ------------------------------------------------------------------ the Echo

class Recovery:
    def __init__(self, adb, serial):
        self.adb, self.serial = adb, serial

    def run(self, *args, check=True, timeout=600, stdin=None):
        # A child must never read our stdin: it would swallow the confirmation.
        r = subprocess.run([self.adb, "-s", self.serial, *args], capture_output=True,
                           timeout=timeout, **({"input": stdin} if stdin is not None
                                               else {"stdin": subprocess.DEVNULL}))
        out = r.stdout.decode(errors="replace").replace("\r\n", "\n")
        if check and r.returncode != 0:
            raise Fail("adb %s failed: %s" % (" ".join(args)[:120],
                       (r.stderr.decode(errors="replace") or out).strip()))
        return out

    def sh(self, command, check=True, timeout=600):
        """Run a shell command and FAIL unless it printed our end marker, so a
        command that dies half way is never mistaken for one that succeeded."""
        out = self.run("shell", command + " && echo __ok__", check=check, timeout=timeout)
        if check and not out.rstrip().endswith("__ok__"):
            raise Fail("on the Echo, '%s' failed:\n%s" % (command[:120], out.strip()))
        return out.rstrip()[:-len("__ok__")].rstrip("\n") if out.rstrip().endswith("__ok__") else out

    def read(self, offset, length):
        """Raw bytes from the eMMC user area, by exec-out (binary-safe)."""
        r = subprocess.run([self.adb, "-s", self.serial, "exec-out",
                            DD + " if=/dev/block/mmcblk0 bs=512 skip=%d count=%d 2>/dev/null"
                            % (offset // SECTOR, length // SECTOR)],
                           capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
        if r.returncode != 0 or len(r.stdout) != length:
            raise Fail("could not read %d bytes at %d from the Echo" % (length, offset))
        return r.stdout


def in_recovery(adb, serial):
    out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=30,
                         stdin=subprocess.DEVNULL).stdout
    states = dict(l.split("\t")[:2] for l in out.splitlines()[1:] if "\t" in l)
    if states.get(serial) != "recovery":
        raise Fail("the Echo %s is not in TWRP (state: %s)" % (serial, states.get(serial, "not attached")))
    echo = Recovery(adb, serial)
    if echo.sh("getprop ro.product.device") != "biscuit" or echo.sh("getprop ro.boot.serialno") != serial:
        raise Fail("the device in recovery is not the Echo Dot 2 %s" % serial)
    if not echo.sh("getprop ro.twrp.version"):
        raise Fail("the Echo %s is in a recovery that is not TWRP" % serial)
    echo.run("push", BUSYBOX_LOCAL, BUSYBOX)
    echo.sh("chmod 755 %s" % BUSYBOX)
    if echo.sh("%s sha256sum %s" % (BUSYBOX, BUSYBOX)).split()[0] != sha256_file(BUSYBOX_LOCAL):
        raise Fail("the helper busybox did not arrive intact")
    return echo


def emmc_mounts(echo):
    """(device, mountpoint) for everything mounted from the eMMC. TWRP lists
    devices in /proc/mounts by their by-name path, not as mmcblk0pN, so each
    is resolved on the Echo before deciding."""
    found = []
    for line in echo.sh("cat /proc/mounts").splitlines():
        f = line.split()
        if len(f) >= 2 and f[0].startswith("/dev/block/"):
            real = echo.sh("readlink -f %s" % f[0]).strip()
            if real.startswith("/dev/block/mmcblk0"):
                found.append((real, f[1]))
    return found


def unmount_all(echo):
    for _, point in sorted(emmc_mounts(echo), key=lambda m: -len(m[1])):
        echo.run("shell", "umount %s" % point, check=False)
    left = emmc_mounts(echo)
    if left:
        raise Fail("could not unmount: %s" % "; ".join("%s on %s" % m for m in left))


# ------------------------------------------------------------- the partition table

class GptImage:
    """The GPT metadata of a disk: its first 34 and last 33 sectors, at the
    disk's true size. layout_check's CaptureReader refuses any read outside
    them, so nothing is ever silently read as zeros - and no disk-sized file is
    made, which on Windows is not sparse and fills the drive."""
    def __init__(self, total_sectors, head, tail):
        if len(head) != 34 * SECTOR or len(tail) != 33 * SECTOR:
            raise Fail("GPT capture has the wrong length")
        self.reader = layout_check.CaptureReader(
            total_sectors * SECTOR, [(0, head), ((total_sectors - 33) * SECTOR, tail)])

    def check(self):
        try:
            g = layout_check.validate_gpt(self.reader, allow_amonet_pmbr=True)
        except layout_check.Rejected as e:
            raise Fail("the partition table is not valid: %s" % e)
        return g, layout_check.outer_kind(g)

    def close(self):
        pass


def gpt_regions(disk_image_path, total):
    with open(disk_image_path, "rb") as f:
        head = f.read(34 * SECTOR)
        f.seek((total - 33) * SECTOR)
        tail = f.read(33 * SECTOR)
    return head, tail


def merged_table(head, tail, total):
    """The stock v2 table with entries 13-16 replaced by one userdata, followed
    by two 1 MiB stubs named system_a and system_b at the very end of the disk.

    Entry 13 keeps the old userdata's type, unique GUID, attributes and name;
    it starts where system_a began. The stubs keep system_a's and system_b's
    own GUIDs. The disk GUID, the protective MBR and entries 1-12 are carried
    over byte for byte.

    The stubs are not optional. amonet v2's Fire OS 6 bootloader looks up
    system_a by name after loading the boot image, even with verity disabled,
    and without it hangs until the watchdog resets the Echo, every 30 s,
    before any kernel runs (Device 2, 2026-09-28, by UART). It only needs the
    entry to exist; it never reads the stub."""
    mbr, hdr, arr = bytearray(head[:512]), bytearray(head[512:1024]), bytearray(head[1024:1024 + 128 * 128])
    count, esize = struct.unpack_from("<II", hdr, 80)
    if (count, esize) != (128, 128):
        raise Fail("unexpected GPT entry geometry %d x %d" % (count, esize))
    entries = [arr[i * 128:(i + 1) * 128] for i in range(128)]
    names = [e[56:128].decode("utf-16-le").rstrip("\0") for e in entries[:16]]
    if names[12:16] != ["system_a", "system_b", "cache", "userdata"]:
        raise Fail("entries 13-16 are %s, not the stock v2 system_a..userdata" % names[12:16])
    userdata, system_a, system_b = (bytearray(entries[i]) for i in (15, 12, 13))
    last_usable = struct.unpack_from("<Q", hdr, 48)[0]
    struct.pack_into("<QQ", userdata, 32, MERGED_FIRST, last_usable - 2 * STUB_SECTORS)
    struct.pack_into("<QQ", system_a, 32, last_usable - 2 * STUB_SECTORS + 1, last_usable - STUB_SECTORS)
    struct.pack_into("<QQ", system_b, 32, last_usable - STUB_SECTORS + 1, last_usable)
    new_arr = (b"".join(bytes(e) for e in entries[:12]) + bytes(userdata) + bytes(system_a)
               + bytes(system_b) + b"\0" * (128 * 113))
    array_crc = zlib.crc32(new_arr) & 0xFFFFFFFF

    def header(my, alt, array_lba):
        h = bytearray(hdr[:92])
        struct.pack_into("<QQ", h, 24, my, alt)
        struct.pack_into("<Q", h, 72, array_lba)
        struct.pack_into("<I", h, 88, array_crc)
        struct.pack_into("<I", h, 16, 0)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
        return bytes(h) + b"\0" * (SECTOR - 92)

    new_head = bytes(mbr) + header(1, total - 1, 2) + new_arr
    new_tail = new_arr + header(total - 1, 1, total - 33)
    assert len(new_head) == 34 * SECTOR and len(new_tail) == 33 * SECTOR
    return new_head, new_tail


# ------------------------------------------------------------------ step 1: export

def load_manifest():
    with open(MANIFEST) as f:
        return json.load(f)["profiles"]


def variants(item):
    return item.get("variants") or [{"bytes": item["bytes"], "sha256": item["sha256"]}]


def carve_fpga(boot_image_path):
    """The FPGA bitstream is not a file on Fire OS: the stock kernel carries it.
    Find each gzip stream in the boot image, inflate it, and cut the Lattice
    image out by its signature."""
    data = open(boot_image_path, "rb").read()
    at = 0
    while True:
        at = data.find(b"\x1f\x8b\x08", at)
        if at < 0:
            return None
        try:
            inflated = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data[at:], 64 << 20)
        except zlib.error:
            at += 3
            continue
        i = inflated.find(FPGA_SIGNATURE)
        if i >= 0 and FPGA_PART in inflated[i:i + 256]:
            cut = inflated[i:i + FPGA_LENGTH]
            if len(cut) == FPGA_LENGTH:
                return cut
        at += 3


def cmd_export(a, adb):
    backup = Backup(a.backup)
    backup.verify()
    echo = in_recovery(adb, backup.serial)
    profiles = load_manifest()
    wanted = {}
    for profile, spec in profiles.items():
        for item in spec.get("files", []):
            for v in variants(item):
                wanted[v["sha256"]] = (profile, item["name"], v["bytes"])
    sizes = {b for _, _, b in wanted.values()}

    staging = backup.file("export-tree")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    unmount_all(echo)
    found = {}
    for slot in ("system_a", "system_b"):
        mnt = "/tmp/biscuit-%s" % slot
        echo.sh("mkdir -p %s && mount -o ro -t ext4 /dev/block/by-name/%s %s" % (mnt, slot, mnt))
        try:
            for rel in SYSTEM_DIRS:
                listing = echo.run("shell", "cd %s/%s 2>/dev/null && find . -type f -size -8192k "
                                   "-exec stat -c '%%s %%n' {} +" % (mnt, rel), check=False)
                for line in listing.splitlines():
                    size, _, path = line.partition(" ")
                    if not size.isdigit() or int(size) not in sizes:
                        continue
                    remote = "%s/%s/%s" % (mnt, rel, path[2:])
                    local = os.path.join(staging, slot, rel, path[2:])
                    os.makedirs(os.path.dirname(local), exist_ok=True)
                    echo.run("pull", remote, local, timeout=300)
                    digest = sha256_file(local)
                    if digest in wanted:
                        found.setdefault(digest, (local, "%s:/%s/%s" % (slot, rel, path[2:])))
        finally:
            echo.run("shell", "umount %s" % mnt, check=False)
    for slot in ("boot_a", "boot_b"):
        cut = carve_fpga(backup.file("parts", slot + ".img"))
        if cut:
            local = os.path.join(staging, slot + "-fpga.bin")
            open(local, "wb").write(cut)
            digest = hashlib.sha256(cut).hexdigest()
            if digest in wanted:
                found.setdefault(digest, (local, "%s: carved from the stock kernel" % slot))

    out = backup.file("assets")
    shutil.rmtree(out, ignore_errors=True)
    report = {"serial": backup.serial, "profiles": {}}
    for profile, spec in sorted(profiles.items()):
        have, missing = [], []
        for item in spec.get("files", []):
            hit = next((v for v in variants(item) if v["sha256"] in found), None)
            if hit is None:
                (missing if item.get("required", True) else []).append(item["name"])
                continue
            src, origin = found[hit["sha256"]]
            dst = os.path.join(out, profile, item["name"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            if sha256_file(dst) != hit["sha256"]:
                raise Fail("copy of %s did not verify" % item["name"])
            have.append({"name": item["name"], "bytes": hit["bytes"], "sha256": hit["sha256"],
                         "from": origin})
        complete = not missing
        report["profiles"][profile] = {"complete": complete, "files": have, "missing": missing}
        say("  %-9s %s  (%d file(s)%s)" % (profile, "COMPLETE" if complete else "incomplete",
            len(have), "; missing " + ", ".join(missing) if missing else ""))
        if not complete:
            shutil.rmtree(os.path.join(out, profile), ignore_errors=True)
    with open(os.path.join(backup.path, "assets.json"), "w", newline="\n") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    if not report["profiles"].get("firmware", {}).get("complete"):
        raise Fail("this Echo's firmware set is incomplete; pmOS would have no Wi-Fi. "
                   "Do not repartition.")
    say("Export complete: %s" % out)


# ------------------------------------------------------------ step 2: repartition

def cmd_repartition(a, adb):
    backup = Backup(a.backup)
    backup.verify()
    report = os.path.join(backup.path, "assets.json")
    if not os.path.exists(report) or not json.load(open(report))["profiles"]["firmware"]["complete"]:
        raise Fail("run 'export' first: the firmware must be saved before Fire OS is erased")
    echo = in_recovery(adb, backup.serial)
    total = backup.m["dumps"]["mmcblk0"]["bytes"] // SECTOR
    if int(echo.sh("blockdev --getsize64 /dev/block/mmcblk0")) != total * SECTOR:
        raise Fail("the eMMC is not the size that was backed up")

    live = GptImage(total, echo.read(0, 34 * SECTOR), echo.read((total - 33) * SECTOR, 33 * SECTOR))
    saved = GptImage(total, *gpt_regions(backup.file("mmcblk0.img"), total))
    try:
        g_live, kind = live.check()
        g_saved, _ = saved.check()
        if kind == "v2_merged_geometry":
            say("The partition table is already merged; continuing with the remaining steps.")
            new_head = new_tail = None
        elif kind not in ("v2_stock_geometry", "v2_merged_without_stubs"):
            raise Fail("the partition table is '%s', not stock amonet v2; refusing" % kind)
        elif kind == "v2_stock_geometry" and g_live["metadata_sha256"] != g_saved["metadata_sha256"]:
            raise Fail("the partition table changed since the backup; back up again")
        else:
            # Stock, or this installer's first merged layout, which lacked the
            # system_a/system_b stubs and cannot boot: both get the same table,
            # computed from the backed-up stock one.
            new_head, new_tail = merged_table(*gpt_regions(backup.file("mmcblk0.img"), total), total)
            planned = GptImage(total, new_head, new_tail)
            try:
                g_new, new_kind = planned.check()
            finally:
                planned.close()
            if new_kind != "v2_merged_geometry":
                raise Fail("internal error: the computed table checks as '%s'" % new_kind)
            if [e for e in g_new["entries"][:12]] != [e for e in g_saved["entries"][:12]]:
                raise Fail("internal error: the computed table changed partitions 1-12")
            ud = g_new["entries"][12]
            say("New table: 12 partitions unchanged, userdata %d..%d (%d MiB)"
                % (ud["first"], ud["last"], (ud["last"] - ud["first"] + 1) // 2048))
    finally:
        live.close()
        saved.close()

    say("")
    say("THIS ERASES FIRE OS on %s: system_a, system_b, cache and userdata." % backup.serial)
    say("The backup in %s is verified. Type the serial number to continue:" % backup.path)
    if input("> ").strip() != backup.serial:
        raise Fail("not confirmed; nothing was changed")

    unmount_all(echo)
    if new_head is not None:
        say("Writing the partition table ...")
        for name, blob, lba in (("head", new_head, 0), ("tail", new_tail, total - 33)):
            local = os.path.join(backup.path, "gpt-merged-%s.bin" % name)
            open(local, "wb").write(blob)
            echo.run("push", local, "/tmp/gpt-%s.bin" % name)
            if echo.sh("sha256sum /tmp/gpt-%s.bin" % name).split()[0] != hashlib.sha256(blob).hexdigest():
                raise Fail("the table did not arrive intact; nothing was written")
        # Backup table first: while only it is new, the primary still describes
        # the old disk and a reread of the primary sees a consistent stock table.
        for name, lba, count in (("tail", total - 33, 33), ("head", 0, 34)):
            echo.sh(DD + " if=/tmp/gpt-%s.bin of=/dev/block/mmcblk0 bs=512 seek=%d count=%d conv=notrunc,fsync"
                    " 2>/dev/null" % (name, lba, count))
        echo.sh("sync")
        after = GptImage(total, echo.read(0, 34 * SECTOR), echo.read((total - 33) * SECTOR, 33 * SECTOR))
        try:
            g_after, kind_after = after.check()
        finally:
            after.close()
        if kind_after != "v2_merged_geometry" or g_after["table_sha256"] != hashlib.sha256(new_head[1024:]).hexdigest():
            raise Fail("the table read back from the Echo is not the one written (%s)" % kind_after)
        say("  written and read back: v2 merged layout")
    echo.run("shell", "blockdev --rereadpt /dev/block/mmcblk0", check=False)

    # Zero the whole merged area. pmOS is written sparse, so anything not
    # overwritten would survive - including ext4 superblocks of the old system_a,
    # which TWRP's startup e2fsck -y would find and use to "repair" pmOS into
    # garbage. Measured 2026-09-16 (release-work-20260916/recovery).
    last = total - 34
    mib = (last + 1 - MERGED_FIRST) // 2048
    rest = (last + 1 - MERGED_FIRST) - mib * 2048
    say("Zeroing the new userdata (%d MiB; a few minutes) ..." % mib)
    t0 = time.time()
    echo.sh(DD + " if=/dev/zero of=/dev/block/mmcblk0 bs=1048576 seek=%d count=%d conv=notrunc,fsync"
            " 2>/dev/null" % (MERGED_FIRST // 2048, mib), timeout=3600)
    if rest:
        echo.sh(DD + " if=/dev/zero of=/dev/block/mmcblk0 bs=512 seek=%d count=%d conv=notrunc,fsync"
                " 2>/dev/null" % (MERGED_FIRST + mib * 2048, rest))
    echo.sh("sync")
    say("  done in %.0f s" % (time.time() - t0))
    probe = echo.read((MERGED_FIRST + 2) * SECTOR, 2 * SECTOR) + echo.read((last - 1) * SECTOR, 2 * SECTOR)
    if any(probe):
        raise Fail("the new userdata did not read back as zeros")

    # The settings store. pmOS mounts /opt by the label biscuit-opt; a stock
    # persist has Amazon's filesystem there (saved in the backup), and without
    # our store nothing the owner sets up survives a reflash.
    say("Making the settings store on persist ...")
    persist = [p for p in g_saved["entries"] if p["name"] == "persist"][0]
    if (persist["first"], persist["last"]) != (131072, 163839):
        raise Fail("persist is not where the stock v2 layout puts it")
    echo.sh("mke2fs -F -q -t ext4 -L %s /dev/block/by-name/persist" % STORE_LABEL, timeout=300)
    echo.sh("mkdir -p /tmp/opt && mount -t ext4 /dev/block/by-name/persist /tmp/opt")
    try:
        assets = backup.file("assets")
        n = 0
        for profile in sorted(os.listdir(assets)):
            for root, _dirs, files in os.walk(os.path.join(assets, profile)):
                for name in files:
                    local = os.path.join(root, name)
                    rel = os.path.relpath(local, assets).replace(os.sep, "/")
                    remote = "/tmp/opt/%s/%s" % (STORE_ASSETS, rel)
                    echo.sh("mkdir -p '%s'" % os.path.dirname(remote))
                    echo.run("push", local, remote)
                    if echo.sh("sha256sum '%s'" % remote).split()[0] != sha256_file(local):
                        raise Fail("%s did not arrive intact in the store" % rel)
                    n += 1
        echo.sh("chmod -R u=rwX,go=rX /tmp/opt/persist && sync")
        say("  %d file(s) in the store" % n)
    finally:
        echo.run("shell", "umount /tmp/opt", check=False)

    say("Rebooting to fastboot ...")
    echo.run("reboot", "bootloader", check=False)
    say("Repartition complete. Next: flash.")


# ------------------------------------------------------------------ step 3: flash

def newc(entries):
    """An uncompressed newc cpio of (path, mode, data) entries, with its trailer."""
    out = io.BytesIO()
    ino = 1000
    for path, mode, data in entries + [("TRAILER!!!", 0, b"")]:
        name = path.encode() + b"\0"
        hdr = "070701%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X%08X" % (
            ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0)
        out.write(hdr.encode() + name)
        out.write(b"\0" * (-out.tell() % 4))
        out.write(data)
        out.write(b"\0" * (-out.tell() % 4))
        ino += 1
    return out.getvalue()


def parse_newc(cpio):
    """name -> (mode, data) for an uncompressed newc archive."""
    pos, entries = 0, {}
    while pos + 110 <= len(cpio):
        h = cpio[pos:pos + 110]
        if h[:6] != b"070701":
            raise Fail("the initramfs is not a newc cpio archive")
        mode, size, nlen = int(h[14:22], 16), int(h[54:62], 16), int(h[94:102], 16)
        name = cpio[pos + 110:pos + 110 + nlen - 1].decode()
        data_at = pos + 110 + nlen
        data_at += -data_at % 4
        if name == "TRAILER!!!":
            break
        entries[name] = (mode, cpio[data_at:data_at + size])
        pos = data_at + size
        pos += -pos % 4
    return entries


def resolve(entries, path):
    """Follow the archive's own relative symlinks along path, component by
    component, so a file meant for lib/firmware lands where lib points."""
    parts, done = path.split("/"), []
    for _ in range(40):
        if not parts:
            return "/".join(done)
        cand = "/".join(done + [parts[0]])
        mode, data = entries.get(cand, (0, b""))
        if mode & 0o170000 == 0o120000:
            target = data.decode()
            if target.startswith("/"):
                done, parts = [], target.strip("/").split("/") + parts[1:]
            else:
                parts = target.split("/") + parts[1:]
            continue
        done.append(parts.pop(0))
    raise Fail("symlink loop resolving %s in the initramfs" % path)


def owner_boot_image(boot, firmware_dir):
    """pmOS's boot.img with this Echo's FPGA and Bluetooth firmware appended to
    its initramfs as a second cpio archive. Both are requested by built-in
    drivers within a second of power-on, long before any filesystem, so they
    must be in the initramfs from the very first boot.

    The appended archive must never contain an entry whose type differs from
    what the first archive has at that path: the kernel's unpacker DELETES the
    old entry first. pmOS's initramfs has lib -> usr/lib, and a plain `lib`
    directory entry replaced that symlink with an empty directory - taking the
    musl loader with it, so /init failed with ENOENT and the kernel panicked.
    So paths are resolved through the archive's own symlinks and only
    directories that do not exist yet are added."""
    img = bytearray(open(boot, "rb").read())
    if img[:8] != b"ANDROID!":
        raise Fail("%s is not an Android boot image" % boot)
    ksize, _, rsize, _, ssize, _, _, page = struct.unpack_from("<8I", img, 8)
    if page != 2048:
        raise Fail("boot image page size is %d, not 2048" % page)
    koff = page
    if struct.unpack_from("<I", img, koff)[0] != MTK_MAGIC:
        raise Fail("the kernel has no MediaTek header; this image would bootloop")
    roff = koff + -(-ksize // page) * page
    ramdisk = bytes(img[roff:roff + rsize])
    second = bytes(img[roff + -(-rsize // page) * page:][:ssize])
    try:
        base = parse_newc(zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(ramdisk))
    except zlib.error as e:
        raise Fail("cannot read the initramfs: %s" % e)
    files, dirs = [], []
    for rel in ("i2s_to_spi_v34.bin", "mediatek/ROMv2_lm_patch_1_0_hdr.bin",
                "mediatek/ROMv2_lm_patch_1_1_hdr.bin"):
        src = os.path.join(firmware_dir, *rel.split("/"))
        if not os.path.exists(src):
            raise Fail("the export has no %s" % rel)
        target = resolve(base, "lib/firmware/" + rel)
        parts = target.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            mode = base.get(d, (None,))[0]
            if mode is None:
                if d not in [x[0] for x in dirs]:
                    dirs.append((d, 0o40755, b""))
            elif mode & 0o170000 != 0o040000:
                raise Fail("%s in the initramfs is not a directory" % d)
        if target in base:
            raise Fail("the initramfs already has %s" % target)
        files.append((target, 0o100644, open(src, "rb").read()))
    extra = newc(dirs + files)
    new_ramdisk = ramdisk + b"\0" * (-len(ramdisk) % 4) + extra
    kernel = bytes(img[koff:koff + ksize])
    h = hashlib.sha1()
    for blob in (kernel, new_ramdisk, second):
        h.update(blob)
        h.update(struct.pack("<I", len(blob)))
    header = bytearray(img[:page])
    struct.pack_into("<I", header, 16, len(new_ramdisk))
    header[576:608] = h.digest() + b"\0" * 12
    # amonet v2's bootloader starts the kernel in whatever mode bootopt's third
    # field names, and the kernel is arm64: 32N2 there means a hang before the
    # kernel's first instruction. Same length, so nothing else moves.
    cmd = bytes(header[64:576])
    at = cmd.find(b"bootopt=")
    if at < 0 or len(cmd) < at + 0x12 + 2:
        raise Fail("the boot image command line has no bootopt")
    if cmd[at + 0x12:at + 0x14] == b"32":
        header[64 + at + 0x12:64 + at + 0x14] = b"64"
    elif cmd[at + 0x12:at + 0x14] != b"64":
        raise Fail("unrecognised bootopt in the boot image command line")
    pad = lambda b: b + b"\0" * (-len(b) % page)
    out = bytes(header) + pad(kernel) + pad(new_ramdisk) + (pad(second) if second else b"")
    if len(out) > BOOT_SLOT_BYTES:
        raise Fail("the boot image is %d bytes; a boot slot holds %d" % (len(out), BOOT_SLOT_BYTES))
    return out


def fastboot(fb, serial, *args, timeout=1800):
    r = subprocess.run([fb, "-s", serial, *args], capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    out = (r.stdout + r.stderr).replace("\r\n", "\n")
    if r.returncode != 0:
        raise Fail("fastboot %s failed:\n%s" % (" ".join(args), out.strip()))
    return out


def getvar(fb, serial, name):
    out = fastboot(fb, serial, "getvar", name, timeout=60)
    for line in out.splitlines():
        line = line.replace("(bootloader)", "").strip()
        # The name itself may contain a colon (partition-size:userdata), so
        # strip it whole rather than splitting on the first colon.
        if line.startswith(name + ":"):
            return line[len(name) + 1:].strip()
    raise Fail("fastboot getvar %s gave no value" % name)


def cmd_flash(a, adb):
    backup = Backup(a.backup)
    fb = tool("fastboot", a.fastboot)
    devices = subprocess.run([fb, "devices"], capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL).stdout
    if backup.serial not in devices:
        raise Fail("the Echo %s is not in fastboot mode" % backup.serial)
    if getvar(fb, backup.serial, "product") != "BISCUIT":
        raise Fail("the fastboot device is not an Echo Dot 2")
    total = backup.m["dumps"]["mmcblk0"]["bytes"] // SECTOR
    want = (total - 34 + 1 - MERGED_FIRST - 2 * STUB_SECTORS) * SECTOR
    have = int(getvar(fb, backup.serial, "partition-size:userdata"), 16)
    if have != want:
        raise Fail("fastboot sees userdata as %d bytes, not the merged %d; run 'repartition' first"
                   % (have, want))
    for slot in ("boot_a", "boot_b"):
        size = int(getvar(fb, backup.serial, "partition-size:" + slot), 16)
        if size != BOOT_SLOT_BYTES:
            raise Fail("%s is %d bytes, not a 16 MiB boot slot" % (slot, size))
    image = owner_boot_image(a.boot, backup.file("assets", "firmware"))
    owned = backup.file("boot-owner.img")
    open(owned, "wb").write(image)
    say("Boot image with this Echo's firmware: %d bytes, sha256 %s"
        % (len(image), hashlib.sha256(image).hexdigest()))
    for slot in ("boot_a", "boot_b"):
        say("Flashing %s ..." % slot)
        fastboot(fb, backup.serial, "flash", slot, owned)
    say("Flashing userdata (several minutes) ...")
    fastboot(fb, backup.serial, "-S", "100M", "flash", "userdata", a.disk, timeout=3600)
    say("Rebooting into postmarketOS.")
    fastboot(fb, backup.serial, "reboot")
    say("Done. The Echo raises its setup hotspot when it has booted.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="step", required=True)
    for name in ("export", "repartition", "flash"):
        p = sub.add_parser(name)
        p.add_argument("--backup", required=True, help="the folder biscuit_backup.py made")
        p.add_argument("--adb")
        p.add_argument("--fastboot")
        if name == "flash":
            p.add_argument("--boot", required=True, help="pmOS boot.img")
            p.add_argument("--disk", required=True, help="pmOS disk image")
    a = ap.parse_args()
    try:
        adb = tool("adb", a.adb)
        {"export": cmd_export, "repartition": cmd_repartition, "flash": cmd_flash}[a.step](a, adb)
        return 0
    except Fail as e:
        print("\nSTOPPED: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
