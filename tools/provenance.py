#!/usr/bin/env python3
"""Generate PROVENANCE.md from the binary artifacts this repository actually ships.

A hand-written manifest goes stale the first time a virtualenv is rebuilt and
nobody notices, which is the failure that already cost this project a history
rewrite: a superseded venv tarball shipped an `av` wheel bundling GPL
libx264/libx265 with no corresponding source, and it was found by reading the
tarball rather than by reading a document. So this reads the tarballs and
writes the document, and can be re-run after any rebuild.

What it checks, per bundled Python distribution:

  * name, version and declared licence, from the dist-info METADATA - the
    SPDX License-Expression first, which newer wheels use instead of License:
    (reading only License: is what reported zeroconf, an LGPL package, as
    UNSTATED)
  * whether a LICENSE/COPYING text is actually shipped with it, because for a
    copyleft dependency that text is part of the obligation, not a nicety
  * whether the package ships source (.py) or only compiled objects - for a GPL
    dependency, pure-Python IS the corresponding source, and a compiled-only one
    would be an obligation we are not meeting

Native shared objects are reported separately, with the dynamic libraries they
name, plus any library a wheel bundles in a *.libs directory (the GCC runtime
among them). That is what settles the FFmpeg question: PyAV's extensions
reference libavcodec.so.62 and friends rather than containing them, so FFmpeg
comes from Alpine's ffmpeg-libs at runtime and no FFmpeg code is redistributed.

The owner-imported vendor files are listed from biscuit-profile-assets.json,
and each is checked to be ABSENT from the tree. The measured stock parameters
and the code taken from other projects are described in the tables below, and
the kernel patch count is read from the kernel APKBUILD.

Run from the repository root:  python3 tools/provenance.py > PROVENANCE.md
(or pass the repository root as the first argument).
"""
from __future__ import annotations
import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tarfile

ROOT = (pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1
        else pathlib.Path(__file__).resolve().parent.parent)
PKG = ROOT / "device" / "testing" / "device-amazon-biscuit"
KERNEL = ROOT / "device" / "testing" / "linux-amazon-biscuit"

PUBLISHED = "PUBLISHED in the public apk feed as part of %s"

# Tarballs carrying third-party code, and what each one is for.
BUNDLES = [
    ("biscuit-voice-venv.tar.gz",
     "Voice assistant virtualenv - linux-voice-assistant and its dependencies. "
     "Its copy of linux-voice-assistant (Apache-2.0) is already modified: at "
     "build time the package copies the changed files from `lva-src/` over it, "
     "applies the `biscuit-lva-*.patch.txt` patches and adds a change notice "
     "to `models.py`, so every changed or new file says so. See "
     "\"Code from other projects\" below for the list.",
     PUBLISHED % "`device-amazon-biscuit-voice`"),
    ("biscuit-sendspin-venv.tar.gz",
     "Sendspin virtualenv - the media client and its dependencies. The three "
     "`sendspin-*.diff` files (Apache-2.0) patch it at build time.",
     PUBLISHED % "`device-amazon-biscuit-sendspin`"),
]

