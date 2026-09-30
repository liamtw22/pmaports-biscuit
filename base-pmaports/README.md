# base-pmaports: the pmaports tree this port builds on

This repository is an overlay: it holds only `device/testing/device-amazon-biscuit`
and `device/testing/linux-amazon-biscuit`. The rest of the tree it is built on is
upstream pmaports (now maintained by Nura, formerly postmarketOS) at one fixed
commit, plus the three patches in this directory.

| | |
|---|---|
| upstream | `https://gitlab.postmarketos.org/postmarketOS/pmaports.git` |
| base commit | `1ab5c4ed75a362067e27a0238fd80612e3454717` (2026-07-06, "ci: fix test_providers.py to not fail if provider_priority is 0") |
| patches | `0001`-`0003` below, made with `git format-patch` |

## The patches

| Patch | What it does | Does it end up in the image? |
|---|---|---|
| `0001-device-archived-remove-amazon-biscuit-...` | Deletes upstream's `device/archived/device-amazon-biscuit` and `device/archived/linux-amazon-biscuit` (the old Fire OS 3.18 vendor-kernel port). pmbootstrap refuses two aports with the same package name, so they must go before this overlay is copied in. | No: it only removes files. |
| `0002-main-boot-deploy-wrap-the-dtb-appended-kernel-in-the...` | `boot-deploy` 0.24.0-r1: puts the MediaTek header on the kernel that actually goes into `boot.img` when `deviceinfo_append_dtb=true`. Without it the MT8163 bootloader treats the kernel as 32-bit and the device bootloops about 11 s after power-on. | **Yes.** It is the `boot-deploy` in the image, and it rebuilds `/boot/boot.img` on every kernel or initramfs update. boot-deploy is GPL-2.0-or-later; this patch is its corresponding source. |
| `0003-main-postmarketos-zram-accept-a-built-in-zram-driver` | `postmarketos-zram` 3-r1: accepts a zram driver built into the kernel. | No. Upstream has since moved to 4-r0, which outranks it, so current images install upstream's package. It is kept so this tree matches the one the releases were built from. |

Nothing else differs. The build machine's tree also had its `maintainer=` line
in `cross/gcc-x86_64/APKBUILD` rewritten by pmbootstrap to the local user; that
change has no effect on any package and is deliberately left out.

## How the base was found

The build machine's pmaports was a private snapshot without upstream history, so
the fork point was found by content: every upstream commit from late June to
mid-July 2026 was compared with the build tree, ignoring the two biscuit
directories. `1ab5c4ed7` differs in the fewest files, and all of those
differences are the three patches plus the maintainer line above. Applying
`0001`-`0003` to `1ab5c4ed7` reproduces the build tree's non-biscuit files
exactly.

Why an old base still gives a current system: pmbootstrap installs a binary
package from the Nura/postmarketOS and Alpine repositories whenever it is newer
than the local aport, so almost everything in an image is current edge, whatever
this base says. The only local packages that win are the ones whose version is
higher than the binary repository's: this port's two packages, and `boot-deploy`
0.24.0-r1 from `0002` (upstream is at 0.24.0-r0 as of 2026-09-29).

**Check this before every release.** When upstream releases a `boot-deploy`
that outranks 0.24.0-r1, the image silently gets the unpatched version and stops
booting. Rebase `0002` onto the new version and bump its `pkgrel` above
upstream's, then check that the first four bytes of the kernel in `boot.img` are
`88 16 88 58`.

## Applying them

```sh
git clone https://gitlab.postmarketos.org/postmarketOS/pmaports.git ~/pmaports-build
cd ~/pmaports-build
git checkout -b biscuit-base 1ab5c4ed75a362067e27a0238fd80612e3454717
git am /path/to/pmaports-biscuit/base-pmaports/*.patch
```

`git am` needs a committer identity (`git config user.name` / `user.email`).
`git apply` works too if you do not want commits.

The series is made against `1ab5c4ed7` and only applies there: on today's
upstream `main`, `0002` and `0003` conflict, because `boot-deploy` and
`postmarketos-zram` have moved on. To build on a newer base, apply `0001`, then
redo the boot-deploy change on the current version (the embedded
`fix-append-dtb-mtk-header.patch` in `0002` is the part that matters) and skip
`0003`.

## Licence

These patches are pmaports material and are under pmaports' own licence: the
GPL version 3 of pmaports' `LICENSE` (the text is in `../LICENSES/GPL-3.0.txt`).
The software they touch keeps its own licence and authors. `0001` only deletes
upstream files (the archived device package is MIT, the archived kernel
package GPL-2.0-only), and the deleted content in it stays its authors'.
`0002` changes `boot-deploy`, which is GPL-2.0-or-later, and is the
corresponding source of the patched `boot-deploy` in the image. `0003` changes
`postmarketos-zram`, which is GPL-3.0-or-later. See `../NOTICE`.

The rest of the build (overlay, kernel, image, zips) is in `../BUILDING.md`.
