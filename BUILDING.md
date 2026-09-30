# Building

This page builds everything a release contains, from source:

1. the pmaports tree the port sits on (`base-pmaports/`),
2. the kernel package `linux-amazon-biscuit`,
3. the device packages `device-amazon-biscuit` and its subpackages,
4. the disk image and boot image,
5. the four TWRP zips.

Everything below runs on a Linux x86_64 machine. The releases are built on an
Ubuntu 26.04 VM with 4 cores and pmbootstrap 3.10.1; other distributions that
pmbootstrap supports should work. Leave at least 20 GB free for pmbootstrap's
work directory. The kernel is cross-compiled; the device package's C programs are
compiled in an aarch64 chroot under qemu.

In the commands, `~/pmaports-biscuit` is a clone of this repository and
`~/pmaports-build` is the pmaports tree you build from.

## 1. The pmaports tree

**This repository is an overlay, not a buildable pmaports tree.** It contains
only `device/testing/device-amazon-biscuit` and
`device/testing/linux-amazon-biscuit`. A build needs upstream pmaports under it:
`pmaports.cfg`, `main/`, `cross/` and everything else. Pointing pmbootstrap at a
clone of this repository alone fails at once with
`ERROR: Invalid pmaports repository, could not find the config`.

The base is upstream pmaports at commit `1ab5c4ed7` plus three patches, which
are in [`base-pmaports/`](base-pmaports/README.md). One of them, the
`boot-deploy` fix, is required: without it the image does not boot.

```sh
git clone https://gitlab.postmarketos.org/postmarketOS/pmaports.git ~/pmaports-build
cd ~/pmaports-build
git checkout -b biscuit-base 1ab5c4ed75a362067e27a0238fd80612e3454717
git am ~/pmaports-biscuit/base-pmaports/*.patch

# pmbootstrap reads channels.cfg from origin/main, not from the working tree.
git fetch --depth 1 origin main

# Overlay this repository's two packages.
cp -a ~/pmaports-biscuit/device/testing/device-amazon-biscuit ~/pmaports-build/device/testing/
cp -a ~/pmaports-biscuit/device/testing/linux-amazon-biscuit  ~/pmaports-build/device/testing/
```

`pmaports.cfg` at that base says `version=7`, `channel=edge`. The packages
pmbootstrap installs from the binary repositories are current edge, whatever
the base is: see `base-pmaports/README.md` for why, and for the one check that
has to be repeated whenever upstream moves.

### Three requirements that are not obvious

Each of these was found by hitting it, and each produces an error that does not
name the real cause.

- **`origin` must be the real upstream URL.** pmbootstrap matches the remote
  against the gitlab URL, so a clone made from a local path is rejected with
  `could not find remote name for any URL`. Fix with
  `git remote set-url origin https://gitlab.postmarketos.org/postmarketOS/pmaports.git`.
- **`origin/main` must exist as a fetched ref.** Without it pmbootstrap fails
  with `Failed to read channels.cfg from 'origin/main' branch`. The
  `git fetch --depth 1 origin main` above is enough.
- **Always pass `-p ~/pmaports-build`.** Without it pmbootstrap silently uses
  whichever tree it was last configured with, builds something else entirely,
  and exits 0. Check that every `.apk` it produces carries the `pkgrel` you
  expect.

## 2. pmbootstrap settings

```sh
pmbootstrap -p ~/pmaports-build init
```

Choose vendor `amazon`, device `biscuit`, user interface `none`, systemd
`never`, and no extra packages. The resulting `pmbootstrap_v3.cfg` should
contain:

```ini
device = amazon-biscuit
ui = none
systemd = never
extra_packages = none
boot_size = 128
timezone = Etc/UTC
```

- **`extra_packages = none` matters.** The image ships the core package only.
  The voice assistant and music player are installed by the owner during setup
  or later, from the package feed.
