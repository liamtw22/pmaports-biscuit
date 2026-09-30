#!/usr/bin/env python3
"""Back up an amonet-unlocked Echo Dot 2 (biscuit) from TWRP, before pmOS.

Run it with the Echo in TWRP and connected over USB. It reads the whole eMMC
and both hardware boot partitions over ADB and writes nothing to the Echo
except to unmount the filesystems TWRP mounted, so the image is consistent.

    python biscuit_backup.py --serial SERIAL [--out DIR]

What it saves, into DIR/biscuit-backup-SERIAL-DATE/:
  mmcblk0.img        the whole user area: both GPTs, every partition, the gaps
  mmcblk0boot0.img   eMMC boot partition 0 (the preloader)
  mmcblk0boot1.img   eMMC boot partition 1 (this unit's factory calibration and
                     identity, IDME); it cannot be recovered from anywhere else
  parts/NAME.img     each GPT partition, cut from mmcblk0.img, for convenience
  manifest.json      sizes and SHA-256 of everything, the GPT, the device's own
                     hashes, the TWRP and device properties

Each dump is hashed as it streams to this computer, and again by the Echo
itself; the two must agree, and the byte count must equal the device size.
Any mismatch fails the run. RPMB is not readable without Amazon's key and is
not needed.

Only the Echo named by --serial is touched, even with other devices attached.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib

TOOL_VERSION = "0.1.0"
CHUNK = 1 << 20
DUMPS = ("mmcblk0boot0", "mmcblk0boot1", "mmcblk0")
SECTOR = 512


class Fail(Exception):
    pass


def find_adb(explicit):
    if explicit:
        return explicit
    found = shutil.which("adb")
    if not found:
        raise Fail("adb not found; install Android platform-tools or pass --adb PATH")
    return found


class Echo:
    def __init__(self, adb, serial):
        self.adb, self.serial = adb, serial

    def run(self, *args, check=True):
        r = subprocess.run([self.adb, "-s", self.serial, *args],
                           capture_output=True, text=True, timeout=600)
        if check and r.returncode != 0:
            raise Fail("adb %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
        return r.stdout

    def sh(self, command, check=True):
        return self.run("shell", command, check=check).replace("\r\n", "\n")

    def prop(self, name):
        return self.sh("getprop " + name).strip()


def attached(adb):
    out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=30).stdout
    rows = [l.split("\t") for l in out.splitlines()[1:] if "\t" in l]
    return {serial: state for serial, state in rows}


def preflight(adb, serial):
    devices = attached(adb)
    if serial is None:
        if len(devices) != 1:
            raise Fail("%d devices attached (%s); name the Echo with --serial"
                       % (len(devices), ", ".join(devices) or "none"))
        serial = next(iter(devices))
    if devices.get(serial) != "recovery":
        raise Fail("%s is not in recovery (state: %s); boot the Echo to TWRP first"
                   % (serial, devices.get(serial, "not attached")))
    echo = Echo(adb, serial)
    twrp, device = echo.prop("ro.twrp.version"), echo.prop("ro.product.device")
    if not twrp:
        raise Fail("%s is in a recovery, but not TWRP" % serial)
    if device != "biscuit":
        raise Fail("%s is a '%s', not an Echo Dot 2 (biscuit)" % (serial, device))
    if echo.prop("ro.boot.serialno") != serial:
        raise Fail("the recovery's own serial does not match %s" % serial)
    return echo


def unmount_all(echo):
    """Unmount every filesystem on the eMMC, so nothing changes under dd."""
    mounted = emmc_mounts(echo)
    for _, point in sorted(mounted, key=lambda m: -len(m[1])):
        echo.sh("umount %s" % point, check=False)
    left = emmc_mounts(echo)
    if left:
        raise Fail("could not unmount: %s" % "; ".join("%s on %s" % m for m in left))
    return [{"device": d, "mountpoint": p} for d, p in mounted]


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


def dump(echo, name, path):
    dev = "/dev/block/" + name
    size = int(echo.sh("blockdev --getsize64 %s" % dev).strip())
    h = hashlib.sha256()
    n = 0
    proc = subprocess.Popen([echo.adb, "-s", echo.serial, "exec-out",
                             "dd if=%s bs=%d 2>/dev/null" % (dev, CHUNK)],
                            stdout=subprocess.PIPE)
    with open(path, "wb") as f:
        while True:
            block = proc.stdout.read(CHUNK)
            if not block:
                break
            f.write(block)
            h.update(block)
            n += len(block)
            if size >= 64 * CHUNK and n % (256 * CHUNK) < CHUNK:
                print("    %s: %d / %d MiB" % (name, n >> 20, size >> 20), flush=True)
        f.flush()
        os.fsync(f.fileno())
    if proc.wait() != 0:
        raise Fail("reading %s failed (adb exit %d)" % (name, proc.returncode))
    if n != size:
        raise Fail("%s: read %d bytes, the device is %d" % (name, n, size))
    host = h.hexdigest()
    device = echo.sh("sha256sum %s" % dev).split()[0]
    if device != host:
        raise Fail("%s: the Echo hashes %s, this computer %s" % (name, device, host))
    print("  %-13s %11d bytes  sha256 %s  (Echo agrees)" % (name, n, host), flush=True)
    return {"device": dev, "bytes": n, "sha256": host, "sha256_on_device": device}


def guid(b):
    a, b2, c = struct.unpack_from("<IHH", b, 0)
    return "%08X-%04X-%04X-%s-%s" % (a, b2, c, b[8:10].hex().upper(), b[10:16].hex().upper())


def read_gpt(f, lba, disk_bytes):
    f.seek(lba * SECTOR)
    hdr = f.read(SECTOR)
    if hdr[:8] != b"EFI PART":
        raise Fail("no GPT header at LBA %d" % lba)
    hsize = struct.unpack_from("<I", hdr, 12)[0]
    crc = struct.unpack_from("<I", hdr, 16)[0]
    check = bytearray(hdr[:hsize])
    check[16:20] = b"\0\0\0\0"
    if zlib.crc32(bytes(check)) & 0xFFFFFFFF != crc:
        raise Fail("GPT header at LBA %d: bad CRC" % lba)
    my, alt, first, last = struct.unpack_from("<QQQQ", hdr, 24)
    entries_lba, count, esize, ecrc = struct.unpack_from("<QIII", hdr, 72)
    f.seek(entries_lba * SECTOR)
    array = f.read(count * esize)
    if zlib.crc32(array) & 0xFFFFFFFF != ecrc:
        raise Fail("GPT entries for header at LBA %d: bad CRC" % lba)
    parts = []
    for i in range(count):
        e = array[i * esize:(i + 1) * esize]
        if e[:16] == b"\0" * 16:
            continue
        start, end, attrs = struct.unpack_from("<QQQ", e, 32)
        name = e[56:128].decode("utf-16-le").rstrip("\0")
        parts.append({"index": i + 1, "name": name, "type": guid(e[0:16]), "guid": guid(e[16:32]),
                      "first_lba": start, "last_lba": end, "attributes": attrs,
                      "bytes": (end - start + 1) * SECTOR})
    return {"header_lba": my, "alternate_lba": alt, "first_usable": first, "last_usable": last,
            "disk_guid": guid(hdr[56:72]), "entries_lba": entries_lba, "entry_count": count,
            "entry_size": esize, "entries_sha256": hashlib.sha256(array).hexdigest(),
            "partitions": parts}


def split_partitions(image, outdir, disk_bytes):
    with open(image, "rb") as f:
        primary = read_gpt(f, 1, disk_bytes)
        backup = read_gpt(f, disk_bytes // SECTOR - 1, disk_bytes)
    if primary["alternate_lba"] != disk_bytes // SECTOR - 1:
        raise Fail("primary GPT does not point at the last sector")
    if primary["entries_sha256"] != backup["entries_sha256"]:
        raise Fail("primary and backup GPT entry arrays differ")
    os.makedirs(outdir, exist_ok=True)
    with open(image, "rb") as f:
        for p in primary["partitions"]:
            h = hashlib.sha256()
            f.seek(p["first_lba"] * SECTOR)
            left = p["bytes"]
            with open(os.path.join(outdir, p["name"] + ".img"), "wb") as out:
                while left:
                    block = f.read(min(CHUNK, left))
                    out.write(block)
                    h.update(block)
                    left -= len(block)
            p["sha256"] = h.hexdigest()
    return primary


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--serial", help="the Echo's serial, from `adb devices`")
    ap.add_argument("--out", default=".", help="directory to create the backup in")
    ap.add_argument("--adb", help="path to adb")
    a = ap.parse_args()
    try:
        adb = find_adb(a.adb)
        echo = preflight(adb, a.serial)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outdir = os.path.join(a.out, "biscuit-backup-%s-%s" % (echo.serial, stamp))
        os.makedirs(outdir)
        print("Backing up %s into %s" % (echo.serial, outdir), flush=True)
        manifest = {
            "tool": "biscuit_backup.py", "tool_version": TOOL_VERSION, "started_utc": stamp,
            "serial": echo.serial,
            "properties": {p: echo.prop(p) for p in (
                "ro.twrp.version", "ro.product.device", "ro.boot.serialno", "ro.boot.slot_suffix",
                "ro.boot.lk_build_desc", "ro.boot.pl_build_desc", "ro.build.fingerprint")},
            "kernel": echo.sh("uname -a").strip(),
            "cmdline": echo.sh("cat /proc/cmdline").strip(),
            "proc_partitions": echo.sh("cat /proc/partitions"),
            "sgdisk_print": echo.sh("sgdisk -p /dev/block/mmcblk0", check=False),
        }
        manifest["unmounted"] = unmount_all(echo)
        manifest["dumps"] = {}
        for name in DUMPS:
            manifest["dumps"][name] = dump(echo, name, os.path.join(outdir, name + ".img"))
        disk = manifest["dumps"]["mmcblk0"]["bytes"]
        print("Cutting partitions out of mmcblk0.img ...", flush=True)
        manifest["gpt"] = split_partitions(os.path.join(outdir, "mmcblk0.img"),
                                           os.path.join(outdir, "parts"), disk)
        for p in manifest["gpt"]["partitions"]:
            print("  p%-2d %-10s %11d bytes  sha256 %s" % (p["index"], p["name"], p["bytes"], p["sha256"]))
        manifest["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with open(os.path.join(outdir, "manifest.json"), "w", newline="\n") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")
        print("\nBackup complete and verified: %s" % outdir)
        print("Keep a second copy somewhere else before installing pmOS.")
        return 0
    except Fail as e:
        print("\nBACKUP FAILED: %s" % e, file=sys.stderr)
        print("Nothing on the Echo was changed. Fix the problem and run it again.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
