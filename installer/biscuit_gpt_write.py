#!/usr/bin/env python3
"""Write a partition table to an Echo Dot 2 through amonet's bootrom path.

For when the bootloader cannot boot anything - so neither TWRP nor fastboot can
fix the table - but the Echo can still enter USB download mode (hold MUTE while
plugging it in). This uses amonet's own payload to read and write the eMMC.

    python biscuit_gpt_write.py --amonet AMONET_DIR --total-sectors N \\
        --expect-head OLD_HEAD.bin --expect-tail OLD_TAIL.bin \\
        --head NEW_HEAD.bin --tail NEW_TAIL.bin

Start it first, then plug the Echo in with MUTE held. It refuses to write
unless the Echo's current table is byte-for-byte the expected one, writes the
backup table before the primary, reads every block back, and reboots the Echo.
"""
import argparse
import hashlib
import os
import sys

SECTOR = 512


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--amonet", required=True, help="the amonet folder (with modules/ and brom-payload/)")
    ap.add_argument("--total-sectors", type=int, required=True)
    for n in ("expect-head", "expect-tail", "head", "tail"):
        ap.add_argument("--" + n, required=True)
    a = ap.parse_args()

    blobs = {k: open(getattr(a, k.replace("-", "_")), "rb").read() for k in ("expect-head", "expect-tail", "head", "tail")}
    for k, n in (("expect-head", 34), ("head", 34), ("expect-tail", 33), ("tail", 33)):
        if len(blobs[k]) != n * SECTOR:
            sys.exit("%s is %d bytes, expected %d sectors" % (k, len(blobs[k]), n))
    regions = ((0, "head"), (a.total_sectors - 33, "tail"))

    modules = os.path.join(os.path.abspath(a.amonet), "modules")
    os.chdir(modules)                       # amonet loads its payload by relative path
    sys.path.insert(0, modules)
    from common import Device
    from main import prepare, switch_user
    from serial.tools import list_ports
    import time

    # amonet's own discovery opens COM1..COM256 in turn on Windows, which is
    # slower than the bootrom's handshake window: the Echo times out and
    # re-enumerates over and over. Poll the port list by USB ID instead and
    # take the port the moment it appears.
    # 0x0003 is the bootrom; 0x2000 the preloader. amonet only trusts a
    # preloader whose USB manufacturer string is "PWNED", but on Windows
    # pyserial reports the DRIVER's vendor ("MediaTek Inc.") instead, so that
    # check can never pass there. The preloader on an amonet-v2 Echo is
    # amonet's own (its UART log prints the PL-payload banner), and prepare()
    # loads amonet's payload through it during its normal handshake window.
    def usbdl_port():
        for p in list_ports.comports():
            if p.vid == 0x0E8D and p.pid in (0x0003, 0x2000):
                return p.device
        return None

    print("Plug the Echo in now, holding only MUTE ...", flush=True)
    port = None
    while port is None:
        port = usbdl_port()
        if port is None:
            time.sleep(0.02)
    dev = Device(port=port, require_pwned=False)
    prepare(dev)
    switch_user(dev)

    def read(lba, n):
        out = b""
        for i in range(n):
            out += dev.emmc_read(lba + i)
            if i % 16 == 0:
                dev.kick_watchdog()
        return out

    for lba, name in regions:
        have = read(lba, len(blobs[name]) // SECTOR)
        if have == blobs[name]:
            print("%s: already the new table" % name)
        elif have != blobs["expect-" + name]:
            print("REFUSING: the Echo's current %s table is not the expected one (sha256 %s)"
                  % (name, hashlib.sha256(have).hexdigest()))
            dev.reboot()
            return 1
    print("current table matches the expected one", flush=True)

    # Backup table first, as the installer does: while only it is new, the
    # primary still describes a consistent table.
    for lba, name in reversed(regions):
        data = blobs[name]
        for i in range(len(data) // SECTOR):
            block = data[i * SECTOR:(i + 1) * SECTOR]
            dev.emmc_write(lba + i, block)
            if dev.emmc_read(lba + i) != block:
                print("WRITE DID NOT VERIFY at block %d; stopping" % (lba + i))
                dev.reboot()
                return 1
            if i % 8 == 0:
                dev.kick_watchdog()
        print("wrote and verified %s (%d blocks at %d)" % (name, len(data) // SECTOR, lba), flush=True)

    for lba, name in regions:
        if read(lba, len(blobs[name]) // SECTOR) != blobs[name]:
            print("FINAL READBACK FAILED for %s" % name)
            dev.reboot()
            return 1
    print("both tables read back correctly; rebooting the Echo", flush=True)
    dev.reboot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
