#!/usr/bin/env python3
"""Biscuit's peripheral agent: light ring + mute LED, driven by the assistant.

linux-voice-assistant exposes a root-only websocket
(/run/biscuit-peripheral/control.sock) for "peripheral clients", meaning LEDs
and buttons. It broadcasts {"event": ..., "data": ...} on every pipeline
transition, sends a snapshot on connect, and - importantly - will MATERIALISE
HOME ASSISTANT ENTITIES on a peripheral's behalf when sent `register_light`,
`register_number` and the rest. So the ring and the device's settings become
real HA entities; the protocol is documented in lva-src/peripheral_api.py.

WHAT THIS REGISTERS, AND HOW HA STAYS RIGHT
-------------------------------------------
  build_registrations() lists every entity: the Light ring and its two accent
  colours, and one Select, Number or Switch per device setting that also has a
  row on the settings page, named as that row is. It is built afresh on every
  connect, from the files as they are then, and followed by registrations_done
  - LVA keeps Home Assistant out until that arrives, because HA deletes any
  entity it enumerates without.

  After that, watch_settings() stats the files behind those entities every
  second and pushes any change as entity_state, whoever made it: the settings
  page, a button, the pairing window closing itself. A command from HA is
  answered the same way with what was really stored, so a refusal reverts in
  HA and a side effect shows.

TWO SOURCES WANT THE RING, AND THIS IS THE PRECEDENCE
-----------------------------------------------------
  activity  the assistant's pipeline states play their activity animations -
            built-in effects by default, or Fire OS animations the owner imported
            and chose - for wake / listen / think
  HA        whatever the user set on the light entity is the IDLE appearance

An activity animation temporarily takes the ring; when the pipeline returns to
idle the HA state is restored. That way setting the ring to a colour in HA does
not stop the assistant showing what it is doing, and the assistant does not
permanently clobber the user's choice.

BRIGHTNESS COMPOSES, IT DOES NOT OVERRIDE
-----------------------------------------
biscuit-als already owns a 0-255 ceiling in /run/biscuit-als/brightness which
biscuit-ring applies to every channel. HA brightness scales the generated colour
*within* that ceiling rather than replacing it - so a dark room still dims the
ring, and the two control loops do not fight. This is a deliberate choice; the
alternative (overriding the ambient loop) would be invisible until it was
maddening.

FIFO SAFETY
-----------
/run/biscuit-ring/control is a FIFO: opening it for writing blocks until a reader
exists. Every write is O_NONBLOCK and tolerates ENXIO (ring service down) and
EPIPE (reader vanished). A cosmetic service must never wedge anything.
"""

import biscuit_ring_config as ring_config
import biscuit_call_profile as call_profile
import biscuit_eq as eq_store
import asyncio
import errno
import importlib.util
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
import biscuit_services as services
import sys

CONTROL_FIFO = "/run/biscuit-ring/control"
PERIPHERAL_TRANSPORT = "unix-root"
FX_MODULE = "/usr/bin/biscuit-ring-fx.py"
RING_BRIGHTNESS = "/run/biscuit-ring/brightness"

# WHO OWNS THE RING'S BRIGHTNESS - settled with an explicit switch.
#
#   Auto dim ON   the ambient light sensor sets the ceiling. Stock behaviour,
#                 and the default. The brightness slider is ignored, and
#                 RING_BRIGHTNESS is removed so biscuit-ring falls back to the
#                 ambient value.
#   Auto dim OFF  the Home Assistant slider sets the ceiling, written to
#                 RING_BRIGHTNESS.
#
# Before this there was no owner: the ambient ceiling and the slider MULTIPLIED,
# so in a dim room the whole slider compressed into 0-14 of 255 and its bottom
# fifth emitted nothing while still reporting the light on.
AUTODIM_FILE = "/opt/persist/autodim"

# The mute indicator has two binary channels, so it has exactly three states.
# It is offered as three named modes rather than a 0-255 slider, which would
# have been a control with 253 positions that do nothing. biscuit-audio owns the
# LED and reads this file when it lights it; writing the channels from here as
# well would be a second owner, and the mute state already has one.
# Two controls. "Mic Light" is a MANUAL override that lights the lamp whether or
# not the microphones are muted, so Off means "no override" rather than "lamp
# off". "Mute Light" is what the indicator does when muted, and its Auto follows
# the same ambient ceiling that dims the ring.
MIC_LED_FILE = "/opt/persist/mic-led"
MIC_LED_MODES = ["Off", "Low", "High"]
MUTE_LED_FILE = "/opt/persist/mute-led"
MUTE_LED_MODES = ["High", "Low", "Auto", "Off"]
LED_MAP_FILE = "/opt/persist/led-map.json"
DUCK_FILE = "/opt/persist/duck.json"
MIC_ENV = "/opt/persist/mic.env"

# The ambient light sensor.
#
# READ biscuit-als's OUTPUT, NOT THE DRIVER'S in_illuminance_input.
#
# The part is a 2584TSV but the mainline driver binds as `tsl2583` and applies
# the 2583's coefficient table, which is for a different device. Its raw
# channels are right and its computed lux is not: in a room reading a genuine
# ~45 lux, in_illuminance_input reports 0. biscuit-als reads the raw channels
# and applies stock's own equation with the per-unit factory coefficient from
# IDME, and publishes the result here.
#
# So this file is the calibrated answer and the sysfs one is a trap. If
# biscuit-als is not running there is no calibrated value to publish, and
# reporting the driver's figure instead would put a confident 0 lx in Home
# Assistant that is indistinguishable from a dark room.
BUTTONS_FILE = "/opt/persist/buttons.json"

# THE ACTION BUTTON
#
# The Echo's action button is KEY_ASSISTANT (583) on the `mt6779-keypad` input
# device, and nothing on this port has ever used it. The other three buttons are
# owned by biscuit-audio - mute on the PMIC device, volume on gpio-keys - so this
# is the only one free to carry gestures, which is also what it is for.
#
# Found by NAME, never by event number: adding input devices renumbers them, and
# gpio-keys already moved from event1 to event3 when r188 added the keypad.
BUTTON_DEVICE = "mt6779-keypad"
KEY_ASSISTANT = 583

# Gesture timing. LONG_PRESS_S fires while the button is still held, because a
# hold that only resolves on release feels broken - the user is waiting for
# feedback that the device has understood. MULTI_PRESS_S is the window for the
# next press to count as part of the same gesture; longer feels laggy on a
# single press, shorter makes a triple press hard to land.
LONG_PRESS_S = 0.7
MULTI_PRESS_S = 0.45

# Deadlines are compared with a tolerance because they are reached by ADDING
# floats: waking at last_release + 0.45 and testing `now - last_release >= 0.45`
# fails when that arithmetic lands on 0.44999999999999996, which it does for
# most values. Without this a gesture silently never resolves and the watcher
# spins - waking at the deadline, finding nothing to do, re-arming for ~0 s.
GESTURE_EPS = 0.002

# What a gesture can be bound to. The value is what this agent does LOCALLY;
# every gesture also reports itself to Home Assistant regardless, so an
# automation can react to any of them even when the local action is "Nothing".
# Only commands the peripheral API actually implements. There is deliberately
# no "next track" or "previous track": the API has no such command, and an
# option that silently does nothing is worse than not offering it. Track
# skipping belongs in a Home Assistant automation on the button event, which
# fires for every gesture regardless of the local action.
BUTTON_ACTIONS = {
    "Nothing":                  None,
    "Start listening":          "start_listening",
    "Stop":                     "stop_pipeline",
    "Toggle microphone mute":   "toggle_mute",
    "Pause media":              "pause_media_player",
    "Resume media":             "resume_media_player",
    "Stop media":               "stop_media_player",
    "Volume up":                "volume_up",
    "Volume down":              "volume_down",
    "Enter setup mode":         "setup_mode",
    "Bluetooth pairing":        "bt_pairing",
}
BUTTON_ACTION_LABELS = list(BUTTON_ACTIONS)

BUTTON_GESTURES = [
    ("single", "Single press", "Start listening"),
    ("double", "Double press", "Pause media"),
    ("triple", "Triple press", "Nothing"),
    ("long",   "Press and hold", "Bluetooth pairing"),
]
BUTTON_DEFAULTS = {g: default for g, _label, default in BUTTON_GESTURES}

ALS_LUX = "/run/biscuit-als/lux"
ALS_POLL_S = 15.0

# Report only meaningful movement. The value drifts in the third decimal every
# read, and a sensor that republishes noise writes a recorder row each time.
ALS_MIN_DELTA = 1.0     # lux
ALS_MIN_RATIO = 0.10    # or 10%, whichever is larger

# The settings page offers these presets.  They are translated into the same
# composed settings used by Home Assistant; it must never write MIC_MODE on its
# own, because that used to discard the rest of mic.env.
MIC_MODES = [
    ("Stock chain (default)",      ""),
    ("Stock chain, no AEC",        "-stock-noaec"),
    ("Stock chain, no adaptive",   "-stock-noadaptive"),
    ("Centre mic (stock gain)",    "-stock-centre"),
    ("Centre mic",                 "-centre"),
    ("Our beamformer",             "-beam"),
]
DEFAULT_MIC_LABEL = MIC_MODES[0][0]
CUSTOM_MIC_LABEL = "Custom (Home Assistant)"
MIC_LABELS = [CUSTOM_MIC_LABEL] + [label for label, _flag in MIC_MODES]
MIC_BY_LABEL = dict(MIC_MODES)
MIC_BY_FLAG = {flag: label for label, flag in MIC_MODES}

# WHY THIS AGENT DUCKS AND THE ASSISTANT DOES NOT DO IT ALL
#
# linux-voice-assistant already ducks - but only its OWN music player, at a
# factor hardcoded to 0.5 (mpv_player.py: `def duck(self, factor=0.5)`), and it
# has no idea the other audio sources exist. On this device the primary media
# path is sendspin feeding Music Assistant, a separate process, and A2DP is a
# third. Neither is ducked by anything today.
#
# So this ducks at the PipeWire layer, where every source is visible as a
# sink-input, and deliberately SKIPS the assistant's own streams: it already
# handles its music, and ducking its text-to-speech would make the reply quiet -
# the exact opposite of the point. They are identified by owning PID rather than
# by name, because the assistant's music and TTS players are two mpv instances
# in one process and both report the same application.name.
DUCK_DEFAULTS = {"enabled": True, "level": 40}   # percent of each stream's own volume
# biscuit-audio is the SINGLE OWNER of the microphone mute: it drives the ADC
# switches and the red LED, and publishes the authoritative state. This agent
# only relays - it never mutes the hardware itself, because two owners drift
# the moment either misses an event.
AUDIO_MUTE_STATE = "/run/biscuit-audio/muted"
# The device volume, owned by biscuit-audio exactly as the mute state is. It
# writes the codec, tells biscuit-dsp so the per-volume loudness compensation
# follows, and shows the step on the ring - so anything that attenuates
# elsewhere is a second volume none of that knows about.
AUDIO_VOLUME_STATE = "/run/biscuit-audio/volume"
AUDIO_VOLUME_POLL_S = 1.0
AUDIO_MUTE_CONTROL = "/run/biscuit-audio/control"
MUTE_POLL_S = 0.4
# Whether headphones are in the jack, "1" or "0", published by biscuit-audio.
# Absent means unknown - an older biscuit-audio, or the service stopped.
AUDIO_HEADPHONES_STATE = "/run/biscuit-audio/headphones"

# The Home Assistant Light Ring's own state: on/off, colour, temperature and
# effect. Brightness is not here - it is the ring's brightness file, shared with
# the settings page. Persisted so that a restart of this agent does not turn the
# ring dark while HA shows it on, and so it can be told to LVA on connect.
HA_LIGHT_FILE = "/opt/persist/ha-light.json"

# How often the Home Assistant entities are checked against the files behind
# them, and how soon a lost connection to the assistant is retried. The retry
# is short because the assistant holds Home Assistant off until this agent has
# registered (see run()), so every second here is a second of a booting device
# being invisible to HA.
SETTINGS_POLL_S = 1.0
PERIPHERAL_RETRY_S = 1.0

_LOGGER = logging.getLogger("biscuit-va-leds")


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


