#!/usr/bin/env python3
"""Audio input handling for the Echo Dot 2 (biscuit).

Two jobs, both of which need something watching /dev/input:

  1. the microphone mute button, and
  2. switching the UCM output device when the 3.5 mm jack is plugged or
     unplugged, since the kernel's jack detection moves the DAC mux and the
     amplifier but cannot touch the codec routing and gains, which live in UCM.
     The jack state is published in /run/biscuit-audio/headphones for the
     settings page and Home Assistant, so nothing else reads the hardware.

Started life as biscuit-mute and was renamed once it grew the second job.

The mute button watches the PMIC key device for KEY_MUTE and toggles two things
together:

  - the seven-microphone array, by turning the four ADC capture switches off,
    which gives digital silence on every channel;
  - the red LED under the button, on GPIO87.

Why this is userspace: the privacy line does NOT gate the microphones on this
board. The vendor sets hw_latch = <0> and mutes them from a software callback
(amz_priv.c, amz_priv_mute_cb / PRIV_CB_MIC), lighting the LED separately. We do
the same thing, just from here.

Two details that cost time and are easy to get wrong again:

  - the mixer controls only exist under their cset names. `amixer sset` cannot
    find them and fails *silently*, leaving the microphones live while
    everything looks fine.
  - the input device is found by name, never by a fixed event number. Adding
    input devices renumbers them: gpio-keys moved from event1 to event3 when
    r188 added the keypad and PMIC key devices.
"""
import fcntl
import glob
import os
import re
import select
import time
import signal
import struct
import subprocess
import threading
import sys

KEY_MUTE = 113
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115
EV_KEY = 1
RELEASE = 0
PRESS = 1
REPEAT = 2

# Held volume keys repeat from here, not from the kernel. gpio-keys reports
# EV=3 - EV_SYN and EV_KEY only, no EV_REP - because the device tree node has
# no `autorepeat` property, so holding a key produces exactly one event and
# nothing else. Adding autorepeat to the DT would need a kernel rebuild and
# would also hand the rate to the kernel default of about 33/s, which crosses
# all thirty steps in under a second. Doing it here keeps the ramp rate a
# userspace decision: a full sweep takes about 4.5 s.
REPEAT_DELAY_S = 0.4
REPEAT_INTERVAL_S = 0.15

# The 3.5 mm jack reports as a switch, not a key: EV=21 sets bit 5 (EV_SW) and
# SW=4 sets bit 2 (SW_HEADPHONE_INSERT) on the jack input device.
EV_SW = 5
SW_HEADPHONE_INSERT = 2
EV_SYN = 0
SYN_DROPPED = 3
# EVIOCGSW(8): _IOC(_IOC_READ, 'E', 0x1b, 8), the switch bitmap of an input
# device. Read on the same fd the events arrive on, so a plug that happens
# between the read and the first select() is queued rather than lost.
EVIOCGSW_8 = 0x8008451b

INPUT_NAME = "mtk-pmic-keys"
KEYS_NAME = "gpio-keys"
JACK_NAME = "mt8163-biscuit Headphone Jack"
LED = "/sys/class/leds/biscuit:mute:red/brightness"
LED_BRIGHT = "/sys/class/leds/biscuit:mute:bright/brightness"

# How bright the mute indicator is WHEN IT IS ON. Written by the peripheral
# agent from a Home Assistant dropdown; read here because this service owns the
# LED and two writers drift the moment either misses an event.
#
# The hardware has two binary channels, so there are exactly three states and
# offering a 0-255 slider for them was a lie. off / low (red only) / high
# (red + bright).
# TWO controls, because they answer different questions.
#
#   mic-led   a MANUAL override. High or Low lights the indicator now, whether
#             or not the microphones are muted. Off means "no override", which
#             is not the same as "lamp off" - it hands the LED back to the mute
#             indicator below.
#   mute-led  what the indicator does WHEN MUTED: high, low, auto or off.
#
# Auto follows the room: the same ambient ceiling that dims the ring, so the
# mute light is not glaring at night. The threshold is deliberately low - this
# is a two-state lamp, and the only question is whether the room is dark.
LED_LEVEL_FILE = "/opt/persist/mic-led"        # manual override
MUTE_LEVEL_FILE = "/opt/persist/mute-led"      # indicator mode when muted
LED_LEVELS = {"off": (0, 0), "low": (1, 0), "high": (1, 1)}
LED_LEVEL_DEFAULT = "off"                      # no override
MUTE_LEVEL_DEFAULT = "high"
ALS_CEILING = "/run/biscuit-als/brightness"
AUTO_LOW_BELOW = 40                            # ceiling 0-255; below this, low
CARD = "0"
UCM_CARD = "mt8163-biscuit"
MICS = ("A", "B", "C", "D")

