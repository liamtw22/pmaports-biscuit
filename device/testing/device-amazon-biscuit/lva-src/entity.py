# SPDX-License-Identifier: Apache-2.0
#
# MODIFIED FILE. This is a changed copy of
# linux_voice_assistant/entity.py from OHF-Voice/linux-voice-assistant
# at commit b0c53c41c11e,
#   https://github.com/OHF-Voice/linux-voice-assistant
# licensed under the Apache License 2.0 (LICENSES/Apache-2.0.txt).
# Changed by liamtw22 and contributors, 2026, for the Amazon Echo Dot
# (2nd gen) postmarketOS port. The upstream copyright is unchanged.
# Changes: peripheral switch, sensor and binary-sensor entities; the device
#   master volume; quieter handling of broadcast commands; qualification
#   instrumentation.
import logging
from abc import abstractmethod
from collections.abc import Iterable
from typing import Callable, List, Optional, Union

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]
    BinarySensorStateResponse,
    EventResponse,
    LightCommandRequest,
    LightStateResponse,
    ListEntitiesBinarySensorResponse,
    ListEntitiesEventResponse,
    ListEntitiesLightResponse,
    ListEntitiesMediaPlayerResponse,
    ListEntitiesNumberResponse,
    ListEntitiesRequest,
    ListEntitiesSelectResponse,
    ListEntitiesSensorResponse,
    ListEntitiesSwitchResponse,
    MediaPlayerCommandRequest,
    MediaPlayerStateResponse,
    NumberCommandRequest,
    NumberStateResponse,
    SelectCommandRequest,
    SelectStateResponse,
    SensorStateResponse,
    SubscribeHomeAssistantStatesRequest,
    SwitchCommandRequest,
    SwitchStateResponse,
)
from aioesphomeapi.model import (
    ColorMode,
    EntityCategory,
    MediaPlayerCommand,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    NumberMode,
    SensorStateClass,
)
from google.protobuf import message

from .api_server import APIServer
from .mpv_player import MpvMediaPlayer
from .util import call_all

SUPPORTED_MEDIA_PLAYER_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
)


# BISCUIT: presentation metadata a peripheral may send with a registration.
#
# Kept in a plain dict on the entity rather than as new fields on the
# *Registration dataclasses, because models.py is not ours to edit: it comes
# from the venv tarball plus patches. peripheral_api.entity_meta() validates the
# dict, so everything here can trust its types.
_CATEGORIES = {
    "none": EntityCategory.NONE,
    "config": EntityCategory.CONFIG,
    "diagnostic": EntityCategory.DIAGNOSTIC,
}
_NUMBER_MODES = {"auto": NumberMode.AUTO, "box": NumberMode.BOX, "slider": NumberMode.SLIDER}
_STATE_CLASSES = {
    "measurement": SensorStateClass.MEASUREMENT,
    "total": SensorStateClass.TOTAL,
    "total_increasing": SensorStateClass.TOTAL_INCREASING,
}


def _category(meta: Optional[dict], default: EntityCategory) -> EntityCategory:
    return _CATEGORIES.get((meta or {}).get("entity_category", ""), default)


class ESPHomeEntity:
    def __init__(self, server: APIServer) -> None:
        self.server = server

    @abstractmethod
    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        pass


# -----------------------------------------------------------------------------


class MediaPlayerEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        music_player: MpvMediaPlayer,
        announce_player: MpvMediaPlayer,
        initial_volume: float = 1.0,
        on_volume_changed: Optional[Callable[[float], None]] = None,
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self.state = MediaPlayerState.IDLE
        self.volume = max(0.0, min(1.0, initial_volume))
        self.muted = False
        self.previous_volume = 1.0
        self.music_player = music_player
        self.announce_player = announce_player
        self._on_volume_changed = on_volume_changed
        self.apply_volume_from_state(initial_volume)
        self._log = logging.getLogger(f"{self.__class__.__name__}[{self.key}]")

    def _broadcast_state(self, msgs: Iterable[message.Message]) -> None:
        """Push an asynchronous state change to all connected clients.

        Playback-completion callbacks fire outside any request, so the update
        must reach every subscribed client rather than the single connection in
        ``self.server`` (which may belong to another client, or be closed).
        """
        state = getattr(self.server, "state", None)
        if state is not None:
            state.broadcast(msgs)
        else:  # pragma: no cover - no ServerState (e.g. a bare APIServer)
            self.server.send_messages(msgs)

    def play(
        self,
        url: Union[str, List[str]],
        announcement: bool = False,
        done_callback: Optional[Callable[[], None]] = None,
    ) -> Iterable[message.Message]:
        if announcement:
            self._log.debug("PLAY: announcement true")
            if self.music_player.is_playing:
                # Announce, resume music
                self.music_player.pause()
                self.announce_player.play(
                    url,
                    done_callback=lambda: call_all(self.music_player.resume, done_callback),
                )
            else:
                # Announce, idle
                self.announce_player.play(
                    url,
                    done_callback=lambda: call_all(
                        lambda: self._broadcast_state([self._update_state(MediaPlayerState.IDLE)]),
                        done_callback,
                    ),
                )
        else:
            self._log.debug("PLAY: announcement false")
            # Music
            self.music_player.play(
                url,
                done_callback=lambda: call_all(
                    lambda: self._broadcast_state([self._update_state(MediaPlayerState.IDLE)]),
                    done_callback,
                ),
            )

        yield self._update_state(MediaPlayerState.PLAYING)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        self._log.debug("handle_message called with msg: %s", msg)

        # Suppress warning for irrelevant NumberCommandRequest
        if isinstance(msg, (NumberCommandRequest, SelectCommandRequest)):
            return

        if isinstance(msg, MediaPlayerCommandRequest) and (msg.key == self.key):
            self._log.debug("MediaPlayerCommandRequest matched for this key")

            if msg.has_media_url:
                self._log.debug("Executing PLAY")
                self._log.debug("Message has media URL: %s", msg.media_url)
                announcement = msg.has_announcement and msg.announcement
                yield from self.play(msg.media_url, announcement=announcement)

            elif msg.has_command:
                self._log.debug("Message has command: %s", msg.command)
                command = MediaPlayerCommand(msg.command)

                if msg.command == MediaPlayerCommand.PAUSE:
                    self._log.debug("Executing PAUSE")
                    self.music_player.pause()
                    yield self._update_state(MediaPlayerState.PAUSED)

                elif msg.command == MediaPlayerCommand.PLAY:
                    self._log.debug("Executing PLAY / RESUME")
                    self.music_player.resume()
                    yield self._update_state(MediaPlayerState.PLAYING)

                elif command == MediaPlayerCommand.STOP:
                    self._log.debug("Executing STOP")
                    self.music_player.stop()
                    yield self._update_state(MediaPlayerState.IDLE)

                elif command == MediaPlayerCommand.MUTE:
                    self._log.debug("Executing MUTE")
                    if not self.muted:
                        self.previous_volume = self.volume
                        self.volume = 0
                        # The device codec/DSP is the one master volume. Its
                        # VolumeChanged bridge immediately applies this zero;
                        # keeping the local players at unity avoids a second
                        # attenuation after unmute.
                        self.muted = True
                        if hasattr(self.server, "state") and getattr(self.server, "state", None) is not None:
                            self.server.state.persist_volume(self.volume)
                    yield self._update_state(self.state)

                elif command == MediaPlayerCommand.UNMUTE:
                    self._log.debug("Executing UNMUTE")
                    if self.muted:
                        self.volume = self.previous_volume
                        self.muted = False
                        if hasattr(self.server, "state") and getattr(self.server, "state", None) is not None:
                            self.server.state.persist_volume(self.volume)
                    yield self._update_state(self.state)

            elif msg.has_volume:
                self._log.debug("Message has volume: %.2f", msg.volume)
                self._apply_volume(msg.volume, persist=True)
                if hasattr(self.server, "state") and getattr(self.server, "state", None) is not None:
                    self._log.debug("Persisting volume to preferences")
                    self.server.state.persist_volume(self.volume)
                else:
                    self._log.warning("Cannot persist volume - server.state not available")
                yield self._update_state(self.state)

        elif isinstance(msg, ListEntitiesRequest):
            self._log.debug("ListEntitiesRequest received")
            yield ListEntitiesMediaPlayerResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                supports_pause=True,
                feature_flags=SUPPORTED_MEDIA_PLAYER_FEATURES,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self._log.debug("SubscribeHomeAssistantStatesRequest received")
            yield self._get_state_message()
        else:
            # Not an error: satellite.py broadcasts entity commands to every
            # entity and each matches on its own key, so seeing another
            # entity's message is the normal case rather than a fault.
            self._log.debug("Message for another entity ignored: %s", type(msg))

    def _update_state(self, new_state: MediaPlayerState) -> MediaPlayerStateResponse:
        self._log.debug("SET NEW STATE: %s => %s", self.state, new_state)
        self._log.debug("SET NEW STATE: %s => %s", self.state.name, new_state.name)
        self.state = new_state
        return self._get_state_message()

    def _get_state_message(self) -> MediaPlayerStateResponse:
        return MediaPlayerStateResponse(
            key=self.key,
            state=self.state,
            volume=self.volume,
            muted=self.muted,
        )

    def apply_volume_from_state(self, volume: float) -> None:
        """Synchronize the local volume with the stored state without persisting."""

        clamped = max(0.0, min(1.0, float(volume)))

        if self.muted:
            self.previous_volume = clamped
            return

        self._apply_volume(clamped, persist=False)

    def set_volume_callback(self, callback: Optional[Callable[[float], None]]) -> None:
        """Update the callback invoked when the volume changes."""

        self._on_volume_changed = callback

    def _apply_volume(
        self,
        volume: float,
        *,
        persist: bool,
        remember: bool = True,
    ) -> None:
        normalized = max(0.0, min(1.0, float(volume)))

        # ``volume`` represents biscuit-audio's physical master on this
        # appliance. It controls the codec, DSP loudness table and ring. The
        # mpv players must stay at unity so a 50% master is not rendered as
        # 50% at the codec multiplied by 50% in mpv - and mpv's curve is cubic,
        # so its 50% is -18 dB, not -6 dB. Measured: 50% -> -18.06 dB,
        # 25% -> -36.12 dB, on top of the DSP's own taper.

        self.volume = normalized

        if remember:
            self.previous_volume = normalized

        if self._on_volume_changed and persist:
            self._on_volume_changed(normalized)