# Non-Python binary artifacts, which carry no dist-info and are described here.
NATIVE = [
    ("tflite-aarch64-musl.tar.gz",
     "TensorFlow Lite 2.17.0's C API, built by this project for aarch64/musl "
     "because both wake engines otherwise carry an x86-64/glibc "
     "libtensorflowlite_c.so that cannot load here. " + PUBLISHED %
     "`device-amazon-biscuit-voice` (installed to /usr/lib/biscuit-tflite)" + ".",
     "Unmodified upstream components: TensorFlow Lite, Abseil, ruy, gemmlowp, "
     "flatbuffers and ml_dtypes (Apache-2.0); Eigen (MPL-2.0, header-only, "
     "compiled in; source at gitlab.com/libeigen/eigen); farmhash and FXdiv "
     "(MIT); cpuinfo and pthreadpool (BSD-2-Clause); Ooura's FFT (its own "
     "permissive licence). XNNPACK is not built in (TFLITE_ENABLE_XNNPACK=OFF; "
     "the library has no xnn_ code). Nothing in it is Amazon's."),
    ("biscuit-beam-weights.bin",
     "Beamformer weights: six-beam MVDR (superdirective) design on the "
     "1024-point runtime grid.",
     "MIT, this project. Computed from the measured array geometry and a "
     "diffuse-field noise model by the project's weight generator "
     "(tools/beamform/biscuit-superdirective.py, then export-beam-weights.py "
     "with the neutral array file, unity gains); it regenerates to within "
     "about 3e-15, the last bits depending on the numpy build. Not derived "
     "from any vendor file, and carries no unit's calibration."),
    ("biscuit-beam-weights-stock.bin",
     "Beamformer weights for the optional stock-profile path: the same MVDR "
     "design on the stock filterbank's 128-point, hop-64 grid.",
     "MIT, this project. Computed by the same generator (--fft 128 --hop 64); "
     "it regenerates to within about 3e-15. It is our design, not stock's: compared "
     "with Fire OS 5's coefs_FBF.cfg only chance-level values coincide, and the "
     "longest exact run is 2 values."),
    ("biscuit-fireos6-frontend.tar.gz",
     "This project's Fire OS 6-compatible microphone front end, as source.",
     "MIT, this project. A reimplementation of observed Fire OS 6 behaviour: it "
     "embeds no Amazon code or data, and reads the owner-imported `fireos6` "
     "files at run time. Its default AEC step table (aec_table.h) is a "
     "generated logistic curve, not stock's."),
]

# Numeric parameters measured from, or read out of, stock software on the
# owner's own device. Kept by decision and labelled: a handful of facts in our
# own code and formats, never a copied file.
MEASURED = [
    ("`biscuit-audio.py` `STOCK_AVL_DB`",
     "the 30 volume steps (dB), reimplementing stock's speaker AVL steps",
     "the owner's AFE.cfg"),
    ("`mbcl.conf`, `biscuit-dsp.c` defaults",
     "crossovers, per-band thresholds, ratios, limiter thresholds and "
     "releases; the per-volume loudness push (push_80/90/100_db). Attack "
     "times are ours",
     "stock MBCL.cfg; the push measured from the scale between stock's "
     "per-volume EQ files"),
    ("`biscuit-als.py`",
     "sensor gain and integration time, the lux equation constants, the "
     "response-curve coefficients {0, 915, 252, -168}/1000 and the 101-step "
     "brightness ladder `STOCK_LADDER`",
     "Amazon's GPL vendor kernel driver (tsl2583.c); the ladder and curve "
     "observed in stock's libled_hal.so"),
    ("`biscuit-beamform.c` `STOCK_*` constants",
     "smoothing factors, thresholds, hangovers and band limits of the optional "
     "stock-profile selector and canceller paths (-S, -T, -U, -V, -R)",
     "the owner's AFE.cfg, or measured from stock libasp.so's behaviour"),
    ("`biscuit-ring-fx.py` ambient looks",
     "timings, 4-bit colour levels and densities of the 15 effects in the "
     "style of stock's zzz_ set; no frame data",
     "measured from the stock animation files on the owner's device"),
    ("`biscuit-va-leds.py` activity defaults",
     "which generated effect, colour and speed each assistant state uses",
     "measured from stock's activity animations (peak and resting colour, "
     "lit segments, rotation)"),
    ("`biscuit-ring-priorities.json`, earcon and animation name tables",
     "names and ordering only",
     "stock's file names; the tables themselves were written from scratch"),
]

