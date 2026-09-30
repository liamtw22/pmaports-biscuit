#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
#
# MODIFIED FILE. This is a changed copy of
# linux_voice_assistant/__main__.py from OHF-Voice/linux-voice-assistant
# at commit b0c53c41c11e,
#   https://github.com/OHF-Voice/linux-voice-assistant
# licensed under the Apache License 2.0 (LICENSES/Apache-2.0.txt).
# Changed by liamtw22 and contributors, 2026, for the Amazon Echo Dot
# (2nd gen) postmarketOS port. The upstream copyright is unchanged.
# Changes: device start-up and options: the wake-input AGC, command-based
#   microphone capture and the processed microphone profile; the peripheral API
#   moved from a TCP listener to a root-only Unix socket; device build
#   reporting; qualification instrumentation.
from . import qualification_v1 as _qualification_v1
import argparse
import asyncio
import errno
import json
import logging
import os
import math
import socket
import sys
import subprocess
import threading
import time
from pathlib import Path
from queue import Queue
from typing import List, Optional, Union

import numpy as np
import soundcard as sc
from aioesphomeapi.api_pb2 import NumberStateResponse  # type: ignore  # pylint: disable=no-name-in-module
from getmac import get_mac_address  # type: ignore
from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures
from pyopen_wakeword import OpenWakeWord, OpenWakeWordFeatures

from .models import Preferences, ServerState, WakeWordType, initial_stop_word_threshold
from .mpv_player import MpvMediaPlayer
from .peripheral_api import LVAEvent, PeripheralAPIServer
from .satellite import VoiceSatelliteProtocol
from .util import (
    get_default_interface,
    get_default_ipv4,
    get_esphome_version,
    get_version,
)
from .wake_word import find_available_wake_words, load_stop_model, load_wake_models
from .webrtc import WebRTCProcessor
from .zeroconf import HomeAssistantZeroconf

_LOGGER = logging.getLogger(__name__)
_MODULE_DIR = Path(__file__).parent
_REPO_DIR = _MODULE_DIR.parent
_WAKEWORDS_DIR = _REPO_DIR / "wakewords"
_SOUNDS_DIR = _REPO_DIR / "sounds"

# BISCUIT: the device build Home Assistant shows as the firmware version.
_APK_DB = Path("/lib/apk/db/installed")
_DEVICE_PACKAGE = "device-amazon-biscuit"


def device_build(db: Path = _APK_DB, package: str = _DEVICE_PACKAGE) -> Optional[str]:
    """The installed device package's release, as "r293", or None.

    From the apk database rather than `apk info`: this runs once at start and a
    fork of apk costs more than reading the file. The release is what every
    note, changelog and settings page on this device calls a build; the pkgver
    in front of it has not moved in months.
    """
    try:
        with open(db, "r", encoding="utf-8", errors="replace") as handle:
            ours = False
            for line in handle:
                if line.startswith("P:"):
                    ours = line[2:].strip() == package
                elif ours and line.startswith("V:"):
                    version = line[2:].strip()
                    release = version.rpartition("-r")[2]
                    return "r" + release if release.isdigit() else version
    except OSError:
        pass
    return None


def listening_socket(host: str, port: int) -> socket.socket:
    """Bind and listen now; asyncio starts accepting later.

    A connection that arrives before serving starts completes its TCP handshake
    into the kernel backlog and waits there - Home Assistant's Hello simply sits
    unanswered - instead of being refused and backing off for up to a minute.
    create_server(start_serving=False) alone would not do this: it binds, but
    calls listen() only when serving starts.
    """
    family, kind, proto, _name, address = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)[0]
    sock = socket.socket(family, kind, proto)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
        sock.listen(16)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


# -----------------------------------------------------------------------------
class CommandRecorder:
    """Read exact PCM16 blocks from a device-owned capture command."""

    def __init__(self, command: Path, samplerate: int, channels: int):
        if samplerate != 16000:
            raise ValueError("command microphone requires 16000 Hz")
        if channels != 1:
            raise ValueError("command microphone currently supports mono only")
        self.command = command
        self.channels = channels
        self.process: Optional[subprocess.Popen[bytes]] = None

    def __enter__(self):
        self.process = subprocess.Popen(
            [str(self.command)],
            stdout=subprocess.PIPE,
        )
        if self.process.stdout is None:
            raise RuntimeError("capture command has no stdout")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def record_bytes(self, frames: int) -> bytes:
        """Return exactly one mono S16LE block without float conversion."""
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("capture command is not running")
        needed = frames * self.channels * 2
        chunks: list[bytes] = []
        received = 0
        while received < needed:
            chunk = self.process.stdout.read(needed - received)
            if not chunk:
                returncode = self.process.poll()
                raise RuntimeError(
                    f"capture command ended after {received}/{needed} bytes "
                    f"(status={returncode})"
                )
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)


class CommandMicrophone:
    """Minimal soundcard-compatible microphone backed by an executable."""

    def __init__(self, command: Path):
        self.command = command
        self.name = f"command/{command}"

    def recorder(self, *, samplerate: int, channels: int, blocksize: int):
        del blocksize
        return CommandRecorder(self.command, samplerate, channels)