# -----------------------------------------------------------------------------


class MuteSwitchEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_muted: Callable[[], bool],
        set_muted: Callable[[bool], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_muted = get_muted
        self._set_muted = set_muted
        self._switch_state = self._get_muted()  # Sync internal state with actual muted value on init

    def update_set_muted(self, set_muted: Callable[[bool], None]) -> None:
        # Update the callback used to change the mute state.
        self._set_muted = set_muted

    def update_get_muted(self, get_muted: Callable[[], bool]) -> None:
        # Update the callback used to read the mute state.
        self._get_muted = get_muted

    def sync_with_state(self) -> None:
        # Sync internal switch state with the actual mute state.
        self._switch_state = self._get_muted()

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SwitchCommandRequest) and (msg.key == self.key):
            # User toggled the switch - update our internal state and trigger actions
            new_state = bool(msg.state)
            self._switch_state = new_state
            self._set_muted(new_state)
            # Return the new state immediately
            yield SwitchStateResponse(key=self.key, state=self._switch_state)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:microphone-off",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            # Always return our internal switch state
            self.sync_with_state()
            yield SwitchStateResponse(key=self.key, state=self._switch_state)


class ThinkingSoundEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_thinking_sound_enabled: Callable[[], bool],
        set_thinking_sound_enabled: Callable[[bool], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_thinking_sound_enabled = get_thinking_sound_enabled
        self._set_thinking_sound_enabled = set_thinking_sound_enabled
        self._switch_state = self._get_thinking_sound_enabled()  # Sync internal state

    def update_get_thinking_sound_enabled(self, get_thinking_sound_enabled: Callable[[], bool]) -> None:
        # Update the callback used to read the thinking sound enabled state.
        self._get_thinking_sound_enabled = get_thinking_sound_enabled

    def update_set_thinking_sound_enabled(self, set_thinking_sound_enabled: Callable[[bool], None]) -> None:
        # Update the callback used to change the thinking sound enabled state.
        self._set_thinking_sound_enabled = set_thinking_sound_enabled

    def sync_with_state(self) -> None:
        # Sync internal switch state with the actual thinking sound enabled state.
        self._switch_state = self._get_thinking_sound_enabled()

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SwitchCommandRequest) and (msg.key == self.key):
            # User toggled the switch - update our internal state and trigger actions
            new_state = bool(msg.state)
            self._switch_state = new_state
            self._set_thinking_sound_enabled(new_state)
            # Return the new state immediately
            yield SwitchStateResponse(key=self.key, state=self._switch_state)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:music-note",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            # Always return our internal switch state
            self.sync_with_state()
            yield SwitchStateResponse(key=self.key, state=self._switch_state)