# Code in this repository that comes from other projects.
FOREIGN = [
    ("`device/testing/linux-amazon-biscuit/*.patch`",
     "GPL-2.0-only",
     "this project's patches against bengris32/linux-mtk (branch mt8163/7.0, "
     "commit 20951722df6a); PATCHES of them are applied by the APKBUILD"),
    ("`device/testing/linux-amazon-biscuit/mt6625l-wlan-20260823.tar.gz`",
     "GPL-2.0",
     "MediaTek's MT6625L WLAN driver from Amazon's GPL MT8163 3.18 kernel "
     "source release, modified: of its 159 C sources and headers, 133 are "
     "unchanged, 24 "
     "were changed for Linux 7.0, 2 are new, and the Kconfig and Makefile "
     "were written for this port. Branch biscuit-r243 of liamtw22/linux-mtk "
     "commits Amazon's original files first and this port's changes on top"),
    ("`lva-src/*.py`, `biscuit-lva-*.patch.txt`, `biscuit-peripheral-*.patch.txt`",
     "Apache-2.0",
     "OHF-Voice/linux-voice-assistant at b0c53c41c11e, as the -voice package "
     "installs it. Modified, each with a MODIFIED FILE notice: `__main__.py`, "
     "`entity.py`, `satellite.py`, `peripheral_api.py`, `mpv_player.py` and "
     "`player/libmpv.py` (whole files from `lva-src/`), and `models.py` "
     "(changed in the venv's copy and by two `.patch.txt` patches; its notice "
     "is added at build time). New, each with a NEW FILE notice: "
     "`peripheral_transport.py`, `qualification_v1.py`. Every other file is "
     "byte-identical to upstream"),
    ("`sendspin-*.diff`",
     "Apache-2.0",
     "patches against Sendspin/sendspin-cli (22b9ff308afe) and aiosendspin "
     "6.0.5"),
    ("`btproxy/api_pb2.py`, `btproxy/api_options_pb2.py`",
     "MIT",
     "aioesphomeapi 45.3.1, unmodified; Copyright (c) 2018 Otto Winter, full "
     "notice in btproxy/__init__.py"),
    ("`nacl-fast.min.js`",
     "Unlicense (public domain)",
     "TweetNaCl-js 1.0.3, unmodified"),
    ("`pulse-daemon-biscuit.conf`, `pulse-default-biscuit.pa`",
     "MIT",
     "postmarketOS pmaports' archived device-amazon-biscuit, unmodified "
     "(postmarketOS contributors); `deviceinfo`, `ucm2/HiFi.conf`, "
     "`ucm2/mt8163_biscuit.conf` and the APKBUILD started from the same "
     "package"),
    ("`base-pmaports/*.patch`",
     "GPL-3.0 (pmaports' licence)",
     "patches against postmarketOS pmaports 1ab5c4ed7. 0002 changes "
     "boot-deploy (GPL-2.0-or-later) and is the source of the image's "
     "boot-deploy 0.24.0-r1; 0003 changes postmarketos-zram "
     "(GPL-3.0-or-later); 0001 only deletes upstream files, which keep "
     "their licences and authors"),
]

COPYLEFT = re.compile(r"\bA?GPL|LGPL|MPL|EUPL|CDDL|EPL\b|Lesser General|General Public", re.I)
SONAME = re.compile(rb"lib[a-z0-9_+\-]+\.so(?:\.[0-9]+)*")
LICFILE = re.compile(r"(LICEN[SC]E|COPYING|NOTICE)[^/]*$|/[A-Za-z0-9.+-]*-[0-9.]+\.txt$", re.I)
GCC_LICENCE = ("GPL-3.0-or-later WITH GCC-exception-3.1 (the GCC runtime library), "
               "bundled unmodified by the wheel named in its path; this project "
               "did not build it")


def gcc_source(gcc, cross):
    """Where the corresponding source of one bundled libgcc_s is, from what the
    object itself records: its GCC comment, and musl-cross-make's `src_gcc/`
    paths when the toolchain was built with musl-cross-make."""
    m = re.search(r"\(([^)]*)\) ([0-9.]+)", gcc or "")
    if not m:
        return ("the object records no GCC version: **REVIEW**")
    vendor, version = m.group(1), m.group(2)
    upstream = "https://ftp.gnu.org/gnu/gcc/gcc-%s/" % version
    if vendor.startswith("Alpine"):
        return ("built by Alpine Linux's gcc %s package. The object does not "
                "record Alpine's package release, so no single aports commit "
                "can be named: the source is GCC %s (%s) with the patches in "
                "Alpine's aports `main/gcc` for pkgver %s "
                "(https://gitlab.alpinelinux.org/alpine/aports/-/tree/master/main/gcc "
                "and its history)" % (version, version, upstream, version))
    if cross:
        return ("built by a GCC %s cross toolchain made with musl-cross-make "
                "(the source paths it records, `src_gcc/`, are musl-cross-make's "
                "layout): the "
                "source is GCC %s (%s) with musl-cross-make's patches for that "
                "version (https://github.com/richfelker/musl-cross-make, "
                "`patches/gcc-%s/`)" % (version, version, upstream, version))
    return "the source is GCC %s, %s" % (version, upstream)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def field(raw, key):
    m = re.search(r"^%s:\s*(.+)$" % key, raw, re.M | re.I)
    return m.group(1).strip() if m else ""


