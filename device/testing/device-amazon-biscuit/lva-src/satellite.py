# SPDX-License-Identifier: Apache-2.0
#
# MODIFIED FILE. This is a changed copy of
# linux_voice_assistant/satellite.py from OHF-Voice/linux-voice-assistant
# at commit b0c53c41c11e,
#   https://github.com/OHF-Voice/linux-voice-assistant
# licensed under the Apache License 2.0 (LICENSES/Apache-2.0.txt).
# Changed by liamtw22 and contributors, 2026, for the Amazon Echo Dot
# (2nd gen) postmarketOS port. The upstream copyright is unchanged.
# Changes: registration of the peripheral entities, timer ring ownership and
#   re-display, ducking, and qualification instrumentation.
"""Voice satellite protocol."""

from . import qualification_v1 as _qualification_v1
import asyncio
import hashlib
import logging
import posixpath
import shutil
import threading
import time
import zlib
from collections.abc import Iterable
from functools import partial
from typing import Any, Dict, List, Optional, Set, Union
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]
    AuthenticationRequest,
    DeviceInfoRequest,
    DeviceInfoResponse,
    LightCommandRequest,
    ListEntitiesDoneResponse,
    ListEntitiesRequest,
    MediaPlayerCommandRequest,
    NumberCommandRequest,
    SelectCommandRequest,
    SubscribeHomeAssistantStatesRequest,
    SubscribeStatesRequest,
    SubscribeVoiceAssistantRequest,
    SwitchCommandRequest,
    VoiceAssistantAnnounceFinished,
    VoiceAssistantAnnounceRequest,
    VoiceAssistantAudio,
    VoiceAssistantConfigurationRequest,
    VoiceAssistantConfigurationResponse,
    VoiceAssistantEventResponse,
    VoiceAssistantExternalWakeWord,
    VoiceAssistantRequest,
    VoiceAssistantSetConfiguration,
    VoiceAssistantTimerEventResponse,
    VoiceAssistantWakeWord,
)
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
from aioesphomeapi.model import (
    VoiceAssistantEventType,
    VoiceAssistantFeature,
    VoiceAssistantTimerEventType,
)
from google.protobuf import message
from pymicro_wakeword import MicroWakeWord
from pyopen_wakeword import OpenWakeWord

from .api_server import APIServer
from .entity import (
    ButtonEventSensorEntity,
    LEDLightEntity,
    MediaPlayerEntity,
    MicSettingEntity,
    MuteSwitchEntity,
    StopWordSensitivityNumberEntity,
    ThinkingSoundEntity,
    WakeWord1SensitivityNumberEntity,
    WakeWord2SensitivityNumberEntity,
)
from .models import AvailableWakeWord, ServerState, WakeWordType
from .peripheral_api import LVAEvent
from .util import call_all

_LOGGER = logging.getLogger(__name__)

PROTO_TO_MESSAGE_TYPE = {v: k for k, v in MESSAGE_TYPE_TO_PROTO.items()}

# BISCUIT: what Home Assistant shows on the device page. HA takes manufacturer
# and model from project_name ("<manufacturer>.<model>") whenever it is set, and
# shows project_version as the firmware, next to the ESPHome API version.
DEVICE_MANUFACTURER = "Amazon"
DEVICE_MODEL = "Echo Dot (2nd Gen)"

_HAS_AUDIO_DATA2 = "data2" in {f.name for f in VoiceAssistantAudio.DESCRIPTOR.fields}

# BISCUIT: how long HA has to come back from a reconnect LVA forced before the
# ring is told it is disconnected after all.
FORCED_RECONNECT_GRACE_S = 30.0

# BISCUIT: the keys of the entities a peripheral registers come from their
# object_id, not from their position in state.entities. Positions depended on
# timing (whether the peripheral registered before or after the first HA
# connection) and on which optional entities exist (the direction light's two
# selects), and some Home Assistant releases drop the registry entry of an
# entity whose key changes. LVA's own entities are always created first, in a
# fixed order, and keep their small positional keys; this range starts well
# above them and stays under 2**31.
PERIPHERAL_KEY_MIN = 0x10000
PERIPHERAL_KEY_SPAN = 0x7FFFFFFF - PERIPHERAL_KEY_MIN


def peripheral_key(object_id: str, used: Set[int]) -> int:
    """A stable key for a peripheral entity, not one of `used`.

    A collision - two object_ids with one CRC, or one landing on a key in
    use - moves to the next free key and is logged; which of the two moves
    then depends on their order, which is no worse than every key did before.
    """
    key = PERIPHERAL_KEY_MIN + zlib.crc32(object_id.encode("utf-8")) % PERIPHERAL_KEY_SPAN
    while key in used:
        _LOGGER.warning("Entity key %d for '%s' is taken; using the next free one", key, object_id)
        key = PERIPHERAL_KEY_MIN + (key - PERIPHERAL_KEY_MIN + 1) % PERIPHERAL_KEY_SPAN
    return key