class PeripheralSwitchEntity(ESPHomeEntity):
    """A Switch a peripheral registered over the peripheral API. BISCUIT addition.

    Generic counterpart to MicSettingEntity: the value lives on the app state,
    not on the entity, so a Home Assistant reconnect (which rebuilds the
    satellite) does not reset it.
    """

    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_value: Callable[[], bool],
        set_value: Callable[[bool], None],
        icon: str = "mdi:toggle-switch",
        meta: Optional[dict] = None,
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_value = get_value
        self._set_value = set_value
        self.icon = icon
        self.meta = dict(meta or {})

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SwitchCommandRequest) and (msg.key == self.key):
            self._set_value(bool(msg.state))
            yield SwitchStateResponse(key=self.key, state=bool(msg.state))
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=_category(self.meta, EntityCategory.CONFIG),
                icon=self.icon,
                device_class=self.meta.get("device_class", ""),
                disabled_by_default=self.meta.get("disabled_by_default", False),
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield SwitchStateResponse(key=self.key, state=bool(self._get_value()))


class MicSettingEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_value: Callable[[], Union[float, str]],
        set_value: Callable[[Union[float, str]], None],
        min_value: float = 0.0,
        max_value: float = 1.0,
        options: Optional[List[str]] = None,
        icon: str = "mdi:microphone",
        meta: Optional[dict] = None,
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self.options = options  # If present, this behaves as a Dropdown
        self.min_value = min_value
        self.max_value = max_value
        self._get_value = get_value
        self._set_value = set_value
        self._state = self._get_value()
        self.icon = icon
        # BISCUIT: step, unit, mode, category... see _category above. A Number
        # used to be fixed at step 1 with no unit, so a 0.5 dB setting could
        # not be reached from Home Assistant at all.
        self.meta = dict(meta or {})

    def sync_with_state(self) -> None:
        """Sync internal state with the actual value."""
        self._state = self._get_value()

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        # --- 1. HANDLE COMMANDS FROM HOME ASSISTANT ---
        if self.options:
            if isinstance(msg, SelectCommandRequest) and (msg.key == self.key):
                # Biscuit conditional selects use HA's reserved unavailable
                # state. Do not optimistically acknowledge a stale command or
                # forward it to hardware while the control is inactive.
                if self.options == ["unavailable"] or msg.state not in self.options:
                    self.sync_with_state()
                    yield SelectStateResponse(key=self.key, state=str(self._state))
                    return
                new_val = msg.state
                self._state = new_val
                self._set_value(new_val)
                yield SelectStateResponse(key=self.key, state=new_val)
        else:
            if isinstance(msg, NumberCommandRequest) and (msg.key == self.key):
                new_val = msg.state
                self._state = new_val
                self._set_value(new_val)
                yield NumberStateResponse(key=self.key, state=new_val)

        # --- 2. DISCOVERY (TELL HA WHAT TYPE TO SHOW) ---
        if isinstance(msg, ListEntitiesRequest):
            if self.options:
                yield ListEntitiesSelectResponse(
                    object_id=self.object_id,
                    key=self.key,
                    name=self.name,
                    options=self.options,
                    entity_category=_category(self.meta, EntityCategory.CONFIG),
                    icon=self.icon,
                    disabled_by_default=self.meta.get("disabled_by_default", False),
                )
            else:
                yield ListEntitiesNumberResponse(
                    object_id=self.object_id,
                    key=self.key,
                    name=self.name,
                    min_value=self.min_value,
                    max_value=self.max_value,
                    step=float(self.meta.get("step", 1.0)),
                    unit_of_measurement=self.meta.get("unit_of_measurement", ""),
                    mode=_NUMBER_MODES.get(self.meta.get("mode", ""), NumberMode.AUTO),
                    device_class=self.meta.get("device_class", ""),
                    entity_category=_category(self.meta, EntityCategory.CONFIG),
                    icon=self.icon,
                    disabled_by_default=self.meta.get("disabled_by_default", False),
                )

        # --- 3. INITIAL SYNC / STATE UPDATES ---
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            if self.options:
                yield SelectStateResponse(key=self.key, state=str(self._state))
            else:
                yield NumberStateResponse(key=self.key, state=float(self._state))

    def update_get_value(self, get_value: Callable[[], Union[float, str]]) -> None:
        self._get_value = get_value

    def update_set_value(self, set_value: Callable[[Union[float, str]], None]) -> None:
        self._set_value = set_value