def licence_of(raw):
    head = raw.split("\n\n", 1)[0]
    expression = field(head, "License-Expression")
    if expression:
        return expression
    declared = field(head, "License")
    classifiers = re.findall(r"^Classifier:\s*License ::\s*(.+)$", head, re.M)
    classifiers = [c for c in classifiers if "Proprietary" not in c]
    # A declared License: can be the whole licence text. Prefer the classifier
    # when the declared value is clearly prose rather than an identifier.
    if declared and len(declared) <= 60:
        return declared
    if classifiers:
        return " / ".join(c.replace("OSI Approved :: ", "") for c in classifiers)
    return declared[:60] or "UNSTATED"


def norm(name):
    return re.sub(r"[-_.]+", "_", name).lower()


def survey(path):
    """Return (distributions, native_objects, bundled_libs) for one venv tarball."""
    dists, native, records = {}, {}, {}
    with tarfile.open(path, "r:gz") as tf:
        names = tf.getnames()
        licence_files = [n for n in names if LICFILE.search(n)]
        for m in tf:
            if not m.isfile():
                continue
            if m.name.endswith(".so") or ".so." in m.name:
                data = tf.extractfile(m).read()
                needed = sorted({s.decode() for s in SONAME.findall(data)
                                 if b"lib" in s})
                gcc = re.search(rb"GCC: \(([^)]*)\) ([0-9.]+)", data)
                machine = int.from_bytes(data[18:20], "little") if data[:4] == b"\x7fELF" else 0
                cross = b"src_gcc/" in data
                native[m.name.split("site-packages/")[-1]] = (
                    m.size, needed, gcc.group(0).decode() if gcc else "", machine, cross)
                continue
            if re.search(r"\.dist-info/RECORD$", m.name):
                records[m.name] = tf.extractfile(m).read().decode("utf-8", "replace")
                continue
            if re.search(r"\.dist-info/METADATA$", m.name):
                raw = tf.extractfile(m).read().decode("utf-8", "replace")
                name = field(raw, "Name") or m.name
                dist_dir = m.name.rsplit("/", 1)[0]
                # A copy another package vendors (setuptools/_vendor/...) is
                # not an installed distribution. Keyed apart, so it can never
                # overwrite the top-level one of the same name - which is how
                # setuptools' packaging 24.1 once hid the installed 26.3.
                vendor = re.search(r"site-packages/([^/]+)/_vendor/", m.name)
                # Shipped with the distribution, or supplied by this project in
                # a directory named after it.
                has_licence = any(
                    n.startswith(dist_dir + "/") or
                    norm(name) in [norm(part) for part in n.split("/")[:-1]]
                    for n in licence_files)
                key = ("%s (vendored in %s)" % (name, vendor.group(1))
                       if vendor else name)
                dists[key] = {
                    "version": field(raw, "Version"),
                    "licence": licence_of(raw),
                    "licence_text": has_licence,
                    "dist_dir": dist_dir,
                    "vendored": bool(vendor),
                }
        # Count shipped .py per distribution from its own RECORD, which lists
        # exactly what it installed. Guessing module names from the
        # distribution name does NOT work (python-mpv's module is mpv.py).
        for name, info in dists.items():
            raw = records.get(info["dist_dir"] + "/RECORD", "")
            info["py_files"] = sum(
                1 for line in raw.splitlines()
                if line.split(",")[0].endswith(".py")
                and ".dist-info/" not in line.split(",")[0])
        bundled = {n: native[n] for n in native if re.search(r"^[^/]+\.libs/", n)}
    return dists, native, bundled, names


