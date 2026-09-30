# Installing postmarketOS on an Echo Dot (2nd generation)

This replaces Fire OS on a 2nd-generation Echo Dot ("biscuit", model RS03QR, 2016)
with postmarketOS. You end up with a speaker that joins Home Assistant: a voice
assistant, a Music Assistant speaker, a Bluetooth speaker, the light ring and the
buttons.

This is a community project. It is not affiliated with or endorsed by Amazon or
postmarketOS. Report problems on the project's
[GitHub issues](https://github.com/liamtw22/pmaports-biscuit/issues).

Everything runs from a computer through TWRP with four zips, published on the
project's [GitHub releases](https://github.com/liamtw22/pmaports-biscuit/releases):

| Zip | What it does |
|---|---|
| `amazon-biscuit-backup.zip` | Saves what is unique to this Echo, before anything changes |
| `pmos-amazon-biscuit-vX.Y.zip` | Replaces Fire OS with postmarketOS |
| `amazon-biscuit-restore.zip` | Puts this Echo's own files back into an installed postmarketOS |
| `amazon-biscuit-stock-restore.zip` | Takes the Echo back to Fire OS 6 |

- **vX.Y** is the release, for example `v1.0`. The settings page shows it as
  **Device software**, with the build number beside it (`6-r295` for v1.0).
- The three tools have no number in their names: use the ones from the same
  release as the install zip. Each tool still has its own version, which
  changes only when that zip changes, so a release can carry the same tools as
  the one before.
- Every zip prints its name and version first, for example
  `amazon-biscuit-backup v1` or `pmos-amazon-biscuit v1.0`.
- `pmos-amazon-biscuit-vX.Y.zip` is this project's TWRP installer. It is not the
  recovery zip that pmbootstrap itself can build.
- No zip contains any Amazon file. The firmware, microphone files, speaker curve,
  sounds and ring animations postmarketOS needs come from your own Echo, through
  the backup in step 2.

**Risk.** Unlocking with amonet, and repartitioning, can brick the device. Read this
whole page first. None of this can be undone without the backup from step 2.

## What you need

- The Echo, unlocked with **amonet-biscuit v2** and running **Fire OS 6** (step 1).
- A computer with `adb` (Android platform-tools), and on Windows the Kindle Fire
  or Google USB driver.
- A micro-USB **data** cable. The Echo is powered through it while in TWRP.
- A phone or laptop with Wi-Fi, for setup.
- The four zips from the release. Below, `vX.Y` stands for the release in the
  install zip's name.

### Check the downloads

The release has a `SHA256SUMS` file. Check the zips against it before using them.
In the folder that holds the zips and `SHA256SUMS`:

- **Linux:** `sha256sum -c --ignore-missing SHA256SUMS`
- **macOS:** `shasum -a 256 -c SHA256SUMS`
- **Windows, PowerShell:**

  ```
  Get-Content SHA256SUMS | ForEach-Object {
    $hash, $file = $_ -split '\s+', 2
    if ((Get-FileHash $file -Algorithm SHA256).Hash -eq $hash) { "OK   $file" } else { "BAD  $file" }
  }
  ```

- **Windows, one file at a time:** `certutil -hashfile pmos-amazon-biscuit-vX.Y.zip SHA256`,
  then compare the printed hash with that file's line in `SHA256SUMS`.

Every file must say OK or match. If one does not, download it again.

An Echo has no screen, so TWRP is driven entirely from the computer. Every
`adb shell twrp install` command prints what the zip is doing.

## 1. Unlock with amonet, then install Fire OS 6

Follow the amonet-biscuit v2 guide on XDA:
[[UNLOCK][ROOT][TWRP][UNBRICK] Amazon Echo Dot 2nd Gen / 2016 (biscuit)](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/).
It unlocks the Echo, installs TWRP and then has you flash Fire OS 6 twice, once
per slot. The thread says which Fire OS versions its method supports and what the
computer needs. It recommends Linux; a live USB system is enough.

This project was tested with the Fire OS 6 update
`update-kindle-biscuit_puffin-NS6574_user_7623_0013121734532.bin` (Fire OS
6.5.7.4, SHA256 `64ab6d2dd85f8093abdd62c275d229c7e9fdd68e4d46892b48bdbd1d100d46d8`).

**Do not skip Fire OS 6.** postmarketOS takes this Echo's Wi-Fi, Bluetooth and
microphone firmware, and its speaker correction curve, from Fire OS 6's system
partition. The backup cannot collect them from an empty one.

**Keep Fire OS off Wi-Fi.** An automatic Amazon update can replace amonet's
bootloader and lock the Echo again. Nothing in these steps needs Fire OS on a
network.

## 2. Back up the Echo

Boot into TWRP: unplug the Echo, then hold **Volume Up** while you plug it back
in. When `adb devices` shows it as `recovery`, run:

```
adb push amazon-biscuit-backup.zip /tmp/
adb shell twrp install /tmp/amazon-biscuit-backup.zip
```

This takes about 20 seconds. It saves:

- the partition tables, and both eMMC boot areas. One of those holds this unit's
  factory calibration, which exists nowhere else;
- partitions 1-12;
- this Echo's Wi-Fi, Bluetooth and microphone firmware, and its microphone files;
- its speaker correction curve (Fire OS's `EQ_50.cfg`), which the settings
  page's Stock equaliser uses; without it the speaker plays flat;
- its sounds, and its light ring animations.

Check that it reports all four firmware files:

```
  firmware: 4 file(s)
  fireos6: 4 file(s)
  speaker: 1 file(s)
  earcons: 67 file(s)
  ring animations: 265 file(s)
```

Every backup from `amazon-biscuit-backup` has a `led` folder with the ring
animations. Its `info.txt` says `format=2`, how many animations it holds
(`led_count`) and which zip made it. A backup from an older zip has no `format`
line; its ring animations are used if its `led` folder has them.

If a backup has no ring animations while Fire OS still holds them, the install
stops before changing anything: installing erases Fire OS, and with it the only
copy. Flash `amazon-biscuit-backup` again, then copy it off and confirm it as
below. To install without them instead, run
`adb shell touch /sdcard/biscuit-backup/<serial>/NO_LED_OK` first. Afterwards
they can only be imported from animation files you already have, on the settings
page (Storage › Files from stock).

If it lists `missing firmware`, stop: Fire OS 6 is not installed (see step 1).

Copy the backup to your computer. Run this from the folder that holds the zips, on
a drive with at least 200 MB free; the backup is about 110 MB. Then confirm that you
have it, using the serial number the zip printed:

```
adb pull /sdcard/biscuit-backup
adb shell touch /sdcard/biscuit-backup/<serial>/COPIED
```

The install refuses to start until that `COPIED` file exists, because installing
erases the storage the backup is on. **Keep this folder.** It is the only way back
to Fire OS.

On Git Bash, prefix adb commands that name a device path with `MSYS_NO_PATHCONV=1`.
Otherwise Git Bash rewrites `/sdcard/...` into a Windows path.

## 3. Install postmarketOS

Still in TWRP:

```
adb push pmos-amazon-biscuit-vX.Y.zip /tmp/
adb shell twrp install /tmp/pmos-amazon-biscuit-vX.Y.zip
```

Push it to `/tmp`, not to `/sdcard`: `/sdcard` is the storage being replaced, and
the zip refuses to run from there. The install takes about 5 minutes. **Do not
unplug the Echo** while it runs.

The install does the following:

- Checks the backup, and that it holds all four firmware files.
- Builds this Echo's boot image, with its Bluetooth and microphone firmware, in
  memory, and checks that its kernel carries the header the Echo needs to boot
  it and that it fits both boot slots. Up to this point nothing on the Echo has been changed.
- Merges Fire OS's system, cache and userdata partitions into one for postmarketOS.
- Writes and verifies the system.
- Stores this Echo's firmware, microphone files, speaker curve, sounds and ring
  animations in its settings partition.
- Writes the boot image to both slots.

The bootloaders, amonet and TWRP are not touched. When it prints
`postmarketOS is installed.`, run:

```
adb reboot
```

## 4. Set it up

About a minute and a half after rebooting, the Echo opens a Wi-Fi network named
`biscuit-XXXX` and plays a chime.

1. Join `biscuit-XXXX` from a phone. The setup page opens by itself; if it doesn't,
   browse to `http://192.168.4.1/`.
2. **Your account, region and name.** Enter the username and password you will
   sign in with, where the device is, and a name for it.
3. **Your device and Wi-Fi.**
   - Press **Play a sound on the device** and confirm you heard it. This makes sure
     you are setting up the Echo in front of you.
   - Then choose your Wi-Fi network.
4. **Optional extras**:
   - **Voice assistant**: wake word and Home Assistant voice. About 120 MB.
   - **Music speaker** (Sendspin): Music Assistant playback. About 65 MB.
5. Press **Connect**. Watch the light ring: the page explains what each pattern means.

Until setup finishes, the Echo has postmarketOS's default account: user `user`,
password `147147`. Setup replaces it with the account you create. Finish setup
soon after installing, and do not leave an Echo that has not been set up running
where others can reach it.

Once it is on your network, the settings page is at `http://<name>.local:8080`.
Sign in with the account you created. From there you can install the extras
later, change Wi-Fi, set up Bluetooth, and more. Home Assistant finds the Echo
itself through the ESPHome integration once the voice assistant is installed,
or once the Bluetooth proxy is switched on (settings page, Home Assistant
section; it is off by default).

## Updating

An installed Echo is updated over the network; you do not need a new install
zip. Updates come from postmarketOS and from this project's package feed,
`https://liamtw22.github.io/biscuit-apk/edge`, which the Echo adds by itself.

On the settings page, **System › Updates** shows what is installed, checks for
updates and installs them.

A new `pmos-amazon-biscuit-vX.Y.zip` is only needed for a fresh install.

An Echo updated from r293 or older keeps its speaker correction curve: the
update moves it into the store of imported files. If it cannot, the update
still succeeds and the speaker plays flat; import `EQ_50.cfg` from your backup's
`assets/speaker` folder under **Storage › Files from stock**.

## Putting this Echo's files back: `amazon-biscuit-restore`

The install already stores this Echo's firmware, microphone files, speaker curve,
sounds and ring animations. Use restore only if they are ever lost, for example
after the settings partition was wiped. It replaces only what the backup holds,
and changes nothing else:

- Each group of imported files the backup lacks is kept as the Echo has it.
  For example, a backup made by an older backup zip has no speaker curve, so a
  curve imported on the settings page stays. The zip lists them as
  `kept from the store, not in the backup: ...`.
- Ring animations you imported on the settings page that the backup does not
  have are kept, and a backup without ring animations leaves the stored ones as
  they are.

Boot TWRP (hold **Volume Up** while plugging in). On postmarketOS's layout TWRP has
no `/sdcard`, so the backup goes to `/tmp`:

```
adb push biscuit-backup/<serial> /tmp/biscuit-backup/<serial>
adb push amazon-biscuit-restore.zip /tmp/
adb shell twrp install /tmp/amazon-biscuit-restore.zip
adb reboot
```

## Going back to Fire OS: `amazon-biscuit-stock-restore`

This puts back the stock partition table, and this Echo's own persist, boot and misc
partitions. It then clears the space postmarketOS used. amonet and TWRP stay
installed, so the Echo stays unlocked.

Boot TWRP (hold **Volume Up** while plugging in), then:

```
adb push biscuit-backup/<serial> /tmp/biscuit-backup/<serial>
adb push amazon-biscuit-stock-restore.zip /tmp/
adb shell twrp install /tmp/amazon-biscuit-stock-restore.zip
```

It takes about 3 minutes. Then install Fire OS 6 from Amazon's update file (see
step 1). The zip prints these steps when it finishes:

```
adb reboot recovery
adb shell twrp wipe cache
adb shell twrp format data
adb push update-kindle-biscuit_<version>.bin /tmp/update.zip
adb shell twrp install /tmp/update.zip
adb reboot recovery
adb push update-kindle-biscuit_<version>.bin /tmp/update.zip
adb shell twrp install /tmp/update.zip
adb reboot
```

- The stock-restore leaves the userdata partition blank, with no filesystem, so
  TWRP has no `/sdcard` at first. `twrp format data` creates it.
- The update goes to `/tmp`. Restarting TWRP empties `/tmp`, so the update is
  pushed again before the second install.
- It is flashed twice because the Echo has two system slots.

## Reinstalling postmarketOS

To start again from a clean install, take the Echo back to the stock layout
first, then install over it. After stock-restore TWRP has no `/sdcard`, and its
`/tmp` cannot hold both the backup and the install zip. So the backup goes to
`/tmp`, and the install zip is streamed from the computer with `adb sideload`:

```
adb push biscuit-backup/<serial> /tmp/biscuit-backup/<serial>
adb shell touch /tmp/biscuit-backup/<serial>/COPIED
adb push amazon-biscuit-stock-restore.zip /tmp/
adb shell twrp install /tmp/amazon-biscuit-stock-restore.zip
adb shell twrp sideload
adb wait-for-sideload
adb sideload pmos-amazon-biscuit-vX.Y.zip
adb wait-for-recovery
adb shell cat /tmp/biscuit-install.txt
adb reboot
```

`adb sideload` reports success even if the zip stopped with an error, so read
`/tmp/biscuit-install.txt`. It should end with `postmarketOS is installed.`

## If something goes wrong

- **Each zip refuses before it changes anything** if something is wrong: the wrong
  device, the wrong partition layout, a backup that does not verify or belongs to
  another Echo, missing firmware, or (for the install) a boot image the Echo
  could not boot or that does not fit. The message says what to do.
- **What the zip printed** is also saved as a `.txt` file in `/tmp`, and the full
  log as a `.log` file beside it. Copy them with `adb pull` while still in TWRP.
  `/tmp` is cleared when TWRP restarts.

  | Zip | What it printed | Full log |
  |---|---|---|
  | `amazon-biscuit-backup.zip` | `/tmp/biscuit-backup.txt` | `/tmp/biscuit-backup.log` |
  | `pmos-amazon-biscuit-vX.Y.zip` | `/tmp/biscuit-install.txt` | `/tmp/biscuit-install.log` |
  | `amazon-biscuit-restore.zip` | `/tmp/biscuit-restore.txt` | `/tmp/biscuit-restore.log` |
  | `amazon-biscuit-stock-restore.zip` | `/tmp/biscuit-stock-restore.txt` | `/tmp/biscuit-stock-restore.log` |

- **`adb sideload` works too** (`adb shell twrp sideload`, then `adb sideload <zip>`).
  It reports success even when the zip stops with an error, so check its `.txt`
  file afterwards.
- **If the install stops part-way**, the Echo can still reach TWRP. Run
  `amazon-biscuit-stock-restore` to get back to the stock layout, then start again from
  step 1's Fire OS 6 flash.
- **If stock-restore refuses** with "not restoring over an unknown layout", the
  partition table was left half-written. Do not try other zips; see below.

### Last resort: amonet's bootrom mode

If the Echo can no longer start TWRP, or its partition table is in a state no zip
accepts, amonet's bootrom mode still works. It is entered by holding the
**Mute** (microphone off) button while plugging the Echo in.

`biscuit_gpt_write.py`, beside this guide, writes a partition table through that
mode: for example the stock table from your backup's `raw/gpt-head.bin` and
`raw/gpt-tail.bin` (the disk size is `total_sectors` in its `info.txt`). It needs
the amonet folder and Python with pyserial. For safety it only writes if it is
also given the exact table that is on the Echo now, so this is a manual repair:
open an issue with the zip's `.txt` and `.log` files first, and work out the
files to use there.

## Licences

The installer is MIT-licensed (`LICENSE`). Each zip carries `LICENSE`, `NOTICE`
and `COPYING.GPL-2.0`: the zips include BusyBox (GPL-2.0), and `NOTICE` says
where its source is and lists the other code built into the two programs they
carry.