# -----------------------------------------------------------------------------


class WakeWord1SensitivityNumberEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_sensitivity: Callable[[], float],
        set_sensitivity: Callable[[float], None],
        initial_value: float = 0.5,
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_sensitivity = get_sensitivity
        self._set_sensitivity = set_sensitivity
        self.value = initial_value
        self._log = logging.getLogger(f"{self.__class__.__name__}[{self.key}]")

    def update_get_sensitivity(self, get_sensitivity: Callable[[], float]) -> None:
        self._get_sensitivity = get_sensitivity

    def update_set_sensitivity(self, set_sensitivity: Callable[[float], None]) -> None:
        self._set_sensitivity = set_sensitivity

    def sync_with_state(self) -> None:
        old_value = self.value
        self.value = self._get_sensitivity()
        self._log.debug("Entity synchronized: old=%.3f new=%.3f", old_value, self.value)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, NumberCommandRequest) and (msg.key == self.key):
            new_value = float(msg.state)
            self._log.debug("Sensitivity value changed: %s => %s", self.value, new_value)
            self.value = new_value
            self._set_sensitivity(new_value)
            yield NumberStateResponse(key=self.key, state=self.value)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                min_value=0.0,
                max_value=1.0,
                step=0.001,
                mode=NumberMode.BOX,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield NumberStateResponse(key=self.key, state=self.value)


class WakeWord2SensitivityNumberEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_sensitivity: Callable[[], float],
        set_sensitivity: Callable[[float], None],
        initial_value: float = 0.5,
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_sensitivity = get_sensitivity
        self._set_sensitivity = set_sensitivity
        self.value = initial_value
        self._log = logging.getLogger(f"{self.__class__.__name__}[{self.key}]")

    def update_get_sensitivity(self, get_sensitivity: Callable[[], float]) -> None:
        self._get_sensitivity = get_sensitivity

    def update_set_sensitivity(self, set_sensitivity: Callable[[float], None]) -> None:
        self._set_sensitivity = set_sensitivity

    def sync_with_state(self) -> None:
        old_value = self.value
        self.value = self._get_sensitivity()
        self._log.debug("Entity synchronized: old=%.3f new=%.3f", old_value, self.value)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, NumberCommandRequest) and (msg.key == self.key):
            new_value = float(msg.state)
            self._log.debug("Second wake word sensitivity value changed: %s => %s", self.value, new_value)
            self.value = new_value
            self._set_sensitivity(new_value)
            yield NumberStateResponse(key=self.key, state=self.value)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                min_value=0.0,
                max_value=1.0,
                step=0.001,
                mode=NumberMode.BOX,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield NumberStateResponse(key=self.key, state=self.value)


class StopWordSensitivityNumberEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_sensitivity: Callable[[], float],
        set_sensitivity: Callable[[float], None],
        initial_value: float = 0.5,
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_sensitivity = get_sensitivity
        self._set_sensitivity = set_sensitivity
        self.value = initial_value
        self._log = logging.getLogger(f"{self.__class__.__name__}[{self.key}]")

    def update_get_sensitivity(self, get_sensitivity: Callable[[], float]) -> None:
        self._get_sensitivity = get_sensitivity

    def update_set_sensitivity(self, set_sensitivity: Callable[[float], None]) -> None:
        self._set_sensitivity = set_sensitivity

    def sync_with_state(self) -> None:
        old_value = self.value
        self.value = self._get_sensitivity()
        self._log.debug("Entity synchronized: old=%.3f new=%.3f", old_value, self.value)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, NumberCommandRequest) and (msg.key == self.key):
            new_value = float(msg.state)
            self._log.debug("Stop word sensitivity value changed: %s => %s", self.value, new_value)
            self.value = new_value
            self._set_sensitivity(new_value)
            yield NumberStateResponse(key=self.key, state=self.value)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:hand-back-left",
                min_value=0.0,
                max_value=1.0,
                step=0.001,
                mode=NumberMode.BOX,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield NumberStateResponse(key=self.key, state=self.value)


