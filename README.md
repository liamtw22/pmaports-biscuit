# postmarketOS for the Amazon Echo Dot (2nd gen)

Linux on the 2016 Echo Dot (model RS03QR, codename "biscuit", MediaTek MT8163),
built on postmarketOS (renamed Nura in September 2026) with a mainline-based
7.0-rc6 kernel. It turns the Dot into a local Home Assistant voice satellite, a
Music Assistant speaker and a Bluetooth speaker, with a web settings page. No
Amazon account and no cloud service are involved.

**Experimental.** Installing it means unlocking the Echo with an exploit and
repartitioning it, and either can brick the device. Read [Requirements](#requirements)
and the installation guide before starting.

Not affiliated with Amazon. Echo and Alexa are trademarks of Amazon.com, Inc.
or its affiliates.

## What works

| Feature | Status | Notes |
|---|---|---|
| Wi-Fi | Yes | 2.4 and 5 GHz. First setup through a hotspot the Echo opens itself |
| Bluetooth speaker (A2DP) | Yes | Phones and computers play to the Echo |
| Bluetooth casting | Yes | The Echo plays through another Bluetooth speaker |
| Bluetooth calls (HFP) | Partial | Hands-free calls through a paired phone, with echo cancellation and suppression. Works; call audio quality is still being tuned |
| Home Assistant Bluetooth proxy | Yes | Over the ESPHome API, port 6054. Off by default, because scanning costs about half the 2.4 GHz Wi-Fi throughput; switch it on in the settings page's Home Assistant section |
| Speaker | Yes | Speaker correction, multiband limiter, volume ceiling at the stock level. The correction curve is imported from your own Echo; without it the speaker plays flat |
| Headphone jack | Yes | Output only, with plug detection |
| Microphones and voice assistant | Yes | Optional app. All 7 microphones, a beamforming and echo-cancelling front end, on-device wake word (microWakeWord; "Alexa" by default, others selectable), Home Assistant Assist through linux-voice-assistant (ESPHome API, port 6053), timers, ducking of music while you speak |
| Music Assistant speaker | Yes | Optional app. Sendspin player |
| Light ring | Yes | 12 RGB LEDs: activity patterns, talker direction, volume, timers, generated ambient effects, and your own Echo's stock animations if you import them |
| Buttons and mute | Yes | Action, volume up/down and microphone mute with its red LED |
| Ambient light sensor | Yes | Automatic ring brightness; illuminance sensor in Home Assistant |
| Settings page | Yes | `http://<name>.local:8080`: Wi-Fi, Bluetooth, sound, microphones, ring, apps, updates, USB, date and time |
| Home Assistant entities | Yes | With the voice app: device settings (volume, ring, microphones, sounds and more) appear as entities through the ESPHome integration and stay in sync both ways |
| Updates | Yes | Signed package feed; kernel and boot updates installed from the settings page |
| Time | Yes | chrony, plus Home Assistant's clock as a fallback source |
| USB | Yes | Charging only by default. Network (RNDIS), microphone and speaker (USB Audio Class 2) can each be switched on |
| Crash capture | Yes | Kernel logs survive a crash and reset (ramoops) |
| Line-in | No | The hardware has none: the jack is output only |
| Suspend | No | The SoC offers no useful sleep state, and the device must keep listening |
| Alexa / Amazon services | No | Not part of this project |

## Known issues

- **Bluetooth audio starves 2.4 GHz Wi-Fi.** Wi-Fi and Bluetooth share one
  radio. While the Echo plays or sends Bluetooth audio, or carries a call, Wi-Fi
  throughput on 2.4 GHz collapses, and a Music Assistant stream stalls once its
  buffer runs dry. Put the Echo on a 5 GHz network if you use both.
- **The clock starts at 2010-01-01 after power loss** and stays wrong until the
  Echo gets the time from the network or from Home Assistant. Until then,
  anything that checks certificates (HTTPS, the package feed) fails. The
  hardware clock is unreliable; the kernel resets impossible dates to 2010.
- **The kernel is a release-candidate fork.** It is `bengris32/linux-mtk` at
  7.0-rc6 plus 109 patches, with MediaTek's out-of-tree vendor Wi-Fi driver. It
  still carries some diagnostic patches.
- **The setup hotspot is an open network,** and the settings page is plain HTTP
  (no TLS). SSH logins are refused from the setup network and the settings page
  is stopped during setup, but the setup network is not firewalled: on a device
  that is already set up, the unauthenticated ESPHome APIs of the voice
  assistant and the Bluetooth proxy, when they are on, stay reachable from it.
  See [SECURITY.md](SECURITY.md).
- **Pulling the power can lose the last few seconds of logs.** Log files then
  contain runs of NUL bytes.
- **Going back to Fire OS has not been tested end to end.** The stock-restore
  zip puts back the stock partition table and this Echo's partitions, but a full
  restore followed by a Fire OS 6 boot has not been verified on a device yet.
- **Few units tested.** It has been developed on two Echos. One of them has
  been installed the way the guide describes, from a factory-fresh unit.

## Requirements

- An Echo Dot **2nd generation** (2016, model **RS03QR**). No other Echo.
- The Echo unlocked with **amonet-biscuit v2** (by k4y0z and R0rt1z2, on
  [XDA](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/)),
  which also installs TWRP.
- **Fire OS 6 flashed on both slots**, as amonet's guide describes. This project
  was tested with `update-kindle-biscuit_puffin-NS6574_user_7623_0013121734532.bin`
  (Fire OS 6.5.7.4). The backup collects the Wi-Fi, Bluetooth and microphone
  firmware from Fire OS 6; it cannot collect them from an empty or Fire OS 5
  system.
- A computer with `adb` (Android platform-tools), and a micro-USB **data** cable.
- A phone or laptop with Wi-Fi for setup, and Home Assistant if you want the
  voice assistant.

## Installing

Get the zips from the [latest release](https://github.com/liamtw22/pmaports-biscuit/releases)
and check them against its `SHA256SUMS`. Then follow the installation guide,
[`installer/INSTALL.md`](installer/INSTALL.md). In short:

1. Unlock with amonet-biscuit v2 and install Fire OS 6 on both slots.
2. `amazon-biscuit-backup.zip` in TWRP: saves everything unique to this Echo,
   including its factory calibration, and collects its firmware and other
   Amazon files. Copy the backup to your computer and keep it: it is the only
   way back to Fire OS.
3. `pmos-amazon-biscuit-vX.Y.zip` in TWRP: replaces Fire OS. About 5 minutes.
4. Reboot. The Echo opens a Wi-Fi network named `biscuit-XXXX`; join it from a
   phone and the setup page opens. Choose your account, your Wi-Fi network and
   the optional apps.

| Zip | What it is for |
|---|---|
| `amazon-biscuit-backup.zip` | Save what is unique to this Echo, before anything changes |
| `pmos-amazon-biscuit-vX.Y.zip` | Install this project, replacing Fire OS |
| `amazon-biscuit-restore.zip` | Put this Echo's own files back into an installed system |
| `amazon-biscuit-stock-restore.zip` | Take the Echo back to the stock layout, ready for Fire OS 6 |

**Updating:** the Echo checks this project's package feed
(`https://liamtw22.github.io/biscuit-apk/edge`), which is on by default and
put back on every boot unless you turn it off; the settings page's Updates
section installs what it finds, including kernel and boot updates. To stop
using the feed, or to turn it back on, see
[SECURITY.md](SECURITY.md#the-package-feed).

## What is not included

Nothing Amazon ships is in this repository, in the packages or in the zips: no
firmware, no audio tuning, no sounds, no light animations. The backup zip
collects them **from your own Echo**, and the install stores them on the Echo's
settings partition, where they survive reinstalls. They include the Wi-Fi and
Bluetooth firmware, the microphone FPGA's bitstream, the speaker correction
curve, the stock sounds and ring animations, and the Fire OS microphone
configuration used by the optional "stock" microphone profiles.

A few small numeric parameters were measured from Fire OS's behaviour and are
reimplemented here: the 30 volume steps, the multiband limiter's settings, the
light sensor's brightness table, some beamformer constants and the timings of
the ambient ring effects. [PROVENANCE.md](PROVENANCE.md) lists them.

## Repository layout

| Path | What it is |
|---|---|
| `device/testing/device-amazon-biscuit/` | The device package: services, audio DSP, settings page, setup, and the optional apps as subpackages |
| `device/testing/linux-amazon-biscuit/` | The kernel package: upstream source pin, 109 patches (the device tree is built by them), the MT6625L Wi-Fi driver, the kernel configuration |
| `base-pmaports/` | The three patches to upstream pmaports this port is built on |
| `installer/` | The TWRP zips' source, their build script and tools, and the installation guide |
| `tools/` | Developer tools; see [tools/README.md](tools/README.md) |
| `docs/HARDWARE.md` | What is inside the Echo and how each part is supported |

The kernel is also published as a git tree:
[`liamtw22/linux-mtk`](https://github.com/liamtw22/linux-mtk/tree/biscuit-r243),
branch `biscuit-r243`, where the device tree is readable as one file and the
Wi-Fi driver's changes are a separate commit on top of Amazon's original
files.

## Building

See [BUILDING.md](BUILDING.md): the pmaports base, the kernel, the packages, the
image and the zips, from scratch.

## Reporting problems

Open an issue on this repository. Include the build number (the settings
page's About section shows it as Device software) and, for an install problem,
the `.txt` and `.log` files the zip left in TWRP's `/tmp`. For security
problems, see [SECURITY.md](SECURITY.md).

## Licences

The port's own code is MIT (see [LICENSE](LICENSE)). The kernel and its patches
are GPL-2.0, `base-pmaports/` is pmaports material under pmaports' own licence
(the `boot-deploy` change in it is GPL-2.0-or-later), the installer's zips
carry BusyBox (GPL-2.0), and bundled third-party code keeps its own licence.
[NOTICE](NOTICE) and [PROVENANCE.md](PROVENANCE.md) list every third-party
component and its licence, and [`LICENSES/`](LICENSES/) holds the full licence
texts.

## Credits

This builds on the work of many people:

- **amonet-biscuit:** k4y0z, who made the first amonet port for this device;
  R0rt1z2 (Rortiz2), who made amonet-biscuit v2 and its TWRP; xyz\`, whose
  original amonet exploit for karnak made it possible; and AntiEngineer, credited
  in amonet v2 for the UART work.
- **TWRP:** the TWRP project, and Amazon's open-source recovery tree
  (`amazon-oss/android_bootable_recovery`).
- **Kernel:** bengris32's `linux-mtk`, the mainline-based MT8163 kernel this
  port's kernel is built from; the Linux kernel's contributors; MediaTek and
  Amazon, whose GPL kernel source provided the MT6625L Wi-Fi driver.
- **The original postmarketOS port:** Ben Westover (2023) and Connor Eliffe,
  whose vendor-kernel port of this device came first.
- **Base system:** postmarketOS / Nura and pmbootstrap, and Alpine Linux.
- **Voice:** the Open Home Foundation's linux-voice-assistant, microWakeWord
  and pymicro-wakeword, TensorFlow Lite, and ESPHome's aioesphomeapi.
- **Music:** Music Assistant and Sendspin (sendspin-cli, aiosendspin), PyAV and
  FFmpeg.
- **Audio and Bluetooth:** BlueZ, bluez-alsa, PipeWire and WirePlumber,
  SpeexDSP, and chrony for time.
- **Settings page:** TweetNaCl-js.

Maintained by liamtw22 and contributors.