# Volume. Stock shows thirty steps on the ring (volume_step-01..30) plus
# volume-muted, so the levels here are 0..30 to match one-for-one.
#
# The control is the codec's PCM Playback Volume, 0..175 in 0.5 dB steps with
# 127 = 0 dB. **127 is the ceiling on purpose**: it is stock's policy maximum
# (11.56), and running above it is digital gain into the same DAC. Level 30
# therefore lands exactly where the UCM already leaves it, so wiring the keys
# up changes nothing until a key is actually pressed.
#
# DECIDED 2026-09-26, by the owner: the ceiling stays at stock's 0 dB.
# - At volume 100 biscuit-dsp already sits 11.5 dB into its -0.1 dBFS limiter,
#   so any gain above 127 would only clip the loudest passages.
# - It would also make barge-in harder.
# - Nothing measures the amplifier's temperature.
# Do not raise it here, in either ucm2/HiFi.conf line, or anywhere else.
#
# STOCK_AVL_DB reimplements stock's observed volume steps: these 30 values
# were measured from the speaker AVL settings in the owner's own Fire OS
# (audio-algorithms/AFE.cfg), which uses the same steps for music and TTS.
# They are a handful of numeric parameters, not a copy of the file. The taper
# is deliberately not a generic logarithmic, linear-dB, or squared-amplitude
# one: the lower steps are lifted so they remain audible, while the upper
# steps converge near the 0 dB ceiling.
VOLUME_CONTROL = "PCM Playback Volume"
VOLUME_MAX_REG = 127          # 0 dB, stock's ceiling
VOLUME_STEPS = 30
STOCK_AVL_DB = (
    -90, -33, -30, -26, -25, -23, -21, -19, -17, -16,
    -14, -13, -12, -10,  -9,  -8,  -7,  -5,  -5,  -4,
     -4,  -4,  -4,  -3,  -3,  -3,  -3,  -3,  -2,  -1, 0,
)

# The ring content driver's control FIFO. Absent or unread if biscuit-ring is
# not running, which is not an error here - the audio side must keep working.
RING_FIFO = "/run/biscuit-ring/control"

# BISCUIT: mute is shared with Home Assistant via the peripheral agent.
# This service stays the SINGLE OWNER of the hardware - it is the only
# thing that touches the ADC switches and the LED - and simply publishes
# its state and accepts requests. Two processes both interpreting KEY_MUTE
# would drift the moment either missed an event.
AUDIO_RUN_DIR = "/run/biscuit-audio"
MUTE_STATE = AUDIO_RUN_DIR + "/muted"      # 1/0, the authoritative state
MUTE_CONTROL = AUDIO_RUN_DIR + "/control"  # mute | unmute | toggle | volume N
VOLUME_STATE = AUDIO_RUN_DIR + "/volume"   # 0-100, the authoritative level
# The 3.5 mm jack: "1" plugged, "0" not. ABSENT means unknown - the service is
# stopped, or neither the input device nor the ALSA control could be read - and
# readers must show that rather than guess. The settings page and the Home
# Assistant sensor both read exactly this file.
HEADPHONES_STATE = AUDIO_RUN_DIR + "/headphones"
# "Speaker" or "Headphones": the UCM device last selected SUCCESSFULLY. Differs
# from the jack only when alsaucm failed, which is otherwise invisible.
OUTPUT_STATE = AUDIO_RUN_DIR + "/output"

EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

# How often to re-check the switches while muted. Only ever polled in the muted
# state, so this costs nothing during normal use.
RECHECK_SECONDS = 2.0


