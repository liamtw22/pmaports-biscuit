# Legacy host tools (superseded)

These are the first installer: Python scripts run on a computer that drove the
Echo over adb and fastboot. The TWRP zips in `../zip/` replaced them, and
`../INSTALL.md` does not use them. They are kept for reference only.

| File | What it was |
|---|---|
| `biscuit_install.py` | A three-step export / repartition / flash install over adb and fastboot |
| `biscuit_backup.py` | A whole-disk backup through `adb exec-out dd`, about 35 minutes |
| `layout_check.py`, `test_layout_check.py` | The partition-table checks `biscuit_install.py` used, and their tests |
| `biscuit-profile-assets.json` | The owner-asset manifest `biscuit_install.py` read. It is a snapshot: the current manifest is inside the image, at `/usr/share/biscuit/biscuit-profile-assets.json` |

Do not use them to install. They diverge from the zips: no light ring
animations, no backup format 2, and `biscuit_install.py flash` produces a
different install from the documented one.

The emergency tool is not here: `../biscuit_gpt_write.py` rewrites the
partition table through amonet's bootrom mode when neither TWRP nor fastboot
can start. See "Last resort" in `../INSTALL.md`.

## Where the installer lives

Until r294 the installer was kept in a separate, unpublished repository. It
moved into this one, as `installer/`, at r294 without its history; `installer/`
is the only source of the zips from then on.
