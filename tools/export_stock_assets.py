#!/usr/bin/env python3
"""Export everything postmarketOS will want, BEFORE the conversion destroys it.

The merged v2 layout deletes `system_a`, and that is where every owner-supplied
asset lives: the microphone chain's coefficients, the earcons, the LED
animations, the Wi-Fi and Bluetooth firmware, and the library carrying the AEC
step-size table. After conversion none of it can be recovered from the device.
This is the only chance, so it should be one deliberate step in the installer
rather than an afterthought.

Two sources, same logic, so it can be tested without a stock device in hand:

    --from-tree DIR    an extracted or mounted system tree (this is what the
                       repository's firmware dumps are)
    --from-adb SERIAL  a rooted stock device over adb

Both generations are handled. Paths moved between them - firmware is
`/system/etc/firmware` on Fire OS 5 and `/system/vendor/firmware` on Fire OS 6 -
and an exporter that knew only one would quietly produce an incomplete archive,
which is the failure mode that matters most here: the owner does not find out
until after the partition is gone.

## Raw partition backups, and why they are taken anyway

An earlier version of this deliberately skipped two things, on the grounds that
they survive conversion:

  * per-unit calibration (`idme`, in the eMMC hardware boot partition
    `mmcblk0boot1`, which sits OUTSIDE the GPT)
  * the FPGA bitstream, which is carved from `boot_a_x`

**That reasoning is correct and they are still exported.** Item 1.38's rule is
that intended preservation does not replace an independent backup, and it is
right: "it survives repartitioning" is a statement about the repartitioning
going as planned. The calibration in particular is per-unit - `miccal.0`
through `miccal.6` and `alscal` are specific to one physical board, spanning a
~2.9 dB sensitivity spread across the array - so no other device and no archive
anywhere can reconstruct it. It costs about a megabyte to be sure.

Partitions are located through their `by-name` symlinks ONLY. Device 1's
numbering is post-merge and a stock device's is not, so hard-coding a partition
number here would read the wrong partition on the exact devices this is for. If
a `by-name` link is absent, that is reported rather than guessed around.

What is still deliberately NOT exported: locale earcon directories, since only
`base/` is locale-independent and used.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import zlib

HERE = pathlib.Path(__file__).resolve().parent

# (label, candidate source paths relative to the system root, kind)
GROUPS = [
    # Also holds EQ_50.cfg, the speaker correction curve (profile "speaker"),
    # which is owner-imported rather than shipped since r294. The last two
    # paths are where the backup zip also looks.
    ('audio-algorithms',
     ['system/vendor/etc/audio-algorithms', 'vendor/etc/audio-algorithms',
      'system/etc/audio-algorithms', 'etc/audio-algorithms'], 'dir'),
    ('earcons',
     ['system/local/share/earcon/base', 'local/share/earcon/base'], 'dir'),
    ('led-resources',
     ['system/etc/led-resources', 'etc/led-resources'], 'dir'),
    ('firmware',
     ['system/vendor/firmware', 'system/etc/firmware', 'vendor/firmware',
      'etc/firmware'], 'dir'),
    ('libasp',
     ['system/lib/libasp.so', 'lib/libasp.so'], 'file'),
]

# (label, absolute device paths to try in order, what it is)
#
# by-name FIRST and by preference. mmcblk0boot1 is named directly because it is
# an eMMC hardware boot partition rather than a GPT entry, so it has no by-name
# link on any device and its name is fixed by the controller, not the layout.
RAW = [
    ('idme',
     ['/dev/block/mmcblk0boot1', '/dev/mmcblk0boot1'],
     'per-unit factory calibration (miccal.0-6, alscal, board_id, bt_mac_addr)'),
    ('boot_a_x',
     ['/dev/block/platform/mtk-msdc.0/by-name/boot_a_x',
      '/dev/block/by-name/boot_a_x'],
     'carries the FPGA bitstream'),
]

# Files whose absence means a profile cannot be rebuilt later. Checked by name so the
# report can say which generation the export can actually serve.
FIREOS5_MARKERS = ('coefs_FBF.cfg', 'coefs_FilterBank_640.cfg')
FIREOS6_MARKERS = ('coefs_FBFV2_LowLatency_8beams.cfg',
                   'coefs_FilterBank_AnalysisSynthesis_768cvxGLow.cfg')
# The speaker correction curve, matched by content: EQ_50.cfg, EQ_60.cfg and
# EQ_70.cfg are the same bytes, on Fire OS 5 and 6 alike. Same value as the
# "speaker" profile in biscuit-profile-assets.json.
SPEAKER_CURVE_SHA256 = ('8b0c40484c037f6a7e740721c7a5232b'
                        '37fda603eedc1eec7c483666d852e763')


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# The FPGA bitstream is the one blob that is not a file anywhere on the device.
# Stock compiles it into its own kernel through CONFIG_EXTRA_FIRMWARE, exactly as
# we do, so the only way to get it is to carve it back out of the stock kernel on
# boot_a_x.
#
# It is found by SIGNATURE, never by offset. A note in this project recorded it at
# 0x8702e8; in the stock kernel actually to hand it is at 0x7f92a4. An offset is a
# property of one kernel build and silently cuts garbage from any other, which
# would then be clocked into the FPGA.
FPGA_SIGNATURE = b'\xff\x00Lattice\x00'
FPGA_PART = b'Part: iCE40UL1K-SWG16'
FPGA_LENGTH = 30964


def carve_fpga_bitstream(boot_image):
    """Pull the Lattice bitstream out of a stock boot_a_x image.

    Returns (bitstream, note). The kernel inside the boot image is gzipped, and
    there are several gzip streams in there - the ramdisk is one - so each is
    inflated in turn and searched.
    """
    try:
        raw = pathlib.Path(boot_image).read_bytes()
    except OSError as err:
        return None, 'cannot read %s: %s' % (boot_image, err)

    position = 0
    streams = 0
    while True:
        position = raw.find(b'\x1f\x8b\x08', position)
        if position < 0:
            break
        streams += 1
        try:
            inflated = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw[position:])
        except zlib.error:
            position += 1
            continue
        at = inflated.find(FPGA_SIGNATURE)
        if at >= 0 and FPGA_PART in inflated[at:at + 256]:
            cut = inflated[at:at + FPGA_LENGTH]
            if len(cut) != FPGA_LENGTH:
                return None, 'signature found but the image ends early'
            return cut, ('carved from the kernel at gzip 0x%x, decompressed 0x%x'
                         % (position, at))
        position += 1
    return None, ('no Lattice bitstream in any of the %d gzip streams - is this a '
                  'boot_a_x image? boot_a and boot_b are decoys, 92%% zeros' % streams)


class TreeSource:
    def __init__(self, root):
        self.root = pathlib.Path(root)

    def resolve(self, candidates, kind):
        for rel in candidates:
            path = self.root / rel
            if (path.is_dir() if kind == 'dir' else path.is_file()):
                return rel
        return None

    def copy(self, rel, destination, kind):
        source = self.root / rel
        if kind == 'dir':
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def resolve_raw(self, candidates):
        # A system tree is a filesystem, not a disk. Partitions are simply not
        # reachable this way, and saying so is more useful than an empty file.
        return None

    def copy_raw(self, device, destination):
        raise NotImplementedError


class AdbSource:
    def __init__(self, serial):
        self.serial = serial

    def _run(self, *args, **kw):
        return subprocess.run(['adb', '-s', self.serial] + list(args),
                              capture_output=True, text=True, timeout=600, **kw)

    def resolve(self, candidates, kind):
        flag = '-d' if kind == 'dir' else '-f'
        for rel in candidates:
            out = self._run('shell', 'su', '-c',
                            '[ %s /%s ] && echo yes' % (flag, rel)).stdout
            if 'yes' in out:
                return rel
        return None

    def copy(self, rel, destination, kind):
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Staged through /data/local/tmp because `adb pull` cannot read a
        # root-only path directly on these builds.
        stage = '/data/local/tmp/biscuit-export'
        self._run('shell', 'su', '-c', 'rm -rf %s && mkdir -p %s' % (stage, stage))
        self._run('shell', 'su', '-c', 'cp -r /%s %s/payload' % (rel, stage))
        self._run('shell', 'su', '-c', 'chmod -R 0755 %s' % stage)
        self._run('pull', '%s/payload' % stage, str(destination))
        self._run('shell', 'su', '-c', 'rm -rf %s' % stage)

    def resolve_raw(self, candidates):
        for device in candidates:
            out = self._run('shell', 'su', '-c',
                            '[ -e %s ] && echo yes' % device).stdout
            if 'yes' in out:
                return device
        return None

    def copy_raw(self, device, destination):
        """dd the whole partition out, then pull it.

        Read through dd into /data/local/tmp rather than pulled directly: on
        these builds adb cannot read a block device, and a partial read would
        produce a short file that still looks like a backup.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = '/data/local/tmp/biscuit-export-raw'
        self._run('shell', 'su', '-c', 'rm -rf %s && mkdir -p %s' % (stage, stage))
        result = self._run('shell', 'su', '-c',
                           'dd if=%s of=%s/payload bs=1M 2>&1' % (device, stage))
        self._run('shell', 'su', '-c', 'chmod -R 0644 %s/payload' % stage)
        self._run('pull', '%s/payload' % stage, str(destination))
        self._run('shell', 'su', '-c', 'rm -rf %s' % stage)
        return (result.stdout or '').strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--from-tree', help='an extracted or mounted system tree')
    parser.add_argument('--from-adb', help='serial of a rooted stock device')
    parser.add_argument('--out', required=True, help='destination directory')
    parser.add_argument('--apply', action='store_true',
                        help='without this, report what would be exported')
    parser.add_argument('--skip-raw', action='store_true',
                        help='do not back up calibration and boot partitions')
    parser.add_argument('--boot-image',
                        help='a stock boot_a_x image to carve the FPGA bitstream '
                             'from; with --from-adb it is taken from the device')
    args = parser.parse_args()
    if bool(args.from_tree) == bool(args.from_adb):
        parser.error('give exactly one of --from-tree or --from-adb')

    source = (TreeSource(args.from_tree) if args.from_tree
              else AdbSource(args.from_adb))
    out = pathlib.Path(args.out)
    report = {'schema': 'biscuit-stock-export-v2',
              'source': args.from_tree or ('adb:' + args.from_adb),
              'destination': str(out), 'applied': False, 'groups': {}, 'raw': {}}

    missing = []
    for label, candidates, kind in GROUPS:
        rel = source.resolve(candidates, kind)
        report['groups'][label] = {'found_at': rel, 'searched': candidates}
        if rel is None:
            missing.append(label)
    report['missing_groups'] = missing

    raw_missing = []
    if not args.skip_raw:
        tree_source = isinstance(source, TreeSource)
        for label, candidates, what in RAW:
            device = source.resolve_raw(candidates)
            entry = {'found_at': device, 'searched': candidates, 'what': what}
            if device is None:
                # Say WHY. A bare null here reads as "something went wrong",
                # when from a system tree it is simply not a question that can
                # be asked - and an owner needs to know their calibration is
                # still unbacked-up either way.
                entry['error'] = ('a system tree contains filesystems, not '
                                  'partitions; run with --from-adb against the '
                                  'device itself to back this up'
                                  if tree_source else
                                  'no by-name link found; partition numbers are '
                                  'NOT guessed, because a stock layout differs '
                                  'from a converted one')
                raw_missing.append(label)
            report['raw'][label] = entry
    report['missing_raw'] = raw_missing

    if not args.apply:
        report['reason'] = 'dry_run'
        print(json.dumps(report, indent=2))
        return 1 if missing else 0

    out.mkdir(parents=True, exist_ok=True)
    for label, candidates, kind in GROUPS:
        rel = report['groups'][label]['found_at']
        if rel is None:
            continue
        if kind == 'dir':
            destination = out / label
        else:
            destination = out / label / pathlib.PurePosixPath(rel).name
        source.copy(rel, destination, kind)

    for label in report['raw']:
        device = report['raw'][label]['found_at']
        if device is None:
            continue
        destination = out / 'raw' / (label + '.img')
        try:
            report['raw'][label]['dd'] = source.copy_raw(device, destination)
            if destination.is_file():
                report['raw'][label]['bytes'] = destination.stat().st_size
                report['raw'][label]['sha256'] = digest(destination)
            else:
                report['raw'][label]['error'] = 'nothing was pulled'
        except NotImplementedError:
            report['raw'][label]['error'] = 'not reachable from a system tree'

    # The FPGA bitstream, carved out of whichever boot image we have.
    boot_image = args.boot_image
    if not boot_image:
        pulled = out / 'raw' / 'boot_a_x.img'
        if pulled.is_file():
            boot_image = str(pulled)
    if boot_image:
        bitstream, note = carve_fpga_bitstream(boot_image)
        report['fpga'] = {'source': boot_image, 'note': note}
        if bitstream:
            target = out / 'firmware' / 'i2s_to_spi_v34.bin'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bitstream)
            report['fpga']['bytes'] = len(bitstream)
            report['fpga']['sha256'] = hashlib.sha256(bitstream).hexdigest()
    else:
        report['fpga'] = {'error': 'no boot image; pass --boot-image, or use '
                                   '--from-adb so boot_a_x can be read from the '
                                   'device. The bitstream is NOT a file anywhere '
                                   'on the filesystem.'}

    # Inventory what actually landed, by hash, so the archive can be trusted later.
    inventory = {}
    for path in sorted(out.rglob('*')):
        if path.is_file() and path.name != 'MANIFEST.json':
            inventory[str(path.relative_to(out)).replace(chr(92), '/')] = {
                'bytes': path.stat().st_size, 'sha256': digest(path)}
    report['files'] = len(inventory)
    report['total_bytes'] = sum(v['bytes'] for v in inventory.values())

    names = {pathlib.PurePosixPath(k).name for k in inventory}
    report['can_rebuild'] = {
        'fireos5': all(m in names for m in FIREOS5_MARKERS),
        'fireos6': all(m in names for m in FIREOS6_MARKERS),
        'speaker_curve': any(v['sha256'] == SPEAKER_CURVE_SHA256
                             for v in inventory.values()),
        'vss_table': 'libasp.so' in names,
        # Named separately from the asset profiles because these are per-unit
        # and unreconstructable, not merely inconvenient to re-source.
        'calibration_backed_up': 'idme.img' in names,
        'boot_backed_up': 'boot_a_x.img' in names,
        'fpga_bitstream': 'i2s_to_spi_v34.bin' in names,
    }
    report['applied'] = True
    (out / 'MANIFEST.json').write_text(
        json.dumps({'export': report, 'inventory': inventory}, indent=2) + chr(10),
        encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
