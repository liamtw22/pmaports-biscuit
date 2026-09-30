#!/usr/bin/env python3
"""Build the four biscuit TWRP zips.

    build_zips.py --image pmos.img --boot boot.img --out OUTDIR [--build r295]
                  [--manifest biscuit-profile-assets.json] [--record]
    build_zips.py --tools-only [--manifest biscuit-profile-assets.json] --out OUTDIR

    The manifest is the device package's biscuit-profile-assets.json (in this
    repository at device/testing/device-amazon-biscuit/, and in the image at
    /usr/share/biscuit/): the backup's assets.list is made from it. A full
    build reads the image's copy; --tools-only reads the repository's.

    pmos-amazon-biscuit-v<X.Y>.zip        the install, named after the public
                                          release the image carries in
                                          /usr/share/biscuit/release (the
                                          APKBUILD's _release, beside its build,
                                          device-amazon-biscuit 6-r<N>). An image
                                          without that file is named r<N>, as
                                          every build before v1.0 was. --build
                                          only asserts r<N>.
    amazon-biscuit-backup.zip             the tools: versioned on their own. The
    amazon-biscuit-restore.zip            version is inside each zip (zip.prop,
    amazon-biscuit-stock-restore.zip      printed first in TWRP, and a backup's
                                          made_by line), not in its file name: a
                                          release carries the tool zips beside
                                          its install zip, and a version changes
                                          only when that zip's contents do

A public release name belongs to one build. releases.json, beside this script,
records which: a name already recorded for another build is refused (bump
_release in the APKBUILD), and a new name is only recorded with --record.

Each zip holds its update-binary, bin/busybox and bin/biscuit-tool (static
armv7, for TWRP), common/functions.sh, and zip.prop: its own name and version,
which it prints first. Each also carries the licence files from the top of the
installer (installer/ in pmaports-biscuit): LICENSE, NOTICE (the third-party code in the two binaries, and
where busybox's source is) and COPYING.GPL-2.0. backup also gets assets.list: the package manifest's
accepted files as profile|name|bytes|sha256, so the backup takes a file by
content, whatever Fire OS called it. install also gets payload/boot.img and
payload/pmos.img.xz, which it streams to the eMMC, and payload/SHA256SUMS, which
it checks both against.

Before anything is written, the image is checked: boot.img must be an Android
boot image with 2048-byte pages whose kernel starts with MediaTek's header
(magic 0x58881688, name KERNEL), name the image's own pmOS_boot and pmOS_root
filesystems, and carry bootopt ...,64N2.
The build number and the manifest are read from the image's root filesystem
with debugfs; on a host without it, --build and --manifest are required.

A tool zip's contents are compared with tool-versions.json. The same version
with different contents is refused (bump TOOL_VERSIONS), and so is a new version
with the same contents; a new version is only written to the file with --record.
The zips are reproducible (fixed times, Unix attributes on any host), so a
recorded version must also rebuild to the recorded bytes. OUTDIR/SHA256SUMS lists
the zips built; a build refused part-way removes it.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
# Carried at the top of every zip: busybox is GPL-2.0, and NOTICE says where
# its source is and lists the libraries linked into both binaries.
LICENCE_FILES = ("LICENSE", "NOTICE", "COPYING.GPL-2.0")
# The tool zips' versions. Bump one when build_zips refuses to build it: its
# update-binary, common/functions.sh, bin/, the licence files or (backup)
# assets.list changed.
TOOL_VERSIONS = {"backup": 1, "restore": 1, "stock-restore": 1}
NAMES = {
    "install": "pmos-amazon-biscuit",
    "backup": "amazon-biscuit-backup",
    "restore": "amazon-biscuit-restore",
    "stock-restore": "amazon-biscuit-stock-restore",
}
ZIPS = ("backup", "install", "restore", "stock-restore")
LOCK = os.path.join(HERE, "tool-versions.json")
RELEASES = os.path.join(HERE, "releases.json")
# Written by the device package: release=v<X.Y>[.<Z>] and build=<pkgver>-r<pkgrel>.
RELEASE_IN_IMAGE = "/usr/share/biscuit/release"
# The install zip is pushed to TWRP's /tmp, a tmpfs of half the Echo's RAM
# (241 MiB), and read from there while the eMMC is rewritten.
INSTALL_ZIP_MAX = 200 << 20
CORE = "device-amazon-biscuit"
MANIFEST_IN_IMAGE = "/usr/share/biscuit/biscuit-profile-assets.json"
# The same manifest in this repository, beside the installer. --tools-only
# reads it when no --manifest is given.
MANIFEST_IN_REPO = os.path.join(os.path.dirname(TOP), "device", "testing", CORE, "biscuit-profile-assets.json")
# The first four bytes of MediaTek's kernel header, 0x58881688 little-endian.
MTK_MAGIC = struct.pack("<I", 0x58881688)


def sha256(path=None, data=None):
    h = hashlib.sha256()
    if data is not None:
        h.update(data)
        return h.hexdigest()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read(path):
    with open(path, "rb") as f:
        return f.read()


def add(z, arc, path=None, data=None, mode=0o644, store=False):
    info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
    # Unix, whatever builds it: Python writes 0 (MS-DOS) on Windows, which
    # changes every zip's bytes and tells unzip to ignore the mode below.
    info.create_system = 3
    info.external_attr = (0o100000 | mode) << 16
    info.compress_type = zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED
    # Whole-member writes, never ZIP64: TWRP's zip reader (minzip) rejects
    # ZIP64 extra fields and data descriptors with "Invalid zip file format",
    # and nothing here comes near 4 GiB.
    if data is None:
        data = read(path)
    z.writestr(info, data)


def assets_list(manifest_bytes):
    lines = []
    for profile, spec in sorted(json.loads(manifest_bytes)["profiles"].items()):
        for item in spec.get("files", []):
            for v in item.get("variants") or [{"bytes": item["bytes"], "sha256": item["sha256"]}]:
                lines.append("%s|%s|%d|%s" % (profile, item["name"], v["bytes"], v["sha256"]))
    return "\n".join(lines) + "\n"


# --- the image -----------------------------------------------------------------

def gpt_filesystems(image):
    """{label: (first_sector, fs_uuid)} for the image's ext2/3/4 partitions,
    read from its GPT. pmbootstrap's images are GPT: the MBR holds only the
    protective entry, so an MBR reading finds no filesystems at all."""
    out = {}
    with open(image, "rb") as f:
        f.seek(512)
        hdr = f.read(92)
        if hdr[:8] != b"EFI PART":
            sys.exit("%s has no GPT" % image)
        ent_lba, nent, esize = struct.unpack_from("<QII", hdr, 72)
        f.seek(ent_lba * 512)
        ents = f.read(nent * esize)
        for i in range(nent):
            e = ents[i * esize:(i + 1) * esize]
            if e[:16] == bytes(16):
                continue
            first = struct.unpack_from("<Q", e, 32)[0]
            f.seek(first * 512 + 1024)
            sb = f.read(1024)
            if len(sb) < 0x88 or struct.unpack_from("<H", sb, 0x38)[0] != 0xEF53:
                continue
            label = sb[0x78:0x88].split(b"\0")[0].decode(errors="replace")
            out[label] = (first, str(uuid.UUID(bytes=sb[0x68:0x78])))
    return out


def check_image(image, boot):
    """The boot image must name the image's own filesystems, and start the
    kernel as arm64. Returns the root filesystem's first sector."""
    fs = gpt_filesystems(image)
    with open(boot, "rb") as f:
        hdr = f.read(1632)
    if hdr[:8] != b"ANDROID!":
        sys.exit("%s is not an Android boot image" % boot)
    # The kernel must start with MediaTek's 512-byte header (magic 0x58881688,
    # name KERNEL): LK will not start a bare kernel, and the Echo bootloops.
    # The install zip's biscuit-tool owner-boot refuses it too, but only on
    # the device; this refuses it before a zip is made. The kernel is at the
    # first page, and biscuit-tool reads only 2048-byte pages.
    page = struct.unpack_from("<I", hdr, 36)[0]
    if page != 2048:
        sys.exit("%s has a page size of %d, not 2048" % (boot, page))
    with open(boot, "rb") as f:
        f.seek(page)
        mtk = f.read(16)
    if len(mtk) < 16 or mtk[:4] != MTK_MAGIC or mtk[8:14] != b"KERNEL":
        sys.exit("%s: the kernel has no MediaTek header (magic 0x58881688, name KERNEL); "
                 "the Echo would bootloop" % boot)
    # The header's cmdline (64..576) and its extra_cmdline (608..1632).
    cmdline = (hdr[64:576].split(b"\0")[0] + hdr[608:1632].split(b"\0")[0]).decode()
    for key, label in (("pmos_boot_uuid", "pmOS_boot"), ("pmos_root_uuid", "pmOS_root")):
        want = re.search(r"\b%s=(\S+)" % key, cmdline)
        if label not in fs or not want or fs[label][1] != want.group(1):
            sys.exit("boot.img's %s is %s, the image's %s is %s: not a pair" % (
                key, want and want.group(1), label, fs.get(label, (0, None))[1]))
    # kaeru takes the kernel mode from bootopt's third field; 32N2 sends an
    # arm64 kernel down the zImage path and the Echo never starts.
    opt = re.search(r"\bbootopt=(\S+)", cmdline)
    if not opt or opt.group(1).split(",")[2:3] != ["64N2"]:
        sys.exit("boot.img's bootopt is %s, not ...,64N2: amonet v2 would not start it" % (
            opt and opt.group(1)))
    return fs["pmOS_root"][0]


