#!/usr/bin/env python3
"""The speaker EQ: the stored choice, and the files biscuit-dsp reads for it.

    python3 /usr/bin/biscuit_eq.py --apply     (biscuit-dsp.initd, at start)

THIS FILE IS CORE, because biscuit-dsp is. The choice lives in
/opt/persist/eq.json, which survives a reflash and which "Reset settings"
erases. What the DSP actually applies lives on the root filesystem instead -
/etc/biscuit/user-eq.conf and /etc/biscuit/eq/speaker.fir - and nothing ever
re-derived one from the other. So after a reset the page said Stock while the
DSP kept Off or Custom, and after a reflash the page said Custom while the DSP
played Stock. biscuit-dsp.initd now runs --apply before the DSP starts, which
rewrites the two files from eq.json (or from the default, Stock, when it is
absent), and only when they differ, so an unchanged boot writes nothing.

The settings page and the Home Assistant agent (biscuit-va-leds.py save_eq)
both save through save() here, so there is one implementation of the files.

    Stock  - the factory correction curve, no user bands.
    Off    - a unity impulse in the override directory, which biscuit-dsp
             prefers over the factory curve, so the correction passes through.
    Custom - the factory correction PLUS the three parametric bands.

THE FACTORY CURVE IS NOT SHIPPED. It is Amazon's EQ_50.cfg, which each owner
imports from the backup of their own Echo (Settings > Storage > Files from
stock, or biscuit-import-assets --profile speaker) into FACTORY_CURVE, and
biscuit-dsp reads it there as it is. Until it is imported, Stock plays flat -
the same as Off - and Custom is the three bands alone. Nothing here converts or
copies the curve: the modes only decide whether the override hides it.
"""
import json
import os
import sys

EQ_MODES = ["Stock", "Off", "Custom"]
EQ_BANDS = [("bass", 100.0, 0.7), ("mid", 1000.0, 0.7), ("treble", 6000.0, 0.7)]
EQ_DB_MIN, EQ_DB_MAX = -12.0, 12.0
EQ_STATE = "/opt/persist/eq.json"
EQ_USER_CONF = "/etc/biscuit/user-eq.conf"
EQ_OVERRIDE_FIR = "/etc/biscuit/eq/speaker.fir"
# The owner's imported curve (biscuit-profile-assets.json, profile "speaker").
# biscuit-dsp.c's EQ_FACTORY_CURVE names the same file.
FACTORY_CURVE = "/opt/persist/biscuit/assets/speaker/EQ_50.cfg"
DSP_FIFO_PATH = "/run/biscuit-dsp/control"

# The override FIR for Off, and the header of the user EQ file. Exactly what the
# agent writes, so whichever of the two wrote last, the files are the same.
UNITY_FIR = ("# Speaker correction disabled from Home Assistant.\n"
             "# A unity impulse: one tap of 1.0, so the FIR passes audio\n"
             "# through unchanged.\n"
             "1.0\n")
USER_CONF_HEADER = "# Written by biscuit-va-leds from the Home Assistant EQ sliders.\n"


def factory_curve_imported():
    """True when the owner has imported the factory curve, so Stock and Custom
    include it; False means Stock plays flat. The importer checked its hash;
    biscuit-dsp refuses it again if it is not 1024 coefficients."""
    return os.path.isfile(FACTORY_CURVE)


def load():
    """The stored choice, in the agent's shape; Stock when there is none."""
    try:
        with open(EQ_STATE) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    out = {"eq_mode": d.get("mode", "Stock")}
    if out["eq_mode"] not in EQ_MODES:
        out["eq_mode"] = "Stock"
    for name, _fc, _q in EQ_BANDS:
        try:
            v = float(d.get(name, 0.0))
        except (TypeError, ValueError):
            v = 0.0
        out["eq_" + name] = max(EQ_DB_MIN, min(EQ_DB_MAX, v))
    return out


def user_conf_text(s):
    lines = [USER_CONF_HEADER]
    if s["eq_mode"] == "Custom":
        for name, fc, q in EQ_BANDS:
            gain = s["eq_" + name]
            if abs(gain) > 0.05:
                lines.append("peak %.0f %.2f %+.1f\n" % (fc, q, gain))
    return "".join(lines)


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _peaks(text):
    """The bands a user EQ file actually applies, parsed the way biscuit-dsp
    does (a line starting `peak f q g`); comments and layout do not count."""
    out = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "peak":
            try:
                out.append(tuple(round(float(x), 2) for x in parts[1:4]))
            except ValueError:
                continue
    return out


def _write(path, text):
    """Atomic, so the DSP never reads half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_dsp_files(s, at_boot=False):
    """Make the DSP's two files say what `s` says. True if anything changed.

    At boot the files are only touched when what they APPLY differs, so the
    shipped, commented user-eq.conf survives an untouched device and an
    unchanged boot writes nothing. Nor is anything an owner put there by hand
    undone at boot: an override FIR is removed only when it is the unity
    impulse written here, and a user-eq.conf that applies bands but was not
    written here (no USER_CONF_HEADER) is left alone. An explicit save() still
    replaces both, as the shipped user-eq.conf says.
    """
    changed = False
    fir = _read(EQ_OVERRIDE_FIR)
    if s["eq_mode"] == "Off":
        if fir != UNITY_FIR:
            _write(EQ_OVERRIDE_FIR, UNITY_FIR)
            changed = True
    elif fir is not None and (not at_boot or fir == UNITY_FIR):
        try:
            os.remove(EQ_OVERRIDE_FIR)
            changed = True
        except OSError:
            pass
    want = user_conf_text(s)
    have = _read(EQ_USER_CONF)
    hand_edited = (have is not None and not have.startswith(USER_CONF_HEADER)
                   and _peaks(have))
    if have != want and not (at_boot and have is not None
                             and (_peaks(have) == _peaks(want) or hand_edited)):
        _write(EQ_USER_CONF, want)
        changed = True
    return changed


def reload_dsp():
    """Ask a running biscuit-dsp to re-read its files. Never blocks."""
    try:
        fd = os.open(DSP_FIFO_PATH, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, b"reload" + bytes([10]))
        finally:
            os.close(fd)
    except OSError:
        pass


def save(s):
    """Store the choice, then apply it to the running DSP."""
    os.makedirs(os.path.dirname(EQ_STATE), exist_ok=True)
    tmp = EQ_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"mode": s["eq_mode"],
                   "bass": s["eq_bass"], "mid": s["eq_mid"],
                   "treble": s["eq_treble"]}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, EQ_STATE)
    try:
        fd = os.open(os.path.dirname(EQ_STATE), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    write_dsp_files(s)
    reload_dsp()
    return True


def main(argv):
    if argv[1:] != ["--apply"]:
        sys.stderr.write("usage: biscuit_eq.py --apply\n")
        return 2
    s = load()
    try:
        changed = write_dsp_files(s, at_boot=True)
    except OSError as err:
        sys.stderr.write("biscuit_eq: cannot apply %s: %s\n" % (s["eq_mode"], err))
        return 1
    if changed:
        sys.stdout.write("biscuit_eq: applied %s from %s\n" % (s["eq_mode"], EQ_STATE))
        # Harmless before the DSP is up (nothing is listening); useful when
        # this is run by hand against a running one.
        reload_dsp()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
