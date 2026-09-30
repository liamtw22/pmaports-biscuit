# SPDX-License-Identifier: Apache-2.0
#
# MODIFIED FILE. This is a changed copy of
# linux_voice_assistant/peripheral_api.py from OHF-Voice/linux-voice-assistant
# at commit b0c53c41c11e,
#   https://github.com/OHF-Voice/linux-voice-assistant
# licensed under the Apache License 2.0 (LICENSES/Apache-2.0.txt).
# Changed by liamtw22 and contributors, 2026, for the Amazon Echo Dot
# (2nd gen) postmarketOS port. The upstream copyright is unchanged.
# Changes: peripheral entity registration and state sync with Home Assistant
#   (numbers, switches, sensors, selects), mute sync, ring configuration,
#   reconnect handling, and the root-only Unix-socket transport.
"""WebSocket peripheral API.

Bridges LVA state to a separate peripheral container (LEDs, buttons, HAT boards).

Protocol (JSON over WebSocket):
  Events  (LVA → peripheral): {"event": "<name>", "data": {...}}
  Commands (peripheral → LVA): {"command": "<name>", "data": {...}}
  Snapshot (on connect):       {"event": "snapshot", "data": {...}}

Feedback events emitted by LVA
-------------------------------
  wake_word_detected
  listening
  stt_text        data: {"text": str}
              Emitted when HA returns the recognised speech transcript.
              Use this to display what the user said on a screen or LED ticker.
  thinking
  tts_text        data: {"text": str}
              Emitted when HA returns the assistant's response text, just
              before TTS audio begins playing.
              Use this to display the assistant's reply on a screen.
  tts_speaking
  tts_finished
  pipeline_error  data: {"reason": <str>}
              Emitted when the voice pipeline reports an error (STT failure,
              intent error, etc.). NOT emitted when HA disconnects — that
              uses the separate ``disconnected`` event below.
              Peripheral containers should show a brief red error animation
              (e.g. 3 red flashes then off) and then return to idle.
  disconnected
              Emitted when the HA TCP connection is lost.
              Connected leds with peripheral containers should
              show a red twinkle / "no connection" animation and keep retrying
              until they see a ``zeroconf`` event with status "connected".
              NOTE: if LVA itself is not running the peripheral container will
              see a WebSocket connection failure on its end — that is also
              a "disconnected" condition to handle with the same animation.
  idle
  muted                 data: {"muted": true/false}
  timer_ticking   data: {"id": str, "name": str, "total_seconds": int, "seconds_left": int}
  timer_updated   data: {"id": str, "name": str, "total_seconds": int, "seconds_left": int}
  timer_ringing   data: {"id": str, "name": str, "total_seconds": int, "seconds_left": int}
  media_player_playing  Emitted when HA sends music/media to the music_player
                        (non-announcement playback). Not emitted for TTS or
                        voice pipeline announcements — those use tts_speaking.
  volume_changed        data: {"volume": 0.0â€“1.0}
  volume_muted          data: {"muted": true/false}
  zeroconf              data: {"status": "getting_started" | "connected"}
  light_command         data: {"object_id": str, "state": bool, "brightness": float,
                              "red": float, "green": float, "blue": float, "effect": str}
              Fires when HA changes a Light entity that a peripheral
              previously registered via register_light. The peripheral
              matches on object_id and applies the new state. The
              effect names are those the peripheral declared at
              registration; e.g. "Voice Assistant" runs the pipeline
              animations.

Commands accepted from the peripheral container
------------------------------------------------
  start_listening
  stop_pipeline     Abort the active voice pipeline at any phase — listening,
                    thinking, speaking or wake word active. Calls satellite.stop() which
                    cleans up STT streaming, sends VoiceAssistantAnnounceFinished
                    to HA, unducking music, and emits idle to peripherals.
  mute_mic
  unmute_mic
  volume_up
  volume_down
  set_volume        data: {"volume": 0.0â€“1.0}
  stop_timer_ringing
  pause_media_player
  resume_media_player
  stop_media_player
  button_single_press
  button_double_press
  button_triple_press
  button_long_press
  register_light    data: {"name": str, "object_id": str, "effects": [str],
                           "supports_rgb": bool, "supports_brightness": bool}
              The peripheral declares an LED Light it wants exposed in
              HA. LVA creates a matching ESPHome Light entity (visible
              as light.<satellite>_<object_id>) and routes HA changes
              back to the peripheral as light_command events. Send
              after connecting. A repeat registration for the same
              object_id keeps the entity and its state; if its name,
              effects or metadata changed, the entity takes them and HA
              re-reads the entity list (see "Changing an entity" below).
  register_number, register_switch, register_sensor, register_binary_sensor
              BISCUIT. A Number (a Select when "options" is given), Switch,
              read-only Sensor, or read-only Binary Sensor. Same repeat rules
              as register_light, and "initial_value" on a repeat is the value
              the peripheral holds NOW: it replaces LVA's copy and is pushed
              to HA. Optional presentation metadata on any registration:
              entity_category ("none", "config", "diagnostic"), device_class,
              disabled_by_default; numbers also take unit_of_measurement,
              step and mode ("auto", "box", "slider"); sensors state_class.
  registrations_done
              BISCUIT. Everything the peripheral registers has been sent.
              LVA does not serve the ESPHome API until this arrives or a
              bounded wait ends (--peripheral-startup-wait), so HA's first
              enumeration after a start is complete. Without it HA listed the
              entities before they existed and deleted them from its registry.
  entity_state      data: {"object_id": str, "value": float | str | bool}
              BISCUIT. The value a registered Number, Select or Switch
              actually holds, after a change made somewhere else or after the
              peripheral refused or adjusted one from HA. Pushed to every
              connected API client. object_id "mute" sets LVA's own
              microphone mute switch silently - no earcon - which is how the
              peripheral reports the hardware mute state on connect.
  binary_sensor_state  data: {"object_id": str, "value": bool | null}
              BISCUIT. null is unknown.
  light_state       data: light_command's shape
              BISCUIT. The peripheral's own record of a registered Light, so a
              restart on either side shows what the ring is really doing.

Changing an entity
------------------
  HA reads names, option lists and effects only when it enumerates, so a
  change to one needs HA to reconnect. LVA does that itself, once: changes are
  debounced, reconnects are at least LATE_ENTITY_RECONNECT_COOLDOWN_S apart
  (a change inside the cooldown is deferred, never dropped), and none happens
  if HA's last enumeration already included the change. It is held while a
  pipeline runs or media or an announcement plays, and does not stop playback
  or show the ring's disconnected state. Values never need a reconnect - they
  are pushed as states.

  register_button
              The peripheral declares that it has physical buttons and
              wants a Button Press event entity exposed in HA. LVA
              creates a ButtonEventSensorEntity (visible as
              event.<satellite>_button_press_event) that fires
              single_press, double_press, triple_press, and long_press
              events to Home Assistant when the corresponding
              button_* commands are sent. Send once after connecting;
              duplicate registrations are ignored.
"""