# -----------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--name",
        help="Real name for the device",
    )
    parser.add_argument(
        "--audio-input-device",
        help="Name for the audio input device (see --list-input-devices)",
    )
    parser.add_argument(
        "--audio-input-command",
        type=Path,
        help=(
            "Executable that emits mono 16 kHz S16LE microphone PCM on stdout"
        ),
    )
    parser.add_argument(
        "--list-input-devices",
        action="store_true",
        help="List audio input devices and exit",
    )
    parser.add_argument(
        "--audio-input-block-size",
        type=int,
        default=1024,
    )
    parser.add_argument(
        "--audio-output-device",
        help="Name for the audio output device (see --list-output-devices)",
    )
    parser.add_argument(
        "--music-output-device",
        help="mpv name for the music/media output device (defaults to --audio-output-device)",
    )
    parser.add_argument(
        "--list-output-devices",
        action="store_true",
        help="List audio output devices and exit",
    )
    parser.add_argument("--mic-volume", type=int, default=None, choices=list(range(1, 101)), help="Microphone volume level (1 to 100)")
    parser.add_argument("--mic-auto-gain", type=int, default=0, choices=list(range(32)))
    parser.add_argument("--mic-noise-suppression", type=int, default=0, choices=(0, 1, 2, 3, 4))
    parser.add_argument(
        "--processed-mic-profile",
        help=(
            "Name of the hardware/DSP microphone profile feeding LVA. When set, "
            "LVA keeps the input byte-transparent (100%% volume, no WebRTC AGC or "
            "noise suppression) and hides those obsolete post-DSP controls."
        ),
    )
    parser.add_argument(
        "--wake-input-gain-db",
        type=float,
        default=0.0,
        help=(
            "Fixed gain applied only to wake/stop-model feature extraction. "
            "Command audio sent to Home Assistant remains byte-transparent."
        ),
    )
    parser.add_argument(
        "--wake-input-agc",
        action="store_true",
        help=(
            "Normalise the wake/stop-model input level instead of applying a "
            "fixed gain. Slow by design (see WakeInputAgc). Command audio sent "
            "to Home Assistant remains byte-transparent either way."
        ),
    )
    parser.add_argument(
        "--wake-agc-target-dbfs",
        type=float,
        default=-12.0,
        help="Peak level the wake-input AGC aims for (default: -12 dBFS).",
    )
    parser.add_argument(
        "--wake-agc-max-gain-db",
        type=float,
        default=36.0,
        help="Upper bound on wake-input AGC gain (default: 36 dB).",
    )
    parser.add_argument(
        "--wake-agc-noise-ceiling-dbfs",
        type=float,
        default=-38.0,
        help=(
            "Do not amplify a room past this noise-floor level, so a quiet "
            "room is not lifted until its own floor reaches the model."
        ),
    )
    parser.add_argument(
        "--audio-input-channels",
        type=int,
        default=1,
        choices=(1, 2),
        help="Number of mic channels to capture and stream (1=mono, 2=dual-channel voice)",
    )
    parser.add_argument(
        "--wake-word-dir",
        default=[_WAKEWORDS_DIR],
        action="append",
        help="Directory with wake word models (.tflite) and configuration (.json)",
    )
    parser.add_argument(
        "--wake-model",
        default="okay_nabu",
        help="File name of the first active wake model",
    )
    parser.add_argument(
        "--stop-model",
        default="stop",
        help="File name of the stop model",
    )
    parser.add_argument(
        "--download-dir",
        default=_REPO_DIR / "local",
        help="Directory to download custom wake word models to",
    )
    parser.add_argument(
        "--refractory-seconds",
        default=2.0,
        type=float,
        help="Seconds before wake word can be activated again",
    )
    parser.add_argument(
        "--continue-conversation-delay",
        type=float,
        default=0.5,
        help="Seconds to wait after TTS finishes before opening the mic for continued conversation (default: 0.5)",
    )
    parser.add_argument(
        "--wakeup-sound",
        default=str(_SOUNDS_DIR / "wake_word_triggered.flac"),
        help="Directory and file name for wake sound (when you say the wake word)",
    )
    parser.add_argument(
        "--start-listening-sound",
        default=str(_SOUNDS_DIR / "start_listening_button.flac"),
        help="Directory and file name and sound for start listening button (when you press button to talk)",
    )
    parser.add_argument(
        "--timer-finished-sound",
        default=str(_SOUNDS_DIR / "timer_finished.flac"),
        help="Directory and file name for timer finished sound",
    )
    parser.add_argument(
        "--processing-sound",
        default=str(_SOUNDS_DIR / "processing.wav"),
        help="Short sound to play while assistant is processing (thinking)",
    )
    parser.add_argument(
        "--mute-sound",
        default=str(_SOUNDS_DIR / "mute_switch_on.flac"),
        help="Sound to play when muting the assistant",
    )
    parser.add_argument(
        "--unmute-sound",
        default=str(_SOUNDS_DIR / "mute_switch_off.flac"),
        help="Sound to play when unmuting the assistant",
    )
    parser.add_argument(
        "--button-double-press-sound",
        default=str(_SOUNDS_DIR / "button_double_press.flac"),
        help="Sound to play for button double press",
    )
    parser.add_argument(
        "--button-triple-press-sound",
        default=str(_SOUNDS_DIR / "button_triple_press.flac"),
        help="Sound to play for button triple press",
    )
    parser.add_argument(
        "--button-long-press-sound",
        default=str(_SOUNDS_DIR / "button_long_press.flac"),
        help="Sound to play for button long press",
    )
    parser.add_argument(
        "--preferences-file",
        default=_REPO_DIR / "preferences.json",
        help="Directory and file name for the preferences JSON file",
    )
    parser.add_argument(
        "--host",
        help="Optional host IP address to bind to (default: auto-detected by network interface)",
    )
    parser.add_argument(
        "--network-interface",
        help="Network interface the application listens on (default: auto-detected by gateway)",
    )
    # Note that default port is also set in docker-entrypoint.sh
    parser.add_argument(
        "--port",
        type=int,
        default=6053,
        help="Port the application is listening on (default: 6053)",
    )
    parser.add_argument(
        "--enable-thinking-sound",
        action="store_true",
        help="Enable thinking sound on startup",
    )
    # ------------------------------------------------------------------
    # Peripheral API (LEDs, buttons, HAT boards)
    # ------------------------------------------------------------------
    parser.add_argument(
        "--peripheral-volume-step",
        type=float,
        default=PeripheralAPIServer.DEFAULT_VOLUME_STEP,
        metavar="STEP",
        help="Volume change per button press, 0.0–1.0 (default: %(default)s)",
    )
    parser.add_argument(
        "--disable-peripheral-api",
        action="store_true",
        help="Disable the root-only Unix peripheral API (/run/biscuit-peripheral/control.sock)",
    )
    parser.add_argument(
        "--peripheral-startup-wait",
        type=float,
        default=20.0,
        metavar="SECONDS",
        help="Longest wait for a peripheral to send registrations_done before the ESPHome API is served (default: %(default)s; set 0 to skip). Connections made meanwhile wait in the kernel's backlog.",
    )
    # ------------------------------------------------------------------
    parser.add_argument(
        "--timer-max-ring-seconds",
        type=float,
        default=900.0,  # 15 minutes
        help="Seconds before a ringing timer auto-stops (default: 900)",
    )
    parser.add_argument(
        "--listen-during-wake-sound",
        action="store_true",
        help="Start listening immediately after wake word detection, without waiting for the wake sound to finish",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Add this to enable debug logging",
    )
    parser.add_argument(
        "--colored-debug",
        action="store_true",
        help="Add this to enable colored debug logging",
    )
    parser.add_argument(
        "--output-only",
        action="store_true",
        help="Enable output only mode",
    )
    args = parser.parse_args()

    if not -24.0 <= args.wake_input_gain_db <= 24.0:
        parser.error("--wake-input-gain-db must be between -24 and +24 dB")

    if not -40.0 <= args.wake_agc_target_dbfs <= 0.0:
        parser.error("--wake-agc-target-dbfs must be between -40 and 0 dBFS")
    if not 0.0 <= args.wake_agc_max_gain_db <= 60.0:
        parser.error("--wake-agc-max-gain-db must be between 0 and 60 dB")
    if not -80.0 <= args.wake_agc_noise_ceiling_dbfs <= 0.0:
        parser.error("--wake-agc-noise-ceiling-dbfs must be between -80 and 0 dBFS")
    if args.wake_input_agc and args.wake_input_gain_db:
        parser.error(
            "--wake-input-agc replaces --wake-input-gain-db; pass only one"
        )

    if args.colored_debug:
        args.debug = True
        _setup_logging(args)
    elif args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    _LOGGER.debug(args)
    if args.list_input_devices:
        print("Audio Input devices:")
        print("=" * 13)
        for idx, mic in enumerate(sc.all_microphones()):
            print(f"[{idx}]", mic.name)
        return

    if args.list_output_devices:
        from mpv import MPV

        player = MPV()
        print("Audio output devices:")
        print("=" * 14)

        for speaker in player.audio_device_list:  # type: ignore
            print(speaker["name"] + ":", speaker["description"])
        return

    # Resolve network interface for mac-address detection
    if not args.network_interface:
        print("No network interface specified, try to detect default interface")
        network_interface = get_default_interface()
        print(f"Default interface detected: {network_interface}")
    else:
        print("Network interface specified")
        network_interface = args.network_interface
        print(f"Using network interface: {network_interface}")

    # Resolve ip_address where the application will be listening
    if not args.host:
        print("No host (ip-address) specified, try to detect IP-Address")
        host_ip_address = get_default_ipv4(network_interface)
        print(f"IP-Address detected: {host_ip_address}")
    else:
        print("Host specified")
        print(f"Using host: {args.host}")
        host_ip_address = args.host

    # Resolve mac
    if not (mac_address := get_mac_address(interface=network_interface)):
        print("No Mac address was found, app stopped.")
        sys.exit(1)
    mac_address_clean = mac_address.replace(":", "").lower()

    # Resolve name
    if not args.name:
        print("No friendly name specified, try to autogenerate name")
        friendly_name = f"LVA - {mac_address_clean}"
        print(f"Friendly name autogenerated: {friendly_name}")
    else:
        print("Friendly name specified")
        print(f"Using friendly name: {args.name}")
        friendly_name = args.name

    device_name = f"lva-{mac_address_clean}"

    print(f"Device name: {device_name}")

    # Resolve version
    version = get_version()
    print(f"Version: {version}")

    # Resolve esphome version
    esphome_version = get_esphome_version()
    print(f"ESPHome api version: {esphome_version}")

    build = device_build()
    print(f"Device build: {build}")

    # Resolve download dir
    args.download_dir = Path(args.download_dir)
    args.download_dir.mkdir(parents=True, exist_ok=True)

    # Resolve microphone
    if args.audio_input_command is not None:
        if args.audio_input_device is not None:
            parser.error("use only one of --audio-input-command/--audio-input-device")
        mic = CommandMicrophone(args.audio_input_command)
    elif args.audio_input_device is not None:
        try:
            args.audio_input_device = int(args.audio_input_device)
        except ValueError:
            pass

        mic = sc.get_microphone(args.audio_input_device)
    else:
        mic = sc.default_microphone()

    # Load available wake words
    wake_word_dirs = [Path(ww_dir) for ww_dir in args.wake_word_dir]

    # If the operator explicitly pointed --wake-word-dir (or the WAKE_WORD_DIR
    # env var) at the openWakeWord subdirectory, prefer resolving --wake-model
    # to an openWakeWord model of the same name instead of a same-named
    # microWakeWord one. Checked before the automatic dirs below are appended,
    # since those always include the openWakeWord path and would otherwise
    # make every configuration look like an openWakeWord preference.
    preferred_wake_word_type = WakeWordType.OPEN_WAKE_WORD if any("openwakeword" in str(ww_dir).lower() for ww_dir in wake_word_dirs) else None

    # openWakeWord models ship in their own subdirectory under the default
    # wakewords dir. find_available_wake_words() only globs the top level of
    # each directory it's given, so this must be added explicitly or the OWW
    # models never get discovered (and never show up in the HA dropdown).
    # Appended after the user-specified dirs so OWW entries are inserted
    # (and therefore displayed) after the microWakeWord ones.
    oww_dir = _WAKEWORDS_DIR / "openWakeWord"
    if oww_dir not in wake_word_dirs:
        wake_word_dirs.append(oww_dir)

    wake_word_dirs.append(args.download_dir / "external_wake_words")
    available_wake_words = find_available_wake_words(wake_word_dirs, args.stop_model)

    # Load preferences
    preferences_path = Path(args.preferences_file)
    if preferences_path.exists():
        _LOGGER.debug("Loading preferences: %s", preferences_path)
        with open(preferences_path, "r", encoding="utf-8") as preferences_file:
            preferences_dict = json.load(preferences_file)
            preferences = Preferences(**preferences_dict)
    else:
        preferences = Preferences()

    # Load volume from preferences on startup, and ensure it's between 0.0 and 1.0
    initial_volume = preferences.volume if preferences.volume is not None else 1.0
    initial_volume = max(0.0, min(1.0, float(initial_volume)))
    preferences.volume = initial_volume

    # Load stop word sensitivity from preferences on startup, and ensure it's between 0.0 and 1.0
    initial_threshold = initial_stop_word_threshold(preferences.stop_word_sensitivity)
    preferences.stop_word_sensitivity = initial_threshold

    if args.enable_thinking_sound:
        preferences.thinking_sound = 1

    preferences_changed = False
    if args.processed_mic_profile:
        # Biscuit's microphone source is already the complete device AFE: codec
        # PGA, factory calibration, high-pass/filterbank, AEC, fixed/adaptive
        # beams, selection, and stock's +7.2 dB output stage. Applying LVA's
        # legacy WebRTC gain/noise layer after that is both non-stock and harmful
        # to the model's calibrated input distribution. Profile the boundary so
        # future device-chain revisions can migrate old preferences explicitly.
        processed_defaults = (0, 0, 100)
        current_processed = (
            preferences.mic_auto_gain,
            preferences.mic_noise_suppression,
            preferences.mic_volume,
        )
        if current_processed != processed_defaults:
            _LOGGER.warning(
                "Resetting legacy LVA microphone processing for profile %s: %s -> %s",
                args.processed_mic_profile,
                current_processed,
                processed_defaults,
            )
            preferences.mic_auto_gain = 0
            preferences.mic_noise_suppression = 0
            preferences.mic_volume = 100
            preferences_changed = True
        if preferences.mic_input_profile != args.processed_mic_profile:
            preferences.mic_input_profile = args.processed_mic_profile
            preferences_changed = True

    if preferences.mic_auto_gain or preferences.mic_noise_suppression:
        try:
            import webrtc_noise_gain  # type: ignore[import-untyped] # noqa: F401
        except ImportError:
            _LOGGER.exception("Extras for webrtc are not installed")
            sys.exit(1)

    if (not args.processed_mic_profile) and (args.mic_volume is not None):
        preferences.mic_volume = args.mic_volume
    if (not args.processed_mic_profile) and (args.mic_auto_gain > 0):
        preferences.mic_auto_gain = args.mic_auto_gain

    if (not args.processed_mic_profile) and (args.mic_noise_suppression > 0):
        preferences.mic_noise_suppression = args.mic_noise_suppression

    # Load wake/stop models
    wake_models, active_wake_words, fallback_used = load_wake_models(
        available_wake_words,
        [word for word in preferences.active_wake_words if word is not None],
        args.wake_model,
        preferred_type=preferred_wake_word_type,
    )

    # TODO: allow openWakeWord for "stop"
    stop_model = load_stop_model(wake_word_dirs, args.stop_model)
    assert stop_model is not None

    state = ServerState(
        name=device_name,
        friendly_name=friendly_name,
        network_interface=network_interface,
        mac_address=mac_address,
        ip_address=host_ip_address,
        version=version,
        esphome_version=esphome_version,
        audio_queue=Queue(),
        entities=[],
        available_wake_words=available_wake_words,
        wake_words=wake_models,
        active_wake_words=active_wake_words,
        stop_word=stop_model,
        music_player=MpvMediaPlayer(device=args.music_output_device or args.audio_output_device),
        tts_player=MpvMediaPlayer(device=args.audio_output_device),
        wakeup_sound=args.wakeup_sound,
        start_listening_sound=args.start_listening_sound,
        timer_finished_sound=args.timer_finished_sound,
        processing_sound=args.processing_sound,
        mute_sound=args.mute_sound,
        unmute_sound=args.unmute_sound,
        button_double_press_sound=args.button_double_press_sound,
        button_triple_press_sound=args.button_triple_press_sound,
        button_long_press_sound=args.button_long_press_sound,
        preferences=preferences,
        preferences_path=preferences_path,
        refractory_seconds=args.refractory_seconds,
        continue_conversation_delay=args.continue_conversation_delay,
        output_only=args.output_only,
        download_dir=args.download_dir,
        volume=initial_volume,
        stop_word_threshold=initial_threshold,
        mic_volume=preferences.mic_volume,
        mic_auto_gain=preferences.mic_auto_gain,
        mic_noise_suppression=preferences.mic_noise_suppression,
        audio_input_channels=args.audio_input_channels,
        timer_max_ring_seconds=args.timer_max_ring_seconds,
        listen_during_wake_sound=args.listen_during_wake_sound,
        processed_mic_input=bool(args.processed_mic_profile),
        mic_input_profile=args.processed_mic_profile,
        wake_input_gain=10.0 ** (args.wake_input_gain_db / 20.0),
        wake_input_agc=(
            {
                "target_dbfs": args.wake_agc_target_dbfs,
                "max_gain_db": args.wake_agc_max_gain_db,
                "noise_ceiling_dbfs": args.wake_agc_noise_ceiling_dbfs,
            }
            if args.wake_input_agc
            else None
        ),
    )

    # Not a ServerState field: models.py comes from the venv tarball.
    state.device_build = build

    if args.wake_input_agc:
        _LOGGER.info(
            "Wake/stop-model input AGC: target %.1f dBFS, max %+.1f dB, "
            "noise ceiling %.1f dBFS (command audio unchanged)",
            args.wake_agc_target_dbfs,
            args.wake_agc_max_gain_db,
            args.wake_agc_noise_ceiling_dbfs,
        )
    elif args.wake_input_gain_db:
        _LOGGER.info(
            "Wake/stop-model input gain: %+.2f dB (command audio unchanged)",
            args.wake_input_gain_db,
        )

    if fallback_used:
        # Fallback to the default model was used, save as active wake words
        _LOGGER.debug("Fallback was used, save default wake words in Preferences.")
        state.preferences.active_wake_words = list(active_wake_words)
        state.active_wake_words = active_wake_words
        state.wake_words = wake_models
        state.save_preferences()
        state.wake_words_changed = True

    if preferences_changed or args.enable_thinking_sound or args.mic_auto_gain or args.mic_noise_suppression:
        state.save_preferences()

    # biscuit-audio owns the device master volume.  LVA keeps the same value
    # for its ESPHome media-player state, but its two mpv outputs remain at
    # unity; otherwise a persisted 50% master attenuates twice on restart.
    state.music_player.set_volume(100)
    state.tts_player.set_volume(100)

    # ------------------------------------------------------------------
    # Peripheral API (optional – LEDs, buttons, HAT boards)
    # ------------------------------------------------------------------
    peripheral_api: Optional[PeripheralAPIServer] = None
    if not args.disable_peripheral_api:
        peripheral_api = PeripheralAPIServer(
            volume_step=args.peripheral_volume_step,
        )
        peripheral_api.set_state(state)
        state.peripheral_api = peripheral_api

    # ------------------------------------------------------------------
    # ESPHome TCP server (with retry on EADDRINUSE)
    # ------------------------------------------------------------------
    loop = asyncio.get_running_loop()
    max_attempts = 15
    attempt = 1
    server = None

    # Validate VoiceSatelliteProtocol initialization BEFORE starting server
    # This catches errors like missing imports or broken initialization immediately
    # instead of failing silently only when first client connects
    _LOGGER.debug("Validating VoiceSatelliteProtocol initialization...")
    try:
        # Create test instance to run complete __init__ code path
        test_protocol = VoiceSatelliteProtocol(state)
        # Cleanup state reference
        test_protocol.state.satellite = None
        del test_protocol
        _LOGGER.debug("✅ VoiceSatelliteProtocol validation successful")
    except Exception:
        _LOGGER.critical("❌ FATAL ERROR in VoiceSatelliteProtocol initialization!", exc_info=True)
        _LOGGER.critical("Program will exit immediately - fix the error above first!")
        sys.exit(1)

    while attempt <= max_attempts:
        try:
            # Not serving yet: see the peripheral wait below.
            server = await loop.create_server(
                lambda: VoiceSatelliteProtocol(state),
                sock=listening_socket(host_ip_address, args.port),
                start_serving=False,
            )
            break  # connection successful, exit the loop
        except OSError as err:
            message = err.strerror or str(err)
            if err.errno == errno.EADDRINUSE:
                message = "address already in use"
            if attempt < max_attempts:
                _LOGGER.warning(
                    "Attempt %d/%d failed to bind on address (%s, %s): %s. Retrying in 1 second...",
                    attempt,
                    max_attempts,
                    host_ip_address,
                    args.port,
                    message,
                )
                await asyncio.sleep(1)
                attempt += 1
            else:
                _LOGGER.exception(
                    "All %d attempts failed to bind on address (%s, %s): %s",
                    max_attempts,
                    host_ip_address,
                    args.port,
                    message,
                )
                sys.exit(1)

    # ------------------------------------------------------------------
    # Audio processing thread
    # ------------------------------------------------------------------
    process_audio_thread = threading.Thread(
        target=process_audio,
        args=(state, mic, args.audio_input_block_size),
        daemon=True,
    )
    process_audio_thread.start()

    # Auto discovery (zeroconf, mDNS)
    discovery = HomeAssistantZeroconf(
        port=args.port,
        name=state.name,
        mac_address=state.mac_address,
        host_ip_address=host_ip_address,
    )
    await discovery.register_server()

    # ------------------------------------------------------------------
    # Start peripheral API and signal "getting started" to peripherals
    # ------------------------------------------------------------------
    if peripheral_api is not None:
        await peripheral_api.start()
        await peripheral_api.emit_event(LVAEvent.ZEROCONF, {"status": "getting_started"})

        # BISCUIT: HA must not enumerate before the peripheral has
        # registered. An entity missing from HA's enumeration is not merely
        # absent - HA DELETES it from its entity registry, and every
        # automation, dashboard card and Assist exposure on it goes with it.
        # That happened at every boot: the old fixed 2 s sleep was shorter
        # than the agent took to connect, and the comment here claimed the
        # server was not yet serving when create_server had already started
        # it. Now it genuinely is not: the socket listens, so HA's connection
        # waits in the kernel backlog, and accepting starts once the agent
        # says registrations_done - or, with no agent (not installed,
        # crashed), after a bounded wait. A late agent's entities then reach
        # HA through one forced reconnect.
        if args.peripheral_startup_wait > 0:
            _LOGGER.info(
                "Holding the ESPHome API until the peripheral has registered (at most %.0fs)",
                args.peripheral_startup_wait,
            )
            started = time.monotonic()
            if await peripheral_api.wait_for_registrations(args.peripheral_startup_wait):
                _LOGGER.info("Peripheral registered in %.1fs", time.monotonic() - started)
            else:
                _LOGGER.warning(
                    "No peripheral registered within %.0fs; serving without its entities",
                    args.peripheral_startup_wait,
                )

    try:
        async with server:  # type: ignore[union-attr]
            _LOGGER.info("Server started (host=%s, port=%s)", host_ip_address, args.port)
            await server.serve_forever()  # type: ignore[union-attr]
    except KeyboardInterrupt:
        pass
    finally:
        state.audio_queue.put_nowait(None)
        process_audio_thread.join()
        if peripheral_api is not None:
            await peripheral_api.stop()

    _LOGGER.debug("Server stopped")


