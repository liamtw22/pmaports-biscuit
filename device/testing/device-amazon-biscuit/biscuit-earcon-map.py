#!/usr/bin/env python3
"""The activity -> earcon table, and how to resolve one.

THIS FILE IS CORE. It lives in device-amazon-biscuit, not in the voice-assistant
bundle, because the sounds it names belong to the DEVICE: boot, Wi-Fi, setup,
Bluetooth and volume all play earcons on a base image with no assistant
installed. The table started out inside biscuit-va-leds.py, which meant
biscuit-earcon had to import the agent to resolve a sound - and the agent ships
with linux-voice-assistant, so a device without the assistant lost its boot
chime and every other system sound. Hence this module.

The agent imports this too, so there is still exactly ONE table.

Overrides live in SOUND_MAP_FILE and REPLACE per activity, the same rule
load_map() uses for the LED animations. `owner` is carried across an override
because it is a property of the ACTIVITY, not of the sound chosen for it -
losing it would either silence the activity or have two processes play it.
"""
import glob
import json
import os

# WHERE SOUNDS COME FROM, in order.
#
# pmOS ships NO earcons. Stock's are Amazon recordings that an owner extracts
# from their own device and that must not be redistributed, so core carries the
# table and the player and no audio at all.
#
#   /opt/persist/earcon   what the owner extracted themselves. First, because
#                         going to that trouble is a clear statement of intent,
#                         and it survives a reflash with the rest of persist.
#   EARCON_DIR            a stock pack, if one is ever installed separately.
#   the assistant's own   linux-voice-assistant ships eleven sounds of its own
#                         (wake_word_triggered, timer_finished, mute_switch_*,
#                         processing, the button presses). They are upstream
#                         assets and redistributable, so they are the DEFAULT
#                         on any device with the voice assistant installed.
#
# With none of those present nothing resolves, every lookup returns None, and
# the device is silent - which is the correct behaviour, not a failure.
EARCON_DIR = "/usr/share/biscuit/earcon"
OWNER_EARCON_DIR = "/opt/persist/earcon"
_LVA_SOUNDS_GLOB = "/usr/lib/linux-voice-assistant/lib/python3.*/site-packages/sounds"


def earcon_dirs():
    """Every directory a bare sound name is looked up in, in priority order.

    Resolved on each call rather than at import: the assistant can be installed
    or removed after this module is first imported by a long-running service,
    and /opt/persist is a separate mount that may not be up yet at boot.
    """
    dirs = [OWNER_EARCON_DIR, EARCON_DIR]
    dirs.extend(sorted(glob.glob(_LVA_SOUNDS_GLOB)))
    return [d for d in dirs if os.path.isdir(d)]


SOUND_MAP_FILE = "/opt/persist/sound-map.json"

# owner:
#   lva      the assistant plays it, from --<name>-sound flags resolved by
#            biscuit-earcon-args. Read once at launch, so a change restarts it.
#   agent    biscuit-va-leds plays it, for activities the assistant has no
#            sound of its own for.
#   service  a shell service plays it via `biscuit-earcon <activity>`.
#
# A LIST of sounds means "the first of these that is installed". The stock name
# comes first so an owner who extracted their own sounds gets exactly what the
# device used to make; the assistant's own sound is the fallback, and is what a
# device with linux-voice-assistant and no extracted stock actually plays. Five
# activities have an assistant equivalent; the rest are silent without stock,
# which is the intended outcome rather than a gap to fill with something that
# sounds nothing like the device.
DEFAULT_SOUNDS = {
    "wake_word_detected": {"sound": ["ui_wakesound", "wake_word_triggered"],
                                                                   "owner": "lva"},
    "start_listening":    {"sound": ["ui_wakesound_touch", "start_listening_button"],
                                                                   "owner": "lva"},
    # Deliberately silent, as stock was. linux-voice-assistant ships
    # processing.wav and it can be chosen on the settings page, but a sound on
    # every single query is noise rather than feedback.
    "thinking":           {"sound": None,                          "owner": "lva"},
    "timer_ringing":      {"sound": ["system_alerts_melodic_01", "timer_finished"],
                                                                   "owner": "lva"},
    "mute":               {"sound": ["state_privacy_mode_on", "mute_switch_on"],
                                                                   "owner": "lva"},
    "unmute":             {"sound": ["state_privacy_mode_off", "mute_switch_off"],
                                                                   "owner": "lva"},

    "stt_text":           {"sound": "ui_endpointing",              "owner": "agent"},
    "pipeline_error":     {"sound": "ui_error_generic_1",          "owner": "agent"},

    "boot":               {"sound": "state_boot_up_regular",       "owner": "service"},
    "wifi_error":         {"sound": "ui_error_generic_2",          "owner": "service"},
    "setup_mode":         {"sound": "state_setup_mode_on",         "owner": "service"},
    "setup_success":      {"sound": "state_setup_success",         "owner": "service"},
    "setup_error":        {"sound": "ui_error_generic_3",          "owner": "service"},
    "bt_pairing":         {"sound": "state_remote_pairing_start",  "owner": "service"},
    "bt_connected":       {"sound": "state_bluetooth_connected",   "owner": "service"},
    "bt_disconnected":    {"sound": "state_bluetooth_disconnected","owner": "service"},
    "volume_changed":     {"sound": "state_volume_adjust_tone",    "owner": "service"},
}