- **`boot_size = 128` needs a one-line pmbootstrap change.** pmbootstrap 3.10.1
  refuses any `boot_size` below its 512 MiB default
  (`sanity_check_boot_size()` in `pmb/install/_install.py`, which returns early
  only `if int(config.boot_size) >= int(default)`). The releases are built with
  that comparison changed to `>= 64`. Without the change, use the default: the
  image still works, but `/boot` takes 384 MiB more of the Echo's 4 GB and the
  root filesystem gets that much less. A pmbootstrap upgrade or reinstall
  silently undoes the change, so check the image geometry
  (`parted pmos.img unit MiB print`) rather than trusting the setting.

## 3. The kernel: `linux-amazon-biscuit`

```sh
pmbootstrap -p ~/pmaports-build build --force linux-amazon-biscuit
```

What goes into it, all in `device/testing/linux-amazon-biscuit/`:

- **Upstream source:** `bengris32/linux-mtk` at commit
  `20951722df6ae1f6fecbfcea0a7d585118a06f3f` (branch `mt8163/7.0`, a 7.0-rc6
  mainline tree for MediaTek MT8163 devices), downloaded as a GitHub archive.
- **109 patches**, applied in `source=` order by abuild with `patch -p1`. The
  directory also holds patch files that are not in `source=`; those are not
  applied.
- **The MT6625L Wi-Fi driver**, `mt6625l-wlan-20260823.tar.gz`: MediaTek's
  driver from Amazon's GPL kernel source for MT8163 devices (Linux 3.18),
  ported to this kernel. `prepare()` copies it to
  `drivers/net/wireless/mediatek/mt6625l` and hooks it into that directory's
  `Kconfig` and `Makefile`.
- **The configuration**: `make defconfig`, then the `scripts/config` edits in
  `build()`. There is no config file; a running device exposes the result at
  `/proc/config.gz`.
- **No firmware.** `CONFIG_EXTRA_FIRMWARE` is empty. The Wi-Fi, Bluetooth and
  microphone-FPGA firmware is each owner's own, imported from their Echo (see
  "Owner-imported files" below).

The same source as a git tree, with each patch as a commit and the device tree
readable as one file (`arch/arm64/boot/dts/mediatek/mt8163-amazon-biscuit.dts`,
918 lines), is branch `biscuit-r243` of
[`liamtw22/linux-mtk`](https://github.com/liamtw22/linux-mtk/tree/biscuit-r243);
its base is tagged `upstream-20951722`. In that branch the MT6625L driver
arrives in two commits: first MediaTek's driver exactly as Amazon's GPL source
release has it, then this port's changes to it, so one `git diff` between the
two shows everything the port changed. The release also carries the same
source as `linux-amazon-biscuit-r243-source.tar.gz`.

A cold build takes about 50 minutes on the 4-core build VM; with a warm
ccache, about 4.
`uname -v` on the device reports one more than the package's `pkgrel` (kernel
r243 prints `#244`), because pmaports sets `KBUILD_BUILD_VERSION` that way.

## 4. The device packages: `device-amazon-biscuit`

```sh
pmbootstrap -p ~/pmaports-build build --force device-amazon-biscuit --arch aarch64
```

This produces five packages from one APKBUILD:

| Package | What it is |
|---|---|
| `device-amazon-biscuit` | Core: the hardware, Wi-Fi setup, Bluetooth, audio, light ring, buttons, time sync and the settings page |
| `device-amazon-biscuit-voice` | The voice assistant (linux-voice-assistant with microWakeWord) |
| `device-amazon-biscuit-sendspin` | The Music Assistant player (Sendspin) |
| `device-amazon-biscuit-pulseaudio` | Drop-ins for PulseAudio, installed only if PulseAudio is |
| `device-amazon-biscuit-nonfree-firmware` | Only a list telling mkinitfs where the owner's imported firmware is; it contains no firmware |

The subpackages depend on the exact core version (`device-amazon-biscuit=6-rN`),
so they are always built, published and installed together.

### Checksums