class LEDLightEntity(ESPHomeEntity):
    """RGB Light entity for peripheral LEDs.

    The peripheral declares its capabilities (effects, RGB, brightness)
    via the register_light command. When Home Assistant changes the
    entity, on_changed fires so the peripheral API server can broadcast
    a light_command event back to the peripheral, which applies the
    new state to its hardware.
    """

    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        effects: Optional[List[str]] = None,
        supports_rgb: bool = True,
        supports_brightness: bool = True,
        supports_color_temperature: bool = False,
        min_mireds: float = 153.0,
        max_mireds: float = 500.0,
        on_changed: Optional[Callable[[], None]] = None,
        icon: str = "mdi:led-strip-variant",
        meta: Optional[dict] = None,
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self.icon = icon
        self.meta = dict(meta or {})
        self._on_changed = on_changed
        self.effects_list: List[str] = list(effects) if effects else []
        self._supports_rgb = supports_rgb
        self._supports_brightness = supports_brightness
        # BISCUIT: colour temperature as a SECOND supported colour mode.
        # The ring is RGB-only hardware with no white channel, so the
        # peripheral converts mireds to an RGB approximation. Declaring both
        # modes is what makes HA show a colour wheel AND a temperature slider.
        self._supports_cct = supports_color_temperature
        self._min_mireds = float(min_mireds)
        self._max_mireds = float(max_mireds)
        # Track which mode the user last drove, so state reports back the mode
        # HA actually set rather than always claiming RGB.
        self._active_cct = False
        self.color_temperature: float = 250.0

        # Off by default, matching the HA Voice PE LED Ring
        # (restore_mode RESTORE_DEFAULT_OFF): the resting LEDs stay dark
        # until the user turns the light on. Voice animations are driven
        # separately by the peripheral and play regardless.
        self.is_on: bool = False
        # Match the HA Voice PE LED Ring initial state: a light blue at 66%
        # brightness (red 9.4%, green 73.3%, blue 94.9%).
        self.brightness: float = 0.66
        self.red: float = 0.094
        self.green: float = 0.733
        self.blue: float = 0.949
        # Default effect: first declared, or empty if none. The peripheral
        # decides what that is by the order it declares them in.
        self.effect: str = self.effects_list[0] if self.effects_list else ""

    def update_on_changed(self, on_changed: Optional[Callable[[], None]]) -> None:
        self._on_changed = on_changed

    def update_effects(self, effects: List[str]) -> None:
        """BISCUIT: take a changed effect list from a re-registration.

        An effect that is no longer offered falls back to the first one, as a
        new entity would, rather than leaving HA showing a name it cannot pick.
        """
        self.effects_list = list(effects)
        if self.effect not in self.effects_list:
            self.effect = self.effects_list[0] if self.effects_list else ""

    def apply_state(self, data: dict) -> None:
        """BISCUIT: take the peripheral's own record of this light.

        The light_state command, sent when the peripheral connects. It fills in
        what HA would otherwise only learn by setting it, so a restart of either
        side no longer shows the ring off while it is lit. Never calls
        on_changed: this IS the peripheral's state, and echoing it back as a
        light_command would be a loop.
        """
        if isinstance(data.get("state"), bool):
            self.is_on = data["state"]
        for attr in ("brightness", "red", "green", "blue"):
            value = data.get(attr)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(self, attr, max(0.0, min(1.0, float(value))))
        mireds = data.get("color_temperature")
        if isinstance(mireds, (int, float)) and not isinstance(mireds, bool):
            self.color_temperature = max(self._min_mireds, min(self._max_mireds, float(mireds)))
        if data.get("color_mode") in ("rgb", "color_temperature"):
            self._active_cct = self._supports_cct and data["color_mode"] == "color_temperature"
        if data.get("effect") in self.effects_list:
            self.effect = data["effect"]

    def _color_mode(self) -> ColorMode:
        # The mode currently in effect, which is what LightStateResponse must
        # carry. HA uses this to decide which control to highlight.
        if self._supports_cct and self._active_cct:
            return ColorMode.COLOR_TEMPERATURE
        if self._supports_rgb:
            return ColorMode.RGB
        if self._supports_brightness:
            return ColorMode.BRIGHTNESS
        return ColorMode.ON_OFF

    def _supported_color_modes(self) -> List[int]:
        # Every mode the light can do. A single-element list would hide the
        # temperature slider even though the command is accepted.
        modes: List[int] = []
        if self._supports_rgb:
            modes.append(int(ColorMode.RGB))
        if self._supports_cct:
            modes.append(int(ColorMode.COLOR_TEMPERATURE))
        if not modes:
            modes.append(int(ColorMode.BRIGHTNESS if self._supports_brightness
                             else ColorMode.ON_OFF))
        return modes

    def state_dict(self) -> dict:
        """Payload for the light_command event.

        Includes object_id so a peripheral that registered more than one
        Light can route the event to the right hardware.
        """
        return {
            "object_id": self.object_id,
            "state": self.is_on,
            "brightness": self.brightness,
            "brightness_changed": getattr(self, "_brightness_changed", False),
            "rgb_changed": getattr(self,"_rgb_changed",False),
            "state_changed": getattr(self,"_state_changed",False),
            "red": self.red,
            "green": self.green,
            "blue": self.blue,
            "effect": self.effect,
            # BISCUIT: the peripheral needs both, to know whether to honour
            # the RGB triple or synthesise a colour from the temperature.
            "color_temperature": self.color_temperature,
            "color_mode": "color_temperature" if (self._supports_cct and self._active_cct) else "rgb",
        }

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, LightCommandRequest) and msg.key == self.key:
            self._brightness_changed = bool(msg.has_brightness)
            self._rgb_changed = bool(msg.has_rgb)
            self._state_changed = bool(msg.has_state)
            changed = False
            if msg.has_state:
                self.is_on = bool(msg.state)
                changed = True
            if msg.has_brightness and self._supports_brightness:
                self.brightness = max(0.0, min(1.0, float(msg.brightness)))
                changed = True
            if msg.has_rgb and self._supports_rgb:
                self.red = max(0.0, min(1.0, float(msg.red)))
                self.green = max(0.0, min(1.0, float(msg.green)))
                self.blue = max(0.0, min(1.0, float(msg.blue)))
                self._active_cct = False
                changed = True
            if getattr(msg, "has_color_temperature", False) and self._supports_cct:
                self.color_temperature = max(
                    self._min_mireds, min(self._max_mireds, float(msg.color_temperature)))
                self._active_cct = True
                changed = True
            if msg.has_effect:
                requested = str(msg.effect)
                if requested in self.effects_list:
                    self.effect = requested
                    changed = True
            if changed and self._on_changed is not None:
                self._on_changed()
            yield self._state_response()
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesLightResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                supported_color_modes=self._supported_color_modes(),
                min_mireds=self._min_mireds,
                max_mireds=self._max_mireds,
                effects=self.effects_list,
                icon=self.icon,
                entity_category=_category(self.meta, EntityCategory.CONFIG),
                disabled_by_default=self.meta.get("disabled_by_default", False),
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield self._state_response()

    def _state_response(self) -> LightStateResponse:
        return LightStateResponse(
            key=self.key,
            state=self.is_on,
            brightness=self.brightness,
            color_mode=int(self._color_mode()),
            color_brightness=self.brightness,
            red=self.red,
            green=self.green,
            blue=self.blue,
            color_temperature=self.color_temperature,
            effect=self.effect,
        )