def _load_fx():
    spec = importlib.util.spec_from_file_location("biscuit_ring_fx", FX_MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fx = _load_fx()

# Preserve the original Fire OS 5 activity choices for existing overrides and
# the settings selector. Fire OS 6 is the default below.
FIREOS5_MAP = {
    "wake_word_detected": {"animation": "alexa_wake-up", "lifetime": 1},
    "listening":          {"animation": "alexa_active"},
    "stt_text":           {"animation": "alexa_user-talking"},
    "thinking":           {"animation": "alexa_thinking"},
    "tts_speaking":       {"animation": "active-talking"},
    "tts_finished":       {"animation": "alexa_back-to-ready", "lifetime": 1},
    "pipeline_error":     {"animation": "error", "lifetime": 2},
    "timer_ticking":      {"animation": "active_timer"},
    "timer_ringing":      {"animation": "active_alarm"},

    # Not pipeline events. These are played by other services - the boot script,
    # wifi.start, biscuit-setup.sh, biscuit-pair-session, biscuit-btring - which
    # are shell or separate processes and cannot read this map directly. They are
    # listed HERE anyway so there is exactly ONE table of what the device's stock
    # behaviour is; biscuit-settings resolves them out to a shell-sourceable file
    # for those consumers. `mute` is the exception: this agent plays it itself.
    "mute":               {"animation": "_mic-mute"},
    "boot":               {"animation": "boot_success", "lifetime": 3},
    "wifi_connecting":    {"animation": "wifi-config"},
    "wifi_error":         {"animation": "anim_start_error_short", "lifetime": 4},
    "setup_mode":         {"animation": "setup-mode"},
    "setup_success":      {"animation": "anim_start_success", "lifetime": 4},
    "setup_error":        {"animation": "anim_OOBE_start_error", "lifetime": 4},
    "bt_pairing":         {"animation": "btpair-setup"},
    # Stock's two are one-shot (46 frames, no loop) and retire themselves, so
    # stock needs no lifetime. Generated effects LOOP, so a user picking one here
    # would leave it spinning forever. The lifetime is a ceiling that stock never
    # reaches and a custom effect does.
    "bt_connected":       {"animation": "btconnect", "lifetime": 3},
    "bt_disconnected":    {"animation": "btdiscconnect", "lifetime": 3},
}


# Namespaced assets preserve both generations without overwriting legacy files.
# uxconfig.json maps listening to ca-active-start, thinking to active-thinking,
# and speech-end to ca-active-end. Other pmOS activities use their corresponding
# Fire OS 6 resource; not every stock event has a pmOS equivalent.
#
# THIS IS NO LONGER THE DEFAULT. Both stock generations are owner-imported
# Amazon artwork that pmOS must not redistribute, so the shipped defaults below
# are generated effects instead. These two tables stay because the settings page
# offers whichever of them the owner has actually installed - fos_shortcuts()
# filters both against the resource directory - and an existing override naming
# a stock animation has to keep resolving.
FIREOS6_MAP = {key: dict(spec, animation="fos6_" + key)
               for key, spec in FIREOS5_MAP.items()}

# Where stock animations are: shipped with the image (none are, now), or
# imported by the owner from their backup on the settings page.
STOCK_DIRS = ("/usr/share/biscuit-ring/led-resources", "/opt/persist/biscuit/led")
# Fire OS 6's own animation for each activity, by Amazon's file name - from
# its uxconfig.json and resource set. A fos6_<activity> choice plays this file;
# only four differ from the Fire OS 5 table above.
FOS6_SOURCES = dict({k: v["animation"] for k, v in FIREOS5_MAP.items()},
                    wake_word_detected="ca-active-start", listening="ca-active-start",
                    thinking="active-thinking", tts_finished="ca-active-end")


# Two names from an older table: they were Amazon's pointer frames, and are
# the live pointer effects now - as the settings page stores them since.
LEGACY_POINT = {"alexa_point-at-user": "Point at speaker", "alexa_point-at-noise": "Point at noise"}


def normalise_spec(spec):
    """An override as it should be played: the two legacy pointer names become
    the effects they stand for, so they keep following the talker whether or
    not Amazon's files of the same name were imported."""
    if isinstance(spec, dict) and spec.get("animation") in LEGACY_POINT and not spec.get("effect"):
        out = dict(spec)
        out.pop("animation")
        out.update(effect=LEGACY_POINT[spec["animation"]], colours=[[0, 255, 255], [0, 0, 255]])
        return out
    return spec


def stock_source(name):
    """The file a stock animation name plays, or None when it is not here.
    fos6_<activity> names are Fire OS 6's own file for that activity."""
    if not isinstance(name, str) or not name or "/" in name or name.startswith("."):
        return None
    if name.startswith("fos6_"):
        name = FOS6_SOURCES.get(name[len("fos6_"):], "")
        if not name:
            return None
    for directory in STOCK_DIRS:
        path = os.path.join(directory, name + ".animation")
        if os.path.isfile(path):
            return path
    return None
FIREOS6_MAP["booting"] = {"animation": "fos6_booting"}
FIREOS6_MAP["music"] = {"animation": "fos6_music"}


# ---------------------------------------------------------------------------
# Stock animations, as a person sees them
# ---------------------------------------------------------------------------
#
# Amazon names its files for product states and namespaces, not for what they
# look like: "zzz_lava-flow", "anim_OOBE_start_error", "btdiscconnect". Every
# page that shows one - the Light ring picker, Files from stock, "Showing now" -
# asks here, so a name is formatted once and the same file reads the same
# everywhere. The file name stays the stored value; only its label changes.

# The sections of a picker, in order. Ambient first: those are the ones worth
# choosing for their looks. "Listening steps" are the 40 single frames stock
# draws a listening arc from, which nobody would pick for an activity.
STOCK_GROUPS = ("Ambient", "Assistant", "Alarms, timers and reminders", "Microphones",
                "Messages and notifications", "Calls", "Bluetooth", "Volume",
                "Setup and system", "Other", "Listening steps")

# By name prefix, first match wins - so Calls is tested before Bluetooth
# ("btcall-on") and Microphones ("micsoff_btcall-ringing").
_STOCK_GROUP_RULES = (
    ("Listening steps", ("ca-active-start_step",)),
    ("Ambient", ("zzz_", "chase", "comet", "mirror", "3trace", "scan_", "nightday", "solid_",
                 "blue_fadein", "wait", "test", "beam_rotate")),
    ("Calls", ("call_", "phone", "_phone", "btcall", "micsoff_btcall", "voicemail")),
    ("Bluetooth", ("bt",)),
    ("Microphones", ("_mic-mute", "mics-off", "micsoff")),
    ("Volume", ("volume",)),
    # Thinking, not an alarm: it would otherwise fall to the "ready" rule below.
    ("Assistant", ("ready_thinking",)),
    ("Alarms, timers and reminders", ("ready", "active_alarm", "active_timer", "_reminder",
                                      "timer_")),
    ("Messages and notifications", ("sms_", "notice", "do_not_disturb")),
    ("Assistant", ("alexa_", "active", "ca-", "dialog-", "_active", "_thinking", "deep-thinking",
                   "ntt-", "_point-and-hear", "wakeword")),
    ("Setup and system", ("anim_", "setup", "authenticated", "wifi", "scone", "ffs_", "generic",
                          "boot", "start", "_start", "ota", "factory", "fail_", "off", "aed_",
                          "liveview", "error")),
)

# Written by hand wherever the mechanical label below would be wrong, clash
# with a different file, or be Amazon jargon. Twins that differ only by a
# hyphen or an underscore are different animations and get different labels.
# Three words are guesses and are labelled neutrally rather than made up:
# "scone" (a setup flow of some kind - "Alternate setup"), "ntt" and "post"
# ("self-test" is the likeliest reading of a start-up POST).
STOCK_LABELS = {
    # Ambient
    "3traceinv": "Three traces, inverted",
    "beam_rotate": "Beam, blue on red", "beam_rotate_off": "Beam, red on blue",
    "scan_cyan": "Cyan scan", "wait": "Waiting",
    "test": "Test pattern", "test255": "Test pattern, full brightness",
    "zzz_tortoise-hare": "Tortoise and hare",
    # Assistant
    "_active": "Listening, blue and green", "_active-no-point": "Listening, no pointer",
    "_active-point": "Listening, with pointer", "alexa_active": "Listening",
    "ca-active-start": "Listening (Fire OS 6)", "ntt-listening": "Listening, alternate",
    "_thinking": "Thinking, one pass", "alexa_thinking": "Thinking",
    "active-thinking": "Thinking (Fire OS 6)", "active_thinking": "Thinking, fast",
    "deep-thinking": "Deep thinking", "ready_thinking": "Thinking, from idle",
    "active-start": "Session start", "active_start": "Session start, orange",
    "active-on": "Session active", "active_on": "Session active, orange",
    "active-end": "Session end", "active_end": "Session end, orange",
    "ca-active-end": "Session end (Fire OS 6)", "active-talking": "Speaking",
    "alexa_back-to-ready": "Finished replying", "alexa_user-talking": "You are speaking",
    "alexa_wake-up": "Wake word", "wakeword-change": "Wake word changed",
    "alexa_point-at-user": "Point at speaker", "alexa_point-at-noise": "Point at noise",
    "dialog-active": "Conversation, listening", "dialog-talk": "Conversation, speaking",
    "dialog-think": "Conversation, thinking", "dialog-end": "Conversation, ending",
    "dialog-scone": "Conversation, alternate setup",
    # Alarms, timers and reminders
    "active_alarm": "Alarm ringing", "active_timer": "Timer running",
    "ready-alarm": "Alarm", "ready_alarm": "Alarm, blinking", "ready-alarm-short": "Alarm, short",
    "ready-timer": "Timer", "ready_timer": "Timer, slow", "ready-timer-short": "Timer, short",
    "_reminder-delivery": "Reminder", "ready_reminder-delivery": "Reminder, repeating",
    "ready": "Blank, one second",
    # Microphones
    "_mic-mute": "Microphones muted",
    "mics-off_start": "Microphones turning off", "micsoff-start": "Microphones turning off, short",
    "mics-off_on": "Microphones off", "micsoff_on": "Microphones off, steady",
    "mics-off_end": "Microphones back on", "micsoff_end": "Microphones back on, short",
    "mics-off_on-sms_unheard": "Microphones off, message waiting",
    "micsoff-ready-alarm": "Microphones off, alarm", "micsoff-ready-timer": "Microphones off, timer",
    # Messages and notifications
    "notice": "Notification", "noticeoff": "Blank notification", "sms_incoming": "Message arriving",
    "sms_unheard": "Message waiting", "sms_unheard_micsoff": "Message waiting, muted",
    # Calls
    "_phone-alexa-active": "Call, assistant listening",
    "phone_alexa-from-active-call": "Call, assistant from a call",
    "_phone-alexa-back-to-sleep": "Call, assistant done",
    "phone_alexa-from-sw-mute": "Call, assistant while muted",
    "phone_alexa-unmute": "Call, assistant unmuted",
    "_phone-answer": "Call answered", "phone_answer": "Phone answered",
    "_phone-hang-up": "Call ended", "phone_hang-up": "Phone hung up",
    "_phone-on-call": "On a call", "_phone-ringing": "Phone ringing, once",
    "phone_ringing": "Phone ringing", "phone_call-waiting-ring": "Call waiting",
    "phone_hardware-mute": "Phone call muted by button", "phone_software-mute": "Phone call muted",
    "phone_software-unmute": "Phone call unmuted", "phone_switch-call": "Switching calls",
    "call_connected_start": "Call connected, starting", "call_connected_on": "Call connected, steady",
    "call_connected_end": "Call connected, ending", "call_connected_mute": "Call muted",
    "call_connected_mute_start": "Call muted, starting", "call_connected_mute_on": "Call muted, steady",
    "call_hold": "Call on hold", "call_outbound_ringing": "Calling, ringing",
    "call_outbound_dialing": "Calling, dialling", "call_incoming": "Incoming call",
    "call_incoming_call_waiting": "Incoming call, call waiting",
    "call_incoming_muted": "Incoming call, muted", "voicemail-feedback": "Voicemail",
    "btcall-start": "Bluetooth call starting", "btcall-on": "Bluetooth call",
    "btcall-end": "Bluetooth call ended", "btcall-ringing": "Bluetooth call ringing",
    "btcall-end_micsoff": "Bluetooth call ended, microphones off",
    "btcall_micsoff-start": "Bluetooth call starting, microphones off",
    "btcall_micsoff-on": "Bluetooth call, microphones off",
    "btcall_micsoff-end": "Bluetooth call ending, microphones off",
    "micsoff_btcall-ringing": "Bluetooth call ringing, microphones off",
    # Bluetooth
    "bt-double": "Bluetooth, double flash", "bt-pair": "Bluetooth pairing, simple",
    "bt-success": "Bluetooth done", "btpair-setup": "Bluetooth pairing",
    # Volume
    "volume_full-to-off": "Volume, full to off", "volume_off-to-full": "Volume, off to full",
    "volume_mute-ready": "Volume muted", "volume_mute-active": "Volume muted, while listening",
    "volume_unmute-ready": "Volume unmuted", "volume_unmute-active": "Volume unmuted, while listening",
    # Setup and system
    "OTA_alert": "Update alert", "ota-update": "Updating",
    "_start-up": "Start-up", "start-up_loading-full": "Start-up loading",
    "startup-loading-full": "Start-up loading, short", "start-up_post": "Start-up self-test",
    "boot_success": "Finished booting",
    "aed_detected": "Sound detected", "aed_enabled": "Sound detection on",
    "aed_enabled-call_incoming": "Sound detection on, incoming call",
    "aed_enabled-call_incoming-mics-off_on": "Sound detection on, incoming call, microphones off",
    "aed_enabled-mics-off_on": "Sound detection on, microphones off",
    "aed_enabled-mics-off_on-sms_unheard": "Sound detection on, microphones off, message waiting",
    "aed_enabled-sms_unheard": "Sound detection on, message waiting",
    "anim_OOBE_start_error": "Setup failed", "anim_start_error_short": "Setup error, short",
    "anim_start_error_looping": "Setup error, repeating", "anim_start_success": "Setup succeeded",
    "authenticated_setup_mode": "Setup mode, signed in", "wifi-config": "Connecting to Wi-Fi",
    "ffs_action-success": "Quick setup succeeded", "generic-success": "Success",
    "fail_health_check": "Health check failed", "liveview_active": "Live view",
    "liveview_off": "Live view off", "off": "Blank",
    "scone-setup": "Alternate setup", "scone-active": "Alternate setup, active",
    "scone-ambiguity": "Alternate setup, unsure", "scone-error": "Alternate setup failed",
    "scone-success": "Alternate setup succeeded",
}

# Numbered sequences: one rule each rather than a label each.
_STOCK_FAMILIES = (
    (r"ca-active-start_step-(\d+)", lambda m: "Listening width %d of 40" % (int(m.group(1)) + 1)),
    (r"OTA_step_(\d+)", lambda m: "Update progress %d of 12" % int(m.group(1))),
    (r"boot_(\d)", lambda m: "Booting, stage %d of 4" % (int(m.group(1)) + 1)),
    (r"anim_start_phase(\d)", lambda m: "Start-up phase %d of 3" % int(m.group(1))),
    (r"timer_led_countdown_sec-(\d+)", lambda m: "Timer countdown, %d seconds" % int(m.group(1))),
)
_STOCK_DROP = {"zzz", "alexa", "anim", "ca"}      # namespaces, not words
_STOCK_WORDS = {
    "bt": "Bluetooth", "btcall": "Bluetooth call", "btconnect": "Bluetooth connected",
    "btdiscconnect": "Bluetooth disconnected",      # Amazon's spelling
    "btpair": "Bluetooth pairing", "ota": "update", "oobe": "setup", "sms": "message",
    "aed": "sound detection", "wifi": "Wi-Fi", "micsoff": "microphones off",
    "mics": "microphones", "mic": "microphone", "liveview": "live view",
    "nightday": "night and day", "startup": "start-up", "inv": "inverted", "sw": "software",
    "3trace": "three traces", "fadein": "fade-in", "wakeword": "wake word",
    "dnd": "do not disturb", "ffs": "quick setup",
}


def _stock_base(name):
    base = os.path.basename(str(name))
    return base[:-len(".animation")] if base.endswith(".animation") else base


def stock_label(name):
    """'zzz_lava-flow' -> 'Lava flow': what a stock animation is called on
    every page. Hand-written where it matters, mechanical otherwise, so a file
    from a backup nobody has seen yet still reads as words."""
    base = _stock_base(name)
    if base in STOCK_LABELS:
        return STOCK_LABELS[base]
    for pattern, fmt in _STOCK_FAMILIES:
        m = re.fullmatch(pattern, base)
        if m:
            return fmt(m)
    words = []
    for tok in re.split(r"[-_\s]+", re.sub(r"start-?up", "startup", base, flags=re.I)):
        low = tok.lower()
        if not low or (not words and low in _STOCK_DROP):
            continue
        num = re.fullmatch(r"([a-z]+)(\d+)", low)
        if low in _STOCK_WORDS:
            words += _STOCK_WORDS[low].split()
        elif num:
            words += [num.group(1), num.group(2)]
        else:
            words.append(low)
    out = [w for i, w in enumerate(words) if i == 0 or w.lower() != words[i - 1].lower()]
    text = " ".join(out) or base
    return text[:1].upper() + text[1:]


def stock_group(name):
    """Which picker section a stock animation belongs in; see STOCK_GROUPS."""
    low = _stock_base(name).lower()
    for group, prefixes in _STOCK_GROUP_RULES:
        if low.startswith(prefixes):
            return group
    return "Other"


def _natural(text):
    """'Update progress 10' after 'Update progress 9'."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text.lower())]


def _stock_facts(path):
    """(sha256, blank) of one file. Blank means no frame lights anything - stock
    ships two ("off", "noticeoff") and they would only duplicate Off."""
    with open(path, "rb") as f:
        data = f.read()
    blank = True
    for raw in data.decode("utf-8", "replace").splitlines():
        ms, sep, rest = raw.partition(":")
        if not sep or raw.lstrip().startswith("#"):
            continue
        for tok in rest.split(","):
            tok = tok.strip()
            try:
                if tok and int(tok, 16):
                    blank = False
                    break
            except ValueError:
                continue
        if not blank:
            break
    return hashlib.sha256(data).hexdigest(), blank


_catalogue_cache = {}


def stock_catalogue():
    """Every stock animation on this device, in picker order:

        [{"name", "label", "group", "hidden", "same"}]

    `hidden` says why a file is not offered as a choice - "step" (one frame of
    stock's listening arc), "blank", or "pointer" (Amazon's pointer frames,
    which play as the live Point effects anyway) - and it is still listed on
    Files from stock. `same` names the file a byte-identical copy is offered
    as: stock ships thirteen such sets (btconnect is also btdiscconnect,
    generic-success and scone-success), and one look is offered once. The
    canonical copy is the one Fire OS itself maps to an activity, if any.
    """
    seen, entries = set(), []
    for directory in STOCK_DIRS:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for fname in names:
            if not fname.endswith(".animation") or fname.startswith("."):
                continue
            name = fname[:-len(".animation")]
            if name in seen or name.startswith(("act_", "fx_", "volume_step-", "volume-muted")):
                continue
            path = os.path.join(directory, fname)
            try:
                st = os.stat(path)
                key = (path, st.st_size, st.st_mtime_ns)
                if key not in _catalogue_cache:
                    _catalogue_cache[key] = _stock_facts(path)
                digest, blank = _catalogue_cache[key]
            except OSError:
                continue
            seen.add(name)
            group = stock_group(name)
            hidden = ("step" if group == "Listening steps" else "blank" if blank else
                      "pointer" if name in LEGACY_POINT else None)
            entries.append({"name": name, "label": stock_label(name), "group": group,
                            "hidden": hidden, "same": None, "_digest": digest})
    rank = {g: i for i, g in enumerate(STOCK_GROUPS)}
    entries.sort(key=lambda e: (rank.get(e["group"], len(rank)), _natural(e["label"]), e["name"]))
    mapped = set(FOS6_SOURCES.values()) | {v.get("animation") for v in FIREOS5_MAP.values()}
    by_digest = {}
    for e in entries:
        if not e["hidden"]:
            by_digest.setdefault(e.pop("_digest"), []).append(e)
        else:
            e.pop("_digest")
    for twins in by_digest.values():
        if len(twins) > 1:
            first = next((e for e in twins if e["name"] in mapped), twins[0])
            for e in twins:
                if e is not first:
                    e["same"] = first["name"]
    return entries


def held_activity(key):
    """An activity that is a STATE rather than a moment: it has no lifetime,
    and stays on the ring until whatever it stands for ends."""
    return isinstance(key, str) and key in DEFAULT_MAP and not DEFAULT_MAP[key].get("lifetime")


def stage_stock(src, dst, loop=False):
    """Copy a stock animation to `dst` for playing under another name.

    With `loop`, a file that plays once is made to repeat. Ten of the fifteen
    ambient files have no loop marker, so chosen for a held activity - thinking,
    music, setup mode - Fire played for a third of a second and the ring went
    dark while the device was still thinking. A marker at the top repeats the
    whole file. Both copy sites use this: the settings page's published copy and
    the agent's staged one, so the two cannot disagree about it.
    """
    with open(src, "rb") as f:
        data = f.read()
    if loop and not any(line.strip() == b"loop" for line in data.splitlines()):
        data = b"loop\n" + data
    with open(dst, "wb") as f:
        f.write(data)


# The shipped defaults: biscuit-ring-fx effects, rendered per activity into
# /opt/persist/led-fx/act_<activity>.animation by biscuit-settings. Nothing here
# depends on an asset we cannot ship, so the ring works on a stock-free device.
#
# Every entry gives COLOURS rather than a single colour, so each activity starts
# out with the same background-and-main pair the settings page offers, and a
# user changing one colour is editing the thing the default already used. The
# count must match COLOUR_ROLES for the effect - two for almost everything, one
# for Solid - or palette_frames rejects it and publish_for_shell silently falls
# back.
#
# Effects in ONESHOT (Wipe In, Drain Out, Soft Bloom) play once and retire, so
# their lifetime is a ceiling rather than the thing that ends them.
# Colours and motion below are MEASURED from the stock animations, not chosen:
# each activity's file was analysed for its peak colour, its resting colour, how
# many segments are lit, and whether the lit mass rotates (angular centroid
# drift per frame) or the whole ring pulses together. The effect picked is the
# closest thing in biscuit-ring-fx to what stock actually does.
#
#   activity            stock measurement                     -> effect
#   booting             4-seg arc, 1.0 seg/frame, cyan/blue   Arc Spin
#   bt_pairing          3-seg arc, 1.0 seg/frame, pure blue   Arc Spin
#   setup_mode          3-seg arc, orange #FF5500             Arc Spin
#   wifi_connecting     3-seg arc, slow, amber #FFAA00        Arc Spin
#   thinking            all 12 lit, 3.2 seg/frame rotation    Highlight Orbit
#   boot/setup_success  whole ring pulses 0->255              Breathe
#   wifi_error          whole ring pulses, red on dark red    Breathe
#   setup_error         whole ring pulses, PURPLE #6600CC     Breathe
#   tts_speaking        all 12 lit, constant full cyan        Solid
#   mute                settles on #990000 and holds          Solid
#   timer_ringing       two frames alternating                Blink
#
# booting and music have no stock original - stock shows nothing at all while it
# boots - so those two mirror OUR previous versions, which the files themselves
# recorded as "ours, generated": a cyan comet with a blue tail, and a slow dim
# blue-to-cyan breath.
#
# bt_connected and bt_disconnected are the SAME FILE in stock, byte for byte, so
# they get the same default here. Wipe In and Drain Out would tell them apart if
# that is ever wanted; stock simply does not.
_CY_ON_BLUE = [[0, 255, 255], [0, 0, 255]]      # cyan on blue, the voice palette
_LTBLUE     = [[0, 153, 255], [0, 17, 51]]      # #0099FF on #001133
_BLUE_DIM   = [[0, 0, 255], [0, 0, 17]]         # #0000FF on #000011

DEFAULT_MAP = {
    # The three activities where the device is listening TO SOMEONE all point
    # at them. The renderer resolves the bearing at playback time from
    # biscuit-direction's estimate, so these three follow the talker around the
    # room rather than lighting the ring uniformly. With no estimate available -
    # microphones muted, or nothing heard yet - the pointer falls back to the
    # background colour alone, which is why both colours matter here.
    "wake_word_detected": {"effect": "Point at speaker", "colours": _CY_ON_BLUE, "lifetime": 1},
    "listening":          {"effect": "Point at speaker", "colours": _CY_ON_BLUE},
    "stt_text":           {"effect": "Point at speaker", "colours": _CY_ON_BLUE},
    "thinking":           {"effect": "Highlight Orbit", "colours": _CY_ON_BLUE},
    "tts_speaking":       {"effect": "Solid",           "colours": [[0, 255, 255]]},
    "tts_finished":       {"effect": "Drain Out",       "colours": _CY_ON_BLUE, "lifetime": 1},
    "pipeline_error":     {"effect": "Breathe",         "colours": [[255, 17, 0], [0, 0, 0]], "lifetime": 2},
    "timer_ticking":      {"effect": "Breathe",         "colours": [[0, 153, 255], [0, 0, 0]]},
    "timer_ringing":      {"effect": "Blink",           "colours": _LTBLUE},

    # No lifetime: it loops until zz-boot-done retires it, because "still
    # booting" ends at a moment only that script knows.
    # Cyan arc on BLUE. The first attempt took the background from the old
    # comet's tail, #002B47 - which is R0 G43 B71, blue on paper but so
    # desaturated that on the LEDs it reads as dark cyan, so the arc and the
    # background were the same colour. Pure blue is what this wants.
    "booting":            {"effect": "Arc Spin",        "colours": _CY_ON_BLUE},
    "boot":               {"effect": "Breathe",         "colours": [[0, 255, 255], [0, 0, 0]], "lifetime": 3},
    # Music is a STATE lasting the whole track, so no lifetime. The VISUALISER
    # normally paints over this at the same priority; this is what shows when it
    # is switched off or has not produced a frame yet - deliberately dim.
    "music":              {"effect": "Breathe",         "colours": [[0, 112, 128], [0, 16, 37]]},
    # Held state, and the one that must not fade to nothing. #990000 is what
    # stock's mics-off_on settles on and holds; Solid takes a single colour and
    # loops because it is not in ONESHOT.
    "mute":               {"effect": "Solid",           "colours": [[153, 0, 0]]},
    # Colours only, in practice: the effect previews on the settings page, but
    # the live ring uses generate_volume_ramp's 31 levels in these colours.
    "volume_changed":     {"effect": "Volume Ramp",     "colours": [[255, 255, 255], [0, 0, 0]]},

    "wifi_connecting":    {"effect": "Arc Spin",        "colours": [[255, 170, 0], [0, 0, 0]]},
    "wifi_error":         {"effect": "Breathe",         "colours": [[255, 0, 0], [72, 0, 0]], "lifetime": 4},
    "setup_mode":         {"effect": "Arc Spin",        "colours": [[255, 85, 0], [0, 0, 0]]},
    "setup_success":      {"effect": "Breathe",         "colours": _CY_ON_BLUE, "lifetime": 4},
    "setup_error":        {"effect": "Breathe",         "colours": [[102, 0, 204], [10, 0, 20]], "lifetime": 4},

    "bt_pairing":         {"effect": "Arc Spin",        "colours": [[0, 0, 255], [0, 0, 0]]},
    "bt_connected":       {"effect": "Breathe",         "colours": _BLUE_DIM, "lifetime": 3},
    "bt_disconnected":    {"effect": "Breathe",         "colours": _BLUE_DIM, "lifetime": 3},
}


# ---------------------------------------------------------------------------
# Earcons
# ---------------------------------------------------------------------------
#
# Activity -> stock sound, the audio counterpart of DEFAULT_MAP above. These are
# AMAZON'S OWN earcons, converted to FLAC, so a fresh device sounds as it did.
# Overridable per activity via SOUND_MAP_FILE, exactly like the animations.
#
# WHO PLAYS WHAT. Unlike the LED map this table has three owners, because
# linux-voice-assistant already plays sounds of its own and playing a sound
# twice is worse than not playing it at all:
#
#   lva      passed to the assistant as --<name>-sound at launch. Those are
#            read once at startup, so changing one restarts the assistant -
#            the same constraint the mic chain has.
#   agent    played here. Only the ones the assistant has no sound for.
#   service  played by the boot script, wifi.start, biscuit-setup.sh,
#            biscuit-pair-session or biscuit-btring, which are shell and read
#            the resolved env file rather than this table.
#
# Everything is listed here regardless of owner so there is exactly ONE table
# of what the device's stock behaviour is, which is the same reason DEFAULT_MAP
# lists activities it does not play itself.
# The earcon table lives in device-amazon-biscuit, not here: boot, Wi-Fi, setup
# and Bluetooth all play sounds on a device with no assistant installed, so the
# table cannot depend on this agent. Imported rather than duplicated so there is
# still exactly ONE of it.
_em_spec = importlib.util.spec_from_file_location(
    "biscuit_earcon_map", "/usr/bin/biscuit-earcon-map.py")
_em = importlib.util.module_from_spec(_em_spec)
_em_spec.loader.exec_module(_em)

EARCON_DIR = _em.EARCON_DIR
earcon_dirs = _em.earcon_dirs
SOUND_MAP_FILE = _em.SOUND_MAP_FILE
DEFAULT_SOUNDS = _em.DEFAULT_SOUNDS
sound_path = _em.sound_path

# Sounds this process plays. Anything else belongs to the assistant or a shell
# service, and the agent must not play it on a pipeline event.
AGENT_SOUNDS = {k for k, v in DEFAULT_SOUNDS.items() if v.get("owner") == "agent"}



def play_sound(path):
    """Fire and forget.

    sndfile-play rather than mpv: the mpv CLI is not installed (only libmpv, for
    the assistant), and libsndfile is already here. It opens ALSA `default`,
    which /etc/asound.conf documents as the one insertion point every player
    goes through - so an earcon lands in biscuit-dsp and under the single device
    volume like everything else, with nothing to attenuate separately.

    Detached and never waited on: an earcon must not be able to delay the ring,
    which is the piece of feedback whose whole job is to be immediate.
    """
    if not path:
        return
    try:
        subprocess.Popen(["sndfile-play", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as err:
        _LOGGER.warning("earcon %s did not play: %s", path, err)


# ---------------------------------------------------------------------------
# Audio chain settings, as individual Home Assistant controls
# ---------------------------------------------------------------------------
#
# These replace the single "Microphone Processing" dropdown, which bundled six
# fixed combinations.  They are one canonical configuration: HA writes it,
# 8080 translates a preset into it, and biscuit-mic-stream.sh's -compose mode
# executes it.  Do not add a second writer for MIC_MODE here.
#
# TUNING IS ONE CONTROL, NOT ONE PER STAGE. biscuit-beamform takes a single
# weights file and a single profile flag for the whole chain, so "stock AEC with
# pmOS beamforming" is not expressible today. Splitting it would mean teaching
# the beamformer to carry two of each; until then one honest control beats two
# that silently interact.
MIC_SOURCES  = ["Array", "Single microphone"]
MIC_CAPSULES = ["MK%d (outer)" % n for n in range(1, 7)] + ["MK7 (centre)"]
MIC_TUNINGS  = ["Stock tuning", "pmOS tuning"]
ON_OFF       = ["On", "Off"]

_MIC_SOURCE_FLAG = {"Array": "array", "Single microphone": "single"}
_MIC_TUNING_FLAG = {"Stock tuning": "stock", "pmOS tuning": "pmos"}

# The stock processing generation. This REPLACES the old two-way Stock/pmOS tuning
# control rather than sitting beside it, so no new control can disagree with an
# existing one.
#
# The two stock generations are neither interchangeable nor equally obtainable: a
# Fire OS 6 device carries none of the Fire OS 5 chain's coefficient files, and most
# units in the field are on Fire OS 6. See release-work-20260916/profiles/.
# "pmOS 8-beam tuning" is pmOS's second chain: Fire OS 6's architecture on
# coefficients it designs itself, so, like "pmOS tuning", it needs nothing
# imported. See run_pmos8 in biscuit-mic-stream.sh for what it is and why.
MIC_PROFILES = ["pmOS tuning", "pmOS 8-beam tuning", "Fire OS 5 tuning",
                "Fire OS 6 tuning"]
_MIC_PROFILE_FLAG = {"pmOS tuning": "pmos", "pmOS 8-beam tuning": "pmos8",
                     "Fire OS 5 tuning": "fireos5", "Fire OS 6 tuning": "fireos6"}
# Both pmOS chains; anything that means "a pmOS chain, not a stock one".
MIC_PMOS_PROFILES = ("pmOS tuning", "pmOS 8-beam tuning")
# Hashes, from the same manifest biscuit-mic-stream.sh reads before exec on every
# branch restart, so the page agrees with the thing that actually refuses. This used
# to compare sizes on both sides; a size passes a truncated file and, worse, a file
# from the wrong generation, which is loaded as coefficients and produces silence
# rather than an error. Hashing all four Fire OS 6 files costs 5 ms.
#
# The manifest is data, not code, and it ships with the package - hashes only. No
# vendor content is here or anywhere else in it.
PROFILE_ASSET_MANIFEST = "/usr/share/biscuit/biscuit-profile-assets.json"
_MIC_ASSET_CACHE = {}


def _profile_assets(flag):
    """[(path, bytes, sha256, required)] for one profile flag, or [] if unknown."""
    try:
        stamp = os.path.getmtime(PROFILE_ASSET_MANIFEST)
    except OSError:
        return []
    if _MIC_ASSET_CACHE.get("stamp") != stamp:
        try:
            with open(PROFILE_ASSET_MANIFEST) as handle:
                profiles = json.load(handle)["profiles"]
        except (OSError, ValueError, KeyError):
            return []
        _MIC_ASSET_CACHE.clear()
        _MIC_ASSET_CACHE["stamp"] = stamp
        for name, spec in profiles.items():
            entries = []
            for f in spec.get("files", []):
                # A file may legitimately have several acceptable contents. The
                # Bluetooth ROM patches differ between Fire OS 5 and 6, so those
                # entries carry "variants" instead of a single bytes/sha256 pair
                # and a consumer that assumes the flat form raises KeyError.
                if f.get("variants"):
                    sizes = [v["bytes"] for v in f["variants"]]
                    hashes = [v["sha256"] for v in f["variants"]]
                else:
                    sizes = [f["bytes"]]
                    hashes = [f["sha256"]]
                entries.append((os.path.join(spec["directory"], f["name"]),
                                sizes, hashes, f.get("required", True)))
            _MIC_ASSET_CACHE[name] = entries
    return _MIC_ASSET_CACHE.get(flag, [])
# The Fire OS 6 chain is a separate binary, not a mode of biscuit-beamform.
MIC_FIREOS6_FRONTEND = "/usr/bin/biscuit-mic-fireos6"

# Which detector decides when the chain may adapt. Fire OS 6 only: the pmOS and
# Fire OS 5 chains gate adaptation their own way and have no equivalent knob, so
# this control is shown as not applicable on them rather than silently ignored.
#
# Both options are real and the choice is a genuine preference, which is why it is
# offered at all. Measured on the three qualification captures:
#
#              quiet  playback  interferer  total
#   Energy        19        19          15     53
#   Stock DNN     17        20          17     54
#
# The DNN wins barge-in outright and the interferer by two, and loses the quiet
# room by two. A device that lives in a quiet room is genuinely better off with
# the energy detector, so this is not a "worse" option to be hidden.
MIC_VADS = ["Stock DNN", "Energy (built-in)"]
_MIC_VAD_FLAG = {"Stock DNN": "auto", "Energy (built-in)": "energy"}
# Owner-imported, like every other stock asset: a proprietary Amazon file from
# system_a, which the merged v2 conversion destroys.
MIC_VAD_MODEL = ("/usr/share/biscuit/fireos6/vad_lite.tflite", 50704)
# Shipped for microWakeWord; the VAD reuses it rather than carrying its own.
MIC_VAD_RUNTIME = "/usr/lib/biscuit-tflite/libtensorflowlite_c.so"


def mic_vad_unavailable(label):
    """Why this detector cannot be selected, or "" if it can."""
    if label != "Stock DNN":
        return ""
    # Checked against the manifest so a corrupt model is caught here rather than
    # loaded and left to gate adaptation on nonsense. It is the one optional entry
    # in the Fire OS 6 set: absent is a working chain on the energy detector,
    # present-but-wrong is not.
    path, size = MIC_VAD_MODEL
    # Both are LISTS now: a manifest entry may accept more than one content.
    # vad_lite.tflite has only one, but reading it as a scalar would break the
    # moment it gained a second, which is exactly how the Bluetooth ROM patches
    # broke this function.
    digests = next((d for p, _b, d, _r in _profile_assets("fireos6") if p == path), [])
    if not os.path.exists(path):
        return "needs vad_lite.tflite imported from this device's stock firmware"
    if not any(_file_is(path, size, d) for d in digests):
        return "the imported vad_lite.tflite is not the model this chain expects"
    if not os.access(MIC_VAD_RUNTIME, os.R_OK):
        return "the TensorFlow Lite runtime is not installed"
    return ""


def mic_vad_options():
    """Offerable detectors, always including whatever is currently selected."""
    options = [v for v in MIC_VADS if not mic_vad_unavailable(v)]
    current = load_mic_settings()["mic_vad"]
    return options if current in options else options + [current]


def mic_vad_applies(settings=None):
    """Only the Fire OS 6 chain has a selectable adaptation gate."""
    if settings is None:
        settings = load_mic_settings()
    return settings["mic_profile"] == "Fire OS 6 tuning"


def default_mic_vad():
    return "Stock DNN" if not mic_vad_unavailable("Stock DNN") else "Energy (built-in)"
# Fire OS 6 is an eight-channel array processor with no single-capsule path, so that
# combination cannot be expressed rather than merely being untuned.
MIC_PROFILE_ARRAY_ONLY = ("Fire OS 6 tuning", "pmOS 8-beam tuning")


def _file_is(path, size, digest=None):
    """Size first, then content. Both, because they fail differently.

    A wrong size is the ordinary case - a partial copy, or nothing there at all -
    and costs a stat. A right size with wrong content is the one worth the read:
    a file from the other generation, or a truncated-then-padded copy, which the
    chain would load as coefficients and turn into silence rather than an error.
    """
    try:
        if os.path.getsize(path) != size:
            return False
        if digest is None:
            return True
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest() == digest
    except OSError:
        return False


def mic_profile_unavailable(label):
    """Why this profile cannot be selected, or "" if it can."""
    absent, wrong = [], []
    for path, sizes, digests, required in _profile_assets(_MIC_PROFILE_FLAG.get(label, "")):
        if not os.path.exists(path):
            if required:
                absent.append(path)
        # sizes and digests are parallel LISTS since a file may have several
        # acceptable contents. Passing the lists straight to _file_is compared
        # an int to a list, so from 2026-09-21 every correctly imported Fire OS
        # 6 file read as "does not match" and the profile could not be chosen.
        elif not any(_file_is(path, s, d) for s, d in zip(sizes, digests)):
            # Present but not the file it claims to be. Reported apart from
            # "missing" because the remedy is different: this one needs the
            # import re-run, not performed.
            wrong.append(path)
    if absent:
        return ("needs stock files this device does not have: %s"
                % ", ".join(os.path.basename(p) for p in absent))
    if wrong:
        return ("these imported files do not match what this chain expects, so the "
                "import needs re-running: %s"
                % ", ".join(os.path.basename(p) for p in wrong))
    # The 8-beam chain runs in the same binary as Fire OS 6, without its files.
    if (label in ("Fire OS 6 tuning", "pmOS 8-beam tuning")
            and not os.access(MIC_FIREOS6_FRONTEND, os.X_OK)):
        return "the 8-beam processing binary is not installed"
    return ""


# Written by biscuit-mic-stream.sh immediately before it execs the chain, so it
# reports what is running rather than what was asked for. The two differ whenever
# a requested stock generation's assets are absent at start: the script falls back
# to the pmOS chain rather than leaving the device deaf, and without this file
# nothing downstream could tell.
MIC_PROFILE_STATUS = "/run/biscuit-mic/profile.json"


def mic_profile_status():
    """What the microphone chain is actually running.

    Returns a dict with "requested", "effective" and "reason" as labels/text, or
    None when the branch has not published yet - a fresh boot before the first
    start, or an older biscuit-mic-stream.sh. None means "unknown", never "fine":
    callers must not report agreement they have not observed.
    """
    try:
        with open(MIC_PROFILE_STATUS) as status:
            raw = json.load(status)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    rev = {v: k for k, v in _MIC_PROFILE_FLAG.items()}

    def label(key):
        flag = str(raw.get(key, "") or "")
        return rev.get(flag, flag)

    effective = label("effective")
    if not effective:
        return None
    return {"requested": label("requested") or effective,
            "effective": effective,
            "reason": str(raw.get("reason", "") or "")}


def mic_profile_degraded():
    """(degraded, explanation). Degraded means running something else."""
    status = mic_profile_status()
    if status is None or status["effective"] == status["requested"]:
        return False, ""
    reason = status["reason"]
    return True, ("%s is selected but the device is running %s%s"
                  % (status["requested"], status["effective"],
                     " (%s)" % reason if reason and reason != "ok" else ""))


def mic_profiles_available():
    return [p for p in MIC_PROFILES if not mic_profile_unavailable(p)]


def mic_profile_options():
    """Offerable profiles, always including whatever is currently selected.

    A profile can become unavailable under a running configuration - an asset
    removed, a package downgraded - and dropping it from the options would leave
    Home Assistant holding a value outside its own list.
    """
    options = mic_profiles_available()
    current = load_mic_settings()["mic_profile"]
    return options if current in options else options + [current]
def default_mic_profile():
    """The profile a device runs until its owner chooses one: pmOS 8-beam.

    Decided by the 2026-09-27 comparison sitting (qual-0927-compare, doc 9.23):
    TTS talkers at 1 m, 3 m and to the side, replayed through every profile on
    identical audio. With the four normal-speed voices,

                            pmOS   pmOS 8-beam   Fire OS 6
        quiet               100%       100%          81%
        the Echo's music     41%       100%         100%
        another speaker      38%        28%          44%    (all limited by level)
        overall              58%        74%          74%
        false accepts          0          0            2

    and 79/135 against pmOS's 59/135 over all six voices (McNemar p < 0.001).
    8-beam also needs no owner-imported files, so it is the default whether or
    not stock files are present. It used to prefer an imported Fire OS 6, then
    Fire OS 5; both stay in the dropdown. pmOS is the fallback if the 8-beam
    binary is missing.
    """
    if not mic_profile_unavailable("pmOS 8-beam tuning"):
        return "pmOS 8-beam tuning"
    return "pmOS tuning"



# The analogue PGA, in dB. 0.5 dB per step, so 20 dB is raw 40 - which is what
# the UCM sets and what stock uses. Deliberately NOT offered up to the control's
# full 40 dB: too much gain here clips before the calibrated array stages.
# Stock uses a fixed 20 dB and the processed LVA profile deliberately adds no
# second AGC, noise suppression, or software mic-volume stage after this chain.
MIC_GAIN_DB_MIN, MIC_GAIN_DB_MAX, MIC_GAIN_DB_DEFAULT = 10.0, 30.0, 20.0

MIC_DEFAULTS = {
    "mic_source": "Array",
    # Only a last resort. The real choice is default_mic_profile(),
    # which prefers whichever stock generation this device can actually
    # run. pmOS is always available, so it is the safe value here.
    "mic_profile": "pmOS tuning",
    "mic_tuning": "Stock tuning",
    "mic_aec": "On",
    # This is the adaptive cleanup after the fixed six-beam stage.  The fixed
    # stock beamformer remains part of the Stock tuning path.
    "mic_beam": "On",
    # Last resort only; the real choice is default_mic_vad(), which prefers the
    # stock detector when the owner has imported it.
    "mic_vad": "Energy (built-in)",
}

# Legacy flags remain readable so an older persisted mic.env migrates without
# changing its effective path the first time it is opened.  Saving any control
# writes the composed schema below.
MIC_PRESET_SETTINGS = {
    "Stock chain (default)": dict(MIC_DEFAULTS),
    "Stock chain, no AEC": {
        "mic_source": "Array", "mic_tuning": "Stock tuning",
        "mic_aec": "Off", "mic_beam": "On",
    },
    "Stock chain, no adaptive": {
        "mic_source": "Array", "mic_tuning": "Stock tuning",
        "mic_aec": "On", "mic_beam": "Off",
    },
    "Centre mic (stock gain)": {
        "mic_source": "Single microphone", "mic_tuning": "Stock tuning",
        "mic_aec": "On", "mic_beam": "Off",
    },
    "Centre mic": {
        "mic_source": "Single microphone", "mic_tuning": "pmOS tuning",
        "mic_aec": "Off", "mic_beam": "Off",
    },
    "Our beamformer": {
        "mic_source": "Array", "mic_tuning": "pmOS tuning",
        "mic_aec": "Off", "mic_beam": "Off",
    },
}
MIC_LEGACY_SETTINGS_BY_FLAG = {
    flag: dict(MIC_PRESET_SETTINGS[label]) for label, flag in MIC_MODES
}

EQ_MODES = eq_store.EQ_MODES
EQ_BANDS = eq_store.EQ_BANDS
EQ_DB_MIN, EQ_DB_MAX = eq_store.EQ_DB_MIN, eq_store.EQ_DB_MAX
EQ_STATE = eq_store.EQ_STATE

EARCON_SETS = ["Amazon", "Assistant defaults", "Off"]
EARCON_SET_FILE = "/opt/persist/earcon-set"
# What the Sounds select shows when the settings page has chosen sounds one by
# one, so the map matches none of the sets. Offered as an option so HA can show
# it; choosing it does nothing, since it is a description rather than a set.
EARCON_CUSTOM = "Custom"

MICCAL_MODES = ["Factory", "Default", "Manual"]
MICCAL_MODE_FILE = "/opt/persist/miccal-mode"
_MICCAL_FLAG = {"Factory": "factory", "Default": "default", "Manual": "manual"}


def _read_env_file(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip("'").strip('"')
    except (OSError, ValueError):
        # ValueError: not UTF-8. Defaults, rather than an agent that can
        # never register with the assistant again.
        pass
    return out


MIC_RESTART_OUTCOME = "/run/biscuit-mic/restart.json"


def mic_restart_outcome():
    """How the last capture restart went, or None if none has been recorded.

    The restart is detached and takes about ten seconds, so nothing that asks
    for one can wait for the answer. It lands here instead, and both front ends
    read it: a refusal during a call, a rollback, or a chain that never came
    back are all things the person who pressed the button needs told about.
    """
    try:
        with open(MIC_RESTART_OUTCOME) as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def restart_capture(reason="settings"):
    """Reload the single capture owner and refresh active LVA registrations.

    Socket clients reconnect. The helper restarts LVA only if it was already
    active, so changed select options and availability are rediscovered.
    Detach because that restart closes this caller's LVA websocket - which is
    also why the result cannot be returned here and goes to restart.json.
    """
    try:
        subprocess.Popen(["/usr/bin/python3", "/usr/bin/biscuit-mic-restart.py",
                          "--reason", reason],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as err:
        _LOGGER.warning("could not restart microphone hub: %s", err)
    return True


def _mic_pga_db(value):
    """Clamp and snap the user value to the codec's 0.5 dB register steps."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = MIC_GAIN_DB_DEFAULT
    value = max(MIC_GAIN_DB_MIN, min(MIC_GAIN_DB_MAX, value))
    return round(value * 2.0) / 2.0


def _mic_capsule(value):
    try:
        index = int(value)
    except (TypeError, ValueError):
        index = 6
    return MIC_CAPSULES[index if 0 <= index <= 6 else 6]


def load_mic_settings():
    """Return the one canonical mic configuration as Home Assistant labels."""
    env = _read_env_file(MIC_ENV)
    rev_src = {v: k for k, v in _MIC_SOURCE_FLAG.items()}
    rev_tun = {v: k for k, v in _MIC_TUNING_FLAG.items()}
    settings = dict(MIC_LEGACY_SETTINGS_BY_FLAG.get(
        env.get("MIC_MODE", ""), MIC_DEFAULTS))
    settings["mic_source"] = rev_src.get(
        ("single" if env.get("BISCUIT_MIC_SOURCE") == "centre" else
         env.get("BISCUIT_MIC_SOURCE")), settings["mic_source"])
    # BISCUIT_MIC_PROFILE is canonical. BISCUIT_MIC_TUNING is still honoured
    # when it is absent so an existing mic.env keeps its behaviour, and it is
    # mapped explicitly rather than through the availability default: a
    # device already running Fire OS 5 must not silently become Fire OS 6
    # just because its files went missing.
    _rev_profile = {v: k for k, v in _MIC_PROFILE_FLAG.items()}
    _profile_flag = env.get("BISCUIT_MIC_PROFILE")
    _tuning_flag = env.get("BISCUIT_MIC_TUNING")
    if _profile_flag in _rev_profile:
        settings["mic_profile"] = _rev_profile[_profile_flag]
    elif _tuning_flag == "pmos":
        settings["mic_profile"] = "pmOS tuning"
    elif _tuning_flag == "stock":
        settings["mic_profile"] = "Fire OS 5 tuning"
    else:
        settings["mic_profile"] = default_mic_profile()
    # Kept in step so anything still reading the old key sees the truth.
    settings["mic_tuning"] = ("pmOS tuning"
                              if settings["mic_profile"] in MIC_PMOS_PROFILES
                              else "Stock tuning")
    if env.get("BISCUIT_MIC_AEC") in ("on", "off"):
        settings["mic_aec"] = "On" if env["BISCUIT_MIC_AEC"] == "on" else "Off"
    if env.get("BISCUIT_MIC_BEAM") in ("on", "off"):
        settings["mic_beam"] = "On" if env["BISCUIT_MIC_BEAM"] == "on" else "Off"
    if settings["mic_source"] == "Single microphone":
        settings["mic_beam"] = "Off"
    settings["mic_capsule"] = _mic_capsule(env.get("BISCUIT_MIC_CHANNEL"))
    settings["call_mic_capsule"] = _mic_capsule(env.get("BISCUIT_CALL_MIC_CHANNEL"))
    settings["call_profile"] = "Stock-derived tuning" if env.get("BISCUIT_CALL_PROFILE") == "stock" else "Open-source defaults"
    settings["call_processing"] = "Off" if env.get("BISCUIT_CALL_PROCESSING") == "off" else "On"
    settings["mic_pga_gain"] = _mic_pga_db(env.get("BISCUIT_MIC_PGA_DB"))
    # "auto" means the stock detector when it is present, which is exactly what
    # the branch script does with the same value, so the page and the chain
    # cannot disagree.
    _vad_flag = env.get("BISCUIT_MIC_FIREOS6_VAD")
    if _vad_flag == "energy":
        settings["mic_vad"] = "Energy (built-in)"
    elif _vad_flag == "auto":
        settings["mic_vad"] = "Stock DNN"
    else:
        settings["mic_vad"] = default_mic_vad()
    return settings


def mic_aec_available(settings=None):
    """Whether the Echo Cancellation control means anything right now.

    The centre passthrough does not run the array echo canceller. Fire OS 6
    and the 8-beam chain decide it themselves: their per-microphone canceller
    runs only when the adaptive stage is off, because in front of that stage
    it measured as a net loss (see run_fireos6 in biscuit-mic-stream.sh).
    """
    if settings is None:
        settings = load_mic_settings()
    return (settings["mic_source"] != "Single microphone"
            and settings["mic_profile"] not in ("pmOS 8-beam tuning",
                                                "Fire OS 6 tuning"))


def save_mic_settings(settings, restart=True):
    """Atomically save the canonical chain and optionally restart capture.

    Restarting biscuit-mic-pump restarts the assistant with it - about ten
    seconds deaf - so callers must only do this on a real change. Home Assistant
    re-sends every value on reconnect, and obeying that would restart the
    pipeline every time HA blinked.

    Detached, because the restart tears down the websocket this agent is
    talking to.
    """
    old = _read_env_file(MIC_ENV)
    source = settings.get("mic_source", MIC_DEFAULTS["mic_source"])
    _previous_profile = load_mic_settings()["mic_profile"]
    profile = settings.get("mic_profile", _previous_profile)
    if profile not in MIC_PROFILES:
        raise ValueError("unknown microphone profile %r" % profile)
    # Only block a CHANGE to an unavailable profile. Refusing to save anything
    # else while the current profile is unavailable would make an unrelated
    # control unusable until the assets were restored.
    _why = mic_profile_unavailable(profile)
    if _why and profile != _previous_profile:
        raise ValueError("%s cannot be selected: %s" % (profile, _why))
    tuning = "pmOS tuning" if profile in MIC_PMOS_PROFILES else "Stock tuning"
    aec = settings.get("mic_aec", MIC_DEFAULTS["mic_aec"])
    beam = settings.get("mic_beam", MIC_DEFAULTS["mic_beam"])
    if source not in MIC_SOURCES:
        source = MIC_DEFAULTS["mic_source"]
    if profile in MIC_PROFILE_ARRAY_ONLY and source == "Single microphone":
        raise ValueError("%s has no single-microphone path" % profile)
    if aec not in ON_OFF:
        aec = MIC_DEFAULTS["mic_aec"]
    if source == "Single microphone":
        # Keep the array preference, but never accept an ineffective centre
        # AEC change from a stale UI or a direct caller.
        aec = load_mic_settings()["mic_aec"]
    if beam not in ON_OFF or source == "Single microphone":
        beam = "Off"
    previous = load_mic_settings()
    capsules = {}
    for key in ("mic_capsule", "call_mic_capsule"):
        label = settings.get(key, previous[key])
        if label not in MIC_CAPSULES:
            raise ValueError("unknown microphone capsule %r" % label)
        capsules[key] = MIC_CAPSULES.index(label)
    selected_profile = settings.get("call_profile", previous["call_profile"])
    if selected_profile not in call_profile.PROFILE_LABELS:
        raise ValueError("unknown call profile")
    if selected_profile == "Stock-derived tuning" and call_profile.stock_info() is None:
        raise ValueError("import stock AFE.cfg before selecting stock-derived tuning")
    call_processing = settings.get("call_processing", previous["call_processing"])
    if call_processing not in ON_OFF:
        raise ValueError("unknown call processing value")
    pga = _mic_pga_db(settings.get("mic_pga_gain", previous["mic_pga_gain"]))
    vad = settings.get("mic_vad", previous["mic_vad"])
    if vad not in MIC_VADS:
        vad = default_mic_vad()
    # Refuse a detector this device cannot run rather than writing it and
    # letting the branch quietly fall back - the page would then show one
    # thing and the chain do another.
    _why = mic_vad_unavailable(vad)
    if _why and vad != previous["mic_vad"]:
        raise ValueError("%s: %s" % (vad, _why))

    # MIC_GAIN is the beamformer's output trim, not the analogue PGA exposed
    # above.  Preserve it across either UI so speaker/mic gain experiments do
    # not disappear when a user changes a preset.
    preserved = {}
    try:
        gain = float(old.get("MIC_GAIN", ""))
        if 0.0 < gain <= 8.0:
            preserved["MIC_GAIN"] = "%.6g" % gain
    except ValueError:
        pass
    if old.get("MIC_RATE") == "16000":
        preserved["MIC_RATE"] = "16000"

    os.makedirs(os.path.dirname(MIC_ENV), exist_ok=True)
    tmp = MIC_ENV + ".tmp"
    with open(tmp, "w") as f:
        f.write("# Unified microphone configuration: Home Assistant and 8080.\n")
        f.write("MIC_MODE=-compose\n")
        f.write("BISCUIT_MIC_SOURCE=%s\n" % _MIC_SOURCE_FLAG[source])
        f.write("BISCUIT_MIC_CHANNEL=%d\n" % capsules["mic_capsule"])
        f.write("BISCUIT_CALL_MIC_CHANNEL=%d\n" % capsules["call_mic_capsule"])
        f.write("BISCUIT_CALL_PROCESSING=%s\n" % call_processing.lower())
        f.write("BISCUIT_CALL_PROFILE=%s\n" % ("stock" if selected_profile == "Stock-derived tuning" else "open"))
        f.write("BISCUIT_MIC_PROFILE=%s\n" % _MIC_PROFILE_FLAG[profile])
        # Written too, derived from the profile, so that rolling the stream
        # script back to a build without the profile field degrades to a
        # working chain rather than a broken one.
        f.write("BISCUIT_MIC_TUNING=%s\n" % _MIC_TUNING_FLAG[tuning])
        if profile in ("Fire OS 6 tuning", "pmOS 8-beam tuning"):
            f.write("BISCUIT_MIC_FIREOS6_FRONTEND=%s\n" % MIC_FIREOS6_FRONTEND)
        f.write("BISCUIT_MIC_AEC=%s\n" % ("on" if aec == "On" else "off"))
        f.write("BISCUIT_MIC_BEAM=%s\n" % ("on" if beam == "On" else "off"))
        f.write("BISCUIT_MIC_PGA_DB=%.1f\n" % pga)
        # Written for every profile, not just Fire OS 6, so the choice
        # survives a trip through another generation and back.
        f.write("BISCUIT_MIC_FIREOS6_VAD=%s\n" % _MIC_VAD_FLAG[vad])
        for key, choices in (("BISCUIT_MIC_ADAPTIVE", ("on", "off")),
                             ("BISCUIT_MIC_SELECTOR", ("stock", "legacy", "contrast"))):
            if old.get(key) in choices:
                f.write("%s=%s\n" % (key, old[key]))
        for key in ("MIC_GAIN", "MIC_RATE"):
            if key in preserved:
                f.write("%s=%s\n" % (key, preserved[key]))
        f.flush()
        os.fsync(f.fileno())
    _durable_replace(tmp, MIC_ENV)
    if restart:
        restart_capture("microphone settings")
    return True


def load_eq():
    return eq_store.load()


def save_eq(s):
    """Apply the EQ selection and reload the DSP live, through biscuit_eq -
    the same code the settings page saves with and biscuit-dsp applies at
    boot, so the three can never write different files."""
    return eq_store.save(s)


def load_earcon_set():
    try:
        with open(EARCON_SET_FILE) as f:
            v = f.read().strip()
        for label in EARCON_SETS:
            if label.lower() == v.lower():
                return label
    except (OSError, ValueError):
        pass
    return EARCON_SETS[0]


def save_earcon_set(label):
    """Choose which sounds play, by writing the per-activity override map.

    Amazon             - the shipped stock earcons, i.e. no overrides at all.
    Assistant defaults - clear our sounds so linux-voice-assistant uses its own
                         bundled ones for the activities it owns.
    Off                - silence every activity.

    Per-activity choices are deliberately NOT here: seventeen selects would
    swamp the Home Assistant page for something most people set once. They live
    on the settings page at :8080, which writes the same file.
    """
    os.makedirs(os.path.dirname(EARCON_SET_FILE), exist_ok=True)
    with open(EARCON_SET_FILE, "w") as f:
        f.write(label + "\n")

    overrides = earcon_set_overrides(label)
    tmp = SOUND_MAP_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(SOUND_MAP_FILE), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(overrides, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        _durable_replace(tmp, SOUND_MAP_FILE)
    except OSError as err:
        _LOGGER.warning("cannot write %s: %s", SOUND_MAP_FILE, err)
        return False
    # The assistant reads its sound flags once at launch, so the ones it owns
    # only change on restart. Ours take effect immediately.
    subprocess.Popen(["rc-service", "--ifstarted", "biscuit-voice-assistant", "restart"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def earcon_set_overrides(label):
    """The sound map a set writes: what save_earcon_set puts in the file."""
    if label == "Off":
        return {k: {"sound": None} for k in DEFAULT_SOUNDS}
    if label == "Assistant defaults":
        return {k: {"sound": None} for k, v in DEFAULT_SOUNDS.items()
                if v.get("owner") == "lva"}
    return {}


def current_earcon_set():
    """The set in force, or EARCON_CUSTOM when sounds were chosen one by one.

    Compares the sound map with what the chosen set would have written: the
    settings page edits the same file per activity, and after that the set's
    name alone no longer describes what plays.
    """
    label = load_earcon_set()
    try:
        with open(SOUND_MAP_FILE) as f:
            current = json.load(f)
    except FileNotFoundError:
        current = {}
    except (OSError, ValueError):
        return label
    return label if current == earcon_set_overrides(label) else EARCON_CUSTOM


def load_miccal_mode():
    try:
        with open(MICCAL_MODE_FILE) as f:
            v = f.read().strip().lower()
        for label, flag in _MICCAL_FLAG.items():
            if flag == v:
                return label
    except (OSError, ValueError):
        pass
    # Keep the HA entity honest when no persisted override exists: the
    # mic-calibration generator's verified stock-parity default is Factory.
    return "Factory"


def save_miccal_mode(label, restart=True):
    """Save the calibration choice; restart=False when the caller restarts.

    The settings page saves this together with the rest of the chain, and two
    restarts back to back had the second refused as busy - which the page then
    reported as a change that had not been applied, when it had.
    """
    os.makedirs(os.path.dirname(MICCAL_MODE_FILE), exist_ok=True)
    with open(MICCAL_MODE_FILE, "w") as f:
        f.write(_MICCAL_FLAG[label] + "\n")
    if restart:
        restart_capture("microphone calibration")
    return True


# Events the agent itself reacts to. Anything else in DEFAULT_MAP belongs to
# another service, and the agent must not try to play it on a pipeline event.
PIPELINE_ACTIVITIES = {
    "wake_word_detected", "listening", "stt_text", "thinking", "tts_speaking",
    "tts_finished", "pipeline_error", "timer_ticking", "timer_ringing",
}
IDLE_EVENTS = {"idle", "disconnected"}


def pactl(*args, **kw):
    """One pactl call. PipeWire runs system-wide here, so the runtime dir is fixed."""
    env = dict(os.environ, XDG_RUNTIME_DIR="/run/pipewire")
    return subprocess.run(("pactl",) + args, env=env, capture_output=True,
                          text=True, timeout=kw.get("timeout", 5))


PAIRING_MARKER = "/run/biscuit-pairing"


def pairing_open():
    return os.path.exists(PAIRING_MARKER)


def start_pairing():
    """Open the Bluetooth pairing window.

    Detached, because biscuit-pair-session runs for three minutes and this is
    called from the event loop. Idempotent enough: a second call while a window
    is open simply restarts it, which is what a user pressing the button again
    expects.
    """
    _LOGGER.info("opening the Bluetooth pairing window")
    try:
        subprocess.Popen(["/usr/bin/biscuit-pair-session"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("could not start pairing: %s", err)
        return False


def stop_pairing():
    """Close the window early.

    The MARKER GOES FIRST. biscuit-btaudio re-asserts discoverability every
    fifteen seconds while a window is open, so clearing the adapter before the
    marker leaves a race in which that loop turns it straight back on - which is
    exactly the "turns off for a second then opens back up" the switch used to
    show.

    `pairable` is cleared as well as `discoverable`. biscuit-pair-session sets
    both, and closing only one leaves the device still accepting pairings from
    anything that already knows its address.

    Blocking, so the agent runs it off the event loop, and bounded: with
    bluetoothd down, bluetoothctl waits for it forever.
    """
    _LOGGER.info("closing the Bluetooth pairing window")
    try:
        os.unlink(PAIRING_MARKER)
    except OSError:
        pass
    for cmd in (["pkill", "-f", "biscuit-pair-session"],
                ["bluetoothctl", "discoverable", "off"],
                ["bluetoothctl", "pairable", "off"]):
        try:
            subprocess.run(cmd, capture_output=True, check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as err:
            _LOGGER.warning("%s: %s", " ".join(cmd), err)


BTPROXY_CONF = "/opt/persist/btproxy.env"
BTPROXY_STATE = "/run/biscuit-btproxy/state"


def btproxy_active():
    """Whether outgoing (active) connections are on.

    Same rule as btproxy_enabled: the RUN state is what the service is actually
    doing; the config is only what it was last asked to do.
    """
    for path, key in ((BTPROXY_STATE, "active"), (BTPROXY_CONF, "btproxy_active")):
        try:
            with open(path) as f:
                for line in f:
                    k, _, v = line.strip().partition("=")
                    if k.strip().lower() == key:
                        return v.strip().strip(chr(34)).lower() in ("1", "yes", "true", "on")
        except (OSError, ValueError):
            continue
    return False


def set_btproxy_active(on):
    return _set_btproxy_key("BTPROXY_ACTIVE", on)


def btproxy_enabled():
    """Whether the Bluetooth proxy is on.

    Read from the RUN state file in preference to the config, because that is
    what the service itself last acted on - the config can have been edited a
    moment ago without a restart, and reporting the intended state as the
    actual one is how a switch ends up lying about what the radio is doing.
    """
    for path, key in ((BTPROXY_STATE, "enabled"), (BTPROXY_CONF, "btproxy_enabled")):
        try:
            with open(path) as f:
                for line in f:
                    k, _, v = line.strip().partition("=")
                    if k.strip().lower() == key:
                        return v.strip().strip('"').lower() in ("1", "yes", "true", "on")
        except (OSError, ValueError):
            continue
    return False


def _set_btproxy_key(key, on):
    """Rewrite one key in btproxy.env and restart the service.

    One helper for both switches: the enabled and active flags live in the same
    file and take effect the same way, and two near-identical writers would be
    two places to get the restart wrong.
    """
    lines, seen = [], False
    try:
        with open(BTPROXY_CONF) as f:
            for line in f:
                if line.strip().upper().startswith(key + "="):
                    lines.append("%s=%d" % (key, 1 if on else 0))
                    seen = True
                else:
                    lines.append(line.rstrip(chr(10)))
    except OSError:
        pass
    if not seen:
        lines.append("%s=%d" % (key, 1 if on else 0))
    try:
        os.makedirs(os.path.dirname(BTPROXY_CONF), exist_ok=True)
        tmp = BTPROXY_CONF + ".tmp"
        with open(tmp, "w") as f:
            f.write(chr(10).join(lines) + chr(10))
            f.flush()
            os.fsync(f.fileno())
        _durable_replace(tmp, BTPROXY_CONF)
    except OSError as err:
        _LOGGER.warning("could not write %s: %s", BTPROXY_CONF, err)
        return False
    subprocess.Popen(["rc-service", "biscuit-btproxy", "restart"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def set_btproxy_enabled(on):
    """Flip the proxy and restart it.

    The service is always in the runlevel and decides enabled-or-idle from this
    file, so toggling is a restart rather than an rc-update. That keeps the
    on/off state in ONE place; splitting it between a config file and a runlevel
    symlink is what made biscuit-mic-pump hard to reason about.
    """
    lines, seen = [], False
    try:
        with open(BTPROXY_CONF) as f:
            for line in f:
                if line.strip().upper().startswith("BTPROXY_ENABLED="):
                    lines.append("BTPROXY_ENABLED=%d" % (1 if on else 0))
                    seen = True
                else:
                    lines.append(line.rstrip(chr(10)))
    except OSError:
        pass
    if not seen:
        lines.append("BTPROXY_ENABLED=%d" % (1 if on else 0))
    try:
        os.makedirs(os.path.dirname(BTPROXY_CONF), exist_ok=True)
        tmp = BTPROXY_CONF + ".tmp"
        with open(tmp, "w") as f:
            f.write(chr(10).join(lines) + chr(10))
            f.flush()
            os.fsync(f.fileno())
        _durable_replace(tmp, BTPROXY_CONF)
    except OSError as err:
        _LOGGER.warning("could not write %s: %s", BTPROXY_CONF, err)
        return False
    subprocess.Popen(["rc-service", "biscuit-btproxy", "restart"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def load_autodim():
    return ring_config.load_autodim()


def save_autodim(on):
    ring_config.save_autodim(on)


def _load_led(path, modes, default):
    try:
        with open(path) as f:
            v = f.read().strip().capitalize()
        if v in modes:
            return v
    except (OSError, ValueError):
        pass
    return default


def _save_led(path, mode):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(mode.lower())
        f.flush()
        os.fsync(f.fileno())
    _durable_replace(tmp, path)
    # biscuit-audio owns the LED; ask it to re-apply rather than writing sysfs.
    audio_control("refresh-led")


def load_mic_led():
    return _load_led(MIC_LED_FILE, MIC_LED_MODES, "Off")


def save_mic_led(mode):
    _save_led(MIC_LED_FILE, mode)


def load_mute_led():
    return _load_led(MUTE_LED_FILE, MUTE_LED_MODES, "High")


def save_mute_led(mode):
    _save_led(MUTE_LED_FILE, mode)


def load_buttons():
    cfg = dict(BUTTON_DEFAULTS)
    try:
        with open(BUTTONS_FILE) as f:
            user = json.load(f)
        if isinstance(user, dict):
            for k, v in user.items():
                if k in cfg and v in BUTTON_ACTIONS:
                    cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("ignoring %s: %s", BUTTONS_FILE, err)
    return cfg


def save_buttons(cfg):
    os.makedirs(os.path.dirname(BUTTONS_FILE), exist_ok=True)
    tmp = BUTTONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    _durable_replace(tmp, BUTTONS_FILE)


def find_input_device(name):
    """Resolve an input device by name. Never trust the event number."""
    import glob
    for path in sorted(glob.glob("/dev/input/event*")):
        sysfs = "/sys/class/input/%s/device/name" % os.path.basename(path)
        try:
            with open(sysfs) as handle:
                if handle.read().strip() == name:
                    return path
        except OSError:
            continue
    return None


class Gestures:
    """Turn press/release timings into single / double / triple / long.

    Kept free of I/O so the timing rules can be tested without a finger on the
    hardware: feed it press() and release() with explicit timestamps and it
    returns the gesture, or None when it is still waiting to see whether another
    press is coming.

    A long press fires on the way DOWN, once the hold threshold passes, and then
    suppresses the release that follows - otherwise every hold would also
    register as a single press when the user let go.
    """

    def __init__(self, long_s=LONG_PRESS_S, multi_s=MULTI_PRESS_S):
        self.long_s = long_s
        self.multi_s = multi_s
        self.count = 0
        self.pressed_at = None
        self.last_release = None
        self.suppress_release = False

    def press(self, now):
        self.pressed_at = now
        return None

    def release(self, now):
        held = now - (self.pressed_at or now)
        self.pressed_at = None
        if self.suppress_release:
            self.suppress_release = False
            return None
        if held >= self.long_s - GESTURE_EPS:
            self.count = 0
            return "long"
        self.count += 1
        self.last_release = now
        return None

    def tick(self, now):
        """Called between events. Emits a long press while still held, and
        settles a multi-press once its window has closed."""
        if self.pressed_at is not None and now - self.pressed_at >= self.long_s - GESTURE_EPS:
            self.pressed_at = None
            self.suppress_release = True
            self.count = 0
            return "long"
        if self.count and self.last_release is not None \
                and now - self.last_release >= self.multi_s - GESTURE_EPS:
            n = min(self.count, 3)
            self.count = 0
            self.last_release = None
            return ("single", "double", "triple")[n - 1]
        return None

    def timeout(self, now):
        """How long the caller may sleep before tick() has something to do."""
        waits = []
        if self.pressed_at is not None:
            waits.append(self.long_s - (now - self.pressed_at))
        if self.count and self.last_release is not None:
            waits.append(self.multi_s - (now - self.last_release))
        if not waits:
            return None
        # Never return 0: a zero timeout on a deadline that has not quite
        # arrived is a busy loop. A floor of one epsilon guarantees forward
        # progress on every wake.
        return max(GESTURE_EPS, min(waits))


async def watch_action_button(ws):
    """Action button -> a gesture -> Home Assistant, and the chosen local action."""
    import struct
    path = find_input_device(BUTTON_DEVICE)
    if not path:
        _LOGGER.warning("action button device %r not found; gestures disabled",
                        BUTTON_DEVICE)
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as err:
        _LOGGER.warning("cannot open %s: %s", path, err)
        return
    _LOGGER.info("action button: watching %s (%s)", path, BUTTON_DEVICE)

    # struct input_event is 16 bytes on 32-bit time_t and 24 on 64-bit. Read the
    # size the running kernel actually uses rather than assuming.
    size = 24 if struct.calcsize("@llHHi") == 24 else 16
    fmt = "@llHHi" if size == 24 else "@iiHHi"

    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    loop.add_reader(fd, lambda: queue.put_nowait(True))
    g = Gestures()
    try:
        while True:
            wait = g.timeout(time.monotonic())
            try:
                await asyncio.wait_for(queue.get(), timeout=wait if wait else None)
                while True:
                    try:
                        data = os.read(fd, size)
                    except BlockingIOError:
                        break
                    if len(data) < size:
                        break
                    _s, _us, etype, code, value = struct.unpack(fmt, data)
                    if etype != 1 or code != KEY_ASSISTANT:
                        continue
                    now = time.monotonic()
                    gesture = g.press(now) if value == 1 else (
                        g.release(now) if value == 0 else None)
                    if gesture:
                        await fire_gesture(ws, gesture)
            except asyncio.TimeoutError:
                pass
            gesture = g.tick(time.monotonic())
            if gesture:
                await fire_gesture(ws, gesture)
    finally:
        loop.remove_reader(fd)
        os.close(fd)


async def fire_gesture(ws, gesture):
    """Report the gesture to Home Assistant, then do the chosen local action.

    Both, always. Reporting is what lets an automation react to a gesture whose
    local action is "Nothing", and doing it first means Home Assistant sees the
    press even if the local action fails.
    """
    _LOGGER.info("action button: %s press", gesture)
    try:
        await ws.send(json.dumps({"command": "button_%s_press" % gesture}))
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("could not report the gesture: %s", err)

    action = BUTTON_ACTIONS.get(load_buttons().get(gesture))
    if not action:
        return
    # Actions this agent performs itself. Anything NOT in here is a
    # peripheral-API command of the same name, so BUTTON_ACTIONS above stays
    # the only mapping and there is no second one to keep in step.
    #
    # A TABLE, not a chain of elifs, because the chain was wrong. Its guard
    # named only toggle_mute and setup_mode, so "Bluetooth pairing" - the
    # DEFAULT long-press action - fell through to the assistant as a command
    # linux-voice-assistant does not implement, and the start_pairing() branch
    # below it was unreachable. The gesture reported itself to Home Assistant
    # and then silently did nothing, which is why the failure survived: the
    # button looked like it worked. A negative list has to be kept in step with
    # the branches it guards; a table cannot fall out of step with itself.
    local = {
        "toggle_mute": lambda: request_mute(not read_hw_muted()),
        "bt_pairing": start_pairing,
        # restart, never start: the setup service is a one-shot and start is
        # refused with "has already been started" once it has run.
        "setup_mode": lambda: subprocess.Popen(
            ["rc-service", "biscuit-setup", "restart"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True),
    }
    handler = local.get(action)
    if handler is not None:
        try:
            handler()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("action %s failed: %s", action, err)
        return
    try:
        await ws.send(json.dumps({"command": action}))
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("action %s failed: %s", action, err)


def als_path():
    """The calibrated lux file, or None when biscuit-als is not publishing."""
    return ALS_LUX if os.path.exists(ALS_LUX) else None


def read_lux():
    return ring_config.lux()


async def watch_ambient_light(ws):
    """Publish illuminance to Home Assistant when it changes.

    Polled rather than pushed because the driver offers no event interface, and
    at 30 s because this is a room's light level, not a control input -
    biscuit-als polls it every second for the ring, and duplicating that rate
    here would be a lot of websocket traffic to tell HA the same number.

    Only genuine changes are sent. A sensor that repeats an unchanged value
    still writes a row to the recorder database every time.
    """
    last = None
    while True:
        lux = read_lux()
        if lux is not None and (
                last is None
                or abs(lux - last) >= max(ALS_MIN_DELTA, last * ALS_MIN_RATIO)):
            last = lux
            try:
                await ws.send(json.dumps({
                    "command": "sensor_state",
                    "data": {"object_id": "ambient_light", "value": lux},
                }))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("could not publish illuminance: %s", err)
                return
        await asyncio.sleep(ALS_POLL_S)


MIC_PROFILE_POLL_S = 30.0


async def watch_mic_profile(ws):
    """Publish whether the running mic chain is the one that was selected.

    Polled rather than pushed because the file is written by a shell branch with
    no event interface, and slowly because it only changes when that branch
    restarts - a few times a day at most. Nothing is sent until the branch has
    published at least once, so the sensor reads "unknown" before the first
    start rather than claiming agreement nobody has checked.
    """
    last = None
    while True:
        degraded, why = mic_profile_degraded()
        value = 1 if degraded else 0
        if mic_profile_status() is not None and value != last:
            if degraded:
                _LOGGER.warning("microphone tuning fallback: %s", why)
            elif last is not None:
                _LOGGER.info("microphone tuning is running as selected again")
            try:
                await ws.send(json.dumps({
                    "command": "sensor_state",
                    "data": {"object_id": "mic_profile_degraded",
                             "value": value},
                }))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("could not publish mic tuning state: %s", err)
                return
            last = value
        await asyncio.sleep(MIC_PROFILE_POLL_S)


def load_mic_mode():
    """Return the matching preset, or say plainly that HA made it custom."""
    env = _read_env_file(MIC_ENV)
    flag = env.get("MIC_MODE", "")
    if flag != "-compose":
        return MIC_BY_FLAG.get(flag, DEFAULT_MIC_LABEL)

    settings = load_mic_settings()
    for label, preset in MIC_PRESET_SETTINGS.items():
        if preset["mic_source"] == "Single microphone" and settings["mic_capsule"] != MIC_CAPSULES[6]:
            continue
        if all(settings[key] == value for key, value in preset.items()):
            return label
    return CUSTOM_MIC_LABEL


def apply_mic_mode(label):
    """Translate an 8080 preset into the same configuration Home Assistant uses.

    Restarting biscuit-mic-pump also restarts the assistant, because OpenRC
    knows the assistant depends on it - roughly ten seconds during which the
    device cannot hear. That is the cost of the setting, and it is why this is a
    dropdown someone chooses deliberately rather than anything automatic.

    Runs detached: the restart tears down the very websocket this agent is
    talking to, so waiting for it here would mean waiting for our own
    disconnection.
    """
    if label == CUSTOM_MIC_LABEL:
        # "Custom" describes an existing HA configuration.  It is not a
        # preset and must not rewrite it just because the settings page opened.
        return False
    preset = MIC_PRESET_SETTINGS.get(label)
    if preset is None:
        _LOGGER.warning("unknown microphone mode %r; ignoring", label)
        return False
    settings = load_mic_settings()
    settings.update(preset)
    # Presets predate the generation control. Translate their Stock/pmOS
    # vocabulary so a legacy caller cannot silently select nothing.
    settings["mic_profile"] = ("pmOS tuning"
                              if preset.get("mic_tuning") == "pmOS tuning"
                              else "Fire OS 5 tuning")
    if settings["mic_source"] == "Single microphone":
        settings["mic_capsule"] = MIC_CAPSULES[6]
    _LOGGER.info("microphone preset -> %s; restarting the capture pipeline", label)
    return save_mic_settings(settings)


def load_duck():
    cfg = dict(DUCK_DEFAULTS)
    try:
        with open(DUCK_FILE) as f:
            user = json.load(f)
        if isinstance(user, dict):
            if "enabled" in user:
                cfg["enabled"] = bool(user["enabled"])
            if "level" in user:
                cfg["level"] = max(0, min(100, int(user["level"])))
    except FileNotFoundError:
        pass
    except Exception as err:  # noqa: BLE001 - a bad file must not stop the pipeline
        _LOGGER.warning("ignoring %s: %s", DUCK_FILE, err)
    return cfg


def save_duck(cfg):
    os.makedirs(os.path.dirname(DUCK_FILE), exist_ok=True)
    tmp = DUCK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    _durable_replace(tmp, DUCK_FILE)


def duck_mtime():
    try:
        return os.stat(DUCK_FILE).st_mtime
    except OSError:
        return None


class Ducker:
    """Lowers every playback stream except the assistant's own, and puts it back.

    Volumes are saved per stream at duck time and restored from that snapshot, so
    a stream that starts DURING a conversation - the assistant's reply, most
    obviously - is never touched. A stream that disappears before the restore is
    simply skipped.
    """

    # The snapshot is mirrored here so an agent that dies mid-conversation does
    # not leave the music quiet forever with no record of how loud it was. /run
    # is the right scope: across a reboot the streams themselves are gone, so
    # there is nothing to restore and a stale file would be actively wrong.
    SNAPSHOT = "/run/biscuit-va-leds/ducked.json"

    # A duck is lifted by tts_finished, pipeline_error, idle or disconnected.
    # A run that ends any OTHER way leaves the music ducked with nothing coming
    # to raise it, and that is not rare: one sample showed four RUN_STARTs
    # against a single TTS_START. This bounds the damage to a timeout instead of
    # forever. It is a backstop, not the mechanism - a duck that hits it is a
    # bug worth the warning it logs.
    MAX_HOLD_S = 30.0

    def __init__(self):
        self.saved = {}          # sink-input id -> volume percent to restore
        self.cfg = load_duck()
        self._mtime = duck_mtime()
        self._ducked_at = 0.0
        self._recover()

    def expired(self, now=None):
        """True when a duck has outlived any plausible interaction."""
        if not self.saved or not self._ducked_at:
            return False
        return (now or time.monotonic()) - self._ducked_at > self.MAX_HOLD_S

    def _persist(self):
        try:
            os.makedirs(os.path.dirname(self.SNAPSHOT), exist_ok=True)
            if self.saved:
                with open(self.SNAPSHOT, "w") as f:
                    json.dump(self.saved, f)
            elif os.path.exists(self.SNAPSHOT):
                os.unlink(self.SNAPSHOT)
        except OSError as err:
            _LOGGER.warning("could not record the duck snapshot: %s", err)

    def _recover(self):
        """Undo a duck left behind by a previous run of this agent."""
        try:
            with open(self.SNAPSHOT) as f:
                self.saved = {str(k): int(v) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return
        if self.saved:
            _LOGGER.warning("found %d stream(s) left ducked by a previous run; "
                            "restoring", len(self.saved))
            self.restore()

    def refresh(self):
        mt = duck_mtime()
        if mt != self._mtime:
            self._mtime = mt
            self.cfg = load_duck()
            _LOGGER.info("ducking config reloaded: %s", self.cfg)

    def set_level(self, level=None, enabled=None):
        # Re-read first. self.cfg is only refreshed inside duck(), so writing it
        # back as it stood put back whatever "enabled" was when the last wake
        # word happened - turning ducking on again behind the settings page,
        # which had switched it off in the meantime.
        self.cfg = load_duck()
        if level is not None:
            self.cfg["level"] = max(0, min(100, int(level)))
        if enabled is not None:
            self.cfg["enabled"] = bool(enabled)
        save_duck(self.cfg)
        self._mtime = duck_mtime()

    _pids = (0.0, frozenset())

    # Players that are the device talking to the user, not content. These must
    # never be ducked, and unlike the assistant they are separate short-lived
    # processes, so the PID list below cannot catch them.
    #
    # This became load-bearing when the ALSA default moved from dmix to
    # PipeWire: earcons used to bypass PipeWire entirely and were invisible
    # here, and are now ordinary sink-inputs. Worse than being ducked once,
    # module-stream-restore persists a stream's volume against its application
    # name, so a single duck that caught an earcon left EVERY later earcon at
    # the ducked level - permanently, and with nothing in any log to say so.
    #
    # Matched against application.name, NOT the process binary: streams that
    # arrive through the PipeWire ALSA plugin carry no application.process.*
    # properties at all, only application.name ("PipeWire ALSA [sndfile-play]")
    # and node.name ("alsa_playback.sndfile-play"). The assistant's own player
    # is a pulse client and does set them, which is why the PID list below
    # still works for it.
    FEEDBACK_PLAYERS = ("sndfile-play",)

    @classmethod
    def assistant_pids(cls):
        """PIDs whose streams must never be ducked.

        Cached for a minute. This sits on the critical path between the wake
        word and the duck, and the answer only changes when the assistant
        restarts - at which point it reconnects and we re-register anyway.
        """
        now = time.monotonic()
        if now - cls._pids[0] < 60.0:
            return cls._pids[1]
        pids = frozenset()
        try:
            out = subprocess.run(["pgrep", "-f", "linux_voice_assistant"],
                                 capture_output=True, text=True, timeout=5).stdout
            pids = frozenset(x.strip() for x in out.split() if x.strip())
        except Exception:  # noqa: BLE001
            pass
        cls._pids = (now, pids)
        return pids

    def streams(self):
        """[(id, volume_percent, pid, names)] for every playback stream."""
        try:
            out = pactl("list", "sink-inputs").stdout
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("cannot list sink-inputs: %s", err)
            return []
        found, sid, vol, pid, app = [], None, None, None, ""
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Sink Input #"):
                if sid is not None:
                    found.append((sid, vol, pid, app))
                sid, vol, pid, app = line.split("#", 1)[1].strip(), None, None, ""
            elif line.startswith("Volume:") and vol is None:
                for tok in line.replace("/", " ").split():
                    if tok.endswith("%"):
                        try:
                            vol = int(tok[:-1])
                        except ValueError:
                            pass
                        break
            elif line.startswith("application.process.id"):
                pid = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("application.name") or line.startswith("node.name"):
                app += " " + line.split("=", 1)[1].strip().strip('"')
        if sid is not None:
            found.append((sid, vol, pid, app))
        return found

    def duck(self):
        self.refresh()
        if not self.cfg["enabled"] or self.saved:
            return                      # disabled, or already ducked
        skip = self.assistant_pids()
        level = self.cfg["level"]
        for sid, vol, pid, app in self.streams():
            if pid in skip or vol is None:
                continue
            if any(name in app for name in self.FEEDBACK_PLAYERS):
                continue
            if vol <= level:
                # Already at or below the ducked level. Either this is a duck of
                # ours that was never lifted, or the stream is deliberately
                # quiet; ducking again would record the ducked value AS the
                # original and make the damage permanent. This is not
                # theoretical - it compounded to 0.2 ** 6 of the original before
                # anyone noticed, because WirePlumber remembers a stream's
                # volume by application name and hands it back to the next
                # stream of the same name. See 52-biscuit-no-stream-restore.conf.
                continue
            target = max(0, int(vol * level / 100))
            if target >= vol:
                continue                # nothing to gain
            if pactl("set-sink-input-volume", sid, "%d%%" % target).returncode == 0:
                self.saved[sid] = vol
        if self.saved:
            self._ducked_at = time.monotonic()
            self._persist()
            _LOGGER.info("ducked %d stream(s) to %d%% of their volume",
                         len(self.saved), level)

    def restore(self):
        if not self.saved:
            return
        # No liveness check: setting the volume of a stream that has already
        # gone just fails harmlessly, and enumerating to find out cost more than
        # the failed calls do.
        for sid, vol in self.saved.items():
            pactl("set-sink-input-volume", sid, "%d%%" % vol)
        _LOGGER.info("restored %d stream(s)", len(self.saved))
        self.saved.clear()
        self._ducked_at = 0.0
        self._persist()


def map_mtime():
    try:
        return os.stat(LED_MAP_FILE).st_mtime
    except OSError:
        return None


def sound_mtime():
    try:
        return os.stat(SOUND_MAP_FILE).st_mtime
    except OSError:
        return None


def resolve_spec(spec, as_name=None, loop=None):
    """A spec names either a STOCK animation or a GENERATED effect.

        {"animation": "alexa_thinking"}                  stock, as Amazon ships it
        {"effect": "Arc Spin", "colour": [0, 128, 255]}   generated, any colour

    `effect` wins when both are present, and the colour defaults to white rather
    than being required, so naming an effect alone is a complete answer. A bad
    spec returns None and leaves the ring on whatever it was already showing - a
    typo in the map must not blank the ring mid-conversation.

    Module level, not a method: the settings UI resolves the same spec shape to
    drive its preview button, and it has no agent instance to borrow.

    `as_name` renames the generated file. fx.generate names its output after the
    EFFECT, so two things showing the same effect in different colours would
    write one filename - and biscuit-ring keys its active animations by name, so
    stopping either would stop both. Callers that can overlap (the mute latch
    against a Home Assistant effect, say) pass a name of their own.

    `loop` makes a one-shot stock file repeat (see stage_stock). Left as None
    it follows the name: an act_<activity> copy repeats when the activity is
    held, so a chosen one-shot does not go dark part way through thinking.
    """
    if spec.get("off"):
        # "Off" for this activity. A dark one-shot rather than no animation at
        # all, so the activity still resolves to a name the ring can play and
        # retire; see fx.generate_blank.
        try:
            return fx.generate_blank(name=as_name or "fx_off")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("blank animation failed: %s", err)
            return None

    effect = spec.get("effect")
    if effect:
        colour = spec.get("colour") or spec.get("color") or [255, 255, 255]
        try:
            anim = fx.generate(effect, tuple(int(c) for c in colour[:3]),
                               colours=spec.get("colours"))
            if as_name and as_name != anim:
                src = os.path.join(fx.FX_DIR, anim + ".animation")
                dst = os.path.join(fx.FX_DIR, as_name + ".animation")
                os.replace(src, dst)               # atomic, same directory
                return as_name
            return anim
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("effect %r failed: %s", effect, err)
            return None
    name = spec.get("animation")
    if not as_name or not name:
        return name
    # A stock animation plays under the caller's name, as a generated effect
    # does: the ring takes an animation's layer from its name, and Amazon's
    # names for most of these are not in the layer table - so under its own
    # name a chosen "Speaking" ring sank below music and timers.
    dst = os.path.join(fx.FX_DIR, as_name + ".animation")
    src = stock_source(name)
    if src is None:
        # The file is gone (removed on the settings page). What the page
        # published for the activity - its default, by now - is under the same
        # name in the persist store, which the ring reads after this one.
        try:
            os.unlink(dst)
        except OSError:
            pass
        _LOGGER.warning("stock animation %r is not installed", name)
        return as_name if os.path.exists(os.path.join("/opt/persist/led-fx", as_name + ".animation")) else None
    if loop is None:
        loop = as_name.startswith("act_") and held_activity(as_name[len("act_"):])
    try:
        os.makedirs(fx.FX_DIR, exist_ok=True)
        stage_stock(src, dst + ".tmp", loop)
        os.replace(dst + ".tmp", dst)
        return as_name
    except OSError as err:
        # A name the ring can still load: the activity's published copy, or
        # the file under its own name in the imported store - never a fos6_
        # name, which exists only here.
        _LOGGER.warning("could not stage %r: %s", name, err)
        if os.path.exists(os.path.join("/opt/persist/led-fx", as_name + ".animation")):
            return as_name
        return os.path.basename(src)[:-len(".animation")]


def load_map():
    """Stock defaults, with per-activity user overrides REPLACING them.

    Replacement, not a field-by-field merge. An override is a complete answer
    for that activity, so merging would leave the stock `animation` sitting
    beside the user's `effect` and make the outcome depend on which key
    resolve_spec happens to prefer. Replacing keeps the effective map identical
    to what the settings page shows.

    `lifetime` is the one exception and is carried over when the override does
    not state one: it encodes whether the activity is a MOMENT or a STATE, which
    is a property of the activity rather than of the animation chosen for it. A
    hand-written {"animation": "x"} for a one-shot must still retire itself.

    Only the activities named in the file are touched, so anything the user has
    not customised keeps following Amazon.
    """
    m = {k: dict(v) for k, v in DEFAULT_MAP.items()}
    try:
        with open(LED_MAP_FILE) as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("expected a JSON object of activities")
        for k, v in user.items():
            if not isinstance(v, dict):
                continue
            spec = normalise_spec(dict(v))
            lifetime = DEFAULT_MAP.get(k, {}).get("lifetime")
            if lifetime and "lifetime" not in spec:
                spec["lifetime"] = lifetime
            m[k] = spec
        _LOGGER.info("loaded %d activity overrides from %s", len(user), LED_MAP_FILE)
    except FileNotFoundError:
        pass
    except Exception as err:  # noqa: BLE001 - a bad file must not stop the ring
        _LOGGER.warning("ignoring %s: %s", LED_MAP_FILE, err)
    return m


def load_sound_map():
    """DEFAULT_SOUNDS with per-activity overrides, from the core module."""
    return _em.load_sound_map(_LOGGER)


def ring(line):
    """One command to the ring FIFO, never blocking."""
    try:
        fd = os.open(CONTROL_FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as err:
        if err.errno not in (errno.ENXIO, errno.ENOENT):
            _LOGGER.warning("ring FIFO open failed: %s", err)
        return
    try:
        os.write(fd, (line + "\n").encode())
        _LOGGER.debug("ring: %s", line)
    except OSError as err:
        if err.errno != errno.EPIPE:
            _LOGGER.warning("ring write failed: %s", err)
    finally:
        os.close(fd)


def audio_control(line):
    """One command to biscuit-audio. Never blocks, never owns the hardware.

    The FIFO has no reader when biscuit-audio is down, which is a normal
    condition rather than an error: O_NONBLOCK plus tolerating ENXIO and EPIPE
    is what stops a cosmetic service wedging on a missing one.
    """
    try:
        fd = os.open(AUDIO_MUTE_CONTROL, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as err:
        if err.errno not in (errno.ENXIO, errno.ENOENT):
            _LOGGER.warning("audio control open failed: %s", err)
        return False
    try:
        # bytes([10]) rather than an escape: a newline written as a literal in a
        # nested heredoc has been mangled here before.
        os.write(fd, line.encode() + bytes([10]))
        return True
    except OSError as err:
        if err.errno != errno.EPIPE:
            _LOGGER.warning("audio control write failed: %s", err)
        return False
    finally:
        os.close(fd)


def request_mute(muted):
    """Ask biscuit-audio to mute or unmute."""
    return audio_control("mute" if muted else "unmute")


def volume_level(pct):
    """Use biscuit-audio's integer-percent, round-to-even 30-step law."""
    return int(round(max(0, min(100, int(round(pct)))) * 30 / 100.0))


def volume_percent(level):
    return int(round(level * 100.0 / 30))


def read_device_volume():
    """Read a canonical owner percentage; missing/malformed state is unknown."""
    try:
        with open(AUDIO_VOLUME_STATE) as f:
            pct = int(f.read().strip())
        if 0 <= pct <= 100 and volume_percent(volume_level(pct)) == pct:
            return pct
    except (OSError, ValueError):
        pass
    return None


def request_volume(pct):
    """Ask biscuit-audio to move to a canonical level; delivery is not an ACK."""
    return audio_control("volume %d" % volume_percent(volume_level(pct)))


class VolumeReconciler:
    """Converge effective volume without mistaking our callback for a command.

    Existing APIs have no command IDs. Keep one report in flight, consume its
    matching callback, and reconnect for a fresh snapshot if its outcome is
    unknown. Hardware acknowledgement is the published effective level. No
    percentage deadband may suppress a real one-step button change.
    """

    ACK_SECONDS = 3.0
    SNAPSHOT_SECONDS = 10.0
    IO_SECONDS = 1.0

    def __init__(self, read=None, request=None, clock=None):
        self.read = read or read_device_volume
        self.request = request or request_volume
        self.clock = clock or time.monotonic
        self.connect()

    def connect(self):
        self.remote = None
        self.report = None
        self.hardware = None
        self.snapshot_deadline = self.clock() + self.SNAPSHOT_SECONDS

    @staticmethod
    def value(data):
        value = data.get("volume") if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value) or not 0 <= value <= 1:
            return None
        return float(value)

    @staticmethod
    def same(value, pct):
        # LVA persistence uses this tolerance too; it is far below one step.
        return value is not None and abs(value - pct / 100.0) < 0.0001

    def snapshot(self, data):
        # The physical owner wins initial reconciliation. A snapshot is not a
        # new volume command and must never move the hardware on reconnect.
        self.remote = self.value(data)

    def changed(self, data):
        value = self.value(data)
        if value is None:
            return
        self.remote = value
        if self.report is not None and self.same(value, self.report[0]):
            self.report = None
            return  # ACK may be late: never undo a newer physical button step.
        current = self.read()
        target = volume_percent(volume_level(value * 100.0))
        if current is None:
            self.hardware = None
            return  # Owner unavailable; do not issue or remember blind commands.
        self.hardware = None
        if current != target:
            if self.request(target):
                self.hardware = (target, current, self.clock() + self.ACK_SECONDS)
                _LOGGER.info("assistant volume -> device level %d", volume_level(target))
            else:
                _LOGGER.warning("volume request not delivered; retaining effective device level")

    async def disconnect(self, ws):
        try:
            await asyncio.wait_for(ws.close(), self.IO_SECONDS)
        except Exception:
            # A canceled close handshake alone may leave run()'s reader alive.
            # Both supported websockets client APIs expose their transport.
            transport = getattr(ws, "transport", None)
            if transport is not None:
                transport.abort()
        return False

    async def tick(self, ws):
        now = self.clock()
        if self.remote is None:
            if now >= self.snapshot_deadline:
                return await self.disconnect(ws)
            return True
        current = self.read()
        if current is None:
            self.hardware = None
            return True
        if self.hardware is not None:
            target, before, deadline = self.hardware
            if current == target:
                self.hardware = None  # Published owner state acknowledges it.
            elif current != before or now >= deadline:
                self.hardware = None  # New physical state or failed command wins.
            else:
                return True
        if self.report is not None:
            if now >= self.report[1]:
                # send() success is not execution proof. A new connection gives
                # an authoritative LVA snapshot without repeating a lost ACK.
                return await self.disconnect(ws)
            return True
        if self.same(self.remote, current):
            return True
        self.report = (current, now + self.ACK_SECONDS)
        try:
            await asyncio.wait_for(ws.send(json.dumps(
                {"command": "set_volume", "data": {"volume": current / 100.0}})),
                self.IO_SECONDS)
        except Exception:
            # Do not label a failed or ambiguous delivery acknowledged.
            return await self.disconnect(ws)
        _LOGGER.info("device volume %d%% -> assistant; awaiting callback", current)
        return True


def read_hw_muted():
    """The authoritative mute state, or None if biscuit-audio is not running."""
    try:
        with open(AUDIO_MUTE_STATE) as f:
            return f.read().strip() == "1"
    except OSError:
        return None


def read_headphones():
    """True/False for the headphone jack, or None when nobody is publishing it."""
    try:
        with open(AUDIO_HEADPHONES_STATE) as f:
            value = f.read().strip()
    except OSError:
        return None
    return {"1": True, "0": False}.get(value)


def mireds_to_rgb(mireds):
    """Approximate a colour temperature on RGB-only hardware.

    The ring has red, green and blue channels and no white one, so a CCT
    slider cannot drive a real white LED. This converts mireds -> Kelvin ->
    an RGB approximation, which is what WLED does on RGB-only strips.

    Tanner Helland's piecewise fit, normalised so the brightest channel is
    always full - the ring's own brightness control handles level, and letting
    the fit dim the output too would make warm temperatures dim as well as warm.
    """
    kelvin = 1000000.0 / max(1.0, float(mireds))
    t = max(1000.0, min(40000.0, kelvin)) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
    if t >= 66:
        b = 255.0
    elif t <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(t - 10) - 305.0447927307
    rgb = [max(0.0, min(255.0, c)) for c in (r, g, b)]
    peak = max(rgb) or 1.0
    return tuple(c * 255.0 / peak for c in rgb)


def ring_effects():
    """The Light Ring's effects in the order Home Assistant offers them.

    Solid first, because LVA takes the first effect as the default: turning the
    light on from HA without choosing one used to show "Volume Ramp", a static
    two-thirds arc that looked like a fault. Effects that only mean something
    as a volume display are left out, and the rest are sorted so a long list is
    searchable. Built from fx.EFFECTS on every call, so an effect added to
    biscuit-ring-fx reaches HA without a change here.
    """
    volume_only = fx.VOLUME_ONLY
    rest = sorted((name for name in fx.EFFECTS if name != "Solid" and name not in volume_only),
                  key=str.lower)
    return (["Solid"] if "Solid" in fx.EFFECTS else []) + rest


def _accent_for_role(name, index, accent):
    """An HA accent for colour role `index` of effect `name`.

    Some of the stock ambient looks keep a role faint on purpose - Twin
    Comets' glow, Pulse's background, Turbo Boost's pad, all at 1/15 - and an
    accent at its own full level there turned a glow into a second comet. So
    an accent landing on such a role is scaled by that role's own level; any
    other role takes it as it is.
    """
    own = fx.AMBIENT_COLOURS.get(name) or []
    if index < len(own):
        level = max(own[index]) / 255
        if 0 < level <= fx.FAINT + 1e-9:
            return [round(c * level) for c in accent]
    return list(accent)


HA_LIGHT_DEFAULTS = {"on": False, "rgb": (255, 255, 255), "effect": "Solid",
                     "mireds": 250.0, "mode": "rgb"}


def load_ha_light():
    """The HA Light Ring state as last set, validated field by field."""
    out = dict(HA_LIGHT_DEFAULTS)
    try:
        with open(HA_LIGHT_FILE) as f:
            saved = json.load(f)
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as err:
        _LOGGER.warning("ignoring %s: %s", HA_LIGHT_FILE, err)
        return out
    if not isinstance(saved, dict):
        return out
    if isinstance(saved.get("on"), bool):
        out["on"] = saved["on"]
    rgb = saved.get("rgb")
    if (isinstance(rgb, list) and len(rgb) == 3
            and all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in rgb)):
        out["rgb"] = tuple(rgb)
    # An effect that has since been renamed or removed falls back to Solid.
    if saved.get("effect") in fx.EFFECTS:
        out["effect"] = saved["effect"]
    mireds = saved.get("mireds")
    if isinstance(mireds, (int, float)) and not isinstance(mireds, bool) and 153 <= mireds <= 500:
        out["mireds"] = float(mireds)
    if saved.get("mode") in ("rgb", "color_temperature"):
        out["mode"] = saved["mode"]
    return out


def save_ha_light(ha):
    """Persist the HA Light Ring state. Skipped when nothing changed.

    HA sends a burst of commands while a colour wheel is dragged, and each is a
    durable write to flash, so an unchanged state costs only the read.
    """
    record = {"on": bool(ha["on"]), "rgb": [int(c) for c in ha["rgb"]],
              "effect": str(ha["effect"]), "mireds": float(ha["mireds"]),
              "mode": str(ha.get("mode", "rgb"))}
    try:
        with open(HA_LIGHT_FILE) as f:
            if json.load(f) == record:
                return
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(os.path.dirname(HA_LIGHT_FILE), exist_ok=True)
        tmp = HA_LIGHT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(record, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        _durable_replace(tmp, HA_LIGHT_FILE)
    except OSError as err:
        _LOGGER.warning("could not save %s: %s", HA_LIGHT_FILE, err)


# The direction light's two settings, as the settings page shows them. Its on /
# paused / off state is biscuit_services' app state machine - the same calls the
# page makes, so the two cannot disagree about what each label does.
DIRECTION_STATES = ["On", "Paused until restart", "Off"]
_DIRECTION_STATE_FLAG = {"On": "on", "Paused until restart": "paused", "Off": "off"}
DIRECTION_MODES = ["Always ready", "After the wake word"]
_DIRECTION_MODE_FLAG = {"Always ready": "always", "After the wake word": "wake"}


def direction_installed():
    return os.path.isfile(os.path.join("/etc/init.d", services.REGISTRY["direction"]["service"]))


def direction_state():
    if (services.PERSIST / "direction.disabled").exists():
        return "Off"
    return "Paused until restart" if (services.RUN / "direction.paused").exists() else "On"


def direction_mode():
    return {v: k for k, v in _DIRECTION_MODE_FLAG.items()}[services.direction_mode()]


def set_direction(action, value):
    """Run one direction-light change through biscuit_services. Blocking:
    it may start or stop the service, so callers run it off the event loop."""
    body = ({"id": "direction", "action": "state", "state": _DIRECTION_STATE_FLAG[value]}
            if action == "state" else
            {"id": "direction", "action": "mode", "mode": _DIRECTION_MODE_FLAG[value]})
    try:
        services.validate(body)
        _LOGGER.info("direction light: %s", services.perform(body))
    except Exception as err:  # noqa: BLE001 - a refusal must not stop the ring
        _LOGGER.warning("direction light %s %r refused: %s", action, value, err)


def apply_direction(action, value):
    """set_direction, only when it changes something - decided when the job
    runs, after any earlier one has finished, rather than when HA asked (see
    SerialJobs). HA re-sends every value on reconnect, and each would
    otherwise restart the service."""
    current = direction_state() if action == "state" else direction_mode()
    if value != current:
        set_direction(action, value)


class Ring:
    """Owns what is on the ring, and the activity/HA precedence."""

    def __init__(self):
        self.current = None          # the animation currently played
        self.muted = False
        self.activity = None         # non-None while the pipeline is active
        # HA light state, as last set - persisted, so a restart of this agent
        # keeps the ring as HA shows it. Off on a fresh device, so it is dark
        # when idle, which is how it behaved before.
        self.ha = dict(load_ha_light(), brightness=ring_config.load_brightness())
        # What Home Assistant is shown, kept in step with the files behind it.
        self.sync = EntitySync()
        self.map = load_map()
        # Stamped BEFORE loading, so a write in between is seen as a change
        # by the first refresh_map rather than taken as its baseline.
        self._sound_mtime = sound_mtime()
        self.sounds = load_sound_map()
        self._map_mtime = map_mtime()
        self.ducker = Ducker()
        self._duck_lock = asyncio.Lock()
        self._duck_queued = False
        self.volume = VolumeReconciler()
        # The HA commands that start or stop a service, in order.
        self.jobs = SerialJobs()

    # ---- low level ----
    def _play(self, animation, lifetime=None):
        if self.current and self.current != animation:
            ring("stop %s" % self.current)
        if lifetime:
            ring("play %s %d" % (animation, lifetime))
            self.current = None      # self-retiring
        else:
            ring("play %s" % animation)
            self.current = animation

    def _clear(self):
        if self.current:
            ring("stop %s" % self.current)
            self.current = None

    # ---- HA light ----
    def _ha_animation(self):
        """Generate the HA-selected effect at its colour, return its name."""
        name = self.ha["effect"] or "Solid"
        if name not in fx.EFFECTS:
            name = "Solid"
        # The colour is NOT pre-scaled by brightness any more. biscuit-ring
        # applies the ceiling, and doing it in both places multiplied the two -
        # which is what made the bottom of the slider emit nothing at all.
        base = (mireds_to_rgb(self.ha["mireds"])
                if self.ha.get("mode") == "color_temperature" else self.ha["rgb"])
        rgb = tuple(int(c) for c in base)
        try:
            accents = ring_config.accents()
            colours = None
            if accents is not None and fx.COLOUR_ROLES[name]:
                colours = fx.default_colours(name, rgb)
                # A fixed-colour effect keeps its own main colour, as it does
                # outside Custom accents; the accents fill the other roles.
                if fx.EFFECTS[name][1]:
                    colours[0] = list(rgb)
                for i in range(1, len(colours)):
                    colours[i] = _accent_for_role(name, i, accents[i - 1])
            return fx.generate(name, rgb, colours=colours)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("effect %s failed: %s", name, err)
            return None

    def push_brightness(self):
        # Settings and HA share the persisted level. Reconnects and unrelated
        # commands must not restore a stale in-memory brightness over 8080.
        self.ha["brightness"] = ring_config.load_brightness()

    def apply_ha(self):
        """Show the HA state. Only ever visible when no activity is running."""
        if self.activity is not None:
            return
        if self.muted:
            self._play(self.mute_animation())
            return
        if not self.ha["on"]:
            self._clear()
            return
        anim = self._ha_animation()
        if anim:
            self._play(anim)

    def light_command(self, data):
        """Apply the assistant's LightEntity.state_dict() payload.

        The REAL shape - confirmed against entity.py, not guessed - is:
            {"object_id", "state": bool, "brightness": float 0..1,
             "red": float 0..1, "green": float 0..1, "blue": float 0..1,
             "effect": str | None}
        brightness/red/green/blue are ESPHome's normalised 0..1 floats, not
        0..255 ints. An earlier version of this method assumed an "rgb" list
        and an integer brightness, which meant every real light_command from
        HA silently produced (255,255,255) at brightness 0 - the light would
        have looked broken despite every layer reporting success.
        """
        obj = str(data.get("object_id") or data.get("id") or "ring")
        if obj in ('ring_colour_2','ring_colour_3'):
            try:
                ring_config.save_accent(int(obj[-1])-2,data)
                self.apply_ha()
            except (ValueError,TypeError) as err:
                _LOGGER.warning('invalid accent command: %s',err)
            return
        if obj != "ring":
            # The mute indicator used to be a light here. It is a mode dropdown
            # now, and biscuit-audio is its only writer - so anything arriving
            # for another object_id is stale and must not touch the hardware.
            _LOGGER.debug("ignoring light_command for %r", obj)
            return
        if "state" in data:
            self.ha["on"] = bool(data["state"])
        if data.get("brightness") is not None:
            self.ha["brightness"] = round(max(0.0, min(1.0, float(data["brightness"]))) * 255)
            if data.get("brightness_changed", True):
                ring_config.save_brightness(self.ha["brightness"])
        if all(data.get(k) is not None for k in ("red", "green", "blue")):
            self.ha["rgb"] = tuple(
                round(max(0.0, min(1.0, float(data[k]))) * 255) for k in ("red", "green", "blue"))
        if data.get("color_temperature") is not None:
            self.ha["mireds"] = float(data["color_temperature"])
        # The entity tells us which control the user actually moved, so a stale
        # RGB triple does not override a temperature the user just set.
        if data.get("color_mode"):
            self.ha["mode"] = str(data["color_mode"])
        eff = data.get("effect")
        if eff:
            self.ha["effect"] = str(eff)
        _LOGGER.info("ring <- HA %s", self.ha)
        save_ha_light(self.ha)
        self.push_brightness()
        self.apply_ha()

    def light_state(self):
        """The Light Ring as it is, for LVA: the light_state command's data."""
        return {"object_id": "ring", "state": bool(self.ha["on"]),
                "brightness": ring_config.load_brightness() / 255,
                "red": self.ha["rgb"][0] / 255, "green": self.ha["rgb"][1] / 255,
                "blue": self.ha["rgb"][2] / 255, "effect": self.ha["effect"],
                "color_temperature": self.ha["mireds"],
                "color_mode": self.ha.get("mode", "rgb")}

    # ---- mute LED ----
    def set_muted(self, muted):
        if muted == self.muted:
            return
        self.muted = muted
        # Deliberately NOT writing the LED here: biscuit-audio owns it and sets
        # it as part of applying the mute. Writing it too would race, and the
        # loser would leave the indicator disagreeing with the microphones.
        if muted:
            self.activity = None
            self._play(self.mute_animation())
        else:
            self._clear()
            self.apply_ha()

    # ---- pipeline events ----
    def mute_animation(self):
        """The mute latch is customisable too, so it is a map lookup like the rest."""
        # The default mute is an effect, with no animation name to fall back
        # on; the last resort is the name the settings page publishes.
        return (resolve_spec(self.map.get("mute", DEFAULT_MAP["mute"]), as_name="act_mute")
                or resolve_spec(DEFAULT_MAP["mute"], as_name="act_mute") or "act_mute")

    def refresh_map(self):
        """Pick up edits to led-map.json without restarting the agent.

        Checked per event rather than polled on a timer: activities are the only
        consumer of the map, so one stat() per event costs nothing and the next
        wake word already reflects a change saved a second earlier.
        """
        mt = map_mtime()
        if mt != self._map_mtime:
            self._map_mtime = mt
            self.map = load_map()
            self.sounds = load_sound_map()
            _LOGGER.info("activity animation and sound maps reloaded")
        # The sounds have their own file, written without touching led-map.json
        # - by the settings page and by the Sounds select - so it is watched on
        # its own; otherwise a changed sound played the old one until restart.
        smt = sound_mtime()
        if smt != self._sound_mtime:
            self._sound_mtime = smt
            self.sounds = load_sound_map()
            _LOGGER.info("sound map reloaded")

    def event(self, name, data):
        self.refresh_map()
        if name == "snapshot":
            self.volume.snapshot(data)
            # biscuit-audio's state, not LVA's: LVA starts believing it is
            # unmuted, so after an LVA restart its snapshot would have taken
            # the mute ring off muted microphones. LVA itself is told a muted
            # state on connect (see run()).
            #
            # The other disagreement - LVA muted, microphones live - is a mute
            # HA sent while this agent was away, since LVA never starts muted.
            # Privacy wins: the microphones are muted, never LVA unmuted.
            hw = read_hw_muted()
            if data.get("muted") and hw is False:
                _LOGGER.info("assistant is muted but the microphones are live; muting them")
                request_mute(True)
                hw = True
            self.set_muted(bool(data.get("muted")) if hw is None else hw)
            self.apply_ha()
            return
        if name == "muted":
            want = bool(data.get("muted", True))
            # Relay to the owner. Only when it disagrees, so an echo of our own
            # relay does not bounce back and forth.
            if read_hw_muted() != want:
                request_mute(want)
            self.set_muted(want)
            return
        if name == "light_command":
            self.light_command(data or {})
            return
        if name == "switch_command":
            d = data or {}
            # Whatever happens below, HA is then told what was actually stored.
            self.sync.note_command(d.get("object_id"), bool(d.get("state")))
            if d.get("object_id") == "duck_enabled":
                on = bool(d.get("state"))
                self.ducker.set_level(enabled=on)
                _LOGGER.info("ducking -> %s from Home Assistant", "on" if on else "off")
                return
            if d.get("object_id") == "bt_pairing":
                # Queued, so an off then on cannot have the off's pkill land
                # on the window the on just opened.
                self.jobs.submit(start_pairing if bool(d.get("state")) else stop_pairing)
                return
            if d.get("object_id") == "btproxy_active":
                on = bool(d.get("state"))
                set_btproxy_active(on)
                _LOGGER.info("bluetooth proxy connections -> %s", "on" if on else "off")
                return
            if d.get("object_id") == "btproxy":
                on = bool(d.get("state"))
                set_btproxy_enabled(on)
                _LOGGER.info("bluetooth proxy -> %s", "on" if on else "off")
                return
            if d.get("object_id") in VIZ_SWITCHES:
                label, _icon, key = VIZ_SWITCHES[d["object_id"]]
                on = bool(d.get("state"))
                ring_config.save_viz({key: 1.0 if on else 0.0})
                _LOGGER.info("%s -> %s", label, "on" if on else "off")
                return
            if d.get("object_id") == "ring_autodim":
                on = bool(d.get("state"))
                save_autodim(on)
                _LOGGER.info("ring auto dim -> %s", "on" if on else "off")
                self.push_brightness()
                self.apply_ha()
            return
        if name == "number_command":
            d = data or {}
            self.sync.note_command(d.get("object_id"), d.get("value"))
            if d.get("object_id") in ("direction_state", "direction_mode"):
                want = str(d.get("value", ""))
                action = "state" if d["object_id"] == "direction_state" else "mode"
                if want in (DIRECTION_STATES if action == "state" else DIRECTION_MODES):
                    self.jobs.submit(apply_direction, action, want)
                return
            if d.get("object_id") == "ring_palette":
                want = str(d.get("value", ""))
                if want in ring_config.PALETTES:
                    ring_config.save_palette(want)
                    self.apply_ha()
                return
            if d.get("object_id") in VIZ_NUMBERS:
                label, _icon, key, to_value, _to_pct = VIZ_NUMBERS[d["object_id"]]
                pct = max(0.0, min(100.0, float(d.get("value", 0.0))))
                ring_config.save_viz({key: to_value(pct)})
                _LOGGER.info("%s -> %.0f%%", label, pct)
                return
            if d.get("object_id") == "duck_level":
                level = int(round(float(d.get("value", self.ducker.cfg["level"]))))
                self.ducker.set_level(level)
                _LOGGER.info("ducking level set to %d%% from Home Assistant", level)
            elif d.get("object_id") == "mic_led":
                want = str(d.get("value", ""))
                if want in MIC_LED_MODES and want != load_mic_led():
                    save_mic_led(want)
                    _LOGGER.info("mic light override -> %s", want)
            elif d.get("object_id") == "mute_led":
                want = str(d.get("value", ""))
                if want in MUTE_LED_MODES and want != load_mute_led():
                    save_mute_led(want)
                    _LOGGER.info("mute light -> %s", want)
            elif str(d.get("object_id", "")).startswith("button_"):
                gesture = str(d["object_id"])[len("button_"):]
                want = str(d.get("value", ""))
                if gesture in BUTTON_DEFAULTS and want in BUTTON_ACTIONS:
                    cfg = load_buttons()
                    if cfg.get(gesture) != want:
                        cfg[gesture] = want
                        save_buttons(cfg)
                        _LOGGER.info("action button: %s press -> %s",
                                     gesture, want)
            elif d.get("object_id") in ("mic_source", "mic_profile", "mic_vad",
                                        "mic_aec", "mic_beam", "mic_pga_gain",
                                        "mic_capsule", "call_mic_capsule", "call_processing", "call_profile"):
                # Any of these restarts capture, and the assistant with it, so act
                # ONLY on a real change - Home Assistant re-sends every value on
                # reconnect, and obeying that would restart the pipeline every time
                # HA blinked.
                key = str(d["object_id"])
                cur = load_mic_settings()
                if key == "mic_aec" and not mic_aec_available(cur):
                    _LOGGER.debug("ignoring AEC command: centre mic bypasses AEC")
                    return
                if key == "mic_pga_gain":
                    try:
                        want = _mic_pga_db(d.get("value", MIC_GAIN_DB_DEFAULT))
                    except (TypeError, ValueError):
                        return
                else:
                    want = str(d.get("value", ""))
                    valid = {"mic_source": MIC_SOURCES,
                             "mic_profile": MIC_PROFILES,
                             "mic_aec": ON_OFF, "mic_beam": ON_OFF,
                             "mic_vad": MIC_VADS,
                             "mic_capsule": MIC_CAPSULES, "call_mic_capsule": MIC_CAPSULES,
                             "call_processing": ON_OFF, "call_profile": call_profile.PROFILE_LABELS}[key]
                    if want not in valid:
                        return
                if key == "call_profile" and want == "Stock-derived tuning" and call_profile.stock_info() is None:
                    return
                if key == "mic_profile":
                    if mic_profile_unavailable(want):
                        _LOGGER.info("refusing profile %s: %s", want,
                                     mic_profile_unavailable(want))
                        return
                    if (want in MIC_PROFILE_ARRAY_ONLY
                            and cur["mic_source"] == "Single microphone"):
                        _LOGGER.info("refusing profile %s: no single-microphone path",
                                     want)
                        return
                if key == "mic_vad" and mic_vad_unavailable(want):
                    _LOGGER.info("refusing detector %s: %s", want,
                                 mic_vad_unavailable(want))
                    return
                # The other half of the check above, and the one a default
                # device meets: 8-beam is the default profile. Refused as the
                # settings page refuses it; HA is then told the stored value.
                if (key == "mic_source" and want == "Single microphone"
                        and cur["mic_profile"] in MIC_PROFILE_ARRAY_ONLY):
                    _LOGGER.info("refusing a single microphone: %s has no "
                                 "single-microphone path; choose pmOS tuning first",
                                 cur["mic_profile"])
                    return
                if cur[key] == want:
                    _LOGGER.debug("%s unchanged (%s)", key, want)
                    return
                cur[key] = want
                # The beamformer has nothing to steer with one microphone, so
                # choosing "Single microphone" turns it off rather than leaving a control
                # that reads On and does nothing.
                if cur["mic_source"] == "Single microphone":
                    cur["mic_beam"] = "Off"
                try:
                    save_mic_settings(cur)
                except ValueError as err:
                    _LOGGER.warning("refusing %s = %s: %s", key, want, err)
                    return
                _LOGGER.info("mic chain: source=%s profile=%s vad=%s aec=%s "
                             "adaptive=%s pga=%.1f dB",
                             cur["mic_source"], cur["mic_profile"], cur["mic_vad"],
                             cur["mic_aec"], cur["mic_beam"], cur["mic_pga_gain"])
            elif d.get("object_id") == "miccal":
                want = str(d.get("value", ""))
                if want in MICCAL_MODES and want != load_miccal_mode():
                    save_miccal_mode(want)
                    _LOGGER.info("mic calibration -> %s", want)
            elif d.get("object_id") in ("eq_mode", "eq_bass", "eq_mid", "eq_treble"):
                key = str(d["object_id"])
                cur = load_eq()
                if key == "eq_mode":
                    want = str(d.get("value", ""))
                    if want not in EQ_MODES or cur[key] == want:
                        return
                    cur[key] = want
                else:
                    try:
                        want = max(EQ_DB_MIN, min(EQ_DB_MAX, float(d.get("value", 0))))
                    except (TypeError, ValueError):
                        return
                    if abs(cur[key] - want) < 0.01:
                        return
                    cur[key] = want
                    # Moving a slider IS choosing a custom curve. Leaving the mode on
                    # Stock while the bands are non-zero would show a setting that is
                    # not what is playing.
                    cur["eq_mode"] = "Custom"
                save_eq(cur)
                _LOGGER.info("speaker EQ: %s (bass %+.1f mid %+.1f treble %+.1f)",
                             cur["eq_mode"], cur["eq_bass"], cur["eq_mid"],
                             cur["eq_treble"])
            elif d.get("object_id") == "earcon_set":
                want = str(d.get("value", ""))
                # Against what HA shows, not the stored name: after a sound was
                # chosen on its own the select reads Custom while earcon-set
                # still names the set, and picking that set must restore it.
                if want in EARCON_SETS and want != current_earcon_set():
                    save_earcon_set(want)
                    _LOGGER.info("earcon set -> %s", want)
            return
        if name == "volume_changed":
            self.volume.changed(data)
            return
        if name == "volume_muted":
            # Speaker mute, not microphone mute. The ring must not claim the mic
            # is off, so this is deliberately ignored.
            return
        if name in IDLE_EVENTS:
            self.activity = None
            self._duck_async(self.ducker.restore)
            if self.muted:
                self._play(self.mute_animation())
            else:
                self._clear()
                self.apply_ha()
            return
        if name not in PIPELINE_ACTIVITIES:
            return
        spec = self.map.get(name)
        if not spec or self.muted:
            return
        self.activity = name
        anim = resolve_spec(spec, as_name="act_" + name)
        if anim:
            self._play(anim, spec.get("lifetime"))

        # The earcon for activities the assistant has no sound of its own for.
        # Restricted to AGENT_SOUNDS so a user who points, say, wake_word_detected
        # at a different file gets it once from the assistant rather than twice
        # from both of us.
        if name in AGENT_SOUNDS:
            play_sound(sound_path(self.sounds.get(name, {}).get("sound")))

        # Duck AFTER the ring, and off the event loop.
        #
        # Ducking spawns pactl once per stream plus one to enumerate, which
        # measured ~135 ms. Doing that inline delayed the wake-word animation by
        # the same amount - the one piece of feedback whose whole job is to be
        # immediate - and stalled the mute relay with it.
        #
        # tts_finished rather than tts_speaking: the reply is part of the
        # interaction, and raising the music underneath it would talk over the
        # answer the user just asked for.
        if name == "wake_word_detected":
            self._duck_async(self.ducker.duck)
        elif name in ("tts_finished", "pipeline_error"):
            self._duck_async(self.ducker.restore)

    def _duck_async(self, action):
        """Run a duck/restore in a worker thread, preserving their order.

        Fire-and-forget would let a restore overtake the duck it belongs to on a
        very short interaction, leaving the music quiet for good; the lock makes
        them a queue. Falls back to running inline when there is no event loop,
        which is how the unit tests drive it.
        """
        # Whether the last one queued was a duck: its worker fills
        # ducker.saved only as it goes, so saved alone misses one in flight.
        self._duck_queued = action == self.ducker.duck
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            action()
            return

        async def _run():
            async with self._duck_lock:
                try:
                    await asyncio.to_thread(action)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("ducking failed: %s", err)

        loop.create_task(_run())


# Home Assistant's registered numbers carry no step, so a slider defaults
# to whole units and a 0-1 range would offer two positions. Every
# visualiser control is therefore a percentage here and a fraction in the
# file. Contrast is the one that is not a fraction at all: it maps onto
# the output curve's exponent, 1.0 to 3.0, where 1 is linear.
VIZ_NUMBERS = {
    'viz_contrast': ('Visualiser contrast', 'mdi:contrast-circle', 'gamma',
                     lambda pct: 1.0 + 2.0 * pct / 100.0,
                     lambda val: (val - 1.0) / 2.0 * 100.0),
    'viz_punch': ('Visualiser beat punch', 'mdi:pulse', 'punch',
                  lambda pct: pct / 100.0, lambda val: val * 100.0),
    'viz_motion': ('Visualiser sweep', 'mdi:rotate-right', 'motion',
                   lambda pct: pct / 100.0, lambda val: val * 100.0),
    'viz_steady': ('Visualiser steadiness', 'mdi:waves', 'relative',
                   lambda pct: pct / 100.0, lambda val: val * 100.0),
    'viz_fade_top': ('Visualiser top fade', 'mdi:gradient-horizontal',
                     'fade_top',
                     lambda pct: pct / 100.0, lambda val: val * 100.0),
    'viz_fade_bottom': ('Visualiser bottom fade', 'mdi:gradient-horizontal',
                        'fade_bottom',
                        lambda pct: pct / 100.0, lambda val: val * 100.0),
}
VIZ_SWITCHES = {
    'viz_enabled': ('Music visualiser', 'mdi:chart-bar', 'enabled'),
    'viz_balance': ('Visualiser even colours', 'mdi:scale-balance', 'balance'),
}


def viz_number_value(object_id):
    _name, _icon, key, _to_value, to_pct = VIZ_NUMBERS[object_id]
    return max(0.0, min(100.0, to_pct(ring_config.load_viz()[key])))


def _select(name, object_id, icon, options, value, **meta):
    return {"command": "register_number", "data": dict(
        name=name, object_id=object_id, icon=icon, options=list(options),
        initial_value=value, **meta)}


def _number(name, object_id, icon, lo, hi, value, **meta):
    return {"command": "register_number", "data": dict(
        name=name, object_id=object_id, icon=icon, min_value=float(lo),
        max_value=float(hi), initial_value=float(value), **meta)}


def _switch(name, object_id, icon, value, **meta):
    return {"command": "register_switch", "data": dict(
        name=name, object_id=object_id, icon=icon, initial_value=bool(value), **meta)}


def build_registrations():
    """Everything this agent shows in Home Assistant, read from the files NOW.

    Built afresh on every connect and every time a file behind it changes
    (watch_settings), never once at import. It used to be a module-level list,
    so the values - and the option lists - were whatever this process read when
    it started, and every LVA restart handed Home Assistant those again: a
    Sounds or Calibration change from HA snapped back as soon as the restart it
    caused was over.

    Names follow the settings page's labels, so the two read as one device.
    object_ids never change: Home Assistant's unique_id is built from them, and
    renaming one would orphan every automation that uses it. Values are the
    registration's initial_value; LVA pushes a changed one to HA as a state.
    """
    mic = load_mic_settings()
    eq = load_eq()
    duck = load_duck()
    viz = ring_config.load_viz()
    buttons = load_buttons()
    stock_call = call_profile.stock_info() is not None

    call_profiles = list(call_profile.PROFILE_LABELS)
    call_value = mic["call_profile"]
    if not stock_call:
        # ESPHome selects have no per-entity availability field. HA accepts
        # the reserved state 'unavailable' when it is a listed option, and
        # disables the control; LVA rejects commands for such a select.
        call_profiles = ["Open-source defaults"]
        if call_value != "Open-source defaults":
            call_profiles, call_value = ["unavailable"], "unavailable"
    aec_options, aec_value = ON_OFF, mic["mic_aec"]
    if not mic_aec_available(mic):
        aec_options, aec_value = ["unavailable"], "unavailable"

    regs = [
        {"command": "register_light", "data": {
            "name": "Ring colour 2", "object_id": "ring_colour_2", "icon": "mdi:palette",
            "supports_rgb": True, "supports_brightness": True, "effects": []}},
        {"command": "register_light", "data": {
            "name": "Ring colour 3", "object_id": "ring_colour_3", "icon": "mdi:palette",
            "supports_rgb": True, "supports_brightness": True, "effects": []}},
        _select("Ring effect colours", "ring_palette", "mdi:palette",
                ring_config.PALETTES, ring_config.load_palette()["preset"]),
        # The one light people actually use, so it is a primary entity rather
        # than a configuration one: shown on default dashboards and offered to
        # Assist.
        {"command": "register_light", "data": {
            "name": "Light ring", "object_id": "ring", "icon": "mdi:circle-outline",
            "effects": ring_effects(),
            "supports_rgb": True, "supports_brightness": True,
            # The ring has no white channel; colour temperature is SYNTHESISED
            # as an RGB approximation (see mireds_to_rgb). Declaring it as a
            # second colour mode is what gives HA a temperature slider
            # alongside the wheel.
            "supports_color_temperature": True,
            "min_mireds": 153.0,    # ~6500K, cool
            "max_mireds": 500.0,    # ~2000K, warm
            "entity_category": "none"}},
        _switch("Dim ring with the room", "ring_autodim", "mdi:brightness-auto", load_autodim()),
        # The mute indicator is NOT a light entity. It has two binary channels
        # and therefore three states, and it is driven by the mute state rather
        # than by Home Assistant - so what the user chooses is how bright it is
        # when it comes on. A dropdown says that; an on/off light with a 0-255
        # slider said something false twice over.
        _select("Mute light while muted", "mute_led", "mdi:microphone-off",
                MUTE_LED_MODES, load_mute_led()),
        _select("Mute light when not muted", "mic_led", "mdi:led-on",
                MIC_LED_MODES, load_mic_led()),
        # Not discoverable unless this is on. It turns itself back off when the
        # window closes, which is why it is a switch rather than a button: the
        # state is meaningful and worth showing.
        _switch("Bluetooth pairing mode", "bt_pairing", "mdi:bluetooth-connect", pairing_open()),
        # Scanning costs roughly half the 2.4 GHz Wi-Fi throughput on this
        # hardware (measured: downloads 0.38 s -> 0.77 s), so this is off by
        # default and worth being able to turn off from the sofa.
        _switch("Bluetooth proxy", "btproxy", "mdi:bluetooth-audio", btproxy_enabled()),
        # Separate from the proxy switch because it is a separate trade:
        # broadcast sensors need only the proxy, while letting Home Assistant
        # connect OUT pauses scanning for as long as a connection is open.
        _switch("Let Home Assistant connect to devices", "btproxy_active",
                "mdi:bluetooth-connect", btproxy_active()),
        # Both halves of ducking. LVA also ducks its own music player by these,
        # from the states pushed to it.
        _switch("Turn other audio down while you speak", "duck_enabled", "mdi:volume-low",
                duck["enabled"]),
        _number("Other audio turned down to", "duck_level", "mdi:volume-low", 0, 100,
                duck["level"], step=5.0, unit_of_measurement="%", mode="slider"),
    ]
    # The music visualiser. Off hands the ring back to the static music
    # animation, so it still shows that something is playing.
    regs += [_switch(label, object_id, icon, viz[key] > 0.0)
             for object_id, (label, icon, key) in VIZ_SWITCHES.items()]
    regs += [_number(label, object_id, icon, 0, 100, viz_number_value(object_id),
                     step=5.0, unit_of_measurement="%", mode="slider")
             for object_id, (label, icon, _key, _to_value, _to_pct) in VIZ_NUMBERS.items()]
    # One control per decision. See the settings model above for why tuning is
    # a single control.
    regs += [
        _select("Microphones used", "mic_source", "mdi:microphone-variant",
                MIC_SOURCES, mic["mic_source"]),
        _select("Single microphone", "mic_capsule", "mdi:microphone",
                MIC_CAPSULES, mic["mic_capsule"]),
        # Offered with whatever this device can actually run, and always the
        # current choice, so HA never holds a value outside its own list.
        _select("Microphone processing", "mic_profile", "mdi:tune-variant",
                mic_profile_options(), mic["mic_profile"]),
        # Fire OS 6 only; see mic_vad_options.
        _select("Voice detector", "mic_vad", "mdi:account-voice",
                mic_vad_options(), mic["mic_vad"]),
        _select("Echo cancellation", "mic_aec", "mdi:ear-hearing", aec_options, aec_value),
        # The adaptive cleanup after the fixed beam stage. Meaningless for one
        # microphone and forced off for it.
        _select("Adaptive beamforming", "mic_beam", "mdi:signal-distance-variant",
                ON_OFF, mic["mic_beam"]),
        # The codec's PGA moves in 0.5 dB steps, and so does this.
        _number("Microphone input gain", "mic_pga_gain", "mdi:microphone",
                MIC_GAIN_DB_MIN, MIC_GAIN_DB_MAX, mic["mic_pga_gain"],
                step=0.5, unit_of_measurement="dB", mode="slider"),
        # Per-capsule factory calibration. The capsules differ by about 2.9 dB
        # on the units measured, which matters for the array and not at all for
        # one mic.
        _select("Microphone calibration", "miccal", "mdi:tune", MICCAL_MODES, load_miccal_mode()),
        _select("Call processing", "call_processing", "mdi:ear-hearing",
                ON_OFF, mic["call_processing"]),
        _select("Call microphone", "call_mic_capsule", "mdi:phone",
                MIC_CAPSULES, mic["call_mic_capsule"]),
        _select("Call processing profile", "call_profile", "mdi:tune", call_profiles, call_value),
        # Playback. There is deliberately NO control for the compressor/limiter:
        # it is what protects the driver from the correction curve's +24 dB of
        # bass, so an "off" here would be a way to damage the speaker from HA.
        _select("Equaliser", "eq_mode", "mdi:equalizer", EQ_MODES, eq["eq_mode"]),
    ]
    regs += [_number("Equaliser " + name, "eq_" + name, "mdi:equalizer",
                     EQ_DB_MIN, EQ_DB_MAX, eq["eq_" + name],
                     step=0.5, unit_of_measurement="dB", mode="slider")
             for name, _fc, _q in EQ_BANDS]
    regs.append(_select("Sounds", "earcon_set", "mdi:music-note",
                        EARCON_SETS + [EARCON_CUSTOM], current_earcon_set()))
    if direction_installed():
        regs += [
            _select("Direction light", "direction_state", "mdi:compass-outline",
                    DIRECTION_STATES, direction_state()),
            _select("Direction light start", "direction_mode", "mdi:timer-play-outline",
                    DIRECTION_MODES, direction_mode()),
        ]
    regs += [
        # Read-only, so a Sensor rather than a Number: a Number would render as
        # a slider the user could drag, and dragging it would do nothing.
        {"command": "register_sensor", "data": {
            "name": "Room light", "object_id": "ambient_light",
            "unit_of_measurement": "lx", "device_class": "illuminance",
            "accuracy_decimals": 1, "icon": "mdi:brightness-5",
            "state_class": "measurement"}},
        # 1 when the chain that is actually running is not the one selected,
        # which happens when a stock generation's assets are absent at start
        # and the branch falls back to pmOS rather than leaving the device
        # deaf. The reason in words is on the settings page, which has room for
        # it. Only published once observed, so it reads "unknown" rather than
        # "fine" before the microphone branch has started.
        {"command": "register_sensor", "data": {
            "name": "Microphone processing fallback", "object_id": "mic_profile_degraded",
            "accuracy_decimals": 0, "icon": "mdi:microphone-question",
            "entity_category": "diagnostic"}},
        # Published by biscuit-audio; unknown until it does.
        {"command": "register_binary_sensor", "data": {
            "name": "Headphones", "object_id": "headphones",
            "device_class": "plug", "icon": "mdi:headphones"}},
        # Gives HA an event entity that fires single/double/triple/long, so an
        # automation can react to any gesture even when its local action is
        # "Nothing".
        {"command": "register_button", "data": {}},
    ]
    regs += [_select("Action button " + label.lower(), "button_" + gesture,
                     "mdi:gesture-tap-button", BUTTON_ACTION_LABELS, buttons[gesture])
             for gesture, label, _default in BUTTON_GESTURES]
    return regs


DIRECTION_DEMAND_PATH = "/run/biscuit-ring/direction-active"


def refresh_wake_direction(state):
    """Warm the existing lossy observer before wake; never activate a ring layer."""
    if (not services.allowed('direction') or services.direction_mode() != 'always'
            or state.muted or read_hw_muted() is not False):
        return
    try:
        with open(DIRECTION_DEMAND_PATH, "w") as demand:
            demand.write(str(time.monotonic()))
    except OSError:
        pass  # Optional LEDs must not interrupt assistant events.


async def watch_duck_timeout(state):
    """Lift a duck that no event ever came to lift.

    Polling rather than a timer armed at duck time: the duck runs in a worker
    thread through _duck_async, so arming and cancelling a loop timer from there
    would mean touching the loop from the wrong thread for something this small.
    Two seconds of latency on a fault path costs nothing.
    """
    while True:
        await asyncio.sleep(2)
        if state.ducker.expired():
            _LOGGER.warning("duck held for over %.0fs with no releasing event; "
                            "restoring", state.ducker.MAX_HOLD_S)
            state._duck_async(state.ducker.restore)


async def watch_wake_direction(state):
    while True:
        refresh_wake_direction(state)
        await asyncio.sleep(.5)


async def watch_ring_config(ws, state):
    previous = None
    while True:
        current = (ring_config.load_autodim(), ring_config.load_brightness(),
                   json.dumps(ring_config.load_palette(), sort_keys=True))
        if current != previous:
            if previous is not None and current[2] != previous[2]:
                state.apply_ha()
            state.ha["brightness"] = current[1]
            await ws.send(json.dumps({"command": "ring_config_state", "data": {
                "autodim": current[0], "brightness": current[1] / 255,
                "palette": ring_config.load_palette()["preset"],
                "accents":ring_config.load_palette()}}))
            previous = current
        await asyncio.sleep(2)


async def watch_device_volume(ws, state):
    """Reconcile each observed effective step and verify both directions."""
    while await state.volume.tick(ws):
        await asyncio.sleep(AUDIO_VOLUME_POLL_S)


async def watch_hardware_mute(ws, state):
    """Physical mute button -> Home Assistant.

    biscuit-audio publishes the authoritative state; this notices a change and
    tells the assistant, which updates its MuteSwitchEntity and therefore HA.

    Polling rather than inotify: the file is tiny, 0.4 s is imperceptible for a
    button, and it avoids a dependency for something this small. Only genuine
    DIFFERENCES are forwarded, so the relay in the other direction cannot echo
    back and start a loop.
    """
    last = read_hw_muted()
    state.muted = bool(last)
    while True:
        await asyncio.sleep(MUTE_POLL_S)
        now = read_hw_muted()
        if now is None or now == last:
            continue
        last = now
        _LOGGER.info("hardware mute changed -> %s; notifying the assistant",
                     "muted" if now else "live")
        try:
            await ws.send(json.dumps(
                {"command": "mute_mic" if now else "unmute_mic"}))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("could not notify assistant: %s", err)
            return


class SerialJobs:
    """Blocking work off the event loop, one job at a time, in request order.

    For commands that start and stop services. These used to get a thread
    each, so two quick HA toggles ran interleaved: an "On" that read the
    state while an "Off" was still inside rc-update del left the direction
    light stopped and out of the runlevel while every page said On. A job that
    decides from the current state does so here after the one before it has
    finished. The drain task is kept referenced, so it cannot be collected
    half-way. Inline when there is no loop, which is how the tests drive it.
    """

    def __init__(self):
        self._queue = []
        self._task = None

    def submit(self, fn, *args):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            fn(*args)
            return
        self._queue.append((fn, args))
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._drain())

    async def _drain(self):
        while self._queue:
            fn, args = self._queue.pop(0)
            try:
                await asyncio.to_thread(fn, *args)
            except Exception as err:  # noqa: BLE001 - later jobs still run
                _LOGGER.warning("%s failed: %s", getattr(fn, "__name__", fn), err)


# Everything a registration's value or options are read from. A change to any
# of them - from the settings page, a button, a timer, an import - is what
# makes watch_settings rebuild the registrations and compare.
SETTINGS_FILES = (
    MIC_ENV, MICCAL_MODE_FILE, call_profile.STOCK_FILE, MIC_VAD_MODEL[0],
    MIC_FIREOS6_FRONTEND, PROFILE_ASSET_MANIFEST,
    EQ_STATE, EARCON_SET_FILE, SOUND_MAP_FILE,
    MIC_LED_FILE, MUTE_LED_FILE, DUCK_FILE, BUTTONS_FILE,
    # Not BTPROXY_STATE: the proxy rewrites it every few seconds with its
    # counters. The two flags read from it are in settings_stamp instead.
    BTPROXY_CONF, PAIRING_MARKER,
    ring_config.VIZ_FILE, ring_config.AUTODIM_FILE, ring_config.PALETTE_FILE,
    str(services.PERSIST / "direction.disabled"), str(services.PERSIST / "direction-mode"),
    str(services.RUN / "direction.paused"),
)
# And a full rebuild now and then regardless, for anything the list misses.
SETTINGS_FULL_S = 60.0


def settings_stamp():
    """A cheap fingerprint of every file behind the registrations: one stat
    each, plus the directories the stock profile assets are imported into, so
    an import that adds a profile changes it too."""
    dirs = {os.path.dirname(path) for flag in _MIC_PROFILE_FLAG.values()
            for path, _sizes, _digests, _required in _profile_assets(flag)}
    stamp = []
    for path in SETTINGS_FILES + tuple(sorted(dirs)):
        try:
            st = os.stat(path)
            stamp.append((st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append(None)
    stamp.append((btproxy_enabled(), btproxy_active()))
    return tuple(stamp)


_UNSENT = object()


class EntitySync:
    """Keeps Home Assistant's copy of every registered value equal to its file.

    LVA holds a value per entity and hands it to HA. It changes that value
    itself when HA sets one - optimistically, before this agent has tried - and
    otherwise only when told. So this tracks what LVA holds, and after any
    change on either side sends what is really stored:

      - a setting changed anywhere else (the settings page, a button, the
        pairing window closing itself) is pushed as entity_state;
      - a command this agent refused or adjusted is pushed back the same way,
        so HA reverts or shows the side effect (an EQ band makes the mode
        Custom, a single microphone turns beamforming off);
      - a registration whose options or name changed is sent again, and LVA
        makes HA re-read the entity list.
    """

    # Commands whose result lands a few seconds later, because they restart a
    # service or start a helper. Reading the files in the meantime would show
    # the old state and flick HA's switch back and forth.
    SETTLE_S = {"bt_pairing": 8.0, "btproxy": 12.0, "btproxy_active": 12.0,
                "direction_state": 15.0, "direction_mode": 2.0}

    def __init__(self):
        self.seq = 0
        self.reset([])

    def reset(self, registrations, headphones=_UNSENT, stamp=None):
        """A new connection: LVA holds exactly what was just registered.

        `stamp` is settings_stamp() taken BEFORE the registrations were read,
        so a file changed while they were built and sent still counts as a
        change; stamping here would have hidden it until the full check."""
        self.sent = {}
        self.lva = {}
        for reg in registrations:
            data = reg.get("data", {})
            if data.get("object_id"):
                self.sent[data["object_id"]] = reg
                if "initial_value" in data:
                    self.lva[data["object_id"]] = data["initial_value"]
        self.hold = {}
        self.commanded = {}
        self.dirty = False
        self.stamp = settings_stamp() if stamp is None else stamp
        self.next_full = time.monotonic() + SETTINGS_FULL_S
        self.headphones = headphones

    def note_command(self, object_id, value):
        """HA set a value: LVA already holds it, so check it against the file."""
        if not isinstance(object_id, str) or not object_id:
            return
        self.lva[object_id] = value
        self.hold[object_id] = time.monotonic() + self.SETTLE_S.get(object_id, 0.0)
        self.seq += 1
        self.commanded[object_id] = self.seq
        self.dirty = True

    @staticmethod
    def _same(a, b):
        if (isinstance(a, (int, float)) and isinstance(b, (int, float))
                and not isinstance(a, bool) and not isinstance(b, bool)):
            return abs(float(a) - float(b)) < 0.01
        return a == b

    @staticmethod
    def _described(reg):
        return {k: v for k, v in reg.get("data", {}).items() if k != "initial_value"}

    def due(self, now):
        """Whether a rebuild is needed: a file changed, a command arrived, a
        hold ran out, or the periodic full check is due."""
        expired = [oid for oid, until in self.hold.items() if until <= now]
        for oid in expired:
            del self.hold[oid]
        stamp = settings_stamp()
        if stamp != self.stamp or self.dirty or expired or now >= self.next_full:
            self.stamp, self.dirty = stamp, False
            self.next_full = now + SETTINGS_FULL_S
            return True
        return False

    def changes(self, registrations, now, built_at=None):
        """The commands that bring LVA, and so HA, up to date.

        `built_at` is self.seq when the registrations started being read. A
        command that arrived after that may have written its file after the
        read, so its entity is left to the next rebuild - which the command
        has already made due - rather than answered with the old value."""
        out = []
        for reg in registrations:
            data = reg.get("data", {})
            oid = data.get("object_id")
            if not oid or self.hold.get(oid, 0.0) > now:
                continue
            if built_at is not None and self.commanded.get(oid, 0) > built_at:
                continue
            previous = self.sent.get(oid)
            self.sent[oid] = reg
            if previous is None or self._described(previous) != self._described(reg):
                out.append(reg)
                if "initial_value" in data:
                    self.lva[oid] = data["initial_value"]
            elif "initial_value" in data and not self._same(self.lva.get(oid), data["initial_value"]):
                out.append({"command": "entity_state",
                            "data": {"object_id": oid, "value": data["initial_value"]}})
                self.lva[oid] = data["initial_value"]
        return out


async def watch_settings(ws, state):
    """Home Assistant follows every file behind its entities, within ~1 s.

    One stat per file per second decides whether anything changed; only then
    are the registrations rebuilt - off the event loop, since checking which
    stock profiles are usable hashes their files - and compared.
    """
    sync = state.sync
    while True:
        await asyncio.sleep(SETTINGS_POLL_S)
        now = time.monotonic()
        if sync.due(now):
            built_at = sync.seq
            # A bad file must not end this task: it would die silently, and HA
            # would stop following every setting until the next reconnect. A
            # send that fails is different - the connection is gone - and ends
            # it as before.
            try:
                registrations = await asyncio.to_thread(build_registrations)
                commands = sync.changes(registrations, time.monotonic(), built_at)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("could not rebuild the Home Assistant entities",
                                exc_info=True)
                commands = []
            for command in commands:
                data = command.get("data", {})
                if command["command"] == "entity_state":
                    _LOGGER.info("Home Assistant <- %s = %r", data["object_id"], data["value"])
                else:
                    _LOGGER.info("Home Assistant <- %s re-registered (its options or name changed)",
                                 data.get("object_id"))
                await ws.send(json.dumps(command))
        headphones = read_headphones()
        if headphones != sync.headphones and not (headphones is None and sync.headphones is _UNSENT):
            _LOGGER.info("headphones -> %s", {True: "in", False: "out", None: "unknown"}[headphones])
            await ws.send(json.dumps({"command": "binary_sensor_state", "data": {
                "object_id": "headphones", "value": headphones}}))
            sync.headphones = headphones


async def run():
    from linux_voice_assistant.peripheral_transport import connect as connect_peripheral

    state = Ring()
    tasks = []
    # Process-scoped, NOT per-connection. A dropped peripheral connection is
    # precisely when a duck leaks - the assistant can no longer send the
    # tts_finished or pipeline_error that would lift it - so a watchdog created
    # inside the loop was being cancelled at the one moment it was needed.
    duck_task = asyncio.create_task(watch_duck_timeout(state))
    build_warned = False
    while True:
        try:
            async with connect_peripheral() as ws:
                state.volume.connect()
                _LOGGER.info("connected to the peripheral API")
                # The assistant does not let Home Assistant in until
                # registrations_done, so everything HA should see on its first
                # enumeration goes first: the entities, the light's real state,
                # the microphones' real mute, the jack. HA deletes an entity it
                # enumerates without, so there is no second chance to be early.
                #
                # Stamped before the files are read: see EntitySync.reset.
                try:
                    stamp, registrations = await asyncio.to_thread(
                        lambda: (settings_stamp(), build_registrations()))
                except Exception:
                    # Retried every second below, but logged only at DEBUG there,
                    # and this one means no ring, mute relay or ducking at all.
                    # Once per run of failures, not once a second.
                    if not build_warned:
                        _LOGGER.warning("could not build the Home Assistant entities",
                                        exc_info=True)
                    build_warned = True
                    raise
                build_warned = False
                for reg in registrations:
                    await ws.send(json.dumps(reg))
                await ws.send(json.dumps({"command": "light_state", "data": state.light_state()}))
                # Only a MUTE is pushed. LVA may hold a mute HA set while this
                # agent was away, which the snapshot relays to the hardware;
                # pushing "live" first would have unmuted LVA before that. Any
                # disagreement is settled toward muted.
                if read_hw_muted():
                    await ws.send(json.dumps({"command": "entity_state", "data": {
                        "object_id": "mute", "value": True}}))
                headphones = read_headphones()
                if headphones is not None:
                    await ws.send(json.dumps({"command": "binary_sensor_state", "data": {
                        "object_id": "headphones", "value": headphones}}))
                await ws.send(json.dumps({"command": "registrations_done"}))
                state.sync.reset(registrations,
                                 headphones=_UNSENT if headphones is None else headphones,
                                 stamp=stamp)
                kinds = {}
                for r in registrations:
                    kinds[r["command"]] = kinds.get(r["command"], 0) + 1
                _LOGGER.info(
                    "registered %d lights, %d numbers/selects, %d switches, "
                    "%d sensors, %d binary sensors, %d buttons, %d ring effects",
                    kinds.get("register_light", 0), kinds.get("register_number", 0),
                    kinds.get("register_switch", 0), kinds.get("register_sensor", 0),
                    kinds.get("register_binary_sensor", 0), kinds.get("register_button", 0),
                    len(ring_effects()))
                tasks = [
                    asyncio.create_task(watch_hardware_mute(ws, state)),
                    asyncio.create_task(watch_ambient_light(ws)),
                    asyncio.create_task(watch_action_button(ws)),
                    asyncio.create_task(watch_device_volume(ws, state)),
                    asyncio.create_task(watch_ring_config(ws, state)),
                    # Connected lifetime only; demand expires two seconds after disconnect.
                    asyncio.create_task(watch_wake_direction(state)),
                    asyncio.create_task(watch_mic_profile(ws)),
                    asyncio.create_task(watch_settings(ws, state)),
                ]
                # Re-assert the ring ceiling on connect. Without this, an agent
                # that restarts with auto dim OFF leaves no override file, so
                # biscuit-ring silently falls back to the ambient value and the
                # switch says one thing while the ring does another.
                state.push_brightness()
                async for message in ws:
                    try:
                        payload = json.loads(message)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(payload, dict):
                        # One bad command must not cost the connection: that
                        # drops the ring, the mute relay and ducking with it,
                        # and the reason would be logged only at DEBUG below.
                        try:
                            state.event(payload.get("event", ""),
                                        payload.get("data") or {})
                        except Exception:  # noqa: BLE001
                            _LOGGER.warning("event %r failed", payload.get("event"),
                                            exc_info=True)
        except Exception as err:  # noqa: BLE001 - reconnect on anything
            _LOGGER.debug("peripheral API unavailable (%s); retrying", err)
        finally:
            for task in tasks:
                task.cancel()
            tasks = []
            # The assistant restarts independently, so a dropped connection is
            # routine. Do not leave the ring showing a state that is stale.
            state._clear()
            # ... and do not leave the MUSIC ducked either. The duck is lifted by
            # tts_finished or pipeline_error, both of which arrive over the
            # connection that has just gone; nothing else was ever going to
            # raise it. This was the leak: every drop while ducked left the
            # music at 20%, and WirePlumber then handed that level to the next
            # stream of the same name and the next duck took 20% of it.
            # A duck still running in its worker counts: the lock queues the
            # restore behind it. Otherwise skipped, so a retry every second
            # while the assistant is down does not cost a thread each time.
            if state.ducker.saved or state._duck_queued:
                state._duck_async(state.ducker.restore)
            await asyncio.sleep(PERIPHERAL_RETRY_S)


def main():
    logging.basicConfig(
        level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        ring("clear")
    return 0


if __name__ == "__main__":
    sys.exit(main())