# WHAT THE ASSISTANT IS TOLD TO PLAY.
#
# linux-voice-assistant takes each of its sounds as a --<name>-sound FILE PATH,
# and a flag that is left out means its own bundled sound. So "no sound" has
# two different meanings for these six activities, and they used to be stored
# the same way ({"sound": null}):
#
#   silent            Off, or Silent picked for one activity. The assistant
#                     has to be handed a file of silence - leaving the flag out
#                     made it play its bundled sound instead, which is exactly
#                     what "Off" did.
#   the assistant's   the "Assistant defaults" set: leave the flag out.
#
# They are told apart by the set that is in force, and by the `silent` mark the
# settings page stores with an explicit Silent choice (so that choosing Silent
# for one activity also works while the set is "Assistant defaults").
EARCON_SET_FILE = "/opt/persist/earcon-set"
SILENCE_FILE = "/usr/share/biscuit/silence.wav"
ASSISTANT_SET = "Assistant defaults"

# activity -> the assistant's flag, and the name of its own bundled sound
LVA_FLAGS = {
    "wake_word_detected": ("--wakeup-sound", "wake_word_triggered"),
    "start_listening":    ("--start-listening-sound", "start_listening_button"),
    "thinking":           ("--processing-sound", "processing"),
    "timer_ringing":      ("--timer-finished-sound", "timer_finished"),
    "mute":               ("--mute-sound", "mute_switch_on"),
    "unmute":             ("--unmute-sound", "mute_switch_off"),
}


def earcon_set():
    """The sound set last chosen, as written by the agent; "" when never set."""
    try:
        with open(EARCON_SET_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def assistant_sound(activity, spec, set_label=None):
    """What the assistant should play for one of its activities.

    Returns (path, why): path is the file to pass, or None to leave the flag
    out so the assistant plays its own sound. `why` is "file", "silent",
    "assistant" (its own, on purpose) or "missing" (a named sound that is not
    installed, which also falls back to the assistant's own).
    """
    name = (spec or {}).get("sound")
    if name:
        path = sound_path(name)
        return (path, "file") if path else (None, "missing")
    if set_label is None:
        set_label = earcon_set()
    if not (spec or {}).get("silent") and set_label.lower() == ASSISTANT_SET.lower():
        return None, "assistant"
    if os.path.exists(SILENCE_FILE):
        return SILENCE_FILE, "silent"
    return None, "missing"


def assistant_flags(sounds=None):
    """[(flag, path), ...] for linux-voice-assistant, in a fixed order.

    The one place the flags are worked out: biscuit-earcon-args prints them at
    launch, and the settings page compares them before and after a change to
    know whether the assistant has to be restarted to hear it.
    """
    if sounds is None:
        sounds = load_sound_map()
    set_label = earcon_set()
    out = []
    for activity, (flag, _own) in LVA_FLAGS.items():
        spec = sounds.get(activity) or {}
        if spec.get("owner") != "lva":
            continue
        path, _why = assistant_sound(activity, spec, set_label)
        if path:
            out.append((flag, path))
    return out


def sound_path(name):
    """Absolute path for a sound name, or None if none of them are installed.

    An absolute path is taken as-is so a user can point an activity at their own
    file. A bare name is looked up across earcon_dirs(), FLAC first.

    `name` may also be a LIST of candidates, which is how the defaults express
    "stock's sound if the owner has it, otherwise the assistant's". The first
    candidate that resolves anywhere wins - candidate order beats directory
    order, so a stock name is never shadowed by a same-named assistant sound.
    """
    if not name:
        return None
    if isinstance(name, (list, tuple)):
        for candidate in name:
            found = sound_path(candidate)
            if found:
                return found
        return None
    if os.path.isabs(name):
        return name if os.path.exists(name) else None
    for directory in earcon_dirs():
        for ext in (".flac", ".wav", ".ogg", ".mp3"):
            cand = os.path.join(directory, name + ext)
            if os.path.exists(cand):
                return cand
    return None


def load_sound_map(logger=None):
    """DEFAULT_SOUNDS with per-activity user overrides REPLACING them."""
    m = {k: dict(v) for k, v in DEFAULT_SOUNDS.items()}
    try:
        with open(SOUND_MAP_FILE) as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("expected a JSON object of activities")
        for k, v in user.items():
            if not isinstance(v, dict):
                continue
            spec = dict(v)
            owner = DEFAULT_SOUNDS.get(k, {}).get("owner")
            if owner and "owner" not in spec:
                spec["owner"] = owner
            m[k] = spec
        if logger:
            logger.info("loaded %d sound overrides from %s", len(user), SOUND_MAP_FILE)
    except FileNotFoundError:
        pass
    except Exception as err:  # noqa: BLE001 - a bad file must not silence the device
        if logger:
            logger.warning("ignoring %s: %s", SOUND_MAP_FILE, err)
    return m