class PeripheralSensorEntity(ESPHomeEntity):
    """A read-only numeric measurement published by a peripheral. BISCUIT.

    Not a Number: a Number renders as a control the user can drag, and dragging
    this would do nothing. The state starts as None and nothing is reported
    until the peripheral sends a real reading - an invented zero would show up
    in Home Assistant as a measurement, and be indistinguishable from a genuine
    dark room.
    """

    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        unit_of_measurement: str = "",
        device_class: str = "",
        accuracy_decimals: int = 0,
        icon: str = "",
        meta: Optional[dict] = None,
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self.unit_of_measurement = unit_of_measurement
        self.device_class = device_class
        self.accuracy_decimals = accuracy_decimals
        self.icon = icon
        self.meta = dict(meta or {})
        self._state: Optional[float] = None
        self._log = logging.getLogger(f"{self.__class__.__name__}[{self.key}]")

    def update_state(self, value: float) -> None:
        self._state = float(value)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSensorResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                unit_of_measurement=self.unit_of_measurement,
                accuracy_decimals=self.accuracy_decimals,
                device_class=self.device_class,
                icon=self.icon,
                state_class=_STATE_CLASSES.get(self.meta.get("state_class", ""), SensorStateClass.NONE),
                entity_category=_category(self.meta, EntityCategory.NONE),
                disabled_by_default=self.meta.get("disabled_by_default", False),
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            if self._state is not None:
                yield self._get_state_message()

    def _get_state_message(self) -> SensorStateResponse:
        return SensorStateResponse(
            key=self.key,
            state=self._state or 0.0,
            missing_state=self._state is None,
        )