from __future__ import annotations

from . import peripheral_transport as _peripheral_transport

from . import qualification_v1 as _qualification_v1
import asyncio
import json
import logging
import math
import time
from dataclasses import asdict
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set

from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # pylint: disable=no-name-in-module
    MediaPlayerStateResponse,
    NumberStateResponse,
    SelectStateResponse,
    SwitchStateResponse,
)
from aioesphomeapi.model import MediaPlayerState  # type: ignore[import]

if TYPE_CHECKING:
    from .models import ServerState

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public enumerations
# ---------------------------------------------------------------------------


class LVAEvent(str, Enum):
    """Events broadcast from LVA to peripheral clients."""

    WAKE_WORD_DETECTED = "wake_word_detected"
    LISTENING = "listening"
    STT_TEXT = "stt_text"
    THINKING = "thinking"
    TTS_TEXT = "tts_text"
    TTS_SPEAKING = "tts_speaking"
    TTS_FINISHED = "tts_finished"
    PIPELINE_ERROR = "pipeline_error"
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    MUTED = "muted"
    TIMER_TICKING = "timer_ticking"
    TIMER_UPDATED = "timer_updated"
    TIMER_RINGING = "timer_ringing"
    MEDIA_PLAYER_PLAYING = "media_player_playing"
    VOLUME_CHANGED = "volume_changed"
    VOLUME_MUTED = "volume_muted"
    ZEROCONF = "zeroconf"
    LIGHT_COMMAND = "light_command"


class LVACommand(str, Enum):
    """Commands accepted from peripheral clients."""

    START_LISTENING = "start_listening"
    STOP_PIPELINE = "stop_pipeline"
    MUTE_MIC = "mute_mic"
    UNMUTE_MIC = "unmute_mic"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    SET_VOLUME = "set_volume"
    STOP_TIMER_RINGING = "stop_timer_ringing"
    STOP_MEDIA_PLAYER = "stop_media_player"
    PAUSE_MEDIA_PLAYER = "pause_media_player"
    RESUME_MEDIA_PLAYER = "resume_media_player"
    BUTTON_SINGLE_PRESS = "button_single_press"
    BUTTON_DOUBLE_PRESS = "button_double_press"
    BUTTON_TRIPLE_PRESS = "button_triple_press"
    BUTTON_LONG_PRESS = "button_long_press"
    REGISTER_LIGHT = "register_light"
    REGISTER_BUTTON = "register_button"
    REGISTER_NUMBER = "register_number"      # BISCUIT
    REGISTER_SENSOR = "register_sensor"      # BISCUIT
    REGISTER_SWITCH = "register_switch"      # BISCUIT
    REGISTER_BINARY_SENSOR = "register_binary_sensor"  # BISCUIT
    REGISTRATIONS_DONE = "registrations_done"          # BISCUIT
    RING_CONFIG_STATE = "ring_config_state"
    SENSOR_STATE = "sensor_state"            # BISCUIT
    BINARY_SENSOR_STATE = "binary_sensor_state"        # BISCUIT
    ENTITY_STATE = "entity_state"            # BISCUIT
    LIGHT_STATE = "light_state"              # BISCUIT


# ---------------------------------------------------------------------------
# Registration metadata (BISCUIT)
# ---------------------------------------------------------------------------

_CHOICES = {
    "entity_category": ("none", "config", "diagnostic"),
    "mode": ("auto", "box", "slider"),
    "state_class": ("measurement", "total", "total_increasing"),
}


def entity_meta(data: Dict[str, Any]) -> Dict[str, Any]:
    """The optional presentation fields of a registration, validated.

    Anything malformed is dropped rather than refused: a registration that
    names a strange unit is still worth creating, just without the unit.
    """
    meta: Dict[str, Any] = {}
    for key, choices in _CHOICES.items():
        if data.get(key) in choices:
            meta[key] = data[key]
    for key in ("device_class", "unit_of_measurement"):
        value = data.get(key)
        if isinstance(value, str) and value and len(value) <= 64:
            meta[key] = value
    if isinstance(data.get("disabled_by_default"), bool):
        meta["disabled_by_default"] = data["disabled_by_default"]
    step = data.get("step")
    if (isinstance(step, (int, float)) and not isinstance(step, bool)
            and math.isfinite(step) and step > 0):
        meta["step"] = float(step)
    return meta