def tar_licences(path):
    with tarfile.open(path, "r:gz") as tf:
        return sorted(n for n in tf.getnames() if LICFILE.search(n)
                      or re.search(r"(^|/)(readme[^/]*\.txt|THIRD[^/]*)$", n, re.I))


def patch_count():
    text = (KERNEL / "APKBUILD").read_text(encoding="utf-8")
    src = re.search(r'^source="(.*?)"', text, re.S | re.M).group(1)
    return sum(1 for t in src.split() if t.endswith(".patch"))


def tracked_anywhere(filename):
    """Tracked by git anywhere in the tree (untracked, ignored copies on a
    developer's disk are not what gets published), or present on disk when
    this is not a git checkout."""
    base = pathlib.PurePosixPath(filename).name
    try:
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                                capture_output=True, text=True, check=True).stdout
        return [n for n in listed.splitlines() if n.rsplit("/", 1)[-1] == base]
    except (OSError, subprocess.CalledProcessError):
        return [str(p) for d in (PKG, KERNEL) for p in d.rglob(base)]


def extra_firmware_set():
    """True if any non-comment line of the kernel APKBUILD sets EXTRA_FIRMWARE."""
    text = (KERNEL / "APKBUILD").read_text(encoding="utf-8")
    return any("EXTRA_FIRMWARE" in line and not line.lstrip().startswith("#")
               for line in text.splitlines())