class PeripheralBinarySensorEntity(ESPHomeEntity):
    """A read-only on/off state published by a peripheral. BISCUIT addition.

    The float sensor above would show a plug as "1" and "0". A binary sensor
    with a device class is what Home Assistant renders as Plugged in and
    Unplugged, and what an automation can trigger on without a template.

    Unknown until the peripheral says otherwise, for the same reason as the
    float sensor: an invented "off" is indistinguishable from a real one.
    """

    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        device_class: str = "",
        icon: str = "",
        meta: Optional[dict] = None,
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self.device_class = device_class
        self.icon = icon
        self.meta = dict(meta or {})
        self._state: Optional[bool] = None

    def update_state(self, value: Optional[bool]) -> None:
        self._state = None if value is None else bool(value)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesBinarySensorResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                device_class=self.device_class,
                icon=self.icon,
                entity_category=_category(self.meta, EntityCategory.NONE),
                disabled_by_default=self.meta.get("disabled_by_default", False),
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            if self._state is not None:
                yield self._get_state_message()

    def _get_state_message(self) -> BinarySensorStateResponse:
        return BinarySensorStateResponse(
            key=self.key,
            state=bool(self._state),
            missing_state=self._state is None,
        )


class ButtonEventSensorEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self.event_types = ["single_press", "double_press", "triple_press", "long_press"]
        self._current_event: Optional[str] = None
        self._log = logging.getLogger(f"{self.__class__.__name__}[{self.key}]")

    def update_state(self, event_type: str) -> None:
        """Update the event state with a button press event."""
        self._current_event = event_type
        self._log.debug("Button event state updated: %s", event_type)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesEventResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                device_class="button",
                event_types=self.event_types,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            # Wait until a press fires: yielding with an empty
            # event_type makes HA reject the state and fail the
            # whole ESPHome config entry to load.
            if self._current_event:
                yield self._get_state_message()

    def _get_state_message(self) -> EventResponse:
        return EventResponse(
            key=self.key,
            event_type=self._current_event or "",
        )


# Backward compatibility export aliases
__all__ = [
    "ESPHomeEntity",
    "PeripheralSensorEntity",
    "PeripheralBinarySensorEntity",
    "MediaPlayerEntity",
    "MuteSwitchEntity",
    "ThinkingSoundEntity",
    "LEDLightEntity",
    "ButtonEventSensorEntity",
    "WakeWord1SensitivityNumberEntity",
    "WakeWord2SensitivityNumberEntity",
    "StopWordSensitivityNumberEntity",
    # Old class names for backward compatibility
    "WakeWordSensitivityNumberEntity",
    "SecondWakeWordSensitivityNumberEntity",
]

WakeWordSensitivityNumberEntity = WakeWord1SensitivityNumberEntity
SecondWakeWordSensitivityNumberEntity = WakeWord2SensitivityNumberEntity


# -----------------------------------------------------------------------------