def _description(spec: Any) -> Dict[str, Any]:
    """Everything HA reads at enumeration: a registration minus its value."""
    described = asdict(spec)
    described.pop("initial_value", None)
    described["meta"] = getattr(spec, "meta", {})
    return described


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class PeripheralAPIServer:
    """
    WebSocket server that bridges LVA state to a peripheral container.

    Usage
    -----
    1. Construct in ``__main__.main()``.
    2. ``peripheral_api.set_state(state)`` once ``ServerState`` exists.
    3. ``await peripheral_api.start()`` inside the running event loop.
    4. Call ``emit_event_sync()`` from any thread (mpv callbacks, audio thread).
    """

    DEFAULT_VOLUME_STEP: float = 0.05
    # Registrations arrive as a burst from a peripheral. Give the burst time
    # to finish, then reconnect once so ESPHome enumerates the complete set.
    LATE_ENTITY_RECONNECT_DEBOUNCE_S: float = 2.0
    LATE_ENTITY_RECONNECT_COOLDOWN_S: float = 60.0
    # How often a reconnect held back by playback or a pipeline looks again.
    LATE_ENTITY_RECONNECT_BUSY_POLL_S: float = 5.0

    def __init__(
        self,
        volume_step: float = DEFAULT_VOLUME_STEP,
    ) -> None:
        self._volume_step = volume_step

        self._clients: Set[Any] = set()
        self._state: Optional[ServerState] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Any = None

        # Last conversation exchange — sent in the snapshot to newly-connecting clients
        self._last_stt_text: Optional[str] = None
        self._last_tts_text: Optional[str] = None

        # Current event state — replayed to newly-connecting clients so they can
        # show the correct animation immediately without waiting for the next event.
        # Only "state" events are tracked (pipeline, timer, media, muted, idle).
        # Transient/informational events (stt_text, tts_text, volume_changed, etc.)
        # are not tracked because they carry no ongoing visual state.
        self._current_state: Optional[LVAEvent] = None
        self._current_state_data: Optional[Dict[str, Any]] = None

        # Safeguarded HA reconnect trigger for late entity registrations.
        self._pending_entity_reconnect_task: Optional[asyncio.Task] = None
        # Minus infinity, NOT 0.0. time.monotonic() counts from boot and LVA
        # starts about 24 s in, so a zero here made the 60 s cooldown look
        # active for the first minute of every boot - and that is exactly when
        # the one reconnect that mattered was due, so it was always skipped.
        self._last_ha_reconnect_at: float = float("-inf")
        self._last_entity_change_at: float = float("-inf")
        self._last_entity_change = ("", "")
        # Bumped whenever what HA would enumerate changes. Each connection
        # records the value it enumerated at (satellite._listed_generation), so
        # a reconnect is only forced on a connection that has not seen it.
        self.entity_generation = 0
        # Set by registrations_done; __main__ holds the ESPHome API until then.
        self._registrations_done = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_state(self, state: "ServerState") -> None:
        """Attach the shared ``ServerState`` so commands can read/mutate it."""
        self._state = state

    async def start(self) -> None:
        """Start the root-peer-only Unix WebSocket; never fall back to TCP."""
        if self._server is not None:
            raise RuntimeError("peripheral_already_started")
        self._loop = asyncio.get_running_loop()
        self._server = await _peripheral_transport.listen(self._handle_client)
        _LOGGER.info("Peripheral API ready: root-only Unix socket")

    async def wait_for_registrations(self, timeout: float) -> bool:
        """Wait for a peripheral's registrations_done, at most `timeout` s.

        False on timeout - no agent installed, or one that crashed - and the
        caller serves anyway: HA without the peripheral's entities beats no HA
        at all, and a late registration still reaches HA through one
        reconnect.
        """
        try:
            await asyncio.wait_for(self._registrations_done.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        if self._pending_entity_reconnect_task is not None:
            self._pending_entity_reconnect_task.cancel()
            self._pending_entity_reconnect_task = None
        if self._server is not None:
            await self._server.stop()
            self._server = None
            _LOGGER.info("Peripheral API stopped")

    # ------------------------------------------------------------------
    # Client handling
    # ------------------------------------------------------------------

    async def _handle_client(self, websocket: Any) -> None:
        # Authenticate before snapshots, registration, command handling or client tracking.
        if not _peripheral_transport.authorized_websocket(websocket):
            await websocket.close(code=1008, reason="local_peer_required")
            return
        if len(self._clients) >= _peripheral_transport.MAX_CLIENTS:
            await websocket.close(code=1013, reason="client_limit")
            return
        self._clients.add(websocket)
        try:
            await self._send_snapshot(websocket)
            async for raw in websocket:
                if not _peripheral_transport.valid_command(raw):
                    await websocket.close(code=1007, reason="invalid_command")
                    break
                await self._dispatch_command(raw)
        except Exception:
            _LOGGER.debug("Peripheral client ended")
        finally:
            self._clients.discard(websocket)

    async def _send_snapshot(self, websocket: Any) -> None:
        """Push current LVA state to a newly connected peripheral client."""
        state = self._state
        if state is None:
            return

        payload = json.dumps(
            {
                "event": "snapshot",
                "data": {
                    "muted": state.muted,
                    "volume": round(state.volume, 3),
                    "volume_muted": state.volume == 0.0,
                    "ha_connected": state.connected,
                    "peripheral_transport": _peripheral_transport.configuration(self._clients),
                    "qualification_v1": _qualification_v1.snapshot(state),
                    "last_stt_text": self._last_stt_text,
                    "last_tts_text": self._last_tts_text,
                },
            }
        )
        try:
            await websocket.send(payload)
        except Exception:  # pylint: disable=broad-except
            return

        # Replay the current event state so the client immediately shows the
        # right animation — e.g. a timer ticking animation when reconnecting
        # mid-timer, or the muted indicator when reconnecting while muted.
        current_state = self._current_state
        if current_state is None:
            return

        if current_state == LVAEvent.DISCONNECTED and state.connected:
            return

        state_payload: Dict[str, Any] = {"event": current_state.value}
        if self._current_state_data:
            state_payload["data"] = self._current_state_data
        try:
            await websocket.send(json.dumps(state_payload))
        except Exception:  # pylint: disable=broad-except
            pass

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    async def _dispatch_command(self, raw: str) -> None:
        """Parse and execute a JSON command from the peripheral container."""
        try:
            msg: Dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.warning("Peripheral: invalid JSON")
            return

        command: str = msg.get("command", "")
        if not command:
            return

        _LOGGER.debug("Peripheral command received: %s", command)

        state = self._state
        if state is None:
            return

        satellite = state.satellite

        if command == LVACommand.RING_CONFIG_STATE:
            self._ring_config_state(msg.get("data") or {}, satellite)

        elif command == LVACommand.START_LISTENING:
            if satellite is None or state.muted:
                return
            satellite.start_listening()  # plays sound, then starts pipeline

        elif command == LVACommand.STOP_PIPELINE:
            # Stops the voice pipeline at any active phase:
            # listening, thinking, speaking or wake word active.
            if satellite is not None:
                satellite.stop()

        elif command == LVACommand.MUTE_MIC:
            if state.muted:
                return
            if satellite is not None:
                satellite._set_muted(True)  # pylint: disable=protected-access
                await self._push_mute_switch(satellite, muted=True)
            else:
                # Physical mute must remain authoritative even when Home
                # Assistant is unreachable and there is no satellite protocol
                # instance to own the state transition.
                state.muted = True
                await self.emit_event(LVAEvent.MUTED, {"muted": True})
                state.tts_player.stop()
                state.stop_word.is_active = False  # type: ignore[attr-defined]
                state.tts_player.play(state.mute_sound)

        elif command == LVACommand.UNMUTE_MIC:
            if not state.muted:
                return
            if satellite is not None:
                satellite._set_muted(False)  # pylint: disable=protected-access
                await self._push_mute_switch(satellite, muted=False)
            else:
                state.muted = False
                await self.emit_event(LVAEvent.MUTED, {"muted": False})
                state.tts_player.play(state.unmute_sound)
                await self.emit_event(LVAEvent.IDLE)

        elif command in (LVACommand.VOLUME_UP, LVACommand.VOLUME_DOWN):
            delta = self._volume_step if command == LVACommand.VOLUME_UP else -self._volume_step
            new_vol = max(0.0, min(1.0, state.volume + delta))

            # biscuit-audio owns the appliance master.  VolumeChanged is
            # relayed to it by biscuit-va-leds, which changes the codec and
            # biscuit-dsp loudness curve together.  Do not also attenuate mpv:
            # that would apply the same user volume twice.

            if state.media_player_entity is not None:
                state.media_player_entity.volume = new_vol
                state.media_player_entity.previous_volume = new_vol
                if new_vol > 0.0:
                    state.media_player_entity.muted = False

                # Push the new volume to HA so its media player entity updates in real time
                self._broadcast(
                    [
                        MediaPlayerStateResponse(
                            key=state.media_player_entity.key,
                            state=state.media_player_entity.state,
                            volume=new_vol,
                            muted=state.media_player_entity.muted,
                        )
                    ]
                )

            # persist_volume also emits VOLUME_CHANGED via models.py
            state.persist_volume(new_vol)

        elif command == LVACommand.SET_VOLUME:
            data = msg.get("data", {})
            volume = data.get("volume")
            if not isinstance(volume, (int, float)):
                _LOGGER.warning("Peripheral: invalid volume in set_volume command: %s", volume)
                return

            new_vol = max(0.0, min(1.0, float(volume)))


            if state.media_player_entity is not None:
                state.media_player_entity.volume = new_vol
                state.media_player_entity.previous_volume = new_vol
                # Raising the physical volume is an explicit unmute.  Without
                # this, an HA media mute followed by the hardware volume-up
                # button could leave the entity reporting muted indefinitely.
                if new_vol > 0.0:
                    state.media_player_entity.muted = False

                # Push the new volume to HA so its media player entity updates in real time
                self._broadcast(
                    [
                        MediaPlayerStateResponse(
                            key=state.media_player_entity.key,
                            state=state.media_player_entity.state,
                            volume=new_vol,
                            muted=state.media_player_entity.muted,
                        )
                    ]
                )

            # This updates LVA/HA state and emits VOLUME_CHANGED.  The bridge
            # sees that the device is already at this volume and suppresses the
            # echo; mpv remains at unity.
            state.persist_volume(new_vol)

        elif command == LVACommand.STOP_TIMER_RINGING:
            if satellite is None:
                return
            if getattr(satellite, "_timer_finished", False):
                satellite._timer_finished = False  # pylint: disable=protected-access
                state.active_wake_words.discard(state.stop_word.id)
                state.tts_player.stop()
                satellite.unduck()
                await self.emit_event(LVAEvent.IDLE)

        elif command == LVACommand.STOP_MEDIA_PLAYER:
            state.music_player.stop()
            if state.media_player_entity is not None:

                state.media_player_entity.state = MediaPlayerState.IDLE
                self._broadcast([self._create_media_player_response(MediaPlayerState.IDLE)])

        elif command == LVACommand.PAUSE_MEDIA_PLAYER:
            state.music_player.pause()
            if state.media_player_entity is not None:
                state.media_player_entity.state = MediaPlayerState.PAUSED
                self._broadcast([self._create_media_player_response(MediaPlayerState.PAUSED)])

        elif command == LVACommand.RESUME_MEDIA_PLAYER:
            state.music_player.resume()
            if state.media_player_entity is not None:
                state.media_player_entity.state = MediaPlayerState.PLAYING
                self._broadcast([self._create_media_player_response(MediaPlayerState.PLAYING)])

        elif command == LVACommand.BUTTON_SINGLE_PRESS:
            if state.button_event_sensor_entity is not None:
                state.button_event_sensor_entity.update_state("single_press")
                self._broadcast([state.button_event_sensor_entity._get_state_message()])  # pylint: disable=protected-access

        elif command == LVACommand.BUTTON_DOUBLE_PRESS:
            state.tts_player.play(state.button_double_press_sound)
            if state.button_event_sensor_entity is not None:
                state.button_event_sensor_entity.update_state("double_press")
                self._broadcast([state.button_event_sensor_entity._get_state_message()])  # pylint: disable=protected-access

        elif command == LVACommand.BUTTON_TRIPLE_PRESS:
            state.tts_player.play(state.button_triple_press_sound)
            if state.button_event_sensor_entity is not None:
                state.button_event_sensor_entity.update_state("triple_press")
                self._broadcast([state.button_event_sensor_entity._get_state_message()])  # pylint: disable=protected-access

        elif command == LVACommand.BUTTON_LONG_PRESS:
            state.tts_player.play(state.button_long_press_sound)
            if state.button_event_sensor_entity is not None:
                state.button_event_sensor_entity.update_state("long_press")
                self._broadcast([state.button_event_sensor_entity._get_state_message()])  # pylint: disable=protected-access

        elif command == LVACommand.REGISTER_SENSOR:
            self._register_sensor(msg.get("data") or {}, satellite)

        # BISCUIT: the three state pushes below keep their value on the state
        # as well as the entity, like peripheral_number_values. Entities only
        # materialise once an ESPHome connection exists, and the peripheral
        # sends these at start-up, before Home Assistant is let in - so a value
        # kept only on the entity was dropped exactly when it mattered, and the
        # ring came back "off" after every LVA restart.
        elif command == LVACommand.SENSOR_STATE:
            data = msg.get("data") or {}
            object_id = str(data.get("object_id", ""))
            value = data.get("value")
            if (object_id and isinstance(value, (int, float))
                    and not isinstance(value, bool) and math.isfinite(value)):
                self._kept(state, "peripheral_sensor_values")[object_id] = float(value)
                entity = state.peripheral_sensor_entities.get(object_id)
                if entity is not None:
                    entity.update_state(float(value))
                    self._broadcast([entity._get_state_message()])  # pylint: disable=protected-access

        elif command == LVACommand.BINARY_SENSOR_STATE:
            data = msg.get("data") or {}
            object_id = str(data.get("object_id", ""))
            value = data.get("value")
            if object_id and (value is None or isinstance(value, bool)):
                self._kept(state, "peripheral_binary_sensor_values")[object_id] = value
                entity = getattr(state, "peripheral_binary_sensor_entities", {}).get(object_id)
                if entity is not None:
                    entity.update_state(value)
                    self._broadcast([entity._get_state_message()])  # pylint: disable=protected-access

        elif command == LVACommand.ENTITY_STATE:
            await self._entity_state(msg.get("data") or {})

        elif command == LVACommand.LIGHT_STATE:
            data = msg.get("data") or {}
            object_id = str(data.get("object_id", ""))
            if object_id:
                kept = self._kept(state, "peripheral_light_states")
                kept[object_id] = dict(kept.get(object_id, {}), **data)
                light = state.led_light_entities.get(object_id)
                if light is not None:
                    light.apply_state(data)
                    self._broadcast([light._state_response()])  # pylint: disable=protected-access

        elif command == LVACommand.REGISTER_NUMBER:
            self._register_number(msg.get("data") or {}, satellite)
        elif command == LVACommand.REGISTER_SWITCH:
            self._register_switch(msg.get("data") or {}, satellite)
        elif command == LVACommand.REGISTER_BINARY_SENSOR:
            self._register_binary_sensor(msg.get("data") or {}, satellite)

        elif command == LVACommand.REGISTER_LIGHT:
            self._register_light(msg.get("data") or {}, satellite)

        elif command == LVACommand.REGISTER_BUTTON:
            self._register_button(satellite)

        elif command == LVACommand.REGISTRATIONS_DONE:
            if not self._registrations_done.is_set():
                _LOGGER.info(
                    "Peripheral registrations complete: %d lights, %d numbers/selects, "
                    "%d switches, %d sensors, %d binary sensors",
                    len(state.pending_lights), len(state.pending_numbers),
                    len(state.pending_switches), len(state.pending_sensors),
                    len(getattr(state, "pending_binary_sensors", [])))
            self._registrations_done.set()

    @staticmethod
    def _kept(state: Any, name: str) -> Dict[str, Any]:
        """A dict of values kept on the state; models.py has no field for it."""
        kept = getattr(state, name, None)
        if kept is None:
            kept = {}
            setattr(state, name, kept)
        return kept

    def _broadcast(self, messages: Iterable[Any]) -> None:
        """Push state changes to EVERY connected ESPHome client.

        Not just state.satellite: that is Home Assistant's connection, and a
        second client - a diagnostic tool, a second HA - would otherwise keep
        showing whatever it was told when it connected.
        """
        state = self._state
        messages = list(messages)
        if state is not None and messages:
            state.broadcast(messages)

    async def _entity_state(self, data: Dict[str, Any]) -> None:
        """The value a peripheral Number, Select or Switch actually holds."""
        state = self._state
        object_id = str(data.get("object_id", ""))
        value = data.get("value")
        if state is None or not object_id:
            return
        if object_id == "mute":
            if isinstance(value, bool):
                await self._sync_mute(value)
            return
        if any(spec.object_id == object_id for spec in state.pending_switches):
            if isinstance(value, bool):
                self._set_switch_value(object_id, value)
            return
        self._set_number_value(object_id, value)

    def _set_number_value(self, object_id: str, raw: Any) -> bool:
        """Store a Number/Select value and push it to HA if it changed."""
        state = self._state
        spec = next((s for s in state.pending_numbers if s.object_id == object_id), None)
        if spec is None or raw is None or isinstance(raw, bool):
            return False
        if spec.options:
            value: Any = str(raw)
            if value not in spec.options:
                _LOGGER.debug("Ignoring %r for %s: not one of its options", value, object_id)
                return False
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(value):
                return False
            value = max(spec.min_value, min(spec.max_value, value))
        if state.peripheral_number_values.get(object_id) == value:
            return False
        state.peripheral_number_values[object_id] = value
        entity = getattr(state, "peripheral_number_entities", {}).get(object_id)
        if entity is not None:
            entity.sync_with_state()
            self._broadcast([SelectStateResponse(key=entity.key, state=value) if spec.options
                             else NumberStateResponse(key=entity.key, state=value)])
        return True

    def _set_switch_value(self, object_id: str, value: bool) -> bool:
        state = self._state
        if state.peripheral_switch_values.get(object_id) == value:
            return False
        state.peripheral_switch_values[object_id] = value
        entity = getattr(state, "peripheral_switch_entities", {}).get(object_id)
        if entity is not None:
            self._broadcast([SwitchStateResponse(key=entity.key, state=value)])
        return True

    async def _sync_mute(self, muted: bool) -> None:
        """Adopt the hardware mute state without the mute earcon.

        MUTE_MIC is a user action and plays the sound. This is bookkeeping: LVA
        starts believing it is unmuted, and an LVA restart while the mics were
        muted left HA showing Mute off - and let the button start listening
        into muted microphones.
        """
        state = self._state
        if state.muted == muted:
            return
        state.muted = muted
        satellite = state.satellite
        if muted and satellite is not None:
            satellite._is_streaming_audio = False  # pylint: disable=protected-access
        entity = state.mute_switch_entity
        if entity is not None:
            entity._switch_state = muted  # pylint: disable=protected-access
            self._broadcast([SwitchStateResponse(key=entity.key, state=muted)])
        _LOGGER.info("Microphone mute synced from the peripheral: %s", muted)
        await self.emit_event(LVAEvent.MUTED, {"muted": muted})

    def _ring_config_state(self, data: Dict[str, Any], satellite: Any) -> None:
        """Reflect local controls in HA without echoing hardware commands."""
        state = self._state
        if state is None or not isinstance(data, dict):
            return
        brightness = data.get("brightness")
        autodim = data.get("autodim")
        palette = data.get("palette")
        if (isinstance(brightness, bool) or not isinstance(brightness, (int, float))
                or not 0 <= brightness <= 1 or not isinstance(autodim, bool)):
            return
        options = next((spec.options for spec in state.pending_numbers
                        if spec.object_id == "ring_palette"), [])
        if palette not in (options or []):
            return
        accents=data.get('accents')
        if accents is not None:
            try:
                colours=accents['colours']; levels=accents['levels']; enabled=accents['enabled']
                if len(colours)!=2 or len(levels)!=2 or len(enabled)!=2 or any(len(c)!=3 or any(isinstance(v,bool) or not isinstance(v,int) or not 0<=v<=255 for v in c) for c in colours) or any(isinstance(v,bool) or not isinstance(v,int) or not 0<=v<=255 for v in levels) or any(not isinstance(v,bool) for v in enabled):
                    return
            except (KeyError,TypeError): return
        state.peripheral_ring_accents=accents
        state.peripheral_ring_brightness = brightness
        state.peripheral_switch_values["ring_autodim"] = autodim
        state.peripheral_number_values["ring_palette"] = palette
        messages = []
        light = state.led_light_entities.get("ring")
        if light is not None:
            light.brightness = brightness
            messages.append(light._state_response())
        switch = getattr(state, "peripheral_switch_entities", {}).get("ring_autodim")
        if switch is not None:
            messages.append(SwitchStateResponse(key=switch.key, state=autodim))
        select = getattr(state, "peripheral_number_entities", {}).get("ring_palette")
        if select is not None:
            select.sync_with_state()
            messages.append(SelectStateResponse(key=select.key, state=palette))
        if accents is not None:
            for i in range(2):
                light=state.led_light_entities.get('ring_colour_'+str(i+2))
                if light is not None:
                    light.red,light.green,light.blue=[v/255 for v in colours[i]]
                    light.brightness=levels[i]/255
                    light.is_on=enabled[i]
                    messages.append(light._state_response())
        self._broadcast(messages)

    # ------------------------------------------------------------------
    # Registration (BISCUIT: repeatable, see "Changing an entity" above)
    # ------------------------------------------------------------------

    def _upsert(self, kind: str, specs: List[Any], spec: Any) -> Optional[Any]:
        """Add a registration, or take a repeat of one.

        Returns the previous spec for a repeat whose description changed, the
        new spec itself for a new entity, and None for a repeat that changed
        nothing HA enumerates. Either of the first two means HA must re-read
        the entity list.
        """
        for index, old in enumerate(specs):
            if old.object_id != spec.object_id:
                continue
            if _description(old) == _description(spec):
                return None
            specs[index] = spec
            _LOGGER.info("%s re-registered with changes: %s", kind.capitalize(), spec.object_id)
            return old
        specs.append(spec)
        return spec

    def _register_sensor(self, data: Dict[str, Any], satellite: Any) -> None:
        """Register a read-only measurement. BISCUIT addition."""
        from .models import SensorRegistration  # local import to avoid a cycle

        object_id = str(data.get("object_id", "")).strip()
        if not object_id:
            _LOGGER.warning("register_sensor without object_id; ignoring")
            return

        state = self._state
        if state is None:
            return

        spec = SensorRegistration(
            name=str(data.get("name", "Sensor")),
            object_id=object_id,
            unit_of_measurement=str(data.get("unit_of_measurement", "")),
            device_class=str(data.get("device_class", "")),
            accuracy_decimals=int(data.get("accuracy_decimals", 0)),
            icon=str(data.get("icon", "mdi:gauge")),
        )
        spec.meta = entity_meta(data)
        changed = self._upsert("sensor", state.pending_sensors, spec)
        if changed is None:
            return
        entity = state.peripheral_sensor_entities.get(object_id)
        if entity is not None:
            entity.name, entity.icon = spec.name, spec.icon
            entity.unit_of_measurement = spec.unit_of_measurement
            entity.device_class = spec.device_class
            entity.accuracy_decimals = spec.accuracy_decimals
            entity.meta = dict(spec.meta)
        elif changed is spec:
            _LOGGER.info("Sensor registered: %s (%s)", object_id,
                         data.get("unit_of_measurement") or "no unit")

        if satellite is not None:
            satellite.register_pending_sensors()

        self._entities_changed("sensor", object_id)

    def _register_binary_sensor(self, data: Dict[str, Any], satellite: Any) -> None:
        """Register a read-only on/off state. BISCUIT addition.

        There is no BinarySensorRegistration in models.py, and models.py is not
        ours to change, so the sensor dataclass carries it and the list lives
        on the state under its own name - the precedent is
        peripheral_switch_entities in satellite.py.
        """
        from .models import SensorRegistration  # local import to avoid a cycle

        object_id = str(data.get("object_id", "")).strip()
        state = self._state
        if not object_id or state is None:
            return
        if not hasattr(state, "pending_binary_sensors"):
            state.pending_binary_sensors = []
            state.peripheral_binary_sensor_entities = {}

        spec = SensorRegistration(
            name=str(data.get("name", "Binary sensor")),
            object_id=object_id,
            device_class=str(data.get("device_class", "")),
            icon=str(data.get("icon", "")),
        )
        spec.meta = entity_meta(data)
        changed = self._upsert("binary sensor", state.pending_binary_sensors, spec)
        if changed is None:
            return
        entity = state.peripheral_binary_sensor_entities.get(object_id)
        if entity is not None:
            entity.name, entity.icon = spec.name, spec.icon
            entity.device_class = spec.device_class
            entity.meta = dict(spec.meta)
        elif changed is spec:
            _LOGGER.info("Binary sensor registered: %s", object_id)

        if satellite is not None:
            satellite.register_pending_binary_sensors()

        self._entities_changed("binary sensor", object_id)

    def _register_number(self, data: Dict[str, Any], satellite: Any) -> None:
        """Register a Number declared by a peripheral. BISCUIT addition.

        A repeat keeps the entity. Its value is the peripheral's current one -
        the peripheral builds its registrations from its files on every
        connect - so it replaces LVA's copy and goes to HA as a state. That
        used to be a setdefault, which froze every value at whatever the
        peripheral read when it first started.
        """
        from .models import NumberRegistration  # local import to avoid a cycle

        object_id = str(data.get("object_id", "")).strip()
        if not object_id:
            _LOGGER.warning("register_number without object_id; ignoring")
            return

        state = self._state
        if state is None:
            return

        spec = NumberRegistration(
            name=str(data.get("name", "Number")),
            object_id=object_id,
            icon=str(data.get("icon", "mdi:tune")),
            min_value=float(data.get("min_value", 0.0)),
            max_value=float(data.get("max_value", 100.0)),
            options=([str(o) for o in data["options"]]
                     if data.get("options") else None),
            initial_value=(str(data.get("initial_value", ""))
                           if data.get("options")
                           else float(data.get("initial_value", 0.0))),
        )
        spec.meta = entity_meta(data)
        changed = self._upsert("number", state.pending_numbers, spec)
        if changed is spec:
            state.peripheral_number_values[object_id] = spec.initial_value
        elif changed is not None:
            entity = getattr(state, "peripheral_number_entities", {}).get(object_id)
            if entity is not None:
                entity.name, entity.icon = spec.name, spec.icon
                entity.options = spec.options
                entity.min_value, entity.max_value = spec.min_value, spec.max_value
                entity.meta = dict(spec.meta)

        if changed is not None:
            if satellite is not None:
                satellite.register_pending_numbers()
            self._entities_changed("number", object_id)
        if changed is not spec and "initial_value" in data:
            self._set_number_value(object_id, spec.initial_value)

    def _register_switch(self, data: Dict[str, Any], satellite: Any) -> None:
        """Register a Switch declared by a peripheral. BISCUIT addition.

        Same repeat rules as _register_number, including the value.
        """
        from .models import SwitchRegistration  # local import to avoid a cycle

        object_id = str(data.get("object_id", "")).strip()
        if not object_id:
            _LOGGER.warning("register_switch without object_id; ignoring")
            return

        state = self._state
        if state is None:
            return

        spec = SwitchRegistration(
            name=str(data.get("name", "Switch")),
            object_id=object_id,
            icon=str(data.get("icon", "mdi:toggle-switch")),
            initial_value=bool(data.get("initial_value", False)),
        )
        spec.meta = entity_meta(data)
        changed = self._upsert("switch", state.pending_switches, spec)
        if changed is spec:
            state.peripheral_switch_values[object_id] = spec.initial_value
            _LOGGER.info("Switch registered: %s", object_id)
        elif changed is not None:
            entity = getattr(state, "peripheral_switch_entities", {}).get(object_id)
            if entity is not None:
                entity.name, entity.icon = spec.name, spec.icon
                entity.meta = dict(spec.meta)

        if changed is not None:
            if satellite is not None:
                satellite.register_pending_switches()
            self._entities_changed("switch", object_id)
        if changed is not spec and "initial_value" in data:
            self._set_switch_value(object_id, spec.initial_value)

    def _register_light(self, data: Dict[str, Any], satellite: Any) -> None:
        """Register a Light declared by a peripheral.

        A repeat (e.g. after a peripheral reconnect) keeps the existing entity
        and its state, and takes a changed name, effect list or metadata. The
        state itself comes separately, as light_state.
        """
        from .models import LightRegistration  # local import to avoid a cycle

        object_id = str(data.get("object_id", "")).strip()
        if not object_id:
            _LOGGER.warning("register_light without object_id; ignoring")
            return

        state = self._state
        if state is None:
            return

        spec = LightRegistration(
            name=str(data.get("name", "LEDs")),
            object_id=object_id,
            icon=str(data.get("icon", "mdi:led-strip-variant")),
            effects=[str(e) for e in data.get("effects", []) if e],
            supports_rgb=bool(data.get("supports_rgb", True)),
            supports_brightness=bool(data.get("supports_brightness", True)),
            supports_color_temperature=bool(data.get("supports_color_temperature", False)),
            min_mireds=float(data.get("min_mireds", 153.0)),
            max_mireds=float(data.get("max_mireds", 500.0)),
        )
        spec.meta = entity_meta(data)
        changed = self._upsert("light", state.pending_lights, spec)
        if changed is None:
            return
        entity = state.led_light_entities.get(object_id)
        if entity is not None:
            # The colour modes are fixed at creation; a light that changes
            # those is a different light, and nothing here does that.
            entity.name, entity.icon = spec.name, spec.icon
            entity.meta = dict(spec.meta)
            entity.update_effects(spec.effects)
        elif changed is spec:
            _LOGGER.info("Light registered: %s (%d effects)", object_id, len(spec.effects))

        # If the satellite is already running, materialise the entity
        # now so future messages route correctly. HA only sees it
        # after the integration reconnects, but LVA stays consistent.
        if satellite is not None:
            satellite.register_pending_lights()

        self._entities_changed("light", object_id)

    def _register_button(self, satellite: Any) -> None:
        """Register button press event support declared by a peripheral.

        Idempotent: repeat registrations from a reconnecting peripheral
        are a no-op — the existing entity and its accumulated event state
        are preserved.

        When the satellite is already running, the entity is materialised
        immediately so subsequent button_* commands route correctly.
        HA only sees the new entity after it reconnects, which LVA forces
        (see "Changing an entity" above).
        """
        state = self._state
        if state is None:
            return

        if state.pending_button:
            # Already registered; keep the existing entity.
            return

        state.pending_button = True
        _LOGGER.info("Button event sensor registered by peripheral")

        if satellite is not None:
            satellite.register_pending_button()

        self._entities_changed("button", "button_press_event")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _entities_changed(self, entity_kind: str, entity_id: str) -> None:
        """Something HA enumerates changed: make sure HA re-reads it, once."""
        self.entity_generation += 1
        self._last_entity_change_at = time.monotonic()
        self._last_entity_change = (entity_kind, entity_id)
        self._schedule_ha_reconnect_for_late_entity(entity_kind, entity_id)

    def _schedule_ha_reconnect_for_late_entity(self, entity_kind: str, entity_id: str) -> None:
        """Reconnect HA once for entity changes made after its enumeration.

        Coalesced: one task at a time, which waits for the burst to finish and
        for the cooldown, then decides on what is true THEN - so a hundred
        registrations in a row cost at most one reconnect, a change inside the
        cooldown is deferred rather than lost, and a connection that already
        enumerated the change is left alone.
        """
        if self._loop is None:
            return
        task = self._pending_entity_reconnect_task
        if task is not None and not task.done():
            _LOGGER.debug("HA reconnect already pending; %s '%s' joins it",
                          entity_kind, entity_id)
            return
        self._pending_entity_reconnect_task = self._loop.create_task(self._reconnect_when_settled())

    async def _reconnect_when_settled(self) -> None:
        try:
            while True:
                quiet = time.monotonic() - self._last_entity_change_at
                if quiet >= self.LATE_ENTITY_RECONNECT_DEBOUNCE_S:
                    break
                await asyncio.sleep(self.LATE_ENTITY_RECONNECT_DEBOUNCE_S - quiet)

            wait = self._last_ha_reconnect_at + self.LATE_ENTITY_RECONNECT_COOLDOWN_S - time.monotonic()
            if wait > 0:
                _LOGGER.info("HA reconnect for changed entities deferred %.0fs by the cooldown", wait)
                await asyncio.sleep(wait)

            held = None
            while True:
                state = self._state
                satellite = state.satellite if state is not None else None
                if state is None or not state.connected or satellite is None:
                    # Nothing to refresh: HA enumerates the current list when it
                    # next connects.
                    return
                listed = getattr(satellite, "_listed_generation", None)
                if listed is None or listed >= self.entity_generation:
                    # Not enumerated yet (it will see the current list), or it
                    # already saw every change.
                    return
                transport = getattr(satellite, "_transport", None)
                if transport is None:
                    return
                # A disconnect stops the music and the reply and abandons the
                # pipeline, so a refresh that is only cosmetic waits for them.
                busy = self._busy_for_reconnect(state, satellite)
                if busy is None:
                    break
                if busy != held:
                    _LOGGER.info("HA reconnect for changed entities held while %s", busy)
                    held = busy
                await asyncio.sleep(self.LATE_ENTITY_RECONNECT_BUSY_POLL_S)

            self._last_ha_reconnect_at = time.monotonic()
            entity_kind, entity_id = self._last_entity_change
            _LOGGER.info(
                "Entities changed after HA's enumeration (latest: %s '%s'); forcing one HA reconnect",
                entity_kind,
                entity_id,
            )
            # Ours, not HA leaving: connection_lost then keeps the ring and
            # shared playback as they are (see satellite.connection_lost).
            satellite._forced_refresh = True
            transport.close()
        except asyncio.CancelledError:
            return

    @staticmethod
    def _busy_for_reconnect(state: Any, satellite: Any) -> Optional[str]:
        """Why a forced reconnect should wait now, or None if it need not."""
        if getattr(satellite, "_pipeline_active", False):
            return "a voice pipeline is running"
        for player, what in ((state.tts_player, "an announcement is playing"),
                             (state.music_player, "media is playing")):
            try:
                if player is not None and player.is_playing:
                    return what
            except Exception:  # pylint: disable=broad-except
                continue
        return None

    async def _push_mute_switch(self, satellite: Any, *, muted: bool) -> None:
        """Reflect a peripheral-triggered mute change to Home Assistant."""
        state = self._state
        if state is None or state.mute_switch_entity is None:
            return

        entity = state.mute_switch_entity
        entity._switch_state = muted  # pylint: disable=protected-access

        self._broadcast([SwitchStateResponse(key=entity.key, state=muted)])

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    async def emit_event(
        self,
        event: LVAEvent | str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Broadcast an event to all connected peripheral clients.

        Built-in events use LVAEvent, while peripheral-only commands (numbers
        and switches) intentionally use their protocol name directly.
        """
        event_name = event.value if isinstance(event, LVAEvent) else str(event)
        # Cache the last conversation text so newly-connecting clients get it in the snapshot
        if event == LVAEvent.STT_TEXT and data:
            self._last_stt_text = data.get("text")
        elif event == LVAEvent.TTS_TEXT and data:
            self._last_tts_text = data.get("text")
        # Clear both sides at the start of a new pipeline run
        elif event == LVAEvent.LISTENING:
            self._last_stt_text = None
            self._last_tts_text = None

        # ----------------------------------------------------------------
        # Track current "state" so newly-connecting clients receive it on
        # connect via _send_snapshot.  Only persistent/visual states are
        # stored — transient informational events are skipped.
        # ----------------------------------------------------------------
        _STATE_EVENTS = {
            LVAEvent.WAKE_WORD_DETECTED,
            LVAEvent.LISTENING,
            LVAEvent.THINKING,
            LVAEvent.TTS_SPEAKING,
            LVAEvent.TTS_FINISHED,
            LVAEvent.IDLE,
            LVAEvent.MUTED,
            LVAEvent.TIMER_TICKING,
            LVAEvent.TIMER_RINGING,
            LVAEvent.MEDIA_PLAYER_PLAYING,
            LVAEvent.DISCONNECTED,
            LVAEvent.PIPELINE_ERROR,
            LVAEvent.VOLUME_MUTED,
        }
        if event in _STATE_EVENTS:
            self._current_state = event
            self._current_state_data = data or None
        elif event == LVAEvent.TIMER_UPDATED and self._current_state == LVAEvent.TIMER_TICKING:
            # Keep state as TIMER_TICKING but refresh the countdown data
            self._current_state_data = data or None

        if not self._clients:
            return

        payload: Dict[str, Any] = {"event": event_name}
        if data:
            payload["data"] = data

        raw_msg = json.dumps(payload)
        dead: Set[Any] = set()

        for ws in list(self._clients):
            try:
                await ws.send(raw_msg)
            except Exception:  # pylint: disable=broad-except
                dead.add(ws)

        self._clients -= dead
        _LOGGER.debug(
            "Peripheral event %-25s → %d client(s)",
            event_name,
            len(self._clients),
        )

    def emit_event_sync(
        self,
        event: LVAEvent,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Thread-safe fire-and-forget event emission.

        Safe to call from mpv callbacks, the audio processing thread, or any
        non-async context while the asyncio event loop is running.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self.emit_event(event, data), loop)

    def _create_media_player_response(self, state: MediaPlayerState) -> MediaPlayerStateResponse:
        """Create a MediaPlayerStateResponse with current entity state."""
        assert self._state is not None
        media_entity = self._state.media_player_entity
        assert media_entity is not None
        return MediaPlayerStateResponse(
            key=media_entity.key,
            state=state,
            volume=media_entity.volume,
            muted=media_entity.muted,
        )