Never hand-edit the `sha512sums=` block. After changing a source file, run
`pmbootstrap -p ~/pmaports-build checksum device-amazon-biscuit` and check that
the APKBUILD actually changed. abuild expects the sums in `source=` order.

### The bundled binaries and how to rebuild them

Three archives in `source=` are prebuilt, because building them takes an hour
or more under qemu. They are in the repository, and `PROVENANCE.md` lists what
is inside each one.

- **`biscuit-sendspin-venv.tar.gz`**: rebuilt by `build-sendspin-venv.sh`, with
  versions pinned in `sendspin-venv-requirements.txt`. Run it in an aarch64
  chroot (`pmbootstrap -p ~/pmaports-build chroot -b aarch64 -- sh /path/to/build-sendspin-venv.sh`);
  it takes about an hour, most of it compiling PyAV. PyAV is built from source
  against Alpine's FFmpeg on purpose: the PyPI wheel bundles its own FFmpeg with
  GPL-licensed libx264 and libx265.
- **`biscuit-voice-venv.tar.gz`**: linux-voice-assistant at commit
  `b0c53c4` and its dependencies. The exact distributions and versions inside
  the tarball are the voice table in `PROVENANCE.md`, which is generated from
  the tarball itself. `biscuit-voice.requirements.lock` is the requirement set
  the venv was first resolved from. It does not match the tarball exactly (it
  names packages, such as aiohttp, that the tarball does not contain), so do
  not treat it as the tarball's manifest. The venv's copy of
  linux-voice-assistant already carries some of this port's changes;
  `package()` then applies the `biscuit-lva-*.patch.txt` patches, copies the
  files in `lva-src/` over upstream's and adds a change notice to `models.py`,
  so the modified source is all in this repository. There is no scripted
  rebuild of the venv yet.
- **`tflite-aarch64-musl.tar.gz`**: TensorFlow Lite 2.17.0's C library, built
  for aarch64 musl, because pymicro-wakeword only bundles an x86-64 glibc build.
  In an aarch64 chroot with `cmake g++ make git python3 patch linux-headers`:

  ```sh
  wget https://github.com/tensorflow/tensorflow/archive/refs/tags/v2.17.0.tar.gz
  tar xzf v2.17.0.tar.gz && mkdir tfbuild && cd tfbuild
  cmake ../tensorflow-2.17.0/tensorflow/lite/c -DCMAKE_BUILD_TYPE=Release \
      -DTFLITE_ENABLE_XNNPACK=OFF -DTFLITE_ENABLE_RUY=ON \
      -DTFLITE_ENABLE_GPU=OFF -DBUILD_SHARED_LIBS=ON
  make -j4 tensorflowlite_c
  ```

  flatbuffers does not build on musl as downloaded: it enables
  `FLATBUFFERS_LOCALE_INDEPENDENT` because musl reports `_XOPEN_VERSION >= 700`,
  but musl has no `strtoll_l`. Set that macro to 0 in `flatbuffers/base.h`
  (Alpine patches its own flatbuffers the same way). The archive holds
  `libtensorflowlite_c.so` and the shared libraries it links against.

## 5. The disk image and boot image

Build every published image from a fresh `pmbootstrap zap` and a committed
tree, never by upgrading an older image in place:

```sh
pmbootstrap -y -p ~/pmaports-build zap
pmbootstrap -y -p ~/pmaports-build install --password 147147
pmbootstrap -p ~/pmaports-build export
mkdir -p ~/payload
cp -L /tmp/postmarketOS-export/amazon-biscuit.img ~/payload/pmos.img
cp -L /tmp/postmarketOS-export/boot.img ~/payload/boot.img
```

- The image ships postmarketOS's documented default account, `user` / `147147`.
  The owner replaces it during setup. See [SECURITY.md](SECURITY.md).
- `pmbootstrap export` puts links to `boot.img` and the disk image,
  `amazon-biscuit.img`, in `/tmp/postmarketOS-export/`; `cp -L` copies the
  files they point to. The disk image has two partitions, `pmOS_boot` and
  `pmOS_root`.

