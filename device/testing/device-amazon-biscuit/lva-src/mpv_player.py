# SPDX-License-Identifier: Apache-2.0
#
# MODIFIED FILE. This is a changed copy of
# linux_voice_assistant/mpv_player.py from OHF-Voice/linux-voice-assistant
# at commit b0c53c41c11e,
#   https://github.com/OHF-Voice/linux-voice-assistant
# licensed under the Apache License 2.0 (LICENSES/Apache-2.0.txt).
# Changed by liamtw22 and contributors, 2026, for the Amazon Echo Dot
# (2nd gen) postmarketOS port. The upstream copyright is unchanged.
# Changes: qualification instrumentation hooks (read-only); waits for the
# speaker amplifier before a sound starts (_wait_for_amplifier), off the
# caller's thread.
# mpv_player.py
from . import qualification_v1 as _qualification_v1
import logging
import os
import threading
import time
from typing import Callable, List, Optional, Union

from .player.libmpv import LibMpvPlayer
from .player.state import PlayerState


# biscuit-dsp's speaker amplifier state, and its control FIFO. See
# biscuit-amp-wait: after ten seconds of silence the amplifier is off, and comes
# back about 275 ms after the next sound starts, so the wake chime, the mute
# sound and the start of every reply lost their beginning and were cut by the
# click of the amplifier switching on.
_AMP_STATE = "/run/biscuit-dsp/amp"
_DSP_CONTROL = "/run/biscuit-dsp/control"
_AMP_WAIT_S = 0.6


def _read_amp_state() -> str:
    try:
        with open(_AMP_STATE) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _wait_for_amplifier() -> None:
    """Return once the amplifier can be heard; wake it if it is off.

    At once when it is already up or biscuit-dsp is not running, and never
    longer than _AMP_WAIT_S: a late sound is better than none.
    """
    state = _read_amp_state()
    if state in ("", "ready"):
        return
    if state == "off":
        try:
            fd = os.open(_DSP_CONTROL, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, b"wake\n")
            finally:
                os.close(fd)
        except OSError:
            pass
    deadline = time.monotonic() + _AMP_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(0.01)
        if _read_amp_state() in ("", "ready"):
            return


class MpvMediaPlayer:
    """
    Linux Voice Assistant MediaPlayer implementation based on libmpv.

    This class provides the MediaPlayer interface expected by LVA and
    delegates all playback logic to LibMpvPlayer.
    """

    def __init__(self, device: str | None = None) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._player = LibMpvPlayer(device=device)
        self._done_callback: Optional[Callable[[], None]] = None
        self._playlist: List[str] = []
        # BISCUIT: a sound waiting for the amplifier. play() is called on the
        # satellite's event loop, so the wait happens in a thread of its own;
        # every play() and stop() bumps the generation, and a waiting start
        # whose generation is no longer current is dropped.
        self._start_lock = threading.Lock()
        self._generation = 0
        self._pending = False

        self._log.debug("MpvMediaPlayer initialized (device=%s)", device)

    @_qualification_v1.activity
    def play(
        self,
        url: Union[str, List[str]],
        done_callback: Optional[Callable[[], None]] = None,
        stop_first: bool = False,
    ) -> None:
        """
        Play a media URL.

        Args:
            url: Media URL or list of URLs for sequential playback.
            done_callback: Optional callback invoked when playback finishes.
            stop_first: Kept for API compatibility.
        """
        # Handle single URL vs list
        if isinstance(url, str):
            urls = [url]
        else:
            urls = list(url)  # Copy the list

        if not urls:
            self._log.warning("play() called with empty URL list")
            return

        # Track is changing - stop if needed
        if self._done_callback is not None:
            if self._player.state() != PlayerState.IDLE:
                self._log.debug("Stopping active playback before starting new media")
                self._player.stop(for_replacement=True)
            self._done_callback = None

        self._log.info("Playing %d URL(s): %s", len(urls), urls[0])

        # Store playlist and callback
        self._playlist = urls
        self._done_callback = done_callback

        # Start playing first URL, once the speaker can be heard. Normally at
        # once: the amplifier is held on unless mbcl.conf sets idle_release_s.
        with self._start_lock:
            self._generation += 1
            generation = self._generation
            self._pending = _read_amp_state() not in ("", "ready")
        if not self._pending:
            self._start(generation, stop_first)
            return
        threading.Thread(
            target=self._start_when_audible, args=(generation, stop_first), daemon=True
        ).start()

    def _start_when_audible(self, generation: int, stop_first: bool) -> None:
        _wait_for_amplifier()
        self._start(generation, stop_first)

    def _start(self, generation: int, stop_first: bool) -> None:
        with self._start_lock:
            if generation != self._generation or not self._playlist:
                return  # stopped, or replaced by a newer play(), while waiting
            self._pending = False
            next_url = self._playlist.pop(0)
            self._player.play(next_url, done_callback=self._on_track_finished, stop_first=stop_first)

    @_qualification_v1.activity
    def _on_track_finished(self) -> None:
        """Called when a track finishes - plays next or invokes done callback."""
        if self._playlist:
            # More tracks to play
            next_url = self._playlist.pop(0)
            self._log.debug("Playing next URL from playlist: %s", next_url)
            self._player.play(next_url, done_callback=self._on_track_finished, stop_first=False)
        else:
            # Playlist finished
            callback = self._done_callback
            self._done_callback = None

            if callback:
                self._log.debug("Playlist finished, invoking done_callback")
                try:
                    callback()
                except Exception as e:
                    self._log.exception("Error in done_callback: %s", e)

    def pause(self) -> None:
        """Pause playback."""
        self._log.debug("pause() called")
        self._player.pause()

    def resume(self) -> None:
        """Resume playback."""
        self._log.debug("resume() called")
        self._player.resume()

    @_qualification_v1.activity
    def stop(self) -> None:
        """Stop playback and invoke the done callback if present."""
        self._log.debug("stop() called")

        with self._start_lock:
            self._generation += 1
            self._pending = False
        self._player.stop()

        if self._done_callback:
            self._log.debug("Invoking done_callback due to stop()")
            try:
                self._done_callback()
            finally:
                self._done_callback = None

    @property
    def is_playing(self) -> bool:
        """Check if the player is currently playing or paused."""
        state = self._player.state()
        return self._pending or state in (
            PlayerState.PLAYING, PlayerState.PAUSED, PlayerState.LOADING
        )

    def set_volume(self, volume: float) -> None:
        """
        Set playback volume.

        Args:
            volume: Volume in percent (0.0-100.0).
        """
        self._log.debug("set_volume(volume=%.2f)", volume)
        self._player.set_volume(volume)

    def duck(self, factor: float = 0.5) -> None:
        """
        Temporarily reduce volume.

        Args:
            factor: Volume multiplier (0.0-1.0).
        """
        self._log.debug("duck(factor=%.2f)", factor)
        self._player.duck(factor)

    def unduck(self) -> None:
        """Restore volume after ducking."""
        self._log.debug("unduck() called")
        self._player.unduck()