class VoiceSatelliteProtocol(APIServer):

    def __init__(self, state: ServerState) -> None:
        super().__init__(state.name)

        self.state = state
        # BISCUIT: one satellite per connection, but only ONE of them is Home
        # Assistant's voice client, and state.satellite must stay that one.
        # Taking it unconditionally meant any second ESPHome client - a
        # diagnostic tool - captured the wake word, the button and every push,
        # and cleared them for good when it disconnected. A connection takes it
        # here only when nobody holds it, and otherwise by subscribing to the
        # voice assistant (_claim_satellite), which only HA does.
        self._voice_client = False
        # The entity generation this connection enumerated; see
        # PeripheralAPIServer._reconnect_when_settled. None until it has.
        self._listed_generation: Optional[int] = None
        if self.state.satellite is None:
            self.state.satellite = self
        if not self.state.connections:
            self.state.connected = False

        # Report capabilities appropriately
        if state.output_only:
            _LOGGER.debug("Output only features")
            self.supported_features = VoiceAssistantFeature.API_AUDIO | VoiceAssistantFeature.ANNOUNCE
        else:
            _LOGGER.debug("Voice assistant features")
            self.supported_features = (
                VoiceAssistantFeature.VOICE_ASSISTANT | VoiceAssistantFeature.API_AUDIO | VoiceAssistantFeature.ANNOUNCE | VoiceAssistantFeature.START_CONVERSATION | VoiceAssistantFeature.TIMERS
            )
            # Channel 1 carries echo-reference audio; advertise SPEAKER so HA knows to use it for server-side AEC.
            if state.audio_input_channels >= 2:
                self.supported_features |= VoiceAssistantFeature.MULTI_CHANNEL_AUDIO  # pylint: disable=no-member

        existing_mute_switches = [entity for entity in self.state.entities if isinstance(entity, MuteSwitchEntity)]
        existing_media_players = [entity for entity in self.state.entities if isinstance(entity, MediaPlayerEntity)]

        if existing_media_players:
            # Keep the first instance and remove any extras.
            self.state.media_player_entity = existing_media_players[0]
            for extra_player in existing_media_players[1:]:
                self.state.entities.remove(extra_player)

        if existing_mute_switches:
            self.state.mute_switch_entity = existing_mute_switches[0]
            for extra_mute in existing_mute_switches[1:]:
                self.state.entities.remove(extra_mute)

        if self.state.media_player_entity is None:
            self.state.media_player_entity = MediaPlayerEntity(
                server=self,
                key=len(state.entities),
                name="Media player",
                object_id="linux_voice_assistant_media_player",
                music_player=state.music_player,
                announce_player=state.tts_player,
                initial_volume=state.volume,
            )
            self.state.entities.append(self.state.media_player_entity)
        elif self.state.media_player_entity not in self.state.entities:
            self.state.entities.append(self.state.media_player_entity)

        self.state.media_player_entity.server = self
        self.state.media_player_entity.volume = state.volume
        self.state.media_player_entity.previous_volume = state.volume

        # Add/update mute switch entity (like ESPHome Voice PE)
        mute_switch = self.state.mute_switch_entity
        if mute_switch is None:
            mute_switch = MuteSwitchEntity(
                server=self,
                key=len(state.entities),
                name="Mute",
                object_id="mute",
                get_muted=lambda: self.state.muted,
                set_muted=self._set_muted,
            )
            self.state.entities.append(mute_switch)
            self.state.mute_switch_entity = mute_switch
        elif mute_switch not in self.state.entities:
            self.state.entities.append(mute_switch)

        mute_switch.server = self
        mute_switch.update_get_muted(lambda: self.state.muted)
        mute_switch.update_set_muted(self._set_muted)
        mute_switch.sync_with_state()

        existing_thinking_sound_switches = [entity for entity in self.state.entities if isinstance(entity, ThinkingSoundEntity)]
        if existing_thinking_sound_switches:
            self.state.thinking_sound_entity = existing_thinking_sound_switches[0]
            for extra_thinking in existing_thinking_sound_switches[1:]:
                self.state.entities.remove(extra_thinking)

        # Add/update thinking sound entity
        thinking_sound_switch = self.state.thinking_sound_entity
        if thinking_sound_switch is None:
            thinking_sound_switch = ThinkingSoundEntity(
                server=self,
                key=len(state.entities),
                name="Thinking sound",
                object_id="thinking_sound",
                get_thinking_sound_enabled=lambda: self.state.thinking_sound_enabled,
                set_thinking_sound_enabled=self._set_thinking_sound_enabled,
            )
            self.state.entities.append(thinking_sound_switch)
            self.state.thinking_sound_entity = thinking_sound_switch
        elif thinking_sound_switch not in self.state.entities:
            self.state.entities.append(thinking_sound_switch)

        # Load thinking sound enabled state from preferences
        if hasattr(self.state.preferences, "thinking_sound") and self.state.preferences.thinking_sound in (0, 1):
            self.state.thinking_sound_enabled = bool(self.state.preferences.thinking_sound)
        else:
            self.state.thinking_sound_enabled = False

        thinking_sound_switch.server = self
        thinking_sound_switch.update_get_thinking_sound_enabled(lambda: self.state.thinking_sound_enabled)
        thinking_sound_switch.update_set_thinking_sound_enabled(self._set_thinking_sound_enabled)
        thinking_sound_switch.sync_with_state()

        # Add/update Wake Word 1 sensitivity number entity
        sensitivity_1_entity = self.state.sensitivity_1_number_entity
        if sensitivity_1_entity is None:
            sensitivity_1_entity = WakeWord1SensitivityNumberEntity(
                server=self,
                key=len(state.entities),
                name="Wake word 1 threshold",
                object_id="wake_word_1_sensitivity",
                get_sensitivity=lambda: self.state.wake_word_1_threshold,
                set_sensitivity=self._set_sensitivity_1,
                initial_value=self.state.wake_word_1_threshold,
            )
            self.state.entities.append(sensitivity_1_entity)
            self.state.sensitivity_1_number_entity = sensitivity_1_entity
        elif sensitivity_1_entity not in self.state.entities:
            self.state.entities.append(sensitivity_1_entity)

        sensitivity_1_entity.server = self
        sensitivity_1_entity.update_get_sensitivity(lambda: self.state.wake_word_1_threshold)
        sensitivity_1_entity.update_set_sensitivity(self._set_sensitivity_1)

        sensitivity_1_entity.sync_with_state()
        _LOGGER.debug("INIT: Wake Word 1 entity initialized with value %.3f", sensitivity_1_entity.value)

        # Add/update Wake Word 2 sensitivity number entity
        sensitivity_2_entity = self.state.sensitivity_2_number_entity
        if sensitivity_2_entity is None:
            sensitivity_2_entity = WakeWord2SensitivityNumberEntity(
                server=self,
                key=len(state.entities),
                name="Wake word 2 threshold",
                object_id="wake_word_2_sensitivity",
                get_sensitivity=lambda: self.state.wake_word_2_threshold,
                set_sensitivity=self._set_sensitivity_2,
                initial_value=self.state.wake_word_2_threshold,
            )
            self.state.entities.append(sensitivity_2_entity)
            self.state.sensitivity_2_number_entity = sensitivity_2_entity
        elif sensitivity_2_entity not in self.state.entities:
            self.state.entities.append(sensitivity_2_entity)

        sensitivity_2_entity.server = self
        sensitivity_2_entity.update_get_sensitivity(lambda: self.state.wake_word_2_threshold)
        sensitivity_2_entity.update_set_sensitivity(self._set_sensitivity_2)

        sensitivity_2_entity.sync_with_state()

        # Add/update Stop Word sensitivity number entity
        stop_sensitivity_entity = self.state.stop_sensitivity_number_entity
        if stop_sensitivity_entity is None:
            stop_sensitivity_entity = StopWordSensitivityNumberEntity(
                server=self,
                key=len(state.entities),
                name="Stop word threshold",
                object_id="stop_word_sensitivity",
                get_sensitivity=lambda: self.state.stop_word_threshold,
                set_sensitivity=self._set_stop_sensitivity,
                initial_value=self.state.stop_word_threshold,
            )
            self.state.entities.append(stop_sensitivity_entity)
            self.state.stop_sensitivity_number_entity = stop_sensitivity_entity
        elif stop_sensitivity_entity not in self.state.entities:
            self.state.entities.append(stop_sensitivity_entity)

        stop_sensitivity_entity.server = self
        stop_sensitivity_entity.update_get_sensitivity(lambda: self.state.stop_word_threshold)
        stop_sensitivity_entity.update_set_sensitivity(self._set_stop_sensitivity)

        stop_sensitivity_entity.sync_with_state()

        # Mic Gain
        if (not self.state.processed_mic_input) and self.state.mic_gain_entity is None:
            self.state.mic_gain_entity = MicSettingEntity(
                server=self,
                key=len(self.state.entities),
                name="Mic Auto Gain",
                object_id="mic_gain",
                min_value=0.0,
                max_value=31.0,
                get_value=lambda: float(self.state.mic_auto_gain),
                set_value=lambda val: self.state.persist_mic_gain(float(val)),
                icon="mdi:microphone-plus",
            )
            self.state.entities.append(self.state.mic_gain_entity)
        elif (not self.state.processed_mic_input) and self.state.mic_gain_entity not in self.state.entities:
            self.state.entities.append(self.state.mic_gain_entity)

        if self.state.mic_gain_entity is not None:
            self.state.mic_gain_entity.server = self
            self.state.mic_gain_entity.update_get_value(lambda: float(self.state.mic_auto_gain))
            self.state.mic_gain_entity.update_set_value(lambda val: self.state.persist_mic_gain(float(val)))  # type: ignore[arg-type]
            self.state.mic_gain_entity.sync_with_state()

        # Mic Noise Suppression
        _NOISE_OPTIONS = ["Off", "Low", "Medium", "High", "Max"]
        _NOISE_TO_INT = {label: i for i, label in enumerate(_NOISE_OPTIONS)}

        def _get_noise_label() -> str:
            return _NOISE_OPTIONS[max(0, min(4, self.state.mic_noise_suppression))]

        def _set_noise_label(label: Union[float, str]) -> None:
            self.state.persist_mic_noise(float(_NOISE_TO_INT.get(str(label), 0)))

        if (not self.state.processed_mic_input) and self.state.mic_noise_suppression_entity is None:
            self.state.mic_noise_suppression_entity = MicSettingEntity(
                server=self,
                key=len(self.state.entities),
                name="Mic Noise Suppression",
                object_id="mic_noise",
                options=_NOISE_OPTIONS,
                get_value=_get_noise_label,
                set_value=_set_noise_label,
                icon="mdi:waveform",
            )
            self.state.entities.append(self.state.mic_noise_suppression_entity)
        elif (not self.state.processed_mic_input) and self.state.mic_noise_suppression_entity not in self.state.entities:
            self.state.entities.append(self.state.mic_noise_suppression_entity)

        if self.state.mic_noise_suppression_entity is not None:
            self.state.mic_noise_suppression_entity.server = self
            self.state.mic_noise_suppression_entity.update_get_value(_get_noise_label)
            self.state.mic_noise_suppression_entity.update_set_value(_set_noise_label)
            self.state.mic_noise_suppression_entity.sync_with_state()

        # Mic Volume
        if (not self.state.processed_mic_input) and self.state.mic_volume_entity is None:
            self.state.mic_volume_entity = MicSettingEntity(
                server=self,
                key=len(self.state.entities),
                name="Mic Volume",
                object_id="mic_volume",
                min_value=1.0,
                max_value=100.0,
                get_value=lambda: float(self.state.mic_volume),
                set_value=lambda val: self.state.persist_mic_volume(float(val)),
                icon="mdi:microphone-settings",
            )
            self.state.entities.append(self.state.mic_volume_entity)
        elif (not self.state.processed_mic_input) and self.state.mic_volume_entity not in self.state.entities:
            self.state.entities.append(self.state.mic_volume_entity)

        if self.state.mic_volume_entity is not None:
            self.state.mic_volume_entity.server = self
            self.state.mic_volume_entity.update_get_value(lambda: float(self.state.mic_volume))
            self.state.mic_volume_entity.update_set_value(lambda val: self.state.persist_mic_volume(float(val)))

        # NOTE: ButtonEventSensorEntity is NOT created here unconditionally.
        # It is only materialised when a peripheral sends the register_button
        # command (see register_pending_button below), mirroring the same
        # opt-in pattern used by register_light for LEDLightEntity.

        # Materialise the Light entities peripherals registered before
        # this satellite was constructed (or reattach existing ones).
        self.register_pending_lights()
        self.register_pending_numbers()          # BISCUIT
        self.register_pending_switches()         # BISCUIT
        self.register_pending_sensors()          # BISCUIT
        self.register_pending_binary_sensors()   # BISCUIT
        # Materialise ButtonEventSensorEntity if a peripheral already registered
        # button support before this satellite was constructed (e.g. on an HA
        # reconnect while the peripheral container stayed connected to LVA).
        self.register_pending_button()

        # ---- Instance variables ----

        _qualification_v1.satellite_created(self)
        self._is_streaming_audio = False
        self._tts_url: Optional[str] = None
        self._tts_played = False
        self._continue_conversation = False
        # Ring state deliberately does NOT live here; see the properties below.
        # Every start or stop of the ring bumps this, and every scheduled
        # continuation carries the value it was created under. Without it a
        # Stop followed by a new timer inside one second leaves TWO ring loops
        # running: the Stop's own tts_player.stop() fires the old playback's
        # done_callback, which schedules a continuation, and by the time that
        # continuation fires _timer_finished is True again - for the new timer.
        # It cannot tell the difference, so it rings alongside the new loop.
        self._processing = False
        self._pipeline_active = False
        self._external_wake_words: Dict[str, VoiceAssistantExternalWakeWord] = {}
        self._disconnect_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Peripheral API helper
    # ------------------------------------------------------------------

    def _emit(
        self,
        event: LVAEvent,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Emit a peripheral LED/button event.

        Thread-safe: delegates to ``emit_event_sync`` which uses
        ``run_coroutine_threadsafe`` when called from outside the event loop.
        """
        api = self.state.peripheral_api
        if api is not None:
            api.emit_event_sync(event, data)

    def _peripheral_key(self, object_id: str) -> int:
        """See peripheral_key. BISCUIT."""
        return peripheral_key(object_id, {entity.key for entity in self.state.entities})

    def register_pending_sensors(self) -> None:
        """Materialise read-only Sensor entities peripherals registered. BISCUIT."""
        from .entity import PeripheralSensorEntity

        for spec in self.state.pending_sensors:
            existing = self.state.peripheral_sensor_entities.get(spec.object_id)
            if existing is not None:
                existing.server = self
                if existing not in self.state.entities:
                    self.state.entities.append(existing)
                continue

            entity = PeripheralSensorEntity(
                server=self,
                key=self._peripheral_key(spec.object_id),
                name=spec.name,
                object_id=spec.object_id,
                unit_of_measurement=spec.unit_of_measurement,
                device_class=spec.device_class,
                accuracy_decimals=spec.accuracy_decimals,
                icon=spec.icon,
                meta=getattr(spec, "meta", None),
            )
            # A reading the peripheral sent before this entity existed.
            value = getattr(self.state, "peripheral_sensor_values", {}).get(spec.object_id)
            if value is not None:
                entity.update_state(value)
            self.state.peripheral_sensor_entities[spec.object_id] = entity
            self.state.entities.append(entity)

    def register_pending_binary_sensors(self) -> None:
        """Materialise read-only Binary Sensor entities peripherals registered. BISCUIT."""
        from .entity import PeripheralBinarySensorEntity

        entities = getattr(self.state, "peripheral_binary_sensor_entities", {})
        for spec in getattr(self.state, "pending_binary_sensors", []):
            existing = entities.get(spec.object_id)
            if existing is not None:
                existing.server = self
                if existing not in self.state.entities:
                    self.state.entities.append(existing)
                continue

            entity = PeripheralBinarySensorEntity(
                server=self,
                key=self._peripheral_key(spec.object_id),
                name=spec.name,
                object_id=spec.object_id,
                device_class=spec.device_class,
                icon=spec.icon,
                meta=getattr(spec, "meta", None),
            )
            entity.update_state(
                getattr(self.state, "peripheral_binary_sensor_values", {}).get(spec.object_id))
            entities[spec.object_id] = entity
            self.state.entities.append(entity)

    def register_pending_numbers(self) -> None:
        """Materialise Number entities peripherals registered. BISCUIT addition.

        The value lives on the state, not the entity, so an HA reconnect (which
        rebuilds this satellite) does not reset it. Changes are pushed back to
        the peripheral as a number_command event, matching how light_command
        carries HA's changes to a registered Light.
        """
        from .entity import MicSettingEntity

        for spec in self.state.pending_numbers:
            if spec.object_id in getattr(self.state, "peripheral_number_entities", {}):
                entity = self.state.peripheral_number_entities[spec.object_id]
                entity.server = self
                if entity not in self.state.entities:
                    self.state.entities.append(entity)
                continue

            object_id = spec.object_id

            # A Select carries strings, a Number carries floats. Coercing a
            # Select's value to float would raise on the first option chosen.
            _is_select = bool(spec.options)

            # sel is bound as a default, NOT looked up when called. These are
            # closures made in a loop, and a helper they shared by name
            # resolved to the LAST iteration's: every entity then cast like the
            # last one registered, so Selects went through float() when that
            # was a Number, and Numbers became strings when it was a Select.
            def _cast(v, sel):
                return str(v) if sel else float(v)

            def _get(oid=object_id, sel=_is_select):
                default = "" if sel else 0.0
                return _cast(self.state.peripheral_number_values.get(oid, default), sel)

            def _set(val, oid=object_id, sel=_is_select):
                value = _cast(val, sel)
                self.state.peripheral_number_values[oid] = value
                self._emit("number_command", {"object_id": oid, "value": value})

            entity = MicSettingEntity(
                server=self,
                key=self._peripheral_key(object_id),
                name=spec.name,
                object_id=object_id,
                min_value=spec.min_value,
                max_value=spec.max_value,
                options=spec.options,
                get_value=_get,
                set_value=_set,
                icon=spec.icon,
                meta=getattr(spec, "meta", None),
            )
            if not hasattr(self.state, "peripheral_number_entities"):
                self.state.peripheral_number_entities = {}
            self.state.peripheral_number_entities[object_id] = entity
            self.state.entities.append(entity)

    def register_pending_switches(self) -> None:
        """Materialise Switch entities peripherals registered. BISCUIT addition.

        Mirrors register_pending_numbers: the value lives on the state so an HA
        reconnect does not reset it, and changes go back to the peripheral as a
        switch_command event.
        """
        from .entity import PeripheralSwitchEntity

        if not hasattr(self.state, "peripheral_switch_entities"):
            self.state.peripheral_switch_entities = {}

        for spec in self.state.pending_switches:
            if spec.object_id in self.state.peripheral_switch_entities:
                entity = self.state.peripheral_switch_entities[spec.object_id]
                entity.server = self
                if entity not in self.state.entities:
                    self.state.entities.append(entity)
                continue

            object_id = spec.object_id

            def _get(oid=object_id):
                return bool(self.state.peripheral_switch_values.get(oid, False))

            def _set(val, oid=object_id):
                value = bool(val)
                self.state.peripheral_switch_values[oid] = value
                self._emit("switch_command", {"object_id": oid, "state": value})

            entity = PeripheralSwitchEntity(
                server=self,
                key=self._peripheral_key(object_id),
                name=spec.name,
                object_id=object_id,
                get_value=_get,
                set_value=_set,
                icon=spec.icon,
                meta=getattr(spec, "meta", None),
            )
            self.state.peripheral_switch_entities[object_id] = entity
            self.state.entities.append(entity)

    def register_pending_lights(self) -> None:
        """Materialise LightEntities for peripheral registered lights.

        Called from __init__ so entities exist by the time HA enumerates,
        and again from the peripheral_api dispatcher when a light arrives
        after the satellite is already running. HA only sees a late
        registration after its next reconnect, but LVA stays consistent.
        """
        for spec in self.state.pending_lights:
            if spec.object_id in self.state.led_light_entities:
                # Already materialised. Reattach the server in case the
                # satellite has been reconstructed (HA reconnect).
                self.state.led_light_entities[spec.object_id].server = self
                if self.state.led_light_entities[spec.object_id] not in self.state.entities:
                    self.state.entities.append(self.state.led_light_entities[spec.object_id])
                continue

            object_id = spec.object_id
            entity = LEDLightEntity(
                server=self,
                key=self._peripheral_key(object_id),
                name=spec.name,
                object_id=object_id,
                icon=spec.icon,
                effects=spec.effects,
                supports_rgb=spec.supports_rgb,
                supports_brightness=spec.supports_brightness,
                supports_color_temperature=spec.supports_color_temperature,
                min_mireds=spec.min_mireds,
                max_mireds=spec.max_mireds,
                on_changed=partial(self._on_led_light_changed, object_id),
                meta=getattr(spec, "meta", None),
            )
            self.state.entities.append(entity)
            # The peripheral's own record of the light, if it sent one before
            # this entity existed (see LIGHT_STATE in peripheral_api).
            kept = getattr(self.state, "peripheral_light_states", {}).get(object_id)
            if kept:
                entity.apply_state(kept)
            if object_id == "ring" and hasattr(self.state, "peripheral_ring_brightness"):
                entity.brightness = self.state.peripheral_ring_brightness
            accents=getattr(self.state,'peripheral_ring_accents',None)
            if object_id in ('ring_colour_2','ring_colour_3') and accents:
                i=int(object_id[-1])-2
                entity.red,entity.green,entity.blue=[v/255 for v in accents['colours'][i]]
                entity.brightness=accents['levels'][i]/255
                entity.is_on=accents['enabled'][i]
            self.state.led_light_entities[object_id] = entity

    def register_pending_button(self) -> None:
        """Materialise ButtonEventSensorEntity once a peripheral has registered button support.

        Called from __init__ (handles HA reconnects where the peripheral container
        stayed connected to LVA and pending_button is already True) and from
        PeripheralAPIServer._register_button() when the command arrives at runtime.

        Safe to call multiple times: idempotent â€” if the entity already exists
        it is only reattached to the current satellite server instance.
        """
        if not self.state.pending_button:
            return

        if self.state.button_event_sensor_entity is not None:
            # Already materialised â€” reattach the server in case the satellite
            # has been reconstructed for an HA reconnect.
            self.state.button_event_sensor_entity.server = self
            if self.state.button_event_sensor_entity not in self.state.entities:
                self.state.entities.append(self.state.button_event_sensor_entity)
            return

        entity = ButtonEventSensorEntity(
            server=self,
            key=self._peripheral_key("button_press_event"),
            name="Action button",
            object_id="button_press_event",
        )
        self.state.entities.append(entity)
        self.state.button_event_sensor_entity = entity
        _LOGGER.info("Button event sensor entity materialised")

    def _on_led_light_changed(self, object_id: str) -> None:
        """Forward an HA Light entity change to peripherals as light_command.

        The event carries object_id so a peripheral that registered more
        than one light can route it to the correct hardware.
        """
        entity = self.state.led_light_entities.get(object_id)
        if entity is None:
            return
        self._emit(LVAEvent.LIGHT_COMMAND, entity.state_dict())

    # ------------------------------------------------------------------
    # Mute / thinking sound
    # ------------------------------------------------------------------

    def _set_thinking_sound_enabled(self, new_state: bool) -> None:
        self.state.thinking_sound_enabled = bool(new_state)
        self.state.preferences.thinking_sound = 1 if self.state.thinking_sound_enabled else 0

        if self.state.thinking_sound_enabled:
            _LOGGER.debug("Thinking sound enabled")
        else:
            _LOGGER.debug("Thinking sound disabled")
            pass
        self.state.save_preferences()

    def _set_sensitivity_1(self, new_value: float) -> None:
        self.state.wake_word_1_threshold = float(new_value)
        self.state.preferences.wake_word_1_sensitivity = float(new_value)
        self.state.save_preferences()
        _LOGGER.debug("Wake Word 1 Sensitivity value set to: %s", new_value)
        # Sync entity state
        if self.state.sensitivity_1_number_entity is not None:
            self.state.sensitivity_1_number_entity.sync_with_state()

    def _set_sensitivity_2(self, new_value: float) -> None:
        self.state.wake_word_2_threshold = float(new_value)
        self.state.preferences.wake_word_2_sensitivity = float(new_value)
        self.state.save_preferences()
        _LOGGER.debug("Wake Word 2 Sensitivity value set to: %s", new_value)
        # Sync entity state
        if self.state.sensitivity_2_number_entity is not None:
            self.state.sensitivity_2_number_entity.sync_with_state()

    def _set_stop_sensitivity(self, new_value: float) -> None:
        self.state.stop_word_threshold = float(new_value)
        self.state.preferences.stop_word_sensitivity = float(new_value)
        self.state.save_preferences()
        _LOGGER.debug("Stop Word Sensitivity value set to: %s", new_value)
        # Sync entity state
        if self.state.stop_sensitivity_number_entity is not None:
            self.state.stop_sensitivity_number_entity.sync_with_state()

    def _set_muted(self, new_state: bool) -> None:
        self.state.muted = bool(new_state)
        self._emit(LVAEvent.MUTED, {"muted": self.state.muted})

        if self.state.muted:
            # voice_assistant.stop behavior
            _LOGGER.debug("Muting voice assistant (voice_assistant.stop)")
            self._is_streaming_audio = False
            self.state.tts_player.stop()
            # Stop any ongoing voice processing
            self.state.stop_word.is_active = False  # type: ignore[attr-defined]
            self.state.tts_player.play(self.state.mute_sound)
        else:
            # voice_assistant.start_continuous behavior
            _LOGGER.debug("Unmuting voice assistant (voice_assistant.start_continuous)")
            self.state.tts_player.play(self.state.unmute_sound)
            self._emit(LVAEvent.IDLE)

    # ------------------------------------------------------------------
    # Voice pipeline event handler
    # ------------------------------------------------------------------

    def handle_voice_event(self, event_type: VoiceAssistantEventType, data: Dict[str, str]) -> None:
        _LOGGER.info("Voice event: type=%s, data=%s", event_type.name, data)

        if event_type == VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START:
            self._tts_url = data.get("url")
            self._tts_played = False
            self._continue_conversation = False
            self._pipeline_active = True

        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_START:
            self._emit(LVAEvent.THINKING)
            # Play optional audible thinking sound
            if self.state.thinking_sound_enabled:
                processing = getattr(self.state, "processing_sound", None)
                if processing:
                    _LOGGER.debug("Playing processing sound: %s", processing)
                    self.state.stop_word.is_active = True  # type: ignore[attr-defined]
                    self._processing = True
                    self.duck()
                    self.state.tts_player.play(self.state.processing_sound)

        elif event_type in (
            VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END,
            VoiceAssistantEventType.VOICE_ASSISTANT_STT_END,
        ):
            self._is_streaming_audio = False
            if event_type == VoiceAssistantEventType.VOICE_ASSISTANT_STT_END:
                stt_text = data.get("text", "").strip()
                if stt_text:
                    self._emit(LVAEvent.STT_TEXT, {"text": stt_text})
                    _LOGGER.debug("STT transcript: %s", stt_text)

        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_PROGRESS:
            if data.get("tts_start_streaming") == "1":
                # Start streaming early
                self.play_tts()

        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_END:
            if data.get("continue_conversation") == "1":
                self._continue_conversation = True

        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START:
            tts_text = data.get("text", "").strip()
            if tts_text:
                self._emit(LVAEvent.TTS_TEXT, {"text": tts_text})
                _LOGGER.debug("TTS response text: %s", tts_text)

        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END:
            self._tts_url = data.get("url")
            self.play_tts()

        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END:
            self._is_streaming_audio = False
            if not self._tts_played:
                self._pipeline_active = False
                self._tts_finished()
            # When TTS is playing, keep _pipeline_active = True to block
            # false wake word detections from speaker audio feedback.
            # _tts_finished() callback will clear it when playback ends.

            self._tts_played = False

        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_ERROR:
            self._emit(LVAEvent.PIPELINE_ERROR)

    # ------------------------------------------------------------------
    # Timer event handler
    # ------------------------------------------------------------------

    @_qualification_v1.activity
    def handle_timer_event(
        self,
        event_type: VoiceAssistantTimerEventType,
        msg: VoiceAssistantTimerEventResponse,
    ) -> None:
        _qualification_v1.timer_event(self, event_type, msg)
        _LOGGER.debug("Timer event: type=%s", event_type.name)

        # Build countdown data from the protobuf message fields.
        # total_seconds: the original timer duration.
        # seconds_left:  remaining seconds at the time of this event.
        timer_data = {
            "id": msg.timer_id,
            "name": msg.name,
            "total_seconds": msg.total_seconds,
            "seconds_left": msg.seconds_left,
            # Paused timers used to be indistinguishable from running ones.
            "is_active": bool(getattr(msg, "is_active", True)),
        }

        if event_type == VoiceAssistantTimerEventType.VOICE_ASSISTANT_TIMER_STARTED:
            self._known_timers[msg.timer_id] = timer_data
            self._emit(LVAEvent.TIMER_TICKING, timer_data)

        elif event_type == VoiceAssistantTimerEventType.VOICE_ASSISTANT_TIMER_UPDATED:
            self._known_timers[msg.timer_id] = timer_data
            self._emit(LVAEvent.TIMER_UPDATED, timer_data)

        elif event_type == VoiceAssistantTimerEventType.VOICE_ASSISTANT_TIMER_CANCELLED:
            self._known_timers.pop(msg.timer_id, None)
            self._emit_after_timer()

        elif event_type == VoiceAssistantTimerEventType.VOICE_ASSISTANT_TIMER_FINISHED:
            self._known_timers.pop(msg.timer_id, None)
            if not self._timer_finished:
                self.state.active_wake_words.add(self.state.stop_word.id)
                self._timer_finished = True
                self._timer_ring_start = time.monotonic()
                self._timer_generation += 1
                self.duck()
                # Kept on the shared state so the ring can be shown again
                # after a disconnect (_show_timers_again).
                self.state._biscuit_ringing_timer = timer_data
                self._emit(LVAEvent.TIMER_RINGING, timer_data)
                self._play_timer_finished(self._timer_generation)

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:  # noqa: C901  (acceptable complexity for a message router)
        if isinstance(msg, VoiceAssistantEventResponse):
            # Pipeline event
            data: Dict[str, str] = {}
            for arg in msg.data:
                data[arg.name] = arg.value

            self.handle_voice_event(VoiceAssistantEventType(msg.event_type), data)

        elif isinstance(msg, VoiceAssistantAnnounceRequest):
            _LOGGER.debug("Announcing: %s", msg.text)

            assert self.state.media_player_entity is not None

            urls = []
            if msg.preannounce_media_id:
                urls.append(msg.preannounce_media_id)
            urls.append(msg.media_id)

            self.state.active_wake_words.add(self.state.stop_word.id)
            self._continue_conversation = msg.start_conversation

            self.duck()
            self._emit(LVAEvent.TTS_SPEAKING)
            self.state.tts_player.play(urls, done_callback=self._tts_finished)

        elif isinstance(msg, VoiceAssistantTimerEventResponse):
            self.handle_timer_event(VoiceAssistantTimerEventType(msg.event_type), msg)

        elif isinstance(msg, DeviceInfoRequest):
            _LOGGER.debug("Device info request")

            # BISCUIT: the device, not the software. project_version is this
            # device build (e.g. r293, read from the apk database at start),
            # which HA shows as the firmware. esphome_version stays the API
            # library's, which is what HA compares features against.
            yield DeviceInfoResponse(
                uses_password=False,
                name=self.state.name,
                friendly_name=self.state.friendly_name,
                project_name=DEVICE_MANUFACTURER + "." + DEVICE_MODEL,
                project_version=getattr(self.state, "device_build", None) or self.state.version,
                esphome_version=self.state.esphome_version,
                mac_address=self.state.mac_address,
                manufacturer=DEVICE_MANUFACTURER,
                model=DEVICE_MODEL,
                voice_assistant_feature_flags=self.supported_features,
            )
        elif isinstance(msg, SubscribeVoiceAssistantRequest):
            if msg.subscribe:
                self._claim_satellite()
        elif isinstance(msg, SubscribeStatesRequest):
            # Standard ESPHome state subscription. Replay current entity state to
            # the subscribing client. (Entities answer SubscribeHomeAssistantStatesRequest;
            # initial state was previously only sent as a side effect of auth.)
            for entity in self.state.entities:
                yield from entity.handle_message(SubscribeHomeAssistantStatesRequest())
        elif isinstance(
            msg,
            (ListEntitiesRequest, SubscribeHomeAssistantStatesRequest, MediaPlayerCommandRequest, SwitchCommandRequest, NumberCommandRequest, SelectCommandRequest, LightCommandRequest),
        ):
            for entity in self.state.entities:
                yield from entity.handle_message(msg)

            # Emit peripheral event when background music starts playing.
            # Announcements (TTS) are explicitly excluded â€” those are covered
            # by TTS_SPEAKING.
            if isinstance(msg, MediaPlayerCommandRequest) and msg.has_media_url:
                is_announcement = msg.has_announcement and msg.announcement
                if not is_announcement:
                    self._emit(LVAEvent.MEDIA_PLAYER_PLAYING)

            if isinstance(msg, ListEntitiesRequest):
                api = self.state.peripheral_api
                self._listed_generation = getattr(api, "entity_generation", 0)
                yield ListEntitiesDoneResponse()

        elif isinstance(msg, VoiceAssistantConfigurationRequest):
            self._claim_satellite()
            _LOGGER.debug("âœ… Received VoiceAssistantConfigurationRequest from Home Assistant")
            _LOGGER.debug("   -> Request contains %d external wake words", len(msg.external_wake_words))

            available_wake_words = [
                VoiceAssistantWakeWord(
                    id=ww.id,
                    wake_word=ww.wake_word,
                    trained_languages=ww.trained_languages,
                )
                for ww in self.state.available_wake_words.values()
            ]

            # Log available internal wake words first
            internal_ww_count = len(self.state.available_wake_words)
            _LOGGER.debug("   -> Found %d internal available wake words", internal_ww_count)
            for ww in available_wake_words:
                _LOGGER.debug("      - %s: '%s' (langs: %s)", ww.id, ww.wake_word, ww.trained_languages)

            for eww in msg.external_wake_words:
                _LOGGER.debug("   -> Processing external wake word: id=%s, word='%s', type=%s", eww.id, eww.wake_word, eww.model_type)

                if eww.model_type != "micro":
                    _LOGGER.debug("      â†’ Skipping: not micro model type")
                    continue

                _LOGGER.debug("      â†’ Adding to available wake words")
                available_wake_words.append(
                    VoiceAssistantWakeWord(
                        id=eww.id,
                        wake_word=eww.wake_word,
                        trained_languages=eww.trained_languages,
                    )
                )

                self._external_wake_words[eww.id] = eww
                _LOGGER.debug("      â†’ Stored in external wake words cache")

            active_ww_ids = [ww.id for ww in self.state.wake_words.values() if ww.id in self.state.active_wake_words]
            _LOGGER.debug("   -> Active wake word IDs: %s", active_ww_ids)

            yield VoiceAssistantConfigurationResponse(
                available_wake_words=available_wake_words,
                active_wake_words=active_ww_ids,
                max_active_wake_words=2,
            )

            _qualification_v1.configured(self, True)
            _LOGGER.info("âœ… Connected to Home Assistant - Configuration handshake completed")
            _LOGGER.debug("âœ… VoiceAssistantConfigurationResponse sent successfully")
        elif isinstance(msg, VoiceAssistantSetConfiguration):
            # Change active wake words
            active_wake_words: Set[str] = set()
            new_wake_words: List[Optional[str]] = [None, None]

            # Get old positions before modification
            old_positions: Dict[str, int] = {}
            for idx, ww_id in enumerate(self.state.preferences.active_wake_words):
                if ww_id is not None and idx < 2:
                    old_positions[ww_id] = idx

            # Process new active wake words
            for wake_word_id in msg.active_wake_words:
                if wake_word_id in self.state.wake_words:
                    # Already active
                    active_wake_words.add(wake_word_id)
                else:
                    model_info = self.state.available_wake_words.get(wake_word_id)
                    if not model_info:
                        # Check external wake words (may require download)
                        external_wake_word = self._external_wake_words.get(wake_word_id)
                        if not external_wake_word:
                            continue

                        model_info = self._download_external_wake_word(external_wake_word)
                        if not model_info:
                            continue

                        self.state.available_wake_words[wake_word_id] = model_info

                    _LOGGER.debug("Loading wake word: %s", model_info.wake_word_path)
                    self.state.wake_words[wake_word_id] = model_info.load()

                    _LOGGER.info("Wake word set: %s", wake_word_id)
                    active_wake_words.add(wake_word_id)

            # Keep old positions
            remaining_ww = list(active_wake_words)
            placed = set()

            # First, place Wake Words in their old positions.
            for ww_id in remaining_ww:
                if ww_id in old_positions:
                    pos = old_positions[ww_id]
                    if pos < 2:
                        new_wake_words[pos] = ww_id
                        placed.add(ww_id)

            # Add remaining wake words to free slots
            free_slots = [i for i in range(2) if new_wake_words[i] is None]
            for ww_id in remaining_ww:
                if ww_id not in placed and free_slots:
                    pos = free_slots.pop(0)
                    new_wake_words[pos] = ww_id
                    placed.add(ww_id)

            # If only one wake word is left and it was at position 1, position 0 remains None
            # Position 2 automatically stays None if not occupied

            self.state.active_wake_words = active_wake_words
            _LOGGER.debug("Active wake words: %s", active_wake_words)
            _LOGGER.debug("Wake word positions: [0]=%s, [1]=%s", new_wake_words[0], new_wake_words[1])

            self.state.preferences.active_wake_words = new_wake_words
            self.state.save_preferences()
            self.state.wake_words_changed = True

    # ------------------------------------------------------------------
    # Audio streaming
    # ------------------------------------------------------------------

    # handle_audio â€” both channels in ONE message
    def handle_audio(self, audio_chunk: bytes, audio_chunk_2: Optional[bytes] = None) -> None:
        if not self._is_streaming_audio or self.state.muted:
            return
        if _HAS_AUDIO_DATA2 and audio_chunk_2 is not None:
            self.send_messages([VoiceAssistantAudio(data=audio_chunk, data2=audio_chunk_2)])
        else:
            self.send_messages([VoiceAssistantAudio(data=audio_chunk)])

    # ------------------------------------------------------------------
    # Wake word / stop
    # ------------------------------------------------------------------

    def wakeup(self, wake_word: Union[MicroWakeWord, OpenWakeWord]) -> None:

        if self.state.muted:
            # Don't respond to wake words when muted (voice_assistant.stop behavior)
            return

        if self._pipeline_active:
            _LOGGER.debug("Ignoring wake word - pipeline already active")
            return

        wake_word_phrase = wake_word.wake_word  # type: ignore[union-attr]
        _LOGGER.debug("Detected wake word: %s", wake_word_phrase)

        self._end_timer_ring()
        _LOGGER.debug("Stopping timer finished sound")
        self._pipeline_active = True
        self._emit(LVAEvent.WAKE_WORD_DETECTED)
        self.duck()
        if self.state.listen_during_wake_sound:
            _LOGGER.debug("Starting audio streaming immediately (listen_during_wake_sound enabled)")
            self.state.tts_player.play(self.state.wakeup_sound)
            self._start_audio_streaming(wake_word_phrase)
        else:
            self.state.tts_player.play(
                self.state.wakeup_sound,
                done_callback=lambda: self._on_wakeup_sound_finished(wake_word_phrase),
            )

    def _start_audio_streaming(self, wake_word_phrase: str) -> None:
        """Start streaming audio during wake sound detection."""
        _LOGGER.debug(
            "Starting audio streaming for: %s",
            wake_word_phrase,
        )
        self.send_messages([VoiceAssistantRequest(start=True, wake_word_phrase=wake_word_phrase)])
        self._is_streaming_audio = True
        self._emit(LVAEvent.LISTENING)

    def _on_wakeup_sound_finished(self, wake_word_phrase: str) -> None:
        """Callback invoked when the wakeup chime finishes; begin STT streaming."""
        _LOGGER.debug(
            "Wakeup sound finished, starting audio streaming for: %s",
            wake_word_phrase,
        )
        self.send_messages([VoiceAssistantRequest(start=True, wake_word_phrase=wake_word_phrase)])
        self._is_streaming_audio = True
        self._emit(LVAEvent.LISTENING)

    def start_listening(self) -> None:
        """
        Manually start the voice pipeline from a button press.

        Plays ``start_listening_sound`` first, then sends
        ``VoiceAssistantRequest`` and begins streaming audio â€” identical flow
        to ``wakeup()`` but without a wake-word phrase and using the dedicated
        button-press sound instead of the wake-word chime. Also stops ringing timer.
        """
        if self.state.muted:
            return

        if self._pipeline_active:
            _LOGGER.debug("Ignoring start_listening - pipeline already active")
            return

        _LOGGER.debug("Button start_listening triggered")
        self._end_timer_ring()
        _LOGGER.debug("Stopping timer finished sound")
        self._pipeline_active = True
        self.duck()
        self.state.tts_player.play(
            self.state.start_listening_sound,
            done_callback=self._on_start_listening_sound_finished,
        )

    def _on_start_listening_sound_finished(self) -> None:
        """Callback invoked when the start-listening chime finishes; begin STT streaming."""
        _LOGGER.debug("Start-listening sound finished, starting audio streaming")
        self.send_messages([VoiceAssistantRequest(start=True, wake_word_phrase="")])
        self._is_streaming_audio = True
        self._emit(LVAEvent.LISTENING)

    def stop(self) -> None:
        self.state.active_wake_words.discard(self.state.stop_word.id)
        self._pipeline_active = False

        if self._timer_finished:
            self._end_timer_ring()
            self.unduck()
            self.state.tts_player.stop()
            self._emit_after_timer()
            _LOGGER.debug("Stopping timer finished sound")
        else:
            # tts_player.stop() invokes the done_callback (_tts_finished),
            # so we don't call _tts_finished() again explicitly.
            self.state.tts_player.stop()
            _LOGGER.debug("TTS response stopped manually")

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------

    def play_tts(self) -> None:
        if (not self._tts_url) or self._tts_played:
            return

        self._tts_played = True
        _LOGGER.debug("Playing TTS response: %s", self._tts_url)

        self.state.active_wake_words.add(self.state.stop_word.id)
        self._emit(LVAEvent.TTS_SPEAKING)
        self.state.tts_player.play(self._tts_url, done_callback=self._tts_finished)

    def _tts_finished(self) -> None:
        self._pipeline_active = False
        self.state.active_wake_words.discard(self.state.stop_word.id)
        self.send_messages([VoiceAssistantAnnounceFinished()])
        self._emit(LVAEvent.TTS_FINISHED)

        if self._continue_conversation:
            self._continue_conversation = False
            # Keep pipeline active during the settle delay so the mic stays closed
            # and does not capture the tail end of the TTS audio from the speaker.
            self._pipeline_active = True
            self._emit(LVAEvent.LISTENING)
            _LOGGER.debug("Continuing conversation after %.2fs settle delay", self.state.continue_conversation_delay)

            def _start_continued_conversation() -> None:
                if self.state.muted:
                    _LOGGER.debug("Skipping continued conversation: muted")
                    self._pipeline_active = False
                    self.unduck()
                    return
                self.send_messages([VoiceAssistantRequest(start=True)])
                self._is_streaming_audio = True
                _LOGGER.debug("Continued conversation started")

            _qualification_v1.pending_timer(self.state.continue_conversation_delay, _start_continued_conversation).start()
        else:
            self._continue_conversation = False
            self.unduck()
            self._emit(LVAEvent.IDLE)

        _LOGGER.debug("TTS response finished")

    # ------------------------------------------------------------------
    # Ducking
    # ------------------------------------------------------------------

    # BISCUIT: the duck factor follows the user's setting.
    #
    # Upstream calls duck() with no argument and takes mpv_player.py's hardcoded
    # 0.5. On this device every other audio source is ducked by the peripheral
    # agent at a level the user sets from Home Assistant, so leaving this one at
    # 0.5 meant HA media ignored the slider that governed everything else.
    #
    # DEFAULT_DUCK_FACTOR is used whenever the level is unknown - before the
    # peripheral has registered its Number, or on a device with no peripheral -
    # which keeps stock-shaped setups behaving exactly as upstream does.
    DEFAULT_DUCK_FACTOR = 0.5

    def _duck_factor(self) -> float:
        # Off on the settings page means off here too. The peripheral pushes
        # the switch and the level whenever duck.json changes.
        if self.state.peripheral_switch_values.get("duck_enabled") is False:
            return 1.0
        level = self.state.peripheral_number_values.get("duck_level")
        if level is None:
            return self.DEFAULT_DUCK_FACTOR
        try:
            return max(0.0, min(1.0, float(level) / 100.0))
        except (TypeError, ValueError):
            return self.DEFAULT_DUCK_FACTOR

    def duck(self) -> None:
        factor = self._duck_factor()
        if factor >= 1.0:
            _LOGGER.debug("Not ducking music: ducking is off")
            return
        _LOGGER.debug("Ducking music (factor=%.2f)", factor)
        self.state.music_player.duck(factor)

    def unduck(self) -> None:
        _LOGGER.debug("Unducking music")
        self.state.music_player.unduck()

    # ------------------------------------------------------------------
    # Timer finished loop
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Ring state lives on the SHARED state, not on this connection
    # ------------------------------------------------------------------
    #
    # A satellite is created per Home Assistant connection and thrown away when
    # it drops. Holding the ring state here meant a disconnect silenced a timer
    # that was ringing in the room, and the reconnect came back believing
    # nothing was happening - the owner had set a timer, it went off, and the
    # network briefly hiccuping was enough to stop it.
    #
    # Properties rather than renamed attributes so every existing call site -
    # there are more than twenty - keeps working untouched, which is also what
    # makes this reviewable: the diff is the storage, not the logic.
    @property
    def _timer_finished(self) -> bool:
        return getattr(self.state, "_biscuit_timer_finished", False)

    @_timer_finished.setter
    def _timer_finished(self, value) -> None:
        self.state._biscuit_timer_finished = bool(value)

    @property
    def _timer_ring_start(self) -> Optional[float]:
        return getattr(self.state, "_biscuit_timer_ring_start", None)

    @_timer_ring_start.setter
    def _timer_ring_start(self, value) -> None:
        self.state._biscuit_timer_ring_start = value

    @property
    def _timer_generation(self) -> int:
        return getattr(self.state, "_biscuit_timer_generation", 0)

    @_timer_generation.setter
    def _timer_generation(self, value) -> None:
        self.state._biscuit_timer_generation = int(value)

    @property
    def _timer_ring_handle(self):
        return getattr(self.state, "_biscuit_timer_ring_handle", None)

    @_timer_ring_handle.setter
    def _timer_ring_handle(self, value) -> None:
        self.state._biscuit_timer_ring_handle = value

    # The timers Home Assistant has told this device about and not yet
    # cancelled or finished, on the shared state so they survive a reconnect
    # like the ring does. Kept only to answer one question: what should the
    # ring show when one timer stops mattering? A cancel used to send IDLE
    # unconditionally, which blanked a DIFFERENT timer's countdown - or an
    # alarm still ringing in the room. Bounded: Home Assistant is the source of
    # truth, and a lost cancel must not grow this forever.
    MAX_KNOWN_TIMERS = 16

    @property
    def _known_timers(self) -> dict:
        timers = getattr(self.state, "_biscuit_known_timers", None)
        if timers is None:
            timers = self.state._biscuit_known_timers = {}
        while len(timers) > self.MAX_KNOWN_TIMERS:
            timers.pop(next(iter(timers)))
        return timers

    def _emit_after_timer(self) -> None:
        """Show whatever timer state is left, after one timer stops mattering."""
        if self._timer_finished:
            return                      # something is still ringing; leave it
        remaining = sorted(self._known_timers.values(),
                           key=lambda t: t.get("seconds_left", 0))
        if remaining:
            self._emit(LVAEvent.TIMER_TICKING, remaining[0])
        else:
            self._emit(LVAEvent.IDLE)

    def _show_timers_again(self) -> None:
        """Re-show the live timer state after DISCONNECTED cleared the ring.

        The LED ring treats DISCONNECTED like IDLE and clears every assistant
        layer. But a ringing timer and the countdowns Home Assistant announced
        live on the shared state and outlive the connection, so without this the
        alarm kept sounding through a dropped connection while the ring sat idle
        (C4, measured 2026-09-26). Nothing is emitted when no timer is live.
        """
        if self._timer_finished:
            self._emit(LVAEvent.TIMER_RINGING,
                       getattr(self.state, "_biscuit_ringing_timer", None) or {})
        elif self._known_timers:
            self._emit_after_timer()

    def _end_timer_ring(self) -> None:
        """Stop ringing and make every outstanding continuation a no-op.

        Four separate places used to clear the two ring fields by hand - the
        stop word, a wake word, the button and the auto-stop - and none of them
        touched the scheduled continuation. Bumping the generation here is what
        makes a pending one harmless, and cancelling the handle keeps it from
        waking the loop at all.
        """
        self._timer_finished = False
        self._timer_ring_start = None
        self._timer_generation += 1
        handle, self._timer_ring_handle = self._timer_ring_handle, None
        if handle is not None:
            try:
                handle.cancel()
            except Exception:  # noqa: BLE001 - a fired handle is not an error
                pass

    def _play_timer_finished(self, generation: Optional[int] = None) -> None:
        if generation is not None and generation != self._timer_generation:
            _LOGGER.debug("Ignoring a timer ring from generation %s (now %s)",
                          generation, self._timer_generation)
            return
        if not self._timer_finished:
            _LOGGER.debug("Timer finished sound stopped")
            self.unduck()
            self._timer_ring_start = None
            return

        # Auto-stop after timer_max_ring_seconds
        if self._timer_ring_start is not None:
            elapsed = time.monotonic() - self._timer_ring_start
            if elapsed >= self.state.timer_max_ring_seconds:
                _LOGGER.info(
                    "Timer auto-stopped after %.0f seconds (max=%.0f)",
                    elapsed,
                    self.state.timer_max_ring_seconds,
                )
                self._end_timer_ring()
                self.state.active_wake_words.discard(self.state.stop_word.id)
                self.unduck()
                # Nothing was sent here, so the ring kept blinking the alarm
                # after the sound had stopped.
                self._emit_after_timer()
                return

        generation = self._timer_generation
        self.state.tts_player.play(
            self.state.timer_finished_sound,
            done_callback=lambda: self._schedule_next_timer_ring(generation),
        )

    # The gap between rings. Deliberate - a timer that beeps continuously is
    # worse than one that beeps - but it used to be a time.sleep(1.0) inside the
    # done_callback, which python-mpv runs on ITS OWN event thread. That blocked
    # mpv's event dispatch for a second of every ring cycle, and a Stop arriving
    # during the sleep was not acted on until it ended.
    TIMER_RING_GAP_S = 1.0

    def _schedule_next_timer_ring(self, generation: Optional[int] = None) -> None:
        """Ring again after the gap, from the event loop rather than a sleep.

        Falls back to the old blocking behaviour only if there is no loop to
        schedule on, which would mean the peripheral API never started: better a
        timer that rings late than one that stops ringing.
        """
        # Rejected here as well as on firing. tts_player.stop() invokes the
        # done_callback synchronously, so a Stop arrives on this path before
        # anything is scheduled - the cheapest place to drop it.
        if generation is not None and generation != self._timer_generation:
            return
        api = self.state.peripheral_api
        loop = getattr(api, "_loop", None) if api is not None else None
        if loop is None or loop.is_closed():
            call_all(lambda: time.sleep(self.TIMER_RING_GAP_S),
                     lambda: self._play_timer_finished(generation))
            return
        # Checked again when it fires: _play_timer_finished returns immediately
        # if the timer was stopped in the meantime, so a cancelled ring costs one
        # no-op rather than needing the handle tracked and cancelled.
        def arm() -> None:
            # Re-checked on the loop thread, where _timer_generation is only
            # ever changed, so the handle we keep is always the current one.
            if generation is not None and generation != self._timer_generation:
                return
            self._timer_ring_handle = loop.call_later(
                self.TIMER_RING_GAP_S, self._play_timer_finished, generation)

        loop.call_soon_threadsafe(arm)

    def connection_made(self, transport) -> None:
        super().connection_made(transport)
        # Track every live connection so asynchronous entity-state changes can be
        # broadcast to all of them (see ServerState.broadcast).
        if self not in self.state.connections:
            self.state.connections.append(self)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connection_lost(self, exc: Optional[Exception]) -> None:
        _qualification_v1.configured(self, False)
        super().connection_lost(exc)

        self._disconnect_event.set()
        self._is_streaming_audio = False
        self._tts_url = None
        self._tts_played = False
        self._continue_conversation = False
        # A ringing timer is NOT stopped here. It lives on the shared state and
        # keeps ringing across the disconnect, and the reconnecting satellite
        # sees it still ringing. The scheduled continuation holds this dying
        # satellite, which is harmless: everything it touches - the state, the
        # player, the peripheral API - is shared and outlives the connection.
        self._pipeline_active = False

        # Deregister this connection.
        if self in self.state.connections:
            self.state.connections.remove(self)

        # BISCUIT: only the voice client's own departure is a disconnect from
        # Home Assistant. Another client closing - a diagnostic tool, a second
        # dashboard - must not clear the satellite HA is using, nor flash the
        # ring's "no connection" animation at the room.
        was_satellite = self.state.satellite is self
        if was_satellite:
            self.state.satellite = next(
                (c for c in self.state.connections if getattr(c, "_voice_client", False)), None)

        # BISCUIT: a close LVA forced itself so HA re-reads changed entities
        # (PeripheralAPIServer._reconnect_when_settled). HA is back within a
        # second or two, so this is not HA going away: playback is left alone
        # and the ring is not told "disconnected" - unless HA does not return.
        forced = getattr(self, "_forced_refresh", False)

        # Only tear down shared playback/state when the LAST client disconnects.
        # Otherwise a secondary client (a diagnostic tool, a second dashboard, or
        # Home Assistant's own overlapping reconnect) dropping would stop audio
        # that belongs to a client still connected.
        if not self.state.connections and forced:
            self.state.connected = False
        elif not self.state.connections:
            # Stop any ongoing audio playback and wake/stop word processing.
            try:
                self.state.music_player.stop()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Failed to stop music player during disconnect")

            try:
                self.state.tts_player.stop()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Failed to stop TTS player during disconnect")

            self.state.stop_word.is_active = False  # type: ignore[attr-defined]
            self.state.connected = False

        if self.state.mute_switch_entity is not None:
            self.state.mute_switch_entity.sync_with_state()

        if self.state.mic_gain_entity is not None:
            self.state.mic_gain_entity.sync_with_state()

        if self.state.mic_noise_suppression_entity is not None:
            self.state.mic_noise_suppression_entity.sync_with_state()

        if self.state.mic_volume_entity is not None:
            self.state.mic_volume_entity.sync_with_state()

        if not was_satellite:
            _LOGGER.info("A second ESPHome client disconnected; the voice connection is unaffected")
            return
        if self.state.satellite is not None:
            _LOGGER.info("A voice client disconnected; another one is still connected")
            return
        if forced:
            _LOGGER.info("Reconnect forced for changed entities; waiting for Home Assistant")
            try:
                asyncio.get_running_loop().call_later(
                    FORCED_RECONNECT_GRACE_S, self._disconnected_unless_back)
            except RuntimeError:
                self._disconnected_unless_back()
            return
        self._disconnected()

    def _disconnected_unless_back(self) -> None:
        """A forced reconnect HA did not come back from is a disconnect after all."""
        if not any(getattr(c, "_voice_client", False) for c in self.state.connections):
            self._disconnected()

    def _disconnected(self) -> None:
        # Notify peripheral container that HA is no longer reachable
        self._emit(LVAEvent.DISCONNECTED)
        # ...which clears the ring, so show any timer still ringing or counting.
        self._show_timers_again()

        _LOGGER.info("Disconnected from Home Assistant; waiting for reconnection")

    def process_packet(self, msg_type: int, packet_data: bytes) -> None:
        super().process_packet(msg_type, packet_data)

        if msg_type == PROTO_TO_MESSAGE_TYPE[AuthenticationRequest]:
            self.state.connected = True
            _LOGGER.debug("Authentication successful, connected to Home Assistant")

            # Send states after connect
            states: List[message.Message] = []
            _LOGGER.debug("Found %d entities in state", len(self.state.entities))
            for i, entity in enumerate(self.state.entities):
                entity_states = list(entity.handle_message(SubscribeHomeAssistantStatesRequest()))
                states.extend(entity_states)
                _LOGGER.debug("Entity %d (%s) returned %d state messages", i, type(entity).__name__, len(entity_states))

            _LOGGER.debug("Total state messages to send: %d", len(states))
            self.send_messages(states)
            for i, msg in enumerate(states):
                _LOGGER.debug("Sent state message %d: %s", i, type(msg).__name__)
            _LOGGER.debug("All entity states sent after connect")

            # Notify peripherals that Home Assistant is now connected - but
            # not for a second client arriving beside it.
            if self.state.satellite is self:
                self._emit(LVAEvent.ZEROCONF, {"status": "connected"})

    def _claim_satellite(self) -> None:
        """This connection is Home Assistant's voice client: make it THE satellite.

        Only a client that subscribes to the voice assistant does this. If a
        diagnostic client happened to connect first it held the role until now;
        the voice pipeline, the wake word and the button belong here.
        """
        self._voice_client = True
        if self.state.satellite is self:
            return
        _LOGGER.info("Voice client connected; it is now the satellite")
        self.state.satellite = self
        self._emit(LVAEvent.ZEROCONF, {"status": "connected"})

    # ------------------------------------------------------------------
    # External wake word download
    # ------------------------------------------------------------------

    def _download_external_wake_word(self, external_wake_word: VoiceAssistantExternalWakeWord) -> Optional[AvailableWakeWord]:
        eww_dir = self.state.download_dir / "external_wake_words"
        eww_dir.mkdir(parents=True, exist_ok=True)

        config_path = eww_dir / f"{external_wake_word.id}.json"
        should_download_config = not config_path.exists()

        # Check if we need to download the model file
        model_path = eww_dir / f"{external_wake_word.id}.tflite"
        should_download_model = True
        if model_path.exists():
            model_size = model_path.stat().st_size
            if model_size == external_wake_word.model_size:
                with open(model_path, "rb") as model_file:
                    model_hash = hashlib.sha256(model_file.read()).hexdigest()

                if model_hash == external_wake_word.model_hash:
                    should_download_model = False
                    _LOGGER.debug(
                        "Model size and hash match for %s. Skipping download.",
                        external_wake_word.id,
                    )

        if should_download_config or should_download_model:
            # Download config
            _LOGGER.debug("Downloading %s to %s", external_wake_word.url, config_path)
            with urlopen(external_wake_word.url) as request:
                if request.status != 200:
                    _LOGGER.warning(
                        "Failed to download: %s, status=%s",
                        external_wake_word.url,
                        request.status,
                    )
                    return None

                with open(config_path, "wb") as model_file:
                    shutil.copyfileobj(request, model_file)

        if should_download_model:
            # Download model file
            parsed_url = urlparse(external_wake_word.url)
            parsed_url = parsed_url._replace(
                path=posixpath.join(posixpath.dirname(parsed_url.path), model_path.name),
            )
            model_url = urlunparse(parsed_url)

            _LOGGER.debug("Downloading %s to %s", model_url, model_path)
            with urlopen(model_url) as request:
                if request.status != 200:
                    _LOGGER.warning("Failed to download: %s, status=%s", model_url, request.status)
                    return None

                with open(model_path, "wb") as model_file:
                    shutil.copyfileobj(request, model_file)

        return AvailableWakeWord(
            id=external_wake_word.id,
            type=WakeWordType.MICRO_WAKE_WORD,
            wake_word=external_wake_word.wake_word,
            trained_languages=external_wake_word.trained_languages,
            wake_word_path=config_path,
        )