def _durable_replace(tmp, dest):
    """os.replace, then make the rename ITSELF durable.

    os.replace is atomic - a reader never sees a half-written file - but it is
    not durable. After it returns, the new directory entry can still be only in
    page cache, and this device is power-cycled rather than shut down: every log
    on it carries NUL runs from exactly that. fsync on the containing directory
    is what commits the rename.

    The caller is expected to have fsynced the file's own contents first; this
    closes the other half. Best effort on purpose - the rename has already
    happened and succeeded by this point, so failing to sync is not worth
    raising over, and a directory that cannot be opened read-only is not a
    situation this can improve.

    Only used for destinations that must survive a power cut. Writes under /run
    are tmpfs, discarded at every boot, and deliberately still use os.replace.
    """
    os.replace(tmp, dest)
    try:
        fd = os.open(os.path.dirname(os.fspath(dest)) or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def log(msg):
    sys.stdout.write("biscuit-audio: %s\n" % msg)
    sys.stdout.flush()


def find_input_device(name):
    for path in sorted(glob.glob("/dev/input/event*")):
        sysfs = "/sys/class/input/%s/device/name" % os.path.basename(path)
        try:
            with open(sysfs) as handle:
                if handle.read().strip() == name:
                    return path
        except OSError:
            continue
    return None


def set_mics(enabled):
    """Turn the four microphone ADC capture switches on or off."""
    value = "1,1" if enabled else "0,0"
    ok = True
    for mic in MICS:
        control = "name=Mic %s PGA Capture Switch" % mic
        result = subprocess.run(
            ["amixer", "-c", CARD, "cset", control, value],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if result.returncode != 0:
            ok = False
            log("failed to set Mic %s: %s"
                % (mic, result.stderr.decode(errors="replace").strip()))
    return ok


def _read_level(path, default, allow_auto=False):
    try:
        with open(path) as handle:
            level = handle.read().strip().lower()
        if level in LED_LEVELS or (allow_auto and level == "auto"):
            return level
    except OSError:
        pass
    return default


def resolve_auto():
    """Auto = follow the room, using the ceiling biscuit-als already computes."""
    try:
        with open(ALS_CEILING) as handle:
            return "low" if int(handle.read().strip()) < AUTO_LOW_BELOW else "high"
    except (OSError, ValueError):
        return "high"


def read_led_level(muted):
    """The level the indicator should show right now.

    The manual override wins when it is set to something. Otherwise the mute
    indicator applies, and only while actually muted - an unmuted device with no
    override shows nothing, which is the whole point of a mute lamp.
    """
    override = _read_level(LED_LEVEL_FILE, LED_LEVEL_DEFAULT)
    if override != "off":
        return override
    if not muted:
        return "off"
    mode = _read_level(MUTE_LEVEL_FILE, MUTE_LEVEL_DEFAULT, allow_auto=True)
    return resolve_auto() if mode == "auto" else mode


def set_led(on):
    """Drive the mute indicator at the level the user picked.

    Both channels are written every time, including the off case: leaving
    :bright lit while :red goes dark would show a white indicator on an
    unmuted device, which reads as a fault rather than a preference.
    """
    red, bright = LED_LEVELS[read_led_level(on)]
    ok = True
    for path, value in ((LED, red), (LED_BRIGHT, bright)):
        try:
            with open(path, "w") as handle:
                handle.write("%d" % value)
        except OSError as err:
            log("cannot write %s: %s" % (path, err))
            ok = False
    return ok


def mics_are_on():
    """True if the first capture switch reads on, None if it cannot be read."""
    result = subprocess.run(
        ["amixer", "-c", CARD, "cget", "name=Mic %s PGA Capture Switch" % MICS[0]],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        return None
    for line in result.stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if line.startswith(": values="):
            return "on" in line
    return None


# biscuit-dsp reproduces stock's per-volume loudness push, which needs to know
# the level. Same fire-and-forget shape as ring(): a FIFO write that is a no-op
# when the DSP is not running, so volume never depends on it.
DSP_FIFO = "/run/biscuit-dsp/control"


def dsp_volume(level):
    """Tell the playback DSP the current level, as a percentage."""
    try:
        if not os.path.exists(DSP_FIFO):
            return
        pct = int(round(level * 100.0 / VOLUME_STEPS))
        fd = os.open(DSP_FIFO, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, ("volume %d\n" % pct).encode())
        finally:
            os.close(fd)
    except OSError:
        # No reader, or the FIFO went away with the service. Not fatal.
        pass


def dsp_wake():
    """Ask the DSP to power the speaker chain up now.

    The DSP releases the codec after ten seconds of silence, which powers the
    amplifier down. Bringing it back takes long enough that the beginning of a
    short earcon was played into an amplifier still on its way up, so the first
    press after a quiet spell sounded like nothing happened. Sent as the button
    is handled, so the wake overlaps the player's own start-up instead of
    adding to it. Fire-and-forget, exactly like ring() and dsp_volume().
    """
    try:
        if not os.path.exists(DSP_FIFO):
            return
        fd = os.open(DSP_FIFO, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, b"wake" + chr(10).encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def ring(command):
    """Send one line to biscuit-ring, if it is listening.

    Non-blocking on purpose. Opening a FIFO for writing blocks until a reader
    exists, so with biscuit-ring stopped a plain open() would wedge the mute
    button. O_NONBLOCK turns that into ENXIO, which is ignored.
    """
    try:
        fd = os.open(RING_FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        os.write(fd, (command + "\n").encode())
    except OSError:
        pass
    finally:
        os.close(fd)


# The codec no longer holds the level, so it cannot be recovered from the mixer
# at start-up: the register reads unity at every volume. Keep it here instead,
# in the store that survives a reboot and a flash. Distinct from VOLUME_STATE,
# which is the /run publication other services follow and must stay there.
VOLUME_PERSIST = "/opt/persist/volume"


def save_volume(level):
    """Persist the level. Same-directory temp then replace, so a crash part way
    through cannot leave a truncated file that reads back as volume 0."""
    try:
        tmp = VOLUME_PERSIST + ".tmp"
        with open(tmp, "w") as handle:
            handle.write("%d\n" % level)
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(tmp, VOLUME_PERSIST)
    except OSError as err:
        log("cannot persist volume: %s" % err)


def load_volume():
    """Persisted level, or None when there is nothing to restore."""
    try:
        with open(VOLUME_PERSIST) as handle:
            level = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    return max(0, min(VOLUME_STEPS, level))


def get_volume_reg():
    """Current PCM Playback Volume, or None."""
    try:
        out = subprocess.run(
            ["amixer", "-c", CARD, "cget", "name=" + VOLUME_CONTROL],
            capture_output=True, text=True, check=False).stdout
    except OSError:
        return None
    m = re.search(r": values=(\d+)", out)
    return int(m.group(1)) if m else None


def reg_to_level(reg):
    """Map a mixer value onto 0..30, rounding to the nearest step."""
    if reg <= 0:
        return 0
    # Some stock AVL entries intentionally share a dB value. Prefer the upper
    # matching step at a tie so a restart does not make the ring appear lower.
    return min(range(1, VOLUME_STEPS + 1),
               key=lambda level: (abs(reg - level_to_reg(level)), -level))


def level_to_reg(level):
    level = max(0, min(VOLUME_STEPS, int(level)))
    if level == 0:
        return 0
    db = STOCK_AVL_DB[level]
    reg = int(round(VOLUME_MAX_REG + db / 0.5))
    return max(0, min(VOLUME_MAX_REG, reg))


# The visible part of a volume_step animation is its single 2 s frame; after
# that it loops on a blank one. Both numbers below follow from that.
VOLUME_SHOW_S = 2.0

# Last volume animation handed to the ring, so it can be retired explicitly.
_last_volume_anim = None

ANIM_ENV = "/opt/persist/led-anim.env"
MUTE_ANIM_DEFAULT = "act_mute"
_mute_anim = None


def mute_animation():
    """The animation biscuit-settings resolved for the `mute` activity.

    This used to be the hard-coded stock name, which made the mute ring the one
    thing on the device a user could see and not change - and, once the stock
    artwork stopped being shipped, the one thing that would silently play
    nothing. Read on each mute rather than cached: the settings page rewrites
    this file the moment the activity changes, and the mute ring is exactly
    what someone would change and then immediately test.
    """
    try:
        with open(ANIM_ENV) as handle:
            for line in handle:
                key, _, value = line.strip().partition("=")
                if key == "LED_mute":
                    name = value.strip().strip("'\"")
                    # The same guard the shell consumers apply: anything that is
                    # not a bare identifier cannot be pasted into a play line.
                    if name and re.match(r"^[A-Za-z0-9_-]+$", name):
                        return name
    except OSError:
        pass
    return MUTE_ANIM_DEFAULT



# ---------------------------------------------------------------------------
# The volume tone
# ---------------------------------------------------------------------------
#
# Stock plays a short tone on every volume change, and the tone IS the feedback:
# it goes out through the same chain as everything else, so it lands at the
# level just set and you hear how loud that level actually is.
#
# It cannot simply be played once per step. The tone runs 0.384 s and a held
# volume key repeats every 0.15 s, so one per step would stack three deep and
# turn into noise. Instead: play immediately when the last tone is far enough
# behind, otherwise arm a single trailing timer. That gives one tick when a
# slide starts and one at the level it settles on - and the settling tone is the
# one that matters, because releasing the button just after a tone would
# otherwise leave the final change unconfirmed.
VOLUME_TONE_ACTIVITY = "volume_changed"
VOLUME_TONE_MIN_S = 0.45
SOUND_MAP_FILE = "/opt/persist/sound-map.json"
# Where biscuit-earcon keeps the one thing that reports a failed play.
EARCON_RUN_DIR = "/run/biscuit-earcon"

_tone_path = None        # resolved file, or "" for "no sound for this activity"
_tone_map_mtime = None
_tone_last = 0.0
_tone_timer = None


def tone_path():
    """The tone's file, resolved through biscuit-earcon and cached.

    Resolved by asking biscuit-earcon rather than naming the file here, so the
    activity->sound table stays the single source of truth and a sound changed
    on the settings page is picked up without a second copy to keep in sync.
    Cached because resolving costs a Python start-up, which is far too slow to
    pay on a volume tick; re-resolved only when the map file changes.
    """
    global _tone_path, _tone_map_mtime
    try:
        mtime = os.stat(SOUND_MAP_FILE).st_mtime
    except OSError:
        mtime = 0
    if _tone_path is None or mtime != _tone_map_mtime:
        _tone_map_mtime = mtime
        try:
            out = subprocess.run(
                ["/usr/bin/biscuit-earcon", "--path", VOLUME_TONE_ACTIVITY],
                capture_output=True, text=True, timeout=10)
            _tone_path = out.stdout.strip()
        except Exception as err:  # noqa: BLE001
            log("volume tone: cannot resolve (%s)" % err)
            _tone_path = ""
    return _tone_path


def _tone_play():
    global _tone_last
    _tone_last = time.monotonic()
    path = tone_path()
    if not path:
        return
    # stderr goes to the file biscuit-earcon uses, NOT to /dev/null.
    # sndfile-play exits 0 even when it cannot open the audio device, so
    # discarding stderr left this path structurally unable to report the one
    # failure mode that is known to be silent here - and that is exactly what
    # happened: every earcon went to the USB gadget sink for days, reporting
    # nothing, because this was the only caller that threw its stderr away.
    try:
        os.makedirs(EARCON_RUN_DIR, exist_ok=True)
        errf = open(os.path.join(EARCON_RUN_DIR, "last-error"), "wb")
    except OSError:
        errf = None
    try:
        # Detached and never waited on: the volume key must stay responsive.
        subprocess.Popen(["sndfile-play", path],
                         stdout=subprocess.DEVNULL,
                         stderr=errf if errf is not None else subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as err:
        log("volume tone did not play: %s" % err)
    finally:
        if errf is not None:
            errf.close()


def volume_tone():
    """Play now, or schedule the trailing tone. See the note above."""
    global _tone_timer
    if _tone_timer is not None:
        _tone_timer.cancel()
        _tone_timer = None
    wait = VOLUME_TONE_MIN_S - (time.monotonic() - _tone_last)
    if wait <= 0:
        _tone_play()
        return
    _tone_timer = threading.Timer(wait, _tone_play)
    _tone_timer.daemon = True
    _tone_timer.start()


def set_volume(level):
    """Apply a level and show it on the ring, the way stock does."""
    global _last_volume_anim

    # The codec stays at unity and the DSP carries the whole volume law.
    #
    # Attenuating here meant MBCL always saw a full-scale signal, so quiet
    # listening was compressed exactly as hard as loud listening and the level
    # only came off afterwards. Stock attenuates before its compressor and
    # leaves its codec alone - its HP driver gain register reads 0x00 at every
    # volume. biscuit-dsp is the only writer of hw:0,0, so if it is not running
    # there is no playback at all; a unity codec cannot leak full-scale audio.
    reg = VOLUME_MAX_REG
    # cset, not sset: sset cannot find these controls and fails silently.
    subprocess.run(["amixer", "-c", CARD, "cset",
                    "name=" + VOLUME_CONTROL, "%d,%d" % (reg, reg)],
                   capture_output=True, check=False)

    dsp_volume(level)
    save_volume(level)
    publish_volume(level)

    name = "volume-muted" if level <= 0 else "volume_step-%02d" % level

    # Retire the previous step before starting the next. Two of them must
    # never be active together: layer 4 lists volume_step-01 first, so a lower
    # step outranks a higher one, and volume *up* would be masked by the step
    # it just replaced - which is exactly how this failed the first time.
    if _last_volume_anim and _last_volume_anim != name:
        ring("stop %s" % _last_volume_anim)
    # A lifetime as well, so a step cannot linger on its blank loop frame and
    # block the ring if nothing replaces it.
    ring("play %s %.1f" % (name, VOLUME_SHOW_S))
    _last_volume_anim = name

    # After the level is applied, so the tone is heard at the new volume.
    volume_tone()

    log("volume level %d/%d (%s = %d)" % (level, VOLUME_STEPS,
                                          VOLUME_CONTROL, reg))


def publish_volume(level):
    """Publish the level as a percentage, atomically.

    Anything that wants to follow the device volume reads this rather than
    guessing from the mixer: the codec register is in 0.5 dB steps with a policy
    ceiling, so the percentage is the only figure that means the same thing to
    Home Assistant, Music Assistant and the ring.
    """
    try:
        os.makedirs(AUDIO_RUN_DIR, exist_ok=True)
        pct = int(round(level * 100.0 / VOLUME_STEPS))
        tmp = VOLUME_STATE + ".tmp"
        with open(tmp, "w") as handle:
            handle.write("%d" % pct)
        os.replace(tmp, VOLUME_STATE)
    except OSError as err:
        log("could not publish volume: %s" % err)


def publish_mute(muted):
    """Publish the authoritative mute state for the peripheral agent."""
    try:
        os.makedirs(AUDIO_RUN_DIR, exist_ok=True)
        tmp = MUTE_STATE + ".tmp"
        with open(tmp, "w") as f:
            f.write("1" if muted else "0")
        os.replace(tmp, MUTE_STATE)   # atomic: a reader never sees a half-write
    except OSError as err:
        log("could not publish mute state: %s" % err)


def _publish(path, text):
    """tmp then os.replace, like the others. /run is tmpfs: no fsync."""
    try:
        os.makedirs(AUDIO_RUN_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as err:
        log("could not publish %s: %s" % (path, err))


def _unpublish(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def publish_headphones(inserted):
    """Publish the jack: "1", "0", or no file at all when it is unknown.

    Called BEFORE set_output, so the indicator follows the plug even when the
    UCM switch fails.
    """
    if inserted is None:
        _unpublish(HEADPHONES_STATE)
    else:
        _publish(HEADPHONES_STATE, "1" if inserted else "0")


def unpublish_jack():
    """A stopped service knows nothing about the jack; say so by absence."""
    _unpublish(HEADPHONES_STATE)
    _unpublish(OUTPUT_STATE)


def apply(muted):
    global _mute_anim
    set_mics(not muted)
    set_led(muted)
    publish_mute(muted)
    # The mute ring holds a steady dim red on layer 0, so anything else that
    # happens while muted still shows over the top of it.
    if muted:
        _mute_anim = mute_animation()
        ring("play %s" % _mute_anim)
    else:
        # Stop the name that was actually PLAYED. The activity can be changed
        # while the mics are muted, and stopping the new name would leave the
        # old animation holding the ring with nothing left to retire it.
        ring("stop %s" % (_mute_anim or MUTE_ANIM_DEFAULT))
        _mute_anim = None
    log("microphones %s" % ("MUTED" if muted else "live"))


def set_output(headphones):
    """Switch the UCM device to follow the jack.

    The kernel's jack detection (r187) moves the DAC mux and powers the
    amplifier down on its own, but the codec routing and gains live in UCM and
    nothing was switching them. The result was the jack playing through the
    speaker's mixer settings - HPL off, so one earpiece only, and 5 dB down.
    """
    # Enabling either UCM device writes its own 127,127 PCM default. Preserve
    # the current register first so a service restart or jack transition never
    # silently turns the volume up to 100%.
    volume_reg = get_volume_reg()
    device = "Headphones" if headphones else "Speaker"
    result = subprocess.run(
        ["alsaucm", "-c", UCM_CARD, "set", "_verb", "HiFi", "set", "_enadev", device],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0:
        log("failed to select %s: %s"
            % (device, result.stderr.decode(errors="replace").strip()))
        return
    if volume_reg is not None:
        result = subprocess.run(
            ["amixer", "-c", CARD, "cset", "name=" + VOLUME_CONTROL,
             "%d,%d" % (volume_reg, volume_reg)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if result.returncode != 0:
            log("failed to restore volume after selecting %s: %s"
                % (device, result.stderr.decode(errors="replace").strip()))
    _publish(OUTPUT_STATE, device)
    log("output -> %s" % device)


def jack_switch(fd):
    """The jack state read from its input device's switch bitmap, or None."""
    try:
        buf = bytearray(8)
        fcntl.ioctl(fd, EVIOCGSW_8, buf, True)
    except OSError as err:
        log("cannot read the jack switch: %s" % err)
        return None
    return bool(buf[SW_HEADPHONE_INSERT // 8] & (1 << (SW_HEADPHONE_INSERT % 8)))


def jack_inserted():
    """Current jack state from the ALSA control, or None if unreadable."""
    try:
        result = subprocess.run(
            ["amixer", "-c", CARD, "cget", "iface=CARD,name=Headphone Jack"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if line.startswith(": values="):
            return "on" in line
    return None


def main():
    path = find_input_device(INPUT_NAME)
    if not path:
        log("input device %r not found; is KEYBOARD_MTK_PMIC built?" % INPUT_NAME)
        return 1
    log("watching %s (%s)" % (path, INPUT_NAME))

    jack_path = find_input_device(JACK_NAME)
    if jack_path:
        log("watching %s (%s)" % (jack_path, JACK_NAME))
    else:
        log("warning: %r not found; output will not follow the jack" % JACK_NAME)

    keys_path = find_input_device(KEYS_NAME)
    if keys_path:
        log("watching %s (%s)" % (keys_path, KEYS_NAME))
    else:
        log("warning: %r not found; volume keys will do nothing" % KEYS_NAME)

    if not os.path.exists(LED):
        log("warning: %s missing; LED will not follow the button" % LED)

    muted = False
    apply(muted)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))

    # Adopt whatever the jack says at startup: headphones may already be in.
    # The device is opened FIRST and its state read on that same fd, so a plug
    # between the read and the loop arrives as an event instead of being missed
    # - reading first and opening afterwards left exactly that window.
    jack_fd = None
    if jack_path:
        try:
            jack_fd = os.open(jack_path, os.O_RDONLY)
        except OSError as err:
            log("cannot open %s: %s" % (jack_path, err))
    state = jack_switch(jack_fd) if jack_fd is not None else None
    if state is None:
        state = jack_inserted()
    publish_headphones(state)
    if state is not None:
        set_output(state)
    else:
        log("warning: the jack state cannot be read; reporting it as unknown")

    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as err:
        log("cannot open %s: %s" % (path, err))
        return 1

    keys_fd = None
    if keys_path:
        try:
            keys_fd = os.open(keys_path, os.O_RDONLY)
        except OSError as err:
            log("cannot open %s: %s" % (keys_path, err))

    # Adopt whatever the mixer is already set to rather than forcing a level,
    # so starting this service is not itself a volume change.
    # Restore the persisted level; the mixer can no longer answer this. Falling
    # back to the old register inverse keeps a device upgrading from a
    # codec-attenuated build from jumping to full volume on its first boot.
    level = load_volume()
    reg = get_volume_reg()
    if level is None:
        # A register at unity carries NO information about the level, because
        # this build pins it there: reg_to_level() would return maximum every
        # time. Only believe the register when it is below unity, which means
        # the device is upgrading from a build that still attenuated here.
        # Anything else starts at half - never at full volume, which is what
        # this did when a missing state file met a pinned register.
        if reg is not None and reg < VOLUME_MAX_REG:
            level = reg_to_level(reg)
        else:
            level = VOLUME_STEPS // 2
    # Publish immediately: a follower that starts before the first volume
    # change would otherwise have nothing to read and have to guess.
    # Pin the codec to unity now, without the ring animation or the volume
    # tone that set_volume() would bring. A device upgrading from a build that
    # attenuated here would otherwise keep that attenuation until the first
    # volume change while the DSP also applied the new law - attenuated twice,
    # and very quiet.
    subprocess.run(["amixer", "-c", CARD, "cset",
                    "name=" + VOLUME_CONTROL,
                    "%d,%d" % (VOLUME_MAX_REG, VOLUME_MAX_REG)],
                   capture_output=True, check=False)
    publish_volume(level)
    # And tell the DSP, which is the whole point of adopting the level rather
    # than forcing one. biscuit-dsp learns the volume ONLY from this FIFO, and
    # applies stock's per-volume loudness push from it - +14.55 dB at maximum.
    # Without this it sits at push 0 from boot until the first volume change,
    # so a device nobody has touched plays about 15 dB quieter than stock at the
    # same indicated volume. That was the bug; do not drop this call.
    dsp_volume(level)
    log("volume starts at level %d/%d (%s = %s)"
        % (level, VOLUME_STEPS, VOLUME_CONTROL, reg))

    watched = [fd]
    if jack_fd is not None:
        watched.append(jack_fd)
    if keys_fd is not None:
        watched.append(keys_fd)

    # BISCUIT: control FIFO, so Home Assistant (via the peripheral agent) can
    # request mute without ever touching the hardware itself. This service
    # stays the single owner of the ADC switches and the LED; two processes
    # both interpreting KEY_MUTE would drift the moment either missed an event.
    # Opened O_RDWR deliberately: a FIFO with no writer reports EOF, and
    # select() would then spin on it forever.
    ctl_fd = None
    try:
        os.makedirs(AUDIO_RUN_DIR, exist_ok=True)
        if not os.path.exists(MUTE_CONTROL):
            os.mkfifo(MUTE_CONTROL, 0o620)
        # mkfifo's mode is masked by umask, so 0o620 lands as 0o600 and only
        # root can write. chmod explicitly so the mode is what it claims.
        os.chmod(MUTE_CONTROL, 0o620)
        ctl_fd = os.open(MUTE_CONTROL, os.O_RDWR | os.O_NONBLOCK)
        watched.append(ctl_fd)
        log("mute control FIFO at %s" % MUTE_CONTROL)
    except OSError as err:
        log("no mute control FIFO (%s); the button still works" % err)

    held_code = None        # volume key currently held down, if any
    next_repeat = 0.0

    def volume_step(code):
        """One step in the direction of `code`, clamped but always shown."""
        nonlocal level
        step = 1 if code == KEY_VOLUMEUP else -1
        # Clamped, but still shown. Pressing up at maximum has to light the
        # full ring rather than doing nothing visible - otherwise "already
        # loudest" is indistinguishable from "the button is broken".
        level = max(0, min(VOLUME_STEPS, level + step))
        set_volume(level)

    while True:
        # While muted, wake periodically to check nothing has quietly turned
        # the microphones back on. The UCM Mic device no longer sets these
        # switches for exactly that reason, but anything else that writes them
        # - a stray alsactl restore, an older UCM file - would otherwise leave
        # the array live with the LED still red. While unmuted there is nothing
        # to defend against, so block indefinitely and cost nothing.
        waits = []
        if muted:
            waits.append(RECHECK_SECONDS)
        if held_code is not None:
            waits.append(max(0.0, next_repeat - time.monotonic()))
        timeout = min(waits) if waits else None

        try:
            ready, _, _ = select.select(watched, [], [], timeout)
        except (OSError, InterruptedError):
            continue

        if not ready and muted and mics_are_on():
            log("microphones were turned back on while muted; re-muting")
            set_mics(False)

        for ready_fd in ready:
            if ctl_fd is not None and ready_fd == ctl_fd:
                # Text commands, not 24-byte input events.
                try:
                    raw = os.read(ctl_fd, 256).decode("utf-8", "replace")
                except (OSError, InterruptedError):
                    continue
                # Split on LINES, not whitespace: `volume 50` is one command
                # of two words, and splitting on spaces would see two unknown
                # ones.
                for line in raw.splitlines():
                    line = line.strip().lower()
                    if not line:
                        continue
                    if line == "refresh-led":
                        # The manual override can light the lamp while unmuted,
                        # so re-applying must not depend on the mute state.
                        set_led(muted)
                        continue
                    if line.startswith("volume"):
                        parts = line.split()
                        try:
                            pct = max(0, min(100, int(parts[1])))
                        except (IndexError, ValueError):
                            log("control: bad volume command %r" % line)
                            continue
                        want_level = int(round(pct * VOLUME_STEPS / 100.0))
                        if want_level != level:
                            level = want_level
                            set_volume(level)
                        else:
                            # Already there. Re-publish so a caller that lost
                            # track still sees the truth, exactly as mute does.
                            publish_volume(level)
                        continue
                    want = {"mute": True, "unmute": False,
                            "toggle": not muted}.get(line)
                    if want is None:
                        log("mute control: unknown command %r" % line)
                        continue
                    if want != muted:
                        muted = want
                        apply(muted)
                    else:
                        # Already in that state. Re-publish anyway so a
                        # requester that lost track still sees the truth.
                        publish_mute(muted)
                continue

            try:
                data = os.read(ready_fd, EVENT_SIZE * 64)
            except (OSError, InterruptedError):
                continue
            for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, etype, code, value = struct.unpack(
                    EVENT_FMT, data[offset:offset + EVENT_SIZE])

                if (ready_fd == fd and etype == EV_KEY
                        and code == KEY_MUTE and value == PRESS):
                    dsp_wake()
                    muted = not muted
                    apply(muted)

                elif (ready_fd == jack_fd and etype == EV_SW
                        and code == SW_HEADPHONE_INSERT):
                    publish_headphones(bool(value))
                    set_output(bool(value))

                elif (ready_fd == jack_fd and etype == EV_SYN
                        and code == SYN_DROPPED):
                    # The kernel's queue overflowed and a transition may be
                    # gone with it; the switch bitmap still has the truth.
                    state = jack_switch(jack_fd)
                    if state is not None:
                        publish_headphones(state)
                        set_output(state)

                # Autorepeat counts, so holding a key ramps rather than
                # needing thirty presses.
                elif (ready_fd == keys_fd and etype == EV_KEY
                        and code in (KEY_VOLUMEUP, KEY_VOLUMEDOWN)):
                    if value in (PRESS, REPEAT):
                        # Before the level changes, so the codec is on its way
                        # up while the tone is still being resolved and spawned.
                        dsp_wake()
                        volume_step(code)
                        # Hold this key down and start the repeat clock. REPEAT
                        # is handled too in case autorepeat is ever enabled in
                        # the device tree, in which case this just re-arms.
                        held_code = code
                        next_repeat = time.monotonic() + REPEAT_DELAY_S
                    elif value == RELEASE and held_code == code:
                        held_code = None

        # Fire the repeat only after the pending events have been read, so a
        # release that lands on the same wake-up cancels the hold instead of
        # racing it and getting one step too many.
        if held_code is not None and time.monotonic() >= next_repeat:
            volume_step(held_code)
            next_repeat = time.monotonic() + REPEAT_INTERVAL_S


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # SIGTERM arrives as SystemExit from the handler above, so this runs on
        # a clean stop as well as on any other way out.
        unpublish_jack()