# -----------------------------------------------------------------------------
def _setup_logging(args: argparse.Namespace) -> None:
    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }
    RESET = "\033[0m"

    original_format = logging.Formatter.format

    def colored_format(self, record: logging.LogRecord) -> str:
        color = COLORS.get(record.levelno, RESET)
        return f"{color}{original_format(self, record)}{RESET}"

    logging.Formatter.format = colored_format  # type: ignore

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        handlers=[handler],
    )


# -----------------------------------------------------------------------------


class WakeInputAgc:
    """Slow level normaliser for the wake/stop model input only.

    The deployed microWakeWord model is strongly level dependent: on the
    retained paired captures the same audio scores 2-3 of 6 at the level the
    AFE actually delivers and 5-6 of 6 once its peak reaches roughly -20 to
    -12 dBFS, with background false accepts unchanged at zero. A single fixed
    gain cannot cover that, because the delivered level moves by 20 dB or more
    between a near talker and a far one in a noisy room.

    It must be slow. A fast AGC measurably makes things worse -- it moves
    within a single "Alexa" and distorts the envelope the model keys on, which
    scored 1 of 6 against 3 of 6 for a plain fixed gain. The defaults here move
    at 1 dB/s upward and 3 dB/s downward, so the gain is effectively constant
    across any one utterance and only tracks the room.

    A noise-floor ceiling stops a quiet room from being amplified until its own
    noise reaches the model. Command audio sent to Home Assistant never passes
    through this; only the feature extractors see it.
    """

    def __init__(
        self,
        target_dbfs: float = -12.0,
        max_gain_db: float = 36.0,
        min_gain_db: float = 0.0,
        noise_ceiling_dbfs: float = -38.0,
        env_release_s: float = 20.0,
        rise_db_s: float = 1.0,
        fall_db_s: float = 3.0,
        rate: int = 16000,
    ) -> None:
        self.target = (10.0 ** (target_dbfs / 20.0)) * 32768.0
        self.ceiling = (10.0 ** (noise_ceiling_dbfs / 20.0)) * 32768.0
        self.max_gain_db = max_gain_db
        self.min_gain_db = min_gain_db
        self.env_release_s = env_release_s
        self.rise_db_s = rise_db_s
        self.fall_db_s = fall_db_s
        self.rate = rate
        self.env = 0.0
        self.noise = 1e-6
        self.gain_db = min_gain_db
        # Slewing up from 0 dB at 1 dB/s would leave the wake model starved
        # for half a minute after every restart, so the first chunk seeds the
        # gain outright and the slew limits only apply from then on.
        self.primed = False

    def process(self, chunk: bytes) -> bytes:
        samples = np.frombuffer(chunk, dtype="<i2")
        if samples.size == 0:
            return chunk
        dt = samples.size / float(self.rate)
        peak = float(np.max(np.abs(samples.astype(np.int32))))
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) + 1e-9

        self.env = max(peak, self.env * math.exp(-dt / self.env_release_s))
        # Slow-rising, fast-falling floor: it must follow a room getting
        # quieter at once, but never chase speech upward.
        if rms < self.noise:
            self.noise = rms
        else:
            self.noise = min(rms, self.noise * math.exp(dt / self.env_release_s))

        want = 20.0 * math.log10(self.target / max(self.env, 1e-6))
        cap = 20.0 * math.log10(max(self.ceiling / max(self.noise, 1e-6), 1e-6))
        want = min(want, cap, self.max_gain_db)
        want = max(want, self.min_gain_db)

        if self.primed:
            step = (self.rise_db_s if want > self.gain_db else self.fall_db_s) * dt
            self.gain_db += max(-step, min(step, want - self.gain_db))
        else:
            self.gain_db = want
            self.primed = True

        if abs(self.gain_db) < 0.05:
            return chunk
        scaled = np.clip(
            np.rint(samples.astype(np.float32) * (10.0 ** (self.gain_db / 20.0))),
            -32768.0,
            32767.0,
        ).astype("<i2")
        return scaled.tobytes()


