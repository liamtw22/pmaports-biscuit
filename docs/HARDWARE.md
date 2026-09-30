# Hardware

The Amazon Echo Dot 2nd generation: model RS03QR, released 2016, codename
"biscuit". This page lists what is inside it and how well each part is
supported. Driver names are the kernel's; patch numbers refer to
`device/testing/linux-amazon-biscuit/`.

## Summary

| Part | Chip | Support | Driver / how |
|---|---|---|---|
| SoC | MediaTek MT8163, 4× Cortex-A53 | Yes | Mainline-based kernel (`bengris32/linux-mtk`, 7.0-rc6), running 64-bit. Secondary CPUs are started by a custom SMP path (0013), because the firmware's PSCI `CPU_ON` reports success without powering the CPU |
| RAM | 512 MB | Yes | About 470 MB usable |
| Storage | eMMC, 3.9 GB | Yes | See [Storage layout](#storage-layout) |
| PMIC | MediaTek MT6323 | Yes | Regulators, RTC. Several board rails are forced on (0044, 0045) |
| Wi-Fi | MT8163's built-in connectivity block (CONSYS) with the MT6625L radio | Yes | 2.4 and 5 GHz. MediaTek's out-of-tree `mt6625l` driver, ported from Amazon's 3.18 GPL source. Firmware is the owner's own |
| Bluetooth | Same CONSYS block | Yes | New drivers for the CONSYS power sequence, the BTIF UART and the STP/WMT transport (`mt8163-consys`, `mt8163-btif`, `hci_mt8163_stp`). A2DP, HFP (call audio through BTCVSD), LE. Shares the radio with Wi-Fi, see below |
| Speaker | TI TLV320AIC32x4 codec + Awinic AW8736 class-D amplifier | Yes | `tlv320aic32x4` plus a new machine driver `mt8163-biscuit` |
| Headphone jack | Same codec | Yes | Output only, with plug detection. There is no line-in |
| Microphones | 7 microphones on 4 TI TLV320ADC3101 stereo ADCs, collected by an FPGA | Yes | `tlv320adc3xxx` with TDM slot support added (0052-0055, 0114, 0115), and a new driver `dough-fpga` for the FPGA (0050, 0051). FPGA bitstream is the owner's own |
| Light ring | ISSI IS31FL3236A, 12 RGB segments | Yes | `leds-is31fl32xx` |
| Mute LED | GPIO | Yes | `gpio-leds` (0096) |
| Buttons | Action, volume up, volume down, microphone mute | Yes | `gpio-keys` (0095) |
| Ambient light sensor | 2584TSV (bound as `tsl2583`) | Yes | Read by `biscuit-als`, see below |
| Thermal | SoC thermal sensor | Yes | Thermal zones and CPU cooling (0098, 0100, 0101) |
| RTC | In the MT6323 | Partial | Loses the time on power loss; implausible dates are reset to 2010-01-01 (0117) |
| USB | Micro-USB, MUSB controller, device mode | Yes | Power plus optional gadget functions: RNDIS network, USB Audio Class 2 microphone and speaker |
| UART | Test pads | Yes | 921600 8N1, bootloader only by default. The amonet-biscuit v2 thread shows the pads |
| Power | Mains through micro-USB | n/a | No battery. No suspend: the SoC offers only s2idle and nothing useful can wake it |

## Notes on individual parts

### Wi-Fi and Bluetooth

There is no separate Wi-Fi chip on a bus to probe. The radio is the MT8163's
on-die connectivity subsystem, driven through fixed registers. No mainline
driver covers it, so the port uses MediaTek's vendor Wi-Fi driver, ported from
Linux 3.18 (`mt6625l-wlan-20260823.tar.gz`), and new drivers for the power
sequencing and the Bluetooth transport.

- **Firmware** is copied from the owner's own Fire OS 6 system by the backup
  zip and kept on the persist partition:
  - The two Bluetooth ROM patches must be in the initramfs, because the
    built-in Bluetooth driver asks for them about a second after power-on,
    before the root filesystem is mounted. The boot image written at install
    time has them appended.
  - The Wi-Fi firmware, imported as `WIFI_RAM_CODE` (Fire OS's
    `WIFI_RAM_CODE_8163`), is not in the initramfs. `wlan_mt6625l` is a
    loadable module that comes up about 35 s after power-on and reads it from
    `/lib/firmware` on the root filesystem, where `biscuit-persist` copies it
    from the persist partition early in every boot.
- **Coexistence:** Wi-Fi and Bluetooth share one radio. Bluetooth audio (A2DP,
  and the eSCO link of a call) takes most of the 2.4 GHz airtime, so Wi-Fi on
  2.4 GHz slows to a trickle while it runs. 5 GHz Wi-Fi is unaffected.
- **Bluetooth address:** read from the unit's identity block (IDME) and
  programmed into the controller at start-up (0097).
- **Calls:** the controller does not send call audio over HCI. It leaves it in a
  shared memory bank, which mainline's BTCVSD driver (`CONFIG_SND_SOC_MTK_BTCVSD`)
  reads (0120).

### Microphone array

The seven microphones are on four TLV320ADC3101 stereo ADCs that share one TDM
bus (DSP_B framing, 24-bit slots). The first ADC is the bus clock master; the
others take their clock from the bus. An FPGA (driver name "dough") collects the
bus and presents nine channels to the SoC:

| Channel | Source |
|---|---|
| 0-5 | Microphones on ADCs A, B and C (left and right) |
| 6 | ADC D left: the seventh microphone |
| 7, 8 | A digital copy of the speaker signal (the same samples twice), used as the echo-cancellation reference |

The FPGA only passes microphone data while the speaker output is clocking, so
the audio service keeps playback open. Its bitstream (`i2s_to_spi_v34.bin`) is
the owner's own, from the backup.

### Speaker path

The codec drives the AW8736 amplifier. Playback goes through PipeWire and a DSP
service (`biscuit-dsp`): speaker correction, a multiband compressor/limiter and
the volume taper. The correction curve is imported from the owner's Echo; with
none imported the speaker plays flat. The volume ceiling is the same as
Fire OS's.

### Light ring and light sensor

The ring's 36 LED channels (12 segments × RGB) are exposed as Linux LED class
devices (`biscuit:ring:segment-NN:red` and so on). The sensor's mainline driver
applies another part's coefficients and reads 0 lux in a lit room, so
`biscuit-als` reads the raw channels and applies the unit's factory calibration
from IDME; the result is in `/run/biscuit-als/lux`.

### Per-unit calibration

Each Echo carries factory calibration (Bluetooth and Wi-Fi addresses, radio
calibration, microphone trims, the light sensor's coefficient, the serial
number) in an identity block called IDME, in the eMMC's second hardware boot
area (`mmcblk0boot1`), outside the partition table. The bootloader passes it to
the kernel in the device tree. The backup zip saves both boot areas; nothing
writes to them.

## Storage layout

The eMMC has a GPT with the stock partitions. The install zip keeps the
bootloader partitions and merges Fire OS's system, cache and userdata space
into one `userdata` partition, which holds a second, nested partition table
for the operating system. After a zip install:

| Partition | Size | Contents |
|---|---|---|
| `kb`, `dkb`, `lk_a`, `lk_b`, `tee1`, `tee2`, `expdb`, `misc` | small | Bootloaders and firmware: never touched |
| `persist` | 16 MiB | Mounted at `/opt`: settings, the owner's imported files, SSH host keys. Survives updates; the install zip recreates it and restores the imported files from the backup |
| `boot_a`, `boot_b` | 16 MiB each | The boot image (kernel, device tree, initramfs with the owner's Bluetooth and FPGA firmware appended) |
| `recovery` | 16 MiB | TWRP, from amonet |
| `userdata` | about 3.5 GiB | Nested GPT: `pmOS_boot` (ext2, `/boot`) and `pmOS_root` (ext4, `/`), grown to fill the space on first boot |
| `system_a`, `system_b` | 1 MiB each | Empty stubs at the end of the disk. amonet v2's bootloader looks them up by name on every boot and hangs if they are missing |

Always find partitions by name on the device in front of you: the numbers
differ between a stock Echo and an installed one.

## Booting

amonet-biscuit v2 runs the Fire OS 6 bootloader with its own hooks (kaeru).
The boot image is Android header version 0 with 2048-byte pages. The kernel
must carry the MediaTek `KERNEL` header (magic `88 16 88 58`), and the kernel
command line must say `bootopt=64S3,32N2,64N2`: kaeru boots the kernel in the
mode the last field names, and `32N2` sends an arm64 kernel down the 32-bit
path. The device tree is appended to the kernel.

## Device tree

The board's device tree is `arch/arm64/boot/dts/mediatek/mt8163-amazon-biscuit.dts`
in the kernel tree. Upstream's version covers only the keys and USB; 31 of the
port's patches extend it to 918 lines: codec, amplifier, the microphone FPGA and
its four ADCs, the light sensor, the LED ring, CONSYS Wi-Fi and Bluetooth,
BTCVSD, thermal and ramoops. The patched file is readable in branch
`biscuit-r243` of `liamtw22/linux-mtk`.
