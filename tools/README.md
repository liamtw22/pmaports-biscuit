# tools

Developer tools. None of these is installed on the Echo or needed to use it;
the packages and the TWRP zips are all an owner needs.

| Tool | Runs on | What it is for |
|---|---|---|
| `export_stock_assets.py` | A computer | Exports the Fire OS files the port can import (firmware, microphone configuration, speaker correction curve, sounds, ring animations) from an extracted Fire OS system tree (`--from-tree DIR`) or a rooted stock Echo over adb (`--from-adb SERIAL`), and reports which of them a tree can supply. The backup zip does this job for a normal install; this is for working with firmware dumps and for older, adb-based routes |
| `provenance.py` | A computer | Regenerates `../PROVENANCE.md` from the binary archives the device package bundles: every Python distribution with its version, declared licence and whether its licence text ships, and every native library with what it links against. Run from the repository root: `python3 tools/provenance.py > PROVENANCE.md` |
| `make_setup_chime.py` | A computer | Generates `biscuit-setup-chime.wav`, the port's own two-note chime for setup mode (played when the owner has not imported the stock sounds). Deterministic: `python3 tools/make_setup_chime.py OUT.wav` always writes the same bytes |
| `settings-capture.py` | The Echo, as root | Snapshots every read-only `/api` endpoint of the settings page as JSON, by loading `biscuit-settings.py` and calling the same functions its request handler does. Changes nothing |
| `settings-mock.py` | A computer | Serves the settings page locally against a capture from `settings-capture.py`, re-reading the page source on every request. For working on the page's layout and wording; it proves nothing about the back ends, which have to be tested on a device |
| `beamform/` | A computer, with numpy | Regenerates the shipped beamformer weight files from the microphone-array geometry in `biscuit-mic-array.json`: `biscuit-superdirective.py` computes the six-beam superdirective (MVDR) design and `export-beam-weights.py` writes the binary the device reads. Both files regenerate to within about 3e-15 of the shipped ones; the last bits can differ between numpy builds |

## Typical use

Working on the settings page without a device in the loop:

```sh
# on the Echo
sudo python3 settings-capture.py > capture.json
# on the computer, from the repository root
python3 tools/settings-mock.py capture.json --port 8099
```

Regenerating the beamformer weights, from the repository root:

```sh
cd tools/beamform
python3 biscuit-superdirective.py --out W.npz
python3 export-beam-weights.py --weights W.npz --out biscuit-beam-weights.bin
# the stock-profile grid
python3 biscuit-superdirective.py --fft 128 --hop 64 --out W-stock.npz
python3 export-beam-weights.py --weights W-stock.npz --out biscuit-beam-weights-stock.bin
```

After rebuilding one of the bundled virtualenv or library archives in
`device/testing/device-amazon-biscuit/`:

```sh
python3 tools/provenance.py > PROVENANCE.md
git diff PROVENANCE.md
```

## Not here

- The package feed's publishing script is `tools/publish-feed.sh` in the
  `liamtw22/biscuit-apk` repository.
- The TWRP zip builder is [`installer/zip/build_zips.py`](../installer/zip/build_zips.py),
  and the installer's own tools are in [`installer/tools/`](../installer/tools/);
  see [`../BUILDING.md`](../BUILDING.md).