def process_audio(state: ServerState, mic, block_size: int):
    """Process audio chunks from the microphone."""
    _qualification_v1.claim_realtime()
    n_channels = state.audio_input_channels

    wake_words: List[Union[MicroWakeWord, OpenWakeWord]] = []
    micro_features: Optional[MicroWakeWordFeatures] = None
    micro_inputs: List[np.ndarray] = []

    oww_features: Optional[OpenWakeWordFeatures] = None
    oww_inputs: List[np.ndarray] = []
    has_oww = False

    last_active: Optional[float] = None
    webrtc: Optional[WebRTCProcessor] = None
    wake_agc: Optional[WakeInputAgc] = None
    if state.wake_input_agc is not None:
        wake_agc = WakeInputAgc(**state.wake_input_agc)

    try:
        _LOGGER.debug("Opening audio input device: %s", mic.name)
        with mic.recorder(samplerate=16000, channels=n_channels, blocksize=block_size) as mic_in:
            while True:
                if hasattr(mic_in, "record_bytes"):
                    # Biscuit's direct device path is already mono S16LE. Keep
                    # its beamformer output byte-exact for both the wake model
                    # and command stream; float round-tripping is unnecessary.
                    channel_chunks = [mic_in.record_bytes(block_size)]
                else:
                    # Shape: (block_size, n_channels) for stereo, (block_size, 1) for mono.
                    raw = mic_in.record(block_size)  # float32, range [-1, 1]
                    mic_vol_scalar = 1.0 if state.processed_mic_input else max(
                        0.1, min(1.0, state.mic_volume / 100.0)
                    )
                    # Build per-channel byte arrays. Channel 0 is the primary
                    # microphone; channel 1 (when present) is the reference.
                    channel_chunks = []
                    for ch in range(n_channels):
                        col = raw[:, ch] if n_channels > 1 else raw.reshape(-1)
                        chunk = (np.clip(col * mic_vol_scalar, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                        channel_chunks.append(chunk)

                # The gate opens once a COMPLETE block exists, which is here and
                # not above the read: record_bytes() blocks until the next block
                # arrives, so a stamp taken before it measures the wait for the
                # producer rather than the consumer's response. Measured with it
                # in the wrong place, mean latency came out at 63.96 ms against a
                # 64.0 ms block period - the cadence, exactly, which is what gave
                # the mistake away.
                _gate = _qualification_v1.block_received(block_size / 16.0)

                # Primary channel drives WebRTC and is the byte-transparent
                # command stream sent to Home Assistant.
                audio_chunk = channel_chunks[0]
                agc = 0 if state.processed_mic_input else (state.preferences.mic_auto_gain or 0)
                ns = 0 if state.processed_mic_input else (state.preferences.mic_noise_suppression or 0)

                if agc > 0 or ns > 0:
                    if webrtc is None:
                        webrtc = WebRTCProcessor(agc_level=agc, ns_level=ns)
                    else:
                        webrtc.update_settings(agc, ns)
                    audio_chunk = webrtc.process(audio_chunk)
                    if not audio_chunk:
                        _qualification_v1.mic_progress(state, False)
                        _qualification_v1.block_done(_gate)
                        continue

                # The deployed wake model needs a different presentation level
                # from stock's proprietary classifier. Keep that calibration at
                # the model boundary: never raise the AFE/command stream or feed
                # the scaled samples back into AEC. This is a fixed model input
                # mapping, not AGC, and the explicit clip makes overflow safe.
                wake_audio_chunk = audio_chunk
                if wake_agc is not None:
                    wake_audio_chunk = wake_agc.process(audio_chunk)
                elif state.wake_input_gain != 1.0:
                    wake_samples = np.frombuffer(audio_chunk, dtype="<i2").astype(
                        np.float32
                    )
                    wake_samples = np.clip(
                        np.rint(wake_samples * state.wake_input_gain),
                        -32768.0,
                        32767.0,
                    ).astype("<i2")
                    wake_audio_chunk = wake_samples.tobytes()

                # WAKE WORD
                if (not wake_words) or (state.wake_words_changed and state.wake_words):
                    # Update list of wake word models to process
                    state.wake_words_changed = False
                    wake_words = [ww for ww in state.wake_words.values() if ww.id in state.active_wake_words]

                    # TODO: Load default stop word value from json into state and preferences missing.

                    has_oww = False
                    for idx, wake_word in enumerate(wake_words):

                        # Load default threshold from model json
                        wake_word_id = wake_word.id if hasattr(wake_word, "id") else next(iter(state.wake_words.keys()))
                        available_word = state.available_wake_words.get(wake_word_id)
                        # _LOGGER.debug("word= %s", state.available_wake_words.get(wake_word_id))
                        default_threshold = available_word.probability_cutoff if available_word else 0.7
                        _LOGGER.debug("Using default threshold %.3f for wake word '%s' from model config", default_threshold, wake_word_id)
                        # Check preferences override
                        if idx == 0:
                            old_val = state.wake_word_1_threshold
                            if state.preferences.wake_word_1_sensitivity is not None:
                                state.wake_word_1_threshold = state.preferences.wake_word_1_sensitivity
                            else:
                                state.wake_word_1_threshold = default_threshold
                            _LOGGER.debug("Wake Word 1 threshold set to %.3f (was %.3f, preferences: %s)", state.wake_word_1_threshold, old_val, state.preferences.wake_word_1_sensitivity)
                        elif idx == 1:
                            old_val = state.wake_word_2_threshold
                            if state.preferences.wake_word_2_sensitivity is not None:
                                state.wake_word_2_threshold = state.preferences.wake_word_2_sensitivity
                            else:
                                state.wake_word_2_threshold = default_threshold
                            _LOGGER.debug("Wake Word 2 threshold set to %.3f (was %.3f, preferences: %s)", state.wake_word_2_threshold, old_val, state.preferences.wake_word_2_sensitivity)

                        if isinstance(wake_word, OpenWakeWord):
                            has_oww = True

                    # Sync entity states after threshold values were updated
                    if state.satellite is not None:
                        _LOGGER.debug("Updating WebUI entities with new threshold values")

                        # Wake Word 1
                        if state.satellite.state.sensitivity_1_number_entity is not None:
                            _LOGGER.debug("  → Syncing Wake Word 1 entity to value %.3f", state.wake_word_1_threshold)
                            state.satellite.state.sensitivity_1_number_entity.sync_with_state()
                            _LOGGER.debug("  ✅ Wake Word 1 entity now has value %.3f", state.satellite.state.sensitivity_1_number_entity.value)

                        # Wake Word 2
                        if state.satellite.state.sensitivity_2_number_entity is not None:
                            _LOGGER.debug("  → Syncing Wake Word 2 entity to value %.3f", state.wake_word_2_threshold)
                            state.satellite.state.sensitivity_2_number_entity.sync_with_state()
                            _LOGGER.debug("  ✅ Wake Word 2 entity now has value %.3f", state.satellite.state.sensitivity_2_number_entity.value)

                        # Stop Word
                        if state.satellite.state.stop_sensitivity_number_entity is not None:
                            _LOGGER.debug("  → Syncing Stop Word entity to value %.3f", state.stop_word_threshold)
                            state.satellite.state.stop_sensitivity_number_entity.sync_with_state()
                            _LOGGER.debug("  ✅ Stop Word entity now has value %.3f", state.satellite.state.stop_sensitivity_number_entity.value)

                        _LOGGER.debug("All sensitivity entities synced successfully")

                        # Force push new state to connected Home Assistant instance
                        if state.satellite is not None:
                            try:
                                _LOGGER.debug("Pushing updated state values to Home Assistant")
                                for entity in [
                                    state.satellite.state.sensitivity_1_number_entity,
                                    state.satellite.state.sensitivity_2_number_entity,
                                    state.satellite.state.stop_sensitivity_number_entity,
                                ]:
                                    if entity is not None:
                                        state.broadcast([NumberStateResponse(key=entity.key, state=entity.value)])  # type: ignore[attr-defined]
                                        _LOGGER.debug("  → Pushed value %.3f for entity %d", entity.value, entity.key)
                            except Exception as e:
                                _LOGGER.debug("Could not push state (no client connected yet): %s", e)

                    # TODO: Save settings: At this moment settings are only saved when changed in the UI. Means that the default value can change while updating since its not saved in preferences.

                    if micro_features is None:
                        micro_features = MicroWakeWordFeatures()

                    if has_oww and (oww_features is None):
                        oww_features = OpenWakeWordFeatures.from_builtin()

                try:
                    # Both channels travel in one message: data=ch0 (enhanced), data2=ch1 (raw reference)
                    audio_chunk_2 = channel_chunks[1] if n_channels >= 2 else None
                    satellite = state.satellite
                    if satellite is not None and hasattr(satellite, "_is_streaming_audio"):
                        satellite.handle_audio(audio_chunk, audio_chunk_2)

                    assert micro_features is not None
                    micro_inputs.clear()
                    micro_inputs.extend(
                        micro_features.process_streaming(wake_audio_chunk)
                    )

                    if has_oww:
                        assert oww_features is not None
                        oww_inputs.clear()
                        oww_inputs.extend(
                            oww_features.process_streaming(wake_audio_chunk)
                        )

                    activated_any = False
                    for wake_word_index, wake_word in enumerate(wake_words):
                        activated = False

                        # Set dynamic threshold depending on wake word index
                        if wake_word_index == 0:
                            threshold = state.wake_word_1_threshold
                            # _LOGGER.debug("Set wake word %d probability cutoff to %.3f", wake_word_index+1, state.wake_word_1_threshold)
                        elif wake_word_index == 1:
                            threshold = state.wake_word_2_threshold
                            # _LOGGER.debug("Set wake word %d probability cutoff to %.3f", wake_word_index+1, state.wake_word_2_threshold)
                        else:
                            threshold = 0.7
                            # _LOGGER.debug("Set wake word %d probability cutoff to fallback value 0.7", wake_word_index+1)

                        if isinstance(wake_word, MicroWakeWord):
                            # No debugging when no detection
                            wake_word.debug_probabilities = False

                            # set microWakeWord cutoff
                            wake_word.probability_cutoff = threshold

                            for micro_input in micro_inputs:
                                if wake_word.process_streaming(micro_input):
                                    wake_word.debug_probabilities = True
                                    activated = True
                        elif isinstance(wake_word, OpenWakeWord):
                            for oww_input in oww_inputs:
                                for prob in wake_word.process_streaming(oww_input):
                                    if prob > threshold:
                                        _LOGGER.debug("Wake word '%s' activated (probability %.3f exceeded threshold %.3f)", wake_word.wake_word, prob, threshold)  # type: ignore[attr-defined]
                                        activated = True

                        if activated and not state.muted:
                            # Check refractory
                            now = time.monotonic()
                            if (last_active is None) or ((now - last_active) > state.refractory_seconds):
                                satellite = state.satellite
                                if satellite is not None:
                                    satellite.wakeup(wake_word)
                                else:
                                    wake_word_phrase = wake_word.wake_word  # type: ignore[union-attr]
                                    _LOGGER.info(
                                        "Wake word detected while Home Assistant is disconnected: %s",
                                        wake_word_phrase,
                                    )
                                    peripheral_api = state.peripheral_api
                                    if peripheral_api is not None:
                                        peripheral_api.emit_event_sync(
                                            LVAEvent.WAKE_WORD_DETECTED,
                                            {"wake_word": wake_word_phrase, "offline": True},
                                        )
                                last_active = now
                            activated_any = True

                    # Always process to keep state correct
                    stopped = False

                    # No debugging when no detection
                    state.stop_word.debug_probabilities = False

                    # Apply stop word sensitivity threshold
                    state.stop_word.probability_cutoff = state.stop_word_threshold
                    # _LOGGER.debug("Set stop word probability cutoff to %.3f", state.stop_word_threshold)
                    for micro_input in micro_inputs:
                        if state.stop_word.process_streaming(micro_input):
                            state.stop_word.debug_probabilities = True
                            stopped = True

                    if stopped and (state.stop_word.id in state.active_wake_words) and not state.muted:
                        _LOGGER.debug("Stop word detected")
                        satellite = state.satellite
                        if satellite is not None:
                            satellite.stop()
                    _qualification_v1.mic_progress(state, bool(wake_words) and micro_features is not None and (not has_oww or oww_features is not None))
                    # Everything for this block is done: features, every wake
                    # word, the stop word, and any wakeup()/stop() dispatched
                    # above. `dispatched` separates a turn from an idle block.
                    _qualification_v1.block_done(_gate, dispatched=(activated_any or stopped))
                except Exception:  # pylint: disable=broad-except
                    _qualification_v1.mic_progress(state, False)
                    _qualification_v1.block_done(_gate)
                    _LOGGER.exception("Unexpected error handling audio")
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("Unexpected error processing audio")
        # process_audio runs in a daemon thread, where sys.exit() raises
        # SystemExit that threading catches to end *only this thread*.  The
        # process then stays up looking perfectly healthy - Home Assistant
        # connected, every entity answering, commands accepted - while nothing
        # consumes the microphone.  The device is permanently deaf and says so
        # nowhere.  Observed live: the wake counter froze while the capture
        # chain and API both looked fine.
        #
        # Take the whole process down instead.  supervise-daemon runs this with
        # respawn_delay=5 and respawn_max=0, so it comes straight back with a
        # fresh capture command, which is what the original sys.exit() intended.
        logging.shutdown()
        os._exit(1)


# -----------------------------------------------------------------------------


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