def debugfs_path():
    return shutil.which("debugfs") or next(
        (p for p in ("/usr/sbin/debugfs", "/sbin/debugfs") if os.access(p, os.X_OK)), None)


def read_from_root(debugfs, image, root_first, path, missing_ok=False):
    """A file from the image's root filesystem, read with debugfs (read-only).
    With missing_ok, a file that is not there is None rather than an exit."""
    r = subprocess.run([debugfs, "-R", "cat " + path, "%s?offset=%d" % (image, root_first * 512)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    # debugfs reports a missing file on stderr and still exits 0.
    if not r.stdout:
        if missing_ok:
            return None
        why = r.stderr.decode(errors="replace").strip().splitlines()
        sys.exit("cannot read %s from the image: %s" % (path, why[-1] if why else "empty"))
    return r.stdout


def core_build(installed_db):
    """device-amazon-biscuit 6-r293 -> ("r293", "6-r293"), as the settings
    page's Device software and biscuit_system.release_tag read it."""
    for block in installed_db.decode(errors="replace").split("\n\n"):
        fields = dict(l.split(":", 1) for l in block.splitlines() if len(l) > 2 and l[1] == ":")
        if fields.get("P") == CORE:
            m = re.search(r"-r(\d+)$", fields.get("V", ""))
            if m:
                return "r" + m.group(1), fields["V"]
    sys.exit("the image has no %s package" % CORE)


def release_of(release_file, core_v):
    """The public release name in the image's /usr/share/biscuit/release, or
    None when the image has none (every build before v1.0). The file names its
    own build, and it must be the core package the image actually carries: a
    mismatch means the file came from somewhere else, and naming the zip after
    it would put one build's install under another's release."""
    if release_file is None:
        return None
    fields = dict(l.split("=", 1) for l in release_file.decode(errors="replace").splitlines() if "=" in l)
    name, build = fields.get("release", ""), fields.get("build", "")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+(\.[0-9]+)?", name):
        sys.exit("%s names the release %r, not v<X.Y> or v<X.Y.Z>" % (RELEASE_IN_IMAGE, name))
    if build != core_v:
        sys.exit("%s is for %s, but the image carries %s %s" % (RELEASE_IN_IMAGE, build, CORE, core_v))
    return name


def check_release(name, core_v, releases, record):
    """A release name is given to one build. Returns True when it registered
    a new name in `releases`."""
    had = releases.get(name)
    if had == core_v:
        return False
    if had:
        sys.exit("release %s is already %s %s; this image is %s. Set a new _release in the APKBUILD" % (
            name, CORE, had, core_v))
    if not record:
        sys.exit("release %s (%s) is a new release: build again with --record to register it" % (name, core_v))
    releases[name] = core_v
    return True


# --- the tool zips' versions ---------------------------------------------------

def content_of(members):
    """What decides a tool zip's version: every member but zip.prop, which only
    carries the version itself."""
    per = {arc: sha256(data=data) + " %o" % mode for arc, data, mode, _ in members if arc != "zip.prop"}
    return sha256(data="\n".join("%s %s" % kv for kv in sorted(per.items())).encode()), per


def check_tool(name, version, members, lock, record):
    """Refuses a tool zip whose version and contents disagree with the lock.
    Returns True when it registered a new version in `lock`."""
    content, per = content_of(members)
    had = lock.get(name)
    label = "%s v%d" % (NAMES[name], version)
    if had and version < had["version"]:
        sys.exit("%s: TOOL_VERSIONS says %d, tool-versions.json already has %d" % (label, version, had["version"]))
    if had and version == had["version"]:
        if content != had["content"]:
            changed = sorted(a for a in set(per) | set(had["members"]) if per.get(a) != had["members"].get(a))
            sys.exit("%s changed (%s) but its version did not: set TOOL_VERSIONS[%r] = %d" % (
                label, ", ".join(changed), name, version + 1))
        return False
    if had and content == had["content"]:
        sys.exit("%s: the version went up but nothing in the zip changed" % label)
    if not record:
        sys.exit("%s is a new version: build again with --record to register it" % label)
    lock[name] = {"version": version, "content": content, "members": per}
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--image")
    ap.add_argument("--boot")
    ap.add_argument("--out", required=True)
    ap.add_argument("--build", help="r<N>: asserted against the image's %s; required without debugfs" % CORE)
    ap.add_argument("--release", help="v<X.Y>: without debugfs, the release name the image carries (if any)")
    ap.add_argument("--manifest", help="default: the image's own " + MANIFEST_IN_IMAGE)
    ap.add_argument("--lock", default=LOCK)
    ap.add_argument("--releases", default=RELEASES)
    ap.add_argument("--record", action="store_true", help="register new tool versions and release names")
    ap.add_argument("--tools-only", action="store_true", help="build and check only the tool zips")
    a = ap.parse_args()
    if a.build and not re.fullmatch(r"r[0-9]+", a.build):
        sys.exit("--build is r<N>, as in r295")
    if a.release and not re.fullmatch(r"v[0-9]+\.[0-9]+(\.[0-9]+)?", a.release):
        sys.exit("--release is v<X.Y> or v<X.Y.Z>, as in v1.0")

    build = core_v = release = None
    if a.tools_only:
        if not a.manifest:
            if not os.path.isfile(MANIFEST_IN_REPO):
                sys.exit("--tools-only needs --manifest (no %s here)" % MANIFEST_IN_REPO)
            a.manifest = MANIFEST_IN_REPO
            print("(manifest: %s)" % MANIFEST_IN_REPO)
        manifest = read(a.manifest)
    else:
        if not (a.image and a.boot):
            sys.exit("--image and --boot are required (or --tools-only)")
        root_first = check_image(a.image, a.boot)
        debugfs = debugfs_path()
        if debugfs:
            build, core_v = core_build(read_from_root(debugfs, a.image, root_first, "/lib/apk/db/installed"))
            if a.build and a.build != build:
                sys.exit("--build %s, but the image carries %s %s" % (a.build, CORE, core_v))
            release = release_of(read_from_root(debugfs, a.image, root_first, RELEASE_IN_IMAGE, missing_ok=True), core_v)
            if a.release and a.release != release:
                sys.exit("--release %s, but the image carries %s" % (a.release, release or "no release name"))
            manifest = read(a.manifest) if a.manifest else read_from_root(
                debugfs, a.image, root_first, MANIFEST_IN_IMAGE)
        else:
            if not (a.build and a.manifest):
                sys.exit("no debugfs here to read the image: give --build and --manifest (and --release if it has one)")
            print("(no debugfs: %s%s taken from the arguments, not checked against the image)" % (
                a.build, " " + a.release if a.release else ""))
            build, release, manifest = a.build, a.release, read(a.manifest)
            # The ledger records the core package's version; without the image
            # that is only known as 6-r<N> by this package's own convention.
            core_v = "6-" + build if release else None
    alist = assets_list(manifest)

    try:
        lock = json.load(open(a.lock))
    except FileNotFoundError:
        lock = {}
    try:
        releases = json.load(open(a.releases))
    except FileNotFoundError:
        releases = {}
    # Checked before anything is built, like the tool versions below. The
    # install zip is named after the release, or after the build before v1.0.
    new_release = bool(release) and check_release(release, core_v, releases, a.record)
    label = release or build

    def members_of(name, version):
        return [
            ("META-INF/com/google/android/update-binary", read(os.path.join(HERE, name, "update-binary")), 0o755, False),
            ("META-INF/com/google/android/updater-script", b"# dummy; see update-binary\n", 0o644, False),
            ("zip.prop", ("ZIP_NAME=%s\nZIP_VERSION=%s\n" % (NAMES[name], version)).encode(), 0o644, False),
            ("bin/busybox", read(os.path.join(HERE, "bin", "busybox")), 0o755, False),
            ("bin/biscuit-tool", read(os.path.join(HERE, "bin", "biscuit-tool")), 0o755, False),
            ("common/functions.sh", read(os.path.join(HERE, "common", "functions.sh")), 0o644, False),
        ] + [(f, read(os.path.join(TOP, f)), 0o644, False) for f in LICENCE_FILES] + ([("assets.list", alist.encode(), 0o644, False)] if name == "backup" else [])

    # Every tool zip is checked before anything is compressed or written.
    plan, recorded = [], False
    for name in ZIPS:
        if name == "install":
            if not a.tools_only:
                plan.append((name, label, None))
            continue
        members = members_of(name, "v%d" % TOOL_VERSIONS[name])
        recorded |= check_tool(name, TOOL_VERSIONS[name], members, lock, a.record)
        plan.append((name, "v%d" % TOOL_VERSIONS[name], members))
    os.makedirs(a.out, exist_ok=True)

    # Each zip is written under a temporary name and renamed once its checks
    # pass. A refusal from here on leaves no half-built set: the temporary, the
    # xz and OUTDIR/SHA256SUMS go, so the folder cannot pass for a release.
    sums = os.path.join(a.out, "SHA256SUMS")
    xz = payload_index = tmp = None
    built = []
    try:
        if not a.tools_only:
            size = os.path.getsize(a.image)
            if size % (1 << 20):
                sys.exit("the disk image is not a whole number of MiB")
            xz = os.path.join(a.out, "pmos.img.xz")
            print("compressing %s (%d MiB) ..." % (a.image, size >> 20), flush=True)
            # xz, not gzip: a third smaller, which is what lets the zip fit TWRP's /tmp.
            # CRC32 and the default filter are what busybox's unxz reads; a fixed thread
            # count keeps the block layout, and so the zip, reproducible.
            with open(a.image, "rb") as src, open(xz, "wb") as out:
                subprocess.run(["xz", "-6", "-T4", "--check=crc32", "-c"], stdin=src, stdout=out, check=True)
            if os.path.getsize(xz) > INSTALL_ZIP_MAX:
                sys.exit("the compressed image alone is %d bytes, over %d MiB: the install zip would not fit TWRP's /tmp" % (
                    os.path.getsize(xz), INSTALL_ZIP_MAX >> 20))
            # version= is what the zip calls itself; build= and core= are what it
            # installs. The install zip reads only core=, pmos.img and boot.img.
            payload_index = "version=%s\n%s%spmos.img %s %d\nboot.img %s %d\n" % (
                label, "build=%s\n" % build if release else "", "core=%s\n" % core_v if core_v else "",
                sha256(a.image), size, sha256(a.boot), os.path.getsize(a.boot))

        for name, version, members in plan:
            if name == "install":
                members = members_of(name, version) + [
                    ("payload/SHA256SUMS", payload_index.encode(), 0o644, False),
                    ("payload/boot.img", read(a.boot), 0o644, False)]
            # Only the install zip carries its version in the file name; see
            # the docstring for why the tool zips do not.
            path = os.path.join(a.out, ("%s-%s.zip" % (NAMES[name], version)) if name == "install"
                                else "%s.zip" % NAMES[name])
            tmp = path + ".tmp"
            with zipfile.ZipFile(tmp, "w") as z:
                for arc, data, mode, store in members:
                    add(z, arc, data=data, mode=mode, store=store)
                if name == "install":
                    add(z, "payload/pmos.img.xz", xz, store=True)
            digest = sha256(tmp)
            if name != "install":
                # The same contents must give the same file: a vN once published never changes.
                had = lock[name].get("sha256")
                if had and had != digest:
                    sys.exit("%s: same contents as recorded, but the zip's bytes differ (%s, recorded %s)" % (
                        os.path.basename(path), digest, had))
                if not had and a.record:
                    lock[name]["sha256"] = digest
                    recorded = True
            print("%-44s %11d bytes  sha256 %s" % (os.path.basename(path), os.path.getsize(tmp), digest))
            if name == "install" and os.path.getsize(tmp) > INSTALL_ZIP_MAX:
                sys.exit("the install zip is over %d MiB: it would not fit TWRP's /tmp" % (INSTALL_ZIP_MAX >> 20))
            os.replace(tmp, path)
            tmp = None
            built.append((os.path.basename(path), digest))
        with open(sums, "w", newline="\n") as f:
            f.writelines("%s  %s\n" % (d, n) for n, d in sorted(built))
    except BaseException:
        for p in (tmp, sums):
            if p and os.path.exists(p):
                os.remove(p)
        raise
    finally:
        if xz and os.path.exists(xz):
            os.remove(xz)
    if recorded:
        with open(a.lock, "w", newline="\n") as f:
            json.dump(lock, f, indent=1, sort_keys=True)
            f.write("\n")
        print("recorded new tool versions in %s" % a.lock)
    if new_release:
        with open(a.releases, "w", newline="\n") as f:
            json.dump(releases, f, indent=1, sort_keys=True)
            f.write("\n")
        print("recorded release %s = %s %s in %s" % (release, CORE, core_v, a.releases))
    if payload_index:
        print(payload_index, end="")


if __name__ == "__main__":
    main()