def main():
    out = []
    w = out.append
    w("# PROVENANCE")
    w("")
    w("Every third-party binary artifact in this repository, every vendor file")
    w("the device uses but this repository does NOT contain, the parameters")
    w("measured from stock, and the code that comes from other projects: what")
    w("each is, under what licence, and whether its obligations are met. The")
    w("full licence texts for this repository's own files are in `LICENSES/`.")
    w("")
    w("**Generated by `tools/provenance.py`. Re-run it after rebuilding any")
    w("bundle** - a stale hand-written manifest is what let a wheel bundling")
    w("GPL libx264/libx265 into this tree once already.")
    w("")
    w("Generated %s." % datetime.date.today().isoformat())
    w("")

    all_copyleft, all_gcc = [], []
    for filename, what, exposure in BUNDLES:
        path = PKG / filename
        w("## %s" % filename)
        w("")
        if not path.exists():
            w("**MISSING FROM THE TREE** - cannot be surveyed.")
            w("")
            continue
        dists, native, bundled, names = survey(path)
        w(what)
        w("")
        w("- %d bytes, sha256 `%s`" % (path.stat().st_size, sha256(path)))
        vendored = sum(1 for d in dists.values() if d["vendored"])
        w("- %d Python distributions%s, %d native objects"
          % (len(dists) - vendored,
             " (plus %d vendored inside another package, listed with it)" % vendored
             if vendored else "", len(native)))
        w("- Exposure: %s" % exposure)
        w("")
        w("| Distribution | Version | Licence | Licence text shipped |")
        w("|---|---|---|---|")
        for name in sorted(dists, key=str.lower):
            d = dists[name]
            mark = "yes" if d["licence_text"] else "**NO**"
            w("| `%s` | %s | %s | %s |" % (name, d["version"], d["licence"], mark))
        w("")
        copyleft = {n: d for n, d in dists.items() if COPYLEFT.search(d["licence"])}
        if copyleft:
            all_copyleft.append((filename, exposure, copyleft))
            w("### Copyleft dependencies in this bundle")
            w("")
            for name in sorted(copyleft, key=str.lower):
                d = copyleft[name]
                w("- **`%s` %s - %s.** Licence text shipped: %s. "
                  "Python source files shipped: %d."
                  % (name, d["version"], d["licence"],
                     "yes" if d["licence_text"] else "**NO**", d["py_files"]))
            w("")
        content = []
        # openWakeWord's pretrained wake-word models only. Its shared feature
        # models (melspectrogram, embedding_model) are not among them, and
        # microWakeWord's models of the same names (pymicro_wakeword/models,
        # wakewords/*.tflite) are Apache-2.0.
        oww = [n for n in names if re.search(r"(openWakeWord|pyopen_wakeword)/", n)
               and re.search(r"(alexa|hey_jarvis|hey_mycroft|hey_rhasspy|ok_nabu)"
                             r"[^/]*\.(tflite|onnx)$", n)]
        if oww:
            content.append("**%d openWakeWord pretrained model files (CC-BY-NC-SA-4.0, "
                           "non-commercial) are in this bundle: REVIEW**" % len(oww))
        if any(n.endswith("site-packages/sounds/LICENSE.md") for n in names):
            content.append("linux-voice-assistant's `sounds/` (CC-BY-4.0, Clayton "
                           "Charles Tapp); its LICENSE.md is shipped beside them.")
        if content:
            w("### Non-code content")
            w("")
            for line in content:
                w("- " + line)
            w("")
        if bundled:
            w("### Libraries bundled by wheels")
            w("")
            for obj in sorted(bundled):
                size, _needed, gcc, _machine, cross = bundled[obj]
                if re.search(r"/libgcc_s", obj):
                    all_gcc.append((filename, obj, gcc, cross))
                    have = [n for n in names if re.search(r"(COPYING3|COPYING\.RUNTIME)$", n)]
                    w("- `%s` (%d bytes%s) - %s. Corresponding source: %s. "
                      "Licence texts in the bundle: %s."
                      % (obj, size, ", " + gcc if gcc else "", GCC_LICENCE,
                         gcc_source(gcc, cross),
                         "yes" if len(have) >= 2 else "**NO**"))
                else:
                    w("- `%s` (%d bytes) - covered by its wheel's own licence file."
                      % (obj, size))
            w("")
        if native:
            w("### Native objects and the libraries they require")
            w("")
            external = {}
            for obj, (size, needed, _gcc, _machine, _cross) in native.items():
                for lib in needed:
                    if not any(obj.startswith(p) for p in ("PIL/", "pillow.libs/")):
                        external.setdefault(lib, 0)
                        external[lib] += 1
            interesting = {k: v for k, v in external.items()
                           if re.search(r"libav|libsw|libx26|libssl|libcrypto", k)}
            if interesting:
                for lib in sorted(interesting):
                    w("- `%s` - referenced by %d object(s), **not bundled**; "
                      "resolved at runtime from the distribution's own package."
                      % (lib, interesting[lib]))
            else:
                w("- No FFmpeg, OpenSSL or GPL codec library is referenced.")
            foreign = [o for o in native if native[o][3] not in (0, 183)]
            if foreign:
                w("- Objects that are not aarch64 (unusable here; the package "
                  "build deletes or replaces them): %s."
                  % ", ".join("`%s`" % g for g in sorted(foreign)))
            w("")

    w("## Native artifacts without dist-info")
    w("")
    for filename, what, lic in NATIVE:
        path = PKG / filename
        w("### %s" % filename)
        w("")
        w(what)
        w("")
        if path.exists():
            w("- %d bytes, sha256 `%s`" % (path.stat().st_size, sha256(path)))
        else:
            w("- **MISSING FROM THE TREE**")
        w("- %s" % lic)
        if path.exists() and filename.endswith(".tar.gz") and "tflite" in filename:
            texts = tar_licences(path)
            w("- Licence texts in the tarball: %s"
              % ("%d files" % len(texts) if texts else "**NONE**"))
        w("")

    w("## Vendor files NOT in this repository (owner-imported)")
    w("")
    w("These are Amazon's or MediaTek's. None is in this repository, in any")
    w("package built from it, or in the install zips. Each owner exports them")
    w("from their own device and imports them with `biscuit-import-assets`,")
    w("which checks every file against these hashes. The kernel compiles no")
    w("firmware in (`CONFIG_EXTRA_FIRMWARE` is empty)%s; the `-nonfree-firmware`"
      % (" - **but the kernel APKBUILD sets EXTRA_FIRMWARE: REVIEW**"
         if extra_firmware_set() else ""))
    w("subpackage ships only the list that puts the owner's Bluetooth and FPGA")
    w("files into the initramfs.")
    w("")
    manifest = json.loads((PKG / "biscuit-profile-assets.json").read_text(encoding="utf-8"))
    w("| Profile | File | Bytes | sha256 | In this tree |")
    w("|---|---|---|---|---|")
    for profile in sorted(manifest["profiles"]):
        for item in manifest["profiles"][profile]["files"]:
            found = tracked_anywhere(item["name"])
            w("| %s | `%s` | %s | %s | %s |"
              % (profile, item["name"], item.get("bytes", "-"),
                 "`%s...`" % item["sha256"][:16] if item.get("sha256") else "-",
                 "**PRESENT - REMOVE**" if found else "no"))
    w("")
    w("The speaker correction curve (`EQ_50.cfg`) was shipped as")
    w("`speaker.fir` up to r293; r294 removed it%s."
      % ("" if not tracked_anywhere("speaker.fir")
         else " - **but speaker.fir is still tracked**"))
    w("")

    w("## Parameters measured from stock")
    w("")
    w("A handful of numeric parameters were measured from, or read out of, the")
    w("stock software on the owner's own device, so that this project's own")
    w("code can reimplement the behaviour an owner expects. They are kept in our")
    w("own code and formats; no stock code is included and no stock file is")
    w("copied.")
    w("")
    w("| Where | What | Measured from |")
    w("|---|---|---|")
    for where, what, source in MEASURED:
        w("| %s | %s | %s |" % (where, what, source))
    w("")

    w("## Code from other projects")
    w("")
    w("| Files | Licence | Origin |")
    w("|---|---|---|")
    count = patch_count()
    for files, lic, origin in FOREIGN:
        w("| %s | %s | %s |" % (files, lic, origin.replace("PATCHES", str(count))))
    w("")
    w("The kernel itself is downloaded at build time from")
    w("`https://github.com/bengris32/linux-mtk` at the pinned commit; the")
    w("patches, the driver tarball and that commit together are the source of")
    w("the published `linux-amazon-biscuit` package.")
    w("")

    w("## Summary of copyleft obligations")
    w("")
    w("Copyleft dependencies are redistributed **unmodified**, each with its own")
    w("licence text. For a pure-Python distribution the shipped `.py` files ARE")
    w("the corresponding source, so the GPL and LGPL source requirement is met by")
    w("the bundle itself. The exceptions, which are binaries, are listed with")
    w("their source.")
    w("")
    for filename, exposure, copyleft in all_copyleft:
        w("**%s** (%s):" % (filename, exposure))
        w("")
        for name in sorted(copyleft, key=str.lower):
            d = copyleft[name]
            ok = d["licence_text"] and d["py_files"] > 0
            w("- `%s` %s, %s - %s"
              % (name, d["version"], d["licence"],
                 "licence text and source both present"
                 if ok else "**REVIEW: licence text or source missing**"))
        w("")
    if all_gcc:
        w("**GCC runtime library** (binary):")
        w("")
        for filename, obj, gcc, cross in all_gcc:
            w("- `%s` in %s%s - %s"
              % (obj, filename, " (" + gcc + ")" if gcc else "",
                 gcc_source(gcc, cross)))
        w("")
        w(GCC_LICENCE + ".")
        w("")
    w("**Eigen** (MPL-2.0, header-only) is compiled into `libtensorflow-lite.so`")
    w("in `tflite-aarch64-musl.tar.gz`, unmodified; its source is the Eigen")
    w("commit TensorFlow 2.17.0 pins, at https://gitlab.com/libeigen/eigen.")
    w("")
    w("This repository's own code is MIT (see LICENSE). Bundling a GPL or LGPL")
    w("dependency does not relicense it, but the bundle as distributed carries")
    w("those dependencies' terms, and anyone redistributing the published apks")
    w("inherits them.")
    w("")
    # Bytes, not print(): Windows' text-mode stdout turns every \n into \r\n, so
    # "provenance.py > PROVENANCE.md" run there committed a CRLF file into an
    # LF-only tree.
    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
