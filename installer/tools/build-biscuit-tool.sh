#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Rebuild zip/bin/biscuit-tool from zip/biscuit-tool.c.
#
#   tools/build-biscuit-tool.sh [OUT]
#       Inside an Alpine armv7 system or chroot (for example
#       `pmbootstrap chroot -b armv7`) with the packages below installed.
#
#   sudo APK=... KEYS=... QEMU=... tools/build-biscuit-tool.sh --root DIR [OUT]
#       On any Linux host with qemu-arm registered in binfmt_misc. Creates DIR
#       as a minimal Alpine armv7 root with apk.static, installs the packages
#       into it, and runs the first form inside it. Needs root, for chroot.
#         APK        apk.static for the host (pmbootstrap keeps one in its work
#                    directory; Alpine ships it as apk-tools-static)
#         KEYS       a directory of Alpine's signing keys (/etc/apk/keys on
#                    Alpine, or pmbootstrap's chroot_native/etc/apk/keys)
#         QEMU       a static qemu-arm, copied to the interpreter path that
#                    binfmt_misc names
#         APK_CACHE  optional: an apk cache directory (index and packages)
#                    that still holds the pinned versions, used offline. Alpine
#                    edge keeps only the newest build of each package, so once
#                    it moves on, the pins below resolve only from a cache.
#         REPOS      optional: the repository URL. With APK_CACHE it must be
#                    the URL the cache was filled from (pmbootstrap uses
#                    http://dl-cdn.alpinelinux.org/alpine/edge/main).
#
# The committed binary was built with Alpine edge armv7 and these packages:
#   gcc 15.2.0-r9, binutils 2.45.1-r1, musl-dev 1.2.6-r3,
#   zlib-dev + zlib-static 1.3.2-r0, fortify-headers 3.0.2-r0
# as
#   gcc -static -O2 -o biscuit-tool biscuit-tool.c -lz && strip biscuit-tool
# Alpine's gcc builds position-independent executables by default, so this is
# a static-pie binary. With the same package versions the result is
# byte-identical to zip/bin/biscuit-tool; the script says whether it is. With
# other versions the bytes differ but the program is the same source.
#
# The binary statically links musl (MIT), zlib (zlib licence) and libgcc (GPL
# with the GCC Runtime Library Exception): see NOTICE.
set -eu

PKGS="gcc=15.2.0-r9 binutils=2.45.1-r1 musl-dev=1.2.6-r3 zlib-dev=1.3.2-r0 zlib-static=1.3.2-r0 fortify-headers=3.0.2-r0"
REPOS=${REPOS:-https://dl-cdn.alpinelinux.org/alpine/edge/main}

here=$(cd "$(dirname "$0")/.." && pwd)
src=$here/zip/biscuit-tool.c
ref=$here/zip/bin/biscuit-tool

compare() {
	new=$(sha256sum "$1" | cut -d' ' -f1)
	echo "built $1"
	echo "  sha256 $new"
	if [ -f "$ref" ]; then
		old=$(sha256sum "$ref" | cut -d' ' -f1)
		if [ "$new" = "$old" ]; then
			echo "  identical to zip/bin/biscuit-tool"
		else
			echo "  differs from zip/bin/biscuit-tool ($old): a different toolchain version?"
		fi
	fi
}

if [ "${1:-}" != "--root" ]; then
	out=${1:-$here/biscuit-tool.out}
	case $(uname -m) in armv7*|armv8l) ;; *)
		echo "not an armv7 system: run this inside an Alpine armv7 chroot, or use --root" >&2
		exit 1 ;;
	esac
	gcc -static -O2 -o "$out" "$src" -lz
	strip "$out"
	compare "$out"
	exit 0
fi

[ $# -ge 2 ] || { echo "usage: $0 --root DIR [OUT]" >&2; exit 2; }
root=$2
out=${3:-$here/biscuit-tool.out}
: "${APK:?set APK to apk.static}" "${KEYS:?set KEYS to a directory of Alpine keys}" "${QEMU:?set QEMU to a static qemu-arm}"
[ "$(id -u)" = 0 ] || { echo "--root needs root (chroot)" >&2; exit 1; }
interp=$(sed -n 's/^interpreter //p' /proc/sys/fs/binfmt_misc/qemu-arm 2>/dev/null || true)
[ -n "$interp" ] || { echo "qemu-arm is not registered in binfmt_misc" >&2; exit 1; }

mkdir -p "$root/etc/apk/keys" "$root/tmp" "$root$(dirname "$interp")"
cp "$KEYS"/*.pub "$root/etc/apk/keys/"
echo "$REPOS" > "$root/etc/apk/repositories"
cp "$QEMU" "$root$interp"
# busybox gives the root a /bin/sh, so the first form of this script runs in it.
# shellcheck disable=SC2086
"$APK" --arch armv7 --root "$root" --initdb ${APK_CACHE:+--cache-dir "$APK_CACHE" --no-network} \
	--no-interactive add busybox $PKGS
# Inside the root the script runs from /src, a copy of the parts of this tree it reads.
mkdir -p "$root/src/tools" "$root/src/zip/bin"
cp "$0" "$root/src/tools/build-biscuit-tool.sh"
cp "$src" "$root/src/zip/biscuit-tool.c"
if [ -f "$ref" ]; then cp "$ref" "$root/src/zip/bin/biscuit-tool"; fi
chroot "$root" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin TMPDIR=/tmp \
	/bin/sh /src/tools/build-biscuit-tool.sh /src/biscuit-tool
cp "$root/src/biscuit-tool" "$out"
compare "$out"