**Check the boot image before using it.** The kernel inside `boot.img` must
start with the MediaTek header, or the Echo bootloops. The first four bytes of
the kernel (at the page size, 2048) must be `88 16 88 58`, followed by the name
`KERNEL`:

```sh
dd if=boot.img bs=2048 skip=1 count=1 2>/dev/null | head -c 16 | od -A d -t x1
```

The kernel command line must contain `bootopt=64S3,32N2,64N2`; amonet v2 boots
the kernel in the mode that field names, and anything but `64` for the last
field fails to boot. The zip builder checks both before it builds anything: it
refuses a boot image whose page size is not 2048 or whose kernel does not start
with `88 16 88 58` followed by `KERNEL`, and one without that `bootopt`. It
also checks that `boot.img` names the image's own `pmOS_boot` and `pmOS_root`
filesystems.

**Clean the image before publishing it.** `pmbootstrap install` leaves build
traces in the root filesystem: `/var/log/apk.log` (full paths of the build
machine's home directory), `/var/cache/apk/` (package indexes) and
`/etc/machine-id` (the same ID on every install; with the file gone, the
`dbus` service makes a new one on first boot). Remove them in the image
itself, not in pmbootstrap's chroot, which is no longer connected to the image
once `install` has finished:

```sh
LOOP=$(sudo losetup --find --show --partscan ~/payload/pmos.img)
sudo mkdir -p /mnt/biscuit-img
sudo mount "${LOOP}p2" /mnt/biscuit-img
sudo truncate -s 0 /mnt/biscuit-img/var/log/apk.log
sudo rm -f /mnt/biscuit-img/etc/machine-id
sudo rm -rf /mnt/biscuit-img/var/cache/apk/*
sudo umount /mnt/biscuit-img
sudo losetup -d "$LOOP"
```

This is the step the v1.0 image (build r295) was built with. Before
unmounting, that build also listed the image's `device-amazon-biscuit` and
`linux-amazon-biscuit` versions, searched the root filesystem for the build
machine's user name, home directory and address, checked that no
`speaker.fir` was present, and printed `/etc/apk/repositories`, the hostname
and the accounts, to confirm the cleanup.

## 6. The TWRP zips

The installer's source is in [`installer/`](installer/): the zip scripts,
`zip/build_zips.py`, `zip/biscuit-tool.c`, the tools and the installation
guide. It moved into this repository at r294 and is the only source of the
zips. From the repository root, it builds the four zips that go on a release:

```sh
python3 installer/zip/build_zips.py --image ~/payload/pmos.img \
    --boot ~/payload/boot.img --out out-v1.0 --record
```

`--record` is needed once per release, to register its name (see below).

To build only the three tool zips, without an image:

```sh
python3 installer/zip/build_zips.py --tools-only --out out-tools
```

- A full build reads the build number, the release name and the owner-import
  manifest from the image with `debugfs` (e2fsprogs). On a machine without it,
  pass `--build rNNN`, `--release vX.Y` and `--manifest` explicitly.
  `--tools-only` reads the manifest from this repository,
  `device/testing/device-amazon-biscuit/biscuit-profile-assets.json`, unless
  `--manifest` is given.
- It checks the boot image first, as described in section 5, and builds
  nothing from one the Echo could not boot.
- `pmos-amazon-biscuit-vX.Y.zip` takes its `vX.Y` from
  `/usr/share/biscuit/release` inside the image, which the core package writes
  from the APKBUILD's `_release`, beside its build (`6-r295`). An image without
  that file (every build before v1.0) gives `pmos-amazon-biscuit-rNNN.zip`.
- A release name belongs to one build, recorded in
  `installer/zip/releases.json`. A name already recorded for another build is
  refused, so each public build needs a new `_release`: `v1.0.1` for fixes,
  `v1.1` for features. `pkgrel` keeps counting underneath, because apk orders
  upgrades by it. Record a name only for the build that is published: if a
  release is rebuilt before it goes out, remove its unpublished entry first.
- The three tool zips carry no version in their file names; a release carries
  them beside its install zip. Their versions are inside them (`zip.prop`,
  printed first in TWRP) and locked in `installer/zip/tool-versions.json`: a
  tool zip whose contents changed under an unchanged version is refused, and a
  new version is recorded only with `--record`.
- The zips are reproducible: the same inputs give the same bytes.
- The install zip must stay under 200 MiB, because TWRP holds it in a tmpfs of
  half the Echo's RAM.
- `OUT/SHA256SUMS` lists the zips.

The scripts' test harness runs on a Linux host with `/usr/bin/busybox`:

```sh
sh installer/tests/shtest.sh
```

Each zip carries a static armv7 BusyBox and `biscuit-tool` for TWRP, both
committed in `installer/zip/bin/`. `biscuit-tool` is rebuilt from
`installer/zip/biscuit-tool.c` by
[`installer/tools/build-biscuit-tool.sh`](installer/tools/build-biscuit-tool.sh),
which names the exact Alpine packages it was built with. BusyBox is Alpine's
`busybox-static` package, unmodified; [`installer/NOTICE`](installer/NOTICE)
names the package and the aports commit it was built from, and the release
carries its complete source.

## 7. Publishing (maintainers)

Releases are published in this order, because the optional apps pin the exact
core version and an install from an older feed would downgrade the core:

1. The package feed (`https://liamtw22.github.io/biscuit-apk/edge`): all five
   device packages and the kernel, in an index signed with the feed's key. The
   script is `tools/publish-feed.sh` in the `liamtw22/biscuit-apk` repository.
2. The GitHub release `vX.Y` of this repository, tagged `vX.Y` and also
   `rNNN` (its build) on the same commit, because the settings page links a
   build's source by its `rNNN` tag. The release carries the zips, `SHA256SUMS`,
   and the source bundles: `linux-amazon-biscuit-r243-source.tar.gz` for the
   kernel, and BusyBox's `busybox-1.38.0.tar.bz2`,
   `aports-main-busybox-768a1e87.tar.gz` and
   `busybox-static-1.38.0-r7.armv7.apk`, the names `installer/NOTICE` gives.

## Owner-imported files

Nothing Amazon ships is in this repository or in any package built from it:
no firmware, no audio tuning, no sounds, no light animations. Each owner's
backup zip collects them from their own Echo, the install zip stores them on
the Echo's persist partition, and `biscuit-persist` and `biscuit-import-assets`
put them in place at boot. They are:

- the two Bluetooth ROM patches and the microphone FPGA bitstream, which
  built-in drivers ask for before the root filesystem is mounted, so they must
  be in the initramfs: the boot image written at install time has them
  appended, and later initramfs builds pick them up from `/lib/firmware`;
- the Wi-Fi firmware, imported as `WIFI_RAM_CODE` (Fire OS calls it
  `WIFI_RAM_CODE_8163`). It is not in the initramfs: `biscuit-persist` copies
  it from the store into `/lib/firmware` on the root filesystem early in every
  boot, and the Wi-Fi driver, a loadable module, reads it from there when
  Wi-Fi starts;
- the Fire OS microphone-processing configuration used by the optional "stock"
  microphone profiles;
- the speaker correction curve (`EQ_50.cfg`); without it the speaker plays flat;
- the sounds (earcons) and the light-ring animations.

The firmware is required. A device without it has no sound card (the
microphone FPGA carries the audio path, and its driver fails without the
bitstream), no Bluetooth and no Wi-Fi, so it cannot even open its setup
hotspot. The install zip therefore refuses a backup that lacks any of the
firmware files.

The rest is optional. Without the microphone configuration the device runs the
port's own microphone chain, which is the default; without the speaker curve
the speaker plays flat; without the sounds and animations it uses a setup chime
of its own and generated ring effects.
